# -*- coding: utf-8 -*-
"""WS-34 ① 리픽싱(전환가액의 조정) 수집·파싱 (격리 모듈).

종목(corp_code) -> list.json(report_nm 필터) -> document.xml -> HTML표 파싱.
회차별 조정후 전환/행사/교환가액과 조정후 (전환가능)주식수를 뽑는다.

함정 반영:
  - 상향/하향 모두 채택(사토시 2,642->3,070 상향, 모아 970->1,379 상향 실측).
  - BW/EB 변형 report_nm 포함(신주인수권행사가액의조정, ...교환가액...조정).
  - "조정후"가 곧 최신 유효가/유효잔량. 이 값으로 오버행·moneyness 재계산.

DART 콜: list.json + document.xml. 원문은 dart_doc 가 캐시(재호출 0).
파싱 산출은 bench_cache/refix/<corp_code>.json 에 캐시. 실계좌·KIS 무관.
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

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))

# report_nm 필터 키워드(부분일치). 파일럿 실측 보고서명 반영.
#   전환가액의조정 / 신주인수권행사가액의조정 / ...교환가액...조정(안내공시)
_REFIX_KEYS = ("전환가액", "행사가액", "교환가액")
_REFIX_MUST = "조정"

_PURE_INT = re.compile(r"\d[\d,]*$")
_DATE_RE = re.compile(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})")


def is_refixing_report(report_nm: str) -> bool:
    """report_nm 이 리픽싱(전환가액/행사가액/교환가액의 조정)인지."""
    if not report_nm:
        return False
    if _REFIX_MUST not in report_nm:
        return False
    return any(k in report_nm for k in _REFIX_KEYS)


def _sec_from_kind_text(kind_text: str) -> str:
    """'증권의 종류' 셀 텍스트로 CB/BW/EB 판정."""
    if not kind_text:
        return ""
    if "신주인수권" in kind_text:
        return "BW"
    if "교환사채" in kind_text or ("교환" in kind_text and "전환" not in kind_text):
        return "EB"
    if "전환사채" in kind_text or "전환" in kind_text:
        return "CB"
    return ""


def sec_type_from_report(report_nm: str, html: str = "") -> str:
    """보고서명/원문으로 CB/BW/EB 판정. 통합 안내공시는 '증권의 종류' 행이 정본."""
    text = (report_nm or "") + " " + (html or "")[:400]
    if "신주인수권" in text and "전환" not in (report_nm or ""):
        return "BW"
    if "교환" in text and "전환" not in (report_nm or "") and "신주인수권" not in (report_nm or ""):
        return "EB"
    return "CB"


# ---------------------------------------------------------------------------
# 파서
# ---------------------------------------------------------------------------
def _flatten_rows(html: str):
    """read_html 전 테이블을 (셀 문자열 리스트) 행들로 평탄화."""
    try:
        tables = pd.read_html(io.StringIO(html))
    except Exception:
        return []
    rows = []
    for t in tables:
        for _, r in t.iterrows():
            rows.append([("" if pd.isna(c) else str(c)).strip() for c in r.tolist()])
    return rows


def _pure_ints(cells):
    """행 셀 중 순수 정수(콤마허용)만 순서대로 int 리스트."""
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
            try:
                return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            except ValueError:
                continue
    return None


def parse_refixing_html(html: str, report_nm: str = "", rcept_dt: str = None):
    """리픽싱 원문 HTML -> 회차별 이벤트 리스트.

    각 원소:
      {tranche:int, sec_type:str, conv_price_before:int|None,
       conv_price_after:int|None, shares_before:int|None,
       shares_after:int|None, unconv_face:int|None,
       adjust_date:str|None, reason:str|None}
    """
    rows = _flatten_rows(html)
    if not rows:
        return []
    sec_type = sec_type_from_report(report_nm, html)

    price_hdr = shares_hdr = None
    adjust_date = None
    reason = None
    for i, row in enumerate(rows):
        j = " ".join(row)
        # '증권의 종류' 행이 있으면 그 값으로 sec_type 확정(통합 안내공시 대응).
        if "증권의 종류" in j or "증권의종류" in j:
            for c in row:
                st = _sec_from_kind_text(c)
                if st and "종류" not in c:
                    sec_type = st
                    break
        # 가격 헤더: 조정전+조정후+'가액'(단, 주식 헤더가 아님).
        #   표준 "조정전 전환가액" 과 통합 "조정전 가액(원)" 모두 매칭.
        if price_hdr is None and "조정전" in j and "조정후" in j and \
           "가액" in j and "주식" not in j:
            price_hdr = i
        if shares_hdr is None and "조정전" in j and "조정후" in j and "주식" in j:
            shares_hdr = i
        if adjust_date is None and "적용일" in j:
            adjust_date = _find_date(row)
        if reason is None and "조정사유" in j:
            # 같은 행 뒤쪽 셀 중 '조정사유' 아닌 텍스트
            for c in row:
                if c and "조정사유" not in c and not c.startswith(("3.", "4.")):
                    reason = c[:120]
                    break
    if adjust_date is None:
        # 본문 어디든 '적용일' 다음 날짜 못찾으면 접수일 폴백
        adjust_date = rcept_dt if not rcept_dt else _norm_dt(rcept_dt)

    # 가격 데이터 행: price_hdr 다음부터 shares_hdr 전까지, int>=3 인 행
    price_by_tr = {}
    if price_hdr is not None:
        end = shares_hdr if shares_hdr is not None else len(rows)
        for row in rows[price_hdr + 1:end]:
            ints = _pure_ints(row)
            if len(ints) >= 3:
                tr = ints[0]
                # 회차 뒤 고유 정수: before, after (after 는 마지막 고유값)
                rest = [x for x in ints[1:]]
                before = rest[0]
                after = rest[-1]
                price_by_tr[tr] = (before, after)

    # 주식수 데이터 행: shares_hdr 다음부터 int>=3 인 행(연속)
    shares_by_tr = {}
    if shares_hdr is not None:
        for row in rows[shares_hdr + 1:]:
            ints = _pure_ints(row)
            if len(ints) < 3:
                # 데이터 행 종료(다음 섹션)
                if shares_by_tr:
                    break
                continue
            tr = ints[0]
            rest = ints[1:]
            # 패턴: [회차, 미전환권면총액, 조정전주식수, 조정후주식수]
            face = rest[0] if len(rest) >= 3 else None
            s_before = rest[-2]
            s_after = rest[-1]
            shares_by_tr[tr] = (face, s_before, s_after)

    tranches = sorted(set(price_by_tr) | set(shares_by_tr))
    events = []
    for tr in tranches:
        pb, pa = price_by_tr.get(tr, (None, None))
        face, sb, sa = shares_by_tr.get(tr, (None, None, None))
        events.append({
            "tranche": tr,
            "sec_type": sec_type,
            "conv_price_before": pb,
            "conv_price_after": pa,
            "shares_before": sb,
            "shares_after": sa,
            "unconv_face": face,
            "adjust_date": adjust_date,
            "reason": reason,
        })
    return events


def _norm_dt(s):
    """YYYYMMDD 또는 YYYY-MM-DD -> YYYY-MM-DD."""
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
# 수집 (list.json + document.xml)
# ---------------------------------------------------------------------------
def _list_refixing(corp_code, bgn_de, end_de, max_pages=4, page_count=100):
    """corp_code 의 리픽싱 공시 목록. (hits:list[dict], calls:int, err|None)."""
    hits = []
    calls = 0
    page = 1
    err = None
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
            if is_refixing_report(it.get("report_nm", "")):
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
    d = os.path.join(_REPO_ROOT, "bench_cache", "refix")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{corp_code}.json")


def collect_refixing(corp_code, bgn_de="20220101", end_de=None,
                     use_cache=True, refresh_list=True):
    """종목의 리픽싱 이벤트 전건 수집.

    반환: {corp_code, events:[...], report_count, doc_ok, doc_fail,
           list_calls, doc_calls}
    events 는 회차별(중복 회차는 여러 조정이력 모두 보존, adjust_date 순).
    document.xml 은 dart_doc 캐시로 재호출 0.
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

    hits, list_calls, list_err = _list_refixing(corp_code, bgn_de, end_de)
    events = []
    doc_ok = doc_fail = doc_calls = 0
    for it in hits:
        rno = it.get("rcept_no")
        html, src = dart_doc.fetch_document_html(rno, kind="refix", use_cache=use_cache)
        if src == "fetch":
            doc_calls += 1
        if html is None:
            doc_fail += 1
            continue
        doc_ok += 1
        evs = parse_refixing_html(html, report_nm=it.get("report_nm", ""),
                                  rcept_dt=it.get("rcept_dt"))
        for e in evs:
            e["rcept_no"] = rno
            e["report_nm"] = it.get("report_nm", "")
            if not e.get("adjust_date"):
                e["adjust_date"] = _norm_dt(it.get("rcept_dt"))
            events.append(e)

    result = {
        "corp_code": corp_code,
        "events": events,
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
