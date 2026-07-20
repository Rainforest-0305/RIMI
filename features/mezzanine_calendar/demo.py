# -*- coding: utf-8 -*-
"""RIMI 메자닌 캘린더 데모 — 단독 실행 파이프라인.

실행법 (둘 다 동작):
  python features/mezzanine_calendar/demo.py
  python -m features.mezzanine_calendar.demo

전체 파이프라인:
  collect -> calendar_view(캘린더 + 종목별 물량) -> price_parity(비-DART 스모크)
요약을 콘솔 출력 + 파일(out_review/mezz_demo_summary.json, utf-8)로 저장.
라이브 DART 콜 = 0.
"""
import json
import os
import sys
import time
from datetime import date

# --- repo 루트 + 모듈 디렉터리 sys.path 삽입 (2가지 실행법 모두 대응) ---
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
for _p in (_REPO_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# flat import (모듈 디렉터리가 path 에 있음). 실패 시 패키지 경로 폴백.
try:
    import collect as _collect
    import calendar_view as _cal
    import price_parity as _pp
    import enrich as _enrich
except ImportError:  # pragma: no cover
    from features.mezzanine_calendar import collect as _collect
    from features.mezzanine_calendar import calendar_view as _cal
    from features.mezzanine_calendar import price_parity as _pp
    from features.mezzanine_calendar import enrich as _enrich


def run(do_parity: bool = True, parity_top_n: int = 5):
    t0 = time.time()

    records, stats = _collect.collect_all()
    calendar, skipped_no_start = _cal.build_calendar(records)
    upcoming, _ = _cal.build_calendar(records, upcoming_only=True)
    holdings = _cal.build_holdings(records)

    parity = None
    enriched = None
    if do_parity:
        parity = _pp.run_parity_smoke(holdings, top_n=parity_top_n)
        # ③④ 시세연계 인리치 (비-DART, 상위 N종목 라이브만)
        enriched = _enrich.enrich_top_holdings(holdings, top_n=parity_top_n)

    elapsed = round(time.time() - t0, 2)

    # 샘플 레코드 1~2개 (타입 다양성 위해 CB/BW 하나씩 시도)
    samples = []
    seen_types = set()
    for r in records:
        if r.sec_type not in seen_types:
            samples.append(r.to_dict())
            seen_types.add(r.sec_type)
        if len(samples) >= 2:
            break

    summary = {
        "generated_at": date.today().isoformat(),
        "dart_live_calls": 0,
        "elapsed_sec": elapsed,
        "collect": {
            "files_total": stats.files_total,
            "records_total": stats.rows_total,
            "by_type": stats.by_type,
            "parse_fail": {
                "start_date": stats.fail_start,
                "end_date": stats.fail_end,
                "maturity_date": stats.fail_maturity,
                "issue_amount": stats.fail_amount,
                "conv_price": stats.fail_price,
                "shares": stats.fail_shares,
            },
            "stock_code_hit": stats.stock_code_hit,
            "stock_code_miss": stats.stock_code_miss,
        },
        "calendar": {
            "items_total": len(calendar),
            "items_upcoming": len(upcoming),
            "skipped_no_start_date": skipped_no_start,
            "first_3": calendar[:3],
            "next_upcoming_3": upcoming[:3],
        },
        "holdings": {
            "corp_count": len(holdings),
            "top_5_by_active_shares": [
                {
                    "corp_name": h["corp_name"],
                    "stock_code": h["stock_code"],
                    "sec_types": h["sec_types"],
                    "tranche_count": h["tranche_count"],
                    "total_shares": h["total_shares"],
                    "active_shares": h["active_shares"],
                    "min_conv_price": h["min_conv_price"],
                }
                for h in holdings[:5]
            ],
        },
        "price_parity": parity,
        "enrichment": enriched,
        "sample_records": samples,
    }
    return summary


def _print_summary(s: dict):
    c = s["collect"]
    cal = s["calendar"]
    print("=" * 60)
    print("RIMI 메자닌(CB/BW/EB) 캘린더 데모 요약")
    print("=" * 60)
    print("생성일:", s["generated_at"], " 소요초:", s["elapsed_sec"])
    print("라이브 DART 콜:", s["dart_live_calls"])
    print("-" * 60)
    print("공시 파일수:", c["files_total"], " 총 레코드:", c["records_total"])
    print("타입별:", c["by_type"])
    print("파싱실패:", c["parse_fail"])
    print("stock_code 매핑 hit/miss:", c["stock_code_hit"], "/", c["stock_code_miss"])
    print("-" * 60)
    print("캘린더 항목수:", cal["items_total"],
          " (다가오는 개시:", cal["items_upcoming"], ")",
          " 개시일None제외:", cal["skipped_no_start_date"])
    print("종목수:", s["holdings"]["corp_count"])
    print("-" * 60)
    if s["price_parity"] is not None:
        p = s["price_parity"]
        print("price parity(비-DART): checked=%d skip_no_code=%d skip_no_price=%d fetch_fail=%d"
              % (p["checked"], p["skipped_no_code"], p["skipped_no_price"], p["fetch_fail"]))
        for r in p["results"]:
            print("  ", r["corp_name"], r["stock_code"],
                  "전환가", r["conv_price"], "현재가", r["current_price"],
                  "괴리%", r["parity_pct"], r["source"])
    else:
        print("price parity: 스킵")
    print("-" * 60)
    e = s.get("enrichment")
    if e is not None:
        print("③④ 인리치(비-DART): checked=%d price_fail=%d mktcap_fail=%d skip_no_code=%d skip_no_price=%d"
              % (e["checked"], e["price_fail"], e["mktcap_fail"],
                 e["skipped_no_code"], e["skipped_no_price"]))
        print("③ moneyness 분포(종목 min전환가 기준):", e["moneyness_dist"],
              " 트랜치 기준:", e["tranche_moneyness_dist"])
        print("④ 시총대비 희석% 분포(총물량):", e["dilution_stats"])
        for r in e["results"]:
            print("  %s %s [%s] 현재가=%s 최저전환가=%s money=%s 프리미엄%%=%s 시총=%s 희석%%(총/활성)=%s/%s (%s)"
                  % (r["corp_name"], r["stock_code"], r["market"],
                     r["current_price"], r["min_conv_price"], r["moneyness"],
                     r["premium_pct"], r["market_cap"],
                     r["dilution_vs_mktcap_pct"], r["active_dilution_vs_mktcap_pct"],
                     r["mktcap_source"]))
    else:
        print("③④ 인리치: 스킵")
    print("=" * 60)


def main():
    do_parity = "--no-parity" not in sys.argv
    summary = run(do_parity=do_parity)
    _print_summary(summary)

    # 격리 준수: 산출물은 모듈 디렉터리 안에만 쓴다.
    out_path = os.path.join(_HERE, "mezz_demo_summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("요약 저장:", out_path)
    return summary


if __name__ == "__main__":
    main()
