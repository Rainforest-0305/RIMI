# -*- coding: utf-8 -*-
"""공시알리미 오케스트레이션: 폴링 -> 요약·분류 -> 알림.

사용:
  python main.py --once      # 1회 폴링(신규만 알림) — 상주 없이 검증/크론용
  python main.py --demo      # seen 무시하고 최근 공시 몇 건 강제 요약·출력(작동증명)
  python main.py --loop      # config.POLL_INTERVAL_SEC 주기 상주 폴링
  python main.py --status    # 워치리스트/seen 상태 출력

중복방지: 처리한 rcept_no 를 data/seen.json 에 원자적 저장. 재실행해도
같은 공시를 다시 알리지 않는다.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import config
import dart_poll
from summarize import summarize
from notify_alert import send
try:  # X(트위터) 자동게시 — 추가 모듈. 기본 비활성(X_ENABLED/X_DRYRUN 없으면 no-op).
    import x_poster  # noqa: F401
except Exception:
    x_poster = None
try:  # 텔레그램 채널 자동발행 — 추가 모듈. 기본 비활성(TG_CHANNEL_ENABLED/TG_DRYRUN 없으면 no-op).
    import tg_channel  # noqa: F401
except Exception:
    tg_channel = None

sys.stdout.reconfigure(encoding="utf-8")


# ---------- seen 저장(원자적) ----------
def load_seen() -> set:
    if config.SEEN_FILE.exists():
        try:
            return set(json.loads(config.SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set):
    # rcept_no(YYYYMMDD+일련)는 사전식=시간순 → 최신 SEEN_MAX 개만 보존해
    # seen.json 무한증가 방지(rotation). 중복방지엔 최근분이면 충분.
    ordered = sorted(seen)
    cap = getattr(config, "SEEN_MAX", 5000)
    if cap and len(ordered) > cap:
        ordered = ordered[-cap:]          # 가장 최근(큰) rcept_no 유지
    tmp = config.SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(ordered), encoding="utf-8")
    os.replace(tmp, config.SEEN_FILE)  # 원자적 교체(패턴: 트레이딩 상태파일)


def load_watchlist():
    wl = json.loads(config.WATCHLIST_FILE.read_text(encoding="utf-8"))
    return wl.get("stocks", []), wl.get("keywords", [])


# ---------- 핵심 1회 폴링 ----------
def poll_once(mark_seen=True, force=False, limit_per_stock=20, verbose=True):
    """워치리스트 전체 1회 폴링. 신규(또는 force=True면 전부) 공시를 알림.
    반환: 처리(알림)한 건수."""
    stocks, keywords = load_watchlist()
    seen = load_seen()
    new_seen = set(seen)
    handled = 0

    for s in stocks:
        code = s["stock_code"]
        corp = dart_poll.resolve_corp(code)
        if not corp:
            if verbose:
                print(f"[skip] {s['name']}({code}) corp_code 해석 실패")
            continue
        items = dart_poll.fetch_disclosures(corp, page_count=limit_per_stock)
        if verbose:
            print(f"[poll] {s['name']}({code}) corp={corp}: {len(items)}건 조회")
        # DART는 최신순 반환 → 오래된 것부터 알리도록 역순 처리
        for item in reversed(items):
            rno = item.get("rcept_no", "")
            if not rno:
                continue
            if not force and rno in seen:
                continue
            item.setdefault("stock_code", code)
            result = summarize(item)
            send(item, result)
            if x_poster is not None:  # 기본 비활성·fail-open(핵심 폴링 무영향)
                x_poster.on_new_disclosure(item, result)
            if tg_channel is not None:  # 기본 비활성·fail-open(핵심 폴링 무영향)
                tg_channel.on_new_disclosure(item, result)
            handled += 1
            new_seen.add(rno)
        # 유량 배려: 종목 간 짧은 간격
        time.sleep(0.3)

    if mark_seen:
        save_seen(new_seen)
    return handled


# ---------- 데모: seen 무시하고 최근 N건 강제 요약(작동 증명) ----------
def demo(n=3):
    stocks, _ = load_watchlist()
    print(f"=== DEMO: 워치리스트 종목별 최근 공시 최대 {n}건 요약·분류·출력 ===\n")
    shown = 0
    for s in stocks:
        corp = dart_poll.resolve_corp(s["stock_code"])
        if not corp:
            continue
        items = dart_poll.fetch_disclosures(corp, page_count=n)
        for item in items[:n]:
            item.setdefault("stock_code", s["stock_code"])
            send(item, summarize(item))
            shown += 1
        time.sleep(0.3)
        if shown >= n:   # 데모는 총 n건이면 충분(부하 최소)
            break
    print(f"=== DEMO 완료: {shown}건 end-to-end 처리 ===")
    return shown


def status():
    stocks, keywords = load_watchlist()
    seen = load_seen()
    print(f"워치리스트: {len(stocks)}종목 {[s['name'] for s in stocks]}")
    print(f"추가 키워드: {keywords}")
    print(f"처리 완료(seen) rcept_no: {len(seen)}건")
    print(f"DART 키: {'있음' if config.DART_API_KEY else '없음'}")
    print(f"텔레그램 토큰: {'있음' if config.TELEGRAM_TOKEN else '없음'} / "
          f"테스트채널: {'설정됨' if config.TEST_CHAT_ID else '없음(콘솔폴백)'}")
    print(f"폴링주기: {config.POLL_INTERVAL_SEC}초")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1회 폴링(신규만 알림)")
    ap.add_argument("--demo", action="store_true", help="최근 공시 강제 요약·출력")
    ap.add_argument("--loop", action="store_true", help="주기 상주 폴링")
    ap.add_argument("--status", action="store_true", help="상태 출력")
    ap.add_argument("-n", type=int, default=3, help="demo 처리 건수")
    args = ap.parse_args()

    if args.status:
        status()
    elif args.demo:
        demo(args.n)
    elif args.loop:
        print(f"[loop] {config.POLL_INTERVAL_SEC}초 주기 폴링 시작 (Ctrl+C 중단)")
        while True:
            try:
                t = datetime.now().strftime("%H:%M:%S")
                h = poll_once()
                print(f"[{t}] 신규 {h}건 처리")
            except KeyboardInterrupt:
                print("중단됨")
                break
            except Exception as e:
                print(f"[loop] 오류(무시하고 계속): {e}")
            time.sleep(config.POLL_INTERVAL_SEC)
    else:  # 기본 = --once
        h = poll_once()
        print(f"신규 {h}건 처리 완료")


if __name__ == "__main__":
    main()
