# -*- coding: utf-8 -*-
"""공시알리미(gongsi-alert) 텔레그램 채널 자동발행 모듈 — 추가 모듈(공시코어 무손상).

x_poster.py(트위터) 골격을 텔레그램 채널용으로 모방한다. 임포트만으로는 어떤
네트워크 동작도 하지 않는다. 실발행(실채널 게시)은 TG_CHANNEL_ENABLED 플래그와
봇 토큰·채널ID 가 **셋 다** 있을 때만 도달한다.

안전 규칙(WS-25 게이트):
- 봇 토큰은 오직 env(`TELEGRAM_BOT_TOKEN` = gongsi 봇)에서만 읽는다.
  ⚠️ config.TELEGRAM_TOKEN(트레이딩 봇)과 절대 혼동 금지 — 이 모듈은 그 값을
  참조하지 않는다. 채널ID 도 env(`TG_CHANNEL_ID`)에서만. 하드코딩·커밋 금지.
- 발행: POST api.telegram.org/bot{TOKEN}/sendPhoto|sendMessage (status 200 성공).
- (키 미설정 or TG_CHANNEL_ENABLED off) → 실발송 없이 포맷된 메시지를 로그로만
  출력(dry-run). dry-run 은 텔레그램 API 네트워크 호출 0.
- 감지 훅(on_new_disclosure)은 기본 비활성 — TG_CHANNEL_ENABLED/TG_DRYRUN 없으면
  즉시 no-op. 핵심 폴링 루프(main.poll_once)에 영향 없음.

중복 가드(X 쿼터와 완전 별개 · rcept_no 기준 발행 dedup). Render 비영속 FS
(data/* gitignore, 재시작 시 seen 소실) 감안해 **두 가드 병행**:
  (1) 시간창 가드(주 방어): rcept_dt(또는 rcept_no 앞 8자리 YYYYMMDD)를 파싱해
      최근 N시간(기본 48h) 이내만 발행. 파일 소실과 무관 → 콜드스타트/재배포 시
      과거공시 대량 재발행을 막는 backstop. 파싱 실패는 fail-open(발행 허용)+로그.
  (2) seen 파일(보조): rcept_no 원자적 저장. 세션/프로세스 내 중복 방어.
- 왜 병행인가: 파일 seen 은 세션내 중복만 막고 재배포 시 소실되므로 콜드스타트에
  무력 → 시간창이 backstop. 반대로 시간창만으론 N시간 내 같은 공시를 재폴링하면
  둘 다 '최근'이라 중복 발행 → 그건 파일 seen 이 막는다. 상호보완.

텔레그램 길이 규칙:
- caption(sendPhoto) 상한 1024, message(sendMessage) 상한 4096.
- X 처럼 가중치 계산 불필요 — 텔레그램은 유니코드 코드포인트 기준이므로 len() 사용.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

# config 임포트: side-effect 로 로컬 .env(+kis .env)가 os.environ 에 로드된다.
# (네트워크 동작 없음.) 실패해도 모듈은 살아있게 방어(x_poster 와 동일 패턴).
try:
    import config  # noqa: F401
    _DATA_DIR = config.DATA
except Exception:
    _DATA_DIR = Path(__file__).parent / "data"
    _DATA_DIR.mkdir(exist_ok=True)

log = logging.getLogger("gongsi.tg_channel")

# ── 상수 ─────────────────────────────────────────────────────────────────────
APP_URL = "https://mirialert.com"  # 텔레그램 링크는 무과금 → 본문 포함 가능
DISCLAIMER = "정보 제공용·투자 권유 아님"
ELLIPSIS = "…"

CAPTION_LIMIT = 1024   # sendPhoto caption 상한(코드포인트)
MESSAGE_LIMIT = 4096   # sendMessage text 상한(코드포인트)

DEFAULT_MAX_AGE_HOURS = 48.0   # 시간창 가드 기본값(env TG_MAX_AGE_HOURS 로 조정)

# 텔레그램 Bot API 엔드포인트(토큰은 런타임 env 조회 — 하드코딩 금지).
_API_BASE = "https://api.telegram.org/bot{token}/{method}"

# seen 파일(원자적 저장). data/* 는 .gitignore 로 제외 → 커밋 안 됨.
# 테스트에서 이 모듈 전역을 재지정해 임시경로로 우회 가능(x_poster COUNTER_FILE 패턴).
SEEN_FILE = Path(os.environ.get(
    "TG_SEEN_FILE", str(_DATA_DIR / "tg_channel_seen.json")))
SEEN_MAX = 5000   # seen 무한증가 방지(rcept_no 사전식=시간순 → 최신분 유지)


# ── env 헬퍼(런타임 조회 — 테스트에서 setenv 후 즉시 반영) ────────────────────
def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def tg_enabled() -> bool:
    return _flag("TG_CHANNEL_ENABLED")


def _bot_token():
    # ⚠️ gongsi 봇 토큰. config.TELEGRAM_TOKEN(트레이딩 봇)과 혼동 금지.
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def _channel_id():
    # 채널 chat_id(예: -100...). env 미설정 시 no-op 이 기본 — 하드코딩 금지.
    return os.environ.get("TG_CHANNEL_ID")


def _keys_present() -> bool:
    return bool(_bot_token() and _channel_id())


def _max_age_hours() -> float:
    try:
        return float(os.environ.get("TG_MAX_AGE_HOURS", str(DEFAULT_MAX_AGE_HOURS)))
    except Exception:
        return DEFAULT_MAX_AGE_HOURS


# ── 길이 축약 유틸(코드포인트 len 기준) ──────────────────────────────────────
def _fit(text: str, limit: int) -> str:
    """len(text) 가 limit 이하가 되도록 말줄임(…)으로 축약. 텔레그램은 코드포인트
    기준이므로 X 의 가중치 계산과 달리 단순 len() 을 쓴다."""
    s = str(text or "")
    if limit <= 0:
        return ""
    if len(s) <= limit:
        return s
    if limit <= len(ELLIPSIS):
        return s[:limit]
    return s[:limit - len(ELLIPSIS)].rstrip() + ELLIPSIS


# ── 과거 통계 1줄 (x_poster.impact_stat_line 로직 재사용) ─────────────────────
def impact_stat_line(impact_block) -> str:
    """과거 유사공시 통계 1줄. status!=ok 이면 '집계 중' 표기."""
    if not isinstance(impact_block, dict) or impact_block.get("status") != "ok":
        return "과거 유사공시 통계 집계 중"
    w = (impact_block.get("windows") or {}).get("w1") or {}
    tag = impact_block.get("matched_tag") or impact_block.get("query_tag") or ""
    avg = w.get("raw_avg")
    up = w.get("up_prob")
    n = w.get("n")
    parts = []
    if isinstance(avg, (int, float)) and not isinstance(avg, bool):
        parts.append(f"1주 {'+' if avg > 0 else ''}{float(avg):.1f}%")
    if isinstance(up, (int, float)) and not isinstance(up, bool):
        parts.append(f"상승{int(round(up * 100))}%")
    if isinstance(n, (int, float)) and not isinstance(n, bool):
        parts.append(f"n={int(n)}")
    body = " · ".join(parts) if parts else "통계 참고"
    head = f"과거 유사({tag})" if tag else "과거 유사공시"
    return f"{head} {body}"


# ── 메시지 본문 구성(텔레그램 풀버전) ────────────────────────────────────────
def build_message(item: dict, summary_lines=None, impact_block=None) -> str:
    """공시 dict → 텔레그램 발행 텍스트(풀버전).

    구성: ①[종목명] 제목 ②(빈줄) ③3줄요약 각 줄 ④과거통계 1줄 ⑤앱링크 ⑥면책.
    길이 축약은 발송 분기(_fit)에서 caption/message 상한에 맞춰 수행한다.
    """
    corp = (item.get("corp_name") or "").strip()
    title = (item.get("report_nm") or "").strip()
    head = f"[{corp}] {title}" if corp else title
    hdr_key = head.replace(" ", "")

    lines = [head, ""]
    for ln in (summary_lines or []):
        t = str(ln or "").strip()
        if not t or t.replace(" ", "") == hdr_key:  # 헤더 중복 라인 제거
            continue
        lines.append(t)
    lines.append(impact_stat_line(impact_block))
    lines.append(APP_URL)   # 텔레그램 링크 무과금 → 본문 포함
    lines.append(DISCLAIMER)
    return "\n".join(lines)


# ── 시간창 가드(주 방어 · 비영속 FS 강건) ────────────────────────────────────
def _parse_rcept_dt(item: dict):
    """rcept_dt(우선) 또는 rcept_no 앞 8자리에서 접수일시 파싱.
    DART rcept_dt 는 'YYYYMMDD' 또는 'YYYYMMDDHHMMSS', rcept_no 앞 8자리는 YYYYMMDD.
    파싱 불가 시 None(→ 호출측 fail-open)."""
    raw = str(item.get("rcept_dt") or "").strip()
    rno = str(item.get("rcept_no") or "").strip()
    cand = raw if raw else (rno[:8] if len(rno) >= 8 else "")
    digits = "".join(c for c in cand if c.isdigit())
    if len(digits) >= 14:
        fmt, digits = "%Y%m%d%H%M%S", digits[:14]
    elif len(digits) >= 8:
        fmt, digits = "%Y%m%d", digits[:8]
    else:
        return None
    try:
        return datetime.strptime(digits, fmt)
    except Exception:
        return None


def _is_too_old(item: dict) -> bool:
    """접수시각이 최근 N시간(기본 48h) 밖이면 True. 파싱 실패는 fail-open(False)+로그."""
    dt = _parse_rcept_dt(item)
    if dt is None:
        log.info("[tg-age] rcept_dt 파싱 실패 → fail-open(발행 허용) rcept_no=%s",
                 item.get("rcept_no"))
        return False
    age_h = (datetime.now() - dt).total_seconds() / 3600.0
    return age_h > _max_age_hours()


# ── seen 파일(보조 · 세션내 중복 방어) ───────────────────────────────────────
def _read_seen() -> set:
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _is_seen(rno: str) -> bool:
    return bool(rno) and rno in _read_seen()


def _mark_seen(rno: str) -> None:
    """rcept_no 를 seen 에 원자적 추가(x_poster _atomic_write os.replace 패턴)."""
    if not rno:
        return
    s = _read_seen()
    if rno in s:
        return
    s.add(rno)
    ordered = sorted(s)
    if len(ordered) > SEEN_MAX:
        ordered = ordered[-SEEN_MAX:]   # 최신(큰) rcept_no 유지
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(ordered, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SEEN_FILE)   # 원자적 교체


# ── 실발송(이번 배포에서는 도달 금지) ────────────────────────────────────────
def _http_post(url: str, **kwargs):
    """requests.post 얇은 래퍼(mock 이음매). 실발송 경로에서만 호출 — dry-run
    네트워크 0(lazy import requests). selftest 는 이 함수를 mock 으로 갈아끼워 목검증."""
    import requests  # lazy: dry-run 은 이 함수에 도달하지 않음
    return requests.post(url, **kwargs)


def _api_url(method: str) -> str:
    return _API_BASE.format(token=_bot_token(), method=method)


def _send_message(text: str) -> bool:
    """sendMessage(텍스트). 키+플래그 있을 때만 도달. status 200 성공."""
    r = _http_post(_api_url("sendMessage"),
                   json={"chat_id": _channel_id(), "text": text,
                         "disable_web_page_preview": True},
                   timeout=10)
    ok = getattr(r, "status_code", None) == 200
    if not ok:
        log.warning("텔레그램 sendMessage 실패 status=%s",
                    getattr(r, "status_code", "?"))
    return ok


def _send_photo(photo_path: str, caption: str) -> bool:
    """sendPhoto(멀티파트 파일 업로드 + caption). 키+플래그 있을 때만 도달."""
    with open(photo_path, "rb") as fh:
        r = _http_post(_api_url("sendPhoto"),
                       data={"chat_id": _channel_id(), "caption": caption},
                       files={"photo": fh},
                       timeout=20)
    ok = getattr(r, "status_code", None) == 200
    if not ok:
        log.warning("텔레그램 sendPhoto 실패 status=%s",
                    getattr(r, "status_code", "?"))
    return ok


def _render_card(item: dict):
    """공시 카드 PNG 렌더 → 경로(str). 실패 시 예외(호출측 try/except).
    별도 함수로 분리한 이유: selftest 에서 이 함수를 mock 으로 갈아끼워 카드
    성공/실패 두 분기를 실API·실렌더 없이 검증하기 위한 이음매."""
    import tempfile
    import card_render  # lazy: dry-run·비발송 경로는 도달하지 않음
    out = Path(tempfile.gettempdir()) / f"tg_card_{item.get('rcept_no', 'x')}.png"
    return card_render.render_card_file(item, str(out), valmode="avg", win="m1")


def post_disclosure(item: dict, summary_lines=None, impact_block=None) -> dict:
    """공시 1건 → 텔레그램 채널 발행(또는 dry-run 로그).

    반환 status: dry_run | duplicate | posted | send_failed | too_old
    - too_old : 시간창(N시간) 밖 — 콜드스타트 backstop. seen/네트워크 미소모.
    - duplicate: seen 에 이미 있는 rcept_no. seen/네트워크 미소모.
    - dry_run : 실발송 없음(포맷 로그만). 네트워크 0. dedup 검증 위해 seen 은 기록
      (→ 2회째 duplicate). 카드/실API 미도달.
    - posted / send_failed: 실발송 경로(플래그+토큰+채널ID 셋 다 있을 때만 도달).
      성공 시 seen 기록, 실패 시 미기록(재시도 여지).

    dedup 은 실발송·dry-run 양쪽 모두에 적용.
    """
    rno = str(item.get("rcept_no") or "").strip()
    text = build_message(item, summary_lines, impact_block)
    base = {"text": text, "len": len(text),
            "caption_len": len(_fit(text, CAPTION_LIMIT)),
            "message_len": len(_fit(text, MESSAGE_LIMIT))}

    # (1) 시간창 가드(주 방어) — 파일 소실과 무관한 콜드스타트 backstop. 먼저 판정.
    if _is_too_old(item):
        log.info("[tg-skip] 시간창(%.0fh) 밖 — 발행 스킵 rcept_no=%s",
                 _max_age_hours(), rno)
        return {"status": "too_old", **base}

    # (2) seen 파일(보조) — 세션/프로세스 내 중복 방어.
    if _is_seen(rno):
        log.info("[tg-skip] 중복(seen) — 발행 스킵 rcept_no=%s", rno)
        return {"status": "duplicate", **base}

    real = tg_enabled() and _keys_present()
    if not real:
        why = "TG_CHANNEL_ENABLED off" if not tg_enabled() else "키 미설정"
        log.info("[tg-dry-run] (%s) 실발송 없음 · len=%d\n%s", why, len(text), text)
        # dry-run 도 seen 기록 → 같은 rcept_no 2회째 duplicate(dedup 검증 가능).
        _mark_seen(rno)
        return {"status": "dry_run", "reason": why, **base}

    # ── 실발송 경로(플래그+토큰+채널ID 셋 다 있을 때만 도달; 이번 배포 미실행) ──
    photo_path = None
    try:
        photo_path = _render_card(item)   # 렌더 실패 대비 try/except
    except Exception as e:
        log.warning("[tg] 카드 렌더 실패 → sendMessage 폴백: %s", e)
        photo_path = None

    if photo_path:
        # caption 상한(1024) 초과 시 판단: 카드 이미지가 핵심 차별 페이로드이고
        # 렌더된 카드 하단에 이미 면책 문구가 박혀 있으므로, 드문 초과 케이스에선
        # 사진을 유지한 채 caption 을 _fit 로 말줄임한다(sendMessage 다운그레이드 대신).
        caption = _fit(text, CAPTION_LIMIT)
        sent = _send_photo(photo_path, caption)
    else:
        sent = _send_message(_fit(text, MESSAGE_LIMIT))

    if sent:
        _mark_seen(rno)   # 실발송 성공 시에만 seen 기록
    return {"status": "posted" if sent else "send_failed", **base}


# ── 감지 훅(main.poll_once 에서 1줄 호출; 기본 비활성) ────────────────────────
def on_new_disclosure(item: dict, result: dict):
    """신규 공시 감지 시 호출되는 어댑터. 기본 비활성(TG_CHANNEL_ENABLED/TG_DRYRUN
    없으면 no-op). 어떤 예외도 핵심 폴링을 깨지 않게 격리(fail-open)."""
    try:
        if not (tg_enabled() or _flag("TG_DRYRUN")):
            return None  # 기본 비활성 — 핵심 루프 무영향
        summary_lines = result.get("summary") if isinstance(result, dict) else None
        tags = result.get("tags") if isinstance(result, dict) else None
        impact_block = None
        try:
            import impact as _impact  # read-only, 네트워크 없음
            impact_block = _impact.impact_for_tags(tags or [])
        except Exception:
            impact_block = None
        return post_disclosure(item, summary_lines=summary_lines,
                               impact_block=impact_block)
    except Exception as e:
        log.warning("[tg-hook] 무시(fail-open): %s", e)
        return None


# ── 자체 검증(DoD 증거 산출) ─────────────────────────────────────────────────
def _selftest():
    import sys
    import tempfile
    import shutil
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    global SEEN_FILE, _render_card, _http_post
    orig_seen, orig_render, orig_http = SEEN_FILE, _render_card, _http_post
    tmpdir = tempfile.mkdtemp(prefix="tg_selftest_")
    SEEN_FILE = Path(tmpdir) / "tg_channel_seen.json"  # 실 data/ 오염 방지
    today = datetime.now().strftime("%Y%m%d")

    print("=" * 72)
    print("네트워크 미도달 확인: TG_CHANNEL_ENABLED=%s / 키present=%s → dry-run 경로만"
          % (tg_enabled(), _keys_present()))
    print("봇토큰 소스 = env TELEGRAM_BOT_TOKEN (config.TELEGRAM_TOKEN=트레이딩봇 미사용)")
    print("caption 상한=%d / message 상한=%d / 시간창=%.0fh"
          % (CAPTION_LIMIT, MESSAGE_LIMIT, _max_age_hours()))
    print("=" * 72)

    results = {}
    try:
        # ── [A] dry-run 포맷 3건 ──────────────────────────────────────────────
        print("\n[A] dry-run 포맷 3건 (caption≤%d / message≤%d 증명)\n"
              % (CAPTION_LIMIT, MESSAGE_LIMIT))
        long_body = ("복수 완성차 제조사 컨소시엄과 자율주행 통합 플랫폼 및 차세대 "
                     "배터리관리시스템을 2026년부터 2031년까지 6년간 공급하는 대규모 "
                     "장기 계약. ") * 40  # 초장문 유도(>1024 코드포인트)
        samples = [
            ("① 통계 있음(텍스트분기 상당)",
             {"corp_name": "삼성전자",
              "report_nm": "주요사항보고서(자기주식취득결정)",
              "rcept_no": today + "000001", "rcept_dt": today},
             ["자기주식 3천억 규모 취득 결정", "주주가치 제고 목적", "취득기간 3개월"],
             {"status": "ok", "matched_tag": "자사주",
              "windows": {"w1": {"raw_avg": 1.8, "up_prob": 0.62, "n": 145}}}),
            ("② 통계 pending(집계중 표기)",
             {"corp_name": "에이비엘바이오",
              "report_nm": "투자판단관련주요경영사항(임상시험계획 승인)",
              "rcept_no": today + "000002", "rcept_dt": today},
             ["임상 2상 계획 식약처 승인", "연내 환자 등록 개시", "파트너십 협의 중"],
             {"status": "pending", "message": "이 유형은 아직 집계 중"}),
            ("③ 초장문(축약 강제 → 상한 이내 증명)",
             {"corp_name": "케이지모빌리티인터내셔널홀딩스",
              "report_nm": "단일판매ㆍ공급계약체결(자율주행 통합 플랫폼 장기 공급)",
              "rcept_no": today + "000003", "rcept_dt": today},
             [long_body, "계약금액 최근 매출액 대비 상당한 비중"],
             {"status": "ok", "matched_tag": "공급계약",
              "windows": {"w1": {"raw_avg": -0.7, "up_prob": 0.48, "n": 63}}}),
        ]
        a_ok = True
        for label, item, summ, imp in samples:
            res = post_disclosure(item, summary_lines=summ, impact_block=imp)
            cap_ok = res["caption_len"] <= CAPTION_LIMIT
            msg_ok = res["message_len"] <= MESSAGE_LIMIT
            is_dry = res["status"] == "dry_run"
            ok = cap_ok and msg_ok and is_dry
            a_ok &= ok
            print("--- %s status=%s ---" % (label, res["status"]))
            print(_fit(res["text"], MESSAGE_LIMIT))
            print(">> full_len=%d · caption_fit=%d/%d · message_fit=%d/%d → %s"
                  % (res["len"], res["caption_len"], CAPTION_LIMIT,
                     res["message_len"], MESSAGE_LIMIT, "OK" if ok else "FAIL"))
            if "③" in label:
                print(">> (초장문 축약 검증) full_len>%d=%s · caption 말줄임포함=%s"
                      % (CAPTION_LIMIT, res["len"] > CAPTION_LIMIT,
                         ELLIPSIS in _fit(res["text"], CAPTION_LIMIT)))
            print()
        results["dry-run3"] = a_ok

        # ── [B] 길이 상한 실측(caption≤1024 / message≤4096) ──────────────────
        print("=" * 72)
        print("[B] 길이 상한 실측 (_fit 경계)\n")
        huge = "가" * 6000
        cap = _fit(huge, CAPTION_LIMIT)
        msg = _fit(huge, MESSAGE_LIMIT)
        b_cap = len(cap) <= CAPTION_LIMIT
        b_msg = len(msg) <= MESSAGE_LIMIT
        print("   caption 경로: 원문 %d → _fit(1024)=%d (≤1024:%s)"
              % (len(huge), len(cap), b_cap))
        print("   message 경로: 원문 %d → _fit(4096)=%d (≤4096:%s)"
              % (len(huge), len(msg), b_msg))
        results["길이"] = b_cap and b_msg
        print("   → %s" % ("PASS" if results["길이"] else "FAIL"))

        # ── [C] dedup(같은 rcept_no 2회) ─────────────────────────────────────
        print("\n" + "=" * 72)
        print("[C] dedup 검증 (임시 seen 파일 · rcept_no 기준)\n")
        ditem = {"corp_name": "중복테스트", "report_nm": "테스트공시",
                 "rcept_no": today + "999999", "rcept_dt": today}
        r1 = post_disclosure(ditem, summary_lines=["a"], impact_block=None)
        r2 = post_disclosure(ditem, summary_lines=["a"], impact_block=None)
        c_ok = r1["status"] == "dry_run" and r2["status"] == "duplicate"
        print("   1회째 status=%s (기대 dry_run) / 2회째 status=%s (기대 duplicate)"
              % (r1["status"], r2["status"]))
        print("   → %s" % ("PASS" if c_ok else "FAIL"))
        results["dedup"] = c_ok

        # ── [D] 시간창 가드(오래된 → too_old, 최근 → 통과) ───────────────────
        print("\n" + "=" * 72)
        print("[D] 시간창 가드 검증\n")
        old_item = {"corp_name": "옛날공시", "report_nm": "과거공시",
                    "rcept_no": "20000101000001", "rcept_dt": "20000101"}
        new_item = {"corp_name": "최근공시", "report_nm": "오늘공시",
                    "rcept_no": today + "555555", "rcept_dt": today}
        r_old = post_disclosure(old_item, summary_lines=["x"], impact_block=None)
        r_new = post_disclosure(new_item, summary_lines=["y"], impact_block=None)
        d_ok = r_old["status"] == "too_old" and r_new["status"] == "dry_run"
        print("   오래된(20000101) status=%s (기대 too_old)" % r_old["status"])
        print("   최근(오늘) status=%s (기대 dry_run/통과)" % r_new["status"])
        print("   → %s" % ("PASS" if d_ok else "FAIL"))
        results["시간창"] = d_ok

        # ── [E] sendPhoto/sendMessage 목검증(실API 0) ────────────────────────
        print("\n" + "=" * 72)
        print("[E] sendPhoto/sendMessage 목검증 (mock _http_post · 네트워크 0)\n")

        class _R:
            def __init__(s, status):
                s.status_code = status

        def _mock():
            state = {"urls": [], "kw": []}
            def fake(url, **kw):
                state["urls"].append(url)
                state["kw"].append(kw)
                return _R(200)
            return fake, state

        # 실발송 경로 도달을 위해 env(플래그+토큰+채널ID) 임시 주입 + mock.
        os.environ["TG_CHANNEL_ENABLED"] = "1"
        os.environ["TELEGRAM_BOT_TOKEN"] = "TESTBOTTOKEN"
        os.environ["TG_CHANNEL_ID"] = "-1000000000000"
        dummy_png = Path(tmpdir) / "dummy_card.png"
        dummy_png.write_bytes(b"\x89PNG\r\n\x1a\n")  # 최소 더미(실렌더 회피)
        try:
            # E-1: 카드 렌더 성공 → sendPhoto(files/caption 전달)
            print("E-1 카드 있을 때 sendPhoto:")
            _render_card = lambda it: str(dummy_png)   # 렌더 성공 mock
            _http_post, st1 = _mock()
            e1_item = {"corp_name": "포토테스트", "report_nm": "카드공시",
                       "rcept_no": today + "111111", "rcept_dt": today}
            rp = post_disclosure(e1_item, summary_lines=["카드요약"], impact_block=None)
            url1 = st1["urls"][0] if st1["urls"] else ""
            kw1 = st1["kw"][0] if st1["kw"] else {}
            e1 = (rp["status"] == "posted" and url1.endswith("/sendPhoto")
                  and "photo" in (kw1.get("files") or {})
                  and "caption" in (kw1.get("data") or {}))
            print("   status=%s url=/%s files=%s caption유무=%s → %s"
                  % (rp["status"], url1.rsplit("/", 1)[-1],
                     list((kw1.get("files") or {}).keys()),
                     "caption" in (kw1.get("data") or {}),
                     "OK" if e1 else "FAIL"))

            # E-2: 카드 렌더 실패/없음 → sendMessage(text 전달)
            print("E-2 카드 없을 때 sendMessage:")
            def _render_fail(it):
                raise RuntimeError("render off")
            _render_card = _render_fail
            _http_post, st2 = _mock()
            e2_item = {"corp_name": "텍스트테스트", "report_nm": "텍스트공시",
                       "rcept_no": today + "222222", "rcept_dt": today}
            rm = post_disclosure(e2_item, summary_lines=["텍스트요약"], impact_block=None)
            url2 = st2["urls"][0] if st2["urls"] else ""
            kw2 = st2["kw"][0] if st2["kw"] else {}
            e2 = (rm["status"] == "posted" and url2.endswith("/sendMessage")
                  and "text" in (kw2.get("json") or {}))
            print("   status=%s url=/%s text유무=%s → %s"
                  % (rm["status"], url2.rsplit("/", 1)[-1],
                     "text" in (kw2.get("json") or {}),
                     "OK" if e2 else "FAIL"))

            # 실API 미도달 증명: 두 호출 모두 mock(_R) 이었고 요청 URL 은 api.telegram.org 형식.
            e_url_ok = url1.startswith("https://api.telegram.org/bot") and \
                url2.startswith("https://api.telegram.org/bot")
            print("   URL 형식(api.telegram.org/bot...) 확인=%s" % e_url_ok)
            results["목검증"] = e1 and e2 and e_url_ok
            print("   → %s" % ("PASS" if results["목검증"] else "FAIL"))
        finally:
            _http_post = orig_http
            _render_card = orig_render
            for k in ("TG_CHANNEL_ENABLED", "TELEGRAM_BOT_TOKEN", "TG_CHANNEL_ID"):
                os.environ.pop(k, None)

    finally:
        SEEN_FILE = orig_seen
        _render_card = orig_render
        _http_post = orig_http
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 72)
    all_ok = all(results.values())
    print("종합 DoD:", "PASS" if all_ok else "FAIL",
          "| dry-run3=%s 길이=%s dedup=%s 시간창=%s 목검증=%s | 실게시/실API 호출 0(모두 mock)"
          % (results.get("dry-run3"), results.get("길이"), results.get("dedup"),
             results.get("시간창"), results.get("목검증")))
    return all_ok


if __name__ == "__main__":
    ok = _selftest()
    raise SystemExit(0 if ok else 1)
