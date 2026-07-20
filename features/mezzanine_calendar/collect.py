# -*- coding: utf-8 -*-
"""메자닌(CB/BW/EB) 공시 파싱본 -> 정규화 레코드 추출.

소스(라이브 DART 0콜): bench_cache/amounts/
  - cvbdIsDecsn_*.json : 전환사채(CB)
  - bdwtIsDecsn_*.json : 신주인수권부사채(BW)
  - exbdIsDecsn_*.json : 교환사채(EB)

각 파일은 dict: 키=rcept_no, 값=공시 dict.

필드 매핑(실제 JSON key 확인 완료):
  공통 : corp_code, corp_name, rcept_no, bd_knd(사채종류),
         bd_fta(발행총액, 콤마문자열), bd_mtd(만기, "YYYY년 MM월 DD일")
  CB   : cv_prc(전환가), cvisstk_cnt(전환가능주식수),
         cvisstk_tisstk_vs(발행주식대비%), cvrqpd_bgd(청구개시), cvrqpd_edd(청구종료)
  BW   : ex_prc(행사가), nstk_isstk_cnt(행사시 발행주식수),
         nstk_isstk_tisstk_vs(발행주식대비%), expd_bgd(행사개시), expd_edd(행사종료)
  EB   : ex_prc(교환가), extg_stkcnt(교환대상 주식수),
         extg_tisstk_vs(발행주식대비%), exrqpd_bgd(교환청구개시), exrqpd_edd(교환청구종료)
"""
import glob
import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Optional

# ---------------------------------------------------------------------------
# 경로 유틸 (features/ 밖 파일은 읽기만; DART 콜 0)
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))


def default_amounts_dir() -> str:
    return os.path.join(_REPO_ROOT, "bench_cache", "amounts")


def default_corp_map_file() -> str:
    return os.path.join(_REPO_ROOT, "data", "corp_map.json")


# ---------------------------------------------------------------------------
# 타입별 필드 매핑 테이블
# ---------------------------------------------------------------------------
# (파일 glob prefix, sec_type, price_key, shares_key, vs_key, start_key, end_key)
TYPE_SPECS = {
    "CB": {
        "glob": "cvbdIsDecsn_*.json",
        "price": "cv_prc",
        "shares": "cvisstk_cnt",
        "vs": "cvisstk_tisstk_vs",
        "start": "cvrqpd_bgd",
        "end": "cvrqpd_edd",
    },
    "BW": {
        "glob": "bdwtIsDecsn_*.json",
        "price": "ex_prc",
        "shares": "nstk_isstk_cnt",
        "vs": "nstk_isstk_tisstk_vs",
        "start": "expd_bgd",
        "end": "expd_edd",
    },
    "EB": {
        "glob": "exbdIsDecsn_*.json",
        "price": "ex_prc",
        "shares": "extg_stkcnt",
        "vs": "extg_tisstk_vs",
        "start": "exrqpd_bgd",
        "end": "exrqpd_edd",
    },
}

# ---------------------------------------------------------------------------
# 파싱 유틸
# ---------------------------------------------------------------------------
_KDATE_RE = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})")
_NUM_RE = re.compile(r"-?\d[\d,]*")


def parse_kdate(s) -> Optional[date]:
    """한글 날짜 "YYYY년 MM월 DD일" -> date. 실패 시 None."""
    if not s or not isinstance(s, str):
        return None
    m = _KDATE_RE.search(s)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(y, mo, d)
    except (ValueError, TypeError):
        return None


def parse_amount(s) -> Optional[int]:
    """콤마 포함 금액/주식수 문자열 -> int. 실패 시 None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    if not isinstance(s, str):
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_float(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 정규화 레코드
# ---------------------------------------------------------------------------
@dataclass
class MezzRecord:
    sec_type: str          # CB / BW / EB
    corp_code: str         # DART 8자리
    corp_name: str
    rcept_no: str
    stock_code: Optional[str]      # 6자리(있으면), 역매핑 실패 시 None
    bd_knd: str            # 사채 종류(원문)
    issue_amount: Optional[int]    # 발행총액(원)
    conv_price: Optional[int]      # 전환/행사/교환가
    shares: Optional[int]          # 전환/행사/교환 가능 주식수
    vs_pct: Optional[float]        # 발행주식대비 %
    start_date: Optional[date]     # 청구/행사 개시일
    end_date: Optional[date]       # 청구/행사 종료일
    maturity_date: Optional[date]  # 만기
    src_file: str = ""

    def to_dict(self):
        d = asdict(self)
        for k in ("start_date", "end_date", "maturity_date"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class CollectStats:
    files_total: int = 0
    rows_total: int = 0
    by_type: dict = field(default_factory=lambda: {"CB": 0, "BW": 0, "EB": 0})
    fail_start: int = 0
    fail_end: int = 0
    fail_maturity: int = 0
    fail_amount: int = 0
    fail_price: int = 0
    fail_shares: int = 0
    stock_code_hit: int = 0
    stock_code_miss: int = 0
    dart_live_calls: int = 0  # 이 모듈은 항상 0

    def to_dict(self):
        return asdict(self)


def _load_corp_reverse_map(corp_map_file: str) -> dict:
    """corp_map.json (stock_code -> dart_corp_code) 를 역매핑.

    반환: dart_corp_code(8자리) -> stock_code(6자리). DART 콜 0 (로컬 캐시).
    """
    rev = {}
    try:
        with open(corp_map_file, encoding="utf-8") as f:
            fwd = json.load(f)
        for stock_code, dart_code in fwd.items():
            # zfill 정규화
            rev[str(dart_code).zfill(8)] = str(stock_code).zfill(6)
    except (OSError, ValueError):
        pass
    return rev


def _snapshot_path() -> str:
    # 배포(Render)용 사전 파싱 스냅샷. bench_cache(23MB, gitignore) 부재 시 이걸 읽는다.
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "mezz_records.json")


def _rec_to_dict(r: "MezzRecord") -> dict:
    def d(x):
        return x.isoformat() if hasattr(x, "isoformat") else x
    return {"sec_type": r.sec_type, "corp_code": r.corp_code, "corp_name": r.corp_name,
            "rcept_no": r.rcept_no, "stock_code": r.stock_code, "bd_knd": r.bd_knd,
            "issue_amount": r.issue_amount, "conv_price": r.conv_price, "shares": r.shares,
            "vs_pct": r.vs_pct, "start_date": d(r.start_date), "end_date": d(r.end_date),
            "maturity_date": d(r.maturity_date), "src_file": r.src_file}


def _dict_to_rec(o: dict) -> "MezzRecord":
    from datetime import date as _date
    def pd(s):
        if not s:
            return None
        try:
            y, m, dd = str(s).split("-"); return _date(int(y), int(m), int(dd))
        except Exception:
            return None
    return MezzRecord(
        sec_type=o.get("sec_type", ""), corp_code=o.get("corp_code", ""),
        corp_name=o.get("corp_name", ""), rcept_no=o.get("rcept_no", ""),
        stock_code=o.get("stock_code"), bd_knd=o.get("bd_knd", ""),
        issue_amount=o.get("issue_amount"), conv_price=o.get("conv_price"),
        shares=o.get("shares"), vs_pct=o.get("vs_pct"),
        start_date=pd(o.get("start_date")), end_date=pd(o.get("end_date")),
        maturity_date=pd(o.get("maturity_date")), src_file=o.get("src_file", ""))


def save_snapshot(records, path: Optional[str] = None) -> str:
    """collect_all 결과를 배포용 스냅샷 JSON으로 저장(로컬 빌드 시 1회)."""
    path = path or _snapshot_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump([_rec_to_dict(r) for r in records], f, ensure_ascii=False)
    return path


def _load_snapshot(path: Optional[str] = None):
    path = path or _snapshot_path()
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    recs = [_dict_to_rec(o) for o in rows]
    stats = CollectStats()
    stats.rows_total = len(recs)
    for r in recs:
        stats.by_type[r.sec_type] += 1
        if r.stock_code:
            stats.stock_code_hit += 1
        else:
            stats.stock_code_miss += 1
    return recs, stats


def collect_all(amounts_dir: Optional[str] = None,
                corp_map_file: Optional[str] = None):
    """세 타입 전체 순회 -> (records: list[MezzRecord], stats: CollectStats).

    bench_cache/amounts 가 없으면(배포 환경) 사전 빌드 스냅샷(mezz_records.json)을 읽는다.
    """
    amounts_dir = amounts_dir or default_amounts_dir()
    corp_map_file = corp_map_file or default_corp_map_file()

    # 소스 파일 부재(Render 등) → 스냅샷 폴백
    has_source = any(
        glob.glob(os.path.join(amounts_dir, spec["glob"])) for spec in TYPE_SPECS.values()
    )
    if not has_source and os.path.exists(_snapshot_path()):
        return _load_snapshot()

    rev_map = _load_corp_reverse_map(corp_map_file)

    records = []
    stats = CollectStats()

    for sec_type, spec in TYPE_SPECS.items():
        pattern = os.path.join(amounts_dir, spec["glob"])
        for fpath in sorted(glob.glob(pattern)):
            stats.files_total += 1
            try:
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            fname = os.path.basename(fpath)
            for rcept_no, row in data.items():
                if not isinstance(row, dict):
                    continue
                stats.rows_total += 1
                stats.by_type[sec_type] += 1

                corp_code = str(row.get("corp_code", "")).zfill(8)
                stock_code = rev_map.get(corp_code)
                if stock_code:
                    stats.stock_code_hit += 1
                else:
                    stats.stock_code_miss += 1

                amount = parse_amount(row.get("bd_fta"))
                if amount is None:
                    stats.fail_amount += 1
                price = parse_amount(row.get(spec["price"]))
                if price is None:
                    stats.fail_price += 1
                shares = parse_amount(row.get(spec["shares"]))
                if shares is None:
                    stats.fail_shares += 1
                vs = parse_float(row.get(spec["vs"]))

                start = parse_kdate(row.get(spec["start"]))
                if start is None:
                    stats.fail_start += 1
                end = parse_kdate(row.get(spec["end"]))
                if end is None:
                    stats.fail_end += 1
                maturity = parse_kdate(row.get("bd_mtd"))
                if maturity is None:
                    stats.fail_maturity += 1

                records.append(MezzRecord(
                    sec_type=sec_type,
                    corp_code=corp_code,
                    corp_name=(row.get("corp_name") or "").strip(),
                    rcept_no=str(row.get("rcept_no") or rcept_no),
                    stock_code=stock_code,
                    bd_knd=(row.get("bd_knd") or "").strip(),
                    issue_amount=amount,
                    conv_price=price,
                    shares=shares,
                    vs_pct=vs,
                    start_date=start,
                    end_date=end,
                    maturity_date=maturity,
                    src_file=fname,
                ))

    return records, stats


if __name__ == "__main__":
    recs, st = collect_all()
    print("records:", len(recs), "stats:", st.to_dict())
