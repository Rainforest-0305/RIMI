# -*- coding: utf-8 -*-
"""단독 실행 데모: 캐시 파이프라인(collect→predict) + 라이브 프로브 요약.

두 방식 모두 지원:
  python features/earnings_calendar/demo.py         (스크립트 직접)
  python -m features.earnings_calendar.demo         (모듈)
어느 쪽이든 저장소 루트를 sys.path 에 삽입해 dart_poll/config 절대 import.

결과는 콘솔 + 파일(features/earnings_calendar/out/) 둘 다 남긴다(콘솔 mojibake
무관하게 파일로 검증). 결과 요약: 정기보고서 추출건수, 종목수, 다음발표 예상
생성수, 라이브 조회 종목수·실측 DART콜수, 소요초, 샘플 레코드.
"""
import os
import sys
import io
import json
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import collect       # noqa: E402
import predict       # noqa: E402
import live_probe    # noqa: E402

OUT_DIR = os.path.join(_HERE, "out")


def run(live=True, live_n=5):
    t0 = time.time()
    # --- 소스1: 캐시 파이프라인 ---
    records, cstats = collect.load_all()
    groups = collect.group_by_corp(records)
    as_of = collect.dataset_as_of(records)
    preds, skipped = predict.predict_all(groups)

    # 신뢰도 분포
    conf_dist = {}
    method_dist = {}
    for p in preds:
        conf_dist[p["confidence"]] = conf_dist.get(p["confidence"], 0) + 1
        method_dist[p["method"]] = method_dist.get(p["method"], 0) + 1

    # 시장분포(코스피/코스닥) — 추출 정기보고서 rows 기준 & 예측(종목) 기준
    ext_market = {}
    for r in records:
        ext_market[r["market"]] = ext_market.get(r["market"], 0) + 1
    pred_market = {}
    for p in preds:
        pred_market[p["market"]] = pred_market.get(p["market"], 0) + 1

    # 데이터 기준일(as_of) 이후 예상건(미래 발표) 카운트
    future = [p for p in preds if p["predicted_date"].replace("-", "") > as_of]

    cache_sec = round(time.time() - t0, 2)

    # --- 소스2: 라이브 프로브 ---
    if live:
        lp = live_probe.probe(max_n=live_n)
    else:
        lp = {"skipped": True, "reason": "live=False", "http_calls": 0,
              "fetch_calls": 0, "probed": [], "elapsed_sec": 0.0,
              "errors": [], "stocks_planned": 0}

    elapsed = round(time.time() - t0, 2)

    summary = {
        "as_of_snapshot": as_of,
        "cache": {
            "files_scanned": cstats["files"],
            "rows_scanned": cstats["scanned_rows"],
            "periodic_extracted": cstats["periodic_rows"],
            "corps": len(groups),
            "predictions_generated": len(preds),
            "predict_skipped": skipped,
            "future_after_asof": len(future),
            "confidence_dist": conf_dist,
            "method_dist": method_dist,
            "market_dist_extracted": ext_market,
            "market_dist_predictions": pred_market,
            "cache_pipeline_sec": cache_sec,
        },
        "live": {
            "skipped": lp["skipped"],
            "reason": lp["reason"],
            "key_loaded": (not lp["skipped"]) or (lp["reason"] != "키 미로드로 라이브 스킵"),
            "stocks_planned": lp["stocks_planned"],
            "stocks_probed": len(lp["probed"]),
            "dart_http_calls_measured": lp["http_calls"],
            "fetch_calls": lp["fetch_calls"],
            "errors": lp["errors"],
            "live_sec": lp["elapsed_sec"],
        },
        "elapsed_sec": elapsed,
        "sample_predictions": preds[:3],
        "sample_live": lp["probed"][:3],
    }

    # 파일로 저장(검증용)
    os.makedirs(OUT_DIR, exist_ok=True)
    with io.open(os.path.join(OUT_DIR, "demo_result.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    # 예측 전량도 별도 저장(캘린더 산출물)
    with io.open(os.path.join(OUT_DIR, "earnings_calendar.json"), "w", encoding="utf-8") as fh:
        json.dump({"as_of": as_of, "count": len(preds), "predictions": preds},
                  fh, ensure_ascii=False, indent=2)

    _print_summary(summary)
    return summary


def _print_summary(s):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    c, l = s["cache"], s["live"]
    lines = [
        "=== RIMI earnings_calendar 데모 요약 ===",
        f"데이터 기준일(as_of): {s['as_of_snapshot']}",
        f"[캐시] 파일 {c['files_scanned']}개 / 스캔 {c['rows_scanned']}행 "
        f"→ 정기보고서 추출 {c['periodic_extracted']}건",
        f"[캐시] 종목수 {c['corps']} / 다음발표 예상 생성 {c['predictions_generated']}건 "
        f"(스킵 {c['predict_skipped']}, as_of 이후 미래건 {c['future_after_asof']})",
        f"[캐시] 시장분포 추출건 {c['market_dist_extracted']} / 예측종목 {c['market_dist_predictions']}",
        f"[캐시] 신뢰도분포 {c['confidence_dist']} / 방식 {c['method_dist']} "
        f"/ 소요 {c['cache_pipeline_sec']}s",
        f"[라이브] skipped={l['skipped']} reason='{l['reason']}' key_loaded={l['key_loaded']}",
        f"[라이브] 조회종목 {l['stocks_probed']}/{l['stocks_planned']} "
        f"/ 실측 DART HTTP콜 {l['dart_http_calls_measured']} "
        f"/ fetch콜 {l['fetch_calls']} / errors={l['errors']} / {l['live_sec']}s",
        f"총 소요: {s['elapsed_sec']}s",
        "--- 샘플 예측 ---",
    ]
    for p in s["sample_predictions"]:
        lines.append(
            f"  {p.get('stock_code')} {p.get('corp_name')} "
            f"[{p.get('market')}] 다음:{p.get('target_type')} "
            f"예상 {p.get('predicted_date')} ({p.get('method')}/{p.get('confidence')}) "
            f"| {p.get('basis')}")
    lines.append("--- 샘플 라이브 증거 ---")
    for p in s["sample_live"]:
        lp = p.get("latest_periodic")
        lines.append(
            f"  {p.get('stock_code')} {p.get('name')} corp={p.get('corp_code')} "
            f"| {p.get('note')}"
            + (f" | {lp['rcept_dt']} {lp['report_nm']}" if lp else ""))
    txt = "\n".join(lines)
    print(txt)
    with io.open(os.path.join(OUT_DIR, "demo_result.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")


if __name__ == "__main__":
    run(live=True, live_n=5)
