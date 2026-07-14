# -*- coding: utf-8 -*-
"""빌드타임 스크립트: DART corpCode.xml + KRX 상장목록 -> data/corp_index.json.

목적: 런타임 검색(/api/search)이 DART/네트워크를 **0콜**로 하도록, 상장종목의
      (종목코드, 종목명, 시장) 인덱스를 **빌드시 1회만** 생성한다.

데이터 소스(빌드타임에만 네트워크 사용):
- 종목명: config.DART_API_KEY 로 corpCode.xml 1회 다운로드·파싱(패턴: dart_poll.corp_map).
          stock_code 가 있는(상장) 항목만 추출. corpCode.xml 엔 시장구분이 없다.
- 시장구분: KRX 상장법인목록(kind.krx.co.kr corpList.do)을 시장별 1회씩 받아
          종목코드 -> KOSPI/KOSDAQ/KONEX 매핑을 만든다(재현 가능한 공개 소스).
          KRX 취득 실패 시 graceful: 해당 종목 market="-" 폴백(크래시 없음).

- 출력: data/corp_index.json = 리스트
    [{"code":"005930","name":"삼성전자","market":"KOSPI"}, ...]
  code = 6자리 zfill, name = corp_name strip, market = KOSPI/KOSDAQ/KONEX/"-".
- 원자적 저장(.tmp -> os.replace).

주의: 이 스크립트만 네트워크(DART+KRX)를 호출한다. 런타임 검색은 이 파일만 읽는다.
"""
import io
import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import requests

import config

CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
OUT_FILE = config.DATA / "corp_index.json"

# KRX 상장법인목록(EXCEL) 다운로드. 시장구분(marketType) 별로 1회씩.
KRX_URL = "http://kind.krx.co.kr/corpgeneral/corpList.do"
KRX_MARKETS = [("stockMkt", "KOSPI"), ("kosdaqMkt", "KOSDAQ"), ("konexMkt", "KONEX")]
# 종목코드 셀 패턴: 숫자 6자리(KOSPI/KOSDAQ) 또는 영숫자 6자리(KONEX 예:0070X0).
_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")


class _TableParser(HTMLParser):
    """KRX corpList 다운로드 HTML(<table> 안 <tr><td>...)에서 행별 셀 텍스트 수집."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._cur = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._cur = []
        elif tag == "td" and self._cur is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self._cur.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._cur is not None:
            self.rows.append(self._cur)
            self._cur = None


def _fetch_krx_market(market_type):
    """단일 시장 상장목록을 받아 종목코드 집합 반환. 실패 시 예외 전파(호출부에서 격리)."""
    r = requests.get(
        KRX_URL,
        params={"method": "download", "marketType": market_type},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60,
    )
    r.raise_for_status()
    html = r.content.decode("euc-kr", "replace")  # KRX 다운로드는 EUC-KR
    p = _TableParser()
    p.feed(html)
    codes = set()
    for row in p.rows:
        for cell in row:
            c = cell.strip()
            if _CODE_RE.match(c):
                codes.add(c.zfill(6))
                break  # 행당 첫 코드 셀(종목코드)만
    return codes


def fetch_krx_market_map():
    """종목코드 -> 시장라벨 매핑. 시장별 부분 실패는 격리(그 시장만 스킵).
    전부 실패해도 빈 dict 반환(호출부에서 "-" 폴백)."""
    market_map = {}
    for mtype, label in KRX_MARKETS:
        try:
            codes = _fetch_krx_market(mtype)
            for c in codes:
                # 충돌 시 먼저 채운 시장 우선(KOSPI>KOSDAQ>KONEX). 실제로는 겹치지 않음.
                market_map.setdefault(c, label)
            print(f"  KRX {label}: {len(codes)}건")
        except Exception as e:  # noqa: BLE001 — 시장 1개 실패가 빌드 전체를 막지 않게
            print(f"  KRX {label} 취득 실패(건너뜀): {e}")
    return market_map


def build():
    if not config.DART_API_KEY:
        raise SystemExit("DART_API_KEY 가 비어있다. config/.env 확인.")

    # 1) DART corpCode.xml: 상장종목 (코드, 종목명)
    r = requests.get(CORP_URL, params={"crtfc_key": config.DART_API_KEY}, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]).decode("utf-8"))

    # 2) KRX 상장목록: 종목코드 -> 시장라벨 (빌드타임 1회, 실패 시 "-" 폴백)
    print("KRX 상장목록 취득 중...")
    market_map = fetch_krx_market_map()

    rows = []
    seen_codes = set()
    for e in root.iter("list"):
        stock = (e.findtext("stock_code") or "").strip()
        name = (e.findtext("corp_name") or "").strip()
        if not stock or not name:
            continue  # 상장(stock_code 보유) 항목만
        code = stock.zfill(6)
        if code in seen_codes:
            continue  # 종목코드 중복 방지(첫 항목 우선)
        seen_codes.add(code)
        market = market_map.get(code, "-")  # KRX 매핑 없으면 graceful "-"
        rows.append({"code": code, "name": name, "market": market})

    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, OUT_FILE)  # 원자적 교체
    return rows, market_map


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    rows, market_map = build()
    filled = sum(1 for x in rows if x["market"] != "-")
    print(f"corp_index.json 생성: {len(rows)}건 -> {OUT_FILE}")
    print(f"시장구분 채움: {filled}/{len(rows)}건 (KRX 매핑 {len(market_map)}종목)")
    samsung = [x for x in rows if x["code"] == "005930"]
    print("삼성전자(005930):", samsung[0] if samsung else "없음(!)")
