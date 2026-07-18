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

기기별 분리(device_id):
  관심종목/그룹/키워드는 **기기 단위**로 격리된다. 웹 API 는 요청의 X-Device-Id
  헤더에서 device_id 를 받아 그 기기 데이터만 읽고/쓴다.
    - load_watch_state(device_id)   : 그 기기 상태(빈 device_id → 임시 빈 상태, 미영속).
    - save_watch_state(state, dev)  : 그 기기만 스냅샷 교체(빈 device_id → 미영속).
    - load_all_watch_state()        : 전 기기 union(서버 폴러/알림용, main.poll_once).
  Supabase 는 3개 테이블에 device_id 컬럼(+인덱스)을 두고 device_id 로 필터한다.
  JSON 폴백은 {"devices": {"<device_id>": {groups,stocks,keywords}}} 구조로 격리한다.
  구(舊) 단일공유 watchlist.json(평면 스키마)은 로드 시 device_id='legacy' 로 이관한다.
"""
import json
import os
from urllib.parse import quote as _urlquote

import config

try:  # requests 는 이미 requirements 에 존재(무추가). 없더라도 JSON 폴백은 동작.
    import requests
except Exception:  # pragma: no cover
    requests = None


DEFAULT_GROUP_ID = "default"
DEFAULT_GROUP_NAME = "관심"

# 구(舊) 단일공유 데이터의 기기 라벨. 스키마 default 및 JSON 이관 대상과 일치.
LEGACY_DEVICE_ID = "legacy"

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


def _sb_get(table, base, headers, device_id=None):
    q = base + "/" + table + "?select=*"
    if device_id is not None:
        # 그 기기 행만. device_id=None 이면 필터 없음(전 기기 union) → 폴러용.
        q += "&device_id=eq." + _urlquote(str(device_id), safe="")
    r = requests.get(q, headers=headers, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _sb_delete_device(table, base, headers, device_id):
    # 삭제는 항상 device_id 로 스코프. 필터가 곧 그 기기 전용이라 타 기기 데이터를
    # 절대 건드리지 않는다(PostgREST 삭제 필수 필터 요건도 충족).
    r = requests.delete(base + "/" + table + "?device_id=eq."
                        + _urlquote(str(device_id), safe=""),
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


def _supabase_load(device_id=None):
    """device_id 스코프 로드. device_id=None 이면 전 기기 union(폴러용).

    union 시 normalize_state 가 중복 stock_code/keyword/group-id 를 정리한다
    (여러 기기의 'default' 그룹은 하나로 접힘 — 폴러는 종목/키워드만 사용).
    """
    base, headers = _supabase_rest()
    groups = _sb_get("watch_groups", base, headers, device_id)
    stocks = _sb_get("watch_stocks", base, headers, device_id)
    keywords = _sb_get("watch_keywords", base, headers, device_id)
    raw = {
        "groups": [{"id": g.get("id"), "name": g.get("name"),
                    "order": g.get("sort_order", 0)} for g in groups],
        "stocks": [{"name": s.get("name"), "stock_code": s.get("stock_code"),
                    "group": s.get("group_id") or DEFAULT_GROUP_ID,
                    "order": s.get("sort_order", 0)} for s in stocks],
        "keywords": [r.get("keyword") for r in keywords if r.get("keyword")],
    }
    return normalize_state(raw)


def _supabase_save(state, device_id):
    """그 기기(device_id)만 스냅샷 전체 교체(delete-scoped + bulk insert).

    순서: 자식(stocks) 먼저 지우고 부모(groups) 삭제 → 삽입은 groups(부모) 먼저.
    삭제/삽입 모두 device_id 로 스코프되어 타 기기 데이터는 불변.
    """
    base, headers = _supabase_rest()
    _sb_delete_device("watch_stocks", base, headers, device_id)
    _sb_delete_device("watch_keywords", base, headers, device_id)
    _sb_delete_device("watch_groups", base, headers, device_id)

    _sb_insert("watch_groups",
               [{"device_id": device_id, "id": g["id"], "name": g["name"],
                 "sort_order": g["order"]}
                for g in state["groups"]], base, headers)
    _sb_insert("watch_stocks",
               [{"device_id": device_id, "stock_code": s["stock_code"],
                 "name": s["name"], "group_id": s["group"],
                 "sort_order": s["order"]}
                for s in state["stocks"]], base, headers)
    _sb_insert("watch_keywords",
               [{"device_id": device_id, "keyword": k}
                for k in state["keywords"]], base, headers)


# ============================================================
# JSON 폴백 백엔드
# ============================================================
def _json_load_raw():
    """watchlist.json 원본 dict 을 {"devices": {id: {...}}} 형태로 반환.

    하위호환: 구(舊) 평면 스키마({groups,stocks,keywords})는 device_id='legacy'
    로 이관해 반환한다(파일은 다음 저장 때 새 구조로 재기록됨).
    """
    f = config.WATCHLIST_FILE
    if not f.exists():
        return {"devices": {}}
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {"devices": {}}
    if not isinstance(raw, dict):
        return {"devices": {}}
    if isinstance(raw.get("devices"), dict):
        return raw
    # 구 평면 스키마 → legacy 기기로 이관(그 안에 실데이터가 있을 때만).
    if raw.get("stocks") or raw.get("groups") or raw.get("keywords"):
        return {"devices": {LEGACY_DEVICE_ID: {
            "groups": raw.get("groups") or [],
            "stocks": raw.get("stocks") or [],
            "keywords": raw.get("keywords") or [],
        }}}
    return {"devices": {}}


def _json_load(device_id):
    devices = _json_load_raw().get("devices") or {}
    return normalize_state(devices.get(device_id) or {})


def _json_load_union():
    """전 기기 union(폴러용). normalize 가 중복을 정리한다."""
    devices = _json_load_raw().get("devices") or {}
    merged = {"groups": [], "stocks": [], "keywords": []}
    for st in devices.values():
        if not isinstance(st, dict):
            continue
        merged["groups"].extend(st.get("groups") or [])
        merged["stocks"].extend(st.get("stocks") or [])
        merged["keywords"].extend(st.get("keywords") or [])
    return normalize_state(merged)


def _json_save(state, device_id):
    raw = _json_load_raw()
    devices = raw.get("devices")
    if not isinstance(devices, dict):
        devices = {}
    devices[device_id] = {
        "groups": state["groups"],
        "stocks": state["stocks"],
        "keywords": state["keywords"],
    }
    payload = {
        "_comment": ("기기별 관심종목. devices[<device_id>] = {groups,stocks,"
                     "keywords}. stock_code=6자리. group=소속그룹 id."),
        "devices": devices,
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


def _norm_device(device_id):
    return (str(device_id).strip() if device_id is not None else "")


def load_watch_state(device_id=None):
    """그 기기(device_id) 상태 조회. Supabase 우선, 실패/미설정 시 JSON 폴백.

    device_id 미제공(빈 문자열/None) → 임시 빈 상태(default 그룹만) 반환(에러 없음,
    미영속). 프론트는 항상 X-Device-Id 를 보내므로 이 경로는 비프론트/헬스용.
    """
    dev = _norm_device(device_id)
    if not dev:
        return normalize_state({})
    if supabase_enabled():
        try:
            return _supabase_load(dev)
        except Exception as e:
            _log_fallback("load", e)
    return _json_load(dev)


def load_all_watch_state():
    """전 기기 union 상태(서버 폴러/알림 main.poll_once 용).

    한 채널(본인 테스트 채널)로만 나가는 서버 폴러가 '어느 기기든 관심 등록한
    종목'을 계속 알리도록 union 을 쓴다(비파괴: 기존 legacy 종목 알림 유지).
    """
    if supabase_enabled():
        try:
            return _supabase_load(None)
        except Exception as e:
            _log_fallback("load(all)", e)
    return _json_load_union()


def save_watch_state(state, device_id=None):
    """그 기기(device_id) 상태만 저장(정규화 후). Supabase 성공 시에도 JSON 미러 없음.

    device_id 미제공 → 영속하지 않고 정규화 스냅샷만 반환(임시 세션, 에러 없음).
    Supabase 저장 실패 시 JSON 으로 폴백 저장하여 유실을 막는다(graceful).
    """
    state = normalize_state(state)
    dev = _norm_device(device_id)
    if not dev:
        return state
    if supabase_enabled():
        try:
            _supabase_save(state, dev)
            return state
        except Exception as e:
            _log_fallback("save", e)
    _json_save(state, dev)
    return state


def backend_name():
    """현재 유효 백엔드 이름(관측/health 용). 키는 노출하지 않음."""
    return "supabase" if supabase_enabled() else "json"
