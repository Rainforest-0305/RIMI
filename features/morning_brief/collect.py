# -*- coding: utf-8 -*-
"""소스1(캐시, 0콜): bench_cache/dart/A_*.json + B_*.json 로드 + 최근 top-N + 유형분류.

- A_ = KOSPI(코스피), B_ = KOSDAQ(코스닥). President: 스코프 축소 금지 → 둘 다 사용.
- '최신 분기 덤프'는 파일명 날짜(끝 YYYYMMDD)로 가장 최근 것을 A/B 각각 선택.
- 캐시 행 키: rcept_no, corp_code, corp_name, stock_code, report_nm, rcept_dt(YYYYMMDD).
  (행 자체엔 corp_cls 없음 → 파일 접두사로 market 라벨을 부착한다.)
- 모든 IO encoding='utf-8' 명시(콘솔 mojibake와 무관하게 파일은 진짜 utf-8).
"""
import json
import os
import re
import sys
from pathlib import Path

# --- repo 루트를 sys.path 에 넣어 config 절대 import 보장(격리 안전) ---
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent  # repo 루트
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402  (import 허용, 편집 금지)

BENCH_CACHE_DIR = Path(config.BASE) / "bench_cache" / "dart"

# 캐시 파일 접두사 실측 규명(중요):
#   A_/B_/I_   = **코스피(corp_cls=Y)**,  A=정기공시 B=주요사항 I=거래소공시
#   K_A_/K_B_/K_I_ = **코스닥(corp_cls=K)**, 동일 카테고리
# (태스크 원문의 "B_=코스닥"은 오기 — 실제 코스닥은 K_* 파일. 검증: SK하이닉스·
#  대한항공 등 코스피가 B_ 에 있고, 에코프로비엠 등 코스닥은 K_* 에만 존재.)
# President '스코프 축소 금지' 진의 = 코스피+코스닥 모두 → K_* 반드시 포함.
# 시장은 파일 접두사로 확정(캐시행에 corp_cls 없음).
#
# (prefix, 시장, 카테고리)
_SOURCES = [
    ("A",   "코스피", "정기공시"),
    ("B",   "코스피", "주요사항"),
    ("I",   "코스피", "거래소공시"),
    ("K_A", "코스닥", "정기공시"),
    ("K_B", "코스닥", "주요사항"),
    ("K_I", "코스닥", "거래소공시"),
]

# report_nm -> 벤치마크 유형키 매핑 규칙(순서 중요: 위에서부터 먼저 맞는 것).
# 값은 data 벤치(impact_benchmark.json)의 실제 키와 정렬시켜 매칭수를 높인다.
# 벤치 키: 감사보고서 공급계약 기타공시 무상증자 배당 소송 실적 유상증자 임상
#          자사주 전환사채 정정공시 주식소각 지분변동 최대주주변경 합병분할
_RULES = [
    (("유상증자",), "유상증자"),
    (("무상증자",), "무상증자"),
    (("전환사채", "신주인수권부사채", "교환사채"), "전환사채"),
    (("자기주식", "자사주"), "자사주"),
    (("감자", "소각"), "주식소각"),
    (("합병", "분할", "주식교환", "주식이전", "교환ㆍ이전", "교환·이전"), "합병분할"),
    (("배당",), "배당"),
    (("소송", "가처분", "회생절차", "파산"), "소송"),
    (("공급계약", "단일판매", "수주", "공급"), "공급계약"),
    (("임상", "품목허가"), "임상"),
    (("감사보고서",), "감사보고서"),
    (("최대주주",), "최대주주변경"),
    (("지분", "대량보유", "주요주주"), "지분변동"),
    (("분기보고서", "반기보고서", "사업보고서", "영업실적", "결산", "실적"), "실적"),
]

# 분류 표시용(브리핑에 사람이 읽는 유형명) — 벤치키와 동일하게 씀.
DEFAULT_TYPE = "기타공시"
CORRECTION_TYPE = "정정공시"


def _strip_brackets_prefix(name: str) -> str:
    """[기재정정][첨부정정][정정명령부과] 같은 대괄호 접두사만 제거한 본문 반환.

    분류는 '정정' 껍데기가 아니라 안쪽 실제 이벤트로 하기 위함
    (예: [기재정정]주요사항보고서(유상증자결정) -> 유상증자).
    """
    s = name or ""
    s = re.sub(r"^(\[[^\]]*\])+", "", s)
    s = re.sub(r"\s+", " ", s)  # DART report_nm 내부 과다공백 정규화
    return s.strip()


def classify(report_nm: str) -> str:
    """report_nm -> 공시유형(벤치키 정렬). 매칭 없으면 정정공시/기타공시."""
    raw = report_nm or ""
    body = _strip_brackets_prefix(raw)
    hay = body  # 괄호 안 이벤트명까지 포함해 검색
    for keys, label in _RULES:
        for k in keys:
            if k in hay:
                return label
    # 이벤트 규칙 미매칭: 정정 껍데기면 정정공시, 아니면 기타공시
    if "정정" in raw:
        return CORRECTION_TYPE
    return DEFAULT_TYPE


def _latest_dump(prefix: str):
    """접두사별 최신 덤프 파일 경로. 파일명 끝 YYYYMMDD 로 최신 선택.

    파일명 형식: {prefix}_{시작}_{끝}.json (예: A_20260401_20260515.json,
    K_A_20260401_20260515.json). 접두사 정확 일치만(A 가 K_A 를 오매칭 안 함).
    없으면 None.
    """
    best = None
    best_key = ""
    if not BENCH_CACHE_DIR.exists():
        return None
    pat = re.compile(rf"^{re.escape(prefix)}_(\d{{8}})_(\d{{8}})$")
    for p in BENCH_CACHE_DIR.glob(f"{prefix}_*.json"):
        m = pat.match(p.stem)
        if not m:
            continue
        end = m.group(2)
        if end > best_key:
            best_key = end
            best = p
    return best


def load_latest(sources=None):
    """최신 분기 덤프를 코스피(A/B/I)+코스닥(K_A/K_B/K_I) 전부 로드·병합.

    sources: [(prefix, market, category)] 리스트. 기본 _SOURCES(6종, 양시장).
    시장/카테고리는 파일 접두사로 확정 부착(_market, _category).

    반환: (rows, meta)
      rows: list[dict] (원본행 + '_market' + '_category' + '_type' + '_report_body')
      meta: dict {files, cat_counts, per_market_counts, market_cat_counts,
                  total, max_rcept_dt, missing_sources}
    """
    srcs = sources if sources is not None else _SOURCES
    rows = []
    files = {}
    cat_counts = {}
    per_market = {}
    market_cat = {}
    missing = []
    for prefix, market, cat in srcs:
        f = _latest_dump(prefix)
        if not f:
            files[prefix] = None
            missing.append(prefix)
            continue
        files[prefix] = f.name
        data = json.loads(f.read_text(encoding="utf-8"))
        for r in data:
            row = dict(r)
            row["_market"] = market
            row["_category"] = cat
            row["_type"] = classify(row.get("report_nm", ""))
            row["_report_body"] = _strip_brackets_prefix(row.get("report_nm", ""))
            rows.append(row)
        n = len(data)
        cat_counts[cat] = cat_counts.get(cat, 0) + n
        per_market[market] = per_market.get(market, 0) + n
        market_cat[f"{market}/{cat}"] = n
    max_dt = max((r.get("rcept_dt", "") for r in rows), default="")
    meta = {
        "files": files,
        "cat_counts": cat_counts,
        "per_market_counts": per_market,
        "market_cat_counts": market_cat,
        "total": len(rows),
        "max_rcept_dt": max_dt,
        "missing_sources": missing,
    }
    return rows, meta


def top_recent(rows, n=20):
    """최근 rcept_dt(그다음 rcept_no) 기준 상위 n건. 최신순 정렬 반환."""
    ordered = sorted(
        rows,
        key=lambda x: (x.get("rcept_dt", ""), x.get("rcept_no", "")),
        reverse=True,
    )
    return ordered[:n]


# 아침 브리핑에서 '주요사항 하이라이트'로 부각할 실질 이벤트가 아닌 유형.
# (분기/사업보고서=실적 홍수, 감사보고서, 단순 정정, 기타는 제외)
NON_MATERIAL = {"실적", "기타공시", "정정공시", "감사보고서"}


def material_recent(rows, n=30, exclude=None):
    """실질 이벤트(유상증자·전환사채·자사주·합병분할·주식소각·소송 등)만
    최신순 top-n. 마감일 분기보고서 홍수에 묻히는 material 공시를 부각한다."""
    ex = exclude if exclude is not None else NON_MATERIAL
    mat = [r for r in rows if r.get("_type") not in ex]
    mat.sort(
        key=lambda x: (x.get("rcept_dt", ""), x.get("rcept_no", "")),
        reverse=True,
    )
    return mat[:n]


def type_distribution(rows):
    """유형분포 dict {유형: 건수} (건수 내림차순 정렬된 dict)."""
    dist = {}
    for r in rows:
        t = r.get("_type") or classify(r.get("report_nm", ""))
        dist[t] = dist.get(t, 0) + 1
    return dict(sorted(dist.items(), key=lambda kv: kv[1], reverse=True))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    rows, meta = load_latest()
    print("files:", meta["files"], "total:", meta["total"], "max_dt:", meta["max_rcept_dt"])
    top = top_recent(rows, 20)
    print("top-N dist:", type_distribution(top))
