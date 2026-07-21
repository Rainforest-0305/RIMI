# -*- coding: utf-8 -*-
"""소스2(라이브 증거, DART ≤~20콜): watchlist 상위 N종목 실데이터 조회.

저장소 루트 dart_poll(import 허용) 의 resolve_corp / fetch_disclosures 를 써서
watchlist 상위 5~10 종목의 최근 정기보고서를 실제로 1콜 수준씩 조회한다.

실측 콜카운터: dart_poll.requests.get 을 래핑해 **실제 HTTP GET 횟수**를 센다
(fetch_disclosures 내부 재시도/ corpCode 다운로드까지 포함한 진짜 DART 콜수).
동시에 fetch_disclosures 논리 호출수도 별도로 센다.

graceful: 종목별 try/except, 순차 호출 + 간격(pacing)으로 DART 020(유량초과)
백오프. 키 미로드면 라이브 전체 스킵.
"""
import os
import sys
import time
import json
import io

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (_HERE, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import collect  # noqa: E402

WATCHLIST_FILE = os.path.join(_ROOT, "watchlist.json")


def load_watchlist_stocks(max_n=10):
    """watchlist.json devices.legacy.stocks 상위 max_n 종목 [{name, stock_code}]."""
    try:
        with io.open(WATCHLIST_FILE, encoding="utf-8") as fh:
            wl = json.load(fh)
    except (OSError, ValueError):
        return []
    stocks = (wl.get("devices", {}).get("legacy", {}).get("stocks", []) or [])
    out = []
    for s in stocks[:max_n]:
        code = str(s.get("stock_code", "")).strip()
        if code:
            out.append({"name": s.get("name", ""), "stock_code": code})
    return out


def probe(max_n=5, days=120, pace_sec=0.6):
    """watchlist 상위 max_n(5~10) 종목 라이브 조회.

    반환 dict:
      skipped(bool), reason, http_calls(실측), fetch_calls(논리),
      probed(list of 종목 증거), elapsed_sec, errors(list)
    """
    t0 = time.time()
    stocks = load_watchlist_stocks(max_n)

    # config/dart_poll 은 라이브가 필요할 때만 import(키 없으면 config 는 로드되나
    # 네트워크는 안 씀). import 자체는 저장소 루트 절대 import.
    try:
        import config
        import dart_poll
    except Exception as e:  # noqa: BLE001
        return {"skipped": True, "reason": f"모듈 import 실패: {type(e).__name__}",
                "http_calls": 0, "fetch_calls": 0, "probed": [],
                "elapsed_sec": round(time.time() - t0, 2), "errors": [str(e)],
                "stocks_planned": len(stocks)}

    if not config.DART_API_KEY:
        return {"skipped": True, "reason": "키 미로드로 라이브 스킵",
                "http_calls": 0, "fetch_calls": 0, "probed": [],
                "elapsed_sec": round(time.time() - t0, 2), "errors": [],
                "stocks_planned": len(stocks)}

    # ---- 실측 콜카운터: 실제 HTTP GET 래핑 ----
    counter = {"http": 0, "fetch": 0}
    _orig_get = dart_poll.requests.get

    def _counting_get(*a, **k):
        counter["http"] += 1
        return _orig_get(*a, **k)

    dart_poll.requests.get = _counting_get

    probed = []
    errors = []
    try:
        for i, s in enumerate(stocks):
            code = s["stock_code"]
            rec = {"stock_code": code, "name": s["name"], "corp_code": None,
                   "latest_periodic": None, "note": ""}
            try:
                corp = dart_poll.resolve_corp(code)  # corp_map 캐시 사용(0콜)
                rec["corp_code"] = corp
                if not corp:
                    rec["note"] = "corp_code 미해결"
                    probed.append(rec)
                    continue
                # 순차 pacing (DART 020 유량초과 예방 백오프)
                if i > 0:
                    time.sleep(pace_sec)
                bgn = _daysago(days)
                end = _today()
                counter["fetch"] += 1
                items = dart_poll.fetch_disclosures(corp, bgn_de=bgn, end_de=end,
                                                    page_count=100)
                # 정기보고서만 필터 → 최신 1건 증거
                peri = [it for it in items
                        if collect.report_type(it.get("report_nm", ""))]
                peri.sort(key=lambda x: x.get("rcept_dt", ""), reverse=True)
                if peri:
                    top = peri[0]
                    rec["latest_periodic"] = {
                        "report_nm": top.get("report_nm", ""),
                        "rcept_dt": top.get("rcept_dt", ""),
                        "rcept_no": top.get("rcept_no", ""),
                        "report_type": collect.report_type(top.get("report_nm", "")),
                        "url": dart_poll.dart_url(top.get("rcept_no", "")),
                    }
                    rec["note"] = f"최근 {days}일 정기보고서 {len(peri)}건 중 최신"
                else:
                    rec["note"] = f"최근 {days}일 정기보고서 없음(공시 {len(items)}건)"
            except Exception as e:  # noqa: BLE001  (종목 단위 graceful)
                errors.append(f"{code}:{type(e).__name__}")
                rec["note"] = f"조회 실패: {type(e).__name__}"
                # DART 유량 의심 시 추가 백오프
                time.sleep(pace_sec * 2)
            probed.append(rec)
    finally:
        dart_poll.requests.get = _orig_get  # 원복(격리)

    return {
        "skipped": False,
        "reason": "",
        "http_calls": counter["http"],   # 실측 DART HTTP GET 수
        "fetch_calls": counter["fetch"],  # fetch_disclosures 논리 호출 수
        "probed": probed,
        "elapsed_sec": round(time.time() - t0, 2),
        "errors": errors,
        "stocks_planned": len(stocks),
    }


def _today():
    from datetime import date
    return date.today().strftime("%Y%m%d")


def _daysago(n):
    from datetime import date, timedelta
    return (date.today() - timedelta(days=n)).strftime("%Y%m%d")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    res = probe(max_n=5)
    print("skipped:", res["skipped"], res["reason"])
    print("http_calls(실측):", res["http_calls"], "fetch_calls:", res["fetch_calls"])
    for p in res["probed"]:
        print(p["stock_code"], p["name"], p["corp_code"], p["note"])
        if p["latest_periodic"]:
            print("   ->", p["latest_periodic"]["rcept_dt"],
                  p["latest_periodic"]["report_nm"])
