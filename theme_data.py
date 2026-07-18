# -*- coding: utf-8 -*-
"""
theme_data.py  (WS-22 데이터 레인)

pykrx로 KRX 업종/테마 등락률을 실측하는 read-only 헬퍼.
실계좌·KIS API 무접촉, 무과금. pykrx(KRX/Naver egress)에만 의존.

경로 구조(견고한 2단):
  1) PRIMARY  : 각 테마 대표 구성종목의 개별 OHLCV(get_market_ohlcv_by_date,
                Naver 소스, 로그인 불요)를 받아 거래대금(종가x거래량) 가중 평균
                등락률로 테마 등락을 집계(기존 정상경로 · 정본). 일일 출력은 이
                경로가 담당하며 착수 前 동작과 불변.
  2) FALLBACK : 구성종목 경로가 통째로 결측일 때만, 시장 대표지수(코스피200/
                코스닥150) '추종 ETF 종가 프록시'로 카드를 살린다(fail-open
                안전망). 지수 API(get_index_ohlcv_by_date)는 KRX 로그인
                (KRX_ID/KRX_PW)을 요구해 크론에서 불능이므로, 추종 KODEX
                ETF(069500/229200)의 개별종목 OHLCV(로그인 불요) 전일대비
                등락률을 지수 '프록시'로 사용한다. 지수 실측이 아니라 ETF 종가
                프록시(추적오차·괴리율 존재)임을 universe/부제 라벨에 정직 표기.

반환 등락률(pct)은 모두 '전일대비 일간 등락률(%)' 실측치.
"""

import sys
import argparse
import datetime as _dt

from pykrx import stock


class NoTradingDataError(Exception):
    """mode에 맞는 거래일을 못 찾거나 데이터가 결측일 때 raise."""
    pass


# 대표종목: 거래일 판정용 (KRX 대표 대형주)
_PROBE_TICKER = "005930"  # 삼성전자

# ── PRIMARY: 시장 대표지수 프록시 ETF (표시명 → 추종 ETF 종목코드) ──
# 지수 API(get_index_ohlcv_by_date)는 KRX 로그인 필요 → 크론 불능. 대신 지수를
# 추종하는 KODEX ETF 종가를 개별종목 엔드포인트(get_market_ohlcv_by_date, Naver
# 소스, 로그인 불요)로 조회해 전일대비 등락률을 지수 '프록시'로 산출한다.
#   069500 = KODEX 200      (코스피200 추종)
#   229200 = KODEX 코스닥150 (코스닥150 추종)
# NOTE: 코드값은 canonical(널리 통용). 종목명 확인 엔드포인트
# (get_market_ticker_name/get_etf_ticker_name)는 KRX 로그인을 요구해 라이브
# 확인 불가하나, 두 코드의 개별 OHLCV 는 로그인 없이 정상 조회됨(실측 확인).
_ETF_INDEX_PROXY = [
    ("코스피200", "069500"),
    ("코스닥150", "229200"),
]

# ── FALLBACK: 테마 → 대표 구성종목(티커) ──
_THEME_CONSTITUENTS = {
    "반도체": ["005930", "000660", "042700", "000990"],
    "2차전지": ["373220", "006400", "247540", "051910"],
    "자동차": ["005380", "000270", "012330"],
    "바이오": ["207940", "068270", "000100"],
    "은행금융": ["105560", "055550", "086790"],
    "인터넷": ["035420", "035720"],
    "조선": ["009540", "042660", "010140"],
    "방산": ["012450", "047810", "079550"],
    "철강": ["005490", "004020"],
    "엔터": ["352820", "035900", "041510"],
}

_TOP_N = 5


def _ymd(d):
    return d.strftime("%Y%m%d")


def _iso(d):
    return d.strftime("%Y-%m-%d")


def _parse_date(date_str):
    return _dt.datetime.strptime(date_str, "%Y-%m-%d").date()


def _probe_trading_days(fromdate, todate):
    """대표종목 OHLCV가 존재하는 실제 거래일 목록(datetime.date, 오름차순)을 반환.
    거래일 판정은 캘린더가 아니라 '실데이터 존재'로 한다."""
    df = stock.get_market_ohlcv_by_date(_ymd(fromdate), _ymd(todate), _PROBE_TICKER)
    if df is None or df.empty:
        return []
    return [ts.date() for ts in df.index]


def _resolve_trading_day(mode, date):
    """mode에 맞는 실측 거래일을 반환.
       evening = 당일(입력일이 거래일일 때만), morning = 입력일 직전 거래일.
       룩어헤드 금지: morning은 입력일 당일/미래 데이터를 조회하지 않는다."""
    if mode == "evening":
        # 입력일이 거래일인지 확인 (당일 참조 허용)
        lo = date - _dt.timedelta(days=7)
        days = _probe_trading_days(lo, date)
        if date in days:
            return date
        raise NoTradingDataError(
            "evening: %s 은(는) 거래일이 아님(대표종목 데이터 없음)" % _iso(date)
        )
    elif mode == "morning":
        # 입력일 '직전'까지만 조회 (todate = date - 1). 당일/미래 무참조.
        hi = date - _dt.timedelta(days=1)
        lo = date - _dt.timedelta(days=14)
        days = _probe_trading_days(lo, hi)
        prev = [d for d in days if d < date]
        if prev:
            return max(prev)
        raise NoTradingDataError(
            "morning: %s 직전 거래일을 14일 내에서 못 찾음" % _iso(date)
        )
    else:
        raise ValueError("mode must be 'morning' or 'evening', got %r" % mode)


def _sector_pct_from_ohlcv(df, trade_day):
    """개별종목 OHLCV df에서 trade_day 행의 (등락률, 거래대금가중치) 반환.
       거래대금 = 종가 x 거래량. 데이터 없으면 None."""
    if df is None or df.empty:
        return None
    rows = df[df.index.map(lambda ts: ts.date() == trade_day)]
    if rows.empty:
        return None
    r = rows.iloc[-1]
    pct = float(r["등락률"])
    weight = float(r["종가"]) * float(r["거래량"])
    return pct, weight


def _fetch_via_etf(trade_day):
    """FALLBACK 경로. 시장 대표지수 추종 ETF(KODEX200/코스닥150)의 개별종목
    OHLCV(get_market_ohlcv_by_date, Naver 소스, 로그인 불요)에서 trade_day 종가
    기준 전일대비 등락률(%)을 지수 '프록시'로 산출.

    성공(2종 모두 확보) 시 [(name, pct), ...] 반환. 둘 중 하나라도 결측이면
    부분카드 방지를 위해 None 을 반환해 구성종목 폴백에 위임(fail-open).
    NOTE: 지수 실측이 아니라 ETF 종가 프록시(추적오차·괴리율 존재) — 라벨로 명시."""
    lo = trade_day - _dt.timedelta(days=10)
    items = []
    for name, code in _ETF_INDEX_PROXY:
        try:
            df = stock.get_market_ohlcv_by_date(_ymd(lo), _ymd(trade_day), code)
        except Exception:
            continue
        res = _sector_pct_from_ohlcv(df, trade_day)  # (등락률, 거래대금가중치)
        if res is None:
            continue
        pct, _w = res
        items.append((name, pct))
    if len(items) < len(_ETF_INDEX_PROXY):
        # 대표지수 프록시는 all-or-nothing: 2종 완전집합이 아니면 폴백 위임
        return None
    return items


def _fetch_via_constituents(trade_day):
    """FALLBACK 경로. 테마 대표 구성종목 개별 OHLCV를 거래대금 가중 평균.
       성공 시 [(name, pct), ...] 반환, 데이터 0건이면 None."""
    lo = trade_day - _dt.timedelta(days=10)
    items = []
    for theme, tickers in _THEME_CONSTITUENTS.items():
        num = 0.0
        den = 0.0
        hit = 0
        for t in tickers:
            try:
                df = stock.get_market_ohlcv_by_date(_ymd(lo), _ymd(trade_day), t)
            except Exception:
                continue
            res = _sector_pct_from_ohlcv(df, trade_day)
            if res is None:
                continue
            pct, w = res
            if w <= 0:
                continue
            num += pct * w
            den += w
            hit += 1
        if hit == 0 or den <= 0:
            continue
        items.append((theme, num / den))
    if not items:
        return None
    return items


def fetch_market_themes(mode, date):
    """KRX 업종/테마 등락 실측.

    Args:
        mode: 'morning'(입력일 직전 거래일) 또는 'evening'(입력일 당일).
        date: 'YYYY-MM-DD'.

    Returns:
        dict: {
          'date':   실측한 거래일 'YYYY-MM-DD',
          'source': 'KRX',
          'items':  [{'name': 업종/테마명, 'pct': 전일대비 등락률(%) float}, ...] 상위 3~5,
          'universe': 집계방식 설명 문자열,
        }

    Raises:
        NoTradingDataError: 거래일 미탐지 또는 데이터 결측.
    """
    d = _parse_date(date)
    trade_day = _resolve_trading_day(mode, d)  # 룩어헤드 없는 거래일 확정

    # 1) PRIMARY: 대표 구성종목 거래대금 가중 집계 (기존 정상경로 · 동작·문자열 무변경)
    #    로그인 불요, 테마 리스트가 정본. 일일 출력은 착수 前 베이스라인과 동일 유지.
    items = _fetch_via_constituents(trade_day)
    if items is not None:
        min_rows = 3
        universe = ("업종지수 미응답 폴백: 테마 %d개 각 대표 구성종목의 개별 OHLCV "
                    "전일대비 등락률을 거래대금(종가x거래량) 가중 평균. Naver 소스."
                    % len(_THEME_CONSTITUENTS))
    else:
        # 2) FALLBACK: 시장 대표지수 프록시 ETF (로그인 불요 안전망).
        #    구성종목 경로가 통째로 결측일 때만 발동 → 코스피200·코스닥150 2행.
        items = _fetch_via_etf(trade_day)
        if items is None:
            raise NoTradingDataError(
                "%s: 구성종목/ETF 지수프록시 두 경로 모두 데이터 결측" % _iso(trade_day)
            )
        min_rows = len(_ETF_INDEX_PROXY)  # 코스피200·코스닥150 2종이 완전집합
        universe = ("코스피200·코스닥150 ETF 종가 프록시"
                    "(get_market_ohlcv_by_date, Naver 소스, 로그인 불요) "
                    "전일대비 일간 등락률. 지수 실측이 아니라 추종 "
                    "ETF(069500/229200) 종가 기준 프록시(추적오차·괴리율 존재).")

    items.sort(key=lambda x: x[1], reverse=True)
    top = items[:_TOP_N]
    if len(top) < min_rows:
        # 경로별 최소 행수 미달이면 실데이터 부족으로 간주
        raise NoTradingDataError(
            "%s: 유효 업종/테마 %d개(<%d)로 카드 구성 불가"
            % (_iso(trade_day), len(top), min_rows)
        )

    return {
        "date": _iso(trade_day),
        "source": "KRX",
        "items": [{"name": n, "pct": round(p, 2)} for n, p in top],
        "universe": universe,
    }


def _main():
    ap = argparse.ArgumentParser(description="KRX 업종/테마 등락 실측 (pykrx)")
    ap.add_argument("--mode", required=True, choices=["morning", "evening"])
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    try:
        res = fetch_market_themes(args.mode, args.date)
    except NoTradingDataError as e:
        sys.stderr.write("NoTradingDataError: %s\n" % e)
        sys.exit(1)
    import json
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
