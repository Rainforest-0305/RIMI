# -*- coding: utf-8 -*-
"""데일리 큐레이션 포스트 생성기 — "📊 오늘 터진 공시 TOP N(중요도순)".

**add-only 모듈. app.py/main.py/tg_channel.py 코어 무손상.**
tg_channel.py 와 동일하게 *임포트만으로는 네트워크 0*(config side-effect 로 env
로드만). 실발행(sendMessage/sendPhoto)은 이 모듈 어디에서도 호출하지 않는다 —
dry-run(파일 저장 + stdout)만 수행한다. 실발행/스케줄 등록은 Partner 게이트.

기존 모듈 재사용(신규 데이터소스 없음):
  - 데이터  : dart_poll.fetch_markets(days=1, markets=("Y","K"))  (KOSPI+KOSDAQ 실공시)
  - 시장라벨: dart_poll.market_label(corp_cls)
  - 분류/요약: summarize.classify / summarize.summarize
  - 과거통계: impact.impact_for_tags  (로컬 impact_benchmark.json 읽기 — 네트워크 0)
  - 규모적격: scale_extract.bullet_eligible  (제목 규칙 판정 — 네트워크 0)
  - 발행유틸: tg_channel._fit / impact_stat_line / APP_URL / DISCLAIMER / MESSAGE_LIMIT

콜예산(중요):
  (1) 랭킹은 **네트워크 0 신호만** 사용 → 시장전체 공시에 대해 DART 추가호출 없음.
      (유형가중 + impact 크기(로컬파일) + bullet_eligible(제목규칙)).
      ※ scale_extract.scale_lookup 은 항목당 DART 문서호출을 유발하므로 **호출하지
        않는다**(콜예산 보호). 규모신호는 0-네트워크 프록시 bullet_eligible 로 대체.
  (2) 요약(summarize.summarize)은 **먼저 랭킹으로 TOP N 을 추린 뒤 그 N 건에만** 호출
      하고, 호출 총량에 하드캡(<=N, 기본 6)을 건다. LLM 훅 미주입 시 summarize 는
      규칙기반(네트워크 0)으로 폴백 → 무한재시도·과금폭주 없음.
  (3) DART 페이지네이션은 fetch_markets 의 max_pages 로 상한 유지.
"""
import argparse
import os
import re
import sys
from datetime import datetime

# config 임포트: side-effect 로 .env(+kis .env) 를 os.environ 에 로드(네트워크 0).
try:
    import config  # noqa: F401
    _BASE = config.BASE
except Exception:  # pragma: no cover - config 없이도 모듈은 살아있게
    from pathlib import Path
    _BASE = Path(__file__).parent

import dart_poll
import summarize as _sz
import impact as _impact
import scale_extract as _scale
import tg_channel as _tg

OUT_DIR = _BASE / "out_review"

DEFAULT_TOPN = 6

# 공시유형 가중(0-네트워크). summarize.classify 태그 → 중요도 기본점.
# 값 근거: 지분·자본구조 변동(합병/최대주주/증자/사채) > 자본정책(자사주/소각/배당)
# > 영업이벤트(공급/실적/임상/소송) > 지분변동/정정 > 기타. 한 항목의 여러 태그 중
# 최댓값을 기본점으로 쓴다.
TYPE_WEIGHT = {
    "최대주주변경": 10.0,
    "합병분할": 10.0,
    "유상증자": 9.0,
    "전환사채": 8.0,
    "감사보고서": 8.0,
    "주식소각": 8.0,
    "공급계약": 7.0,
    "자사주": 7.0,
    "무상증자": 6.0,
    "실적": 6.0,
    "소송": 6.0,
    "임상": 6.0,
    "배당": 5.0,
    "지분변동": 4.0,
    "정정공시": 2.0,
    "기타공시": 1.0,
}


def _topn_env(default=DEFAULT_TOPN) -> int:
    try:
        n = int(os.environ.get("DAILY_CURATION_TOPN", str(default)))
        return n if n > 0 else default
    except (ValueError, TypeError):
        return default


def _rcept_date(item) -> str:
    """항목의 접수일 YYYYMMDD(8자리). rcept_dt 우선, 없으면 rcept_no 앞 8자리."""
    raw = str(item.get("rcept_dt") or "").strip()
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    rno = "".join(c for c in str(item.get("rcept_no") or "") if c.isdigit())
    return rno[:8] if len(rno) >= 8 else ""


# 시장운영기관(거래소/시장본부) 행정·시장조치 공시는 발행사 자본이벤트가 아니므로
# 큐레이션에서 제외(과거 자본이벤트 통계 오귀속 방지). add-only 필터(core 무접촉).
_NOISE_TITLE_KWS = (
    "주권매매거래정지", "매매거래정지", "정리매매", "투자주의", "투자경고",
    "투자위험", "상장폐지", "관리종목", "불성실공시", "조회공시", "풍문또는보도",
)
_NOISE_FILER_KWS = ("시장본부", "거래소")


def _is_noise(item) -> bool:
    """거래소/시장본부 행정·시장조치 공시(발행사 자본이벤트 아님) → 큐레이션 제외."""
    title = (item.get("report_nm") or "").replace(" ", "")
    if any(k in title for k in _NOISE_TITLE_KWS):
        return True
    flr = (item.get("flr_nm") or "").replace(" ", "")
    if any(k in flr for k in _NOISE_FILER_KWS):
        return True
    return False


def _impact_ok(block) -> bool:
    return isinstance(block, dict) and block.get("status") == "ok"


def _score(tags, impact_block, report_nm) -> float:
    """0-네트워크 중요도 점수. (유형가중 최댓값)+(impact 크기·표본)+(규모적격 보너스)."""
    base = max((TYPE_WEIGHT.get(t, 1.0) for t in (tags or ["기타공시"])), default=1.0)

    imp = 0.0
    if _impact_ok(impact_block):
        w1 = (impact_block.get("windows") or {}).get("w1") or {}
        avg = w1.get("raw_avg")
        n = w1.get("n")
        if isinstance(avg, (int, float)) and not isinstance(avg, bool):
            imp += min(abs(float(avg)), 15.0) * 0.3          # 최대 +4.5
        if isinstance(n, (int, float)) and not isinstance(n, bool):
            if n >= 80:
                imp += 1.0
            elif n >= 30:
                imp += 0.5

    elig = 1.5 if _scale.bullet_eligible(report_nm) else 0.0
    return base + imp + elig


def _fetch_items(days=1):
    """fetch_markets 래퍼 + DART list.json 페이지 호출수 계측.
    반환: (items, errors, dart_page_calls)."""
    orig = dart_poll._request_list
    counter = [0]

    def _counted(*a, **k):
        counter[0] += 1
        return orig(*a, **k)

    dart_poll._request_list = _counted
    try:
        items, errors = dart_poll.fetch_markets(days=days, markets=("Y", "K"))
    finally:
        dart_poll._request_list = orig
    return items, errors, counter[0]


def build_curation(topn=None):
    """오늘(개장일 당일) TOP N 큐레이션 포스트 텍스트 + 메타 산출.

    반환 dict: text, target_date(YYYYMMDD), topn, items(선정 N건 요약),
               dart_page_calls, summary_calls, market_counts, errors, fallback.
    실발행은 하지 않는다(dry-run 전용).
    """
    n = topn if (isinstance(topn, int) and topn > 0) else _topn_env()

    items, errors, dart_calls = _fetch_items(days=1)

    # ── 대상 개장일 확정: 오늘자 있으면 오늘, 없으면(주말/휴장) 최근 개장일 폴백 ──
    today = datetime.now().strftime("%Y%m%d")
    dates = sorted({d for d in (_rcept_date(it) for it in items) if d}, reverse=True)
    fallback = False
    if today in dates:
        target = today
    elif dates:
        target = dates[0]
        fallback = True
    else:
        # days=1 창에 아무것도 없음(예: 월요일 새벽) → 최근 개장일 탐색용 넓은 창 폴백.
        wide, werr, wcalls = _fetch_items(days=5)
        dart_calls += wcalls
        errors = list(errors) + list(werr)
        items = wide
        dates = sorted({d for d in (_rcept_date(it) for it in items) if d}, reverse=True)
        target = dates[0] if dates else today
        fallback = target != today

    raw_day_items = [it for it in items if _rcept_date(it) == target]
    day_items = [it for it in raw_day_items if not _is_noise(it)]

    # ── 랭킹(네트워크 0): 태그·impact·규모적격 산출 후 점수화 ─────────────────
    ranked = []
    for it in day_items:
        report_nm = it.get("report_nm", "")
        tags = _sz.classify(report_nm)                 # 0-네트워크
        impact_block = _impact.impact_for_tags(tags)   # 로컬파일 읽기(0-네트워크)
        sc = _score(tags, impact_block, report_nm)
        ranked.append((sc, it, tags, impact_block))
    # 점수 desc, 동점은 접수번호 desc(최신 우선)
    ranked.sort(key=lambda r: (r[0], str(r[1].get("rcept_no") or "")), reverse=True)

    top = ranked[:n]

    # ── 요약: TOP N 에만 호출(하드캡 <=N). LLM 훅 미주입 시 규칙기반(0-네트워크) ──
    summary_calls = 0
    rendered = []
    for sc, it, tags, impact_block in top:
        if summary_calls >= n:                         # 하드캡 안전장치
            break
        summ = _sz.summarize(it)                       # 요약 호출(계측 대상)
        summary_calls += 1
        rendered.append({
            "score": round(sc, 2),
            "item": it,
            "tags": tags,
            "impact_block": impact_block,
            "summary": summ.get("summary") or [],
        })

    text = _render_text(rendered, target, n)

    market_counts = {"KOSPI": 0, "KOSDAQ": 0}
    for it in day_items:
        lbl = dart_poll.market_label(it.get("corp_cls"))
        if lbl in market_counts:
            market_counts[lbl] += 1

    return {
        "text": text,
        "target_date": target,
        "topn": n,
        "selected": len(rendered),
        "day_pool": len(day_items),
        "raw_day_pool": len(raw_day_items),
        "items": rendered,
        "dart_page_calls": dart_calls,
        "summary_calls": summary_calls,
        "market_counts": market_counts,
        "errors": errors,
        "fallback": fallback,
    }


# 시장 표시라벨: market_label(KOSPI/KOSDAQ) → 한글 표기(코스피/코스닥). 스코프 유지.
_MARKET_KO = {"KOSPI": "코스피", "KOSDAQ": "코스닥", "KONEX": "코넥스"}


def _market_ko(corp_cls) -> str:
    lbl = dart_poll.market_label(corp_cls)
    return _MARKET_KO.get(lbl, lbl)


_BIGO_TAIL = re.compile(r"\s*·\s*비고\s+.{1,4}$")


def _cur_norm(s) -> str:
    """비교용 정규화: 공백·구분기호 제거."""
    return "".join(ch for ch in str(s or "")
                   if not ch.isspace() and ch not in "·-[]().,ㆍ")


def _render_text(rendered, target_date, n) -> str:
    """선정 항목 → 최종 포스트 텍스트. 톤: 과거통계·정보가치 중심(과장·권유 금지).
    tg_channel._fit 로 최종 4096(코드포인트) 이내 보장."""
    ymd = f"{target_date[0:4]}-{target_date[4:6]}-{target_date[6:8]}" \
        if len(target_date) == 8 else target_date

    lines = [f"📊 오늘 터진 공시 TOP {len(rendered)} · {ymd} (중요도순)"]

    for i, r in enumerate(rendered, 1):
        it = r["item"]
        corp = (it.get("corp_name") or "").strip()
        report_nm = (it.get("report_nm") or "").strip()
        mkt = _market_ko(it.get("corp_cls"))
        head = f"{i}. [{corp}] ({mkt}) {report_nm}"

        lines.append("")            # 항목 간 여백(가독 리듬)
        lines.append(head)
        corp_key = _cur_norm(corp) + _cur_norm(report_nm)
        for ln in (r["summary"] or []):
            t = str(ln or "").strip()
            if not t:
                continue
            t = _BIGO_TAIL.sub("", t).strip()          # DART 내부 '비고 X' 코드 제거
            if not t:
                continue
            nt = _cur_norm(t)
            if nt and (nt == corp_key or nt in corp_key):  # 헤더 재진술 라인 제거
                continue
            lines.append(f"   - {t}")
        # 과거 유사공시 통계 1줄(있을 때만). tg_channel 유틸 재사용.
        if _impact_ok(r["impact_block"]):
            lines.append(f"   · {_tg.impact_stat_line(r['impact_block'])}")

    lines.append("")
    lines.append(_tg.APP_URL)       # 텔레그램 링크 무과금 → 본문 포함
    lines.append(_tg.DISCLAIMER)    # "정보 제공용·투자 권유 아님"

    text = "\n".join(lines)
    return _tg._fit(text, _tg.MESSAGE_LIMIT)   # 최종 4096 이내 보장


def save_dry_run(result) -> str:
    """렌더 텍스트를 out_review/daily_curation_YYYYMMDD.txt 로 저장. 경로 반환.
    ※ 텔레그램 API 는 호출하지 않는다(dry-run 전용)."""
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"daily_curation_{result['target_date']}.txt"
    path.write_text(result["text"], encoding="utf-8")
    return str(path)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="MIRI 데일리 큐레이션 포스트 dry-run 렌더(실발행 없음).")
    ap.add_argument("--topn", type=int, default=None,
                    help="상위 N건(기본: env DAILY_CURATION_TOPN 또는 6)")
    args = ap.parse_args(argv)

    result = build_curation(topn=args.topn)
    path = save_dry_run(result)
    text = result["text"]

    print(text)
    print("\n" + "=" * 72)
    print("[dry-run 메타 — 실발행 0]")
    print(f"  저장경로      : {path}")
    print(f"  대상 개장일   : {result['target_date']}"
          + ("  (폴백: 오늘자 없음 → 최근 개장일)" if result["fallback"] else "  (오늘자)"))
    print(f"  당일 공시풀   : {result['day_pool']}건(필터후) / {result['raw_day_pool']}건(원시) "
          f"(코스피 {result['market_counts'].get('KOSPI', 0)} / "
          f"코스닥 {result['market_counts'].get('KOSDAQ', 0)})")
    print(f"  선정          : TOP {result['topn']} 중 {result['selected']}건 렌더")
    print(f"  렌더 길이     : {len(text)} 코드포인트 (<=4096: {len(text) <= 4096})")
    print(f"  DART 페이지호출: {result['dart_page_calls']}회")
    print(f"  요약 호출     : {result['summary_calls']}회 (하드캡 <= {result['topn']})")
    print(f"  DART 에러     : {result['errors'] or '없음'}")
    print("  텔레그램 발행 : 0 (sendMessage/sendPhoto 미호출)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
