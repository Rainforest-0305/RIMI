# -*- coding: utf-8 -*-
"""소스1(캐시, DART 0콜): bench_cache/dart/ 전 시장·전 유형 덤프 스캔.

정기보고서(사업/반기/분기) 이력을 추출하고 종목별로 그룹핑한다.
- 접두사별 시장/유형 (실측 확정):
    A_   = 코스피 정기공시(정기보고서 다수)   I_   = 코스피 기타(정기보고서 소수)
    B_   = 코스피 주요사항(정기보고서 0)
    K_A_ = 코스닥 정기공시(정기보고서 다수)   K_I_ = 코스닥 기타(정기보고서 소수)
    K_B_ = 코스닥 주요사항(정기보고서 0)
  → 6개 접두사 전부 스캔한다. report_type 필터가 비정기보고서를 걸러내므로
    전부 넣어도 안전. 시장 라벨은 K_ 접두사 유무로 코스피/코스닥 구분.
    (President 지시: 스코프 코스피 축소 금지 — 코스피·코스닥 둘 다 커버.)
- report_nm 부분일치로 정기보고서 판정. '기재정정' 표기는 별도 플래그.
- report_nm 말미 괄호 (YYYY.MM) 는 결산(회계) 기준기말 → fy/fm 로 파싱.

DART 라이브 콜을 절대 하지 않는다(순수 로컬 파일 IO). 모든 IO encoding='utf-8'.
"""
import os
import re
import io
import json
import glob

_HERE = os.path.dirname(os.path.abspath(__file__))
# repo_root = features/earnings_calendar -> features -> <repo root>
_ROOT = os.path.dirname(os.path.dirname(_HERE))

CACHE_DIR = os.path.join(_ROOT, "bench_cache", "dart")

# 정기보고서 3종. 판정 우선순위(부분일치). 사업>반기>분기 순으로 검사.
REPORT_TYPES = ("사업보고서", "반기보고서", "분기보고서")

# 결산기준 (YYYY.MM) 파싱. 구분자 . - / 허용.
_PAREN = re.compile(r"\((\d{4})[.\-/](\d{1,2})\)")


def report_type(report_nm):
    """report_nm 에 포함된 정기보고서 종류를 반환. 없으면 None."""
    nm = report_nm or ""
    for t in REPORT_TYPES:
        if t in nm:
            return t
    return None


def is_amend(report_nm):
    """'기재정정' 표기 여부(정정 공시 플래그)."""
    return "기재정정" in (report_nm or "")


def parse_period(report_nm):
    """report_nm 말미의 (YYYY.MM) → (fy:int, fm:int). 실패 시 (None, None)."""
    m = _PAREN.search(report_nm or "")
    if not m:
        return None, None
    fy, fm = int(m.group(1)), int(m.group(2))
    if 1 <= fm <= 12 and 1990 <= fy <= 2100:
        return fy, fm
    return None, None


def _market_from_filename(path):
    """파일명 접두사 → 시장 라벨. K_ 접두사=코스닥, 그 외=코스피.

    K_A_/K_B_/K_I_ → KOSDAQ, A_/B_/I_ → KOSPI.
    """
    base = os.path.basename(path)
    if base.startswith("K_"):
        return "KOSDAQ"
    if base[:2] in ("A_", "B_", "I_"):
        return "KOSPI"
    return "UNKNOWN"


def load_all(cache_dir=None):
    """A_*.json + B_*.json 전체 스캔 → 정기보고서 레코드 list.

    각 레코드 dict:
      rcept_no, corp_code, corp_name, stock_code, report_nm, rcept_dt(YYYYMMDD),
      report_type, market, fy, fm, amend(bool)
    """
    cache_dir = cache_dir or CACHE_DIR
    # 6개 접두사 전부(코스피 A_/B_/I_ + 코스닥 K_A_/K_B_/K_I_). glob 은 파일명
    # 선두부터 매칭이라 A_* 가 K_A_* 를, I_* 가 K_I_* 를 잡지 않는다(접두사 배타).
    files = []
    for pat in ("A_*.json", "B_*.json", "I_*.json",
                "K_A_*.json", "K_B_*.json", "K_I_*.json"):
        files += sorted(glob.glob(os.path.join(cache_dir, pat)))
    records = []
    scanned_rows = 0
    for f in files:
        try:
            with io.open(f, encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            continue
        market = _market_from_filename(f)
        if not isinstance(rows, list):
            continue
        for r in rows:
            scanned_rows += 1
            nm = r.get("report_nm", "")
            t = report_type(nm)
            if not t:
                continue
            fy, fm = parse_period(nm)
            records.append({
                "rcept_no": r.get("rcept_no", ""),
                "corp_code": r.get("corp_code", ""),
                "corp_name": r.get("corp_name", ""),
                "stock_code": r.get("stock_code", ""),
                "report_nm": nm,
                "rcept_dt": r.get("rcept_dt", ""),
                "report_type": t,
                "market": market,
                "fy": fy,
                "fm": fm,
                "amend": is_amend(nm),
            })
    return records, {"files": len(files), "scanned_rows": scanned_rows,
                     "periodic_rows": len(records)}


def group_by_corp(records):
    """corp_code 기준 그룹핑. 각 그룹은 rcept_dt 오름차순 정렬.

    corp_code 가 비면 stock_code 로 폴백 키. 종목 메타(name/stock/market)는
    최신 레코드 값을 대표로 담아 반환한다.
    """
    groups = {}
    for r in records:
        key = r.get("corp_code") or ("S:" + (r.get("stock_code") or ""))
        if not key or key == "S:":
            continue
        groups.setdefault(key, []).append(r)
    for key, rows in groups.items():
        rows.sort(key=lambda x: (x.get("rcept_dt") or "", x.get("rcept_no") or ""))
    return groups


def dataset_as_of(records):
    """데이터 스냅샷 기준일 = 전체 rcept_dt 최댓값(YYYYMMDD). 예측의 '현재' 기준."""
    m = ""
    for r in records:
        d = r.get("rcept_dt") or ""
        if d > m:
            m = d
    return m


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    recs, stats = load_all()
    g = group_by_corp(recs)
    print("stats:", stats)
    print("corps:", len(g))
    print("as_of:", dataset_as_of(recs))
