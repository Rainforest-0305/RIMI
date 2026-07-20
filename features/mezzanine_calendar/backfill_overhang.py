# -*- coding: utf-8 -*-
"""WS-34 순잔량 오버행 백필 → 커밋용 스냅샷 mezz_overhang.json 생성.

로컬 전용(pandas·DART 콜 필요). Render 런타임은 산출 스냅샷만 읽는다.
우선 build_holdings active_shares desc 상위 N종목만 백필해 라이브 교정 증거 확보.
전체 커버리지는 로그로 남긴다(silent 절단 금지).

실행:  python features/mezzanine_calendar/backfill_overhang.py --top 60
"""
import argparse
import json
import os
import sys
import time
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
for p in (_REPO_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import collect as _collect          # noqa: E402
import calendar_view as _cal        # noqa: E402
import overhang as _ov              # noqa: E402


def snapshot_path():
    return os.path.join(_HERE, "mezz_overhang.json")


def _trim(ov):
    """스냅샷 저장용 축약(트랜치 상세는 audit 위해 보존하되 컴팩트)."""
    return {
        "corp_code": ov["corp_code"],
        "corp_name": ov["corp_name"],
        "stock_code": ov["stock_code"],
        "as_of": ov["as_of"],
        "sec_types": ov["sec_types"],
        "net_remaining_total": ov["net_remaining_total"],
        "gross_shares_total": ov["gross_shares_total"],
        "expired_shares_dropped": ov["expired_shares_dropped"],
        "min_conv_price": ov["min_conv_price"],
        "latest_conv_price_by_tranche": ov["latest_conv_price_by_tranche"],
        "net_remaining_shares_by_tranche": ov["net_remaining_shares_by_tranche"],
        "tranches": ov["tranches"],
        "coverage": ov["coverage"],
    }


def run(top_n=60, sleep=0.15, as_of=None, refresh_list=True, limit_total=None):
    as_of = as_of or date.today().isoformat()
    records, cstats = _collect.collect_all()
    holdings = _cal.build_holdings(records)
    total_stocks = len(holdings)

    # enrich 후보와 동일 기준: stock_code + min_conv_price 있는 종목
    candidates = [h for h in holdings
                  if h.get("stock_code") and h.get("min_conv_price")]
    if limit_total:
        candidates = candidates[:limit_total]
    targets = candidates[:top_n]

    print(f"[backfill] total_holdings={total_stocks} candidates={len(candidates)} "
          f"targets(top {top_n})={len(targets)} as_of={as_of}")

    stocks = {}
    total_calls = 0
    fail = 0
    t0 = time.time()
    for i, h in enumerate(targets, 1):
        cc = h["corp_code"]
        nm = h.get("corp_name", "")
        sc = h.get("stock_code")
        try:
            ov = _ov.compute_overhang(cc, corp_name=nm, stock_code=sc,
                                      as_of=as_of, refresh_list=refresh_list)
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  [{i}/{len(targets)}] {nm}({sc}) FAIL {type(e).__name__}: {str(e)[:120]}")
            continue
        calls = ov["coverage"]["dart_calls"]
        total_calls += calls
        gross = ov["gross_shares_total"]
        net = ov["net_remaining_total"]
        red = (1 - net / gross) * 100 if gross else 0
        stocks[cc] = _trim(ov)
        errflags = []
        if ov["coverage"].get("refix_list_err"):
            errflags.append("refixErr")
        if ov["coverage"].get("conv_list_err"):
            errflags.append("convErr")
        if ov["coverage"].get("refix_doc_fail") or ov["coverage"].get("conv_doc_fail"):
            errflags.append(f"docFail={ov['coverage']['refix_doc_fail']+ov['coverage']['conv_doc_fail']}")
        print(f"  [{i}/{len(targets)}] {nm}({sc}) gross={gross:,} net={net:,} "
              f"(-{red:.0f}%) calls={calls} {' '.join(errflags)}")
        if sleep:
            time.sleep(sleep)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "as_of": as_of,
        "schema": "ws34-overhang-v1",
        "note": ("종목별 순잔량(net remaining) 오버행. net=전환청구기간내 미전환 잔량"
                 "(만료·전환완료 제외, 회차별 최근언급 잔액/리픽싱 조정후 반영)."),
        "count": len(stocks),
        "total_holdings": total_stocks,
        "candidates": len(candidates),
        "coverage": {
            "covered": len(stocks),
            "not_covered": total_stocks - len(stocks),
            "target_fail": fail,
            "total_dart_calls": total_calls,
        },
        "stocks": stocks,
    }
    sp = snapshot_path()
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    dt = time.time() - t0
    print(f"[backfill] DONE covered={len(stocks)}/{total_stocks} "
          f"fail={fail} calls={total_calls} {dt:.0f}s -> {sp}")
    return payload


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--limit-total", type=int, default=None)
    args = ap.parse_args()
    run(top_n=args.top, sleep=args.sleep, as_of=args.as_of,
        limit_total=args.limit_total)
