# -*- coding: utf-8 -*-
"""공시알리미(gongsi-alert) X(트위터) 자동 게시 모듈 — 추가 모듈(공시코어 무손상).

상태: **가동 승인(President 2026-07-16) · X_ENABLED 로 활성화.**
임포트만으로는 어떤 네트워크 동작도 하지 않는다. 실게시는 X_ENABLED 플래그와
OAuth1.0a 키가 **둘 다** 있을 때만 도달한다.

안전 규칙(WS-17 게이트):
- 인증: OAuth 1.0a user-context (만료 없음 → Render 비영속 FS에서도 안전.
  OAuth2.0 은 access 2h 만료+refresh rotation 저장 필요라 서버 자동화에 부적합해 전환).
  키는 오직 env 에서만 읽는다(X_API_KEY / X_API_SECRET /
  X_ACCESS_TOKEN_OAUTH1 / X_ACCESS_SECRET_OAUTH1). 하드코딩·커밋 금지.
- 게시: POST api.x.com/2/tweets (requests-oauthlib OAuth1 서명). 401/403 = 실패 로그(재시도 없음).
- 트윗에 링크 미포함(링크 포함 시 X 종량제 $0.20/건 vs 미포함 $0.015/건 — 링크는 프로필 bio 위임).
- (키 미설정 or X_ENABLED off) → 실발송 없이 포맷된 트윗을 로그로만 출력(dry-run).
- 감지 훅(on_new_disclosure)은 기본 비활성 — X_ENABLED/X_DRYRUN 없으면 즉시 no-op.
  핵심 폴링 루프(main.poll_once)에 영향 없음.
- 쿼터 가드: Free 티어 월 500포스트 대응 일 한도(기본 15). 원자적 카운터 +
  날짜별 리셋. 텔레그램과 독립 카운터.

X 가중길이 규칙(280 상한):
- URL 은 t.co 단축 기준 23 고정.
- 한글·CJK·이모지 등 = 2, ASCII/라틴 등 = 1 (트위터 공식 weight-range 반영).
"""
import json
import logging
import os
import re
from datetime import date
from pathlib import Path

# config 임포트: side-effect 로 로컬 .env(+kis .env)가 os.environ 에 로드된다.
# (네트워크 동작 없음. telegram_bot.py 와 동일 패턴.) 실패해도 모듈은 살아있게 방어.
try:
    import config  # noqa: F401
    _DATA_DIR = config.DATA
except Exception:
    _DATA_DIR = Path(__file__).parent / "data"
    _DATA_DIR.mkdir(exist_ok=True)

log = logging.getLogger("gongsi.x_poster")

# ── 상수 ─────────────────────────────────────────────────────────────────────
APP_URL = "https://rimi-s76t.onrender.com"  # 트윗 본문 미포함(비용) — 프로필 bio 참조용
MAX_WEIGHTED = 280
URL_WEIGHT = 23                    # t.co 단축 후 고정 가중치(본문에 URL 넣을 경우 대비 유지)
ELLIPSIS = "…"               # '…'

# X API v2 엔드포인트(OAuth1.0a user-context 서명).
TWEETS_URL = "https://api.x.com/2/tweets"

# 규제 톤 마커(설계 판단: 매 트윗 짧은 마커 + 프로필 bio 에 전체 면책 — 보고 참조).
# 짧은 마커로 전체 면책문(약 80units)의 1/4 수준. 정보제공·투자권유아님 명시.
DISCLAIMER_MARKER = "정보제공·투자권유X"

# 카운터 파일(원자적·날짜별 리셋). data/* 는 .gitignore 제외 → 커밋 안 됨.
# 테스트에서 이 모듈 전역을 재지정해 임시경로로 우회 가능.
COUNTER_FILE = Path(os.environ.get(
    "X_COUNTER_FILE", str(_DATA_DIR / "x_post_counter.json")))


# ── env 헬퍼(런타임 조회 — 테스트에서 setenv 후 즉시 반영) ────────────────────
def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def x_enabled() -> bool:
    return _flag("X_ENABLED")


def _keys() -> dict:
    return {k: os.environ.get(k) for k in (
        "X_API_KEY", "X_API_SECRET",
        "X_ACCESS_TOKEN_OAUTH1", "X_ACCESS_SECRET_OAUTH1")}


def _keys_present() -> bool:
    return all(_keys().values())


def daily_limit() -> int:
    try:
        return max(0, int(os.environ.get("X_DAILY_LIMIT", "15")))
    except Exception:
        return 15


# ── X 가중길이 계산 ──────────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://\S+")


def _char_weight(ch: str) -> int:
    """트위터 weight-range: 아래 구간 = 1, 그 외(한글/CJK/이모지 등) = 2."""
    cp = ord(ch)
    if (cp <= 0x10FF
            or 0x2000 <= cp <= 0x200D
            or 0x2010 <= cp <= 0x201F
            or 0x2032 <= cp <= 0x2037):
        return 1
    return 2


def _chars_weight(s: str) -> int:
    return sum(_char_weight(c) for c in s)


def weighted_len(text: str) -> int:
    """X 가중길이. URL 은 23 고정, 그 외 문자별 1/2 합산."""
    total = 0
    idx = 0
    for m in _URL_RE.finditer(text):
        total += _chars_weight(text[idx:m.start()])
        total += URL_WEIGHT
        idx = m.end()
    total += _chars_weight(text[idx:])
    return total


def _fit_weighted(s: str, budget: int) -> str:
    """가중길이가 budget 이하가 되도록 말줄임(…)으로 축약. URL 미포함 텍스트 전용."""
    if budget <= 0:
        return ""
    if _chars_weight(s) <= budget:
        return s
    ellw = _chars_weight(ELLIPSIS)  # 2
    target = budget - ellw
    if target <= 0:
        return ELLIPSIS if budget >= ellw else ""
    out, w = [], 0
    for ch in s:
        cw = _char_weight(ch)
        if w + cw > target:
            break
        out.append(ch)
        w += cw
    return "".join(out).rstrip() + ELLIPSIS


# ── 트윗 본문 구성 ───────────────────────────────────────────────────────────
def _condense_summary(summary_lines, corp: str, title: str) -> str:
    """3줄 요약 → 한 줄 축약. ①행(종목명·제목)과 중복되는 라인은 제거."""
    if not summary_lines:
        return ""
    hdr = f"{corp} · {title}".replace(" ", "")
    lines = []
    for ln in summary_lines:
        if not ln:
            continue
        t = str(ln).strip()
        if not t or t.replace(" ", "") == hdr:
            continue
        lines.append(t)
    return " · ".join(lines)


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
    if isinstance(avg, (int, float)):
        parts.append(f"1주 {'+' if avg > 0 else ''}{float(avg):.1f}%")
    if isinstance(up, (int, float)):
        parts.append(f"상승{int(round(up * 100))}%")
    if isinstance(n, (int, float)):
        parts.append(f"n={int(n)}")
    body = " · ".join(parts) if parts else "통계 참고"
    head = f"과거 유사({tag})" if tag else "과거 유사공시"
    return f"{head} {body}"


def _assemble(corp: str, title: str, summary: str, stat: str) -> str:
    parts = [f"[{corp}] {title}" if corp else title]
    if summary:
        parts.append(summary)
    parts.append(stat)
    parts.append(DISCLAIMER_MARKER)
    # 링크 미포함(종량제: 링크 포함 $0.20/건 vs 미포함 $0.015/건 — 링크는 프로필 bio 위임).
    return "\n".join(parts)


def build_tweet(disclosure: dict, summary_lines=None, impact_block=None):
    """공시 dict → (트윗 텍스트, 가중길이). 항상 가중 280 이내 보장.

    포맷: ①[종목명] 공시제목 ②3줄요약 축약 ③과거통계 1줄 ④면책마커 ⑤앱링크
    초과 시 요약 먼저, 그래도 넘치면 제목을 말줄임(…)으로 축약.
    """
    corp = (disclosure.get("corp_name") or "").strip()
    title = (disclosure.get("report_nm") or "").strip()
    summary = _condense_summary(summary_lines, corp, title)
    stat = impact_stat_line(impact_block)

    text = _assemble(corp, title, summary, stat)
    if weighted_len(text) <= MAX_WEIGHTED:
        return text, weighted_len(text)

    # 1) 요약 축약: 요약 라인을 포함한 프레임 가중치를 빼고 요약 예산 산정.
    #    placeholder '\x01'(가중1)로 프레임 측정 후 1 을 뺀다.
    frame_with_summary = _chars_weight(_assemble(corp, title, "\x01", stat)) - 1
    summary_budget = MAX_WEIGHTED - frame_with_summary
    if summary and summary_budget >= _chars_weight(ELLIPSIS):
        summary = _fit_weighted(summary, summary_budget)
        text = _assemble(corp, title, summary, stat)
        if weighted_len(text) <= MAX_WEIGHTED:
            return text, weighted_len(text)

    # 2) 그래도 초과 → 요약 제거하고 제목 축약.
    frame_no_summary = _chars_weight(_assemble(corp, "\x01", "", stat)) - 1
    title_budget = MAX_WEIGHTED - frame_no_summary
    title = _fit_weighted(title, title_budget)
    text = _assemble(corp, title, "", stat)
    # 3) 최종 하드클램프: 프레임(종목명·통계·마커) 자체가 커서 여전히 초과하는
    #    엣지케이스에서도 280 을 절대 보장(전체 텍스트 기준 축약).
    if weighted_len(text) > MAX_WEIGHTED:
        text = _fit_weighted(text, MAX_WEIGHTED)
    return text, weighted_len(text)


# ── 쿼터 가드(원자적 카운터 · 날짜별 리셋 · 파일락) ───────────────────────────
def _today() -> str:
    return date.today().isoformat()


def _read_raw() -> dict:
    try:
        return json.loads(COUNTER_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _normalized_count() -> int:
    """오늘자 사용량(다른 날짜면 0). 파일 미변경(peek)."""
    raw = _read_raw()
    return int(raw.get("count", 0)) if raw.get("date") == _today() else 0


def quota_status() -> dict:
    used = _normalized_count()
    lim = daily_limit()
    return {"date": _today(), "count": used, "limit": lim,
            "remaining": max(0, lim - used)}


def _atomic_write(d: dict) -> None:
    tmp = COUNTER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, COUNTER_FILE)  # 원자적 교체(패턴: main.save_seen)


def _acquire_lock(retries: int = 50):
    """원자적 O_EXCL 락파일. stale 락(mtime 60초+)은 회수 후 재시도.
    끝내 실패하면 None 반환 — 호출측(reserve_quota)은 fail-closed(예약 거부)."""
    lock = COUNTER_FILE.with_suffix(".lock")
    import time
    for _ in range(retries):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return fd, lock
        except FileExistsError:
            try:  # stale 락 회수: 보유 프로세스가 죽어 60초+ 방치된 경우
                if time.time() - lock.stat().st_mtime > 60:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            time.sleep(0.01)
    return None, lock


def _release_lock(handle):
    fd, lock = handle
    try:
        if fd is not None:
            os.close(fd)
        if lock.exists():
            os.remove(lock)
    except Exception:
        pass


def reserve_quota() -> bool:
    """오늘 한도 내면 1 증가 후 True, 도달 시 증가 없이 False.
    날짜 롤오버 시 자동 리셋. 파일락 + 원자적 rename 으로 read-modify-write 보호.
    실게시 경로의 유일한 카운터 증가 지점.
    락 획득 실패 시 fail-closed: 쿼터는 비용 가드라 무보호 RMW 로 진행하지 않는다."""
    handle = _acquire_lock()
    if handle[0] is None:
        # 주의: 남의 락을 지우면 안 되므로 _release_lock 호출 없이 거부만.
        log.warning("[x-quota] 락 획득 실패(경합/잔존) — 예약 거부(fail-closed)")
        return False
    try:
        raw = _read_raw()
        today = _today()
        count = int(raw.get("count", 0)) if raw.get("date") == today else 0
        if count >= daily_limit():
            return False
        _atomic_write({"date": today, "count": count + 1})
        return True
    finally:
        _release_lock(handle)


# ── 실발송(이번 배포에서는 도달 금지) ────────────────────────────────────────
def _http_post(url: str, **kwargs):
    """requests.post 얇은 래퍼(mock 이음매). 실발송 경로에서만 호출 — dry-run 네트워크 0."""
    import requests  # lazy: dry-run 은 이 함수에 도달하지 않음
    return requests.post(url, **kwargs)


def _resp_text(r) -> str:
    try:
        return getattr(r, "text", "") or ""
    except Exception:
        return ""


def _oauth1():
    """OAuth1 서명 객체(요청별 생성 — env 변경 즉시 반영). 실발송 경로에서만 호출."""
    from requests_oauthlib import OAuth1  # lazy: dry-run 은 도달하지 않음
    k = _keys()
    return OAuth1(k["X_API_KEY"], k["X_API_SECRET"],
                  k["X_ACCESS_TOKEN_OAUTH1"], k["X_ACCESS_SECRET_OAUTH1"])


def _send_tweet(text: str, media_ids=None) -> bool:
    """OAuth1.0a user-context 로 X v2 트윗 게시. 키+플래그 있을 때만 도달.
    OAuth1 은 만료가 없어 리프레시 불요 — 실패는 로그 후 False(재시도 없음)."""
    payload = {"text": text}
    if media_ids:
        payload["media"] = {"media_ids": [str(m) for m in media_ids]}
    r = _http_post(TWEETS_URL, auth=_oauth1(), json=payload, timeout=10)
    ok = getattr(r, "status_code", None) in (200, 201)
    if not ok:
        log.warning("X 게시 실패 status=%s body=%s",
                    getattr(r, "status_code", "?"), _resp_text(r)[:200])
    return ok


def post_disclosure(disclosure: dict, summary_lines=None, impact_block=None) -> dict:
    """공시 1건 → 트윗 게시(또는 dry-run 로그).

    반환 status: dry_run | quota_exceeded | posted | send_failed
    실발송은 x_enabled() 와 키 4종이 모두 있을 때만. dry-run 은 카운터 미소모.
    """
    text, wlen = build_tweet(disclosure, summary_lines, impact_block)
    st = quota_status()

    if st["remaining"] <= 0:
        log.info("[x-skip] 일 한도(%d) 도달 — 게시 스킵 (count=%d)",
                 st["limit"], st["count"])
        return {"status": "quota_exceeded", "text": text, "weighted_len": wlen,
                "quota": st}

    real = x_enabled() and _keys_present()
    if not real:
        why = "X_ENABLED off" if not x_enabled() else "키 미설정"
        log.info("[x-dry-run] (%s) 실발송 없음 · 가중길이=%d/%d\n%s",
                 why, wlen, MAX_WEIGHTED, text)
        return {"status": "dry_run", "reason": why, "text": text,
                "weighted_len": wlen, "quota": st}

    # ── 실발송 경로(키+플래그 둘 다 있을 때만 도달; 이번 배포에서는 실행 안 함) ──
    if not reserve_quota():
        log.info("[x-skip] 예약 시점 한도 도달 — 게시 스킵")
        return {"status": "quota_exceeded", "text": text, "weighted_len": wlen,
                "quota": quota_status()}
    sent = _send_tweet(text)
    return {"status": "posted" if sent else "send_failed", "text": text,
            "weighted_len": wlen, "quota": quota_status()}


# ── 감지 훅(main.poll_once 에서 1줄 호출; 기본 비활성) ────────────────────────
def on_new_disclosure(item: dict, result: dict):
    """신규 공시 감지 시 호출되는 어댑터. 기본 비활성(X_ENABLED/X_DRYRUN 없으면 no-op).
    어떤 예외도 핵심 폴링을 깨지 않게 격리(fail-open)."""
    try:
        if not (x_enabled() or _flag("X_DRYRUN")):
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
        log.warning("[x-hook] 무시(fail-open): %s", e)
        return None


# ── 자체 검증(DoD 증거 산출: dry-run 3건 + 쿼터 가드 단위검증) ────────────────
def _selftest():
    import sys
    import tempfile
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print("=" * 72)
    print("네트워크 미도달 확인: X_ENABLED=%s / 키present=%s → dry-run 경로만"
          % (x_enabled(), _keys_present()))
    print("규제 톤 마커=%r (가중 %d) / 링크 미포함 설계(종량제 절감)"
          % (DISCLAIMER_MARKER, weighted_len(DISCLAIMER_MARKER)))
    print("=" * 72)

    samples = [
        # ① 짧은 유형(자사주) — 통계 있음
        (
            {"corp_name": "삼성전자", "report_nm": "주요사항보고서(자기주식취득결정)"},
            ["삼성전자 · 주요사항보고서(자기주식취득결정)",
             "2026-07-16 접수 · 제출 삼성전자", "분류: 자사주"],
            {"status": "ok", "matched_tag": "자사주",
             "windows": {"w1": {"raw_avg": 1.8, "up_prob": 0.62, "n": 145}}},
        ),
        # ② 통계 pending(집계 중) 유형
        (
            {"corp_name": "에이비엘바이오", "report_nm": "투자판단관련주요경영사항(임상시험계획 승인)"},
            ["에이비엘바이오 · 임상시험계획 승인", "2026-07-16 접수", "분류: 임상"],
            {"status": "pending", "message": "이 유형은 아직 집계 중"},
        ),
        # ③ 초장문 제목+요약 — 축약 동작 강제(가중 280 초과 유도)
        (
            {"corp_name": "케이지모빌리티인터내셔널홀딩스",
             "report_nm": ("단일판매ㆍ공급계약체결(자율주행 통합 플랫폼 및 차세대 "
                           "배터리관리시스템 장기 공급 계약, 계약상대방 복수 완성차 "
                           "제조사 컨소시엄, 계약기간 2026년부터 2031년까지 총 6년)")},
            ["케이지모빌리티 · 장기 공급계약 체결",
             ("복수 완성차 제조사 컨소시엄과 자율주행 플랫폼 및 배터리관리시스템을 "
              "2026년부터 2031년까지 6년간 공급하는 대규모 장기 계약으로 계약금액은 "
              "최근 매출액 대비 상당한 비중을 차지한다"),
             "분류: 공급계약"],
            {"status": "ok", "matched_tag": "공급계약",
             "windows": {"w1": {"raw_avg": -0.7, "up_prob": 0.48, "n": 63}}},
        ),
    ]

    print("\n[A] dry-run 트윗 포맷 3건 (가중길이 ≤ %d 증명)\n" % MAX_WEIGHTED)
    all_ok = True
    for i, (item, summ, imp) in enumerate(samples, 1):
        res = post_disclosure(item, summary_lines=summ, impact_block=imp)
        wl = res["weighted_len"]
        ok = wl <= MAX_WEIGHTED and res["status"] == "dry_run"
        all_ok &= ok
        print("--- 샘플 %d (%s) status=%s ---" % (i, item["corp_name"], res["status"]))
        print(res["text"])
        print(">> 가중길이 = %d / %d  (원문자수=%d)  →  %s"
              % (wl, MAX_WEIGHTED, len(res["text"]), "OK" if wl <= MAX_WEIGHTED else "초과!"))
        if i == 3:
            print(">> (축약 검증) 말줄임 포함 = %s" % (ELLIPSIS in res["text"]))
        print()

    print("=" * 72)
    print("[B] 쿼터 가드 단위검증 (임시 카운터파일 · 네트워크 0)\n")
    global COUNTER_FILE
    orig = COUNTER_FILE
    tmpdir = tempfile.mkdtemp(prefix="x_quota_")
    COUNTER_FILE = Path(tmpdir) / "x_post_counter.json"
    os.environ["X_DAILY_LIMIT"] = "3"
    try:
        today = _today()

        # B-1: 0부터 채우기 — 한도(3)까지 True, 초과분 False + 카운터 불변
        print("B-1 한도채움(limit=3):")
        COUNTER_FILE.write_text(json.dumps({"date": today, "count": 0}), encoding="utf-8")
        seq = [reserve_quota() for _ in range(5)]
        after = _normalized_count()
        print("   reserve 5회 결과 =", seq, "→ 기대 [T,T,T,F,F]")
        print("   최종 count =", after, "→ 기대 3 (한도 초과분 미증가)")
        b1 = seq == [True, True, True, False, False] and after == 3

        # B-2: 한도 도달 상태에서 post_disclosure 스킵 + 카운터 불변
        print("B-2 한도도달 시 post 스킵:")
        before = _normalized_count()
        r = post_disclosure({"corp_name": "테스트", "report_nm": "테스트공시"},
                            summary_lines=["a", "b", "c"], impact_block=None)
        b2 = r["status"] == "quota_exceeded" and _normalized_count() == before
        print("   post status =", r["status"], "/ count", before, "→", _normalized_count(),
              "→", "OK" if b2 else "FAIL")

        # B-3: 날짜 롤오버 리셋 — 어제 999 → 오늘 첫 예약이 1
        print("B-3 날짜 롤오버 리셋:")
        COUNTER_FILE.write_text(json.dumps({"date": "2000-01-01", "count": 999}),
                                encoding="utf-8")
        g = reserve_quota()
        after3 = _normalized_count()
        b3 = g is True and after3 == 1
        print("   어제 count=999 → 오늘 reserve =", g, "/ 오늘 count =", after3,
              "→", "OK" if b3 else "FAIL")

        print()
        print("쿼터검증 종합:", "PASS" if (b1 and b2 and b3) else "FAIL",
              "(B-1=%s B-2=%s B-3=%s)" % (b1, b2, b3))
        quota_ok = b1 and b2 and b3
    finally:
        COUNTER_FILE = orig
        os.environ.pop("X_DAILY_LIMIT", None)
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ── [C] OAuth1.0a 게시 경로 목 검증 (실API 0) ────────────────────────────
    print("=" * 72)
    print("[C] OAuth1.0a 게시 목 검증 (mock _http_post · 네트워크 0)\n")
    global _http_post
    orig_http = _http_post

    class _R:  # 목 응답
        def __init__(s, status, payload=None, text=""):
            s.status_code = status; s._p = payload or {}; s.text = text
        def json(s): return s._p

    def _mock(sequence):
        state = {"i": 0, "urls": [], "kw": []}
        def fake(url, **kw):
            state["urls"].append(url); state["kw"].append(kw)
            resp = sequence[min(state["i"], len(sequence) - 1)]
            state["i"] += 1
            return resp
        return fake, state

    for k, v in {"X_API_KEY": "ck", "X_API_SECRET": "cs",
                 "X_ACCESS_TOKEN_OAUTH1": "at", "X_ACCESS_SECRET_OAUTH1": "as"}.items():
        os.environ[k] = v
    try:
        # C-1: 201 → 성공, OAuth1 서명 객체가 auth 로 전달 + media_ids 페이로드 반영
        print("C-1 게시 성공 + OAuth1 auth 전달 + media_ids:")
        _http_post, st1 = _mock([_R(201, {"data": {"id": "1"}})])
        ok1 = _send_tweet("hello", media_ids=["123"])
        kw1 = st1["kw"][0]
        c1 = (ok1 is True and st1["urls"] == [TWEETS_URL]
              and kw1.get("auth") is not None
              and kw1.get("json", {}).get("media", {}).get("media_ids") == ["123"])
        print("   send=%s url=%s auth전달=%s media=%s → %s"
              % (ok1, st1["urls"][0].rsplit("/", 1)[-1], kw1.get("auth") is not None,
                 kw1.get("json", {}).get("media"), "OK" if c1 else "FAIL"))

        # C-2: 401/403 → False (재시도 없음 — OAuth1 은 만료 개념 없음, 키 문제로 판단)
        print("C-2 401 실패 → send_failed(재시도 없음):")
        _http_post, st2 = _mock([_R(401, text="unauthorized")])
        ok2 = _send_tweet("hi")
        c2 = ok2 is False and len(st2["urls"]) == 1
        print("   send=%s 호출수=%d (기대 False·1회) → %s"
              % (ok2, len(st2["urls"]), "OK" if c2 else "FAIL"))

        print()
        print("OAuth1검증 종합:", "PASS" if (c1 and c2) else "FAIL",
              "(C-1=%s C-2=%s)" % (c1, c2))
        auth_ok = c1 and c2
    finally:
        _http_post = orig_http
        for k in ("X_API_KEY", "X_API_SECRET",
                  "X_ACCESS_TOKEN_OAUTH1", "X_ACCESS_SECRET_OAUTH1"):
            os.environ.pop(k, None)

    print("=" * 72)
    print("종합 DoD:", "PASS" if (all_ok and quota_ok and auth_ok) else "FAIL",
          "| dry-run3건=%s 쿼터=%s OAuth1=%s | 실게시/실API 호출 0(모두 mock)"
          % (all_ok, quota_ok, auth_ok))
    return all_ok and quota_ok and auth_ok


if __name__ == "__main__":
    ok = _selftest()
    raise SystemExit(0 if ok else 1)
