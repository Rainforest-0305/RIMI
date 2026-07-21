# -*- coding: utf-8 -*-
"""ranking demo — 실제 피드 + 실제 TOSS 시세로 payload 생성 후 실측 콘솔 출력.

DoD: (a) payload 샘플(JSON 일부), (b) 표본수(공시종목 N·시세조회 N),
     (c) 외부 콜수(DART·TOSS), (d) 소요초.

- 피드: app._get_feed(force=False) 1회 취득(force 폴링 안 함 = 콜 최소화).
- 시세: price_adapter.movers_for(read-only). 401/네트워크 실패 시 movers 부분만
  graceful degrade(빈 movers + 사유), 공시/화제 파트는 실측 완주.
- app.py 무수정: dart_poll.requests.get 은 demo 프로세스 내에서만 래핑해 콜 카운트.
"""
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent  # gongsi-alert repo 루트
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import price_adapter  # noqa: E402  (features/ranking 로컬)
from ranking import build_ranking_payload  # noqa: E402


def _install_dart_counter():
    """dart_poll 의 opendart HTTP GET 콜을 세는 카운터 설치(demo 국소, 파일 무수정)."""
    import dart_poll
    counter = {"n": 0}
    orig = dart_poll.requests.get

    def counting_get(url, *a, **k):
        if isinstance(url, str) and "opendart.fss.or.kr" in url:
            counter["n"] += 1
        return orig(url, *a, **k)

    dart_poll.requests.get = counting_get
    return counter, (lambda: setattr(dart_poll.requests, "get", orig))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    t0 = time.time()

    dart_counter, restore = _install_dart_counter()
    import app  # noqa: E402  (import 시 startup 훅은 uvicorn 아래서만 발화 → 부작용 없음)

    t_feed0 = time.time()
    feed = app._get_feed(force=False)   # 캐시 피드 1회 취득(force 금지)
    t_feed = time.time() - t_feed0
    dart_calls = dart_counter["n"]
    restore()

    alerts = feed.get("alerts", [])
    cached = feed.get("cached")

    # 실제 TOSS 시세 price_fn 주입(read-only). 콜예산=후보 수(candidate_cap).
    def price_fn(codes):
        return price_adapter.movers_for(codes, interval="1d")

    t_pay0 = time.time()
    payload = build_ranking_payload(feed, price_fn=price_fn, cand_cap=20)
    t_pay = time.time() - t_pay0

    pm = payload["meta"]["price_meta"]
    total_sec = time.time() - t0

    # ---- 실측 콘솔 출력 ----
    print("=" * 72)
    print("RANKING DEMO 실측 (features/ranking/demo.py)")
    print("=" * 72)
    print(f"[표본수] feed alerts(공시)      = {len(alerts)}")
    print(f"[표본수] 랭킹 대상 종목(코드有) = {payload['meta']['ranked_stocks']}")
    print(f"[표본수] 시세조회 후보(cap {payload['meta']['candidate_cap']})   = {payload['meta']['price_candidates']}")
    print(f"[표본수] 시세 산출 성공         = {pm.get('resolved')}")
    print("-" * 72)
    print(f"[콜수 ] DART GET               = {dart_calls}  (feed force=False, cached={cached})")
    print(f"[콜수 ] TOSS GET(candles+token)= {pm.get('toss_calls')}")
    if pm.get("degraded"):
        print(f"[경고 ] price_movers DEGRADED  : {pm.get('reason') or pm.get('errors')}")
    if pm.get("errors"):
        print(f"[경고 ] TOSS 오류샘플          : {pm['errors'][:3]}")
    print("-" * 72)
    print(f"[소요 ] feed 취득              = {t_feed:.2f}s")
    print(f"[소요 ] payload 조립(+시세)    = {t_pay:.2f}s")
    print(f"[소요 ] 총                     = {total_sec:.2f}s")
    print("=" * 72)

    # payload 샘플(상위 일부만)
    sample = {
        "generated_at": payload["generated_at"],
        "feed_generated_at": payload["feed_generated_at"],
        "ref_day": payload["ref_day"],
        "disclosure_hot": payload["disclosure_hot"][:5],
        "buzz": payload["buzz"][:5],
        "price_movers": {
            "gainers": payload["price_movers"]["gainers"][:5],
            "losers": payload["price_movers"]["losers"][:5],
        },
        "meta": {
            "feed_alerts": payload["meta"]["feed_alerts"],
            "ranked_stocks": payload["meta"]["ranked_stocks"],
            "price_candidates": payload["meta"]["price_candidates"],
            "buzz_proxy": payload["meta"]["buzz_proxy"],
            "price_meta": pm,
        },
    }
    print("[payload 샘플 JSON (상위 5씩)]")
    print(json.dumps(sample, ensure_ascii=False, indent=2))
    return payload


if __name__ == "__main__":
    main()
