# -*- coding: utf-8 -*-
"""keep-alive 핑 스크립트 (Render 무료티어 15분 슬립 방지용).

Render 무료 웹서비스는 15분간 요청이 없으면 슬립 상태로 내려가고, 다음 요청에서
콜드부팅(수십 초 지연)을 겪는다. 외부 크론이 주기적으로 경량 헬스 엔드포인트를
GET 하면 인스턴스가 깨어 있어 슬립을 막는다.

이 스크립트는 대상 URL 을 GET 해 (URL·상태코드·응답시각·소요ms) 를 로깅한다.
대상 URL 은 앱에 이미 있는 /api/health (경량 200) 를 기본으로 쓴다 — 신규
엔드포인트를 만들 필요가 없다.

대상 URL:
    환경변수 PING_URL (기본: http://127.0.0.1:8073/api/health)

모드:
    --dry-run   : 실제 요청 없이 무엇을 때릴지만 출력(안전 점검).
    --once      : 1회 GET 후 종료. 외부 크론이 "매 호출마다 프로세스 기동" 방식일
                  때/로컬 검증에 사용. (기본 모드)
    --loop      : --interval 초마다 무한 반복 GET(로컬 상주 검증용).
    --interval N: --loop 반복 주기 초(기본 600 = 10분. Render 15분 슬립 전 여유).

사용 예:
    python keepalive_ping.py --dry-run
    python keepalive_ping.py --once
    PING_URL=https://<앱>.onrender.com/api/health python keepalive_ping.py --once
    python keepalive_ping.py --loop --interval 600

======================= 외부 등록/배포 주의 (게이트) =======================
실제 keep-alive 는 이 스크립트를 로컬에서 상주시키는 게 아니라, 배포 URL 의
/api/health 를 외부 스케줄러(cron-job.org / UptimeRobot 등)가 5~10분 주기로
GET 하도록 "외부에 등록" 하는 것이다. 등록 방법(참고):
  - cron-job.org: 새 cronjob → URL=https://<앱>.onrender.com/api/health,
    schedule=*/10 * * * * (10분마다), method=GET.
  - UptimeRobot: HTTP(s) 모니터 → 위 URL, 인터벌 5분.
** 실제 외부 등록·Render 배포는 상위 게이트 승인 후에만 수행한다. 이 파일 자체는
   구현 + 로컬 검증(--dry-run/--once) 전용이며, 스크립트가 외부 등록을 하지 않는다. **
"""
import argparse
import os
import sys
import time
from datetime import datetime

import requests

DEFAULT_URL = "http://127.0.0.1:8073/api/health"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ping_once(url: str, timeout: float = 10.0) -> int:
    """대상 URL 1회 GET. (상태코드 반환, -1=요청실패). 로그 1줄 출력."""
    t0 = time.time()
    try:
        r = requests.get(url, timeout=timeout)
        ms = (time.time() - t0) * 1000
        print(f"[ping] {_now()} GET {url} -> {r.status_code} ({ms:.0f}ms)")
        return r.status_code
    except Exception as e:
        ms = (time.time() - t0) * 1000
        print(f"[ping] {_now()} GET {url} -> ERROR ({ms:.0f}ms): {e}")
        return -1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="keep-alive 핑(Render 슬립 방지)")
    ap.add_argument("--dry-run", action="store_true",
                    help="실제 요청 없이 대상만 출력")
    ap.add_argument("--once", action="store_true",
                    help="1회 GET 후 종료(기본)")
    ap.add_argument("--loop", action="store_true",
                    help="--interval 초마다 무한 반복 GET")
    ap.add_argument("--interval", type=int, default=600,
                    help="--loop 반복 주기 초(기본 600)")
    args = ap.parse_args(argv)

    url = os.getenv("PING_URL", DEFAULT_URL).strip() or DEFAULT_URL

    if args.dry_run:
        print(f"[ping] DRY-RUN {_now()} 대상 URL = {url} (요청 미전송)")
        print(f"[ping] DRY-RUN 모드=once, --loop 시 interval={args.interval}s")
        return 0

    if args.loop:
        print(f"[ping] LOOP 시작: {url} 매 {args.interval}s")
        while True:
            ping_once(url)
            time.sleep(max(1, args.interval))

    # 기본: 1회
    code = ping_once(url)
    # 200 이면 0, 아니면 1 (외부 크론/모니터가 성공여부 판단 가능)
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
