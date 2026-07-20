# -*- coding: utf-8 -*-
"""WS-34 ② 전환청구권/신주인수권 행사 실적 수집·파싱 (격리 모듈).

종목(corp_code) -> list.json(report_nm='전환청구권행사'/'신주인수권행사')
 -> document.xml -> HTML표 파싱.

핵심 산출(함정4: '잔량 직접값' 채택):
  "전환사채 잔액" 표(표2)의 회차별
    - unconv_balance        : 신고일 현재 미전환사채 잔액(원)  ← 진짜 오버행(금액)
    - remaining_conv_shares : 전환가능 주식수(잔량)            ← 진짜 오버행(주식)
    - conv_price            : (리픽싱 반영된) 현재 전환가액
  이 표는 '해당 sec_type 의 잔존 트랜치 전체'를 신고일 기준으로 나열하므로,
  가장 최근 행사공시 1건이 곧 현재 순잔량의 권위 있는 스냅샷.

일별 청구내역(표1)에서 이번 회차 청구일/발행주식수도 부수 수집.
DART 콜: list.json + document.xml(dart_doc 캐시). KIS 무관.
"""
import io
import json
import os
import re
from datetime import datetime

import pandas as pd

try:
    import config
    from dart_poll import _request_list
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)))
    import config
    from dart_poll import _request_list

try:
    import dart_doc
except ImportError:  # pragma: no cover
    from features.mezzanine_calendar import dart_doc

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))

_PURE_INT = re.compile(r"\d[\d,]*$")
_DATE_RE = re.compile(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})")


def is_conversion_report(report_nm: str) -> bool:
    """전환청구권행사 / 신주인수권행사(BW) 실적 공시 여부.
    '...조정' 은 리픽싱이므로 제외."""
    if not report_nm:
        return False
    if "조정" in report_nm:
        return False
    return ("전환청구권행사" in report_nm) or ("신주인수권행사" in report_nm)


def sec_type_from_report(report_nm: str) -> str:
    if report_nm and "신주인수권" in report_nm:
        return "BW"
    return "CB"


def _flatten_tables(html: str):
    try:
        return pd.read_html(io.StringIO(html))
    except Exception:
        return []


def _cells(row):
    return [("" if pd.isna(c) else str(c)).strip() for c in row.tolist()]


def _pure_ints(cells):
    out = []
    for c in cells:
        c = c.strip()
        if _PURE_INT.match(c):
            try:
                out.append(int(c.replace(",", "")))
            except ValueError:
                pass
    return out


def _find_date(cells):
    for c in cells:
        m = _DATE_RE.search(c)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_conversion_html(html: str, report_nm: str = "", rcept_dt: str = None):
    """행사 원문 HTML -> 정규화 dict.

    반환:
      {sec_type, cum_exercised_shares, total_shares, exercise_date,
       claims:[{tranche, conv_price, shares_issued}],
       balance:[{tranche, face_total, unconv_balance, conv_price,
                 remaining_conv_shares}]}
    balance 가 핵심(순잔량). 빈 리스트면 파싱 실패.
    """
    tables = _flatten_tables(html)
    sec_type = sec_type_from_report(report_nm)
    out = {
        "sec_type": sec_type,
        "cum_exercised_shares": None,
        "total_shares": None,
        "exercise_date": None,
        "claims": [],
        "balance": [],
    }
    if not tables:
        return out

    for t in tables:
        rows = [_cells(r) for _, r in t.iterrows()]
        if not rows:
            continue
        head_join = " ".join(" ".join(r) for r in rows[:2])

        # (a) 누계·발행주식총수 표
        if "누계" in head_join or "발행주식총수" in head_join:
            for r in rows:
                j = " ".join(r)
                ints = _pure_ints(r)
                if "누계" in j and ints and out["cum_exercised_shares"] is None:
                    out["cum_exercised_shares"] = ints[-1]
                if "발행주식총수" in j and "대비" not in j and ints \
                        and out["total_shares"] is None:
                    out["total_shares"] = ints[-1]

        # (b) 일별 청구내역 표(청구일자/회차/전환가액/발행한 주식수)
        if ("청구일자" in head_join or "행사일자" in head_join) and "발행한" in head_join:
            # 헤더행(들) 다음 데이터행: 첫 셀이 날짜
            for r in rows:
                d = _find_date([r[0]]) if r else None
                if not d:
                    continue
                ints = _pure_ints(r)
                # 패턴: [날짜, 회차, (종류), 청구금액?, 전환가액, 발행주식수, ...]
                # 회차=첫 정수, 전환가액/발행주식수는 뒤쪽. 안전히 위치 대신 값추출.
                tr = ints[0] if ints else None
                # 전환가액·발행주식수: 청구금액(가장 큰 값, '원' 포함 텍스트라 순수정수 아님)
                # 순수정수 리스트 = [회차, 전환가액, 발행주식수] 형태가 일반적.
                price = ints[1] if len(ints) >= 2 else None
                shares_issued = ints[2] if len(ints) >= 3 else (
                    ints[1] if len(ints) == 2 else None)
                if out["exercise_date"] is None:
                    out["exercise_date"] = d
                out["claims"].append({
                    "tranche": tr, "conv_price": price,
                    "shares_issued": shares_issued, "date": d,
                })

        # (c) 잔액 표(회차/권면총액/미전환잔액/전환가액/전환가능주식수)  ← 핵심
        if ("미전환" in head_join and "잔액" in head_join) or \
           ("잔액" in head_join and "전환가능" in head_join):
            for r in rows:
                # 헤더/통화행 스킵: 첫 셀이 회차(정수)인 데이터행만
                if not r or not _PURE_INT.match(r[0].strip()):
                    continue
                ints = _pure_ints(r)
                # KRW 텍스트 셀은 제외되어 ints = [회차, 권면총액, 미전환잔액, 전환가액, 잔량]
                if len(ints) < 5:
                    continue
                tr, face, unconv, price, rem = ints[0], ints[1], ints[2], ints[3], ints[4]
                out["balance"].append({
                    "tranche": tr,
                    "face_total": face,
                    "unconv_balance": unconv,
                    "conv_price": price,
                    "remaining_conv_shares": rem,
                })

    if out["exercise_date"] is None and rcept_dt:
        out["exercise_date"] = _norm_dt(rcept_dt)
    return out


def _norm_dt(s):
    if not s:
        return None
    s = str(s).strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    m = _DATE_RE.search(s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
def _list_conversion(corp_code, bgn_de, end_de, max_pages=4, page_count=100):
    hits, calls, err = [], 0, None
    page = 1
    while page <= max_pages:
        params = {
            "crtfc_key": config.DART_API_KEY, "corp_code": corp_code,
            "bgn_de": bgn_de, "end_de": end_de,
            "page_no": page, "page_count": page_count,
            "sort": "date", "sort_mth": "desc",
        }
        d, e = _request_list(params)
        calls += 1
        if e:
            err = e
            break
        for it in d.get("list", []) or []:
            if is_conversion_report(it.get("report_nm", "")):
                hits.append(it)
        try:
            total_page = int(d.get("total_page") or 1)
        except (TypeError, ValueError):
            total_page = 1
        if page >= total_page:
            break
        page += 1
    return hits, calls, err


def _cache_file(corp_code):
    d = os.path.join(_REPO_ROOT, "bench_cache", "conv")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{corp_code}.json")


def collect_conversion(corp_code, bgn_de="20220101", end_de=None,
                       use_cache=True, refresh_list=True):
    """종목의 행사 실적 전건 수집.

    반환: {corp_code, filings:[{rcept_no, report_nm, rcept_dt, sec_type,
           exercise_date, cum_exercised_shares, total_shares, balance:[...]}],
           latest_balance_by_sec, report_count, doc_ok, doc_fail,
           list_calls, doc_calls}
    latest_balance_by_sec[sec_type] = 가장 최근 exercise_date 공시의 잔액표.
    """
    if end_de is None:
        end_de = datetime.now().strftime("%Y%m%d")
    cf = _cache_file(corp_code)
    if use_cache and not refresh_list and os.path.exists(cf):
        try:
            with open(cf, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass

    hits, list_calls, list_err = _list_conversion(corp_code, bgn_de, end_de)
    filings = []
    doc_ok = doc_fail = doc_calls = 0
    for it in hits:
        rno = it.get("rcept_no")
        html, src = dart_doc.fetch_document_html(rno, kind="conv", use_cache=use_cache)
        if src == "fetch":
            doc_calls += 1
        if html is None:
            doc_fail += 1
            continue
        doc_ok += 1
        parsed = parse_conversion_html(html, report_nm=it.get("report_nm", ""),
                                       rcept_dt=it.get("rcept_dt"))
        parsed["rcept_no"] = rno
        parsed["report_nm"] = it.get("report_nm", "")
        parsed["rcept_dt"] = _norm_dt(it.get("rcept_dt"))
        filings.append(parsed)

    # sec_type 별 최신(잔액표가 있는) 공시 채택 (참고용)
    latest = {}
    # 회차별 '최근언급' 잔액: 발행사마다 잔액표에 '행사된 회차 1개'만 싣는 곳(아스트)과
    # '잔존 트랜치 전체'를 싣는 곳(우리기술)이 섞여 있다. 단일 최신공시로는
    # 전자에서 다른 회차 잔량을 과소계상(0)하므로, 회차별로 가장 최근 언급을 채택.
    balance_by_tranche = {}   # {sec: {회차: {remaining, unconv_balance, conv_price, date}}}
    for f in sorted(filings, key=lambda x: (x.get("exercise_date") or "",
                                            x.get("rcept_dt") or "")):
        if not f.get("balance"):
            continue
        sec = f["sec_type"]
        fdate = f.get("exercise_date") or f.get("rcept_dt")
        latest[sec] = {
            "rcept_no": f["rcept_no"], "exercise_date": fdate, "balance": f["balance"],
        }
        d = balance_by_tranche.setdefault(sec, {})
        for b in f["balance"]:
            tr = b["tranche"]
            # 정렬이 오름차순이므로 뒤에서 덮어쓰면 최근언급이 남는다.
            d[tr] = {
                "remaining_conv_shares": b["remaining_conv_shares"],
                "unconv_balance": b["unconv_balance"],
                "conv_price": b["conv_price"],
                "date": fdate,
            }

    result = {
        "corp_code": corp_code,
        "filings": filings,
        "latest_balance_by_sec": latest,
        "balance_by_tranche": balance_by_tranche,
        "report_count": len(hits),
        "doc_ok": doc_ok,
        "doc_fail": doc_fail,
        "list_calls": list_calls,
        "doc_calls": doc_calls,
        "list_err": list_err,
    }
    try:
        with open(cf, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
    except OSError:
        pass
    return result
