# -*- coding: utf-8 -*-
"""
price_source.py  (WS-32C ③ 시세 소스 우선순위 체인)

단일 종목 현재가/최근종가를 '우선순위 체인'으로 취득하는 read-only 헬퍼.
어느 소스가 응답했는지 반환값(source)과 로그에 라벨링한다. 조용한 실패 금지.

체인(순서 고정):
  1순위 pykrx  : stock.get_market_ohlcv_by_date 최근 종가 (로그인/앱키 불요, KRX/Naver egress)
  2순위 toss   : kis-trading/toss_data.py 의 price()/candles() (KIS-독립 OAuth 소스)
  3순위 None   : 명확한 sentinel(None) + WARNING 로그. 폴백 실패도 반드시 드러난다.

★안전: 이 모듈은 GET/조회만 한다. 주문·계좌 변경 없음.
       toss_data.py 는 절대 수정하지 않고 import만 한다.

의존성 주의: 토스(2순위)는 403 등으로 실패할 수 있다(WS-32C ① 별도 수리).
            그 경우에도 체인은 반드시 다음 순위로 폴백해야 한다 — 이게 핵심.
"""

import sys
import logging
import datetime as _dt
from pathlib import Path

logger = logging.getLogger("price_source")

# 3순위 폴백을 나타내는 명시 sentinel. price is None 으로도 판별 가능.
SOURCE_NONE = None

# ── toss_data.py 를 수정 없이 import 하기 위한 경로 추가 ──
_KIS_DIR = Path.home() / "kis-trading"
if _KIS_DIR.is_dir() and str(_KIS_DIR) not in sys.path:
    sys.path.insert(0, str(_KIS_DIR))


def _try_pykrx(ticker, lookback_days=15):
    """1순위. pykrx 최근 종가. 성공 시 (price:float, asof:'YYYY-MM-DD'), 실패 시 None."""
    try:
        from pykrx import stock
    except Exception as e:
        logger.warning("[price_source] pykrx import 실패: %r", e)
        return None
    try:
        end = _dt.date.today()
        start = end - _dt.timedelta(days=lookback_days)
        df = stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker)
    except Exception as e:
        logger.warning("[price_source] pykrx 호출 실패 %s: %r", ticker, e)
        return None
    if df is None or len(df) == 0:
        logger.info("[price_source] pykrx 데이터 0건 %s → 다음 순위 폴백", ticker)
        return None
    try:
        last = df.iloc[-1]
        close = float(last["종가"])
        asof = df.index[-1].strftime("%Y-%m-%d")
    except (KeyError, ValueError, TypeError, IndexError) as e:
        logger.warning("[price_source] pykrx 파싱 실패 %s: %r → 다음 순위 폴백", ticker, e)
        return None
    if close <= 0:
        logger.info("[price_source] pykrx 종가<=0 %s → 다음 순위 폴백", ticker)
        return None
    return close, asof


def _try_toss(ticker):
    """2순위. toss_data.price() 우선, 실패 시 candles() 마지막 종가.
       성공 시 (price:float, asof:str|None), 실패 시 None. (현재 403이면 정상적으로 None 반환.)"""
    try:
        import toss_data
    except Exception as e:
        logger.warning("[price_source] toss_data import 실패: %r → 다음 순위 폴백", e)
        return None
    # 2a) 실시간 현재가
    try:
        px = toss_data.price(ticker)
        if px and ticker in px and float(px[ticker]) > 0:
            return float(px[ticker]), None
        logger.info("[price_source] toss price() 응답에 %s 없음/0 → candles 시도", ticker)
    except Exception as e:
        logger.warning("[price_source] toss price() 실패 %s: %r → candles 시도", ticker, e)
    # 2b) 캔들 마지막 종가
    try:
        df = toss_data.candles(ticker, "1d")
        if df is not None and len(df):
            close = float(df["close"].iloc[-1])
            asof = df.index[-1].strftime("%Y-%m-%d")
            if close > 0:
                return close, asof
        logger.info("[price_source] toss candles 데이터 없음 %s → 다음 순위 폴백", ticker)
    except Exception as e:
        logger.warning("[price_source] toss candles 실패 %s: %r → 다음 순위 폴백", ticker, e)
    return None


# 소스 이름 → 시도 함수. get_price(sources=...) 로 순서/부분집합 지정 가능(테스트/폴백검증용).
_DISPATCH = {
    "pykrx": _try_pykrx,
    "toss": _try_toss,
}
_DEFAULT_CHAIN = ("pykrx", "toss")


def get_price(ticker, sources=_DEFAULT_CHAIN):
    """단일 종목 시세를 우선순위 체인으로 취득.

    Args:
        ticker: 종목코드 문자열 (예 '005930').
        sources: 시도 순서. 기본 ('pykrx','toss'). 각 소스 실패 시 다음으로 폴백.

    Returns:
        dict: {
          'ticker': str,
          'price':  float | None,          # None = 3순위 폴백(모든 소스 실패)
          'source': 'pykrx'|'toss'|None,   # 실제 응답한 소스 라벨 (None = 폴백)
          'asof':   'YYYY-MM-DD' | None,   # 시세 기준일(가능한 경우)
          'chain':  [{'source':..,'ok':bool}, ...],  # 시도 궤적(관측용)
        }
        어느 경우에도 예외를 던지지 않는다(체인은 조용히가 아니라 로그로 실패를 드러냄).
    """
    chain = []
    for name in sources:
        fn = _DISPATCH.get(name)
        if fn is None:
            logger.warning("[price_source] 알 수 없는 소스 %r 건너뜀", name)
            chain.append({"source": name, "ok": False})
            continue
        res = fn(ticker)
        if res is not None:
            price, asof = res
            logger.info("[price_source] %s → %s @%s (source=%s)", ticker, price, asof, name)
            chain.append({"source": name, "ok": True})
            return {"ticker": ticker, "price": price, "source": name,
                    "asof": asof, "chain": chain}
        chain.append({"source": name, "ok": False})
    # 3순위: 최종 폴백
    logger.warning("[price_source] %s: 모든 소스 실패 → sentinel(None) 반환. 시도=%s",
                   ticker, [c["source"] for c in chain])
    return {"ticker": ticker, "price": SOURCE_NONE, "source": None,
            "asof": None, "chain": chain}


if __name__ == "__main__":
    import argparse
    import json
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(description="시세 우선순위 체인 헬퍼 (pykrx→toss→None)")
    ap.add_argument("--ticker", default="005930")
    ap.add_argument("--sources", default="pykrx,toss",
                    help="시도 순서 콤마구분 (예 'pykrx,toss' | 'toss' | 'toss,pykrx')")
    args = ap.parse_args()
    src = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    out = get_price(args.ticker, sources=src)
    print(json.dumps(out, ensure_ascii=False, indent=2))
