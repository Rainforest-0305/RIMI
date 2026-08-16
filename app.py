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
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import dart_poll
import watch_store  # 관심종목 영속(Supabase/JSON 폴백) 추상 스토어
import push_store   # 웹푸시 구독 영속(Supabase/JSON 폴백) — watch_store 패턴
import miri_cache   # 애널리스트/시총 캐시(Supabase upsert/select + 로컬 JSON 폴백)
import dedup  # 중복 이벤트(결정/결과·정정/원본) 접기
import impact
import scale_extract  # 규모보정 온디맨드 조회(/api/scale)
from summarize import summarize
import main as core  # load_watchlist / load_seen 재사용

api = FastAPI(title="미리(MIRI) 공시앱 API", version="2.0")

# 응답 압축: /api/alerts(~460KB JSON)·index.html이 모바일 회선 병목 → gzip으로 5~8배 축소.
from fastapi.middleware.gzip import GZipMiddleware
api.add_middleware(GZipMiddleware, minimum_size=1500)

# ---------------- 종목 단축코드 형식(단일 정의) ----------------
# KRX 는 2024-01 코드체계 개편으로 주권 «단축코드 한 자리에 영문 대문자»를 혼용한다
# (발급여력 5만→16.5만건). 실제 라이브 사례: `0126Z0` 삼성에피스홀딩스 — KOSPI 시총 68위.
# 숫자 6자리만 허용하면 이런 종목이 컨센서스·규모·관심종목 전 경로에서 조회 불가가 된다.
#
# ★ 이 게이트는 «보안 장치»다. code 는 파일명·캐시키·경로로 흘러가므로 임의 .json
#   읽기·존재 오라클(`../` 경로순회)을 막아야 한다. 아래는 «완화»가 아니라
#   «문자집합 확장»이다 — `.` `/` `\` `..` 공백·제어문자는 여전히 «전부» 차단된다.
# ★ 오히려 종전보다 «좁다»: `\d` 는 파이썬에서 «유니코드» 숫자까지 매칭해
#   `٠١٢٣٤٥`(아랍-인도 숫자) 같은 비ASCII 문자열이 게이트를 통과했다(실측).
#   `[0-9A-Z]` 는 ASCII 한정이라 그 구멍이 닫힌다.
_STOCK_CODE_RE = re.compile(r"[0-9A-Z]{6}")


def norm_stock_code(code):
    """종목코드 입력 → 정규화(대문자) 코드. 형식 위반이면 "" 반환(예외 없음).

    ★ 소문자를 «거부»하지 않고 «대문자로 정규화»하는 근거(실측):
      - DART corp_map 3,976 종목 전수에서 소문자 코드 0건. 영문 포함 53건은 «전부 대문자».
        즉 정본 표기는 대문자 하나뿐이다.
      - 같은 저장소의 build_corp_index.py 도 이미 `^[0-9A-Z]{6}$` 로 파싱한다.
      - corp_map 조회(dict.get)·Supabase upsert(on_conflict="code")·메모리 캐시가 모두
        코드 «문자열»을 키로 쓴다. 소문자를 그대로 통과시키면 키가 어긋나 «조용한»
        조회 미스와 캐시 분열이 생긴다. 그래서 거부보다 정규화가 안전하다.
      - 정규화는 검증 «앞»에서 일어나므로, 하류로 나가는 값은 항상 [0-9A-Z]{6} 이다.
    """
    c = (code or "").strip().upper()
    return c if _STOCK_CODE_RE.fullmatch(c) else ""


# ---------------- [42] 읽기 API 캐시(CDN/프록시 + ETag/304) ----------------
# 가드레일: s-maxage 는 폴링주기(85s)보다 신선해야 함 → 30s(+SWR 60s). 개인화
# /api/watchlist 는 공유캐시 금지(private, no-store). 응답 본문/스키마 불변(헤더만).
_READ_CACHE_CC = "public, s-maxage=30, stale-while-revalidate=60"


def _json_cached(request: Request, payload, cache_control: str = _READ_CACHE_CC):
    """읽기 API 응답에 Cache-Control + 약한 ETag 부착 + If-None-Match 304 처리.

    - 본문 바이트를 그대로 해시(콘텐츠 해시) → 변화 없으면 304(본문 0바이트 전송).
    - 약한 ETag(W/): gzip 등 콘텐츠 인코딩과 무관하게 조건부 비교 안전(RFC 권고).
    - 본문은 JSONResponse 와 동일 직렬화(ensure_ascii=False, 최소 구분자)로 생성해
      ETag 와 전송 바이트가 정확히 일치. 스키마/값 불변.
    """
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")
    etag = 'W/"' + hashlib.md5(body).hexdigest() + '"'
    headers = {"Cache-Control": cache_control, "ETag": etag}
    inm = request.headers.get("if-none-match", "")
    if inm:
        tokens = [t.strip() for t in inm.split(",")]
        if etag in tokens or "*" in tokens:
            return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)

# ---------------- 피드 캐시(노트북/DART 유량 배려) ----------------
_FEED_CACHE = {"ts": 0.0, "data": None}
# 초. /api/poll 은 이 캐시를 강제 무효화한다.
# 2026-07-30 60→600 (President 승인). 근거: TTL 60초는 재빌드(~8초, DART 최대 10콜)를
#   요청경로에서 유발했고, 트래픽이 성기면 사실상 모든 진입이 만료 후 첫 요청이라
#   유저가 매번 8초를 물었다(실측: 60초 간격 샘플 7회 중 6회 8초대, 1회 29.6초).
#   keepalive(.github/workflows/keepalive.yml)가 10분마다 /api/today 를 치도록 함께
#   바꿨으므로, TTL 을 그 주기에 맞춰 600 으로 올리면 캐시가 콜드로 만료되지 않는다.
#   부수효과(의도): _push_dispatch 는 _build_feed 안에서만 발화하는데, 기존에는
#   실유저 방문에만 의존했다. 이제 10분마다 확실히 발화한다.
#   DART 비용: 144회/일 × 최대 10콜 = 1,440콜/일 (한도 20,000콜/일의 7%).
#
# 2026-07-30 2차 조정 600→900. 1차(600)는 keepalive 주기와 **정확히 같아서** 경합이 났다.
#   GitHub Actions cron 은 수 분 지연되는 것이 정상이므로, TTL==핑주기면 핑이 늦는 만큼
#   캐시가 먼저 만료돼 그 창에 들어온 유저가 8초 재빌드를 문다(샘플링에서 실제 목격).
#   TTL 을 핑주기보다 넉넉히 길게(900 > 600) 두면 **항상 핑이 먼저 도착해** 캐시를 갱신하고,
#   유저는 만료된 캐시를 볼 일이 없다. 재빌드 횟수는 핑 주기가 정하므로 DART 비용은 불변.
_FEED_TTL = 900.0
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


# NEW 배지 recency 창(공시일 rcept_dt 기준). '최근 3일' = 공시일 포함 오늘·어제·그제.
_NEW_WINDOW_DAYS = 3


def _is_recent(rcept_dt: str, days: int = _NEW_WINDOW_DAYS) -> bool:
    """rcept_dt(YYYYMMDD=공시일)가 오늘 기준 최근 `days`일 이내인지(공시일 포함).

    delta = today - 공시일(일수). 0<=delta<days → True(예: days=3 이면 0,1,2일 전).
    파싱 실패/미래일자는 False. DART 콜 0(로컬 계산).
    """
    s = (rcept_dt or "").strip()
    if len(s) != 8 or not s.isdigit():
        return False
    try:
        d = datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return False
    delta = (datetime.now().date() - d).days
    return 0 <= delta < days


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
                # NEW 배지 = 미열람(seen 밖) AND 공시일 최근 3일 이내(_NEW_WINDOW_DAYS).
                # 3일창 정합: seen 회전(SEEN_MAX)으로 오래된 미열람건이 NEW로 새는 것 차단.
                # (mobile 이 기기별 seen-state 를 추가로 담당 — 서버는 recency 상한만 보증.)
                "is_new": (rno not in seen) and _is_recent(it.get("rcept_dt", "")),
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

# 프론트 계약 플래그: /api/alerts 응답 최상위 summary_ui. 프론트가 이 값으로 3줄
# 요약 패널 노출을 결정한다. 기본 false → 값만 추가될 뿐 기존 필드 불변(G3 opt-in
# 승인 후 GONGSI_SUMMARY_UI=1 로만 켠다). 요약 승격(LLM)과 독립된 UI 게이트.
_SUMMARY_UI = os.getenv("GONGSI_SUMMARY_UI", "").strip().lower() in ("1", "true", "yes", "on")


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
    # 메자닌 캐시도 미리 채운다(라이브 시세 콜드 ~수십초 → 첫 사용자 클릭 즉시화).
    try:
        t1 = time.time()
        _MEZZ_CACHE["data"] = _build_mezzanine_payload()
        _MEZZ_CACHE["ts"] = time.time()
        print(f"[prewarm] mezz cache 채움 in {(time.time() - t1) * 1000:.0f}ms")
    except Exception as e:
        print(f"[prewarm] mezz 실패(무시): {e}")
    # 랭킹 급등락도 미리 워밍(TOSS candles 콜드 ~11s → 첫 사용자 요청경로 라이브콜 0).
    # mezz prewarm 과 동형: startup 데몬에서만 발생, 요청경로엔 절대 안 들어간다.
    try:
        t2 = time.time()
        base = _ranking_base(pool_n=40)               # base 풀 캐시 채움(DART 0콜)
        codes = [it.get("stock_code") for it in (base.get("pool") or [])[:30]]
        warmed = _warm_prices(codes)                  # TOSS candles 배치(이 데몬에서만)
        print(f"[prewarm] ranking price 워밍 {warmed}종목 in {(time.time() - t2) * 1000:.0f}ms")
    except Exception as e:
        print(f"[prewarm] ranking price 실패(무시): {e}")


# ---------------- 주기 리프레셔(2026-07-30 신설) ----------------
# 왜 필요한가: 피드캐시는 TTL 로 만료되는데, 만료 후 첫 요청이 _build_feed 를
#   **요청 경로 안에서** 돌아 ~8초가 걸린다(실측 8.3~8.6초, 최대 29.6초).
#   트래픽이 성기면 사실상 모든 진입이 "만료 후 첫 요청"이라 유저가 매번 문다.
#
# 왜 keepalive 로는 안 되는가(실측):
#   .github/workflows/keepalive.yml 은 `*/10 * * * *` 로 10분 주기를 선언하지만,
#   GitHub Actions 무료 스케줄러는 이를 지키지 않는다. 실행 이력 실측(12회):
#     간격 179 / 192 / 451 / 613 / 224 / 283 / 334 / 595 / 232 / 360 / 740 분
#   즉 **3~12시간에 한 번**이며 2주 이상 이 상태였다. TTL 을 핑 주기에 맞춰
#   조정하는 접근(60→600→900) 은 전제가 틀렸다 — TTL 900 배포 후에도 t+14분
#   샘플에서 8.62초가 그대로 재발했다.
#
# 해법: 외부 스케줄러에 의존하지 않고 프로세스 안에서 주기 갱신한다.
#   _prewarm 과 동일한 데몬 패턴. 요청 경로엔 절대 들어가지 않는다.
#   TTL(900) 보다 짧은 주기(600)로 돌아 캐시가 콜드로 만료되는 창 자체를 없앤다.
#   기존 keepalive 는 그대로 둔다 — Render 슬립 해제(깨우기) 역할은 여전히 유효하고,
#   인스턴스가 잠들면 이 스레드도 같이 죽으므로 깨워줄 무언가가 필요하다.
#
# 부수효과(의도): _push_dispatch 는 _build_feed 안에서만 발화한다. 기존에는
#   실유저 방문에만 의존했으나 이제 10분마다 확실히 발화한다.
#
# DART 비용: force=True 1회당 list.json 최대 10콜 + bullet 선추출 최대
#   _BULLET_PREFETCH_CAP(12)콜. 144회/일 → 최대 3,168콜/일.
#   bullet 비동기 워머는 별도 _WARM_DAILY_CAP(3000) 서킷브레이커로 이미 상한이 있다.
#   합산 최악 ~6,200콜/일 = OpenDART 한도 20,000콜/일의 약 31%. 여유 있음.
_REFRESH_SEC = float(os.getenv("GONGSI_FEED_REFRESH_SEC", "600"))
_REFRESH_ENABLED = os.getenv("GONGSI_FEED_REFRESH", "1").strip().lower() not in ("0", "false", "no", "")


def _feed_refresher():
    """백그라운드 데몬: _REFRESH_SEC 마다 피드캐시를 선제 갱신한다.

    - 첫 실행 전 한 번 sleep 한다(startup 프리웜과 겹쳐 이중 빌드하지 않기 위해).
    - _get_feed 내부의 _BUILD_LOCK single-flight 가 유저 요청과의 동시 빌드를 막는다.
    - 어떤 예외도 스레드를 죽이지 않는다(swallow+print 후 다음 주기 계속).
    """
    while True:
        time.sleep(_REFRESH_SEC)
        t0 = time.time()
        try:
            data = _get_feed(force=True)
            print(f"[refresh] feed cache 갱신: alerts={data.get('count')} "
                  f"in {(time.time() - t0) * 1000:.0f}ms")
        except Exception as e:
            # 다음 주기에 다시 시도한다. 스레드는 절대 종료시키지 않는다.
            print(f"[refresh] 실패(무시, 다음 주기 재시도): {type(e).__name__}: {e}")


@api.on_event("startup")
def _startup_prewarm():
    """uvicorn 기동 직후 호출. 프리웜 스레드만 띄우고 즉시 반환(기동 무지연)."""
    # 실LLM 3줄요약(staged, 기본 off). GONGSI_LLM_ENABLED+ANTHROPIC_API_KEY 있을
    # 때만 훅 설치+워커기동. 기본 off 라 배선 후에도 현행 동작과 바이트 동일(훅
    # 미설치→규칙기반 스텁). 어떤 실패에도 앱 기동 무영향(try/except swallow).
    try:
        import llm_summary_client
        llm_summary_client.install_if_enabled()
    except Exception as e:
        print(f"[llm_summary] install 스킵(무시, 기동 유지): {type(e).__name__}")

    # 배치 non-run 워치독(①). 프리웜과 독립적으로 항상 띄운다 — 프리웜을 끈 환경에서
    # 감시까지 같이 꺼지면 안 된다. 데몬이라 기동/종료를 지연시키지 않는다.
    if _ANALYST_WATCH_ENABLED:
        threading.Thread(target=_analyst_watchdog, name="analyst-freshness",
                         daemon=True).start()
        print(f"[freshness] 워치독 기동(임계 {_ANALYST_STALE_DAYS:g}일, "
              f"주기 {_ANALYST_CHECK_SEC:g}s)")
    else:
        print("[freshness] 워치독 비활성(GONGSI_ANALYST_WATCH=0)")

    # 층2: 배치 «미실행» 감시(달력 데드라인). stale 임계(8일)와 «다른 축»이라 같이 띄운다 —
    # 임계로는 1회 미실행을 8일보다 빨리 잡을 수 없다(12일 사고의 구조적 원인).
    if _SLOT_WATCH_ENABLED:
        threading.Thread(target=_batch_slot_watchdog, name="batch-slot-watch",
                         daemon=True).start()
        print(f"[slot] 미실행 워치독 기동(주기 {_SLOT_CHECK_SEC:g}s, 유예 4h)")
    else:
        print("[slot] 미실행 워치독 비활성(GONGSI_SLOT_WATCH=0)")

    if not _PREWARM_ENABLED:
        print("[prewarm] 비활성(GONGSI_PREWARM=0) — 콜드빌드 경로 유지")
        return
    threading.Thread(target=_prewarm, name="feed-prewarm", daemon=True).start()
    print("[prewarm] 백그라운드 프리웜 스레드 기동(startup 즉시 반환)")

    # 주기 리프레셔. 프리웜과 독립적으로 띄운다 — 프리웜은 1회성이라 이게 없으면
    # TTL 만료 후 첫 요청이 다시 8초를 문다(실측). 데몬이라 기동/종료 무지연.
    if _REFRESH_ENABLED:
        threading.Thread(target=_feed_refresher, name="feed-refresher",
                         daemon=True).start()
        print(f"[refresh] 주기 리프레셔 기동(주기 {_REFRESH_SEC:g}s, TTL {_FEED_TTL:g}s)")
    else:
        print("[refresh] 비활성(GONGSI_FEED_REFRESH=0)")


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
    out = {
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
        # 종가 자동갱신 관측: external=실제 외부조회 횟수, skipped=최신이라 호출 안 한 횟수.
        # '이미 최신이면 외부 호출 0'을 운영 중에도 수치로 확인하기 위한 카운터.
        "price_chain": list(_PRICE_CHAIN),
        "price_expected_trade_day": _expected_trade_day(),
        "price_market_latest": _MARKET.get("latest"),
        # 진단용: cap=그 관측에 적용한 기준일 컷, age=관측 후 경과초.
        # latest == cap 이면 '컷에 눌린 관측'이라 스킵 게이트의 증거로 쓰이지 않는다.
        "price_market_cap": _MARKET.get("cap"),
        "price_market_age_sec": (round(time.time() - _MARKET["checked_ts"], 1)
                                 if _MARKET.get("checked_ts") else None),
        "price_calls": dict(_PRICE_CALLS),
    }
    # 배치 non-run 감시(①): analyst_consensus.updated_at 신선도. 배치와 독립된 경로라
    # 배치가 한 번도 안 돌아도(2026-07-25 실사고) 여기서 드러난다. 요청경로 외부호출 0
    # (워치독 데몬이 채워둔 스냅샷만 읽는다).
    try:
        out.update(_analyst_freshness_view())
    except Exception as e:  # noqa: BLE001
        out["analyst_freshness_error"] = type(e).__name__
    # 층1(R3/D3): top100 은 analyst 의 «입력»이라, 이게 죽으면 analyst 감시가 무력화된다.
    try:
        out.update(_top100_freshness_view())
    except Exception as e:  # noqa: BLE001
        out["top100_freshness_error"] = type(e).__name__
    # 층2(R3): 슬롯 데드라인 기반 «미실행» 판정. stale 임계(8일)와 독립된 축.
    try:
        out.update(_batch_slot_view())
    except Exception as e:  # noqa: BLE001
        out["batch_slot_error"] = type(e).__name__
    return out


@api.get("/api/alerts")
def get_alerts(request: Request):
    feed = _get_feed(force=False)
    # 프론트 계약: summary_ui 로 3줄요약 패널 노출 여부 판단(기본 false → 값만 추가,
    # 기존 필드 불변). feed 는 캐시 복사본이므로 여기서 주입해도 캐시 오염 없음.
    feed["summary_ui"] = _SUMMARY_UI
    return _json_cached(request, feed)


# ---- /api/poll 스로틀(공유 DART 키 소진·DoS 방어) ----
# 기기(X-Device-Id)당 최소 간격 + 일일 상한. 초과 시 에러 대신 캐시 피드 반환
# (사용자는 데이터 계속 봄, throttled 플래그로 프론트가 안내). 헤더 없으면 IP 폴백.
_POLL_MIN_INTERVAL = 30.0     # 초. 같은 기기 강제 재조회 최소 간격
_POLL_DAILY_CAP = 200         # 기기당 하루 강제 새로고침 상한
_POLL_STATE: dict = {}        # key -> {"last": ts, "day": epoch_day, "count": int}
_POLL_LOCK = threading.Lock()


def _poll_key(request: Request) -> str:
    dev = _device_id(request)
    if dev:
        return "d:" + dev
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    ip = xff or (request.client.host if request.client else "")
    return "ip:" + (ip or "?")


def _poll_allowed(request: Request) -> bool:
    now = time.time()
    day = int(now // 86400)
    key = _poll_key(request)
    with _POLL_LOCK:
        st = _POLL_STATE.get(key)
        if st is None or st["day"] != day:
            _POLL_STATE[key] = {"last": now, "day": day, "count": 1}
            return True
        if now - st["last"] < _POLL_MIN_INTERVAL:
            return False                      # 간격 미달 → 차단
        if st["count"] >= _POLL_DAILY_CAP:
            return False                      # 일일 상한 초과 → 차단
        st["last"] = now
        st["count"] += 1
        # 메모리 누수 방지: 상태 dict 과대 성장 시 오래된 항목 정리
        if len(_POLL_STATE) > 20000:
            for k in [k for k, v in _POLL_STATE.items() if v["day"] != day][:10000]:
                _POLL_STATE.pop(k, None)
        return True


@api.post("/api/poll")
def post_poll(request: Request):
    """수동 새로고침: 캐시 무효화 후 실 DART 재조회.

    스로틀 초과 시 강제 재조회를 건너뛰고 현재 캐시 피드를 반환한다(데이터는 계속
    보이며 throttled=true 로 신호). 정상 사용자(30초 내 재클릭 없음)는 영향 없음.
    """
    if not _poll_allowed(request):
        data = _get_feed(force=False)          # 캐시 사용(DART 0콜)
        data = dict(data); data["throttled"] = True
        return JSONResponse(data)
    return JSONResponse(_get_feed(force=True))


def _merge_regime_scale(res: dict) -> None:
    """/api/scale 응답에 **조회된 (유형×버킷) 1개 셀만** by_regime_scale 을 병합.

    해자 보호: impact_benchmark.json(34만 이벤트 집계)의 교차 테이블을 통째로
    내보내지 않는다 — scale_lookup 이 확정한 stype/bucket 딱 하나의 셀만 꺼낸다.
    패리티 보호: impact.load_benchmark()(서버가 이미 로드·캐시한 사전 구운 JSON)만
    읽는다. 런타임 DART/pykrx/bench_cache 접근 0(레짐 자동선택 없음 — 3레짐 전부 병기).
    없으면(버킷에 by_regime_scale 미집계, 또는 labels 미확인) 두 필드 모두 생략."""
    if not isinstance(res, dict) or res.get("status") != "ok":
        return
    stype, bucket = res.get("stype"), res.get("bucket")
    if not stype or not bucket:
        return
    bench = impact.load_benchmark()
    bkey = scale_extract.STYPE_BENCH_KEY.get(stype, stype)
    entry = bench.get(bkey)
    if not isinstance(entry, dict):
        return
    buckets = ((entry.get("scale_buckets") or {}).get("buckets") or {})
    brow = buckets.get(bucket)
    by_rs = brow.get("by_regime_scale") if isinstance(brow, dict) else None
    if not isinstance(by_rs, dict):
        return
    labels = ((bench.get("_meta") or {}).get("regime_axis") or {}).get("proposed_labels_ko")
    if not isinstance(labels, dict):
        return
    res["by_regime_scale"] = by_rs             # 조회 버킷 1개의 3레짐 셀만(벤치마크 값 그대로)
    res["regime_scale_labels_ko"] = labels      # _meta 소싱(President 확정 라벨, 하드코딩 금지)


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
    # 형식 검증(경로조작 차단): rcept/corp/code/dt 는 파일명·경로로 흘러가므로
    # 숫자 형식만 허용한다(../ 등 임의 .json 파일 읽기·존재 오라클 방지).
    code = (code or "").strip()
    corp = (corp or "").strip()
    dt = (dt or "").strip()
    # ★ [0-9] 로 «ASCII 한정». `\d` 는 파이썬에서 유니코드 숫자(아랍-인도 ٠١٢٣٤٥ ·
    #   전각 ０１２３ · 데바나가리 ०१२३)까지 매칭해 게이트를 그대로 통과시켰다(실측).
    #   그 값이 DART API 파라미터·캐시 파일명으로 흘러 쓰레기 호출과 비ASCII 파일명을 만든다.
    #   (int("٠١٢٣٤٥") == 12345 로 «숫자처럼 해석»되기까지 한다.)
    if not re.fullmatch(r"[0-9]{14}", rcept):
        raise HTTPException(status_code=400, detail="rcept 형식 오류(14자리 숫자)")
    if code:
        code = norm_stock_code(code)     # 대문자 정규화 + 형식검증(경로문자 전면차단 유지)
        if not code:
            raise HTTPException(status_code=400,
                                detail="code 형식 오류(6자리 영숫자)")
    if corp and not re.fullmatch(r"[0-9]{8}", corp):     # ★ ASCII 한정(위 주석 참조)
        raise HTTPException(status_code=400, detail="corp 형식 오류(8자리 숫자)")
    if dt and not re.fullmatch(r"[0-9]{8}", dt):         # ★ ASCII 한정(위 주석 참조)
        raise HTTPException(status_code=400, detail="dt 형식 오류(8자리 숫자)")
    corp_code = corp
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
    _merge_regime_scale(res)   # WS-33A: 조회 버킷의 레짐교차 셀만 추가 병합(신규 정적노출·외부콜 0)
    return JSONResponse(res)


# ---------------- 메자닌(CB/BW/EB) 전환 캘린더 (WS-34) ----------------
# features/mezzanine_calendar 격리 모듈을 지연 import(부재/실패해도 앱 전체 무영향).
# 발행데이터=로컬 캐시(DART 0콜), 시세/시총=pykrx·FDR(비-DART, 상위 N종목만).
# 시세 라이브 비용 큼 → TTL 15분 캐시.
_MEZZ_CACHE = {"data": None, "ts": 0.0, "lock": threading.Lock()}
_MEZZ_TTL_SEC = 900


def _build_mezzanine_payload(top_n: int = 5, upcoming_only: bool = True) -> dict:
    """collect → calendar/holdings → enrich(③moneyness ④시총희석). DART 콜 0."""
    import sys as _sys
    _mdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "features", "mezzanine_calendar")
    if _mdir not in _sys.path:
        _sys.path.insert(0, _mdir)
    import collect as _mc          # noqa: E402
    import calendar_view as _mcal  # noqa: E402
    import enrich as _menr         # noqa: E402
    records, _stats = _mc.collect_all()
    calendar, skipped = _mcal.build_calendar(records, upcoming_only=upcoming_only)
    holdings = _mcal.build_holdings(records)
    enriched = _menr.enrich_top_holdings(holdings, top_n=top_n)
    # 이번 달/다음 달 예상 개시 건수(순수 in-memory 집계, DART 0콜).
    # 근거=위 calendar(전체, truncate 전)를 연-월 그룹핑. build_monthly_outlook 주석 참조.
    monthly_outlook = _mcal.build_monthly_outlook(calendar)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dart_live_calls": 0,
        "market_scope": "코스피·코스닥",
        "disclaimer": "공시·시세 기반 사실/통계 정보이며 투자권유가 아닙니다.",
        "note": "시총대비 희석은 종목별 기준 표기(순잔량=전환청구 반영 후 잔량, 누적 발행=상환·기전환분 미차감)에 따릅니다.",
        "calendar": {
            "items": calendar[:50],
            "count_total": len(calendar),
            "skipped_no_start_date": skipped,
        },
        "monthly_outlook": monthly_outlook,
        "moneyness_summary": enriched["moneyness_dist"],
        "tranche_moneyness_summary": enriched["tranche_moneyness_dist"],
        "dilution_summary": enriched["dilution_stats"],
        "top_holdings": enriched["results"],
        "enrich_quality": {
            "checked": enriched["checked"],
            "price_fail": enriched["price_fail"],
            "mktcap_fail": enriched["mktcap_fail"],
            "skipped_no_code": enriched["skipped_no_code"],
            "skipped_no_price": enriched["skipped_no_price"],
        },
    }


@api.get("/api/mezzanine")
def get_mezzanine(request: Request, top_n: int = 5, upcoming_only: bool = True):
    """온디맨드 메자닌 전환 캘린더 + moneyness/시총희석(참고 통계). 실패해도 500 대신
    마지막 캐시/503. TTL 15분(라이브 시세 비용 흡수)."""
    top_n = max(1, min(int(top_n), 20))
    now = time.time()
    with _MEZZ_CACHE["lock"]:
        if _MEZZ_CACHE["data"] is not None and now - _MEZZ_CACHE["ts"] < _MEZZ_TTL_SEC:
            return _json_cached(request, _MEZZ_CACHE["data"])
    try:
        data = _build_mezzanine_payload(top_n=top_n, upcoming_only=upcoming_only)
    except Exception as e:  # noqa: BLE001
        if _MEZZ_CACHE["data"] is not None:
            return _json_cached(request, _MEZZ_CACHE["data"])
        return JSONResponse({"error": "mezzanine_build_failed", "detail": str(e)[:200]},
                            status_code=503)
    with _MEZZ_CACHE["lock"]:
        _MEZZ_CACHE["data"] = data
        _MEZZ_CACHE["ts"] = now
    return _json_cached(request, data)


# [36] /api/watchlist 지연 단축: load_watch_state 는 Supabase 3개 테이블
# (groups/stocks/keywords)을 순차 GET(3 왕복)해 626~772ms. GET 은 압도적 다수
# 읽기라 device_id 별 짧은 TTL 캐시로 반복 왕복을 제거한다. 정합성: 쓰기(담기/삭제/
# 그룹변경) 시 그 device 캐시를 방금 저장한 정규화 상태로 즉시 재적재(_watch_save)
# → stale 없음. TTL 은 외부변경 방어용 상한(쓰기 없이도 자연 만료).
_WATCH_CACHE = {"lock": threading.Lock(), "map": {}}   # device_id -> {"data":state,"ts":float}
_WATCH_TTL_SEC = 30


def _watch_load_cached(device_id: str) -> dict:
    """device_id 상태 조회(캐시 우선). 빈 device_id 는 캐시하지 않는다(임시 세션)."""
    dev = (device_id or "").strip()
    if not dev:
        return watch_store.load_watch_state(dev)
    now = time.time()
    with _WATCH_CACHE["lock"]:
        ent = _WATCH_CACHE["map"].get(dev)
        if ent is not None and now - ent["ts"] < _WATCH_TTL_SEC:
            return ent["data"]
    state = watch_store.load_watch_state(dev)   # 캐시미스 → 백엔드 1회
    with _WATCH_CACHE["lock"]:
        _WATCH_CACHE["map"][dev] = {"data": state, "ts": time.time()}
    return state


def _watch_save(state: dict, device_id: str) -> dict:
    """쓰기 경로 저장 + 그 device 캐시를 저장결과로 즉시 재적재(담기/삭제 즉시반영)."""
    saved = watch_store.save_watch_state(state, device_id)
    dev = (device_id or "").strip()
    if dev:
        with _WATCH_CACHE["lock"]:
            _WATCH_CACHE["map"][dev] = {"data": saved, "ts": time.time()}
    return saved


@api.get("/api/watchlist")
def get_watchlist(request: Request):
    state = _watch_load_cached(_device_id(request))
    # [42] 개인화(X-Device-Id) → 공유캐시(CDN/프록시) 금지. 서버측 device 캐시(36)는 유지.
    return JSONResponse(
        {"stocks": state["stocks"], "keywords": state["keywords"],
         "groups": state["groups"]},
        headers={"Cache-Control": "private, no-store"})


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

    name = (body.name or "").strip()[:40]   # 이름 길이 캡(payload 팽창 방지)
    if len(stocks) >= 300:                    # 기기당 관심종목 상한
        raise HTTPException(status_code=400,
                            detail="관심종목은 최대 300개까지 담을 수 있습니다.")
    # 1순위: 입력 그대로 코드 형식인가(영문 혼용 신형식 0126Z0 포함, 소문자는 대문자 정규화).
    code = norm_stock_code(raw)
    if not code:
        # 2순위(하위호환): 구분자 섞인 숫자 입력(예: "005-930", "종목 005930")에서 숫자만 추출.
        # ch.isdigit() 는 유니코드 숫자도 참이지만, norm_stock_code 가 ASCII 로 한 번 더 거른다.
        code = norm_stock_code("".join(ch for ch in raw if ch.isdigit()))
    if not code:
        raise HTTPException(
            status_code=400,
            detail="6자리 종목코드로 등록하세요. 예: 005930 (삼성전자)")
    if not name or name == raw:
        name = raw if raw.upper() != code else ""

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
    state = _watch_save(state, device_id)
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
    state = _watch_save(state, device_id)
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

    state = _watch_save(state, device_id)
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
    state = _watch_save(state, device_id)
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
    state = _watch_save(state, device_id)
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

    state = _watch_save(state, device_id)
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
    state = _watch_save(state, device_id)
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


# ---------------- ①오늘 / ③랭킹 탭 피드 (탭 스켈레톤, 정적마운트보다 먼저) ----------------
# features/today_feed 격리 빌더를 지연 import(부재/실패해도 앱 전체 무영향).
# 데이터 소스: 오늘·랭킹 **모두 이미 캐시된 /api/alerts live 피드(_get_feed)** 재사용.
# (①오늘은 bench_cache/morning_brief 를 은퇴 — 배포 빈값·stale 결함 해소. 데이터시점 ①==③.)
# 두 경로 모두 신규 DART 폴링 0. _MEZZ_CACHE 와 동일한 dict+lock+TTL 캐시 패턴.
_TODAY_CACHE = {"data": None, "ts": 0.0, "lock": threading.Lock()}
_TODAY_TTL_SEC = 300
_RANKING_CACHE = {"data": None, "ts": 0.0, "lock": threading.Lock()}
_RANKING_TTL_SEC = 120
# 큐레이션 폴백 캐시(seam 이 매 응답 재계산하지 않게; TTL 내 1회 build). LLM 훅이
# 켜져도 요청당 재요약 폭주 없음. secretary 계약 확정 시 이 캐시는 자연 무의미해진다.
_CURATION_CACHE = {"data": None, "ts": 0.0, "lock": threading.Lock()}
_CURATION_TTL_SEC = 300

# ---------------- ③랭킹 급등락(가격) 신호 캐시 + 백그라운드 워머 ----------------
# ★지연/패리티 가드(CTO): data-lead TOSS movers_for 는 20종목 ~17.7s → 절대 요청경로에
# 동기로 넣지 않는다. 요청경로는 이 stock_code TTL 캐시만 읽고(라이브콜 0), 미스면
# price_signal=null 로 즉시 반환한 뒤 백그라운드 워머가 top-N 후보만 채운다(수렴).
# TOSS 실패/타임아웃은 삼켜 price_signal=null(500 금지). _MEZZ_CACHE/_warm_worker 패턴 재사용.
_PRICE_CACHE: dict = {}                 # code -> {"chg_pct": float|None, "source","as_of","ts"}
_PRICE_TTL_SEC = 90.0                   # 급등락 캐시 신선도(요청경로 read-only)
_PRICE_LOCK = threading.Lock()
_PRICE_WARM_QUEUE: list = []            # 워밍 대기 종목코드
_PRICE_WARM_SEEN: set = set()           # 큐/처리중 dedup
_PRICE_WARM_THREAD = None
_PRICE_WARM_DAY = None
_PRICE_WARM_COUNT = 0
_PRICE_WARM_DAILY_CAP = 5000            # 일일 TOSS candles 호출 상한(남용 방지)
_PRICE_WARM_BATCH = 30                  # 워커 1회 처리 상한(top-N 후보)


_PRICE_ADAPTER_MOD = None


def _price_adapter():
    """features/ranking/price_adapter.py 를 importlib 고립로드(형제 features 모듈명
    충돌 회피 원칙 — collect 충돌 회피와 동일). 부재/실패해도 랭킹 무붕괴."""
    global _PRICE_ADAPTER_MOD
    if _PRICE_ADAPTER_MOD is not None:
        return _PRICE_ADAPTER_MOD
    import importlib.util as _ilu
    import sys as _sys
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "features", "ranking", "price_adapter.py")
    spec = _ilu.spec_from_file_location("_ranking_price_adapter", path)
    mod = _ilu.module_from_spec(spec)
    _sys.modules["_ranking_price_adapter"] = mod   # 고유 키(sys.modules 무오염)
    spec.loader.exec_module(mod)
    _PRICE_ADAPTER_MOD = mod
    return mod


def _price_lookup(code: str):
    """요청경로 read-only: 신선한 급등락 시그널이 캐시에 있으면 dict, 없으면 None.
    라이브 TOSS 호출을 절대 하지 않는다(미스는 워머가 백그라운드로 채운다)."""
    code = (code or "").strip()
    if not code:
        return None
    now = time.time()
    with _PRICE_LOCK:
        ent = _PRICE_CACHE.get(code)
        if ent and (now - ent.get("ts", 0.0)) < _PRICE_TTL_SEC:
            return {"change_pct": ent.get("change_pct"),
                    "price": ent.get("price"),
                    "prev_close": ent.get("prev_close"),
                    "volume": ent.get("volume"),
                    "source": ent.get("source") or "toss",
                    "as_of": ent.get("as_of")}
    return None


def _warm_prices(codes):
    """동기 워밍: price_adapter.movers_for 로 candles 조회→캐시 채움(TOSS 라이브콜).

    prewarm(백그라운드 데몬)·워커에서만 호출된다(요청경로 금지). 실패 전방위 격리:
    어떤 예외에도 캐시를 부분 갱신하고 조용히 반환(500 유발 안 함)."""
    codes = [c for c in ((x or "").strip() for x in (codes or [])) if c]
    if not codes:
        return 0
    try:
        pa = _price_adapter()
        results, _stats = pa.movers_for(codes[:_PRICE_WARM_BATCH])
        print(f"[price] warm resolved={_stats.get('resolved')} "
              f"toss_calls={_stats.get('toss_calls')} degraded={_stats.get('degraded')}")
    except Exception as e:  # noqa: BLE001
        print(f"[price] warm 실패(무시, null 폴백): {type(e).__name__}")
        return 0
    now = time.time()
    asof = time.strftime("%Y-%m-%dT%H:%M:%S")
    n = 0
    with _PRICE_LOCK:
        for code, r in (results or {}).items():
            _PRICE_CACHE[code] = {
                "change_pct": r.get("change_pct"),
                "price": r.get("price"),
                "prev_close": r.get("prev_close"),
                "volume": r.get("volume"),
                "source": "toss",
                "as_of": asof,
                "ts": now,
            }
            n += 1
        # 캐시 과대성장 방지(오래된 항목 정리)
        if len(_PRICE_CACHE) > 5000:
            stale = [k for k, v in _PRICE_CACHE.items()
                     if now - v.get("ts", 0.0) > _PRICE_TTL_SEC][:2000]
            for k in stale:
                _PRICE_CACHE.pop(k, None)
    return n


def _price_warm_worker():
    """큐를 배치로 비우며 _warm_prices 로 캐시를 채운다(fire-and-forget, 요청 무지연)."""
    global _PRICE_WARM_COUNT, _PRICE_WARM_DAY, _PRICE_WARM_THREAD
    while True:
        with _PRICE_LOCK:
            today = datetime.now().strftime("%Y%m%d")
            if _PRICE_WARM_DAY != today:
                _PRICE_WARM_DAY = today
                _PRICE_WARM_COUNT = 0
            if _PRICE_WARM_COUNT >= _PRICE_WARM_DAILY_CAP or not _PRICE_WARM_QUEUE:
                _PRICE_WARM_THREAD = None
                return
            batch = _PRICE_WARM_QUEUE[:_PRICE_WARM_BATCH]
            del _PRICE_WARM_QUEUE[:len(batch)]
        n = _warm_prices(batch)
        with _PRICE_LOCK:
            _PRICE_WARM_COUNT += n
            for c in batch:
                _PRICE_WARM_SEEN.discard(c)
        time.sleep(0.1)


def _price_enqueue(codes):
    """top-N 후보 종목코드를 급등락 워머 큐에 넣고 워커를 깨운다(fire-and-forget).
    이미 신선 캐시가 있는 코드는 건너뛴다(불필요한 TOSS 호출 회피)."""
    global _PRICE_WARM_THREAD
    now = time.time()
    with _PRICE_LOCK:
        for c in ((x or "").strip() for x in (codes or [])):
            if not c or c in _PRICE_WARM_SEEN:
                continue
            ent = _PRICE_CACHE.get(c)
            if ent and (now - ent.get("ts", 0.0)) < _PRICE_TTL_SEC:
                continue  # 이미 신선
            _PRICE_WARM_SEEN.add(c)
            _PRICE_WARM_QUEUE.append(c)
        need = (_PRICE_WARM_THREAD is None) or (not _PRICE_WARM_THREAD.is_alive())
        if _PRICE_WARM_QUEUE and need:
            _PRICE_WARM_THREAD = threading.Thread(
                target=_price_warm_worker, name="price-warmer", daemon=True)
            _PRICE_WARM_THREAD.start()


def _today_feed_builder():
    """features/today_feed/build.py 지연 import(mezzanine 와 동일한 sys.path 방식)."""
    import sys as _sys
    _tdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "features", "today_feed")
    if _tdir not in _sys.path:
        _sys.path.insert(0, _tdir)
    import build as _tb  # noqa: E402
    return _tb


def _curation_windows_valid(windows) -> bool:
    """windows(d1/w1/m1) 중 적어도 한 창에 숫자 raw_avg 또는 n 이 있으면 유효(True).
    피드 알럿 impact(impact_for_tags→status='ok')와 동형 판정. 진짜 데이터 부재
    (windows 비었거나 전부 무효)는 False → '집계 중' 유지(DoD: 집계중은 실제 부재만)."""
    if not isinstance(windows, dict) or not windows:
        return False
    for w in windows.values():
        if not isinstance(w, dict):
            continue
        for k in ("raw_avg", "n"):
            v = w.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return True
    return False


def _restore_item_signals(items, alerts=None):
    """소스 알럿(rcept_no 매칭)으로 표시아이템의 impact/scale_eligible 을 동형 복원.

    curation(항목13/16-a)과 overnight(항목19)이 공유하는 순수 정규화(제자리 변형,
    DART 0콜). build.py/daily_curation.py 무수정 — app.py 격리 seam.

    [impact 주입/정합] 아이템에 impact 가 없으면(overnight 은 _alert_item_dict 가 아예
      드롭) 소스 알럿의 impact(windows 포함)를 복사 주입. 이미 있으면(curation) 유지하되
      windows 유효 시 status='ok' 를 부착(프론트 게이트 imp.status!=='ok' → 집계중 해소).
      진짜 부재(알럿 미매칭 or windows 무효)는 손대지 않음 → '집계 중' 정직 유지.
    [scale_eligible] 소스 알럿의 플래그(피드 _build_feed 산출)를 복원. 매칭 실패/부재는
      기존값 or False(프론트 hasScale 게이트 안전)."""
    if not isinstance(items, list):
        return
    by_rno = {}
    for a in (alerts or []):
        if isinstance(a, dict):
            rno = str(a.get("rcept_no") or "")
            if rno:
                by_rno[rno] = a
    for it in items:
        if not isinstance(it, dict):
            continue
        rno = str(it.get("rcept_no") or "")
        a = by_rno.get(rno)
        imp = it.get("impact")
        # impact 주입(overnight): 아이템에 impact 없고 알럿엔 있으면 복사.
        if not isinstance(imp, dict) and a is not None and isinstance(a.get("impact"), dict):
            imp = dict(a["impact"])           # 얕은 복사(알럿 원본 불변 보호)
            it["impact"] = imp
        # windows 유효 → status='ok' 복원(알럿과 동형). 무효/부재는 그대로.
        if isinstance(imp, dict) and imp.get("status") != "ok" \
                and _curation_windows_valid(imp.get("windows")):
            imp["status"] = "ok"
        # [regime 복원] build_curation_fallback 이 impact 를 grade/confidence/windows 로만
        # 재구성하며 regime(시장국면별 과거영향)을 드롭 → 오늘탭 큐레이션 카드에서 레짐 블록이
        # 통째로 미노출됐다. 소스 알럿(_attach_regime 적용본)의 regime 을 동형 복원한다.
        # 알럿에 regime 이 없으면 손대지 않음 → 프론트 regimeBlock 이 우아하게 스킵(무손상).
        if isinstance(imp, dict) and not imp.get("regime") and a is not None:
            _a_imp = a.get("impact")
            if isinstance(_a_imp, dict) and isinstance(_a_imp.get("regime"), dict):
                imp["regime"] = _a_imp["regime"]
        # scale_eligible 동형 복원(소스 알럿 우선, 매칭 실패 시 기존값/False).
        if a is not None:
            it["scale_eligible"] = bool(a.get("scale_eligible"))
        else:
            it["scale_eligible"] = bool(it.get("scale_eligible"))


def _normalize_overnight_items(data: dict, alerts=None) -> dict:
    """[19] 오늘탭 밤사이(overnight) 밴드 정규화 — curation 과 동일 신호복원 재사용.

    overnight items(_alert_item_dict 산출)는 impact/scale_eligible 이 드롭돼 카드가
    전부 '집계 중'+규모버튼 없음. 소스 알럿 rcept_no 매칭으로 impact.status='ok'
    (windows 유효시)+scale_eligible 복원. ★curation 과 달리 정정정렬/rank 재부여는
    적용하지 않는다(밤사이 최신순·무 rank 유지). 스키마: 필드 추가만(하위호환)."""
    if not isinstance(data, dict):
        return data
    ov = data.get("overnight")
    if isinstance(ov, dict):
        _restore_item_signals(ov.get("items"), alerts)
    return data


def _normalize_curation_items(data: dict, alerts=None) -> dict:
    """SEAM 후처리(app.py 격리 정규화 — build.py/daily_curation.py 무수정).

    [13-a] build_curation_fallback 산출 impact 는 grade/confidence/windows 만 있고
      status 키가 없어(keys=['confidence','grade','windows']) 프론트 게이트
      (imp.status!=='ok')가 전부 '⏳ 집계 중'으로 폴백된다. windows 가 유효하면
      피드 알럿과 동형으로 status='ok' 를 부착(진짜 부재는 손대지 않음).
    [13-b] 기재정정(report_nm 에 '정정' 포함) 공시를 비정정 뒤로 안정정렬(소비측,
      daily_curation._score 시그니처 불변). 정렬 후 rank 만 표시순으로 재부여.
    [16-a] build_curation_fallback 이 소스 알럿의 scale_eligible 을 드롭 → 큐레이션
      카드에 '📏 규모로 보기' 버튼(hasScale=!!a.scale_eligible) 미노출. 소스 알럿을
      rcept_no 로 매칭해 scale_eligible 을 동형 복원(피드/랭킹과 동일 플래그).
      매칭 실패/부재는 False(프론트 게이트 안전). daily_curation.py 무수정.

    스키마 하위호환: status/scale_eligible 추가·정렬만, 키 제거 없음. items 없으면 no-op."""
    if not isinstance(data, dict):
        return data
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return data
    # [13-a/16-a] impact.status + scale_eligible 동형 복원(overnight 과 공용 헬퍼).
    _restore_item_signals(items, alerts)
    # [13-b] 기재정정 후순위(안정정렬: 비정정 상대순서 보존, 정정만 뒤로).
    def _is_correction(it):
        return 1 if ("정정" in str((it or {}).get("report_nm") or "")) else 0
    items = sorted(items, key=_is_correction)
    for i, it in enumerate(items, 1):        # 표시순 rank 재부여(중요도값 rank_score 불변)
        if isinstance(it, dict):
            it["rank"] = i
    data["items"] = items
    return data


def _today_curation(alerts=None) -> dict:
    """SEAM(단일 계약점): '오늘 공시 TOP 큐레이션' → CurationItem[] (중요도순).

    소스는 ③랭킹과 **동일한 live _get_feed 알럿**(alerts 인자). daily_curation 의
    _score/TYPE_WEIGHT 만 단방향 import 해 적용한다(build_ranking_base 와 동일 랭킹함수,
    DART 0콜). ★daily_curation.build_curation 호출 금지(라이브 DART 폴링)·daily_curation.py
    수정 금지. secretary 계약 확정 시 이 함수의 build_curation_fallback 호출만 교체하면
    되고 스키마·호출부·프론트는 불변. impact.windows 는 알럿 impact 그대로 → /api/alerts
    완전 동형.

    콜드(알럿 없음)로 빈 items 면 캐시하지 않는다 → 피드 워밍 후 다음 요청에서 즉시
    오늘자 큐레이션이 채워진다(stale-empty 방지)."""
    alerts = alerts or []
    now = time.time()
    with _CURATION_CACHE["lock"]:
        if _CURATION_CACHE["data"] is not None and now - _CURATION_CACHE["ts"] < _CURATION_TTL_SEC:
            return _CURATION_CACHE["data"]
    try:
        data = _normalize_curation_items(
            _today_feed_builder().build_curation_fallback(alerts), alerts)
    except Exception as e:  # noqa: BLE001
        print(f"[today] curation 폴백 실패(무시, 빈 items): {e}")
        if _CURATION_CACHE["data"] is not None:
            return _CURATION_CACHE["data"]
        try:
            return _today_feed_builder().empty_curation()
        except Exception:
            return {"status": "unavailable", "items": []}
    if data.get("items"):                     # 비어있지 않을 때만 캐시(콜드-빈 캐싱 방지)
        with _CURATION_CACHE["lock"]:
            _CURATION_CACHE["data"] = data
            _CURATION_CACHE["ts"] = now
    return data


@api.get("/api/today")
def get_today(request: Request):
    """①오늘 탭: ③과 **동일한 live _get_feed 알럿 소스**(오늘자). bench_cache 미사용.

    큐레이션은 중요도순 _today_curation(alerts) seam 이 매 응답마다 주입한다(캐시엔
    미포함 → 계약 교체 즉시 반영). _get_feed 캐시히트라 DART 0콜. 알럿 없으면(콜드)
    200 빈-정형 shape 이며 캐시하지 않는다(워밍 후 즉시 오늘자 반영)."""
    now = time.time()
    try:
        alerts = _get_feed(force=False).get("alerts") or []   # 캐시히트 시 DART 0콜
    except Exception as e:  # noqa: BLE001
        print(f"[today] feed 조회 실패(무시): {e}")
        alerts = []
    with _TODAY_CACHE["lock"]:
        if _TODAY_CACHE["data"] is not None and now - _TODAY_CACHE["ts"] < _TODAY_TTL_SEC:
            data = dict(_TODAY_CACHE["data"])
            data["curation"] = _today_curation(alerts)
            return _json_cached(request, data)
    try:
        data = _today_feed_builder().build_today_payload(alerts)
        # [19] overnight 밴드 impact.status/scale_eligible 동형 복원(캐시 전 1회).
        # 캐시히트 경로는 이미 정규화된 data 반환 → 재계산 불요, DART 0콜.
        _normalize_overnight_items(data, alerts)
    except Exception as e:  # noqa: BLE001
        print(f"[today] build 실패(무시, 빈 shape): {e}")
        if _TODAY_CACHE["data"] is not None:
            out = dict(_TODAY_CACHE["data"])
            out["curation"] = _today_curation(alerts)
            return _json_cached(request, out)
        try:
            out = _today_feed_builder().empty_today_payload()
        except Exception:
            out = {"overnight": {"items": [], "count": 0},
                   "type_distribution": {}, "market_scope": "코스피·코스닥"}
        out["curation"] = _today_curation(alerts)
        return _json_cached(request, out)
    if (data.get("overnight") or {}).get("count"):   # 콜드-빈 캐싱 방지
        with _TODAY_CACHE["lock"]:
            _TODAY_CACHE["data"] = data
            _TODAY_CACHE["ts"] = now
    out = dict(data)
    out["curation"] = _today_curation(alerts)
    return _json_cached(request, out)


def _ranking_base(pool_n: int = 40) -> dict:
    """공시중요도 base 풀(TTL 캐시). _get_feed 캐시 재사용 → 신규 DART 폴링 0.
    급등락은 여기 넣지 않는다(응답 시점에 price 캐시로 병합·재정렬)."""
    now = time.time()
    with _RANKING_CACHE["lock"]:
        if _RANKING_CACHE["data"] is not None and now - _RANKING_CACHE["ts"] < _RANKING_TTL_SEC:
            return _RANKING_CACHE["data"]
    tb = _today_feed_builder()
    feed = _get_feed(force=False)              # 캐시 히트 시 DART 0콜(신규 폴링 없음)
    base = tb.build_ranking_base(feed.get("alerts") or [], pool_n=pool_n)
    with _RANKING_CACHE["lock"]:
        _RANKING_CACHE["data"] = base
        _RANKING_CACHE["ts"] = now
    return base


@api.get("/api/ranking")
def get_ranking(request: Request, top_n: int = 30):
    """③랭킹 탭: 공시중요도(활성) + 급등락(활성, additive·graceful).

    - 공시중요도: daily_curation._score/TYPE_WEIGHT (alert tags/impact/report_nm, DART 0콜).
    - 급등락: _price_lookup 로 stock_code TTL 캐시만 read(요청경로 라이브 TOSS콜 0).
      미스면 price_signal=null 로 즉시 반환(순위 성립) + 백그라운드 워머가 top-N 후보를
      채운다(다음 요청부터 수렴). TOSS 실패/타임아웃 삼킴(500 금지).
    - buzz/조회급증은 defer(프론트 disabled 세그). 캐시 비어도 200 빈-정형 shape."""
    top_n = max(1, min(int(top_n), 50))
    try:
        tb = _today_feed_builder()
        base = _ranking_base(pool_n=max(40, top_n + 10))
        # 응답 시점 급등락 병합(캐시 read-only, 라이브콜 0) + 재정렬
        data = tb.apply_price_signal(base, price_lookup=_price_lookup, top_n=top_n)
        # top-N 후보 급등락 백그라운드 워밍(fire-and-forget, 응답 무지연)
        try:
            _price_enqueue([it.get("stock_code") for it in (base.get("pool") or [])[:top_n]])
        except Exception as e:
            print(f"[ranking] price enqueue skip: {e}")
        return _json_cached(request, data)
    except Exception as e:  # noqa: BLE001
        print(f"[ranking] build 실패(무시, 빈 shape): {e}")
        try:
            data = _today_feed_builder().empty_ranking_payload()
        except Exception:
            data = {"count": 0, "items": [], "market_scope": "코스피·코스닥"}
        return _json_cached(request, data)


# ---------------- 애널리스트 전망 / 시총 Top100 (캐시 전용 읽기 API) ----------------
# 두 엔드포인트 모두 **캐시 전용**: Supabase(우리 프로젝트 캐시 테이블) 우선,
# 실패/미설정 시 로컬 JSON 폴백. 요청경로에서 한경/toss/KRX 라이브콜을 절대 하지
# 않는다(수집은 analyst_collect.py / top100_collect.py 배치가 담당). corp_index/
# ranking read-only 패턴 미러. 어떤 예외에도 500 금지(200 + graceful 빈-정형).
_ANALYST_DISCLAIMER = "증권사 전망을 정리한 참고 자료이며 투자 권유가 아닙니다"
_TOP100_FILE = config.DATA / "top100.json"
_ANALYST_CACHE_FILE = config.DATA / "analyst_cache.json"

# 짧은 프로세스-내 TTL 캐시(Supabase/디스크 반복조회 완화). 값 불변, 헤더는 _json_cached.
_MIRI_TTL_SEC = 120.0
_TOP100_MEM = {"ts": 0.0, "data": None}
_ANALYST_MEM = {"ts": 0.0, "data": {}}  # code -> (ts, payload)
_MIRI_LOCK = threading.Lock()


def _corp_name(code):
    """corp_index 에서 code→종목명(없으면 None). _load_corp_index 캐시 재사용."""
    code = (code or "").strip()
    if not code:
        return None
    for r in _load_corp_index():
        if r.get("code") == code:
            return r.get("name") or None
    return None


def _empty_analyst(code, name=None):
    return {"code": code, "name": name, "cached": False, "current": None,
            "avg_tp": None, "n_total": 0, "n_tp": 0, "n_brokers": 0, "brokers": [],
            "window_start": None,
            "updated_at": None, "prices": [], "reports": [],
            "disclaimer": _ANALYST_DISCLAIMER}


# ---------------- 배치 non-run 감시(데이터 신선도 워치독) ----------------
# 배경(2026-07-24~25 실사고): 주간 배치 MIRI_AnalystCollect_Sat 이 절전 구간에 예정
# 시각을 맞아 **한 번도 실행되지 않았다**(LastTaskResult 267011, 로그 파일 자체 없음).
# 그런데 배치 실패 경보(_alert)는 '배치가 돌아야' 호출되므로 non-run 에는 구조적으로
# 무력하다. 자기 자신의 부재를 자기 자신이 알릴 수는 없다.
#   → 감시를 배치 밖(24시간 떠 있는 서버)에 둔다. 서버는 배치 산출물의 updated_at 만
#     보므로 배치가 죽어 있어도, 노트북·VM 이 꺼져 있어도 계속 판정한다.
#
# 임계 N(_ANALYST_STALE_DAYS) = 8일. 근거:
#   - 배치 주기는 주 1회(토 05:00)라 updated_at 나이는 정상 운영에서도 직전 실행 후
#     최대 7일(다음 실행 직전)까지 커진다. 7일 임계는 매주 토요일 새벽 정상 상태에서
#     오경보를 낸다(updated_at 이 date 단위라 경계에서 정확히 7).
#   - 8일이면 '토요일 실행이 통째로 누락된 뒤 일요일'에 처음 걸린다. 오경보 0,
#     탐지 지연 약 1일. 실사고(7/25 금 발견)를 하루 안에 잡는 수준이다.
#   - 더 키우면(9~14일) 다음 주 배치 성공에 가려 사고가 영영 안 드러날 수 있다.
#   운영 중 조정은 코드 수정 없이 GONGSI_ANALYST_STALE_DAYS 로 한다.
_ANALYST_STALE_DAYS = float(os.getenv("GONGSI_ANALYST_STALE_DAYS", "8"))
_ANALYST_CHECK_SEC = float(os.getenv("GONGSI_ANALYST_CHECK_SEC", "21600"))     # 감시 주기 6h
_ANALYST_FIRST_DELAY = float(os.getenv("GONGSI_ANALYST_FIRST_DELAY", "20"))    # 기동 후 첫 점검 지연
_ANALYST_ALERT_COOLDOWN = float(os.getenv("GONGSI_ANALYST_ALERT_COOLDOWN", "86400"))  # 재경보 최소간격 24h
_ANALYST_WATCH_ENABLED = os.getenv("GONGSI_ANALYST_WATCH", "1").strip().lower() \
    not in ("0", "false", "no")
# 텔레그램 실발송 스위치(테스트에서 스팸 없이 경로 검증하기 위한 출구). 기본 on.
_ANALYST_ALERT_TG = os.getenv("GONGSI_ANALYST_ALERT_TG", "1").strip().lower() \
    not in ("0", "false", "no")
_ANALYST_ALERT_MARK = config.DATA / "analyst_stale_alert.json"   # 재기동 후 재경보 폭주 방지

_ANALYST_FRESH_STATE = {
    "updated_at": None,      # 배치 산출물 중 가장 최신 수집일(YYYY-MM-DD)
    "age_days": None,        # 오늘(KST) - updated_at
    "stale": None,           # 임계 초과 여부. None = 아직 점검 전
    "rows": None,            # 관측된 전체 행 수
    "dated_rows": None,      # 그중 updated_at 이 있는 행 수(신선도 판정 분모)
    "stale_rows": None,      # 그중 임계보다 낡은 종목 수(부분 정체 탐지)
    "oldest_updated_at": None,
    "source": None,          # supabase | local | none
    "checked_ts": 0.0,
    "alerted_at": None,      # 마지막 경보 시각(ISO)
    "error": None,
}


def _analyst_probe():
    """analyst_consensus 의 updated_at 분포를 읽어 신선도 상태를 만든다(읽기 전용).

    Supabase 우선, 실패 시 로컬 스냅샷(analyst_cache.json) 폴백. 어느 쪽도 못 읽으면
    source='none' 으로 남기고 **stale 로 단정하지 않는다** — 조회 실패를 데이터 정체로
    오인해 오경보를 내면 경보 자체의 신뢰가 죽는다."""
    today = datetime.now(_KST).date()
    ups, rows, src, err = [], 0, "none", None
    try:
        ok, data = miri_cache.select_columns("analyst_consensus", "code,updated_at")
        if ok and data:
            ups = [str(r.get("updated_at") or "") for r in data if isinstance(r, dict)]
            rows, src = len(data), "supabase"
    except Exception as e:  # noqa: BLE001
        err = type(e).__name__
    if not ups:
        try:
            cache = miri_cache.load_json(_ANALYST_CACHE_FILE, default={}) or {}
            if isinstance(cache, dict) and cache:
                ups = [str((v or {}).get("updated_at") or "")
                       for v in cache.values() if isinstance(v, dict)]
                rows, src = len(cache), "local"
        except Exception as e:  # noqa: BLE001
            err = err or type(e).__name__
    # updated_at 이 비어 있는 행이 실제로 있다(실측 2026-07-28: 138행 중 38행).
    # 서버 종가 갱신 경로가 배치 미수집 종목의 행을 만들 때 생긴다 — 이 행들은 배치
    # 신선도의 근거가 될 수 없으므로 분모에서 제외하고, 개수는 따로 노출한다.
    ups = [u for u in ups if len(u) == 10]
    st = {"rows": rows, "dated_rows": len(ups), "source": src, "error": err,
          "checked_ts": time.time(), "updated_at": None, "age_days": None,
          "stale": None, "stale_rows": None, "oldest_updated_at": None}
    if not ups:
        return st
    newest, oldest = max(ups), min(ups)
    st["updated_at"], st["oldest_updated_at"] = newest, oldest
    try:
        st["age_days"] = (today - datetime.strptime(newest, "%Y-%m-%d").date()).days
    except ValueError:
        return st
    st["stale"] = st["age_days"] > _ANALYST_STALE_DAYS
    limit = (today - _td(days=int(_ANALYST_STALE_DAYS))).isoformat()
    st["stale_rows"] = sum(1 for u in ups if u < limit)
    return st


def _stale_alert_allowed(now_ts):
    """경보 쿨다운(24h). 프로세스 재기동으로 메모리가 날아가도 스팸이 안 되게
    파일 마커를 병행한다(디스크가 휘발돼도 최악 재기동 1회 중복)."""
    last = _ANALYST_FRESH_STATE.get("alerted_ts") or 0.0
    if not last:
        try:
            mark = miri_cache.load_json(_ANALYST_ALERT_MARK, default={}) or {}
            last = float(mark.get("ts") or 0.0)
        except Exception:  # noqa: BLE001
            last = 0.0
    return (now_ts - last) >= _ANALYST_ALERT_COOLDOWN


def _stale_alert(st):
    """운영자 텔레그램 경보(notify_alert._tg_send = config.TEST_CHAT_ID 고정).
    실유저 공시채널(tg_channel/TG_CHANNEL_ID)과 구조적으로 분리돼 있다."""
    msg = ("[MIRI 신선도경보] analyst_consensus 정체\n"
           f"최신 수집일 {st.get('updated_at')} · {st.get('age_days')}일 경과 "
           f"(임계 {_ANALYST_STALE_DAYS:g}일)\n"
           f"수집일 있는 종목 {st.get('dated_rows')}개(전체 {st.get('rows')}) 중 "
           f"{st.get('stale_rows')}개 정체 (가장 오래된 {st.get('oldest_updated_at')})\n"
           f"출처 {st.get('source')}\n"
           "주간 배치(MIRI_AnalystCollect_Sat)가 실행되지 않았을 가능성 — "
           "스케줄러 LastRunTime/LastTaskResult 확인 요망")
    print("[freshness][ALERT] " + msg.replace("\n", " | "))
    sent = False
    if _ANALYST_ALERT_TG:
        try:
            from notify_alert import _tg_send
            sent = bool(_tg_send(msg))
        except Exception as e:  # noqa: BLE001
            print(f"[freshness] 경보 발송 실패: {type(e).__name__}")
    now = time.time()
    _ANALYST_FRESH_STATE["alerted_ts"] = now
    _ANALYST_FRESH_STATE["alerted_at"] = datetime.now(_KST).isoformat(timespec="seconds")
    try:
        miri_cache.save_json(_ANALYST_ALERT_MARK,
                             {"ts": now, "at": _ANALYST_FRESH_STATE["alerted_at"],
                              "updated_at": st.get("updated_at"),
                              "age_days": st.get("age_days"), "tg_sent": sent})
    except Exception:  # noqa: BLE001
        pass
    return sent


def _analyst_freshness_check():
    """1회 점검(+임계 초과 시 경보). 반환: 상태 dict. 예외를 던지지 않는다."""
    try:
        st = _analyst_probe()
    except Exception as e:  # noqa: BLE001
        print(f"[freshness] 점검 예외(무시): {type(e).__name__}")
        return dict(_ANALYST_FRESH_STATE)
    for k, v in st.items():
        _ANALYST_FRESH_STATE[k] = v
    print(f"[freshness] analyst updated_at={st.get('updated_at')} "
          f"age={st.get('age_days')}d stale={st.get('stale')} "
          f"rows={st.get('rows')}(dated {st.get('dated_rows')}) "
          f"stale_rows={st.get('stale_rows')} src={st.get('source')}")
    if st.get("stale") and _stale_alert_allowed(st["checked_ts"]):
        _stale_alert(st)
    return dict(_ANALYST_FRESH_STATE)


def _analyst_watchdog():
    """데몬 스레드: 기동 직후 1회 + 이후 _ANALYST_CHECK_SEC 마다 점검.
    배치와 완전히 독립된 경로다 — 배치가 안 도는 것을 배치가 감지할 수는 없다."""
    time.sleep(max(0.0, _ANALYST_FIRST_DELAY))
    while True:
        _analyst_freshness_check()
        time.sleep(max(60.0, _ANALYST_CHECK_SEC))


def _analyst_freshness_view():
    """/api/health 노출용 스냅샷(요청경로에서 외부 호출 0).

    워치독 스레드가 죽었거나 아직 안 돈 경우(마지막 점검이 주기의 2배 초과)에는
    백그라운드 점검을 1회 띄운다 — 감시자가 조용히 죽는 것도 감시한다."""
    st = dict(_ANALYST_FRESH_STATE)
    ts = st.get("checked_ts") or 0.0
    age = round(time.time() - ts, 1) if ts else None
    if _ANALYST_WATCH_ENABLED and (age is None or age > _ANALYST_CHECK_SEC * 2):
        if not any(t.name == "analyst-freshness-adhoc" and t.is_alive()
                   for t in threading.enumerate()):
            threading.Thread(target=_analyst_freshness_check,
                             name="analyst-freshness-adhoc", daemon=True).start()
    return {
        "analyst_updated_at": st.get("updated_at"),
        "analyst_age_days": st.get("age_days"),
        "analyst_stale": st.get("stale"),
        "analyst_stale_threshold_days": _ANALYST_STALE_DAYS,
        "analyst_rows": st.get("rows"),
        "analyst_dated_rows": st.get("dated_rows"),
        "analyst_stale_rows": st.get("stale_rows"),
        "analyst_oldest_updated_at": st.get("oldest_updated_at"),
        "analyst_freshness_source": st.get("source"),
        "analyst_checked_age_sec": age,
        "analyst_stale_alerted_at": st.get("alerted_at"),
    }


# ================= 층1: top100 신선도 감시 (2026-08-16 R3/D3) =================
# 왜 필요한가: [실측] analyst_collect.target_codes() 가 top100.json 을 «입력»으로 읽는다
#   (analyst_collect.py:661-662, :681). 그래서 top100 이 조용히 죽으면 analyst 는
#   «낡은 대상 집합»으로 정상 완주하며 updated_at 을 갱신하고, analyst stale 감시는
#   False 를 유지한다 → ★아무 경보도 안 난다.
#   같은 계열 사고가 이미 한 번 있었다(analyst_collect.py 주석: 「상한을 두는 것 자체가
#   커버리지를 «조용히» 갉아먹는 구조」). 추정이 아니라 관측이다.
# 임계 근거: top100_collect 도 주 1회(토 05:00)라 정상 운영에서 최대 7일까지 커진다.
#   8일이면 '토요일 실행이 통째로 누락된 뒤'에 처음 걸린다 — analyst 와 동일 근거.
_TOP100_STALE_DAYS = float(os.getenv("GONGSI_TOP100_STALE_DAYS", "8"))

_TOP100_FRESH_STATE = {
    "updated_at": None, "age_days": None, "stale": None,
    "rows": None, "source": None, "checked_ts": 0.0, "error": None,
}


def _top100_probe():
    """market_cap_top100 의 updated_at 을 읽어 신선도 상태를 만든다(읽기 전용).

    Supabase 우선 → 로컬 top100.json 폴백. 어느 쪽도 못 읽으면 source='none' 으로
    남기고 **stale 로 단정하지 않는다** — 조회 실패를 정체로 오인하면 경보 신뢰가 죽는다
    (_analyst_probe 와 동일 원칙).
    ★updated_at 은 rank 1 행 하나가 아니라 «최댓값»으로 잡는다. 부분 갱신 시 선두 행만
      보면 신선해 보이는 함정이 있다(_top100_from_rows 는 첫 행만 본다).
    """
    today = datetime.now(_KST).date()
    ups, rows, src, err = [], 0, "none", None
    try:
        ok, data = miri_cache.select_columns("market_cap_top100", "code,updated_at")
        if ok and data:
            ups = [str(r.get("updated_at") or "") for r in data if isinstance(r, dict)]
            rows, src = len(data), "supabase"
    except Exception as e:  # noqa: BLE001
        err = type(e).__name__
    if not ups:
        try:
            snap = miri_cache.load_json(_TOP100_FILE, default=None)
            if isinstance(snap, dict) and snap.get("updated_at"):
                ups = [str(snap.get("updated_at"))]
                rows = len(snap.get("items") or [])
                src = "local"
        except Exception as e:  # noqa: BLE001
            err = err or type(e).__name__
    ups = [u for u in ups if len(u) == 10]
    st = {"rows": rows, "source": src, "error": err, "checked_ts": time.time(),
          "updated_at": None, "age_days": None, "stale": None}
    if not ups:
        return st
    st["updated_at"] = max(ups)
    try:
        st["age_days"] = (today - datetime.strptime(st["updated_at"], "%Y-%m-%d").date()).days
    except ValueError:
        return st
    st["stale"] = st["age_days"] > _TOP100_STALE_DAYS
    return st


def _top100_freshness_check():
    """1회 점검(경보 없음 — 판정은 층2 소관). 예외를 던지지 않는다."""
    try:
        st = _top100_probe()
    except Exception as e:  # noqa: BLE001
        print(f"[top100fresh] 점검 예외(무시): {type(e).__name__}")
        return
    for k, v in st.items():
        _TOP100_FRESH_STATE[k] = v
    print(f"[top100fresh] updated_at={st.get('updated_at')} age={st.get('age_days')}d "
          f"stale={st.get('stale')} rows={st.get('rows')} src={st.get('source')}")


def _top100_freshness_view():
    """/api/health 노출용 스냅샷.

    ★층1 은 층2(슬롯 데몬)와 «독립»이어야 한다 — 층2 를 GONGSI_SLOT_WATCH=0 으로 꺼도
      top100 신선도는 계속 보여야 한다(전달 복구 전에도 «관측 가능성»을 만드는 게 층1의 존재
      이유다). 그래서 스냅샷이 비었거나 낡았으면 여기서 백그라운드 점검을 1회 띄운다 —
      _analyst_freshness_view 의 adhoc 패턴과 동형. 요청 경로는 «기다리지 않는다».
    """
    st = dict(_TOP100_FRESH_STATE)
    ts = st.get("checked_ts") or 0.0
    if (not ts) or (time.time() - ts) > max(3600.0, _SLOT_CHECK_SEC * 2):
        if not any(t.name == "top100-freshness-adhoc" and t.is_alive()
                   for t in threading.enumerate()):
            threading.Thread(target=_top100_freshness_check,
                             name="top100-freshness-adhoc", daemon=True).start()
    return {
        "top100_updated_at": st.get("updated_at"),
        "top100_age_days": st.get("age_days"),
        "top100_stale": st.get("stale"),
        "top100_stale_threshold_days": _TOP100_STALE_DAYS,
        "top100_rows": st.get("rows"),
        "top100_freshness_source": st.get("source"),
        "top100_checked_age_sec": (round(time.time() - ts, 1) if ts else None),
    }


# ================= 층2: 배치 «미실행» 감시 (달력 데드라인) =================
# stale 임계(8일)와 «다른 축»이다. 임계는 「무엇을 이상으로 볼까」, 이건 「예정된 회차가
# 실제로 돌았나」다. 주 1회 배치라 6~7일 무갱신이 «정상»이므로, stale 임계로는 1회
# 미실행을 8일보다 빨리 잡을 수 «없다» — 12일 사고가 12일 걸린 구조적 이유가 그것이다.
# 유예 4h 근거: [실측] 2026-08-04 catch-up 이 슬롯(05:30) 대비 «3시간 04분» 늦게 시작했다.
#   그보다 짧으면 정상 catch-up 을 미실행으로 오경보한다. 여유 1시간을 더해 4시간.
# 휴장일 예외 «없음»(의도): 슬롯이 토요일(비거래일)이고 updated_at 이 «실행일»이라
#   (top100_collect.py:205 / analyst_collect.py:630 date.today()) 휴장과 무관하다.
#   「정당하게 안 도는 날」이 존재하지 않으므로 예외를 두면 진짜 미실행 경보를 억제한다.
_SLOT_SPECS = {
    "analyst": {"task": "MIRI_AnalystCollect_Sat", "weekday": 5, "hour": 5, "minute": 30,
                "grace_h": 4.0, "label": "애널리스트 컨센서스"},
    "top100": {"task": "MIRI_Top100Collect_Sat", "weekday": 5, "hour": 5, "minute": 0,
               "grace_h": 4.0, "label": "시총 Top100"},
}
_SLOT_CHECK_SEC = float(os.getenv("GONGSI_SLOT_CHECK_SEC", "1800"))     # 30분
_SLOT_ALERT_COOLDOWN = float(os.getenv("GONGSI_SLOT_ALERT_COOLDOWN", "86400"))
_SLOT_WATCH_ENABLED = os.getenv("GONGSI_SLOT_WATCH", "1").strip().lower() \
    not in ("0", "false", "no")
_SLOT_STATE = {}


def _last_slot(spec, now):
    """now 이전(포함) 가장 최근의 예정 실행 시각(KST)."""
    d = now.date()
    back = (d.weekday() - spec["weekday"]) % 7
    cand = datetime.combine(d - _td(days=back), datetime.min.time(),
                            tzinfo=_KST).replace(hour=spec["hour"], minute=spec["minute"])
    if cand > now:
        cand -= _td(days=7)
    return cand


def _slot_verdict(job, updated_at, now):
    """(verdict, why, slot). verdict ∈ OK | NONRUN | UNKNOWN.
    ★조회 실패(updated_at=None)를 미실행으로 «단정하지 않는다»."""
    spec = _SLOT_SPECS[job]
    slot = _last_slot(spec, now)
    deadline = slot + _td(hours=spec["grace_h"])
    if now < deadline:
        return "OK", f"유예 안(마감까지 {(deadline - now).total_seconds() / 3600:.1f}h)", slot
    if not updated_at:
        return "UNKNOWN", "갱신일 미확보 — 판정 보류", slot
    try:
        upd = datetime.strptime(str(updated_at)[:10], "%Y-%m-%d").date()
    except ValueError:
        return "UNKNOWN", f"갱신일 파싱 실패({updated_at!r})", slot
    if upd >= slot.date():
        return "OK", f"슬롯({slot.date()}) 이후 갱신됨({upd})", slot
    return ("NONRUN",
            f"슬롯 {slot:%Y-%m-%d %H:%M} 이 유예 {spec['grace_h']:.0f}h 를 넘겼는데 "
            f"데이터 갱신일은 {upd} — 실행되지 않았다", slot)


def _slot_alert(job, why, slot):
    """미실행 경보. _stale_alert 와 «같은 채널»(notify_alert._tg_send = 운영자 개인).
    실유저 공시채널(TG_CHANNEL_ID)은 이 경로를 쓰지 않으므로 구조적으로 도달 불가."""
    spec = _SLOT_SPECS[job]
    msg = (f"[MIRI 배치 미실행] {spec['label']}\n"
           f"작업 {spec['task']}\n"
           f"{why}\n"
           "조치: schtasks /query /tn <작업> /v 로 LastRunTime·LastTaskResult 확인")
    print("[slot][ALERT] " + msg.replace("\n", " | "))
    sent = False
    if _ANALYST_ALERT_TG:
        try:
            from notify_alert import _tg_send
            sent = bool(_tg_send(msg))
        except Exception as e:  # noqa: BLE001
            print(f"[slot] 경보 발송 실패: {type(e).__name__}")
    _SLOT_STATE.setdefault(job, {}).update(
        alerted_ts=time.time(),
        alerted_at=datetime.now(_KST).isoformat(timespec="seconds"),
        alert_sent=sent)
    return sent


def _slot_check_once():
    """1회 점검. 예외를 던지지 않는다. 반환: {job: verdict}

    ★입력을 «직접 probe» 한다 — /api/health 스냅샷(6h)을 읽지 않으므로 관측지연이 0 이다.
    """
    now = datetime.now(_KST)
    out = {}
    for job in _SLOT_SPECS:
        try:
            if job == "analyst":
                st = _analyst_probe()
                for k, v in st.items():
                    _ANALYST_FRESH_STATE[k] = v
            else:
                st = _top100_probe()
                for k, v in st.items():
                    _TOP100_FRESH_STATE[k] = v
            verdict, why, slot = _slot_verdict(job, st.get("updated_at"), now)
        except Exception as e:  # noqa: BLE001
            print(f"[slot] {job} 점검 예외(무시): {type(e).__name__}: {e}")
            continue
        prev = _SLOT_STATE.setdefault(job, {})
        prev.update(verdict=verdict, why=why, slot=slot.isoformat(timespec="minutes"),
                    checked_ts=time.time())
        out[job] = verdict
        print(f"[slot] {job} {verdict} · {why}")
        if verdict == "NONRUN":
            last = prev.get("alerted_ts") or 0.0
            if (time.time() - last) >= _SLOT_ALERT_COOLDOWN:
                _slot_alert(job, why, slot)
    return out


def _batch_slot_watchdog():
    """데몬: 기동 후 잠시 뒤 1회 + 이후 _SLOT_CHECK_SEC 마다.
    ★배치와 독립된 경로다 — 배치가 안 도는 것을 배치가 감지할 수는 없다.
    ★그리고 stale 임계와도 «독립»이다 — 임계 8일로는 1회 미실행을 8일 전에 못 잡는다.
    ★어떤 예외에도 스레드를 죽이지 않는다. 감시자가 조용히 죽는 게 우리가 고치려는 병이다."""
    time.sleep(max(0.0, _ANALYST_FIRST_DELAY))
    while True:
        try:
            _slot_check_once()
        except Exception as e:  # noqa: BLE001
            # ★여기까지 온 예외는 «반드시» 로그에 남긴다. 조용히 죽으면 감시가 사라진다.
            print(f"[slot][THREAD-ERR] 점검 루프 예외(계속 진행): {type(e).__name__}: {e}")
        time.sleep(max(60.0, _SLOT_CHECK_SEC))


def _batch_slot_view():
    """/api/health 노출용. 외부 호출 0.
    ★slot_*_checked_age_sec 가 커지면 «데몬이 조용히 죽은 것»이다 — 그걸 보라고 낸다."""
    out = {}
    for job in _SLOT_SPECS:
        s = _SLOT_STATE.get(job) or {}
        ts = s.get("checked_ts") or 0.0
        out[f"slot_{job}_verdict"] = s.get("verdict")
        out[f"slot_{job}_why"] = s.get("why")
        out[f"slot_{job}_last_slot"] = s.get("slot")
        out[f"slot_{job}_checked_age_sec"] = (round(time.time() - ts, 1) if ts else None)
        out[f"slot_{job}_alerted_at"] = s.get("alerted_at")
        out[f"slot_{job}_alert_sent"] = s.get("alert_sent")
    out["slot_watch_enabled"] = _SLOT_WATCH_ENABLED
    out["slot_check_sec"] = _SLOT_CHECK_SEC
    return out


# ---------------- 종가 신선도 자동 갱신(서버 자체 · 배치/VM/노트북 불요) ----------------
# 설계: '매 요청 실시간 조회'가 아니다. 저장된 종가의 **기준 거래일**이 지금 시점에서
# 있어야 할 최신 거래일보다 낡았을 때만 외부에서 1회 가져온다. 최신이면 외부 호출 0.
# Render 는 24시간 떠 있으므로 노트북·VM 이 모두 꺼져 있어도 사용자가 앱을 열면
# 그 자리에서 최신 종가로 맞춰진다.
#
# 휴장일(공휴일) 처리: 한국 공휴일 달력이 없으면 '평일인데 종가가 없는 날'을 판정할 수
# 없어 매 요청 재조회에 빠진다. 그래서 달력 대신 **관측**을 쓴다 — 어떤 종목이든 조회에
# 성공하면 그 응답의 거래일을 _MARKET['latest'](시장이 실제로 내놓은 최신 거래일)로
# 기록하고, 저장값이 그 이상이면 더 새 것이 없다고 보고 호출하지 않는다.
# 여기에 종목별 재시도 쿨다운을 더해 어떤 경우에도 호출량이 발산하지 않는다.
from datetime import timedelta as _td, timezone as _tz

_KST = _tz(_td(hours=9))
_MKT_CLOSE_MIN = 16 * 60        # 15:30 정규장 마감 + 정산 여유 → 16:00(KST) 이후를 '오늘 종가 확정'으로 본다
_PRICE_SYNC_WAIT = float(os.getenv("GONGSI_PRICE_SYNC_WAIT", "2.5"))   # 동기 대기 상한(초). 초과 시 기존값 즉시 반환 + 백그라운드가 마저 완료
_PRICE_RETRY_SEC = float(os.getenv("GONGSI_PRICE_RETRY_SEC", "1800"))  # 종목별 재시도 쿨다운(휴장일 무한 재시도 방지)
_MARKET_TTL_SEC = float(os.getenv("GONGSI_MARKET_TTL_SEC", "21600"))   # 관측된 시장 최신거래일 유효기간(6h)
# 소스 체인: 토스 유지(1순위). Render 에는 toss_data/pykrx 가 없어 자연히 fdr 로 폴백된다.
_PRICE_CHAIN = tuple(s.strip() for s in
                     os.getenv("GONGSI_PRICE_CHAIN", "toss,pykrx,fdr").split(",") if s.strip())

_PRICE_FRESH = {}     # code -> {"price","asof","source","ts"}  갱신 성공값 오버레이
_PRICE_JOBS = {}      # code -> {"ev":Event,"res":tuple|None}   in-flight 병합(중복 호출 방지)
_PRICE_ATTEMPT = {}   # code -> 마지막 시도 시각(쿨다운)
_PRICE_PERSISTED = {} # code -> 마지막으로 Supabase 에 쓴 거래일(중복 upsert 차단)
# latest=관측된 시장 최신 거래일, cap=그 관측에 적용했던 기준일 컷(_expected_trade_day).
# cap 을 함께 남기지 않으면 '컷에 눌려서 옛 날짜가 나온 것'과 '시장에 정말 더 없는 것'을
# 구분할 수 없어, 마감 후 당일 종가 갱신이 스스로 막히는 자기봉쇄가 생긴다.
_MARKET = {"latest": None, "cap": None, "checked_ts": 0.0}
_PRICE_LOCK = threading.Lock()
_PRICE_CALLS = {"external": 0, "skipped": 0}   # 관측용 카운터(외부 호출이 실제로 안 나가는지 입증)


def _expected_trade_day(now=None):
    """지금 시점에 '있어야 할 최신 종가'의 거래일(KST, YYYY-MM-DD).

    평일 16:00(KST) 이후면 오늘, 그 전이면 직전 영업일. 주말은 직전 금요일.
    공휴일은 판정하지 않는다(달력 없음) — 그 오차는 _MARKET 관측이 흡수한다."""
    now = now or datetime.now(_KST)
    d = now.date()
    after_close = (now.hour * 60 + now.minute) >= _MKT_CLOSE_MIN
    if not (d.weekday() < 5 and after_close):
        d -= _td(days=1)
    while d.weekday() >= 5:        # 토(5)·일(6) → 직전 금요일
        d -= _td(days=1)
    return d.isoformat()


def _fetch_price_with_asof(code, not_after=None):
    """외부 소스에서 (종가, 거래일, 소스명). 거래일을 못 주는 응답은 채택하지 않는다
    (신선도 판정이 날짜 기반이라 asof 없는 값은 이 설계에서 쓸 수 없다).
    어떤 예외도 밖으로 던지지 않는다.

    ★기준 거래일 컷: 어떤 소스든 장중에는 '오늘' 행을 내려주지만 그건 그 순간 체결가지
    확정 종가가 아니다. not_after(기본 _expected_trade_day())를 넘는 행은 채택하지 않고
    **직전 확정 종가**를 쓴다(analyst_collect._prices 와 동일 규칙). 인자를 안 주면 항상
    현재 기준일이 적용되므로 어떤 호출 경로도 필터를 우회할 수 없다."""
    not_after = not_after or _expected_trade_day()
    for name in _PRICE_CHAIN:
        cand = None
        try:
            if name == "toss":
                # 토스 일봉의 '기준일 이하' 마지막 종가. toss_data 는 읽기 전용 import(수정 금지).
                import price_source as _ps   # sys.path 에 kis-trading 을 넣어주는 역할도 겸함
                df = _ps._cut(_ps_candles(code), not_after)
                if df is not None and len(df):
                    close = float(df["close"].iloc[-1])
                    asof = _ps._asof_str(df.index[-1])
                    if close > 0:
                        cand = (close, asof, "toss")
            else:
                import price_source as _ps
                r = _ps.get_price(code, sources=(name,), not_after=not_after)
                if r and r.get("price") and r.get("asof"):
                    cand = (float(r["price"]), str(r["asof"]), name)
        except Exception as e:  # noqa: BLE001
            print(f"[price] {code} {name} 실패: {type(e).__name__}")
        if cand:
            if cand[1] <= not_after:      # 마지막 방어선(조용히 새어나가지 않게)
                return cand
            print(f"[price] {code} {name} 미확정 종가 {cand[1]} > 기준일 {not_after} → 기각")
    return None


def _ps_candles(code):
    """토스 일봉(읽기 전용). price_source import 로 sys.path 에 kis-trading 이 들어간 뒤에만
    유효하므로 별도 함수로 분리한다(없으면 예외 → 호출부가 다음 소스로 폴백)."""
    import toss_data
    return toss_data.candles(code, "1d")


# ---------------- 중간 거래일 backfill ----------------
# 문제: 서버는 '최신 확정 종가 1점'만 가져와 append 한다. 배치가 07-22 까지 쓰고
# 서버가 07-24 를 붙이면 07-23 은 다음 배치 성공까지 영원히 빈다(주 1회 배치라
# 최대 7일 구멍). 1점 조회 계약으로는 구조적으로 못 메운다.
# 해법: 저장값과 기준일 사이에 '빠졌을 수 있는 거래일'이 실제로 존재할 때만 구간을
# 1회 조회해 빈 날짜만 채운다. 무한 조회 방지 장치는 4중이다.
#   (a) 트리거 — 사이에 평일이 0일이면(금→월 등) 아예 조회하지 않는다
#   (b) 창 상한 — 소급 조회는 _PRICE_BACKFILL_DAYS 로 고정(+price_source 400일 하드캡)
#   (c) 호출 합류 — 기존 단일비행/쿨다운(_PRICE_RETRY_SEC) 경로를 그대로 탄다.
#       즉 구간 조회는 '이미 나가는 그 1회 조회'에 얹히지 별도 호출을 만들지 않는다
#   (d) 자기소멸 — 메우고 나면 (a) 가 거짓이 되어 다음부터 조회 자체가 안 나간다
_PRICE_BACKFILL_DAYS = int(os.getenv("GONGSI_PRICE_BACKFILL_DAYS", "45"))
# prices 배열 길이 상한(배치=100행). 배치가 장기 부재해도 서버 append 로 무한 성장하지
# 않게 tail 절단. 정상 운영(주 1회 배치 + 주 5행)에선 절대 닿지 않는다.
_PRICE_MAX_ROWS = int(os.getenv("GONGSI_PRICE_MAX_ROWS", "140"))


def _biz_days_between(a, b, cap=400):
    """a 초과 b 미만 구간의 평일 수. 인자가 날짜형식이 아니면 0. 루프는 cap 일로 상한.

    공휴일 달력이 없으므로 '평일=거래일'로 근사한다. 과대추정(휴장일을 구멍으로 오인)
    쪽으로만 틀리며, 그 경우 구간조회가 1회 헛돌 뿐 데이터는 변하지 않는다."""
    try:
        da = datetime.strptime(str(a)[:10], "%Y-%m-%d").date()
        db = datetime.strptime(str(b)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0
    n, d, i = 0, da + _td(days=1), 0
    while d < db and i < cap:
        if d.weekday() < 5:
            n += 1
        d += _td(days=1)
        i += 1
    return n


def _fetch_price_series(code, not_after):
    """확정 일봉 구간(기준일 컷 적용). 실패 시 []. 예외를 밖으로 던지지 않는다."""
    try:
        import price_source as _ps
        r = _ps.get_series(code, sources=_PRICE_CHAIN, not_after=not_after,
                           lookback_days=_PRICE_BACKFILL_DAYS)
        ser = [row for row in (r.get("series") or [])
               if len(row) >= 2 and str(row[0]) <= str(not_after)]   # 마지막 방어선
        return ser
    except Exception as e:  # noqa: BLE001
        print(f"[price] {code} 구간조회 실패(무시): {type(e).__name__}")
        return []


def _merge_series(prices, series, upto):
    """빠진 거래일만 채운다. 기존 행 값은 덮지 않는다(출처 혼선·불필요한 쓰기 방지).

    채우는 범위는 prices[0][0] 초과 ~ upto 이하로 제한한다. 앞쪽으로 확장하면
    window_start(=prices[0][0], avg_tp 집계창의 기준)가 움직여 컨센서스 지표가
    이유 없이 변한다. 저장된 prices 가 비어 있으면 아무것도 하지 않는다 —
    최초 적재는 배치의 몫이고, 서버가 짧은 창을 새로 만들면 안 된다."""
    if not prices or not series:
        return prices
    start = str(prices[0][0])
    have = {str(p[0]) for p in prices}
    add = [[str(d), int(v)] for d, v in series
           if start < str(d) <= str(upto) and str(d) not in have]
    if not add:
        return prices
    out = sorted(list(prices) + add, key=lambda p: str(p[0]))
    if len(out) > _PRICE_MAX_ROWS:
        out = out[-_PRICE_MAX_ROWS:]
    print(f"[price] backfill {len(add)}행 채움({add[0][0]}~{add[-1][0]}) "
          f"→ prices {len(prices)}→{len(out)}행")
    return out


def _price_worker(code, job, want_series=False):
    """단일 종목 외부 조회 1회(백그라운드 스레드). 결과를 오버레이·시장관측에 반영.

    want_series=True 면 같은 조회 기회에 구간까지 받아 중간 거래일 구멍을 메운다."""
    res = None
    cap = _expected_trade_day()      # 이 관측에 적용한 기준일 컷(관측과 함께 기록해야 의미가 산다)
    try:
        res = _fetch_price_with_asof(code, cap)
    except Exception as e:  # noqa: BLE001
        print(f"[price] {code} worker 예외: {type(e).__name__}")
    series = _fetch_price_series(code, cap) if (res and want_series) else []
    try:
        if res:
            price, asof, source = res
            with _PRICE_LOCK:
                _PRICE_FRESH[code] = {"price": price, "asof": asof, "source": source,
                                      "series": series, "ts": time.time()}
                if not _MARKET["latest"] or asof > _MARKET["latest"]:
                    _MARKET["latest"] = asof
                _MARKET["cap"] = cap
                _MARKET["checked_ts"] = time.time()
    finally:
        job["res"] = res
        job["ev"].set()


def _run_price_refresh(code, wait, want_series=False):
    """단일비행(single-flight) 갱신. 진행 중이면 새 호출 없이 합류, 쿨다운 중이면 호출 안 함.
    wait 초까지만 기다리고 그 뒤엔 None(=호출자는 기존값으로 응답, 워커는 계속 진행).

    want_series 는 새로 띄우는 워커에만 전달된다 — 이미 진행 중인 조회에 합류한
    경우엔 추가 호출을 만들지 않는다(구멍은 다음 갱신 기회에 메워진다)."""
    now = time.time()
    with _PRICE_LOCK:
        job = _PRICE_JOBS.get(code)
        if job is not None and not job["ev"].is_set():
            pass                                   # 진행 중 → 합류(외부 호출 추가 없음)
        else:
            if now - _PRICE_ATTEMPT.get(code, 0.0) < _PRICE_RETRY_SEC:
                _PRICE_CALLS["skipped"] += 1
                return None                        # 쿨다운 → 외부 호출 안 함
            _PRICE_ATTEMPT[code] = now
            job = {"ev": threading.Event(), "res": None}
            _PRICE_JOBS[code] = job
            _PRICE_CALLS["external"] += 1
            print(f"[price] {code} 외부조회 시작 (누적 external={_PRICE_CALLS['external']}, "
                  f"skipped={_PRICE_CALLS['skipped']})")
            threading.Thread(target=_price_worker, args=(code, job, want_series),
                             daemon=True).start()
    job["ev"].wait(timeout=max(0.0, wait))
    return job["res"] if job["ev"].is_set() else None


def _apply_price(payload, price, asof, source, series=None):
    """갱신된 종가를 payload 에 반영. prices 궤적도 함께 맞춰 프런트의 기준일 표기
    (prices[-1][0])와 값이 어긋나지 않게 한다. 원본 캐시 dict 는 건드리지 않는다(복사).

    series 가 있으면 먼저 중간 거래일 구멍을 메운다(③). 마지막 1점만 갱신/append 하던
    종전 동작은 그대로 남는다 — backfill 은 그 앞단에 얹히는 보강이다."""
    out = dict(payload)
    prices = list(out.get("prices") or [])
    ival = int(round(price))
    if series:
        prices = _merge_series(prices, series, asof)
    if prices and str(prices[-1][0]) == asof:
        prices[-1] = [asof, ival]
    elif not prices or asof > str(prices[-1][0]):
        prices.append([asof, ival])
    out["prices"] = prices
    out["current"] = ival
    out["current_asof"] = asof
    out["current_source"] = source
    return out


def _drop_unconfirmed(payload, exp):
    """기준 거래일(exp)을 넘는 값 = 장중 미확정 체결가 → 종가로 내보내지 않는다.

    저장(Supabase/로컬)된 값에도 과거 폴백이 남긴 미확정 행이 있을 수 있으므로 응답
    경로에서 한 번 더 거른다(코드만 고치면 이미 저장된 오염은 계속 표기된다).
    직전 확정 종가가 남아 있으면 그것으로 되돌리고, 남는 게 없으면 값 없이 둔다 —
    그 뒤 신선도 로직이 확정 종가를 가져와 채운다. 원본 dict 는 건드리지 않는다."""
    prices = payload.get("prices") or []
    over = [p for p in prices if str(p[0]) > exp]
    cur_over = str(payload.get("current_asof") or "") > exp
    if not over and not cur_over:
        return payload
    kept = [p for p in prices if str(p[0]) <= exp]
    out = dict(payload)
    out["prices"] = kept
    if kept:
        out["current"] = kept[-1][1]
        out["current_asof"] = str(kept[-1][0])
        out.pop("current_source", None)      # 되돌린 값의 출처는 알 수 없다 → 주장하지 않음
    else:
        out["current"] = None
        out["current_asof"] = None
        out.pop("current_source", None)
    print(f"[price] {payload.get('code')} 미확정행 {len(over)}건 제외(기준일 {exp}) → "
          f"current={out['current']} asof={out['current_asof']}")
    return out


def _ensure_fresh_price(payload):
    """저장값이 최신 거래일 종가가 아니면 서버가 그 자리에서 갱신한다.
    최신이면 외부 호출 0. 실패·지연 시에도 기존 값으로 정상 응답한다(화면 안 멈춤)."""
    try:
        code = norm_stock_code(payload.get("code"))
        if not code:
            return payload
        exp = _expected_trade_day()
        payload = _drop_unconfirmed(payload, exp)     # ★ 저장된 장중값 무력화(응답 경로 방어)
        prices = payload.get("prices") or []
        asof = str(prices[-1][0]) if prices else str(payload.get("updated_at") or "")
        if asof > exp:
            asof = ""       # 수집일(updated_at)이 기준일보다 뒤 → 신선도 근거로 쓸 수 없다

        # 0) 이전 요청에서 백그라운드로 끝난 갱신결과가 있으면 먼저 반영
        with _PRICE_LOCK:
            ov = _PRICE_FRESH.get(code)
        if ov and ov["asof"] <= exp and (not asof or ov["asof"] >= asof):
            payload = _apply_price(payload, ov["price"], ov["asof"], ov["source"],
                                   ov.get("series"))
            asof = ov["asof"]

        if asof and asof >= exp:
            _PRICE_CALLS["skipped"] += 1
            payload.setdefault("current_asof", asof)
            return payload                                   # 최신 → 외부 호출 0

        # 1) 휴장일 흡수: 시장이 실제로 내놓은 최신 거래일을 최근에 관측했고,
        #    저장값이 이미 그 수준이면 더 새 것은 존재하지 않는다 → 호출 0
        #
        #    ★ 단, 그 관측이 '더 새 것이 없다'의 증거가 되려면 두 조건이 필요하다.
        #    (a) mk_latest < mk_cap — 컷에 눌려서 나온 값(latest == cap)은 소스에 더 새
        #        데이터가 있어도 그렇게 보일 뿐이라 증거가 못 된다. 이걸 증거로 쓰면
        #        마감(16:00) 후 당일 종가 갱신이 다음 관측 TTL 만료까지 막히고, 조회가
        #        막히니 _MARKET 도 갱신되지 않는 자기봉쇄가 된다.
        #    (b) exp <= mk_cap — 그 증거는 관측 당시 컷까지만 커버한다. 기준일이 그보다
        #        앞서 나갔으면(예: 휴장일 관측이 다음 거래일 마감 후까지 이월) 무효다.
        with _PRICE_LOCK:
            mk_latest, mk_cap, mk_ts = _MARKET["latest"], _MARKET["cap"], _MARKET["checked_ts"]
        if (asof and mk_latest and asof >= mk_latest
                and mk_cap and mk_latest < mk_cap and exp <= mk_cap
                and (time.time() - mk_ts) < _MARKET_TTL_SEC):
            _PRICE_CALLS["skipped"] += 1
            payload.setdefault("current_asof", asof)
            return payload

        # 2) 갱신 필요 → 단일비행 조회(대기 상한 내에서만 동기 반영)
        #    저장 마지막 거래일과 기준일 사이에 평일이 남아 있으면 = 중간 거래일이
        #    빠졌을 수 있다 → 같은 조회 기회에 구간까지 받아 메운다(③).
        want_series = bool(payload.get("prices")) and _biz_days_between(asof, exp) > 0
        res = _run_price_refresh(code, _PRICE_SYNC_WAIT, want_series)
        if res:
            price, new_asof, source = res
            if not asof or new_asof >= asof:
                with _PRICE_LOCK:
                    ser = (_PRICE_FRESH.get(code) or {}).get("series") or []
                payload = _apply_price(payload, price, new_asof, source, ser)
                _persist_price_async(payload)
        payload.setdefault("current_asof", asof or None)
        return payload
    except Exception as e:  # noqa: BLE001
        print(f"[price] ensure_fresh 예외(기존값 유지): {type(e).__name__} {e}")
        return payload


def _persist_price_async(payload):
    """영속을 요청 경로에서 떼어낸다.

    단일비행으로 합류한 동시요청은 **모두 같은 결과를 받으므로** 그대로 두면 요청 수만큼
    upsert 가 발생한다(20 동시요청 → 20 upsert, 실측 응답 6.5s). (code, 거래일) 당 1회로
    묶고 백그라운드로 보내 응답 지연과 쓰기 증폭을 함께 없앤다."""
    try:
        code = str(payload.get("code") or "")
        asof = str(payload.get("current_asof") or "")
        exp = _expected_trade_day()
        if asof > exp or any(str(p[0]) > exp for p in (payload.get("prices") or [])):
            print(f"[price] {code} 미확정 종가(asof={asof} > 기준일 {exp}) → 영속 안 함")
            return          # 장중값은 DB 에 '종가'로 남기지 않는다(오염 재발 차단)
        with _PRICE_LOCK:
            if not code or _PRICE_PERSISTED.get(code) == asof:
                return
            _PRICE_PERSISTED[code] = asof
        threading.Thread(target=_persist_price, args=(payload,), daemon=True).start()
    except Exception as e:  # noqa: BLE001
        print(f"[price] 영속 스케줄 실패(무시): {type(e).__name__}")


def _persist_price(payload):
    """갱신분 Supabase 영속(best-effort). 쓰기 권한이 없어도 서버 메모리 갱신만으로
    동작하므로 실패는 무시한다(조용한 실패가 아니라 로그로 드러냄)."""
    try:
        row = dict(payload)
        # ★n_brokers·brokers 는 «응답 전용 파생값»이다(reports 에서 유도). 저장행에 섞어
        #   보내면 Supabase 에 해당 컬럼이 없을 때 PostgREST 400 → 종가 영속이 통째로
        #   죽는다. 파생값은 여기서 반드시 떨어뜨린다(배치가 컬럼을 채우는 것과 별개).
        for k in ("disclaimer", "cached", "current_asof", "current_source",
                  "n_brokers", "brokers"):
            row.pop(k, None)
        miri_cache.upsert("analyst_consensus", [row], on_conflict="code")
    except Exception as e:  # noqa: BLE001
        print(f"[price] supabase 영속 실패(무시): {type(e).__name__}")


@api.get("/api/analyst")
def get_analyst(request: Request, code: str = ""):
    """②종목 애널리스트 전망. 리포트/목표가는 배치 캐시(Supabase→로컬 폴백),
    **종가만** 서버가 신선도를 판정해 필요할 때 갱신한다(최신이면 외부 호출 0).

    미수집/미존재 코드는 200 graceful(cached:false, 빈 reports/prices)."""
    raw_code = (code or "").strip()
    code = norm_stock_code(raw_code)      # 대문자 정규화(0126z0 → 0126Z0)
    if not code:
        return _json_cached(request, _empty_analyst(raw_code, None))
    now = time.time()
    try:
        with _MIRI_LOCK:
            ent = _ANALYST_MEM["data"].get(code)
            if ent and (now - ent[0]) < _MIRI_TTL_SEC:
                # 리포트/목표가는 캐시 그대로, 종가만 신선도 판정(최신이면 외부 호출 0)
                return _json_cached(request, _ensure_fresh_price(ent[1]))
        payload = None
        # 1) Supabase 우선
        try:
            ok, row = miri_cache.select_one("analyst_consensus", code)
            if ok and row:
                payload = _analyst_from_row(row)
        except Exception as e:  # noqa: BLE001
            print(f"[analyst] supabase read 폴백: {type(e).__name__}")
        # 2) 로컬 JSON 폴백
        if payload is None:
            cache = miri_cache.load_json(_ANALYST_CACHE_FILE, default={}) or {}
            row = cache.get(code) if isinstance(cache, dict) else None
            if row:
                payload = _analyst_from_row(row)
        # 3) 미수집 → graceful 빈-정형
        if payload is None:
            payload = _empty_analyst(code, _corp_name(code))
        with _MIRI_LOCK:
            _ANALYST_MEM["data"][code] = (now, payload)   # 배치분(리포트/목표가) 캐시
        return _json_cached(request, _ensure_fresh_price(payload))
    except Exception as e:  # noqa: BLE001
        print(f"[analyst] 예외 폴백: {type(e).__name__} {e}")
        return _json_cached(request, _empty_analyst(code, None))


def _broker_key(name):
    """증권사명 정규화 키. 배치(analyst_collect._broker_key)와 «같은 규칙»이어야 한다.

    공백 정리 + 대문자화까지만('iM증권'/'IM 증권' 표기 흔들림 흡수). '증권'·'투자증권'
    접미사 제거 같은 «적극 정규화»는 금지 — 서로 다른 법인을 합치면 고유 증권사 수가
    «과소»계상돼 대표성이 실제보다 좋아 보인다. 그 방향의 오차가 가장 위험하다."""
    return " ".join(str(name or "").split()).upper()


def _broker_labels(reports):
    """목표가 리포트 → (고유 증권사 수, 표시용 증권사명 목록).

    목표가가 «있는» 리포트만 센다(avg_tp 를 만든 모집단과 같은 분모여야 하므로).
    저장 컬럼(n_brokers)이 아직 없어도 reports(jsonb)만으로 유도된다 = 마이그레이션 불요.
    [실측 2026-08-16] 100종목 전수에서 reports 재계산 == 배치 계산값, 불일치 0건."""
    label = {}
    for r in reports:
        if not isinstance(r, dict):
            continue
        try:
            tp = int(r.get("target_price") or 0)
        except (TypeError, ValueError):
            continue
        if tp <= 0:
            continue
        label.setdefault(_broker_key(r.get("broker")), str(r.get("broker") or "").strip())
    return len(label), sorted(v for v in label.values())


def _analyst_from_row(row):
    """저장행(Supabase/로컬) → 응답 payload(계약 준수). 결측은 안전 기본값."""
    prices = row.get("prices") or []
    reports = row.get("reports") or []
    if not isinstance(prices, list):
        prices = []
    if not isinstance(reports, list):
        reports = []
    # ★추가(2026-08-16 President 지시 「레포트별로 나타나게 해줘」의 데이터 근거).
    #   avg_tp 는 「컨센서스」로 읽히는데 그 값을 «몇 곳»이 만들었는지 화면에서 볼 수 없었다.
    #   n_total 로는 판별 불가 — 코웨이는 n_total=13 인데 증권사는 «1곳»이다.
    #   기존 필드(avg_tp·n_total·n_tp)의 «의미는 불변». 추가만 한다.
    nb = row.get("n_brokers")
    brokers = row.get("brokers")
    if nb is None or not isinstance(brokers, list) or not brokers:
        nb_calc, brokers_calc = _broker_labels(reports)
        if nb is None:
            nb = nb_calc
        if not isinstance(brokers, list) or not brokers:
            brokers = brokers_calc
    return {
        "code": str(row.get("code") or ""),
        "name": row.get("name"),
        "cached": True,
        "current": row.get("current"),
        "avg_tp": row.get("avg_tp"),
        "n_total": int(row.get("n_total") or 0),
        "n_tp": int(row.get("n_tp") or 0),
        "n_brokers": int(nb or 0),
        "brokers": [str(b) for b in brokers],
        "window_start": row.get("window_start"),
        "updated_at": row.get("updated_at"),
        "prices": prices,
        "reports": reports,
        "disclaimer": _ANALYST_DISCLAIMER,
    }


def _empty_top100():
    return {"updated_at": None, "count": 0, "items": []}


@api.get("/api/top100")
def get_top100(request: Request):
    """시총 Top100(캐시 전용). Supabase market_cap_top100 우선 → 로컬 폴백. 라이브콜 0."""
    now = time.time()
    try:
        with _MIRI_LOCK:
            if _TOP100_MEM["data"] is not None and (now - _TOP100_MEM["ts"]) < _MIRI_TTL_SEC:
                return _json_cached(request, _TOP100_MEM["data"])
        data = None
        # 1) Supabase 우선(rank 오름차순)
        try:
            ok, rows = miri_cache.select_all("market_cap_top100", order="rank.asc")
            if ok and rows:
                data = _top100_from_rows(rows)
        except Exception as e:  # noqa: BLE001
            print(f"[top100] supabase read 폴백: {type(e).__name__}")
        # 2) 로컬 JSON 폴백
        if data is None:
            snap = miri_cache.load_json(_TOP100_FILE, default=None)
            if isinstance(snap, dict) and snap.get("items"):
                items = _sanitize_top100_items(snap.get("items") or [])
                data = {"updated_at": snap.get("updated_at"),
                        "count": len(items), "items": items}
        if data is None:
            data = _empty_top100()
        with _MIRI_LOCK:
            _TOP100_MEM["data"] = data
            _TOP100_MEM["ts"] = now
        return _json_cached(request, data)
    except Exception as e:  # noqa: BLE001
        print(f"[top100] 예외 폴백: {type(e).__name__} {e}")
        return _json_cached(request, _empty_top100())


def _sanitize_top100_items(rows):
    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        items.append({
            "rank": int(r.get("rank") or 0),
            "code": str(r.get("code") or ""),
            "name": r.get("name"),
            "market": r.get("market"),
            "market_cap": int(r.get("market_cap") or 0),
            "cap_label": r.get("cap_label"),
        })
    items.sort(key=lambda x: x["rank"])
    return items


def _top100_from_rows(rows):
    items = _sanitize_top100_items(rows)
    updated = None
    for r in rows:
        if isinstance(r, dict) and r.get("updated_at"):
            updated = r.get("updated_at")
            break
    return {"updated_at": updated, "count": len(items), "items": items}


# ---------------- 실적발표(정기보고서) 예상 캘린더 (읽기 전용, 라이브콜 0) ----------------
# 소스: features/earnings_calendar 산출물(out/earnings_calendar.json). 과거 DART 정기보고서
# 접수 '계절성(결산기말→접수지연 median)'으로 추정한 다음 예상 발표일(코스피·코스닥 전시장).
# 요청경로에서 DART/외부 라이브콜 0(순수 파일 read). 미래(오늘 이후) 예측만 정형 items 반환.
# 어떤 예외에도 500 금지(200 + 빈-정형). top100/analyst read-only 패턴 미러.
# 배포 정합성: features/.../out/ 은 .gitignore(out/) 대상이라 배포에 안 실린다. 배포시엔 시드로
# data/earnings_calendar.json(추적 가능 경로, gitignore 예외 필요)을 우선 조회하고, 로컬/개발에선
# features 산출물로 폴백한다. 둘 다 없으면 200 빈-정형(500 금지).
_EARN_FILE_SEED = config.DATA / "earnings_calendar.json"
_EARN_FILE_DEV = Path(__file__).parent / "features" / "earnings_calendar" / "out" / "earnings_calendar.json"
_EARN_MEM = {"ts": 0.0, "data": None}


def _build_earnings_calendar():
    import datetime as _dt
    today = _dt.date.today().isoformat()
    snap = miri_cache.load_json(_EARN_FILE_SEED, default=None)
    if not (isinstance(snap, dict) and snap.get("predictions")):
        snap = miri_cache.load_json(_EARN_FILE_DEV, default={}) or {}
    preds = snap.get("predictions") if isinstance(snap, dict) else None
    items = []
    for p in (preds or []):
        if not isinstance(p, dict):
            continue
        d = p.get("predicted_date")
        code = str(p.get("stock_code") or "")
        if not d or not code or d < today:   # 미래 예측만(과거 추정 제외)
            continue
        items.append({
            "date": d,
            "stock_code": code,
            "corp_name": p.get("corp_name"),
            "market": p.get("market"),
            "report_nm": p.get("target_type"),
            "target_period": p.get("target_period"),
            "confidence": p.get("confidence"),
            "kind": "earn",
        })
    items.sort(key=lambda x: (x["date"], x.get("corp_name") or ""))
    return {"count": len(items), "items": items,
            "as_of": (snap.get("as_of") if isinstance(snap, dict) else None),
            "market_scope": "코스피·코스닥",
            "disclaimer": "과거 정기보고서 접수 계절성으로 추정한 예상 발표일입니다(실제일과 다를 수 있음)."}


@api.get("/api/earnings-calendar")
def get_earnings_calendar(request: Request):
    """④캘린더 '실적발표' 유형(캐시/파일 전용). 미래 예상 발표일만. 라이브콜 0, 500 금지."""
    now = time.time()
    try:
        with _MIRI_LOCK:
            if _EARN_MEM["data"] is not None and (now - _EARN_MEM["ts"]) < _MIRI_TTL_SEC:
                return _json_cached(request, _EARN_MEM["data"])
        data = _build_earnings_calendar()
        with _MIRI_LOCK:
            _EARN_MEM["data"] = data
            _EARN_MEM["ts"] = now
        return _json_cached(request, data)
    except Exception as e:  # noqa: BLE001
        print(f"[earnings-cal] 예외 폴백: {type(e).__name__} {e}")
        return _json_cached(request, {"count": 0, "items": [], "market_scope": "코스피·코스닥"})


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


# ---------------- [42] 정적자산 Cache-Control(경로기반) ----------------
# StaticFiles 는 ETag/Last-Modified 를 주지만 Cache-Control 은 안 준다. 경로별로:
#   - 불변 자산(이미지/아이콘/폰트/splash) = 1년 immutable(재검증 0).
#   - HTML/JS/manifest/sw.js = no-cache(항상 재검증 → StaticFiles ETag 로 대개 304).
#     ※ JS/sw 는 콘텐츠 해시 파일명이 아니고 SW precache+bump(모바일 소유)가 신선도를
#       담당하므로, HTTP 는 안전하게 no-cache(재검증)로 둬 stale JS 배포사고를 차단.
# /api/* 와 /.well-known 은 각 라우트가 헤더를 직접 관리하므로 건드리지 않는다.
_IMMUTABLE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico",
                  ".woff", ".woff2", ".ttf", ".otf")
_NOCACHE_EXACT = ("/manifest.json",)


@api.middleware("http")
async def _static_cache_headers(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/api") or path.startswith("/.well-known"):
        return resp
    if "cache-control" in (k.lower() for k in resp.headers.keys()):
        return resp  # 라우트가 이미 설정(중복/충돌 방지)
    lower = path.lower()
    if lower.endswith(_IMMUTABLE_EXT) or lower.startswith("/splash/") \
            or lower.startswith("/icons/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path == "/" or lower.endswith(".html") or lower.endswith(".js") \
            or path in _NOCACHE_EXACT or lower.endswith("/sw.js"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


api.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
