# -*- coding: utf-8 -*-
"""
theme_data.py  (WS-22 데이터 레인)

pykrx로 KRX 업종/테마 등락률을 실측하는 read-only 헬퍼.
실계좌·KIS API 무접촉, 무과금. pykrx(KRX/Naver egress)에만 의존.

경로 구조(견고한 2단):
  1) PRIMARY  : KRX 업종지수 OHLCV(get_index_ohlcv_by_date)로 업종별 전일대비
                등락률 직접 산출. (KRX egress 필요 -- Partner 크론 런타임에서 정상)
  2) FALLBACK : 업종지수가 응답 없거나 비면, 각 테마 대표 구성종목의 개별
                OHLCV(get_market_ohlcv_by_date, Naver 소스)를 받아 거래대금
                (종가x거래량) 가중 평균 등락률로 테마 등락을 집계.

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

# ── PRIMARY: KRX 업종지수 티커 → 표시명 (KOSPI 업종지수, KRX 고정 코드) ──
_SECTOR_INDEX = {
    "1005": "음식료품",
    "1008": "화학",
    "1009": "의약품",
    "1011": "철강금속",
    "1012": "기계",
    "1013": "전기전자",
    "1015": "운수장비",
    "1016": "유통업",
    "1017": "전기가스업",
    "1018": "건설업",
    "1020": "통신업",
    "1021": "금융업",
    "1024": "은행",
    "1025": "증권",
    "1026": "보험",
    "1027": "서비스업",
}

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


def _fetch_via_index(trade_day):
    """PRIMARY 경로. KRX 업종지수 OHLCV로 업종별 전일대비 등락률 산출.
       성공 시 [(name, pct), ...] 반환, 데이터 0건이면 None."""
    lo = trade_day - _dt.timedelta(days=10)
    items = []
    for code, name in _SECTOR_INDEX.items():
        try:
            df = stock.get_index_ohlcv_by_date(_ymd(lo), _ymd(trade_day), code)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        rows = df[df.index.map(lambda ts: ts.date() == trade_day)]
        if rows.empty:
            continue
        try:
            pct = float(rows.iloc[-1]["등락률"])
        except (KeyError, ValueError, TypeError):
            continue
        items.append((name, pct))
    if not items:
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

    # 1) PRIMARY: 업종지수
    items = _fetch_via_index(trade_day)
    if items is not None:
        universe = ("KRX 업종지수(get_index_ohlcv_by_date) 전일대비 일간 등락률. "
                    "KOSPI 업종지수 %d개 중 데이터 존재 업종을 등락률 내림차순 상위 정렬."
                    % len(_SECTOR_INDEX))
    else:
        # 2) FALLBACK: 대표 구성종목 거래대금 가중 집계
        items = _fetch_via_constituents(trade_day)
        if items is None:
            raise NoTradingDataError(
                "%s: 업종지수/구성종목 두 경로 모두 데이터 결측" % _iso(trade_day)
            )
        universe = ("업종지수 미응답 폴백: 테마 %d개 각 대표 구성종목의 개별 OHLCV "
                    "전일대비 등락률을 거래대금(종가x거래량) 가중 평균. Naver 소스."
                    % len(_THEME_CONSTITUENTS))

    items.sort(key=lambda x: x[1], reverse=True)
    top = items[:_TOP_N]
    if len(top) < 3:
        # 상위 3개도 못 채우면 실데이터 부족으로 간주
        raise NoTradingDataError(
            "%s: 유효 업종/테마 %d개(<3)로 카드 구성 불가" % (_iso(trade_day), len(top))
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
