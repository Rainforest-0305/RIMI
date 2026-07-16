# -*- coding: utf-8 -*-
"""관심종목 영속 스토어 (그룹 / 종목 / 키워드).

목적:
  watchlist.json 단일 파일은 Render 재배포 시 휘발한다. 이 모듈은 저장/조회를
  하나의 추상 레이어로 감싸, 조건이 되면 Supabase(PostgREST)로 영속하고,
  아니면 JSON 파일로 graceful 폴백한다. app.py / main.py 는 오직
  load_watch_state() / save_watch_state() 만 통해 상태에 접근한다.

백엔드 선택:
  - Supabase : config.SUPABASE_URL + 키(SERVICE_ROLE 우선, 없으면 ANON)가 있고
               연결 가능하면 사용.
  - JSON 폴백: 위 조건 불충족 / 네트워크·스키마 장애 시 watchlist.json
               (groups 포함 확장 스키마). 로컬·키없음·장애에서도 poll_once 가
               계속 동작하도록 반드시 유지한다.

보안:
  - 키는 절대 로그 / 응답 / 예외 메시지에 노출하지 않는다. os.environ 참조만
    (config 경유, config 는 os.getenv 만 사용). 폴백 로그는 예외 '타입명'만 남긴다.

상태 스키마(정규화 후):
  {
    "groups":  [{"id": str, "name": str, "order": int}],   # id="default"는 시스템 기본(삭제 불가)
    "stocks":  [{"name": str, "stock_code": str, "group": str, "order": int}],
    "keywords": [str],
  }
"""
import json
import os

import config

try:  # requests 는 이미 requirements 에 존재(무추가). 없더라도 JSON 폴백은 동작.
    import requests
except Exception:  # pragma: no cover
    requests = None


DEFAULT_GROUP_ID = "default"
DEFAULT_GROUP_NAME = "기본"

_HTTP_TIMEOUT = 8  # 초. Supabase REST 호출 상한(요청 블로킹 방지).


# ============================================================
# 정규화 / 마이그레이션
# ============================================================
def _as_int(v, default):
    try:
        if isinstance(v, bool):
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def normalize_state(raw):
    """임의 raw dict 를 표준 스키마로 정규화 + 마이그레이션.

    - groups 없으면 default 그룹 생성. default 는 항상 존재하도록 보장.
    - group 필드 없는(구) 종목은 default 로, order 없으면 배열인덱스로 마이그레이션.
    - order 는 그룹 내에서 0..n-1 로 재부여(빈틈/중복 정리).
    - 중복 그룹 id / 중복 stock_code / 중복 keyword 제거.
    """
    raw = raw or {}
    groups_in = raw.get("groups") or []
    stocks_in = raw.get("stocks") or []
    keywords_in = raw.get("keywords") or []

    # ---- groups ----
    groups = []
    seen_gids = set()
    for i, g in enumerate(groups_in):
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "").strip()
        if not gid or gid in seen_gids:
            continue
        name = str(g.get("name") or "").strip() or gid
        order = _as_int(g.get("order"), i)
        groups.append({"id": gid, "name": name, "order": order})
        seen_gids.add(gid)
    if DEFAULT_GROUP_ID not in seen_gids:
        # 기본 그룹은 항상 맨 앞(order 최소)으로 삽입
        groups.insert(0, {"id": DEFAULT_GROUP_ID, "name": DEFAULT_GROUP_NAME,
                          "order": -1})
        seen_gids.add(DEFAULT_GROUP_ID)
    groups.sort(key=lambda g: (g["order"], g["id"]))
    for i, g in enumerate(groups):
        g["order"] = i
    valid_gids = {g["id"] for g in groups}

    # ---- stocks ----
    stocks = []
    seen_codes = set()
    for i, s in enumerate(stocks_in):
        if not isinstance(s, dict):
            continue
        code = str(s.get("stock_code") or "").strip()
        if not code or code in seen_codes:
            continue
        name = str(s.get("name") or "").strip() or code
        grp = str(s.get("group") or "").strip()
        if grp not in valid_gids:
            grp = DEFAULT_GROUP_ID
        order = _as_int(s.get("order"), i)
        stocks.append({"name": name, "stock_code": code,
                       "group": grp, "order": order})
        seen_codes.add(code)
    # 그룹 내 order 재부여(0..n-1)
    by_group = {}
    for s in stocks:
        by_group.setdefault(s["group"], []).append(s)
    for lst in by_group.values():
        lst.sort(key=lambda s: (s["order"], s["stock_code"]))
        for i, s in enumerate(lst):
            s["order"] = i

    # ---- keywords ----
    keywords = []
    for k in keywords_in:
        ks = str(k).strip()
        if ks and ks not in keywords:
            keywords.append(ks)

    return {"stocks": stocks, "keywords": keywords, "groups": groups}


# ============================================================
# Supabase(PostgREST) 백엔드
# ============================================================
def _supabase_conf():
    url = (getattr(config, "SUPABASE_URL", "") or "").strip()
    key = ((getattr(config, "SUPABASE_SERVICE_ROLE", "") or "").strip()
           or (getattr(config, "SUPABASE_ANON_KEY", "") or "").strip())
    return url, key


def supabase_enabled():
    """Supabase 백엔드 활성 여부.

    안전 기본값: config.WATCH_BACKEND='json'(기본)이면 키가 있어도 절대 활성화
    하지 않는다 → 로컬/개발에서 실 Supabase 오접속 방지(Partner 게이트).
    'supabase' 명시 또는 'auto'(+키존재)일 때만 True. (연결 성공은 호출 시 판정)
    """
    mode = (getattr(config, "WATCH_BACKEND", "json") or "json").strip().lower()
    if mode == "json":
        return False
    url, key = _supabase_conf()
    if not (url and key and requests is not None):
        return False
    return mode in ("supabase", "auto")


def _supabase_rest():
    url, key = _supabase_conf()
    base = url.rstrip("/") + "/rest/v1"
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    return base, headers


def _sb_get(table, base, headers):
    r = requests.get(base + "/" + table + "?select=*",
                     headers=headers, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _sb_delete_all(table, pk, base, headers):
    # PostgREST 는 삭제에 필터가 필수 → pk not null(=전체 매칭)로 전삭.
    r = requests.delete(base + "/" + table + "?" + pk + "=not.is.null",
                        headers=headers, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()


def _sb_insert(table, rows, base, headers):
    if not rows:
        return
    h = dict(headers)
    h["Prefer"] = "return=minimal"
    r = requests.post(base + "/" + table, headers=h,
                      data=json.dumps(rows), timeout=_HTTP_TIMEOUT)
    r.raise_for_status()


def _supabase_load():
    base, headers = _supabase_rest()
    groups = _sb_get("watch_groups", base, headers)
    stocks = _sb_get("watch_stocks", base, headers)
    keywords = _sb_get("watch_keywords", base, headers)
    raw = {
        "groups": [{"id": g.get("id"), "name": g.get("name"),
                    "order": g.get("sort_order", 0)} for g in groups],
        "stocks": [{"name": s.get("name"), "stock_code": s.get("stock_code"),
                    "group": s.get("group_id") or DEFAULT_GROUP_ID,
                    "order": s.get("sort_order", 0)} for s in stocks],
        "keywords": [r.get("keyword") for r in keywords if r.get("keyword")],
    }
    return normalize_state(raw)


def _supabase_save(state):
    """스냅샷 전체 교체(데이터 소량 → delete-all + bulk insert 로 단순·정합).

    순서: 자식(stocks) 먼저 지우고 부모(groups) 삭제 → FK 있어도 안전.
    삽입은 groups 먼저(부모) → stocks(자식).
    """
    base, headers = _supabase_rest()
    _sb_delete_all("watch_stocks", "stock_code", base, headers)
    _sb_delete_all("watch_keywords", "keyword", base, headers)
    _sb_delete_all("watch_groups", "id", base, headers)

    _sb_insert("watch_groups",
               [{"id": g["id"], "name": g["name"], "sort_order": g["order"]}
                for g in state["groups"]], base, headers)
    _sb_insert("watch_stocks",
               [{"stock_code": s["stock_code"], "name": s["name"],
                 "group_id": s["group"], "sort_order": s["order"]}
                for s in state["stocks"]], base, headers)
    _sb_insert("watch_keywords",
               [{"keyword": k} for k in state["keywords"]], base, headers)


# ============================================================
# JSON 폴백 백엔드
# ============================================================
def _json_load():
    f = config.WATCHLIST_FILE
    if not f.exists():
        return normalize_state({})
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    return normalize_state(raw)


def _json_save(state):
    payload = {
        "_comment": ("관심종목. stock_code=6자리. group=소속그룹 id. "
                     "keywords=제목 부분매칭 추가 알림(선택)."),
        "groups": state["groups"],
        "stocks": state["stocks"],
        "keywords": state["keywords"],
    }
    tmp = config.WATCHLIST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, config.WATCHLIST_FILE)  # 원자적 교체(패턴: 트레이딩 상태파일)


# ============================================================
# 공개 API
# ============================================================
def _log_fallback(op, exc):
    # 키·URL·상세 메시지 노출 금지 → 예외 '타입명'만 기록.
    print("[watch_store] Supabase " + op + " 실패, JSON 폴백: "
          + type(exc).__name__)


def load_watch_state():
    """전체 상태 조회. Supabase 우선, 실패/미설정 시 JSON 폴백."""
    if supabase_enabled():
        try:
            return _supabase_load()
        except Exception as e:
            _log_fallback("load", e)
    return _json_load()


def save_watch_state(state):
    """전체 상태 저장(정규화 후). Supabase 성공 시에도 로컬 JSON 미러링은 하지 않음.

    반환: 정규화된 state(스냅샷). Supabase 저장 실패 시 JSON 으로 폴백 저장하여
    데이터 유실을 막는다(graceful).
    """
    state = normalize_state(state)
    if supabase_enabled():
        try:
            _supabase_save(state)
            return state
        except Exception as e:
            _log_fallback("save", e)
    _json_save(state)
    return state


def backend_name():
    """현재 유효 백엔드 이름(관측/health 용). 키는 노출하지 않음."""
    return "supabase" if supabase_enabled() else "json"
