# -*- coding: utf-8 -*-
"""전환가 vs 현재가 괴리(price parity) 스모크 — 비-DART.

pykrx 또는 FinanceDataReader 로 watchlist 상위 종목의 현재가를 라이브 조회해
전환/행사가 대비 괴리율을 계산한다. stock_code 없으면 스킵. 실패해도 모듈은
죽지 않게 전부 try/except + 스킵 카운트.

주의: 여기서 쓰는 소스는 KRX/FDR(비-DART). DART 라이브 콜은 0.
"""
from datetime import date, timedelta


def _fetch_last_close(stock_code: str):
    """현재가(최근 종가) 조회. (price:int|None, source:str)."""
    # 1순위 pykrx
    try:
        from pykrx import stock as _pk
        end = date.today()
        start = end - timedelta(days=14)
        df = _pk.get_market_ohlcv(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), stock_code)
        if df is not None and len(df) > 0:
            close = df["종가"].iloc[-1]
            if close and close > 0:
                return int(close), "pykrx"
    except Exception:
        pass
    # 2순위 FinanceDataReader
    try:
        import FinanceDataReader as _fdr
        start = (date.today() - timedelta(days=14)).strftime("%Y-%m-%d")
        df = _fdr.DataReader(stock_code, start)
        if df is not None and len(df) > 0:
            close = df["Close"].iloc[-1]
            if close and close > 0:
                return int(close), "fdr"
    except Exception:
        pass
    return None, "none"


def run_parity_smoke(holdings, top_n: int = 5):
    """holdings(build_holdings 결과)의 상위 종목에 대해 괴리 스모크.

    선정: stock_code 있고 min_conv_price 있는 종목 중 active_shares 상위 top_n.
    반환: dict(results, checked, skipped_no_code, skipped_no_price, fetch_fail,
              dart_live_calls=0)
    """
    result = {
        "results": [],
        "checked": 0,
        "skipped_no_code": 0,
        "skipped_no_price": 0,
        "fetch_fail": 0,
        "dart_live_calls": 0,
    }

    # 후보 필터
    candidates = []
    for h in holdings:
        if not h.get("stock_code"):
            result["skipped_no_code"] += 1
            continue
        if not h.get("min_conv_price"):
            result["skipped_no_price"] += 1
            continue
        candidates.append(h)
    candidates = candidates[:top_n]

    for h in candidates:
        code = h["stock_code"]
        conv = h["min_conv_price"]
        try:
            price, src = _fetch_last_close(code)
        except Exception:
            price, src = None, "none"
        if price is None:
            result["fetch_fail"] += 1
            result["results"].append({
                "corp_name": h["corp_name"],
                "stock_code": code,
                "conv_price": conv,
                "current_price": None,
                "parity_pct": None,
                "source": "none",
                "status": "fetch_fail",
            })
            continue
        # 괴리율: (현재가 - 전환가) / 전환가 * 100  (양수=현재가가 위, 전환 유리)
        parity = round((price - conv) / conv * 100, 2)
        result["checked"] += 1
        result["results"].append({
            "corp_name": h["corp_name"],
            "stock_code": code,
            "conv_price": conv,
            "current_price": price,
            "parity_pct": parity,
            "source": src,
            "in_the_money": price > conv,
            "status": "ok",
        })
    return result
