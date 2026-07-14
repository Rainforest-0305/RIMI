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
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import dart_poll
import dedup  # 중복 이벤트(결정/결과·정정/원본) 접기
import impact
import scale_extract  # 규모보정 온디맨드 조회(/api/scale)
from summarize import summarize
import main as core  # load_watchlist / load_seen 재사용

api = FastAPI(title="미리(MIRI) 공시앱 API", version="2.0")

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


def _build_feed(force: bool = False) -> dict:
    """KOSPI+KOSDAQ 시장 전체 최근 공시를 조회·요약·과거영향 매핑해 피드로 만든다.
    개별 공시 하나가 malformed 여도 그 항목만 건너뛰고 피드 전체는 살린다."""
    stocks, keywords = core.load_watchlist()
    watched_codes = {s.get("stock_code", "") for s in stocks}
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
                "impact": impact.impact_for_tags(res["tags"]),
                "url": dart_poll.dart_url(rno),
                "is_new": rno not in seen,
                "is_watched": bool(code) and code in watched_codes,
            })
        except Exception as e:
            # malformed 공시 1건이 피드 전체를 깨지 못하게 격리(로그만).
            print(f"[feed] skip malformed item {it.get('rcept_no','?') if isinstance(it, dict) else '?'}: {e}")
            continue

    # 중복 이벤트 dedup: 같은 기업의 사실상 같은 사건(결정↔결과, 정정↔원본, 부수공시)
    # 을 묶어 정보량 큰 대표 1건만 남긴다(규칙: dedup.py). 정렬 전에 접는다.
    items = dedup.dedup(items)

    # 정렬: 관심종목(watchlist) 소속 공시를 최상단 → 그 아래 최신순(접수일+접수번호 desc).
    # is_watched 를 1/0 으로 최우선 키로 두고 전부 내림차순: 관심종목 그룹이 먼저,
    # 각 그룹 안에서 최신 공시가 위로.
    items.sort(key=lambda x: (1 if x.get("is_watched") else 0,
                              x.get("rcept_dt", ""), x.get("rcept_no", "")),
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

    return {
        "count": len(items),
        "market": "KOSPI+KOSDAQ",
        "stocks": stocks,
        "keywords": keywords,
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


# ---------------- 워치리스트 원자적 저장 ----------------
def _save_watchlist(stocks, keywords):
    payload = {
        "_comment": "관심종목. stock_code=6자리. keywords=제목 부분매칭 추가 알림(선택).",
        "stocks": stocks,
        "keywords": keywords,
    }
    tmp = config.WATCHLIST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, config.WATCHLIST_FILE)  # 원자적 교체


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
def get_watchlist():
    stocks, keywords = core.load_watchlist()
    return {"stocks": stocks, "keywords": keywords}


class WatchAdd(BaseModel):
    name: str | None = None
    stock_code: str | None = None


@api.post("/api/watchlist")
def add_watchlist(body: WatchAdd):
    raw = (body.stock_code or body.name or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="종목명 또는 종목코드를 입력하세요.")

    stocks, keywords = core.load_watchlist()

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

    stocks.append({"name": name or code, "stock_code": code})
    _save_watchlist(stocks, keywords)
    _FEED_CACHE["data"] = None  # 다음 조회 시 재구성(is_watched 갱신)
    return {"ok": True, "stocks": stocks, "keywords": keywords}


@api.delete("/api/watchlist/{code}")
def delete_watchlist(code: str):
    stocks, keywords = core.load_watchlist()
    new_stocks = [s for s in stocks if s.get("stock_code") != code]
    if len(new_stocks) == len(stocks):
        raise HTTPException(status_code=404, detail=f"등록되지 않은 종목: {code}")
    _save_watchlist(new_stocks, keywords)
    _FEED_CACHE["data"] = None
    return {"ok": True, "stocks": new_stocks, "keywords": keywords}


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
    - 랭킹: name 이 q로 시작 > code 가 q로 시작 > 그 외 contains. 동순위는 name 오름차순.
    - 상한 30건. count = 반환 results 길이. 어떤 입력에도 500 금지(예외는 빈결과 폴백).
    """
    try:
        query = (q or "").strip()
        if not query:
            return {"query": "", "count": 0, "results": []}

        ql = query.lower()
        index = _load_corp_index()

        matched = []  # (rank, name, row)
        for r in index:
            name = r["name"]
            code = r["code"]
            nl = r["_nl"]
            name_hit = ql in nl
            code_hit = query in code
            if not (name_hit or code_hit):
                continue
            if nl.startswith(ql):
                rank = 0
            elif code.startswith(query):
                rank = 1
            else:
                rank = 2
            matched.append((rank, name, code, r["market"]))

        # 랭크 오름차순 -> 같은 랭크 내 name 오름차순
        matched.sort(key=lambda t: (t[0], t[1]))
        top = matched[:_SEARCH_LIMIT]
        results = [{"name": n, "code": c, "market": m} for (_, n, c, m) in top]
        return {"query": query, "count": len(results), "results": results}
    except Exception as e:
        # 어떤 예외에도 500 금지: 200 + 빈결과 폴백.
        print(f"[search] 예외 폴백: {e}")
        return {"query": (q or "").strip(), "count": 0, "results": []}


# ---------------- 정적 프론트엔드(web/) 마운트 (마지막에) ----------------
_WEB_DIR = Path(__file__).parent / "web"
api.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
