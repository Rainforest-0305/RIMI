# -*- coding: utf-8 -*-
"""RIMI 아침 브리핑 데모(단독 실행).

두 실행법 모두 지원:
  python features/morning_brief/demo.py
  python -m features.morning_brief.demo

동작:
  1) 캐시 최신덤프 로드(0콜) -> 최근 top-N 추출 -> 유형분류
  2) 과거 영향벤치(read-only) 매칭 -> 플레인텍스트 브리핑 생성
  3) 브리핑을 파일로도 저장(콘솔 mojibake 무관, 진짜 utf-8 검증용)
  4) (선택) --live 인자 있고 예산 남으면 dart_poll.fetch_markets(days=1) 1회 실증
  5) 실측 요약 출력(입력수·유형분포·소요초·라이브콜수·벤치매칭수·키로드여부)
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# --- repo 루트 + 모듈 디렉터리를 sys.path 에 삽입(두 실행법 모두 절대/형제 import 보장) ---
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402

try:
    from features.morning_brief import collect, brief, live_today
except ImportError:
    import collect  # type: ignore
    import brief    # type: ignore
    import live_today  # type: ignore


TOP_N = 20
MATERIAL_N = 30


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    do_live = "--live" in argv

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    t0 = time.time()

    # 1) 캐시 로드 + top-N + 분류 + 주요사항(material) 추출
    rows, meta = collect.load_latest()
    top = collect.top_recent(rows, TOP_N)
    material = collect.material_recent(rows, MATERIAL_N)

    # 2) 벤치 로드 + 브리핑 생성
    bench, bench_file = brief.load_benchmark()
    text, bstats = brief.build_brief(top, bench, meta, material_rows=material)

    # 3) 파일로도 저장(진짜 utf-8 검증)
    out_dir = _HERE / "out"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "morning_brief_latest.txt"
    out_path.write_text(text, encoding="utf-8")

    # 4) (선택) 라이브 실증
    live = None
    if do_live:
        live = live_today.run_live_today()

    elapsed = time.time() - t0

    # 5) 실측 요약
    top_dist = collect.type_distribution(top)
    key_loaded = bool(config.DART_API_KEY)

    print(text)
    print()
    print("#" * 52)
    print("[ 스모크 실측 ]")
    print(f"- 입력 공시수(최신덤프 총): {meta['total']}")
    print(f"- 카테고리분포: {meta['cat_counts']}")
    print(f"- 시장분포(파일접두사 확정): {meta['per_market_counts']}")
    print(f"- 시장x카테고리: {meta['market_cat_counts']}")
    if meta.get('missing_sources'):
        print(f"- 누락소스: {meta['missing_sources']}")
    print(f"- 소스파일: {meta['files']}")
    print(f"- 최근 접수일(max rcept_dt): {meta['max_rcept_dt']}")
    print(f"- top-N 추출: {len(top)}건  유형분포: {top_dist}")
    mat_dist = collect.type_distribution(material)
    print(f"- 주요사항 추출: {len(material)}건  유형분포: {mat_dist}")
    print(f"- 벤치파일: {bench_file}  벤치 매칭수: {bstats['bench_matches']} "
          f"(브리핑 유형수 {bstats['types_in_brief']})")
    print(f"- DART 키 로드: {key_loaded}")
    if live is None:
        print("- 라이브 DART: 미실행(--live 인자 없음)  라이브콜수: 0")
    else:
        print(f"- 라이브 DART 실행: ran={live['ran']} "
              f"skip={live['skipped_reason']} "
              f"라이브콜수(실측)={live['calls']} "
              f"오늘공시수={len(live['rows'])} errors={live['errors']}")
    print(f"- 생성 소요초: {elapsed:.3f}s")
    print(f"- 브리핑 저장경로: {out_path}")
    print("#" * 52)

    return {
        "text": text,
        "out_path": str(out_path),
        "meta": meta,
        "top_dist": top_dist,
        "bench_matches": bstats["bench_matches"],
        "elapsed": elapsed,
        "live": live,
        "key_loaded": key_loaded,
    }


if __name__ == "__main__":
    main()
