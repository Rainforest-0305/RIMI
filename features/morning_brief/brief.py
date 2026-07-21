# -*- coding: utf-8 -*-
"""소스2(read-only) 매칭 + 아침 브리핑 플레인텍스트 생성.

- 과거 영향벤치: config.IMPACT_BENCHMARK_FILE 를 먼저 시도, 없으면 repo 루트
  impact_benchmark.json 로 폴백(둘 다 read-only, 편집 절대 금지).
  구조: dict, 키=공시유형명(한국어) + '_meta'. 각 유형값에 d/w/m 구간별
  {raw_avg, car_avg, up_prob, n, conf, scope} 통계.
- 유형별로 매칭되는 벤치 통계가 있으면 "과거 평균 영향" 한 줄을 붙인다.
  없거나 매칭 안 되면 graceful 스킵(그 줄만 생략).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402

try:
    from features.morning_brief import collect  # 패키지 실행 경로
except ImportError:  # 스크립트 직접 실행 경로
    import collect  # type: ignore


def _bench_path():
    """벤치 파일 경로 결정. config 우선, 없으면 repo 루트 폴백. (Path|None)."""
    primary = Path(config.IMPACT_BENCHMARK_FILE)
    if primary.exists():
        return primary
    fallback = Path(config.BASE) / "impact_benchmark.json"
    if fallback.exists():
        return fallback
    return None


def load_benchmark():
    """벤치 dict 로드. 없으면 {} (graceful)."""
    p = _bench_path()
    if not p:
        return {}, None
    try:
        return json.loads(p.read_text(encoding="utf-8")), p.name
    except Exception:
        return {}, None


def _fmt_pct(v):
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def bench_line(bench, disc_type):
    """유형에 대한 과거 영향 한 줄. 매칭 없으면 None(스킵).

    d(익일)·m(1개월) 구간의 CAR(시장초과수익) 평균과 상승확률·신뢰·표본수 요약.
    """
    if not bench or disc_type not in bench:
        return None
    e = bench.get(disc_type) or {}
    d = e.get("d") or {}
    m = e.get("m") or {}
    if not d and not m:
        return None
    parts = []
    if d:
        parts.append(f"익일 CAR {_fmt_pct(d.get('car_avg'))}")
    if m:
        parts.append(f"1개월 CAR {_fmt_pct(m.get('car_avg'))}")
    up = d.get("up_prob")
    if up is not None:
        try:
            parts.append(f"익일 상승확률 {float(up)*100:.0f}%")
        except (TypeError, ValueError):
            pass
    n = d.get("n") or m.get("n")
    conf = d.get("conf") or m.get("conf")
    tail = []
    if n is not None:
        tail.append(f"표본 {n}건")
    if conf:
        tail.append(f"신뢰 {conf}")
    suffix = f" ({', '.join(tail)})" if tail else ""
    return f"    └ 과거 '{disc_type}' 평균: " + ", ".join(parts) + suffix


def _render_type_sections(rows, bench, matched_types, max_lines_per_type=6):
    """rows 를 유형별(분포 많은 순)로 묶어 섹션 라인 리스트 생성.

    matched_types: 이 브리핑에서 벤치 매칭된 유형 집합(중복 카운트 방지 in/out).
    반환: lines(list[str]).
    """
    dist = collect.type_distribution(rows)
    by_type = {}
    for r in rows:
        by_type.setdefault(r.get("_type"), []).append(r)
    lines = []
    for disc_type, _cnt in dist.items():
        group = by_type.get(disc_type, [])
        if not group:
            continue
        lines.append(f"■ {disc_type}  ({len(group)}건)")
        bl = bench_line(bench, disc_type)
        if bl:
            matched_types.add(disc_type)
            lines.append(bl)
        for r in group[:max_lines_per_type]:
            nm = r.get("_report_body") or r.get("report_nm", "")
            lines.append(
                f"  - [{r.get('_market', '')}] {r.get('corp_name', '')}"
                f"({r.get('stock_code', '')}) {nm}  {r.get('rcept_dt', '')}"
            )
        if len(group) > max_lines_per_type:
            lines.append(f"    ... 외 {len(group) - max_lines_per_type}건")
        lines.append("")
    return lines


def build_brief(top_rows, bench, meta, material_rows=None, now=None,
                max_lines_per_type=6):
    """공시 브리핑 플레인텍스트 생성.

    섹션1) 최신 접수 top-N (recency) — 유형별 묶음.
    섹션2) 주요사항 하이라이트 — material 이벤트(유상증자/전환사채/자사주 등)
           최신순. 마감일 분기보고서 홍수에 묻히는 실질공시를 부각(선택).

    반환: (text, stats)  stats: {bench_matches, types_in_brief}
    """
    now = now or datetime.now()
    dist = collect.type_distribution(top_rows)
    matched_types = set()          # 벤치 매칭 유형(섹션 간 중복 제거)
    types_in_brief = set()

    lines = []
    lines.append("=" * 52)
    lines.append(f"  RIMI 아침 공시 브리핑  ({now.strftime('%Y-%m-%d %H:%M')})")
    lines.append("=" * 52)
    src_files = meta.get("files", {})
    pmc = meta.get("per_market_counts", {})
    catc = meta.get("cat_counts", {})
    lines.append(
        f"데이터: 캐시 최신덤프 {src_files}  "
        f"(카테고리 " + ", ".join(f"{k} {v}" for k, v in catc.items()) + ")"
    )
    lines.append(
        "  시장분포: " + ", ".join(f"{k} {v}" for k, v in
                                 sorted(pmc.items(), key=lambda kv: kv[1],
                                        reverse=True))
    )
    lines.append(
        f"최근 접수일 {meta.get('max_rcept_dt', '')} 기준 최신 {len(top_rows)}건 요약"
    )
    lines.append("")
    lines.append("[ 유형 분포 ] " + ", ".join(f"{t} {c}" for t, c in dist.items()))
    lines.append("")

    lines.append(f"[ 섹션1 · 최신 접수 top-{len(top_rows)} ]")
    lines.append("")
    lines += _render_type_sections(top_rows, bench, matched_types,
                                   max_lines_per_type)
    for t in dist:
        types_in_brief.add(t)

    if material_rows:
        lines.append("-" * 52)
        lines.append(f"[ 섹션2 · 주요사항 하이라이트 (실질 이벤트 최신 {len(material_rows)}건) ]")
        lines.append("")
        lines += _render_type_sections(material_rows, bench, matched_types,
                                       max_lines_per_type)
        for t in collect.type_distribution(material_rows):
            types_in_brief.add(t)

    lines.append("-" * 52)
    lines.append(
        "주의: CAR=시장동일가중 대비 초과수익(event study 추정치), "
        "투자권유 아님. 원문은 DART 확인."
    )
    text = "\n".join(lines)
    stats = {"bench_matches": len(matched_types),
             "types_in_brief": len(types_in_brief)}
    return text, stats
