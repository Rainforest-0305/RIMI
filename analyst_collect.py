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
import re
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
    try:
        from notify_alert import _tg_send
        if not _tg_send("[MIRI 배치경보] analyst_collect\n" + text):
            print("[analyst] 텔레그램 미발송(토큰/채널 미설정) — 로그로만 표면화",
                  file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[analyst] 경보 발송 실패: {type(e).__name__}", file=sys.stderr)


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
    if re.fullmatch(r"\d{6}", t):
        return t
    return name2code.get(t.lower())


# ----------------- 한경 리포트 수집(코드 재검증) -----------------
# 리포트 조회 결과 3분기. '리포트 0건'과 '조회 실패'를 절대 같은 값으로 표현하지
# 않는다 — 둘을 섞으면 한경 장애 시 reports=[] 가 정상값으로 저장돼 기존 리포트를
# 지운다(주 1회 배치라 최대 7일 공백). 반대로 0건을 실패로 오인하면 리포트가 진짜
# 없는 소형주가 영구 스킵된다. 그래서 상태를 값과 분리해 명시적으로 돌려준다.
REPORTS_OK = "ok"                  # 페이지 정상 파싱(결과 0건도 여기 — 진짜 0건)
REPORTS_NET_FAIL = "net_fail"      # 요청 자체 실패(예외/타임아웃/비200/빈 본문)
REPORTS_PARSE_FAIL = "parse_fail"  # 200 이지만 우리가 아는 표가 아님(구조 변경·차단페이지)

# 목록표 헤더의 '위치까지' 검증한다. fetch_reports 는 tds[0]=작성일 tds[2]=적정가격
# tds[5]=제공출처 처럼 인덱스로 읽으므로, 컬럼 순서가 바뀌면 파싱은 성공한 척하면서
# 목표가 자리에 엉뚱한 값이 들어간다. 헤더 위치 검증이 그 조용한 오염을 막는다.
# (실측 2026-07-28: 정상/결과0건 페이지 모두 동일 thead 를 내려준다)
_HK_HEAD_EXPECT = {0: "작성일", 1: "제목", 2: "적정가격", 3: "투자의견", 5: "제공출처"}


def _rows(term, sdate, edate, pagenum=40):
    """(데이터행 리스트, 상태). 예외를 밖으로 던지지 않는다.

    상태는 REPORTS_OK / REPORTS_NET_FAIL / REPORTS_PARSE_FAIL 중 하나.
    OK + 빈 리스트 = '검색결과 없음'(한경이 '결과가 없습니다.' 한 줄을 내려주는 정상 응답)."""
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
    return rows, REPORTS_OK


def fetch_reports(code, name, sdate="2025-01-01", edate=None, with_status=False):
    """타깃종목 리포트 전량(목표가 0 포함). row_code==code 재검증. 날짜 오름차순.

    with_status=True 면 (리포트리스트, 상태) 튜플. 상태 판정 규칙:
      - 리포트를 1건이라도 얻었으면 무조건 OK(실패 신호가 섞여 있어도 데이터는 진짜다)
      - 0건인데 시도한 검색어 중 하나라도 실패했으면 그 실패상태(보수적 판정)
      - 0건이고 전 시도가 정상 파싱이면 OK = '리포트가 진짜 0건'
    with_status=False(기본)면 종전과 동일하게 리스트만 반환(하위호환)."""
    if edate is None:
        edate = (date.today() + timedelta(days=1)).isoformat()
    terms = [name]
    if len(name) >= 3:
        terms += [name[:3], name[:2]]
    seen, out = set(), []
    saw_net_fail = saw_parse_fail = False
    for term in terms:
        if not term:
            continue
        rows, st = _rows(term, sdate, edate)
        if st == REPORTS_NET_FAIL:
            saw_net_fail = True
        elif st == REPORTS_PARSE_FAIL:
            saw_parse_fail = True
        for tr in rows:
            tds = tr.find_all("td")
            rdate = tds[0].get_text(strip=True)
            title = tds[1].get_text(" ", strip=True)
            m = re.search(r"\((\d{6})\)", title)
            row_code = m.group(1) if m else ""
            if row_code != code:                # ★ 코드 재검증 — 타깃종목만
                continue
            tp = tds[2].get_text(strip=True).replace(",", "")
            opinion = tds[3].get_text(strip=True)
            broker = tds[5].get_text(strip=True)
            title_clean = re.sub(r"\(\d{6}\)\s*", "", title)
            # 제목 3중 반복 렌더 제거
            half = title_clean[:len(title_clean)//3] if len(title_clean) > 30 else title_clean
            # 제목 앞 중복 종목명 제거
            half = re.sub(r"^(" + re.escape(name) + r")+", "", half).strip()
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
        if out:            # 정식명으로 잡혔으면 프리픽스 재시도 생략
            break
        time.sleep(1.0)
    out.sort(key=lambda x: x["date"])
    if out:
        status = REPORTS_OK
    elif saw_net_fail:
        status = REPORTS_NET_FAIL
    elif saw_parse_fail:
        status = REPORTS_PARSE_FAIL
    else:
        status = REPORTS_OK          # 전 시도 정상 파싱 + 0건 = 진짜 0건
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


def build_payload(code, name, prev_loader=None):
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
    all_reports, rstatus = fetch_reports(code, name, with_status=True)
    preserved = False
    if not all_reports and rstatus != REPORTS_OK:
        prev = prev_loader() if callable(prev_loader) else _prev_row(code)
        prev_reports = [r for r in ((prev or {}).get("reports") or [])
                        if isinstance(r, dict)]
        if prev_reports:
            preserved = True
            tp_reps = sorted((r for r in prev_reports
                              if int(r.get("target_price") or 0) > 0
                              and str(r.get("date") or "") >= window_start),
                             key=lambda x: str(x.get("date") or ""))
            n_total = int((prev or {}).get("n_total") or len(prev_reports))
            print(f"[analyst] {code} 리포트 조회 실패({rstatus}) → 기존 {len(prev_reports)}건 "
                  f"보존(창 내 {len(tp_reps)}건). 가격만 갱신", file=sys.stderr)
        else:
            tp_reps, n_total = [], 0
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
    }


# ----------------- 대상 코드 산출 -----------------
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


def main(limit=15, sleep_sec=1.2, extra=None):
    code2name, name2code = build_name_map()
    codes = target_codes(limit=limit, extra=extra)
    if not codes:
        print("[analyst] 대상 코드 0 — top100.json 먼저 생성 필요", file=sys.stderr)
        return 1
    mc.ensure_table(_DDL)
    cache = mc.load_json(_CACHE_FILE, default={}) or {}
    if not isinstance(cache, dict):
        cache = {}
    ok = 0
    fails = []            # [(code, name, 사유), ...] — 배치 종료 시 표면화
    rep_fails = []        # 리포트 조회 실패(가격은 성공) — 중단 사유는 아니지만 반드시 표면화
    aborted = False
    for i, code in enumerate(codes, 1):
        name = code2name.get(code, code)
        try:
            payload = build_payload(code, name,
                                    prev_loader=lambda c=code: _prev_row(c, cache))
            cache[code] = payload
            save_one(payload)
            ok += 1
            if payload.get("_reports_status") != REPORTS_OK:
                rep_fails.append((code, name, payload.get("_reports_status"),
                                  bool(payload.get("_reports_preserved"))))
            print(f"[analyst] {code} {name}: n_total={payload['n_total']} "
                  f"n_tp={payload['n_tp']} avg_tp={payload['avg_tp']} "
                  f"cur={payload['current']} prices={len(payload['prices'])} "
                  f"win={payload['window_start']}", file=sys.stderr)
        except PriceUnavailable as e:
            # 가격 0행: 저장·캐시 갱신을 모두 건너뛴다(기존 행/캐시 보존).
            fails.append((code, name, "price0"))
            print(f"[analyst] {code} {name}: SKIP price0 ({e}) — 기존 행 보존",
                  file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            fails.append((code, name, type(e).__name__))
            print(f"[analyst] {code} {name}: ERR {type(e).__name__} {e}",
                  file=sys.stderr)
        if len(fails) >= _ABORT_MIN_FAILS and len(fails) / i > _ABORT_FAIL_RATE:
            aborted = True
            print(f"[analyst] 중단: 실패 {len(fails)}/{i}"
                  f"({len(fails) / i:.0%}) > 임계 {_ABORT_FAIL_RATE:.0%}",
                  file=sys.stderr)
            break
        time.sleep(sleep_sec)
    # 로컬 전량 스냅샷 저장(폴백). 실패 종목은 cache 에 손대지 않았으므로 기존
    # 값이 그대로 남는다(merge 성격 — 여기서도 덮어쓰지 않는다).
    mc.save_json(_CACHE_FILE, cache)
    done = ok + len(fails)
    head = "중단" if aborted else "완료"
    print(f"[analyst] {head}: 성공 {ok}/{done}(대상 {len(codes)}) "
          f"실패 {len(fails)}, 캐시 {len(cache)}종목 -> {_CACHE_FILE}")
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
    return 2 if aborted else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--sleep", type=float, default=1.2)
    ap.add_argument("--codes", default="")  # 쉼표구분 추가 코드
    a = ap.parse_args()
    extra = [c.strip() for c in a.codes.split(",") if c.strip()]
    raise SystemExit(main(limit=a.limit, sleep_sec=a.sleep, extra=extra))
