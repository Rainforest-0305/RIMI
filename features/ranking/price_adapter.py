# -*- coding: utf-8 -*-
"""TOSS 시세 read-only 어댑터 (features/ranking 전용, 경량 복사판).

원본: C:\\Users\\urimk\\kis-trading\\toss_data.py (민감 실계좌 레포).
그 레포를 import 하지 않고(의존 회피) 필요한 read-only GET만 복사·경량화했다.

★안전 규칙 (이 파일이 지키는 게이트):
- read-only GET(/prices, /candles)만 구현. 주문/계좌 변경 엔드포인트 없음.
- kis-trading 레포를 **읽지도 쓰지도 않는다**(U7-3 이후). 키는 gongsi-alert/.env
  **단일 소스**, 토큰 캐시는 이 폴더의 .toss_token.local.json 뿐이다.
  ★U7-3 이전 주석은 「수정하지 않는다」만 말해서 독자가 「분리됐다」로 읽었다.
    실제로는 .env·토큰캐시를 **읽는** 폴백이 3곳 있었다 — 「수정 안 함」은 참이지만
    **불완전**했고, 그래서 거짓보다 잡기 어려웠다. 회귀는 ops_env_guard.py 가 잡는다.
- 토큰 값은 로그/출력에 절대 찍지 않는다(조용한 실패 금지 = 상태코드/사유만).
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = "https://openapi.tossinvest.com"
_EXP_MARGIN = 120  # 만료 여유(초)

_HERE = Path(__file__).resolve().parent
_GONGSI_ENV = _HERE.parent.parent / ".env"                 # gongsi-alert/.env (단일 소스)
# ★U7-3: kis-trading 폴백 3곳 제거(실계좌 경계 위반). 키·토큰은 이 레포 것만 쓴다.
#   제거 전에는 _KIS_DIR/".env" 와 _KIS_DIR/".toss_token.json" 을 직접 읽었고,
#   특히 토큰 캐시는 «키 부재와 무관하게» get_token() 에서 항상 먼저 시도됐다.
_LOCAL_TOKF = _HERE / ".toss_token.local.json"             # 우리가 쓸 로컬 캐시

# 관측용 외부호출 카운터(demo 가 콜예산 실측에 사용). 토큰 발급콜과 GET콜 분리.
CALLS = {"token_post": 0, "get_prices": 0, "get_candles": 0}


def reset_calls():
    for k in CALLS:
        CALLS[k] = 0


def _log(msg):
    sys.stderr.write(f"[price_adapter] {msg}\n")
    sys.stderr.flush()


def _read_env_key(path: Path, key: str):
    """path(.env)에서 key= 값 1줄 읽기(값은 반환만, 출력 안 함)."""
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def _creds():
    """(app_key, secret). gongsi-alert/.env ★단일 소스.
    kis-trading/.env 폴백 제거(U7-3) — 실계좌 레포 참조는 경계 위반이다.
    [실측 2026-08-16] gongsi-alert/.env 에 TOSS_APP_KEY·TOSS_SECRET «있음» → 기능 영향 0."""
    ak = _read_env_key(_GONGSI_ENV, "TOSS_APP_KEY")
    sk = _read_env_key(_GONGSI_ENV, "TOSS_SECRET")
    return ak, sk


def _read_cache(path: Path):
    """유효(만료 여유 전)한 access_token 을 캐시파일에서 읽어 반환. 없으면 None."""
    try:
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        if d.get("access_token") and d.get("exp", 0) - _EXP_MARGIN > time.time():
            return d["access_token"]
    except (ValueError, OSError) as e:
        _log(f"cache unreadable {path.name}: {e}")
    return None


def get_token(force=False):
    """유효 토큰 반환. 순서: 로컬캐시 → 신규발급(로컬에만 저장).
    ★U7-3: kis-trading/.toss_token.json 직독 제거. 그건 «실계좌의 유효 토큰»이라
      .env 폴백보다 직접적인 경계 위반이었고, 키 부재와 «무관하게» 항상 먼저 탔다.
      제거 비용은 로컬 캐시가 빌 때 «신규 발급 1회» 추가뿐이고 이후 캐시로 수렴한다.
    force=True 면 캐시 무시하고 신규발급(401/403 후 재시도용). 토큰 값은 출력 안 함."""
    if not force:
        tok = _read_cache(_LOCAL_TOKF)
        if tok:
            return tok
    ak, sk = _creds()
    if not ak or not sk:
        raise RuntimeError("TOSS_APP_KEY/TOSS_SECRET missing (gongsi-alert/.env)")
    CALLS["token_post"] += 1
    r = requests.post(BASE + "/oauth2/token",
                      data={"grant_type": "client_credentials",
                            "client_id": ak, "client_secret": sk}, timeout=15)
    if not r.ok:
        _log(f"token endpoint {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    j = r.json()
    j["exp"] = time.time() + int(j.get("expires_in", 3600))
    # 로컬 캐시에만 원자적 저장(kis-trading 무수정).
    try:
        tmp = _LOCAL_TOKF.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(j))
        os.replace(tmp, _LOCAL_TOKF)
    except OSError as e:
        _log(f"local token cache save failed (무시): {e}")
    return j["access_token"]


def _get(path, params=None):
    """read-only GET. 401/403 시 캐시 무시 재발급 1회 재시도."""
    for attempt in (1, 2):
        h = {"Authorization": "Bearer " + get_token(force=(attempt == 2))}
        r = requests.get(BASE + path, params=params or {}, headers=h, timeout=15)
        if r.status_code in (401, 403) and attempt == 1:
            _log(f"GET {path} -> {r.status_code}; retrying with fresh token")
            continue
        if not r.ok:
            _log(f"GET {path} -> {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        return r.json()


def price(symbols):
    """현재가(배치). symbols: '005930' 또는 리스트. → {sym: float}. 1 GET."""
    if isinstance(symbols, (list, tuple)):
        symbols = ",".join(symbols)
    CALLS["get_prices"] += 1
    res = _get("/api/v1/prices", {"symbols": symbols}).get("result", [])
    return {r["symbol"]: float(r["lastPrice"]) for r in res}


def candles_raw(symbol, interval="1d"):
    """일봉 원시 리스트 [{timestamp, open, high, low, close, volume}...] 최신순 아님.
    pandas 의존 없이 dict 리스트로 반환(경량). 1 GET/symbol."""
    CALLS["get_candles"] += 1
    res = _get("/api/v1/candles", {"symbol": symbol, "interval": interval})
    rows = res.get("result", {}).get("candles", [])
    out = []
    for c in rows:
        try:
            out.append({
                "timestamp": c["timestamp"],
                "open": float(c["openPrice"]), "high": float(c["highPrice"]),
                "low": float(c["lowPrice"]), "close": float(c["closePrice"]),
                "volume": float(c["volume"])})
        except (KeyError, ValueError, TypeError):
            continue
    out.sort(key=lambda x: x["timestamp"])  # 오름차순(과거→최신)
    return out


def movers_for(codes, interval="1d"):
    """후보 종목코드 리스트 → 일간 등락 지표.

    반환: (results, stats)
      results: {code: {"price": 최근종가, "prev_close":.., "change_pct": 전일대비%,
                        "volume": 최근거래량}}  (조회 성공분만)
      stats:   {"toss_calls": int, "requested": int, "resolved": int,
                "errors": [ "code: 사유" ... ], "degraded": bool}

    change_pct = (최근종가/직전종가 - 1)*100. 일봉 2개 이상 있어야 산출.
    콜예산: 종목당 candles 1콜(배치 미지원 엔드포인트). 상위 N 캡은 호출측 책임.
    첫 종목에서 토큰/네트워크가 죽으면 즉시 중단하고 degraded=True(조용한 실패 금지).
    """
    reset_calls()
    results = {}
    errors = []
    degraded = False
    for i, code in enumerate(codes):
        try:
            rows = candles_raw(code, interval=interval)
            if len(rows) < 2:
                errors.append(f"{code}: 일봉 부족({len(rows)})")
                continue
            last, prev = rows[-1], rows[-2]
            pc = prev["close"]
            change = ((last["close"] / pc - 1.0) * 100.0) if pc else None
            results[code] = {
                "price": round(last["close"], 2),
                "prev_close": round(pc, 2),
                "change_pct": round(change, 2) if change is not None else None,
                "volume": last["volume"],
            }
        except requests.RequestException as e:
            # 첫 호출부터 실패하면 인증/네트워크 전역장애로 보고 중단(graceful degrade).
            msg = f"{code}: {type(e).__name__} {getattr(e, 'response', None) and e.response.status_code or ''}".strip()
            errors.append(msg)
            if i == 0:
                degraded = True
                _log(f"first-call failure → degrade movers: {msg}")
                break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{code}: {type(e).__name__}")
    stats = {
        "toss_calls": CALLS["get_candles"] + CALLS["token_post"],
        "token_post": CALLS["token_post"],
        "candle_get": CALLS["get_candles"],
        "requested": len(codes),
        "resolved": len(results),
        "errors": errors,
        "degraded": degraded,
    }
    return results, stats


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    r, s = movers_for(["005930", "000660"])
    print("stats:", s)
    print("results:", json.dumps(r, ensure_ascii=False))
