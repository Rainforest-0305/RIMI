# -*- coding: utf-8 -*-
"""미리(MIRI) 공시앱 웹 API (FastAPI).

기존 백엔드 로직(dart_poll / summarize / impact / main)을 감싼다.
- GET  /api/alerts     : 코스피 시장 전체 최근 공시 피드(요약·태그·과거영향 포함)
- POST /api/poll       : 수동 새로고침(피드 캐시 무효화 후 실 DART 재조회)
- GET  /api/watchlist  : 관심종목·키워드 조회
- POST /api/watchlist  : 관심종목 추가(6자리 코드, corp_code 유효성 검증)
- DELETE /api/watchlist/{code} : 관심종목 삭제
- GET  /api/health     : 상태 점검

정적 프론트엔드(web/)를 루트에 마운트 → uvicorn 하나로 프론트+API 서빙.
텔레그램/알림 발송은 웹 API에서 건드리지 않는다(순수 조회·등록).

핵심: 피드는 관심목록 한정이 아니라 **시장 전체(corp_cls=Y, 코스피)** 를
list.json 단일 호출로 폴링한다. 유저는 아무 코스피 종목이나 관심등록 가능하며,
관심종목은 피드에서 강조/필터된다.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import dart_poll
import watch_store  # 관심종목 영속(Supabase/JSON 폴백) 추상 스토어
import push_store   # 웹푸시 구독 영속(Supabase/JSON 폴백) — watch_store 패턴
import dedup  # 중복 이벤트(결정/결과·정정/원본) 접기
import impact
import scale_extract  # 규모보정 온디맨드 조회(/api/scale)
from summarize import summarize
import main as core  # load_watchlist / load_seen 재사용

api = FastAPI(title="미리(MIRI) 공시앱 API", version="2.0")

# 응답 압축: /api/alerts(~460KB JSON)·index.html이 모바일 회선 병목 → gzip으로 5~8배 축소.
from fastapi.middleware.gzip import GZipMiddleware
api.add_middleware(GZipMiddleware, minimum_size=1500)

# ---------------- 피드 캐시(노트북/DART 유량 배려) ----------------
_FEED_CACHE = {"ts": 0.0, "data": None}
_FEED_TTL = 60.0  # 초. /api/poll 은 이 캐시를 강제 무효화한다.
_MARKET_DAYS = 7       # 최근 며칠 공시를 볼지(피드 창 확대: 3→7일)
_MARKET_PAGE = 100     # DART 페이지당 최대건(list.json 상한). 페이지네이션으로 전건 수집.
_MARKET_MAXPAGES = 5   # 시장별 최대 페이지(폭주 방어 상한: 시장당 최대 500건)
_MARKETS = ("Y", "K")  # KOSPI(Y) + KOSDAQ(K) 병합 폴링
# 숫자 bullet: /api/poll(force) 1회당 미캐시 신규건 DART 추출 상한(노트북/유량 배려).
# 초과분은 bullet 생략되며 다음 poll 에서 캐시로 채워진다.
_BULLET_PREFETCH_CAP = 12

# single-flight 락: 캐시가 콜드일 때 동시요청 N개가 각각 전체 재빌드(DART 호출)를
# 유발하는 캐시 스탬피드를 방지. 한 스레드만 재빌드하고 나머지는 결과를 공유한다.
_BUILD_LOCK = threading.Lock()

# ---------------- 백그라운드 불릿 워머(문제2: 배포 커버리지 수렴) ----------------
# 배포(Render)는 bench_cache 가 비어있고 FS ephemeral 이라 불릿 커버리지가 ~0.
# 모든 피드 빌드에서 'eligible 이나 이번 빌드 캐시전용 bullets 가 빈' 공시를
# 백그라운드 단일 스레드로 뒤에서 DART 추출→디스크 캐시에 채운다. 요청 응답은
# 지연 없이 즉시 반환되고, 다음 빌드/새로고침 때 캐시히트로 커버리지가 수렴한다.
# 기존 force 인라인 프리페치(cap 12)는 그대로 유지(이건 additive).
_WARM_QUEUE = []               # 처리 대기 dict: {rcept_no, code, report_nm, rcept_dt}
_WARM_SEEN = set()             # dedup: 이미 큐/처리중인 rcept_no
_WARM_LOCK = threading.Lock()  # 큐/상태 접근 보호
_WARM_THREAD = None            # 단일 워커 스레드 보장
_WARM_DAY = None               # 서킷브레이커 기준 날짜(YYYYMMDD)
_WARM_COUNT = 0                # 오늘 처리한 건수
_WARM_DAILY_CAP = 3000         # 일일 상한(DART 남용 방지). 초과 시 큐 비우고 중단.


def _warm_enqueue(items):
    """eligible 이나 bullets 가 빈 alert dict 리스트를 워머 큐에 넣고 워커를 깨운다.

    fire-and-forget: 절대 요청 스레드를 블록하지 않는다. 큐/상태 접근만 락으로 감싼다.
    """
    global _WARM_THREAD
    if not items:
        return
    with _WARM_LOCK:
        for a in items:
            rno = (a.get("rcept_no") or "").strip()
            if not rno or rno in _WARM_SEEN:
                continue
            _WARM_SEEN.add(rno)
            _WARM_QUEUE.append({
                "rcept_no": rno,
                "code": (a.get("stock_code") or "").strip(),
                "report_nm": (a.get("report_nm") or "").strip(),
                "rcept_dt": (a.get("rcept_dt") or "").strip(),
            })
        need_worker = (_WARM_THREAD is None) or (not _WARM_THREAD.is_alive())
        if _WARM_QUEUE and need_worker:
            _WARM_THREAD = threading.Thread(
                target=_warm_worker, name="bullet-warmer", daemon=True)
            _WARM_THREAD.start()


def _warm_worker():
    """큐를 하나씩 비우며 bullets_for_item(allow_fetch=True)로 디스크 캐시를 채운다.

    - 큐 비면 종료(재기동은 다음 _warm_enqueue 가 담당).
    - 일일 서킷브레이커 초과면 큐 비우고 종료.
    - 성공/예외 무관 count++ 후 상한 체크. 예외는 swallow+print. sleep 0.15 레이트리밋.
    """
    global _WARM_COUNT, _WARM_DAY, _WARM_THREAD
    while True:
        with _WARM_LOCK:
            # 날짜 바뀌면 서킷브레이커 리셋
            today = datetime.now().strftime("%Y%m%d")
            if _WARM_DAY != today:
                _WARM_DAY = today
                _WARM_COUNT = 0
            # 서킷브레이커: 큐 비우고 종료
            if _WARM_COUNT >= _WARM_DAILY_CAP:
                _WARM_QUEUE.clear()
                # 종료 전 스레드 슬롯 해제(레이스 방지): 다음 enqueue 가 재기동을
                # is_alive 타이밍이 아니라 None 검사로 authoritative 하게 판단.
                _WARM_THREAD = None
                return
            if not _WARM_QUEUE:
                _WARM_THREAD = None
                return
            job = _WARM_QUEUE.pop(0)

        rno = job["rcept_no"]
        try:
            code = job["code"]
            ccode = dart_poll.resolve_corp(code) or "" if code else ""
            # corp_code 없이도 doc-route(공급계약·배당·소각)는 rcept_no 로 처리됨.
            scale_extract.bullets_for_item(
                ccode, code, job["report_nm"], rno, job["rcept_dt"],
                allow_fetch=True, budget=[999], known_files=None)
        except Exception as e:
            print(f"[warm] skip {rno}: {e}")
        finally:
            with _WARM_LOCK:
                _WARM_COUNT += 1
                # 처리한 rcept 는 SEEN 에서 제거: 재요청 시 캐시히트라 재추출 안 함(안전).
                _WARM_SEEN.discard(rno)
        time.sleep(0.15)  # DART 레이트리밋


# ---------------- 웹푸시(관심종목 신규 공시 알림) ----------------
# VAPID 키는 .env(하드코딩 0, os.getenv 만). 미설정이면 푸시 기능 전체 no-op
# (엔드포인트는 200 으로 살아있되 key='' → 프론트가 우아하게 토글 비활성).
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUB = os.getenv("VAPID_SUB", "mailto:urimk0305@gmail.com").strip()
_PUSH_ENABLED = bool(VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY)

# 발송 dedup / 재시작 스팸방지(baseline-seed):
#   - 프로세스 최초 피드빌드는 '현 시점 전체 공시'를 baseline 으로 흡수만 하고 발송 0
#     (재배포/재시작 때 최근 7일치가 통째로 재발송되는 스팸을 원천 차단).
#   - 이후 빌드에서 '처음 관측된 rcept_no' 만 신규로 감지 → 관심 등록 기기에만 발송.
#   - 기기당 동일 rcept 1회(_PUSH_SENT). 관심종목만, 시장 브로드캐스트 금지.
_PUSH_LOCK = threading.Lock()
_PUSH_SEEN_RCEPTS = set()   # 지금까지 관측한 모든 rcept_no(전역 dedup)
_PUSH_SENT = set()          # (device_id, rcept_no) 발송완료(기기당 1회 보장)
_PUSH_BASELINE_DONE = False


def _push_dispatch(items):
    """피드빌드 결과에서 신규 관심공시를 감지해 발송(fire-and-forget).

    동기 구간은 dedup 집합 갱신(네트워크 0)만. 실제 발송(구독조회+HTTP)은 별
    스레드로 던져 요청/빌드 지연 0. 예외는 삼켜 빌드를 절대 깨지 않는다."""
    if not _PUSH_ENABLED:
        return
    global _PUSH_BASELINE_DONE
    new = []
    with _PUSH_LOCK:
        for a in items:
            rno = (a.get("rcept_no") or "").strip()
            if not rno:
                continue
            if rno not in _PUSH_SEEN_RCEPTS:
                _PUSH_SEEN_RCEPTS.add(rno)
                new.append({
                    "rcept_no": rno,
                    "stock_code": (a.get("stock_code") or "").strip(),
                    "corp_name": a.get("corp_name") or "",
                    "report_nm": a.get("report_nm") or "",
                })
        if not _PUSH_BASELINE_DONE:
            _PUSH_BASELINE_DONE = True   # 최초 빌드: 흡수만, 발송 없음
            return
    if not new:
        return
    threading.Thread(target=_push_send_new, args=(new,),
                     name="push-sender", daemon=True).start()


def _push_send_new(new):
    """신규 관심공시를 구독 기기에 발송. 구독 있는 기기만 관심목록 조회(작업 최소화).

    - 발송 실패 410/404(만료/해지) 구독은 endpoint 로 자동 정리.
    - 기기당 동일 rcept 1회(_PUSH_SENT). 예외 전방위 격리(발송 실패가 서버 무영향)."""
    try:
        subs = push_store.all_subs()
    except Exception as e:
        print(f"[push] 구독 조회 실패(무시): {type(e).__name__}")
        return
    if not subs:
        return
    by_dev = {}
    for s in subs:
        by_dev.setdefault(s.get("device_id") or "", []).append(s)
    for dev, dsubs in by_dev.items():
        if not dev:
            continue
        try:
            st = watch_store.load_watch_state(dev)
            codes = {str(x.get("stock_code"))
                     for x in (st.get("stocks") or []) if x.get("stock_code")}
        except Exception as e:
            print(f"[push] 관심목록 조회 실패(dev 스킵): {type(e).__name__}")
            continue
        if not codes:
            continue
        for item in new:
            code = str(item.get("stock_code") or "")
            if not code or code not in codes:
                continue
            key = (dev, item["rcept_no"])
            with _PUSH_LOCK:
                if key in _PUSH_SENT:
                    continue
                _PUSH_SENT.add(key)
            title = (item.get("corp_name") or "관심종목").strip()
            report = (item.get("report_nm") or "새 공시").strip()
            payload = {
                "title": f"{title} · {report}"[:120],
                "body": "관심종목 새 공시 · 탭하여 MIRI에서 확인",
                "url": "/",                      # 클릭 시 앱 열기(외부 링크 아님)
                "rcept": item["rcept_no"],
            }
            for sub in dsubs:
                _push_one(sub, payload)


def _push_one(sub_row, payload):
    """단건 발송. pywebpush 는 지연 import(미설치 환경도 서버 기동 무붕괴)."""
    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        print(f"[push] pywebpush 미설치(발송 불가): {type(e).__name__}")
        return
    try:
        webpush(
            subscription_info=sub_row["sub"],
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUB},
            timeout=10,
        )
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            # 만료/해지 구독 자동정리(레포트만, 상세 노출 금지)
            try:
                push_store.delete_endpoint(sub_row.get("endpoint") or "")
            except Exception:
                pass
            print(f"[push] 만료구독 정리(status={status})")
        else:
            print(f"[push] 발송 실패(status={status})")
    except Exception as e:
        print(f"[push] 발송 예외(무시): {type(e).__name__}")


def _fmt_date(rcept_dt: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD (표시용). 실패 시 원본."""
    s = (rcept_dt or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


# 실제 주가 영향 테마만 노출(지분변동·소유상황·대량보유·정정단독·기타공시 = 노이즈로 제외)
IMPACT_TAGS = {"유상증자", "무상증자", "전환사채", "자사주", "최대주주변경",
               "주식소각", "배당", "실적", "합병분할", "공급계약",
               "소송", "감사보고서", "임상"}

# ---------------- WS-32A 레짐 영향분포(과거 시장국면별 참고정보) ----------------
# 아키텍처 제약(CTO): 배포 런타임 시장데이터 의존 0. by_regime 은 '현재 시장이
# 어느 레짐인지' 자동판정에 절대 쓰지 않는다(그건 069500/229200 종가 조회 필요 →
# 패리티 위반). 순수하게 '유형별 과거 레짐별 영향분포'만 참고정보로 표시한다.
# 한글 라벨은 _meta.regime_axis.proposed_labels_ko 단일 매핑으로만(하드코딩 금지).
# labels_status 가 미확정이라 잠정 라벨임을 UI 에 반영한다.
_REGIME_WK = {"d": "d1", "w": "w1", "m": "m1"}  # by_regime 창키 → 프론트 창키


def _regime_block(tags):
    """공시 태그 → 유형별 과거 레짐별 영향분포(by_regime) 표시블록.

    impact_for_tags 가 매칭한 것과 동일한 유형 엔트리의 by_regime 를 읽어,
    _meta.regime_axis.proposed_labels_ko 로 한글 라벨을 붙여 정규화한다.
    - 현재레짐 판정 없음(런타임 시장데이터 0). 순수 과거 참고정보.
    - 소표본 셀: 데이터가 이미 n<30 평균 미노출·n<5 생략으로 직렬화됨. 여기선
      셀을 그대로 통과시키고(평균 None 가능), 프론트가 '표본부족'/스킵 처리.
    - by_regime 없으면(신유형 B 머지 전 등) None 반환 → 프론트 우아하게 스킵.
    항상 dict|None(에러 없음)."""
    try:
        bench = impact.load_benchmark()
        types, _, _ = impact._types_map(bench)
        if not types:
            return None
        entry = None
        for t in (tags or []):
            if t in types:
                entry = types[t]
                break
        if not isinstance(entry, dict):
            return None
        by_regime = entry.get("by_regime")
        if not isinstance(by_regime, dict) or not by_regime:
            return None

        axis = ((bench.get("_meta") or {}).get("regime_axis") or {})
        labels = axis.get("proposed_labels_ko") or {}          # 단일 매핑(하드코딩 금지)
        order = axis.get("internal_keys") or ["bull", "neutral", "crash"]
        status = str(axis.get("labels_status") or "")
        provisional = ("미확정" in status) or status.startswith("제안")

        regimes = []
        for rk in order:
            cell = by_regime.get(rk)
            if not isinstance(cell, dict):
                continue
            windows = {}
            for wk, outk in _REGIME_WK.items():
                wd = cell.get(wk)
                if not isinstance(wd, dict):
                    continue
                raw_up = wd.get("raw_up_prob")
                car_up = wd.get("up_prob")
                windows[outk] = {
                    "raw_avg": wd.get("raw_avg"),
                    "raw_med": wd.get("raw_med"),
                    "car_avg": wd.get("car_avg"),
                    "raw_up_prob": raw_up,
                    "car_up_prob": car_up,
                    "up_prob": raw_up if raw_up is not None else car_up,
                    "n": wd.get("n"),
                }
            if not windows:
                continue
            regimes.append({
                "key": rk,
                # 라벨은 매핑에서만. neutral 은 데이터가 '중립/보합'(절대 '약세' 아님).
                "label": labels.get(rk, rk),
                "windows": windows,
            })
        if not regimes:
            return None
        return {
            "regimes": regimes,
            "provisional": provisional,   # 잠정 라벨(미확정) UI 반영용
            "note": "유형별 과거 시장국면 영향분포(참고). 현재 시장국면 판정 아님.",
        }
    except Exception as e:
        print(f"[regime] skip: {e}")
        return None


def _attach_regime(imp: dict, tags) -> dict:
    """impact 블록에 regime(과거 레짐 영향분포)를 순수 추가(무손상). status!=ok
    또는 by_regime 없으면 원본 그대로 반환(기존 응답 1바이트도 안 깬다)."""
    if not isinstance(imp, dict) or imp.get("status") != "ok":
        return imp
    reg = _regime_block(tags)
    if not reg:
        return imp
    out = dict(imp)
    out["regime"] = reg
    return out


def _build_feed(force: bool = False) -> dict:
    """KOSPI+KOSDAQ 시장 전체 최근 공시를 조회·요약·과거영향 매핑해 피드로 만든다.
    개별 공시 하나가 malformed 여도 그 항목만 건너뛰고 피드 전체는 살린다.

    피드는 전역 캐시(단일 스냅샷)라 특정 기기의 관심상태를 절대 담지 않는다.
    is_watched(★·강조·상단정렬)는 기기별로 다르므로 프론트가 자기 기기의
    /api/watchlist 로 계산한다 → 서버 피드캐시 오염 방지."""
    seen = core.load_seen()

    # KOSPI(Y)+KOSDAQ(K) 페이지네이션 병합. errors 는 시장별 실패 사유.
    raw, fetch_errors = dart_poll.fetch_markets(
        days=_MARKET_DAYS, markets=_MARKETS,
        page_count=_MARKET_PAGE, max_pages=_MARKET_MAXPAGES)

    bench_ready = impact.has_stats()          # 버그 B 수정: 실스키마도 정확 판정

    # 숫자 bullet 준비: AMT 캐시 파일목록 1회 스캔(멤버십 검사로 디렉토리 재스캔 회피).
    # 캐시 조회는 DART 0콜. force(=/api/poll) 일 때만 미캐시 신규건을 상한만큼 추출.
    try:
        amt_files = set(os.listdir(scale_extract.AMT_CACHE))
    except Exception:
        amt_files = set()
    bullet_budget = [_BULLET_PREFETCH_CAP if force else 0]

    items = []
    for it in raw:
        try:
            if not isinstance(it, dict):
                continue
            code = (it.get("stock_code") or "").strip()
            it.setdefault("stock_code", code)
            res = summarize(it)
            if not (set(res["tags"]) & IMPACT_TAGS):
                continue  # 노이즈 공시 제외(소유상황·대량보유·정정단독·기타)
            rno = (it.get("rcept_no") or "").strip()
            cls = (it.get("corp_cls") or "").strip()
            report_nm = (it.get("report_nm", "") or "").strip()
            # 숫자 bullet: 규모보정 대상 유형만(그 외 route=None → [] 즉시반환, IO 없음).
            # 캐시 우선(DART 0콜); force+예산 남을 때만 미캐시 신규건 1콜 추출.
            bullets = []
            try:
                if scale_extract.bullet_eligible(report_nm):
                    ccode = dart_poll.resolve_corp(code) or "" if code else ""
                    bullets = scale_extract.bullets_for_item(
                        ccode, code, report_nm, rno, it.get("rcept_dt", ""),
                        allow_fetch=force, budget=bullet_budget,
                        known_files=amt_files)
            except Exception:
                bullets = []
            items.append({
                "rcept_no": rno,
                "corp_name": it.get("corp_name", ""),
                "stock_code": code,
                "corp_cls": cls,
                "market": dart_poll.market_label(cls),   # KOSPI/KOSDAQ 라벨
                "report_nm": report_nm,
                "flr_nm": it.get("flr_nm", ""),
                "rcept_dt": it.get("rcept_dt", ""),
                "date": _fmt_date(it.get("rcept_dt", "")),
                "rm": it.get("rm", ""),
                "tags": res["tags"],
                "summary": res["summary"],
                "bullets": bullets,
                # 규모(scale) 대상 = bullet 대상과 동일(금액추출 가능 전 유형).
                # 프론트는 이 플래그로 '📏 규모로 보기' 버튼 노출 → 두 목록 자동 일치.
                "scale_eligible": scale_extract.bullet_eligible(report_nm),
                "impact": _attach_regime(impact.impact_for_tags(res["tags"]),
                                         res["tags"]),
                "url": dart_poll.dart_url(rno),
                "is_new": rno not in seen,
                # is_watched 는 기기별 → 프론트가 계산. 전역 피드엔 항상 False.
                "is_watched": False,
            })
        except Exception as e:
            # malformed 공시 1건이 피드 전체를 깨지 못하게 격리(로그만).
            print(f"[feed] skip malformed item {it.get('rcept_no','?') if isinstance(it, dict) else '?'}: {e}")
            continue

    # 중복 이벤트 dedup: 같은 기업의 사실상 같은 사건(결정↔결과, 정정↔원본, 부수공시)
    # 을 묶어 정보량 큰 대표 1건만 남긴다(규칙: dedup.py). 정렬 전에 접는다.
    items = dedup.dedup(items)

    # 정렬: 최신순(접수일+접수번호 desc). 관심종목 상단정렬은 기기별이라 프론트가
    # 자기 기기 관심목록으로 재정렬한다(전역 피드는 관심상태 무관 = 캐시 공유 안전).
    items.sort(key=lambda x: (x.get("rcept_dt", ""), x.get("rcept_no", "")),
               reverse=True)

    errors = list(fetch_errors)
    if not raw and fetch_errors:
        errors.append("DART 시장 공시 조회 실패(유량/키/네트워크). 잠시 후 새로고침.")

    # 백그라운드 워머: eligible 인데 이번 빌드 캐시전용 bullets 가 빈 건을 뒤에서 채운다.
    # fire-and-forget(요청 응답 무지연). 워머 실패가 빌드를 깨지 않게 격리.
    try:
        _warm_enqueue([a for a in items
                       if a.get("scale_eligible") and not a.get("bullets")])
    except Exception as e:
        print(f"[warm] enqueue skip: {e}")

    # 웹푸시: 신규 관심공시 감지→발송(fire-and-forget). 발송 실패가 빌드를 안 깬다.
    try:
        _push_dispatch(items)
    except Exception as e:
        print(f"[push] dispatch skip: {e}")

    return {
        "count": len(items),
        "market": "KOSPI+KOSDAQ",
        # 관심목록은 기기별 → 전역 피드 payload 에 담지 않는다(타 기기 유출 방지).
        # 프론트는 /api/watchlist(기기 스코프)로 관심상태를 얻는다.
        "stocks": [],
        "keywords": [],
        "benchmark_ready": bench_ready,
        "benchmark_source": impact.benchmark_source(),
        "errors": errors,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "alerts": items,
    }


def _cache_fresh(now: float) -> bool:
    return (_FEED_CACHE["data"] is not None
            and (now - _FEED_CACHE["ts"]) < _FEED_TTL)


def _get_feed(force: bool = False) -> dict:
    # 1) 락 없이 캐시 히트 빠른경로(대부분의 warm 요청)
    now = time.time()
    if (not force) and _cache_fresh(now):
        cached = dict(_FEED_CACHE["data"])
        cached["cached"] = True
        return cached

    # 2) 콜드/만료/force: single-flight. 락을 잡은 한 스레드만 재빌드하고
    #    락을 기다리던 스레드들은 그 사이 채워진 캐시를 재사용(스탬피드 방지).
    with _BUILD_LOCK:
        now = time.time()
        if (not force) and _cache_fresh(now):
            cached = dict(_FEED_CACHE["data"])
            cached["cached"] = True
            return cached
        data = _build_feed(force=force)
        _FEED_CACHE["ts"] = time.time()
        _FEED_CACHE["data"] = data
        out = dict(data)
        out["cached"] = False
        return out


# ---------------- 콜드스타트 프리웜(startup) ----------------
# Render 콜드부팅/최초 요청 시 첫 /api/alerts 가 KOSPI+KOSDAQ 전체 폴링을 인라인으로
# 돌아 수 초 지연된다. startup 에서 데몬 스레드로 _get_feed(force=True) 를 1회 돌려
# _FEED_CACHE 를 미리 채운다. startup 자체는 스레드를 fire-and-forget 으로 띄우고
# 즉시 반환하므로 uvicorn 기동을 절대 블록/지연시키지 않는다. 예외는 swallow+print
# (기동을 깨지 않음). _build_feed 가 내부에서 _warm_enqueue 를 부르므로 별도 bullet
# 워머 startup 을 만들지 않는다(프리웜 1회 build 로 워머가 자연 기동 = 중복 없음).
_PREWARM_DONE = False   # 관측용 완료 플래그(/api/health 에 노출, 측정 시 완료시점 판정)
# GONGSI_PREWARM=0/false 면 프리웜 비활성(콜드빌드 경로 유지 = before 측정용).
_PREWARM_ENABLED = os.getenv("GONGSI_PREWARM", "1").strip().lower() not in ("0", "false", "no", "")


def _prewarm():
    """백그라운드 데몬: _get_feed(force=True) 로 피드캐시를 미리 채운다.
    예외는 swallow+print. 완료 시 _PREWARM_DONE=True(관측용)."""
    global _PREWARM_DONE
    t0 = time.time()
    try:
        data = _get_feed(force=True)
        print(f"[prewarm] feed cache 채움: alerts={data.get('count')} "
              f"in {(time.time() - t0) * 1000:.0f}ms")
    except Exception as e:
        print(f"[prewarm] 실패(무시, 기동 유지): {e}")
    finally:
        _PREWARM_DONE = True


@api.on_event("startup")
def _startup_prewarm():
    """uvicorn 기동 직후 호출. 프리웜 스레드만 띄우고 즉시 반환(기동 무지연)."""
    if not _PREWARM_ENABLED:
        print("[prewarm] 비활성(GONGSI_PREWARM=0) — 콜드빌드 경로 유지")
        return
    threading.Thread(target=_prewarm, name="feed-prewarm", daemon=True).start()
    print("[prewarm] 백그라운드 프리웜 스레드 기동(startup 즉시 반환)")


# ---------------- 워치리스트 스냅샷 헬퍼 ----------------
def _snapshot(state, ok=True):
    """모든 변이 응답의 공통 형태: 전체 스냅샷."""
    return {
        "ok": ok,
        "stocks": state.get("stocks", []),
        "keywords": state.get("keywords", []),
        "groups": state.get("groups", []),
    }


def _group_ids(state):
    return {g["id"] for g in state.get("groups", [])}


def _device_id(request: Request) -> str:
    """요청의 X-Device-Id 헤더(기기 익명 ID). 미제공이면 빈 문자열.

    watch_store 는 빈 device_id 를 '임시 빈 상태(미영속)'로 취급하므로, 헤더 없는
    비프론트 호출도 에러 없이 빈 관심목록을 받는다(전역 공유 결함 제거)."""
    return (request.headers.get("x-device-id") or "").strip()


# ---------------- 엔드포인트 ----------------
@api.get("/api/health")
def health():
    return {
        "ok": True,
        "dart_key": bool(config.DART_API_KEY),
        "watchlist_count": len(core.load_watchlist()[0]),
        "seen_count": len(core.load_seen()),
        "benchmark_ready": impact.has_stats(),   # 버그 B: 실스키마도 정확 판정
        "poll_interval_sec": config.POLL_INTERVAL_SEC,
        "prewarm_enabled": _PREWARM_ENABLED,     # 콜드스타트 프리웜 활성 여부
        "prewarm_done": _PREWARM_DONE,           # 프리웜 완료(피드캐시 채워짐) 여부
        "feed_cached": _FEED_CACHE["data"] is not None,  # 현재 피드캐시 보유 여부
        "watch_backend": watch_store.backend_name(),  # 관심종목 영속 백엔드(supabase/json)
        "push_enabled": _PUSH_ENABLED,                 # VAPID 설정(웹푸시 활성) 여부
        "push_backend": push_store.backend_name(),     # 구독 영속 백엔드(supabase/json)
    }


@api.get("/api/alerts")
def get_alerts():
    return JSONResponse(_get_feed(force=False))


@api.post("/api/poll")
def post_poll():
    """수동 새로고침: 캐시 무효화 후 실 DART 재조회."""
    return JSONResponse(_get_feed(force=True))


@api.get("/api/scale")
def get_scale(rcept: str, code: str = "", report_nm: str = "",
              corp: str = "", dt: str = ""):
    """온디맨드 규모보정: 공시 1건의 상대규모(금액/시총)로 (유형×규모버킷) 통계 반환.

    성능 안전: 피드 빌드와 무관한 **탭 시에만** 호출되는 단건 경로. DART 는 과거
    사건이면 배치 캐시로 0콜, 신규 사건이면 접수일 근방 1콜만 사용. 실패/미지원은
    status 로 폴백 신호(프론트는 유형단위 통계 유지). 예외에도 500 대신 dict 반환.
    """
    rcept = (rcept or "").strip()
    if not rcept:
        raise HTTPException(status_code=400, detail="rcept(접수번호) 필수")
    code = (code or "").strip()
    corp_code = (corp or "").strip()
    if not corp_code and code:
        try:
            corp_code = dart_poll.resolve_corp(code) or ""   # 캐시된 corp_map(DART 0콜)
        except Exception:
            corp_code = ""
    try:
        res = scale_extract.scale_lookup(rcept, corp_code, code,
                                         report_nm or "", dt or None)
    except Exception as e:
        res = {"status": "error", "reason": str(e)[:150]}
    return JSONResponse(res)


@api.get("/api/watchlist")
def get_watchlist(request: Request):
    state = watch_store.load_watch_state(_device_id(request))
    return {"stocks": state["stocks"], "keywords": state["keywords"],
            "groups": state["groups"]}


class WatchAdd(BaseModel):
    name: str | None = None
    stock_code: str | None = None
    group: str | None = None


@api.post("/api/watchlist")
def add_watchlist(body: WatchAdd, request: Request):
    raw = (body.stock_code or body.name or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="종목명 또는 종목코드를 입력하세요.")

    device_id = _device_id(request)
    state = watch_store.load_watch_state(device_id)
    stocks = state["stocks"]

    # 대상 그룹 결정(미지정 → default). 존재하지 않는 그룹이면 400.
    group = (body.group or watch_store.DEFAULT_GROUP_ID).strip() \
        or watch_store.DEFAULT_GROUP_ID
    if group not in _group_ids(state):
        raise HTTPException(status_code=400,
                            detail=f"존재하지 않는 그룹입니다: {group}")

    name = (body.name or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 6:
        code = digits
        if not name or name == raw:
            name = raw if raw != digits else ""
    else:
        raise HTTPException(
            status_code=400,
            detail="6자리 종목코드로 등록하세요. 예: 005930 (삼성전자)")

    # corp_code 로 실제 유효성 검증(코스피/코스닥 무관 존재 확인)
    corp = dart_poll.resolve_corp(code)
    if not corp:
        raise HTTPException(status_code=404,
                            detail=f"종목코드 {code} 를 DART에서 찾을 수 없습니다.")

    for s in stocks:
        if s.get("stock_code") == code:
            raise HTTPException(status_code=409,
                                detail=f"이미 등록된 종목입니다: {s.get('name')} ({code})")

    if not name:
        try:
            recent = dart_poll.fetch_disclosures(corp, page_count=1)
            if recent:
                name = recent[0].get("corp_name", "").strip()
        except Exception:
            pass

    # 그룹 말미에 추가(order = 그룹 내 최대+1; 저장 시 정규화로 0..n 재부여)
    order = max([s["order"] for s in stocks if s["group"] == group],
                default=-1) + 1
    stocks.append({"name": name or code, "stock_code": code,
                   "group": group, "order": order})
    state = watch_store.save_watch_state(state, device_id)
    # 피드는 이제 기기 관심상태와 무관(is_watched 프론트 계산) → 캐시 무효화 불요.
    return _snapshot(state)


@api.delete("/api/watchlist/{code}")
def delete_watchlist(code: str, request: Request):
    device_id = _device_id(request)
    state = watch_store.load_watch_state(device_id)
    new_stocks = [s for s in state["stocks"] if s.get("stock_code") != code]
    if len(new_stocks) == len(state["stocks"]):
        # 멱등 삭제: 이미 빠진 종목에 해제 요청이 와도 404 대신 현 상태 반환.
        return _snapshot(state)
    state["stocks"] = new_stocks
    state = watch_store.save_watch_state(state, device_id)
    return _snapshot(state)


class StockPatch(BaseModel):
    group: str | None = None
    order: int | None = None


@api.patch("/api/watchlist/{code}")
def patch_watchlist(code: str, body: StockPatch, request: Request):
    """종목 그룹이동 / 순서변경."""
    device_id = _device_id(request)
    state = watch_store.load_watch_state(device_id)
    target = next((s for s in state["stocks"]
                   if s.get("stock_code") == code), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"등록되지 않은 종목: {code}")

    if body.group is not None:
        grp = body.group.strip()
        if grp not in _group_ids(state):
            raise HTTPException(status_code=404,
                                detail=f"존재하지 않는 그룹입니다: {grp}")
        target["group"] = grp
    if body.order is not None:
        target["order"] = body.order

    state = watch_store.save_watch_state(state, device_id)
    return _snapshot(state)


class OrderPut(BaseModel):
    group: str | None = None
    order: list[str] | None = None


@api.put("/api/watchlist/order")
def reorder_watchlist(body: OrderPut, request: Request):
    """해당 그룹 내 드래그 벌크 재정렬. order=[code, ...] 순서대로 재부여."""
    group = (body.group or "").strip()
    if not group:
        raise HTTPException(status_code=400, detail="group 을 지정하세요.")
    device_id = _device_id(request)
    state = watch_store.load_watch_state(device_id)
    if group not in _group_ids(state):
        raise HTTPException(status_code=404,
                            detail=f"존재하지 않는 그룹입니다: {group}")
    order_list = body.order or []
    rank = {code: i for i, code in enumerate(order_list)}
    # 지정된 순서 먼저, 미지정 종목은 뒤로(기존 order 유지). 저장 시 0..n 정규화.
    base = len(order_list)
    for s in state["stocks"]:
        if s["group"] == group:
            s["order"] = rank.get(s["stock_code"], base + s["order"])
    state = watch_store.save_watch_state(state, device_id)
    return _snapshot(state)


# ---------------- 그룹 관리 ----------------
class GroupCreate(BaseModel):
    name: str | None = None


class GroupPatch(BaseModel):
    name: str | None = None
    order: int | None = None


def _new_group_id(state):
    import uuid
    existing = _group_ids(state)
    while True:
        gid = "g_" + uuid.uuid4().hex[:8]
        if gid not in existing:
            return gid


@api.post("/api/watchlist/groups")
def create_group(body: GroupCreate, request: Request):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="그룹 이름을 입력하세요.")
    device_id = _device_id(request)
    state = watch_store.load_watch_state(device_id)
    if any(g["name"] == name for g in state["groups"]):
        raise HTTPException(status_code=409,
                            detail=f"이미 존재하는 그룹 이름입니다: {name}")
    order = max([g["order"] for g in state["groups"]], default=-1) + 1
    state["groups"].append({"id": _new_group_id(state),
                            "name": name, "order": order})
    state = watch_store.save_watch_state(state, device_id)
    return _snapshot(state)


@api.patch("/api/watchlist/groups/{gid}")
def patch_group(gid: str, body: GroupPatch, request: Request):
    """그룹 이름변경 / 순서변경. default 도 이름/순서변경은 허용(삭제만 금지)."""
    device_id = _device_id(request)
    state = watch_store.load_watch_state(device_id)
    target = next((g for g in state["groups"] if g["id"] == gid), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"존재하지 않는 그룹: {gid}")

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="그룹 이름은 비울 수 없습니다.")
        if any(g["name"] == name and g["id"] != gid for g in state["groups"]):
            raise HTTPException(status_code=409,
                                detail=f"이미 존재하는 그룹 이름입니다: {name}")
        target["name"] = name
    if body.order is not None:
        target["order"] = body.order

    state = watch_store.save_watch_state(state, device_id)
    return _snapshot(state)


@api.delete("/api/watchlist/groups/{gid}")
def delete_group(gid: str, request: Request):
    """그룹 삭제. 소속 종목은 default 로 이동. default 삭제는 400."""
    if gid == watch_store.DEFAULT_GROUP_ID:
        raise HTTPException(status_code=400, detail="기본 그룹은 삭제할 수 없습니다.")
    device_id = _device_id(request)
    state = watch_store.load_watch_state(device_id)
    if not any(g["id"] == gid for g in state["groups"]):
        raise HTTPException(status_code=404, detail=f"존재하지 않는 그룹: {gid}")
    state["groups"] = [g for g in state["groups"] if g["id"] != gid]
    for s in state["stocks"]:
        if s["group"] == gid:
            s["group"] = watch_store.DEFAULT_GROUP_ID
    state = watch_store.save_watch_state(state, device_id)
    return _snapshot(state)


# ---------------- 종목 검색 (로컬 인덱스, DART 0콜) ----------------
# corp_index.json = 빌드타임(build_corp_index.py)에 1회 생성한 상장종목 인덱스.
# 리스트 형식: [{"code":"005930","name":"삼성전자","market":"-"}, ...]
# 런타임 검색은 이 파일만 메모리에 1회 로드해 쓰며 DART/네트워크를 절대 호출하지 않는다.
_CORP_INDEX_FILE = config.DATA / "corp_index.json"
_CORP_INDEX_CACHE = None  # 지연 로드 후 리스트 캐시(모듈 수명 동안 재사용)


def _load_corp_index():
    """corp_index.json 을 1회 로드해 캐시. 파일없음/파싱실패 시 빈 리스트로 graceful.
    반환 항목은 검색에 쓰기 좋게 code/name(과 name_lower) 정규화."""
    global _CORP_INDEX_CACHE
    if _CORP_INDEX_CACHE is not None:
        return _CORP_INDEX_CACHE
    rows = []
    try:
        raw = json.loads(_CORP_INDEX_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for r in raw:
                if not isinstance(r, dict):
                    continue
                code = str(r.get("code") or "").strip()
                name = str(r.get("name") or "").strip()
                if not code and not name:
                    continue
                market = str(r.get("market") or "-").strip() or "-"
                rows.append({
                    "code": code,
                    "name": name,
                    "market": market,
                    "_nl": name.lower(),  # 대소문자 무시 매칭용(영문 종목명)
                })
    except FileNotFoundError:
        print(f"[search] corp_index 없음: {_CORP_INDEX_FILE} (검색 빈결과 폴백)")
    except Exception as e:
        print(f"[search] corp_index 로드 실패: {e} (검색 빈결과 폴백)")
    _CORP_INDEX_CACHE = rows
    return rows


# 모듈 로드시 1회 로드(전역 캐시 워밍업). 실패해도 위 폴백으로 크래시 없음.
_load_corp_index()

_SEARCH_LIMIT = 30  # 결과 상한(프론트 표시용)


@api.get("/api/search")
def search(q: str = ""):
    """로컬 인덱스 기반 종목 검색(DART 0콜).

    - q strip. 빈 q -> 200 {"query":"","count":0,"results":[]}.
    - 매칭: q in name  또는  q in code (부분일치). 영문명은 대소문자 무시.
    - 관련도: ①정확일치 > ②이름 접두 > ③코드 접두 > ④부분일치.
      동순위는 시총 부재로 KOSPI 우선 + 종목코드 오름차순으로 대체 정렬.
    - 상한 30건. count = 반환 results 길이. 어떤 입력에도 500 금지(예외는 빈결과 폴백).
    """
    try:
        query = (q or "").strip()
        if not query:
            return {"query": "", "count": 0, "results": []}

        ql = query.lower()
        index = _load_corp_index()

        _MK_RANK = {"KOSPI": 0, "KOSDAQ": 1, "KONEX": 2}
        matched = []  # (rank, market_rank, code, name, market)
        for r in index:
            name = r["name"]
            code = r["code"]
            nl = r["_nl"]
            name_hit = ql in nl
            code_hit = query in code
            if not (name_hit or code_hit):
                continue
            # 관련도: ①정확일치 ②이름 접두 ③코드 접두 ④부분일치
            if nl == ql or code == query:
                rank = 0
            elif nl.startswith(ql):
                rank = 1
            elif code.startswith(query):
                rank = 2
            else:
                rank = 3
            # 동순위: 시총 데이터 부재 → KOSPI 우선 + 종목코드 오름차순 대체
            mkrank = _MK_RANK.get(str(r["market"]).strip().upper(), 3)
            matched.append((rank, mkrank, code, name, r["market"]))

        # 관련도 → 시장(KOSPI 우선) → 종목코드 오름차순
        matched.sort(key=lambda t: (t[0], t[1], t[2]))
        top = matched[:_SEARCH_LIMIT]
        results = [{"name": n, "code": c, "market": m}
                   for (_, _, c, n, m) in top]
        return {"query": query, "count": len(results), "results": results}
    except Exception as e:
        # 어떤 예외에도 500 금지: 200 + 빈결과 폴백.
        print(f"[search] 예외 폴백: {e}")
        return {"query": (q or "").strip(), "count": 0, "results": []}


@api.get("/api/config")
def get_config():
    """프론트가 애널리틱스 로더를 켜기 위한 공개 설정. 미설정이면 website_id 빈 문자열 → 스크립트 미로드."""
    return {
        "umami_src": os.getenv("UMAMI_SRC", "https://cloud.umami.is/script.js"),
        "umami_website_id": os.getenv("UMAMI_WEBSITE_ID", ""),
    }


# ---------------- 웹푸시 구독 엔드포인트 ----------------
@api.get("/api/push/key")
def push_key():
    """VAPID 공개키 서빙(프론트 pushManager.subscribe 용). 미설정이면 빈 문자열
    → 프론트가 토글을 우아하게 비활성. 공개키라 노출 안전."""
    return {"key": VAPID_PUBLIC_KEY if _PUSH_ENABLED else ""}


class PushSub(BaseModel):
    endpoint: str | None = None
    keys: dict | None = None
    expirationTime: object | None = None


@api.post("/api/push")
def push_subscribe(body: PushSub, request: Request):
    """웹푸시 구독 등록(기기별, X-Device-Id 스코프). 엔드포인트 기준 upsert."""
    device_id = _device_id(request)
    if not device_id:
        raise HTTPException(status_code=400, detail="기기 식별 헤더가 필요합니다.")
    endpoint = (body.endpoint or "").strip()
    if not endpoint or not isinstance(body.keys, dict):
        raise HTTPException(status_code=400, detail="유효한 구독 정보가 아닙니다.")
    sub = {"endpoint": endpoint, "keys": body.keys}
    if body.expirationTime is not None:
        sub["expirationTime"] = body.expirationTime
    try:
        push_store.save_sub(device_id, sub)
    except Exception:
        raise HTTPException(status_code=500, detail="구독 저장에 실패했습니다.")
    return {"ok": True}


class PushUnsub(BaseModel):
    endpoint: str | None = None


@api.delete("/api/push")
def push_unsubscribe(body: PushUnsub, request: Request):
    """웹푸시 구독 해제(그 기기+엔드포인트). endpoint 없으면 기기 전체 해제. 멱등."""
    device_id = _device_id(request)
    if not device_id:
        raise HTTPException(status_code=400, detail="기기 식별 헤더가 필요합니다.")
    try:
        push_store.delete_device_endpoint(device_id, (body.endpoint or "").strip())
    except Exception:
        raise HTTPException(status_code=500, detail="구독 해제에 실패했습니다.")
    return {"ok": True}


# ---------------- 베타 대기자 등록(waitlist) 스텁 ----------------
# 로컬 파일 기록만 한다. 외부 발송(메일·텔레그램·외부 API) 코드는 없다.
# 저장 파일은 data/ 아래(.gitignore 의 data/* 규칙으로 제외) → 실데이터 미커밋.
_WAITLIST_FILE = config.DATA / "waitlist.jsonl"
# 최소 형식 검증용(로컬·비발송). 완전한 RFC 검증이 아니라 오타/빈값 차단 목적.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WAITLIST_LOCK = threading.Lock()  # append/중복검사 원자성(동시요청 레이스 방지)


class WaitlistJoin(BaseModel):
    email: str | None = None
    telegram: str | None = None


def _load_waitlist_emails() -> set:
    """기존 waitlist.jsonl 의 이메일 소문자 집합(중복 감지용). 없으면 빈 set.
    파싱 불가 라인/파일없음은 조용히 건너뛴다(스텁 신뢰성 우선)."""
    emails = set()
    try:
        with open(_WAITLIST_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                e = (rec.get("email") or "").strip().lower()
                if e:
                    emails.add(e)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[waitlist] 로드 경고(무시): {e}")
    return emails


def _notify_waitlist_tg(rec: dict) -> None:
    """신규 대기자 등록을 President 텔레그램으로 즉시 전달(best-effort).
    ★서버(Render) 디스크는 비영속이라 파일 기록은 재배포 시 유실 — 이 전달이 원본 보존 경로다.
    실패해도 가입 처리는 깨지 않는다(별도 스레드·예외 무시). env 미설정 시 no-op."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("WAITLIST_TG_CHAT_ID")
    if not (tok and chat):
        return
    def _send():
        try:
            import requests as _rq
            msg = (f"[MIRI 베타 대기자] {rec['email']}"
                   + (f" · TG @{rec['telegram']}" if rec.get("telegram") else "")
                   + f" · {rec['ts']}")
            _rq.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                     json={"chat_id": chat, "text": msg}, timeout=10)
        except Exception as e:
            print(f"[waitlist] TG 전달 실패(가입은 정상 처리됨): {e}")
    threading.Thread(target=_send, daemon=True).start()


@api.post("/api/waitlist")
def join_waitlist(body: WaitlistJoin, request: Request):
    """베타 대기자 등록. 이메일 형식 검증 → data/waitlist.jsonl 에 1줄 append
    + President 텔레그램 즉시 전달(_notify_waitlist_tg, best-effort).

    - 중복 이메일은 조용히 ok 처리(status=already), 신규는 status=ok.
    - 잘못된 이메일은 400. 저장 실패는 500(파일 문제만). 개인정보 최소 수집.
    """
    email = (body.email or "").strip().lower()
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="올바른 이메일 주소를 입력하세요.")
    telegram = (body.telegram or "").strip().lstrip("@")[:64]
    ua = request.headers.get("user-agent", "")[:300]

    with _WAITLIST_LOCK:
        if email in _load_waitlist_emails():
            return {"ok": True, "status": "already",
                    "message": "이미 등록된 이메일입니다."}
        rec = {
            "email": email,
            "telegram": telegram,
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ua": ua,
        }
        try:
            with open(_WAITLIST_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[waitlist] 저장 실패: {e}")
            raise HTTPException(status_code=500,
                                detail="등록 처리 중 오류가 발생했습니다.")
    _notify_waitlist_tg(rec)
    return {"ok": True, "status": "ok", "message": "대기자 명단에 등록되었습니다."}


# ---------------- 정적 프론트엔드(web/) 마운트 (마지막에) ----------------
_WEB_DIR = Path(__file__).parent / "web"

# ---------------- TWA Digital Asset Links (명시 라우트, 정적마운트보다 먼저) ----------------
# Android TWA 검증은 배포 도메인의 /.well-known/assetlinks.json 을 application/json
# 200 으로 서빙하는 데 성패가 달렸다. StaticFiles 마운트가 서빙하더라도 content-type 은
# 호스트 mimetypes 레지스트리/Starlette 버전 동작에 의존한다(배포 패리티 리스크).
# 검증 실패는 TWA 전체를 깨므로, 여기서 명시 라우트로 application/json 200 을
# 결정론적으로 보장한다. 이 라우트는 아래 StaticFiles("/") 마운트보다 먼저 등록되어
# 우선 매칭된다(라우트 순서 중요). 파일 내용은 3단계(모바일)가 실제 패키지명+SHA256
# 으로 덮어쓴다 — 여기서는 라우팅만 뚫는다(빈 배열/스켈레톤 유지).
_ASSETLINKS_FILE = _WEB_DIR / ".well-known" / "assetlinks.json"


@api.get("/.well-known/assetlinks.json", include_in_schema=False)
def assetlinks():
    if not _ASSETLINKS_FILE.is_file():
        raise HTTPException(status_code=404, detail="assetlinks.json not found")
    return FileResponse(str(_ASSETLINKS_FILE), media_type="application/json")


api.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
