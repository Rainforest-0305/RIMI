# -*- coding: utf-8 -*-
"""DART 공시검색(list.json) 폴링 + 종목코드→corp_code 매핑.

패턴 출처: kis-trading/dart_fundamental.py (corp_map, requests 재시도).
이 모듈은 트레이딩 코드를 import 하지 않고 자체 완결(격리)이며 DART OpenAPI
만 호출한다. 키는 config 에서 읽기전용으로 온다.
"""
import io
import json
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import requests

import config

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"

# corp_cls -> 시장 라벨. list.json 각 항목의 corp_cls 로 시장을 표기한다.
MARKET_LABELS = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX", "E": "기타법인"}


def market_label(corp_cls) -> str:
    """corp_cls(Y/K/N/E) -> 사람이 읽는 시장 라벨. 미지값은 원본 반환."""
    c = (corp_cls or "").strip().upper()
    return MARKET_LABELS.get(c, c)


def _request_list(params, max_retries=3, timeout=20):
    """list.json 단건 요청. (data|None, error|None) 반환.

    graceful: 타임아웃/네트워크/HTTP 429·5xx/DART 유량초과(020)는 지수 백오프
    재시도. status 013(데이터없음)은 정상 빈결과. 그 외 상태(키오류 등)는
    재시도 무의미 → 에러문자열과 함께 즉시 반환(크래시 없음).
    """
    backoff = 1.0
    last_err = None
    for _ in range(max_retries):
        try:
            r = requests.get(LIST_URL, params=params, timeout=timeout)
        except requests.Timeout:
            last_err = "timeout"
            time.sleep(backoff); backoff *= 2; continue
        except requests.RequestException as e:
            last_err = f"network:{type(e).__name__}"
            time.sleep(backoff); backoff *= 2; continue

        if r.status_code == 429 or r.status_code >= 500:
            last_err = f"http{r.status_code}"
            # Retry-After 존중(있으면), 없으면 백오프
            ra = r.headers.get("Retry-After")
            try:
                wait = float(ra) if ra else backoff
            except ValueError:
                wait = backoff
            time.sleep(min(wait, 10)); backoff *= 2; continue

        try:
            d = r.json()
        except Exception:
            last_err = "badjson"
            time.sleep(backoff); backoff *= 2; continue

        status = d.get("status")
        if status == "013":                     # 데이터 없음 = 정상
            return {"list": [], "total_page": 0, "total_count": 0}, None
        if status == "020":                      # 사용한도(유량) 초과 -> 백오프
            last_err = "dart020_ratelimit"
            time.sleep(backoff); backoff *= 2; continue
        if status != "000":                      # 키오류 등 재시도 무의미
            return None, f"dart_status_{status}"
        return d, None
    return None, last_err or "unknown"


# ---------- stock_code(6자리) -> corp_code(8자리) ----------
def corp_map():
    f = config.CORP_MAP_FILE
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    r = requests.get(CORP_URL, params={"crtfc_key": config.DART_API_KEY}, timeout=30)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]).decode("utf-8"))
    m = {}
    for e in root.iter("list"):
        stock = (e.findtext("stock_code") or "").strip()
        corp = (e.findtext("corp_code") or "").strip()
        if stock and corp:
            m[stock] = corp
    f.write_text(json.dumps(m), encoding="utf-8")
    return m


def resolve_corp(stock_code):
    """6자리 종목코드 -> 8자리 corp_code. 실패 시 None."""
    return corp_map().get(str(stock_code).zfill(6))


# ---------- 공시검색 ----------
def fetch_disclosures(corp_code, bgn_de=None, end_de=None, page_count=20):
    """특정 corp_code 의 최근 접수공시 목록.
    반환: list[dict]  (없거나 실패 시 []).
    각 항목 주요필드: corp_name, stock_code, report_nm, rcept_no, rcept_dt, flr_nm, rm
    """
    if bgn_de is None:
        bgn_de = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
    if end_de is None:
        end_de = datetime.now().strftime("%Y%m%d")
    params = {
        "crtfc_key": config.DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "page_no": 1,
        "page_count": page_count,
    }
    for _ in range(2):
        try:
            r = requests.get(LIST_URL, params=params, timeout=15)
            d = r.json()
        except Exception:
            time.sleep(1)
            continue
        status = d.get("status")
        if status == "013":   # 조회된 데이터 없음 = 정상(해당 기간 공시 없음)
            return []
        if status != "000":
            # 그 외 상태(키오류·유량초과 등)는 조용히 [] 반환하되 상태코드 노출
            return []
        return d.get("list", [])
    return []


# ---------- 시장 전체 공시검색 (페이지네이션) ----------
def fetch_market_disclosures(corp_cls="Y", days=3, page_count=100, page_no=1,
                             bgn_de=None, end_de=None, max_pages=5):
    """단일 시장(기본 코스피 Y) 최근 접수공시를 **전건 페이지네이션** 수집.

    이전 버전은 page_no=1 한 페이지(최대 100건)만 반환 → 공시 폭주 시 누락.
    이제 total_page 만큼(상한 max_pages) 순회해 전건을 모은다.
    반환: list[dict] (없거나 실패 시 [] — 하위호환).
    """
    items, _ = _fetch_paged(corp_cls, days, page_count, max_pages, bgn_de, end_de)
    return items


def _fetch_paged(corp_cls, days, page_count, max_pages, bgn_de, end_de):
    """단일 시장 페이지네이션. (items, error|None) 반환."""
    if bgn_de is None:
        bgn_de = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    if end_de is None:
        end_de = datetime.now().strftime("%Y%m%d")
    out = []
    page = 1
    while page <= max_pages:
        params = {
            "crtfc_key": config.DART_API_KEY,
            "corp_cls": corp_cls,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "page_no": page,
            "page_count": page_count,
            "sort": "date",
            "sort_mth": "desc",
        }
        d, err = _request_list(params)
        if err:
            return out, err          # 여태 모은 것은 살리고 에러 표기
        out.extend(d.get("list", []) or [])
        try:
            total_page = int(d.get("total_page") or 1)
        except (TypeError, ValueError):
            total_page = 1
        if page >= total_page:
            break
        page += 1
    return out, None


def fetch_markets(days=3, markets=("Y", "K"), page_count=100, max_pages=5,
                  bgn_de=None, end_de=None):
    """여러 시장(기본 **KOSPI Y + KOSDAQ K**) 최근 공시를 페이지네이션으로
    전건 수집·병합. rcept_no 기준 중복제거 후 최신순 정렬.

    반환: (items, errors).
      - items: list[dict] (각 항목에 원본 corp_cls 유지 → 시장 라벨 표기 가능)
      - errors: list[str] (시장별 실패 사유. 부분 실패해도 성공분은 반환)
    """
    merged = {}
    errors = []
    for cls in markets:
        items, err = _fetch_paged(cls, days, page_count, max_pages, bgn_de, end_de)
        for it in items:
            rno = (it.get("rcept_no") or "").strip()
            if rno:
                merged.setdefault(rno, it)
        if err:
            errors.append(f"{market_label(cls)}:{err}")
    merged_items = list(merged.values())
    merged_items.sort(key=lambda x: (x.get("rcept_dt", ""), x.get("rcept_no", "")),
                      reverse=True)
    return merged_items, errors


def dart_url(rcept_no):
    """접수번호 -> 공시 원문 뷰어 URL (사용자에게 원문 링크 제공용)."""
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    cc = resolve_corp("005930")  # 삼성전자
    print("삼성전자 corp_code:", cc)
    for it in fetch_disclosures(cc)[:5]:
        print(it["rcept_dt"], it["report_nm"], "->", dart_url(it["rcept_no"]))
