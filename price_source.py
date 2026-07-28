# -*- coding: utf-8 -*-
"""
price_source.py  (WS-32C ③ 시세 소스 우선순위 체인)

단일 종목 현재가/최근종가를 '우선순위 체인'으로 취득하는 read-only 헬퍼.
어느 소스가 응답했는지 반환값(source)과 로그에 라벨링한다. 조용한 실패 금지.

체인(순서 고정):
  1순위 pykrx  : stock.get_market_ohlcv_by_date 최근 종가 (로그인/앱키 불요, KRX/Naver egress)
  2순위 toss   : kis-trading/toss_data.py 의 price()/candles() (KIS-독립 OAuth 소스)
  3순위 fdr    : FinanceDataReader.DataReader 최근 종가 (KIS-독립, pip finance-datareader)
  4순위 None   : 명확한 sentinel(None) + WARNING 로그. 폴백 실패도 반드시 드러난다.

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


# ── 기준 거래일 컷(장중 미확정 행 배제) ──
# 어떤 소스든 '오늘 장중'이면 오늘 날짜 행을 내려준다. 그 값은 체결가일 뿐 종가가
# 아니다. not_after(= 확정 종가의 기준 거래일)를 주면 그 날짜를 넘는 행을 잘라내고
# **직전 확정 종가**를 돌려준다. not_after=None 이면 기존 동작 그대로(하위호환).
def _asof_str(ts):
    """일봉 인덱스 → 'YYYY-MM-DD'. Timestamp/str 어느 쪽이든 안전."""
    try:
        return ts.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return str(ts)[:10]


def _cut(df, not_after):
    """일봉 df 를 not_after(포함)까지로 자른다. not_after 없으면 원본 그대로."""
    if df is None or not_after is None or len(df) == 0:
        return df
    keep = [i for i, ts in enumerate(df.index) if _asof_str(ts) <= str(not_after)]
    if len(keep) == len(df):
        return df
    return df.iloc[keep]


# ── toss_data.py 를 수정 없이 import 하기 위한 경로 추가 ──
_KIS_DIR = Path.home() / "kis-trading"
if _KIS_DIR.is_dir() and str(_KIS_DIR) not in sys.path:
    sys.path.insert(0, str(_KIS_DIR))


def _try_pykrx(ticker, lookback_days=15, not_after=None):
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
    df = _cut(df, not_after)
    if df is None or len(df) == 0:
        logger.info("[price_source] pykrx %s: %s 이전 확정행 없음 → 다음 순위 폴백",
                    ticker, not_after)
        return None
    try:
        last = df.iloc[-1]
        close = float(last["종가"])
        asof = _asof_str(df.index[-1])
    except (KeyError, ValueError, TypeError, IndexError) as e:
        logger.warning("[price_source] pykrx 파싱 실패 %s: %r → 다음 순위 폴백", ticker, e)
        return None
    if close <= 0:
        logger.info("[price_source] pykrx 종가<=0 %s → 다음 순위 폴백", ticker)
        return None
    return close, asof


def _try_toss(ticker, not_after=None):
    """2순위. toss_data.price() 우선, 실패 시 candles() 마지막 종가.
       성공 시 (price:float, asof:str|None), 실패 시 None. (현재 403이면 정상적으로 None 반환.)
       not_after 가 주어지면 2a(실시간 현재가)는 건너뛴다 — 거래일이 없어 확정 종가인지
       판정할 수 없는 값이기 때문(장중이면 그냥 체결가다)."""
    try:
        import toss_data
    except Exception as e:
        logger.warning("[price_source] toss_data import 실패: %r → 다음 순위 폴백", e)
        return None
    # 2a) 실시간 현재가 (기준 거래일 판정이 필요한 호출에서는 사용 불가 → 건너뜀)
    if not_after is None:
        try:
            px = toss_data.price(ticker)
            if px and ticker in px and float(px[ticker]) > 0:
                return float(px[ticker]), None
            logger.info("[price_source] toss price() 응답에 %s 없음/0 → candles 시도", ticker)
        except Exception as e:
            logger.warning("[price_source] toss price() 실패 %s: %r → candles 시도", ticker, e)
    # 2b) 캔들 마지막 종가
    try:
        df = _cut(toss_data.candles(ticker, "1d"), not_after)
        if df is not None and len(df):
            close = float(df["close"].iloc[-1])
            asof = _asof_str(df.index[-1])
            if close > 0:
                return close, asof
        logger.info("[price_source] toss candles 데이터 없음 %s → 다음 순위 폴백", ticker)
    except Exception as e:
        logger.warning("[price_source] toss candles 실패 %s: %r → 다음 순위 폴백", ticker, e)
    return None


def _try_fdr(ticker, lookback_days=15, not_after=None):
    """3순위. FinanceDataReader 최근 종가 (pykrx·toss 모두 실패 시 폴백, KIS-독립).
       성공 시 (price:float, asof:'YYYY-MM-DD'), 실패 시 None."""
    try:
        import FinanceDataReader as fdr
    except Exception as e:
        logger.warning("[price_source] FinanceDataReader import 실패: %r → 다음 순위 폴백", e)
        return None
    try:
        end = _dt.date.today()
        start = end - _dt.timedelta(days=lookback_days)
        df = fdr.DataReader(ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    except Exception as e:
        logger.warning("[price_source] fdr 호출 실패 %s: %r → 다음 순위 폴백", ticker, e)
        return None
    if df is None or len(df) == 0:
        logger.info("[price_source] fdr 데이터 0건 %s → 다음 순위 폴백", ticker)
        return None
    df = _cut(df, not_after)
    if df is None or len(df) == 0:
        logger.info("[price_source] fdr %s: %s 이전 확정행 없음 → 다음 순위 폴백",
                    ticker, not_after)
        return None
    try:
        close = float(df["Close"].iloc[-1])
        asof = _asof_str(df.index[-1])
    except (KeyError, ValueError, TypeError, IndexError) as e:
        logger.warning("[price_source] fdr 파싱 실패 %s: %r → 다음 순위 폴백", ticker, e)
        return None
    if close <= 0:
        logger.info("[price_source] fdr 종가<=0 %s → 다음 순위 폴백", ticker)
        return None
    return close, asof


# 소스 이름 → 시도 함수. get_price(sources=...) 로 순서/부분집합 지정 가능(테스트/폴백검증용).
_DISPATCH = {
    "pykrx": _try_pykrx,
    "toss": _try_toss,
    "fdr": _try_fdr,
}
_DEFAULT_CHAIN = ("pykrx", "toss", "fdr")


def get_price(ticker, sources=_DEFAULT_CHAIN, not_after=None):
    """단일 종목 시세를 우선순위 체인으로 취득.

    Args:
        ticker: 종목코드 문자열 (예 '005930').
        sources: 시도 순서. 기본 ('pykrx','toss','fdr'). 각 소스 실패 시 다음으로 폴백.
        not_after: 'YYYY-MM-DD'. 주면 그 날짜를 넘는 일봉 행(=장중 미확정 체결가)을
            채택하지 않고 직전 확정 종가를 돌려준다. None 이면 종전 동작(하위호환).

    Returns:
        dict: {
          'ticker': str,
          'price':  float | None,          # None = 3순위 폴백(모든 소스 실패)
          'source': 'pykrx'|'toss'|'fdr'|None,  # 실제 응답한 소스 라벨 (None = 폴백)
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
        res = fn(ticker, not_after=not_after)
        if res is not None:
            price, asof = res
            # 방어선(각 소스가 이미 컷했지만, 어떤 경로로도 미확정 종가가 새어나가면 안 된다)
            if not_after and asof and str(asof) > str(not_after):
                logger.warning("[price_source] %s %s: 미확정 종가 %s > 기준일 %s → 기각",
                               ticker, name, asof, not_after)
                chain.append({"source": name, "ok": False})
                continue
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


# ── 일봉 시계열(중간 거래일 backfill 용) ──
# get_price 는 '마지막 확정 종가' 1점만 준다. 그 계약으로는 중간에 빠진 거래일을
# 메울 수 없다(배치가 07-22 까지 쓰고 서버가 07-24 를 붙이면 07-23 이 영원히 빈다).
# 아래는 같은 소스·같은 _cut(not_after) 규칙으로 **구간**을 돌려주는 별도 경로다.
# 기존 함수들은 손대지 않는다 — 장중 미확정 컷 동작에 회귀를 만들지 않기 위해서.
_SERIES_MAX_DAYS = 400        # 조회창 상한(무한 조회 방지). 호출부가 더 크게 줘도 여기서 잘린다


def _norm_series(df, col, not_after):
    """일봉 df → [[YYYY-MM-DD, close:int], ...] 오름차순. not_after 컷 적용. 실패 시 []."""
    df = _cut(df, not_after)
    if df is None or len(df) == 0 or col not in df:
        return []
    out = []
    for ts, v in df[col].items():
        try:
            ds = _asof_str(ts)
            iv = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if iv > 0 and len(ds) == 10:
            out.append([ds, iv])
    out.sort(key=lambda r: r[0])
    return out


def _series_pykrx(ticker, lookback_days, not_after):
    from pykrx import stock
    end = _dt.date.today()
    start = end - _dt.timedelta(days=lookback_days)
    df = stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"),
                                        end.strftime("%Y%m%d"), ticker)
    return _norm_series(df, "종가", not_after)


def _series_fdr(ticker, lookback_days, not_after):
    import FinanceDataReader as fdr
    end = _dt.date.today()
    start = end - _dt.timedelta(days=lookback_days)
    df = fdr.DataReader(ticker, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    return _norm_series(df, "Close", not_after)


def _series_toss(ticker, lookback_days, not_after):
    import toss_data
    return _norm_series(toss_data.candles(ticker, "1d"), "close", not_after)


_SERIES_DISPATCH = {"pykrx": _series_pykrx, "toss": _series_toss, "fdr": _series_fdr}


def get_series(ticker, sources=_DEFAULT_CHAIN, not_after=None, lookback_days=45):
    """확정 일봉 종가 구간을 우선순위 체인으로 취득(읽기 전용).

    Args:
        not_after: 'YYYY-MM-DD'. 이 날짜를 넘는 행(장중 미확정 체결가)은 제외.
        lookback_days: 오늘 기준 소급 일수. _SERIES_MAX_DAYS(400) 로 상한을 강제한다.

    Returns:
        {'ticker','series':[[date,close:int],...],'source':str|None,'chain':[...]}
        어떤 예외도 던지지 않는다. 전 소스 실패 시 series=[] , source=None.
    """
    try:
        lookback_days = max(1, min(int(lookback_days), _SERIES_MAX_DAYS))
    except (TypeError, ValueError):
        lookback_days = 45
    chain = []
    for name in sources:
        fn = _SERIES_DISPATCH.get(name)
        if fn is None:
            chain.append({"source": name, "ok": False})
            continue
        try:
            ser = fn(ticker, lookback_days, not_after)
        except Exception as e:  # noqa: BLE001
            logger.warning("[price_source] series %s %s 실패: %r → 다음 순위", name, ticker, e)
            chain.append({"source": name, "ok": False})
            continue
        # 방어선: 어떤 경로로도 미확정 종가가 새어나가면 안 된다(각 소스가 이미 컷했지만).
        if not_after:
            ser = [r for r in ser if str(r[0]) <= str(not_after)]
        if ser:
            chain.append({"source": name, "ok": True})
            logger.info("[price_source] series %s → %d행 (%s~%s, source=%s)",
                        ticker, len(ser), ser[0][0], ser[-1][0], name)
            return {"ticker": ticker, "series": ser, "source": name, "chain": chain}
        chain.append({"source": name, "ok": False})
    logger.warning("[price_source] series %s: 모든 소스 실패 → 빈 구간", ticker)
    return {"ticker": ticker, "series": [], "source": None, "chain": chain}


if __name__ == "__main__":
    import argparse
    import json
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(description="시세 우선순위 체인 헬퍼 (pykrx→toss→fdr→None)")
    ap.add_argument("--ticker", default="005930")
    ap.add_argument("--sources", default="pykrx,toss,fdr",
                    help="시도 순서 콤마구분 (예 'pykrx,toss,fdr' | 'fdr' | 'toss,fdr')")
    ap.add_argument("--not-after", dest="not_after", default=None,
                    help="기준 거래일 'YYYY-MM-DD'. 이 날짜를 넘는 장중 미확정 행 배제")
    args = ap.parse_args()
    src = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    out = get_price(args.ticker, sources=src, not_after=args.not_after)
    print(json.dumps(out, ensure_ascii=False, indent=2))
