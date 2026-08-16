# -*- coding: utf-8 -*-
"""애널리스트 전망(한경컨센서스) 수집기 + KRX 확정 종가 궤적 (하루 1회 배치, 저강도).

검증된 로직(analyst_proto/hk_collect.py, prep_data.py) 이식.

핵심 함정(필수 준수):
  - 한경 검색은 종목명 부분일치가 변덕스러워 정식명은 실패하고 부분명은 성공한다.
    → 반환행 제목에 박힌 (6자리코드) 로 타깃종목을 재검증한다(row_code==code 만 채택).
  - 정본 도메인은 consensus.hankyung.com. 요청 간 >=1초 슬립(저강도), 하루 1회.
  - 가격은 fdr(FinanceDataReader → 네이버 fchart)이 주는 KRX 공식 일별시세만 쓴다.
    토스 일봉 종가는 NXT 애프터마켓 20:00 최종 체결가라 정규장 확정 종가가
    아니다(12종목×99일 중 85.9% 불일치, 최대 12.91%). 확정 종가 용도로
    토스를 되돌리지 말 것.

대상 = 시총 Top100(top100.json) ∪ 최근 /api/ranking 등장 코드(로컬 서버 가동 시 best-effort).
종목별 payload: reports(목표가>0 & window 이후) + prices(fdr 일봉 종가) + current + avg_tp
             + n_total + n_tp + window_start.

저장: Supabase analyst_consensus(code upsert) + 로컬 data/analyst_cache.json(폴백).
모든 네트워크/파싱 실패는 종목 단위로 격리(한 종목 실패가 배치를 멈추지 않음).
다만 가격 0행은 저장을 건너뛰어 기존 행을 보존하고(가격 전량 소실 방지),
리포트는 '진짜 0건'과 '조회 실패'를 구분해 실패일 때만 기존 리포트를 보존한다
(WS ②). 실패율이 임계를 넘으면 배치를 중단·경보한다 — 조용한 실패 금지."""
import hashlib
import re
import signal
import sys
import time
from datetime import date, datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

import config
import miri_cache as mc

# 가격: FinanceDataReader(네이버 fchart) = KRX 공식 일별시세. 이 배치는 더 이상
# kis-trading 경로·토스 토큰에 의존하지 않는다(배포환경 패리티 개선).
try:
    import FinanceDataReader as _fdr
except Exception as _e:  # noqa: BLE001
    _fdr = None
    print(f"[analyst] FinanceDataReader import 실패(가격 생략): {type(_e).__name__}",
          file=sys.stderr)

_HK_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
         "Referer": "https://consensus.hankyung.com/"}
_HK_LIST = "https://consensus.hankyung.com/analysis/list"
_TABLE = "analyst_consensus"
_CACHE_FILE = config.DATA / "analyst_cache.json"
_CORP_INDEX_FILE = config.DATA / "corp_index.json"
_ALIAS_FILE = config.DATA / "corp_alias.json"
_ALERT_JOURNAL = config.DATA / "analyst_alerts.json"   # 경보 내구 기록(텔레그램 실패 대비)
_DISCLAIMER = "증권사 전망을 정리한 참고 자료이며 투자 권유가 아닙니다"

_DDL = """
CREATE TABLE IF NOT EXISTS analyst_consensus (
  code text PRIMARY KEY,
  name text,
  current bigint,
  avg_tp bigint,
  n_total int,
  n_tp int,
  window_start text,
  updated_at text,
  prices jsonb,
  reports jsonb
);"""


# ----------------- S0 가드: 조용한 실패 차단 -----------------
# 실패율 임계(초과 시 배치 중단 + 경보). 정상 조건의 실패율은 실측 0%였으므로
# 연속 실패는 개별 종목 이슈가 아니라 소스 장애·차단 같은 전역 사유일 확률이
# 높다. 그 상태로 루프를 끝까지 돌면 피해가 종목 수에 비례해 커진다.
_ABORT_FAIL_RATE = 0.20
# 임계 판정에 필요한 최소 실패 건수. 상장폐지·코드변경 같은 산발 실패 1~2건으로
# 배치가 죽지 않게 하는 하한. 전역 장애라면 첫 3건이 연속 실패해 즉시 걸린다.
_ABORT_MIN_FAILS = 3
# 리포트 조회 실패 경보 임계(중단은 하지 않는다 — 가격은 정상 갱신되므로).
# 개별 종목의 산발 실패는 재시도로 흡수되지만, 비율이 높으면 소스 전역 장애다.
_REPORT_ALERT_RATE = 0.20
_REPORT_ALERT_MIN = 3


class PriceUnavailable(Exception):
    """가격 0행 신호. 해당 종목의 저장을 건너뛰어 기존 행을 보존한다.

    fdr/toss 모두 '유효한 6자리지만 데이터 없음'에 예외가 아니라 빈 결과를
    돌려준다. 그래서 try/except 만으로는 이 실패가 잡히지 않는다 — 명시적으로
    던져서 저장 경로에서 반드시 갈라지게 한다."""


def _alert(text):
    """운영 경보 발송(부분실패/중단). 실패해도 배치를 죽이지 않는다.

    채널은 기존 헬퍼 notify_alert._tg_send 재사용. 그 헬퍼는 config.TEST_CHAT_ID
    (운영자 본인 채팅)로만 보내도록 이미 고정돼 있어, 실유저 공시채널
    (tg_channel.py / TG_CHANNEL_ID)과 절대 섞이지 않는다 — 배치 경보를 구독자
    채널로 브로드캐스트하는 사고를 구조적으로 막는다. import 는 지연 로딩
    (notify_alert -> dart_poll 의존을 배치 임포트 시점에 끌어오지 않기 위함).
    """
    print("[analyst][ALERT] " + text, file=sys.stderr)
    sent, err = False, None
    try:
        from notify_alert import _tg_send
        sent = bool(_tg_send("[MIRI 배치경보] analyst_collect\n" + text))
        if not sent:
            print("[analyst] 텔레그램 미발송(토큰/채널 미설정) — 저널로만 표면화",
                  file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        err = type(e).__name__
        print(f"[analyst] 경보 발송 실패: {err}", file=sys.stderr)
    # ★경보 저널(내구 신호). 실측 2026-08-16: 배포 환경(Render/리눅스)에는
    # TELEGRAM_TOKEN·CHAT_ID 가 없고 config.py 가 폴백으로 읽는 kis-trading\.env 경로도
    # 없다 → notify_alert._tg_send 가 «첫 줄에서 return False». 예외도 로그도 재시도도
    # 없어 신선도 경보 6회가 «아무 데도 안 갔다». 텔레그램 한 갈래에만 의존하면 경보는
    # 조용히 증발한다. 전송 성패와 무관하게 파일로 남겨, 채널이 죽어도 사후 확인이
    # 가능하게 한다(최근 50건 링버퍼).
    try:
        j = mc.load_json(_ALERT_JOURNAL, default=[]) or []
        if not isinstance(j, list):
            j = []
        j.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "tg_sent": sent, "tg_error": err, "text": text})
        mc.save_json(_ALERT_JOURNAL, j[-50:])
    except Exception:  # noqa: BLE001
        pass


# ----------------- 종목명↔코드 마스터 + 별칭 -----------------
def _load_corp_index():
    rows = mc.load_json(_CORP_INDEX_FILE, default=[]) or []
    return rows if isinstance(rows, list) else []


def build_name_map():
    """code→name, name→code(+별칭) 조회 맵. corp_alias.json 이 있으면 별칭 병합."""
    idx = _load_corp_index()
    code2name, name2code = {}, {}
    for r in idx:
        if not isinstance(r, dict):
            continue
        code = str(r.get("code") or "").strip()
        name = str(r.get("name") or "").strip()
        if code:
            code2name[code] = name
        if name and code:
            name2code.setdefault(name.lower(), code)
    alias = mc.load_json(_ALIAS_FILE, default={}) or {}
    if isinstance(alias, dict):
        for a, c in alias.items():
            if a and c:
                name2code.setdefault(str(a).strip().lower(), str(c).strip())
    return code2name, name2code


def resolve_code(term, name2code):
    """별칭/구명/영문명 → 코드. 6자리 숫자면 그대로. 없으면 None."""
    t = (term or "").strip()
    c = norm_stock_code(t)          # 소문자 입력도 대문자로 정규화해 반환(app.py 와 동일)
    if c:
        return c
    return name2code.get(t.lower())


# ----------------- 한경 리포트 수집(코드 재검증) -----------------
# 리포트 조회 결과 3분기. '리포트 0건'과 '조회 실패'를 절대 같은 값으로 표현하지
# 않는다 — 둘을 섞으면 한경 장애 시 reports=[] 가 정상값으로 저장돼 기존 리포트를
# 지운다(주 1회 배치라 최대 7일 공백). 반대로 0건을 실패로 오인하면 리포트가 진짜
# 없는 소형주가 영구 스킵된다. 그래서 상태를 값과 분리해 명시적으로 돌려준다.
REPORTS_OK = "ok"                  # 리포트를 1건 이상 얻음
REPORTS_NET_FAIL = "net_fail"      # 요청 자체 실패(예외/타임아웃/비200/빈 본문)
REPORTS_PARSE_FAIL = "parse_fail"  # 200 이지만 우리가 아는 표가 아님(구조 변경·차단페이지)
# ★2026-08-16 신설. 종전엔 '조회 성공 + 0건'을 REPORTS_OK 로 뭉뚱그렸다. 그래서
# '이 종목은 리포트가 없다'와 '검색어가 안 맞아 0건이 나왔다'가 같은 값이 됐고,
# 실제로 SK바이오팜(13건 실재)이 n_total=0 으로 «조용히» 저장됐다.
# 조직 규율 「「0건」을 부재의 증거로 삼지 마라」의 프로덕션 재현이었다.
# 이제 0건은 별도 상태로 올라오고, 저장부(build_payload)가 기존 리포트와 대조해
# '있다가 0이 된' 이상 케이스를 보존·경보한다.
REPORTS_ZERO = "zero"              # 페이지 정상 파싱 + 결과 0건(= 리포트 부재 «후보»)

# 목록표 헤더의 '위치까지' 검증한다. fetch_reports 는 tds[0]=작성일 tds[2]=적정가격
# tds[5]=제공출처 처럼 인덱스로 읽으므로, 컬럼 순서가 바뀌면 파싱은 성공한 척하면서
# 목표가 자리에 엉뚱한 값이 들어간다. 헤더 위치 검증이 그 조용한 오염을 막는다.
# (실측 2026-07-28: 정상/결과0건 페이지 모두 동일 thead 를 내려준다)
_HK_HEAD_EXPECT = {0: "작성일", 1: "제목", 2: "적정가격", 3: "투자의견", 5: "제공출처"}

# ★종목 단축코드는 더 이상 '숫자 6자리'가 아니다(실측 2026-08-16).
#   라이브 top100 rank 68 = 0126Z0 삼성에피스홀딩스 — 영문자가 섞인 신형식.
#   한경 리포트 제목도 그대로 '삼성에피스홀딩스(0126Z0) ...' 로 내려온다.
# 종전 r"\((\d{6})\)" 재검증 정규식은 이런 코드에 «영원히» 매치되지 않는다.
#   → 모든 반환행이 기각 → n_total=0 «고착». 조회가 되는데도 리포트가 없는 것처럼 보인다.
#   (실측: term='삼성에피스홀딩스' → 2행 수신, \d{6} 매치 None, [0-9A-Z]{6} 매치 0126Z0)
# 오탐 위험: 괄호 안 6자 영숫자가 코드가 아닐 수 있으나, 아래 사용처는 전부
# 'row_code == 타깃코드' 동등비교와 함께 쓰므로 잘못된 행이 «채택»되는 경로는 없다.
_CODE_CHARS = r"[0-9A-Z]{6}"
_TITLE_CODE_RE = re.compile(r"\((" + _CODE_CHARS + r")\)")
_TITLE_CODE_STRIP_RE = re.compile(r"\(" + _CODE_CHARS + r"\)\s*")


_STOCK_CODE_RE = re.compile(r"[0-9A-Z]{6}")


def norm_stock_code(code):
    """종목코드 입력 → 정규화(대문자) 코드. 형식 위반이면 "" 반환(예외 없음).

    ★app.py:64 norm_stock_code 와 «동일 계약»이다. 한쪽만 다르면 '검색은 되는데
    조회는 비는' 절반 파손이 난다 — 두 함수는 반드시 같이 움직여야 한다.
    소문자를 거부하지 않고 «정규화»하는 근거(app.py 주석과 동일 실측):
      DART corp_map 3,976종 전수 소문자 0건(영문 포함 53건 전부 대문자),
      build_corp_index.py 도 ^[0-9A-Z]{6}$ 파싱, 코드 문자열이 dict/upsert 키라
      소문자 통과 시 조용한 조회 미스·캐시 분열이 생긴다.
    ★ASCII 한정(r"\\d" 아님): 파이썬 \\d 는 유니코드 숫자 4계열(아랍-인도·전각·
      데바나가리·벵골)을 통과시킨다(실측). [0-9] 로 명시해 막는다."""
    c = (code or "").strip().upper()
    return c if _STOCK_CODE_RE.fullmatch(c) else ""


def is_stock_code(t):
    """단축코드 형식인가(정규화 후 판정). 숫자 6자리 + 문자 혼합 신형식(0126Z0) 허용."""
    return bool(norm_stock_code(t))


# 한 페이지에 받는 행 수. 종전 40 은 «조용한 절단»이었다 —
# 실측 2026-08-16(005930, 2025-01-01~): pagenum=40 → 40행(잘림) / 100 → 65행 / 200 → 65행.
# 즉 40 은 리포트가 많은 대형주에서 25건을 통째로 버리고 있었고, 그만큼 avg_tp 집계창이
# 좁아졌다. 100 으로 올려도 «요청 수는 그대로»라 상대 서버 부담은 늘지 않는다.
_HK_PAGENUM = 100


def _rows(term, sdate, edate, pagenum=_HK_PAGENUM):
    """(데이터행 리스트, 상태). 예외를 밖으로 던지지 않는다.

    상태는 REPORTS_OK(행 있음) / REPORTS_ZERO(정상 파싱·0행) /
    REPORTS_NET_FAIL / REPORTS_PARSE_FAIL 중 하나."""
    from bs4 import BeautifulSoup
    p = {"sdate": sdate, "edate": edate, "now_page": 1, "search_value": "BUSINESS",
         "report_type": "CO", "pagenum": pagenum, "business_code": "",
         "order_type": "", "search_text": term}
    try:
        r = requests.get(_HK_LIST, params=p, headers=_HK_H, timeout=25)
    except Exception as e:  # noqa: BLE001
        print(f"[analyst] 한경 요청 실패 term='{term}': {type(e).__name__}", file=sys.stderr)
        return [], REPORTS_NET_FAIL
    if r.status_code != 200 or not (r.text or "").strip():
        print(f"[analyst] 한경 비정상 응답 term='{term}': HTTP {r.status_code} "
              f"len={len(r.text or '')}", file=sys.stderr)
        return [], REPORTS_NET_FAIL
    r.encoding = "utf-8"
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        head = [th.get_text(strip=True) for th in soup.select("table thead th")]
    except Exception as e:  # noqa: BLE001
        print(f"[analyst] 한경 파싱 실패 term='{term}': {type(e).__name__}", file=sys.stderr)
        return [], REPORTS_PARSE_FAIL
    if not head or any(len(head) <= i or head[i] != want
                       for i, want in _HK_HEAD_EXPECT.items()):
        print(f"[analyst] 한경 표 구조 불일치 term='{term}': head={head[:8]}",
              file=sys.stderr)
        return [], REPORTS_PARSE_FAIL
    rows = [tr for tr in (soup.select("table tbody tr") or soup.select("table tr"))
            if len(tr.find_all("td")) >= 6
            and tr.find_all("td")[0].get_text(strip=True)[:4].isdigit()]
    return rows, (REPORTS_OK if rows else REPORTS_ZERO)


def fetch_reports(code, name, sdate="2025-01-01", edate=None, with_status=False,
                  alt_names=None):
    """타깃종목 리포트 전량(목표가 0 포함). row_code==code 재검증. 날짜 오름차순.

    ★★검색어를 «종목코드»로 대체(2026-08-16). 이름 문자열 매칭은 근본적으로 깨져 있었다.
    이 배치의 name 은 corp_index.json = DART 정식명인데, 한경 리포트 제목은 «시장
    통용명»을 쓴다. 어긋나면 0건이 나오고 상태는 정상이라 '리포트가 없는 종목'으로
    조용히 저장된다. 실측(2026-08-16, top100 중 7종목 이름 불일치):
        에스케이바이오팜 0건 / SK바이오팜   13건   (326030 — President 신고 종목)
        케이티          0건 / KT           21건   (030200)
        케이티앤지      0건 / KT&G         16건   (033780)
        엘에스일렉트릭  0건 / LS ELECTRIC  19건   (010120)
        현대자동차      4건 / 현대차       39건   (005380 — 부분 소실 → avg_tp 오염)
    이름 정규화·별칭표는 땜질이다(신규·개명 종목마다 재발). 그래서 «검색어 자체»를
    바꿨다 — 한경 검색은 6자리 종목코드를 그대로 받는다(실측):
        search_text='326030' → 13행, 전부 326030
        search_text='030200' → 40행, 전부 030200 (이름 'KT' 는 21행 — 코드 쪽이 우월)
        search_text='005930' → 65행, 전부 005930
      즉 «정확»(오탐 0)하고 «완전»(이름 검색의 상위집합)하며 이름 변화에 불변이다.
      요청 수도 줄어든다: 종전 1~3회(이름·프리픽스) → 1회.

    검색 티어(하위 티어는 상위가 0건일 때만 — 실패 시 안전망):
        T1 종목코드  →  T2 정식명·표시명  →  T3 이름 프리픽스(3자/2자)
    T2/T3 는 코드 검색이 언젠가 막힐 때를 위한 폴백일 뿐 정상경로가 아니다.

    with_status=True 면 (리포트리스트, 상태) 튜플. 상태 판정:
      - 1건이라도 얻으면 REPORTS_OK
      - 0건 + 조회 실패 신호 있으면 그 실패상태(보수적)
      - 0건 + 전 시도 정상이면 REPORTS_ZERO ('부재 후보' — «부재 확정이 아니다»)
    with_status=False(기본)면 종전과 동일하게 리스트만 반환(하위호환)."""
    if edate is None:
        edate = (date.today() + timedelta(days=1)).isoformat()
    names = []
    for n in [name] + list(alt_names or []):
        n = (n or "").strip()
        if n and n not in names:
            names.append(n)
    # T1 = 종목코드(정상경로). 6자리 숫자일 때만 — 신형 코드(예: 0126Z0)는 이름으로 간다.
    # T1 = 종목코드. ★단 «숫자 6자리일 때만» 유효하다 — 실측 2026-08-16:
    #   search_text='326030' → 13행 / '005930' → 65행 (숫자코드는 검색됨)
    #   search_text='0126Z0' → 0행   (문자 포함 신형식은 검색 «안 됨»)
    # 그래서 문자코드 종목은 tier1 을 비우고 이름(T2)으로 간다. 재검증 정규식은
    # _TITLE_CODE_RE 로 문자코드까지 받으므로 채택 단계에서 더는 기각되지 않는다.
    # ★r"\d" 를 쓰지 않는다. 파이썬 \d 는 «유니코드 숫자»를 통과시킨다
    # (실측 2026-08-16: re.fullmatch(r"\d{6}", "٠١٢٣٤٥") / "０１２３４５" 둘 다 True).
    # 그런 문자열을 한경 검색어로 보내면 조회는 되고 결과는 0건이라 또 조용히 0이 된다.
    # 문자는 «넓히되»(0126Z0) 숫자는 «ASCII 로 좁힌다».
    tier1 = [code] if re.fullmatch(r"[0-9]{6}", str(code or "")) else []
    tier2 = [n for n in names if n != code]
    tier3 = []
    for n in tier2:
        if len(n) >= 3:
            for p in (n[:3], n[:2]):
                if p and p not in tier2 and p not in tier3:
                    tier3.append(p)
    if not (tier1 or tier2):
        return ([], REPORTS_ZERO) if with_status else []
    strip_re = re.compile(r"^(" + "|".join(re.escape(n) for n in (names or [code])) + r")+")
    seen, out = set(), []
    state = {"net": False, "parse": False, "req": 0, "ceiling": False}

    def _scan(term):
        """단일 검색어 조회 → out 에 병합. 요청 간 >=1초 간격을 여기서 보장한다."""
        if state["req"]:
            time.sleep(1.0)
        state["req"] += 1
        rows, st = _rows(term, sdate, edate)
        # ★천장 접촉 인바리언트. 반환행이 pagenum 과 «같으면» 잘렸을 수 있다.
        # 종전 40 이 정확히 이 상태였고(캐시 110종 중 n_total>40 이 0건 = 천장 자국),
        # 삼성전기가 하필 '40'으로 저장돼 자연값처럼 보였다. 상한을 올린 뒤에도
        # 같은 함정이 재발하지 않도록 «상시» 감시한다.
        if len(rows) >= _HK_PAGENUM:
            state["ceiling"] = True
        if st == REPORTS_NET_FAIL:
            state["net"] = True
        elif st == REPORTS_PARSE_FAIL:
            state["parse"] = True
        for tr in rows:
            tds = tr.find_all("td")
            rdate = tds[0].get_text(strip=True)
            title = tds[1].get_text(" ", strip=True)
            m = _TITLE_CODE_RE.search(title)
            row_code = m.group(1) if m else ""
            if row_code != code:                # ★ 코드 재검증 — 타깃종목만
                continue
            tp = tds[2].get_text(strip=True).replace(",", "")
            opinion = tds[3].get_text(strip=True)
            broker = tds[5].get_text(strip=True)
            title_clean = _TITLE_CODE_STRIP_RE.sub("", title)
            # 제목 3중 반복 렌더 제거
            half = title_clean[:len(title_clean)//3] if len(title_clean) > 30 else title_clean
            # 제목 앞 중복 종목명 제거
            half = strip_re.sub("", half).strip()
            key = (rdate, broker, tp)
            if key in seen:
                continue
            seen.add(key)
            try:
                tp_val = int(tp)
            except Exception:
                tp_val = 0
            out.append({"date": rdate, "title": half[:45],
                        "target_price": tp_val, "opinion": opinion, "broker": broker})

    for tier in (tier1, tier2, tier3):
        for term in tier:
            _scan(term)
            if out and tier is not tier2:
                break        # T1/T3 는 히트 즉시 종료
        if out:
            break            # 상위 티어가 잡았으면 하위 티어는 안 돈다
    saw_net_fail, saw_parse_fail = state["net"], state["parse"]
    out.sort(key=lambda x: x["date"])
    if out:
        status = REPORTS_OK
    elif saw_net_fail:
        status = REPORTS_NET_FAIL
    elif saw_parse_fail:
        status = REPORTS_PARSE_FAIL
    else:
        # 전 시도 정상 파싱 + 0건. ★'부재 확정'이 아니라 '부재 후보'다.
        # 확정 판정은 호출부가 기존 데이터와 대조해서 내린다(build_payload).
        status = REPORTS_ZERO
    if state["ceiling"]:
        print(f"[analyst] {code} ★천장 접촉: 한 페이지가 pagenum({_HK_PAGENUM})을 채웠다 "
              f"— n_total={len(out)} 은 «절단됐을 수 있다»(신뢰불가)", file=sys.stderr)
    fetch_reports.last_ceiling = state["ceiling"]
    return (out, status) if with_status else out


# 최근 N거래일로 창을 고정한다. window_start=prices[0][0] 이 avg_tp 집계창을
# 결정하므로(4-1) 행 수가 바뀌면 평균 목표주가·상승여력이 함께 움직인다.
# 실측상 100거래일 ≈ 146일이라 조회는 160일 여유로 잡고 tail 로 자른다.
_PRICE_DAYS = 100
_PRICE_LOOKBACK_DAYS = 160

# app.py:1886 _expected_trade_day() 와 동일 규칙의 사본(16:00 KST 전환).
# 서버와 배치가 같은 정의를 공유해야 한다 — 한쪽만 바꾸지 말 것.
# TODO(별건 게이트): 공용 모듈로 추출해 이 사본을 없앨 것.
_KST = timezone(timedelta(hours=9))
_MKT_CLOSE_MIN = 16 * 60   # 15:30 마감 + 정산 여유 → 16:00(KST) 이후를 확정으로 본다


def _expected_trade_day(now=None):
    """지금 시점에 '있어야 할 최신 종가'의 거래일(KST, YYYY-MM-DD).

    평일 16:00(KST) 이후면 오늘, 그 전이면 직전 영업일. 주말은 직전 금요일.
    공휴일은 판정하지 않는다(달력 없음) — 그 오차는 실제 데이터가 흡수한다."""
    now = now or datetime.now(_KST)
    d = now.date()
    after_close = (now.hour * 60 + now.minute) >= _MKT_CLOSE_MIN
    if not (d.weekday() < 5 and after_close):
        d -= timedelta(days=1)
    while d.weekday() >= 5:        # 토(5)·일(6) → 직전 금요일
        d -= timedelta(days=1)
    return d.isoformat()


def _prices(code):
    """fdr 일봉 → [[YYYY-MM-DD, close_int], ...] 오름차순. 실패 시 [].

    반환 계약은 종전과 동일(문자열 날짜 + 정수 종가, 오름차순, 최근 100거래일).
    호출부(build_payload)는 [] 를 PriceUnavailable 로 승격시켜 저장을 건너뛴다.

    실패 판정 주의: fdr 은 '유효한 6자리지만 데이터 없음'(예: 999999)에 예외를
    던지지 않고 빈 DataFrame 을 준다. 배치 코드는 전부 6자리라 try/except 만으론
    이 실패가 절대 잡히지 않는다 — len(df)==0 을 명시적 실패로 취급한다.
    """
    if _fdr is None:
        return []
    # 조회창은 KST 기준으로 잡는다. 이 배치는 현재 로컬(KST)에서 schtasks 로 돌지만,
    # UTC 환경(컨테이너·해외 리전)으로 이관하면 date.today() 가 00~09시 KST 구간에서
    # 하루 밀려 최신 거래일이 창 밖으로 떨어진다. 미리 고정해 둔다.
    end = datetime.now(_KST).date()
    start = end - timedelta(days=_PRICE_LOOKBACK_DAYS)
    try:
        df = _fdr.DataReader(code, start.isoformat(), end.isoformat())
    except Exception as e:  # noqa: BLE001
        print(f"[analyst] {code} fdr 실패: {type(e).__name__}", file=sys.stderr)
        return []
    if df is None or len(df) == 0 or "Close" not in df:
        print(f"[analyst] {code} fdr 빈 응답(0행) — 실패로 판정", file=sys.stderr)
        return []
    exp = _expected_trade_day()
    out = []
    for d, c in df["Close"].items():
        try:
            ds = d.strftime("%Y-%m-%d")
            if ds > exp:      # 장중 미확정 행(= 그 순간 체결가) 유입 차단
                continue
            v = round(float(c))
        except Exception:
            continue
        if v > 0:
            out.append([ds, v])
    out.sort(key=lambda r: r[0])   # 오름차순 계약을 소스와 무관하게 보장
    if not out:
        print(f"[analyst] {code} fdr 유효행 0(전량 필터됨) — 실패로 판정",
              file=sys.stderr)
    elif len(out) < _PRICE_DAYS:
        # 창이 짧아지면 window_start 가 늦어져 avg_tp 집계 대상 리포트가 줄어든다.
        # 조용히 지나가면 컨센서스 지표가 이유 없이 움직인 것처럼 보인다.
        print(f"[analyst] {code} fdr {len(out)}행 < {_PRICE_DAYS} — window_start 가 "
              "평소보다 늦다(신규상장/조회창 부족). avg_tp 집계창 축소 주의",
              file=sys.stderr)
    return out[-_PRICE_DAYS:]


def _prev_row(code, cache=None):
    """기존 저장행(Supabase 우선 → 로컬 캐시 폴백). 읽기 전용. 없으면 None.

    리포트 조회가 '실패'로 판정된 종목에서만 호출한다(정상 경로엔 추가 조회 0)."""
    try:
        ok, row = mc.select_one(_TABLE, code)
        if ok and isinstance(row, dict) and row:
            return row
    except Exception as e:  # noqa: BLE001
        print(f"[analyst] {code} 기존행 조회 실패: {type(e).__name__}", file=sys.stderr)
    row = (cache or {}).get(code) if isinstance(cache, dict) else None
    return row if isinstance(row, dict) else None


def build_payload(code, name, prev_loader=None, alt_names=None):
    """단일 종목 payload(계약 준수). 가격 0행이면 PriceUnavailable 을 던진다.

    가격은 저장 계약의 핵심이라 실패를 삼키지 않는다. 빈 prices 를 그대로 올리면 upsert 가
    merge-duplicates 라 기존 100행이 빈 배열로 교체된다 — 오염이 아니라 전량
    소실이다. 또한 window_start 의 "2025-01-01" 폴백은 리포트 창을 급팽창시켜
    avg_tp 를 흔들었다. 두 경로를 여기서 함께 닫는다.

    ★리포트 보존(②): fetch_reports 가 '0건'과 '조회 실패'를 구분해 돌려주므로
    여기서 갈라진다.
      - 0건(OK)  : 종전대로 reports=[] 저장. 리포트가 진짜 없는 소형주가 정상 반영된다.
      - 실패(NET/PARSE): 기존 행의 리포트를 그대로 되실어 저장한다. 즉 upsert 가
        merge-duplicates 여도 기존 리포트가 같은 값으로 덮이므로 소실이 없다.
        (컬럼 생략에 의존하지 않는다 — PostgREST 의 부분컬럼 동작에 기대면 검증
        불가능한 가정이 남는다. 값을 명시적으로 되쓰는 쪽이 결정적이다.)
        가격은 이 경우에도 정상 갱신한다 — 리포트 장애로 종가까지 멈출 이유가 없다.
      - 실패인데 기존 행도 없으면(신규 종목) 보존할 게 없으므로 빈 리포트로 저장하고
        상태만 표면화한다(가격은 살린다).

    보존 시 avg_tp/n_tp 는 '보존된 리포트 ∩ 새 window_start' 로 재계산한다. window_start
    는 새 prices 에서 나오므로, 옛 avg_tp 를 그대로 두면 창과 지표가 어긋난다.
    n_total(목표가 0 포함 전체 건수)은 원본 조회가 실패해 재계산이 불가하므로 직전 값을
    유지한다 — 추정하지 않는다.

    반환 payload 의 '_' 로 시작하는 키는 관측용 메타이며 저장 직전 save_one 이 제거한다
    (테이블에 없는 컬럼을 올리면 PostgREST 가 400 을 낸다).
    """
    prices = _prices(code)
    if not prices:
        raise PriceUnavailable(f"{code} 가격 0행")
    window_start = prices[0][0]
    current = prices[-1][1]
    all_reports, rstatus = fetch_reports(code, name, with_status=True,
                                         alt_names=alt_names)
    ceiling = bool(getattr(fetch_reports, "last_ceiling", False))
    # ★집계창 검산. window_start 는 prices 에서 나오고(최근 100거래일), 리포트 조회창은
    # sdate=2025-01-01 부터라 «훨씬 넓다». 따라서 조회된 최고령 리포트는 정상이라면
    # window_start «이전»이어야 한다. 그보다 늦다면 앞부분이 조회조차 안 된 것이고
    # (실측 사례: 034730 SK — win=2026-03-23 인데 최고령 행 2026-05-18, 앞 56일 미조회),
    # 그 상태로 계산한 avg_tp 는 창과 어긋난 값이다.
    oldest = min((str(r.get("date") or "") for r in all_reports), default="")
    window_gap = bool(all_reports and oldest > window_start)
    if window_gap:
        print(f"[analyst] {code} ★집계창 결손: 조회 최고령 {oldest} > window_start "
              f"{window_start} — 창 앞부분이 미조회(avg_tp 신뢰불가)", file=sys.stderr)
    preserved = False
    zero_conflict = False
    if not all_reports and rstatus != REPORTS_OK:
        prev = prev_loader() if callable(prev_loader) else _prev_row(code)
        prev_reports = [r for r in ((prev or {}).get("reports") or [])
                        if isinstance(r, dict)]
        prev_n_total = int((prev or {}).get("n_total") or 0)
        # ★REPORTS_ZERO + 직전엔 리포트가 있었음 = 「0건」을 부재로 받아들이면 안 되는 상황.
        # 리포트 조회창은 sdate 고정(2025-01-01)이라 n_total 은 «단조 증가»한다. 있다가
        # 0이 되는 것은 종목의 사실이 아니라 «우리 조회가 바뀐 것»이다(검색어 불일치·
        # 소스 사양변경). 덮어쓰지 않고 보존한 뒤 별도 신호로 올린다.
        if rstatus == REPORTS_ZERO and (prev_reports or prev_n_total > 0):
            zero_conflict = True
        if prev_reports:
            preserved = True
            tp_reps = sorted((r for r in prev_reports
                              if int(r.get("target_price") or 0) > 0
                              and str(r.get("date") or "") >= window_start),
                             key=lambda x: str(x.get("date") or ""))
            n_total = prev_n_total or len(prev_reports)
            tag = "0건 응답(부재 후보)" if rstatus == REPORTS_ZERO else f"조회 실패({rstatus})"
            print(f"[analyst] {code} 리포트 {tag} → 기존 {len(prev_reports)}건 "
                  f"보존(창 내 {len(tp_reps)}건). 가격만 갱신"
                  f"{' ★ZERO_CONFLICT' if zero_conflict else ''}", file=sys.stderr)
        else:
            tp_reps, n_total = [], 0
            if rstatus == REPORTS_ZERO:
                # 보존할 것도 없고 조회도 정상 → 여기서만 '리포트 없음'을 확정한다.
                print(f"[analyst] {code} 리포트 0건 확정(조회 정상·기존 데이터 없음)",
                      file=sys.stderr)
            else:
                print(f"[analyst] {code} 리포트 조회 실패({rstatus}) + 보존할 기존 리포트 없음 "
                      "→ 빈 리포트로 저장(가격은 갱신)", file=sys.stderr)
    else:
        n_total = len(all_reports)
        # 목표가>0 & window_start 이후만, 날짜 오름차순
        tp_reps = [r for r in all_reports
                   if r.get("target_price", 0) > 0 and r["date"] >= window_start]
        tp_reps.sort(key=lambda x: x["date"])
    tps = [int(r.get("target_price") or 0) for r in tp_reps]
    avg_tp = round(sum(tps) / len(tps)) if tps else None
    return {
        "code": code,
        "name": name,
        "current": current,
        "avg_tp": avg_tp,
        "n_total": n_total,
        "n_tp": len(tp_reps),
        "window_start": window_start,
        "updated_at": date.today().isoformat(),
        "prices": prices,
        "reports": tp_reps,
        "disclaimer": _DISCLAIMER,
        "_reports_status": rstatus,
        "_reports_preserved": preserved,
        "_zero_conflict": zero_conflict,
        "_ceiling": ceiling,
        "_window_gap": window_gap,
        "_oldest_report": oldest,
    }


# ----------------- 대상 코드 산출 -----------------
def display_names():
    """code → 시장 통용 표시명(top100.json 의 네이버 종목명). 없으면 {}.

    corp_index(=DART 정식명)와 어긋나는 종목의 한경 조회 실패를 막는 보조 검색어다.
    fetch_reports 의 alt_names 로 들어간다."""
    snap = mc.load_json(config.DATA / "top100.json", default={}) or {}
    out = {}
    for it in (snap.get("items") or []):
        if not isinstance(it, dict):
            continue
        c = str(it.get("code") or "").strip()
        n = str(it.get("name") or "").strip()
        if c and n:
            out[c] = n
    return out


def target_codes(limit=None, extra=None):
    """top100.json 코드 ∪ 최근 랭킹 코드(로컬 서버 best-effort) ∪ extra."""
    codes = []
    seen = set()

    def add(c):
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)

    # 관심종목(watchlist.json) 우선 — 최근 공시가 없어 top100/ranking 에 안 잡히는 종목도
    # 컨센서스/종가 그래프가 뜨도록 항상 수집 대상에 포함(항목8). Supabase 백엔드면 로컬
    # 스냅샷 best-effort(없으면 skip). limit 앞에 두어 잘려나가지 않게 최우선 배치.
    try:
        wl = mc.load_json(config.WATCHLIST_FILE, default={}) or {}
        for s in (wl.get("stocks") or []):
            add((s.get("stock_code") or "") if isinstance(s, dict) else "")
    except Exception:
        pass
    snap = mc.load_json(config.DATA / "top100.json", default={}) or {}
    for it in (snap.get("items") or []):
        add(it.get("code"))
    # 최근 /api/ranking 등장 코드(서버 가동 중이면). 실패 무시.
    try:
        r = requests.get("http://127.0.0.1:8891/api/ranking?top_n=40", timeout=3)
        if r.ok:
            for it in (r.json().get("items") or []):
                add(it.get("stock_code") or it.get("code"))
    except Exception:
        pass
    for c in (extra or []):
        add(c)
    if limit:
        codes = codes[:limit]
    return codes


def save_one(payload):
    """단일 종목 upsert(Supabase) — 배치 종료 시 로컬 전량 저장은 별도.

    반환: True=업서트 시도함, False=가드로 스킵. upsert 는 merge-duplicates 라
    빈 prices/None current 를 올리면 기존 행이 지워진다. build_payload 가 이미
    막지만, 다른 호출부가 생겨도 안전하도록 저장 직전에 한 번 더 막는다.

    reports 는 여기서 보지 않는다 — 0건이 정상인 종목이 있어 단순 차단이 불가하다.
    리포트 소실 방어는 build_payload 가 '조회 실패' 신호를 받았을 때 기존 리포트를
    되실어 주는 방식으로 이미 끝나 있다(②).
    """
    row = {k: v for k, v in payload.items() if not str(k).startswith("_")}
    row.pop("disclaimer", None)  # 로컬/응답에서 상수 부착(테이블엔 미저장)
    if not row.get("prices") or row.get("current") is None:
        print(f"[analyst] {row.get('code')}: 빈 가격 — upsert 스킵(기존 행 보존)",
              file=sys.stderr)
        return False
    mc.upsert(_TABLE, [row], on_conflict="code")
    return True


# ----------------- 중단 내성: 체크포인트 · 재개 · 시그널 -----------------
# 배경(실측 2026-08-04): 배치가 72/100 종목에서 죽었다. 종료코드 3221225786
# (0xC000013A = STATUS_CONTROL_C_EXIT) + 로그 말미 리터럴 "^C". 스케줄러 종료
# 이벤트(111)도, 전원/절전 이벤트도 없었다 → 콘솔 CTRL 이벤트에 의한 수동 중단.
# 그때 «72종목분 작업이 로컬 캐시에서 통째로 증발»했다. mc.save_json 이 루프가
# 끝난 뒤 «한 번만» 호출됐기 때문이다. 아래 셋으로 닫는다.
#   ① N종목마다 캐시 체크포인트(mc.save_json 은 tmp+os.replace 라 원자적이다)
#   ② 진행 원장(analyst_progress.json) — 다음 실행이 «남은 것부터» 이어서 돈다
#   ③ SIGINT/SIGBREAK 를 잡아 «현재 종목까지 마치고» 안전 저장 후 종료
_PROGRESS_FILE = config.DATA / "analyst_progress.json"
_CHECKPOINT_EVERY = 5          # 종목 N개마다 캐시+진행원장 저장
# 진행 원장 유효기간. 이보다 오래된 원장은 무시하고 새로 전수를 돈다(주 1회 배치라
# 3일이면 '같은 회차의 재개'와 '다음 회차'를 안전하게 가른다).
_RESUME_MAX_AGE_H = 72.0

# ★인배치 양성대조. 「0건」을 부재의 증거로 삼지 않기 위한 «본 검사 앞»의 대조군이다.
# 리포트가 항상 수십 건 존재하는 종목을 먼저 조회해서, 0건이 나오면 소스가 깨진
# 것으로 보고 «한 종목도 저장하지 않고» 중단한다. 이 가드가 없으면 소스 사양변경
# 시 100종목이 전부 n_total=0 으로 덮여 데이터가 통째로 날아간다.
_CANARY_CODE = "005930"
_CANARY_MIN = 5

_interrupted = {"hit": False, "sig": None}


def _install_signal_handlers():
    """CTRL_C(SIGINT)/CTRL_BREAK(SIGBREAK) 를 «즉사»가 아니라 «플래그»로 바꾼다.

    루프는 현재 종목을 끝내고 캐시·진행원장을 저장한 뒤 빠져나간다. 다음 실행이
    이어서 돌 수 있으므로 중단이 곧 작업 소실이 아니게 된다."""
    def handler(signum, frame):   # noqa: ARG001
        _interrupted["hit"] = True
        _interrupted["sig"] = signum
        print(f"\n[analyst] 중단 신호 수신(sig={signum}) — 현재 종목 마치고 "
              "안전 저장 후 종료합니다(다음 실행이 이어서 진행)", file=sys.stderr)
    for nm in ("SIGINT", "SIGBREAK", "SIGTERM"):
        s = getattr(signal, nm, None)
        if s is None:
            continue
        try:
            signal.signal(s, handler)
        except Exception:  # noqa: BLE001
            pass          # 스레드/플랫폼 제약 — 못 걸어도 배치는 돈다


def _target_sig(codes):
    """대상 코드 집합의 지문. 대상이 바뀌면 옛 진행원장을 재사용하지 않는다."""
    return hashlib.sha1(",".join(sorted(codes)).encode("utf-8")).hexdigest()[:12]


def _load_progress(sig):
    """재사용 가능한 진행 원장이면 dict, 아니면 None. 판정 근거를 출력한다."""
    p = mc.load_json(_PROGRESS_FILE, default=None)
    if not isinstance(p, dict) or not p:
        return None
    why = None
    if p.get("finished"):
        why = "직전 회차가 완주 상태"
    elif p.get("target_sig") != sig:
        why = f"대상 집합이 바뀜({p.get('target_sig')} != {sig})"
    else:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(p["started_at"])).total_seconds() / 3600.0
            if age_h > _RESUME_MAX_AGE_H:
                why = f"원장이 {age_h:.1f}h 경과(> {_RESUME_MAX_AGE_H:g}h)"
        except Exception:  # noqa: BLE001
            why = "started_at 파싱 불가"
    if why:
        print(f"[analyst] 진행원장 미사용({why}) — 전수 재시작", file=sys.stderr)
        return None
    return p


def _save_progress(sig, total, done, failed, finished, started_at):
    mc.save_json(_PROGRESS_FILE, {
        "target_sig": sig, "total": total,
        "done": sorted(done), "failed": sorted(failed),
        "finished": bool(finished), "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def _canary_ok():
    """양성대조 1회. (통과여부, 설명). 요청 1건만 쓴다."""
    reps, st = fetch_reports(_CANARY_CODE, _CANARY_CODE, with_status=True)
    if st in (REPORTS_NET_FAIL, REPORTS_PARSE_FAIL):
        return False, f"대조군 {_CANARY_CODE} 조회 실패({st})"
    if len(reps) < _CANARY_MIN:
        return False, (f"대조군 {_CANARY_CODE} 리포트 {len(reps)}건 < 최소 {_CANARY_MIN}건 "
                       "— 소스 사양변경/차단 의심")
    return True, f"대조군 {_CANARY_CODE} {len(reps)}건 정상"


class _DryRun:
    """--dry-run 컨텍스트: 프로덕션 부작용을 «구조적으로» 차단한다.

    배경(구조 결함): 이 배치는 종목마다 Supabase 에 upsert 하므로 «검증 목적의
    1회 실행»조차 프로덕션 쓰기가 된다 → 검증이 곧 게이트 대상이 되어, 고친 것이
    도는지 확인할 방법이 없었다. 아래를 전부 무력화해 그 고리를 끊는다:
      - Supabase 쓰기(upsert/ensure_table) 및 읽기(_prev_row 의 select_one)
      - 텔레그램 경보 발송
      - 프로덕션 캐시/진행원장 파일  → *.dryrun.json 으로 우회
    차단 건수를 세어 종료 시 출력한다('막았다'를 주장이 아니라 수치로 남긴다)."""

    def __init__(self):
        self.blocked = {"upsert": 0, "ensure_table": 0, "select_one": 0, "alert": 0}
        self._saved = {}

    def __enter__(self):
        g = globals()
        self._saved = {"upsert": mc.upsert, "ensure_table": mc.ensure_table,
                       "select_one": mc.select_one, "_alert": g["_alert"],
                       "_CACHE_FILE": g["_CACHE_FILE"],
                       "_PROGRESS_FILE": g["_PROGRESS_FILE"]}

        def blk(kind, ret=None):
            def f(*a, **k):
                self.blocked[kind] += 1
                return ret
            return f

        mc.upsert = blk("upsert")
        mc.ensure_table = blk("ensure_table")
        mc.select_one = blk("select_one", (False, None))

        def dry_alert(text):
            self.blocked["alert"] += 1
            print("[analyst][DRY-ALERT] " + text.replace("\n", " | "), file=sys.stderr)

        g["_alert"] = dry_alert
        g["_CACHE_FILE"] = config.DATA / "analyst_cache.dryrun.json"
        g["_PROGRESS_FILE"] = config.DATA / "analyst_progress.dryrun.json"
        print(f"[analyst] ★DRY-RUN: Supabase 읽기/쓰기·텔레그램 차단, "
              f"파일은 {g['_CACHE_FILE'].name} / {g['_PROGRESS_FILE'].name}", file=sys.stderr)
        return self

    def __exit__(self, *exc):
        g = globals()
        mc.upsert = self._saved["upsert"]
        mc.ensure_table = self._saved["ensure_table"]
        mc.select_one = self._saved["select_one"]
        g["_alert"] = self._saved["_alert"]
        g["_CACHE_FILE"] = self._saved["_CACHE_FILE"]
        g["_PROGRESS_FILE"] = self._saved["_PROGRESS_FILE"]
        print(f"[analyst] ★DRY-RUN 차단 실적: {self.blocked}", file=sys.stderr)
        return False


def main(limit=0, sleep_sec=1.2, extra=None, resume=True, deadline_min=0.0,
         dry_run=False):
    """limit<=0 또는 None = 전수(대상 코드 전량). 기본값을 «전수»로 바꿨다.

    종전 기본값 15 는 top100 하위 종목을 매번 잘라내 그래프가 영구히 안 뜨게 만들었다.
    운영 태스크는 --limit 100 을 넘기고 있었지만, target_codes 가 watchlist·ranking
    코드를 top100 «앞»에 붙이므로 그 둘이 늘어나는 순간 top100 꼬리가 다시 잘린다.
    상한을 두는 것 자체가 커버리지를 조용히 갉아먹는 구조라 전수를 기본으로 둔다.
    (전수 소요는 종목당 ≈2.6초 — 실측 근거는 리포지토리 리포트 참조)"""
    if dry_run:
        with _DryRun():
            return main(limit=limit, sleep_sec=sleep_sec, extra=extra,
                        resume=resume, deadline_min=deadline_min, dry_run=False)
    code2name, name2code = build_name_map()
    disp = display_names()
    codes = target_codes(limit=(limit if (limit or 0) > 0 else None), extra=extra)
    if not codes:
        print("[analyst] 대상 코드 0 — top100.json 먼저 생성 필요", file=sys.stderr)
        return 1
    # ★본 검사 앞의 양성대조. 실패하면 «한 종목도 건드리지 않고» 종료한다.
    good, why = _canary_ok()
    print(f"[analyst] 양성대조: {why}", file=sys.stderr)
    if not good:
        _alert(f"양성대조 실패 — 수집을 시작하지 않았다\n{why}\n"
               "기존 데이터는 그대로다(0건 덮어쓰기 방지)")
        return 3

    _install_signal_handlers()
    sig = _target_sig(codes)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done_codes, failed_codes = set(), set()
    prev_prog = _load_progress(sig) if resume else None
    if prev_prog:
        done_codes = set(prev_prog.get("done") or [])
        failed_codes = set(prev_prog.get("failed") or [])
        started_at = prev_prog.get("started_at") or started_at
        print(f"[analyst] 재개: 이미 완료 {len(done_codes)}종목 건너뜀 "
              f"(원장 {_PROGRESS_FILE})", file=sys.stderr)
    pending = [c for c in codes if c not in done_codes]
    if not pending:
        print(f"[analyst] 재개 결과 남은 종목 0 — 이미 전수 완료(대상 {len(codes)})")
        _save_progress(sig, len(codes), done_codes, failed_codes, True, started_at)
        return 0

    mc.ensure_table(_DDL)
    cache = mc.load_json(_CACHE_FILE, default={}) or {}
    if not isinstance(cache, dict):
        cache = {}
    ok = 0
    fails = []            # [(code, name, 사유), ...] — 배치 종료 시 표면화
    rep_fails = []        # 리포트 조회 실패(가격은 성공) — 중단 사유는 아니지만 반드시 표면화
    zero_conflicts = []   # 「있다가 0건」 이상징후 — 부재로 «확정하지 않은» 건들
    zeros = 0             # 조회 정상 + 0건(부재 확정)
    ceilings = []         # 천장 접촉 — n_total 이 절단됐을 수 있는 종목
    window_gaps = []      # 집계창 앞부분 미조회 — avg_tp 신뢰불가 종목
    aborted = False
    stop_reason = None
    t_begin = time.time()

    def _checkpoint(finished=False):
        mc.save_json(_CACHE_FILE, cache)
        _save_progress(sig, len(codes), done_codes, failed_codes, finished, started_at)

    for i, code in enumerate(pending, 1):
        # 이름 결정: DART 정식명이 없으면 표시명으로 폴백(둘 다 없으면 코드).
        # 표시명은 alt_names 로도 같이 넘겨 한경 조회 누락을 막는다.
        dname = disp.get(code, "")
        name = code2name.get(code) or dname or code
        alts = [dname] if (dname and dname != name) else []
        try:
            payload = build_payload(code, name, alt_names=alts,
                                    prev_loader=lambda c=code: _prev_row(c, cache))
            cache[code] = payload
            save_one(payload)
            ok += 1
            done_codes.add(code)
            rst = payload.get("_reports_status")
            if payload.get("_ceiling"):
                ceilings.append((code, name, payload["n_total"]))
            if payload.get("_window_gap"):
                window_gaps.append((code, name, payload.get("_oldest_report"),
                                    payload["window_start"]))
            if payload.get("_zero_conflict"):
                zero_conflicts.append((code, name, payload["n_total"]))
            elif rst == REPORTS_ZERO:
                zeros += 1
            elif rst != REPORTS_OK:
                rep_fails.append((code, name, rst,
                                  bool(payload.get("_reports_preserved"))))
            print(f"[analyst] {code} {name}: n_total={payload['n_total']} "
                  f"n_tp={payload['n_tp']} avg_tp={payload['avg_tp']} "
                  f"cur={payload['current']} prices={len(payload['prices'])} "
                  f"win={payload['window_start']}", file=sys.stderr)
        except PriceUnavailable as e:
            # 가격 0행: 저장·캐시 갱신을 모두 건너뛴다(기존 행/캐시 보존).
            fails.append((code, name, "price0"))
            failed_codes.add(code)
            print(f"[analyst] {code} {name}: SKIP price0 ({e}) — 기존 행 보존",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            fails.append((code, name, type(e).__name__))
            failed_codes.add(code)
            print(f"[analyst] {code} {name}: ERR {type(e).__name__} {e}",
                  file=sys.stderr)
        if i % _CHECKPOINT_EVERY == 0:
            _checkpoint()
        if len(fails) >= _ABORT_MIN_FAILS and len(fails) / i > _ABORT_FAIL_RATE:
            aborted = True
            stop_reason = (f"실패율 임계초과 {len(fails)}/{i}"
                           f"({len(fails) / i:.0%}) > {_ABORT_FAIL_RATE:.0%}")
            print(f"[analyst] 중단: {stop_reason}", file=sys.stderr)
            break
        if _interrupted["hit"]:
            aborted = True
            stop_reason = f"중단 신호(sig={_interrupted['sig']}) — {i}/{len(pending)} 처리 후 안전 종료"
            print(f"[analyst] 중단: {stop_reason}", file=sys.stderr)
            break
        if deadline_min and (time.time() - t_begin) / 60.0 >= deadline_min:
            aborted = True
            stop_reason = (f"데드라인 {deadline_min:g}분 도달 — {i}/{len(pending)} 처리 후 "
                           "안전 종료(다음 실행이 이어서 진행)")
            print(f"[analyst] 중단: {stop_reason}", file=sys.stderr)
            break
        time.sleep(sleep_sec)
    # 로컬 전량 스냅샷 저장(폴백). 실패 종목은 cache 에 손대지 않았으므로 기존
    # 값이 그대로 남는다(merge 성격 — 여기서도 덮어쓰지 않는다).
    covered = len(done_codes)
    complete = covered >= len(codes)
    _checkpoint(finished=complete)
    done = ok + len(fails)
    head = "완주" if complete else "미완주"
    print(f"[analyst] {head}: 커버 {covered}/{len(codes)} "
          f"(이번 회차 성공 {ok} 실패 {len(fails)}), 캐시 {len(cache)}종목 -> {_CACHE_FILE}")
    print(f"[analyst] 리포트: 0건확정 {zeros} · 0건이상징후 {len(zero_conflicts)} · "
          f"조회실패 {len(rep_fails)} · 천장접촉 {len(ceilings)} · 집계창결손 {len(window_gaps)}")
    if ceilings:
        d = ", ".join(f"{c}({n}):{t}" for c, n, t in ceilings[:10])
        print(f"[analyst] ★천장접촉 {len(ceilings)}종목(n_total 절단 의심): {d}", file=sys.stderr)
        _alert(f"천장접촉 {len(ceilings)}종목 — pagenum({_HK_PAGENUM}) 을 채웠다\n{d}\n"
               "n_total 이 절단됐을 수 있다(화면 확정값으로 쓰지 말 것)")
    if window_gaps:
        d = ", ".join(f"{c}({n}):최고령 {o} > 창 {w}" for c, n, o, w in window_gaps[:10])
        print(f"[analyst] ★집계창 결손 {len(window_gaps)}종목: {d}", file=sys.stderr)
        _alert(f"집계창 결손 {len(window_gaps)}종목 — 창 앞부분이 미조회\n{d}\n"
               "avg_tp 가 창과 어긋난다")
    # ★커버리지 경보. 종전엔 '몇 종목을 돌았는지'를 아무도 보지 않아, 72/100 에서
    # 죽어도 조용했다. app.py 신선도 워치독은 max(updated_at) 만 보므로 부분 실패를
    # 원리적으로 못 잡는다(그날 72종목이 갱신되면 age=0 → stale=false).
    if not complete:
        _alert(f"미완주: 커버 {covered}/{len(codes)}"
               f"{chr(10) + '사유: ' + stop_reason if stop_reason else ''}\n"
               f"진행원장 저장됨 — 다음 실행이 남은 {len(codes) - covered}종목부터 이어서 진행")
    if fails:
        detail = ", ".join(f"{c}({n}):{r}" for c, n, r in fails[:10])
        if len(fails) > 10:
            detail += f" 외 {len(fails) - 10}건"
        print(f"[analyst] 실패목록: {detail}", file=sys.stderr)
        _alert(f"{head} · 성공 {ok}/{done}(대상 {len(codes)}) 실패 {len(fails)}"
               f"{' · 실패율 임계초과' if aborted else ''}\n{detail}\n"
               "실패 종목은 저장을 건너뛰어 기존 값이 보존됨")
    # 리포트 조회 실패는 배치를 멈추지 않지만(가격은 정상 갱신) 조용히 넘기면
    # '리포트가 원래 0건'과 구분이 안 된다. 별도로 집계·경보한다.
    if rep_fails:
        kept = sum(1 for r in rep_fails if r[3])
        detail = ", ".join(f"{c}({n}):{s}{'·보존' if p else '·보존없음'}"
                           for c, n, s, p in rep_fails[:10])
        if len(rep_fails) > 10:
            detail += f" 외 {len(rep_fails) - 10}건"
        print(f"[analyst] 리포트 조회 실패 {len(rep_fails)}종목(보존 {kept}): {detail}",
              file=sys.stderr)
        if len(rep_fails) >= _REPORT_ALERT_MIN and len(rep_fails) / max(1, done) > _REPORT_ALERT_RATE:
            _alert(f"리포트 조회 실패 {len(rep_fails)}/{done}종목(기존 리포트 보존 {kept})\n"
                   f"{detail}\n한경(consensus.hankyung.com) 장애·구조변경 의심 — "
                   "가격은 정상 갱신됨")
    # ★「있다가 0건이 됐다」는 종목의 사실일 수 없다(조회창 sdate 고정 → n_total 단조증가).
    # 우리 조회가 바뀐 것이므로 절대 0으로 덮지 않고(build_payload 가 보존) 여기서 경보한다.
    if zero_conflicts:
        detail = ", ".join(f"{c}({n}):보존 n_total={t}" for c, n, t in zero_conflicts[:10])
        if len(zero_conflicts) > 10:
            detail += f" 외 {len(zero_conflicts) - 10}건"
        print(f"[analyst] ★0건 이상징후 {len(zero_conflicts)}종목: {detail}", file=sys.stderr)
        _alert(f"「있다가 0건」 이상징후 {len(zero_conflicts)}종목 — 기존 리포트 보존(덮어쓰지 않음)\n"
               f"{detail}\n검색어·소스 사양변경 의심. 「0건」을 부재로 확정하지 않았다")
    return 2 if aborted else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="0 = 전수(기본). 양수면 그 개수만 처리(디버그용)")
    ap.add_argument("--sleep", type=float, default=1.2)
    ap.add_argument("--codes", default="")  # 쉼표구분 추가 코드
    ap.add_argument("--no-resume", action="store_true",
                    help="진행원장을 무시하고 전수 재수집(코호트 통일용)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Supabase 읽기/쓰기·텔레그램을 차단하고 *.dryrun.json 에만 기록. "
                         "프로덕션 부작용 없이 검증용으로 돌린다")
    ap.add_argument("--deadline-min", type=float, default=0.0,
                    help="이 분(minute)을 넘기면 안전 저장 후 종료. 0=무제한. "
                         "작업 실행시간 상한(PT2H)보다 넉넉히 작게 줄 것")
    a = ap.parse_args()
    extra = [c.strip() for c in a.codes.split(",") if c.strip()]
    raise SystemExit(main(limit=a.limit, sleep_sec=a.sleep, extra=extra,
                          resume=not a.no_resume, deadline_min=a.deadline_min,
                          dry_run=a.dry_run))
