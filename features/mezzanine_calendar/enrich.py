# -*- coding: utf-8 -*-
"""메자닌 레코드 시세연계 인리치 — 비-DART.

추가 데이터 2종:
  ③ In/Out-of-the-money 판정
     각 트랜치의 전환/행사가를 현재가와 비교.
       전환가 < 현재가 -> moneyness='in'  (전환유인 큼·희석 임박)
       전환가 > 현재가 -> moneyness='out' (전환 가능성 낮음)
       거의 동일(±0.5%)  -> moneyness='at'
     premium_pct(괴리율) = (전환가 - 현재가) / 현재가 * 100
       (양수=전환가가 현재가 위 = out-of-the-money 프리미엄,
        음수=전환가가 현재가 아래 = in-the-money)

  ④ 시가총액 대비 희석률
     dilution_vs_mktcap_pct = 전환주식수 × 현재가 ÷ 시가총액 × 100
     시총/상장주식수는 pykrx(get_market_cap_by_date) 1순위, FDR StockListing 2순위
     (전부 비-DART). 발행주식수 대비 vs_pct(공시시점 기준)와 구분되는,
     "현재 시총/현재 상장주식수" 기준의 희석 지표.

시세/시총 소스는 KRX(pykrx)·FDR 뿐. DART 라이브 콜 = 0.
시세는 상위 N종목(기본 5) 라이브만, 전부 try/except + 스킵/실패 카운트.
"""
from datetime import date, timedelta

# 가격 조회는 price_parity 재사용(중복 구현 금지, DART 콜 0).
try:
    from price_parity import _fetch_last_close  # type: ignore
except ImportError:  # pragma: no cover
    from features.mezzanine_calendar.price_parity import _fetch_last_close  # type: ignore


# ---------------------------------------------------------------------------
# 시총/상장주식수 소스 (비-DART)
# ---------------------------------------------------------------------------
# FDR StockListing('KRX') 는 시장 전체 스냅샷 1콜로 code->시총/상장주식수 제공.
# 반복 조회를 피하려 모듈 레벨 1회 캐시.
_LISTING_CACHE = {"loaded": False, "map": {}}


def _load_listing_map():
    """FDR StockListing('KRX') -> {code: {marcap, stocks, market}}. 1회 캐시.

    실패해도 죽지 않게 빈 dict. (네트워크 1콜, 비-DART.)
    """
    if _LISTING_CACHE["loaded"]:
        return _LISTING_CACHE["map"]
    m = {}
    try:
        import FinanceDataReader as _fdr
        df = _fdr.StockListing("KRX")
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                code = str(row.get("Code", "")).zfill(6)
                if not code or code == "000000":
                    continue
                try:
                    marcap = row.get("Marcap")
                    stocks = row.get("Stocks")
                    marcap = int(marcap) if marcap and marcap > 0 else None
                    stocks = int(stocks) if stocks and stocks > 0 else None
                except (TypeError, ValueError):
                    marcap, stocks = None, None
                m[code] = {
                    "marcap": marcap,
                    "stocks": stocks,
                    "market": str(row.get("Market", "") or ""),
                }
    except Exception:
        pass
    _LISTING_CACHE["loaded"] = True
    _LISTING_CACHE["map"] = m
    return m


def _fetch_mktcap(stock_code, listing_map):
    """현재 시총/상장주식수 조회. (market_cap:int|None, listed_shares:int|None,
    market:str|None, source:str).

    1순위 pykrx get_market_cap_by_date, 2순위 FDR StockListing 스냅샷.
    """
    # 1순위 pykrx
    try:
        from pykrx import stock as _pk
        end = date.today()
        start = end - timedelta(days=14)
        df = _pk.get_market_cap_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), stock_code)
        if df is not None and len(df) > 0:
            row = df.iloc[-1]
            marcap = int(row["시가총액"]) if "시가총액" in df.columns else None
            shares = int(row["상장주식수"]) if "상장주식수" in df.columns else None
            if marcap and marcap > 0:
                return marcap, (shares if shares and shares > 0 else None), None, "pykrx"
    except Exception:
        pass
    # 2순위 FDR StockListing 스냅샷
    info = listing_map.get(str(stock_code).zfill(6))
    if info and info.get("marcap"):
        return info["marcap"], info.get("stocks"), info.get("market") or None, "fdr"
    return None, None, (info.get("market") if info else None), "none"


# ---------------------------------------------------------------------------
# 판정 유틸
# ---------------------------------------------------------------------------
_AT_BAND_PCT = 0.5  # 현재가 대비 ±0.5% 이내는 'at'(등가)로 본다.


def classify_moneyness(conv_price, current_price):
    """(moneyness:'in'|'out'|'at'|None, premium_pct:float|None).

    premium_pct = (conv - current) / current * 100.
    """
    if not conv_price or not current_price or current_price <= 0:
        return None, None
    premium = round((conv_price - current_price) / current_price * 100, 2)
    if abs(premium) <= _AT_BAND_PCT:
        return "at", premium
    if conv_price < current_price:
        return "in", premium
    return "out", premium


def dilution_vs_mktcap(shares, current_price, market_cap):
    """전환주식수 × 현재가 ÷ 시가총액 × 100. 결측 시 None."""
    if not shares or not current_price or not market_cap or market_cap <= 0:
        return None
    return round(shares * current_price / market_cap * 100, 3)


# ---------------------------------------------------------------------------
# 메인: 상위 N종목 인리치
# ---------------------------------------------------------------------------
def enrich_top_holdings(holdings, top_n: int = 5):
    """holdings(build_holdings 결과)의 상위 N종목에 ③④ 부여.

    선정: stock_code 있고 min_conv_price 있는 종목 중 상위(=active_shares desc,
          build_holdings 정렬 유지) top_n. 시세는 이들만 라이브 조회.

    각 결과 dict:
      corp_name, stock_code, market, current_price, price_source,
      min_conv_price, moneyness, premium_pct,
      market_cap, listed_shares, mktcap_source,
      total_shares, active_shares,
      dilution_vs_mktcap_pct(총물량 기준),
      active_dilution_vs_mktcap_pct(활성물량 기준),
      vs_pct_disclosure(공시 발행주식대비 최댓값, 비교용),
      tranches:[{sec_type, conv_price, shares, moneyness, premium_pct,
                 dilution_vs_mktcap_pct}]

    반환 dict:
      results, checked, skipped_no_code, skipped_no_price,
      price_fail, mktcap_fail,
      moneyness_dist{in,out,at} (종목 min_conv 기준),
      tranche_moneyness_dist{in,out,at},
      dilution_stats{min,max,median,n}(총물량 시총희석%),
      dart_live_calls=0
    """
    result = {
        "results": [],
        "checked": 0,
        "skipped_no_code": 0,
        "skipped_no_price": 0,
        "price_fail": 0,
        "mktcap_fail": 0,
        "moneyness_dist": {"in": 0, "out": 0, "at": 0},
        "tranche_moneyness_dist": {"in": 0, "out": 0, "at": 0},
        "dilution_stats": {"min": None, "max": None, "median": None, "n": 0},
        "dart_live_calls": 0,
    }

    # 후보 필터 (price_parity 와 동일 기준)
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

    listing_map = _load_listing_map() if candidates else {}
    dilution_values = []

    for h in candidates:
        code = h["stock_code"]
        min_conv = h["min_conv_price"]
        total_shares = h.get("total_shares") or 0
        active_shares = h.get("active_shares") or 0
        # 공시 발행주식대비 최댓값(비교용)
        vs_disc = None
        for t in h.get("tranches", []):
            v = t.get("vs_pct")
            if v is not None:
                vs_disc = v if vs_disc is None else max(vs_disc, v)

        try:
            price, psrc = _fetch_last_close(code)
        except Exception:
            price, psrc = None, "none"
        try:
            marcap, listed, market, msrc = _fetch_mktcap(code, listing_map)
        except Exception:
            marcap, listed, market, msrc = None, None, None, "none"

        rec = {
            "corp_name": h["corp_name"],
            "stock_code": code,
            "market": market,
            "current_price": price,
            "price_source": psrc,
            "min_conv_price": min_conv,
            "moneyness": None,
            "premium_pct": None,
            "market_cap": marcap,
            "listed_shares": listed,
            "mktcap_source": msrc,
            "total_shares": total_shares,
            "active_shares": active_shares,
            "dilution_vs_mktcap_pct": None,
            "active_dilution_vs_mktcap_pct": None,
            "vs_pct_disclosure": vs_disc,
            "tranches": [],
        }

        if price is None:
            result["price_fail"] += 1
        else:
            result["checked"] += 1
            m, prem = classify_moneyness(min_conv, price)
            rec["moneyness"] = m
            rec["premium_pct"] = prem
            if m in result["moneyness_dist"]:
                result["moneyness_dist"][m] += 1

        if marcap is None:
            result["mktcap_fail"] += 1

        # ④ 시총 대비 희석 (총물량/활성물량)
        d_total = dilution_vs_mktcap(total_shares, price, marcap)
        d_active = dilution_vs_mktcap(active_shares, price, marcap)
        rec["dilution_vs_mktcap_pct"] = d_total
        rec["active_dilution_vs_mktcap_pct"] = d_active
        if d_total is not None:
            dilution_values.append(d_total)

        # 트랜치별 ③④ (현재가/시총 확보 시)
        for t in h.get("tranches", []):
            tm, tprem = classify_moneyness(t.get("conv_price"), price)
            td = dilution_vs_mktcap(t.get("shares"), price, marcap)
            if tm in result["tranche_moneyness_dist"]:
                result["tranche_moneyness_dist"][tm] += 1
            rec["tranches"].append({
                "sec_type": t.get("sec_type"),
                "conv_price": t.get("conv_price"),
                "shares": t.get("shares"),
                "moneyness": tm,
                "premium_pct": tprem,
                "dilution_vs_mktcap_pct": td,
            })

        result["results"].append(rec)

    # 희석 분포 통계
    if dilution_values:
        sv = sorted(dilution_values)
        n = len(sv)
        median = sv[n // 2] if n % 2 else round((sv[n // 2 - 1] + sv[n // 2]) / 2, 3)
        result["dilution_stats"] = {
            "min": sv[0], "max": sv[-1], "median": median, "n": n,
        }
    return result
