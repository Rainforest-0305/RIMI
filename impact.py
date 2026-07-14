# -*- coding: utf-8 -*-
"""과거 영향 분석(impact benchmark) 리더 — 스키마 적응형.

strat-data(`build_impact_benchmark.py`)가 생성하는 `data/impact_benchmark.json`
을 읽어 공시 유형별 과거 통계(1일/1주/1개월 시장보정 초과등락·상승확률·표본N·
신뢰도등급)를 공시 카드에 매핑한다.

**두 가지 스키마를 모두 읽는다(중요 — 생산자와의 결합 방어):**

  (A) strat-data 실산출 스키마(top-level 유형):
      { "_meta": {...},
        "<유형>": { "d": {car_avg,raw_avg,car_med,up_prob,n,conf,scope},
                    "w": {...}, "m": {...} }, ... }
      - 창 키: d/w/m,  초과등락 대표값: car_med(시장보정 CAR 중앙값),
        conf: "높음/보통/참고", scope: 실제 집계에 쓴 라벨(폴백 시 상위버킷/전체).
        strat-data 가 유형별 폴백(self→bucket→전체)을 내부에서 이미 수행한다.

  (B) 시드 placeholder 스키마(초기 개발용):
      { "_meta", "buckets": {대분류:[유형...]},
        "types": { "<유형>": { "windows": {d1/w1/m1: {excess,up_prob,n}},
                               "confidence": "A|B|C" } } }

**설계 원칙:** 통계를 생성하지 않고 읽기만 한다. 파일이 없거나 유형이 아직
집계되지 않았으면 에러 대신 status="pending"("집계 중")을 반환한다.
출력은 프론트가 쓰는 단일 형태(windows d1/w1/m1, grade=A/B/C, confidence 표시문구)
로 정규화한다.
"""
import json

import config

WINDOW_LABELS = {"d1": "1일", "w1": "1주", "m1": "1개월"}
# 프론트 창키(d1/w1/m1) <- 원본 후보키(시드 d1.., strat d/w/m)
_WIN_CANDS = {"d1": ("d1", "d"), "w1": ("w1", "w"), "m1": ("m1", "m")}

# 신뢰도 표기 -> CSS 등급(A/B/C). strat(높음/보통/참고)·시드(A/B/C) 모두 매핑.
_GRADE = {"A": "A", "B": "B", "C": "C",
          "높음": "A", "보통": "B", "참고": "C"}


def grade_css(conf) -> str:
    return _GRADE.get(str(conf).strip(), "na")


def grade_from_n(n) -> str:
    if not isinstance(n, (int, float)):
        return "참고"
    if n >= 80:
        return "높음"
    if n >= 30:
        return "보통"
    return "참고"


_CACHE = {"key": None, "data": None}


def _candidate_paths():
    """벤치마크 파일 후보 경로. 생산자(strat-data)와 소비자가 경로를 다르게
    잡아도(루트 vs data/) 깨지지 않도록 둘 다 탐색하고, 가장 최근 수정본을 쓴다.
    - data/impact_benchmark.json : 기본(시드 placeholder 위치)
    - <root>/impact_benchmark.json: strat-data build_impact_benchmark.py 산출 위치
    """
    return [config.IMPACT_BENCHMARK_FILE, config.BASE / "impact_benchmark.json"]


def has_stats() -> bool:
    """벤치마크에 실제 집계된 유형이 하나라도 있으면 True.
    strat 실스키마(top-level 유형)·시드 스키마(types 래퍼) 모두 정확 판정.
    app.py benchmark_ready 오보고(버그 B) 방지용 단일 진실원."""
    types, _, _ = _types_map(load_benchmark())
    return bool(types)


def benchmark_source() -> str:
    """벤치마크 출처 표기. 시드(_meta.source)·strat(_meta.method/generated_at) 대응."""
    meta = (load_benchmark().get("_meta") or {})
    return (meta.get("source") or meta.get("method")
            or meta.get("generated_at") or "")


def load_benchmark() -> dict:
    """impact_benchmark.json 로드(경로·mtime 캐시). 없으면 {}."""
    try:
        cands = [p for p in _candidate_paths() if p.exists()]
        if not cands:
            _CACHE["key"], _CACHE["data"] = None, {}
            return {}
        f = max(cands, key=lambda p: p.stat().st_mtime)   # 최신 수정본 우선
        key = (str(f), f.stat().st_mtime)
        if _CACHE["data"] is not None and _CACHE["key"] == key:
            return _CACHE["data"]
        data = json.loads(f.read_text(encoding="utf-8"))
        _CACHE["key"], _CACHE["data"] = key, data
        return data
    except Exception:
        return {}


def _types_map(bench: dict):
    """스키마 판별 -> {유형: entry} 통일 맵 + buckets(있으면).
    반환: (types_dict, buckets_dict|None, schema_str)."""
    if not bench:
        return {}, None, "none"
    if isinstance(bench.get("types"), dict):          # (B) 시드 스키마
        return bench["types"], (bench.get("buckets") or None), "seed"
    # (A) strat-data 스키마: _meta 제외한 top-level 유형
    types = {k: v for k, v in bench.items()
             if k != "_meta" and isinstance(v, dict)}
    return types, None, "strat"


def _window(inner: dict, outk: str) -> dict:
    wd = {}
    for c in _WIN_CANDS[outk]:
        if isinstance(inner.get(c), dict):
            wd = inner[c]
            break
    # 3-way 대표값 모두 노출: 평균값(raw_avg)·중앙값(raw_med)·보정값(car_avg).
    # 시드 스키마 폴백: raw_avg 없으면 excess.
    raw_avg = wd.get("raw_avg")
    if raw_avg is None:
        raw_avg = wd.get("excess")
    raw_med = wd.get("raw_med")     # 원자료 등락 중앙값(없을 수 있음 → 프론트가 폴백)
    car_avg = wd.get("car_avg")     # 시장보정 초과등락 평균
    # 상승확률: 원자료 기준(raw_up_prob)과 보정 기준(up_prob) 분리.
    raw_up = wd.get("raw_up_prob")
    if raw_up is None:
        raw_up = wd.get("up_prob")
    car_up = wd.get("up_prob")
    if car_up is None:
        car_up = raw_up
    n = wd.get("n")

    def _down(u):
        return round(1 - u, 4) if isinstance(u, (int, float)) else None

    return {
        "label": WINDOW_LABELS[outk],
        # 대표값 3종 + 각각의 상승확률(평균값/중앙값=원자료 raw_up, 보정값=car_up).
        "raw_avg": raw_avg,
        "raw_med": raw_med,
        "car_avg": car_avg,
        "raw_up_prob": raw_up,
        "car_up_prob": car_up,
        # 하위호환: 기존 프론트가 참조하던 단일 필드(=평균값 기준).
        "up_prob": raw_up,
        "down_prob": _down(raw_up),
        "car_down_prob": _down(car_up),
        "n": n,
        "conf": wd.get("conf"),      # strat: 창별 신뢰도
        "scope": wd.get("scope"),    # strat: 창별 집계 라벨
    }


def _shape(entry: dict, tag: str) -> dict:
    """원본 entry -> 표시용 정규화 블록."""
    inner = entry.get("windows", entry)  # 시드는 windows 래핑, strat은 평면
    windows = {k: _window(inner, k) for k in ("d1", "w1", "m1")}

    # 신뢰도: entry레벨(시드 confidence) 우선, 없으면 창(d) conf, 없으면 N기반
    conf = entry.get("confidence") or entry.get("conf") \
        or windows["d1"]["conf"] or grade_from_n(windows["d1"]["n"])

    # scope/source: strat 창의 scope(폴백 라벨) 활용. 시드는 tag 자신.
    scope = windows["d1"]["scope"] or windows["m1"]["scope"]
    if scope and scope != tag:
        source = "bucket" if scope != "전체" else "market"
        matched = scope
    else:
        source = "type"
        matched = tag

    return {
        "status": "ok",
        "matched_tag": matched,     # 실제 통계 산출에 쓰인 라벨(유형/버킷/전체)
        "query_tag": tag,           # 공시에서 매칭된 유형
        "source": source,           # type | bucket | market
        "confidence": conf,         # 표시문구(높음/보통/참고 또는 A/B/C)
        "grade": grade_css(conf),   # CSS 등급 A/B/C/na
        "windows": {k: {kk: v[kk] for kk in (
            "label", "raw_avg", "raw_med", "car_avg",
            "raw_up_prob", "car_up_prob", "up_prob", "down_prob",
            "car_down_prob", "n")}
                    for k, v in windows.items()},
    }


def _bucket_of(tag, buckets):
    if not buckets:
        return None
    for bname, tags in buckets.items():
        if tag in tags:
            return bname
    return None


def impact_for_tags(tags) -> dict:
    """공시 태그 목록 -> 과거 영향 블록.
    유형 정확매칭 > (시드 스키마면) 버킷 폴백 > pending. 항상 dict(에러 없음)."""
    bench = load_benchmark()
    types, buckets, schema = _types_map(bench)

    if not types:
        return {"status": "pending", "reason": "benchmark_missing",
                "message": "과거 영향 데이터 집계 중"}

    tags = tags or []
    # 1) 유형 정확 매칭(우선순위 순서)
    for t in tags:
        if t in types:
            return _shape(types[t], t)
    # 2) 시드 스키마에서만 버킷 폴백(strat 은 내부 폴백이 이미 반영됨)
    if schema == "seed" and buckets:
        for t in tags:
            b = _bucket_of(t, buckets)
            if b and b in types:
                out = _shape(types[b], b)
                out["query_tag"] = t
                out["source"] = "bucket"
                out["matched_tag"] = b
                return out
    return {"status": "pending", "reason": "type_not_aggregated",
            "message": "이 유형은 아직 집계 중"}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("schema:", _types_map(load_benchmark())[2])
    for tags in (["자사주"], ["유상증자"], ["기타공시"], ["없는유형"]):
        r = impact_for_tags(tags)
        if r["status"] == "ok":
            w = r["windows"]["w1"]
            print(tags, "-> ok", r["matched_tag"], r["source"],
                  "conf", r["confidence"], "/", r["grade"],
                  "| w1 raw_avg", w["raw_avg"], "up", w["up_prob"], "n", w["n"])
        else:
            print(tags, "-> pending", r.get("message"))
