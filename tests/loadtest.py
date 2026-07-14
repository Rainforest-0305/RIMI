# -*- coding: utf-8 -*-
"""동시 부하테스트: /api/alerts 처리량·p95지연·에러율·메모리 측정.

사용:
    python tests/loadtest.py --port 8140 --conc 50 --total 500
서버는 별도로 기동돼 있어야 한다(이 스크립트는 클라이언트만).
psutil 있으면 서버 프로세스 RSS도 측정(--pid 로 지정).
"""
import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

sys.stdout.reconfigure(encoding="utf-8")


def one(base, path):
    t0 = time.perf_counter()
    try:
        r = requests.get(base + path, timeout=120)
        dt = time.perf_counter() - t0
        return (r.status_code, dt, len(r.content))
    except Exception as e:
        dt = time.perf_counter() - t0
        return (-1, dt, str(e))


def run(base, path, conc, total):
    lat = []
    errs = 0
    codes = {}
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(one, base, path) for _ in range(total)]
        for f in futs:
            code, dt, _ = f.result()
            lat.append(dt)
            codes[code] = codes.get(code, 0) + 1
            if code != 200:
                errs += 1
    wall = time.perf_counter() - t0
    lat.sort()

    def pct(p):
        if not lat:
            return 0.0
        i = min(len(lat) - 1, int(round(p / 100 * (len(lat) - 1))))
        return lat[i]

    print(f"\n=== {path}  conc={conc} total={total} ===")
    print(f"  wall            : {wall:.2f}s")
    print(f"  throughput      : {total / wall:.1f} req/s")
    print(f"  error rate      : {errs}/{total} = {100*errs/total:.1f}%")
    print(f"  status codes    : {codes}")
    print(f"  latency mean    : {statistics.mean(lat)*1000:.0f} ms")
    print(f"  latency p50     : {pct(50)*1000:.0f} ms")
    print(f"  latency p95     : {pct(95)*1000:.0f} ms")
    print(f"  latency p99     : {pct(99)*1000:.0f} ms")
    print(f"  latency max     : {max(lat)*1000:.0f} ms")
    return {"throughput": total / wall, "p95": pct(95), "err": errs, "codes": codes}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8140)
    ap.add_argument("--conc", type=int, default=50)
    ap.add_argument("--total", type=int, default=500)
    ap.add_argument("--path", default="/api/alerts")
    ap.add_argument("--pid", type=int, default=0)
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    # 워밍업(캐시 채우기 전/후 모두 보고 싶다면 cold 측정을 먼저)
    print("[cold] 캐시 비어있는 상태에서 동시 요청(스탬피드 관찰)")
    cold = run(base, args.path, args.conc, args.conc)  # conc 만큼만 동시에(콜드)

    print("\n[warm] 캐시 채워진 상태에서 본 부하")
    warm = run(base, args.path, args.conc, args.total)

    if args.pid:
        try:
            import psutil
            rss = psutil.Process(args.pid).memory_info().rss / 1e6
            print(f"\n  server RSS      : {rss:.0f} MB (pid {args.pid})")
        except Exception as e:
            print(f"  (psutil 측정 실패: {e})")


if __name__ == "__main__":
    main()
