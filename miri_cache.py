# -*- coding: utf-8 -*-
"""MIRI 애널리스트/시총 캐시용 Supabase 헬퍼 (watch_store 패턴 재사용).

역할:
  - 우리 프로젝트(nkcuxzvthuwlhtiwcgwa) 자체 캐시 테이블(analyst_consensus,
    market_cap_top100)에 대한 upsert/select 를 감싼다(PostgREST/service role).
  - 테이블 생성은 Supabase Management API(SUPABASE_ACCESS_TOKEN)로 idempotent
    하게 수행(CREATE TABLE IF NOT EXISTS).
  - 모든 함수는 실패해도 예외를 삼키고 (ok, data) 또는 False 로 graceful.
    호출부(수집기/읽기 API)는 반드시 로컬 JSON 폴백을 병행한다.

보안:
  - 키/토큰은 절대 로그/응답/예외에 노출하지 않는다. 실패 로그는 예외 타입명만.
  - config 경유(os.getenv 만)로 키를 읽는다(하드코딩 0).
"""
import json

import config

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

_HTTP_TIMEOUT = 8  # 초. REST/관리 API 호출 상한(블로킹 방지).


def _rest_conf():
    """PostgREST(REST) 접근용 (base, headers). 서비스롤 우선, 없으면 anon."""
    url = (getattr(config, "SUPABASE_URL", "") or "").strip()
    key = ((getattr(config, "SUPABASE_SERVICE_ROLE", "") or "").strip()
           or (getattr(config, "SUPABASE_ANON_KEY", "") or "").strip())
    if not (url and key and requests is not None):
        return None, None
    base = url.rstrip("/") + "/rest/v1"
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    return base, headers


def _mgmt_conf():
    """Management API(테이블 생성) 접근용 (query_url, headers)."""
    url = (getattr(config, "SUPABASE_URL", "") or "").strip()
    tok = (getattr(config, "SUPABASE_ACCESS_TOKEN", "") or "").strip()
    if not (url and tok and requests is not None):
        return None, None
    ref = url.split("//")[-1].split(".")[0]
    if not ref:
        return None, None
    q = "https://api.supabase.com/v1/projects/" + ref + "/database/query"
    headers = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    return q, headers


def _log(op, exc):
    print("[miri_cache] " + op + " 실패(무시, 로컬폴백): " + type(exc).__name__)


def run_sql(sql):
    """Management API 로 임의 SQL 실행(주로 DDL). 성공 True / 실패 False.
    파괴적 DDL 은 호출부에서 넘기지 말 것(이 헬퍼는 IF NOT EXISTS 만 사용)."""
    q, headers = _mgmt_conf()
    if not q:
        return False
    try:
        r = requests.post(q, headers=headers, data=json.dumps({"query": sql}),
                          timeout=_HTTP_TIMEOUT + 12)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        _log("run_sql", e)
        return False


def ensure_table(ddl):
    """CREATE TABLE IF NOT EXISTS ... DDL 을 idempotent 실행. 실패해도 False 만."""
    return run_sql(ddl)


def upsert(table, rows, on_conflict="code"):
    """PostgREST upsert(merge-duplicates). rows=list[dict]. 성공 True / 실패 False."""
    base, headers = _rest_conf()
    if not base or not rows:
        return False
    h = dict(headers)
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    try:
        url = base + "/" + table + "?on_conflict=" + on_conflict
        r = requests.post(url, headers=h, data=json.dumps(rows, ensure_ascii=False,
                          allow_nan=False), timeout=_HTTP_TIMEOUT + 12)
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        _log("upsert(" + table + ")", e)
        return False


def replace_all(table, rows, on_conflict="code"):
    """스냅샷 테이블 전량 교체: 기존 행 삭제 후 신규 삽입. 성공 True / 실패 False.
    top100 처럼 rank 재배치가 있어 잔여행이 남으면 안 되는 스냅샷에 사용."""
    base, headers = _rest_conf()
    if not base or not rows:
        return False
    try:
        # 전체 삭제(PostgREST 는 필터 필수 → 항상 참인 조건). code 는 not null.
        dr = requests.delete(base + "/" + table + "?code=neq.__none__",
                             headers=headers, timeout=_HTTP_TIMEOUT + 12)
        dr.raise_for_status()
    except Exception as e:  # noqa: BLE001
        _log("replace_all.delete(" + table + ")", e)
        return False
    return upsert(table, rows, on_conflict=on_conflict)


def select_all(table, order=None, limit=None):
    """PostgREST select. (ok, rows). 실패 시 (False, [])."""
    base, headers = _rest_conf()
    if not base:
        return False, []
    try:
        url = base + "/" + table + "?select=*"
        if order:
            url += "&order=" + order
        if limit:
            url += "&limit=" + str(int(limit))
        r = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return True, (data if isinstance(data, list) else [])
    except Exception as e:  # noqa: BLE001
        _log("select_all(" + table + ")", e)
        return False, []


def select_one(table, code):
    """단일 code 행 조회. (ok, row|None). 실패 시 (False, None)."""
    base, headers = _rest_conf()
    if not base:
        return False, None
    try:
        from urllib.parse import quote as _q
        url = base + "/" + table + "?select=*&code=eq." + _q(str(code), safe="") + "&limit=1"
        r = requests.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return True, data[0]
        return True, None
    except Exception as e:  # noqa: BLE001
        _log("select_one(" + table + ")", e)
        return False, None


# --------- 로컬 JSON 스냅샷 폴백(원자적 저장) ----------
def save_json(path, obj):
    """원자적 JSON 저장(.tmp -> replace). 실패해도 예외 삼킴(False)."""
    import os
    import uuid
    try:
        tmp = str(path) + ".tmp." + uuid.uuid4().hex[:8]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, str(path))
        return True
    except Exception as e:  # noqa: BLE001
        _log("save_json", e)
        return False


def load_json(path, default=None):
    """로컬 JSON 로드. 없거나 실패 시 default."""
    try:
        with open(str(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default
