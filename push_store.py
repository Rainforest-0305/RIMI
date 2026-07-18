# -*- coding: utf-8 -*-
"""웹푸시 구독(subscription) 영속 스토어 — 기기별(device_id).

watch_store 패턴을 그대로 따른다:
  - Supabase(PostgREST) 우선(config.WATCH_BACKEND 게이트 + 키 존재 시).
  - 실패/미설정 시 JSON 파일(data/push_subs.json)로 graceful 폴백.
  - 키/엔드포인트/구독상세는 로그·응답·예외 메시지에 노출하지 않는다(타입명만).

테이블 push_subs(스키마: supabase_push.sql):
  device_id  text        — 구독 소유 기기(X-Device-Id)
  endpoint   text PK      — 브라우저 푸시 엔드포인트(고유). 같은 기기 재구독 시 upsert.
  sub_json   jsonb        — pushManager.subscribe() 의 전체 구독 객체
  created_at timestamptz  — 생성시각

앱(app.py)은 오직 이 모듈의 공개 함수만 통해 구독에 접근한다.
  - save_sub(device_id, sub)          : upsert(엔드포인트 기준)
  - delete_endpoint(endpoint)         : 발송 실패(410/404) 자동정리용
  - delete_device_endpoint(dev, ep)   : DELETE /api/push(구독 해제)용
  - all_subs()                        : 전 구독(발송 파이프에서 기기별 그룹핑)
"""
import json
import os
from datetime import datetime, timezone
from urllib.parse import quote as _urlquote

import config
import watch_store  # supabase_enabled() / _supabase_rest() 재사용(동일 백엔드 게이트)

try:  # requests 는 이미 requirements 에 존재. 없더라도 JSON 폴백은 동작.
    import requests
except Exception:  # pragma: no cover
    requests = None

_TABLE = "push_subs"
_HTTP_TIMEOUT = 8  # 초
_JSON_FILE = config.DATA / "push_subs.json"


# ============================================================
# 백엔드 선택 (watch_store 와 동일 게이트)
# ============================================================
def _supabase_enabled():
    try:
        return watch_store.supabase_enabled()
    except Exception:
        return False


def backend_name():
    return "supabase" if _supabase_enabled() else "json"


# ============================================================
# Supabase(PostgREST) 백엔드
# ============================================================
def _rest():
    return watch_store._supabase_rest()  # (base, headers) — 키는 노출하지 않음


def _sb_upsert(device_id, sub):
    base, headers = _rest()
    h = dict(headers)
    # 엔드포인트 PK 충돌 시 병합(재구독/갱신). 응답 최소.
    h["Prefer"] = "resolution=merge-duplicates,return=minimal"
    endpoint = str(sub.get("endpoint") or "")
    row = {
        "device_id": str(device_id),
        "endpoint": endpoint,
        "sub_json": sub,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.post(base + "/" + _TABLE, headers=h,
                      data=json.dumps([row]), timeout=_HTTP_TIMEOUT)
    r.raise_for_status()


def _sb_delete(base, headers, query):
    r = requests.delete(base + "/" + _TABLE + "?" + query,
                        headers=headers, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()


def _sb_all():
    base, headers = _rest()
    r = requests.get(base + "/" + _TABLE + "?select=*",
                     headers=headers, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for row in (r.json() or []):
        if not isinstance(row, dict):
            continue
        sub = row.get("sub_json")
        if isinstance(sub, str):
            try:
                sub = json.loads(sub)
            except Exception:
                continue
        if not isinstance(sub, dict) or not sub.get("endpoint"):
            continue
        out.append({
            "device_id": str(row.get("device_id") or ""),
            "endpoint": str(row.get("endpoint") or sub.get("endpoint")),
            "sub": sub,
        })
    return out


# ============================================================
# JSON 폴백 백엔드
# ============================================================
def _json_read():
    try:
        raw = json.loads(_JSON_FILE.read_text(encoding="utf-8"))
        subs = raw.get("subs") if isinstance(raw, dict) else None
        return subs if isinstance(subs, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _json_write(subs):
    payload = {
        "_comment": ("기기별 웹푸시 구독. subs=[{device_id,endpoint,sub,created_at}]. "
                     "endpoint 고유(upsert 기준)."),
        "subs": subs,
    }
    tmp = _JSON_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, _JSON_FILE)  # 원자적 교체


def _json_upsert(device_id, sub):
    endpoint = str(sub.get("endpoint") or "")
    subs = [s for s in _json_read() if s.get("endpoint") != endpoint]
    subs.append({
        "device_id": str(device_id),
        "endpoint": endpoint,
        "sub": sub,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _json_write(subs)


def _json_all():
    out = []
    for s in _json_read():
        sub = s.get("sub")
        if not isinstance(sub, dict) or not sub.get("endpoint"):
            continue
        out.append({
            "device_id": str(s.get("device_id") or ""),
            "endpoint": str(s.get("endpoint") or sub.get("endpoint")),
            "sub": sub,
        })
    return out


# ============================================================
# 공개 API
# ============================================================
def _log_fallback(op, exc):
    print("[push_store] Supabase " + op + " 실패, JSON 폴백: "
          + type(exc).__name__)


def save_sub(device_id, sub):
    """구독 저장(엔드포인트 upsert). sub 은 pushManager.subscribe() 의 dict.

    device_id 없거나 endpoint 없으면 no-op(에러 없음). Supabase 실패 시 JSON 폴백.
    """
    device_id = (str(device_id).strip() if device_id is not None else "")
    if not device_id or not isinstance(sub, dict) or not sub.get("endpoint"):
        return False
    if _supabase_enabled():
        try:
            _sb_upsert(device_id, sub)
            return True
        except Exception as e:
            _log_fallback("save", e)
    _json_upsert(device_id, sub)
    return True


def delete_endpoint(endpoint):
    """엔드포인트 하나 제거(발송 실패 410/404 자동정리). device 무관 정리."""
    endpoint = str(endpoint or "")
    if not endpoint:
        return
    if _supabase_enabled():
        try:
            base, headers = _rest()
            _sb_delete(base, headers,
                       "endpoint=eq." + _urlquote(endpoint, safe=""))
            return
        except Exception as e:
            _log_fallback("delete", e)
    subs = [s for s in _json_read() if s.get("endpoint") != endpoint]
    _json_write(subs)


def delete_device_endpoint(device_id, endpoint):
    """구독 해제(DELETE /api/push): 그 기기+엔드포인트 조합만 제거.

    endpoint 미제공이면 그 기기의 전체 구독을 제거(기기 전체 해제)."""
    device_id = (str(device_id).strip() if device_id is not None else "")
    if not device_id:
        return
    endpoint = str(endpoint or "")
    if _supabase_enabled():
        try:
            base, headers = _rest()
            q = "device_id=eq." + _urlquote(device_id, safe="")
            if endpoint:
                q += "&endpoint=eq." + _urlquote(endpoint, safe="")
            _sb_delete(base, headers, q)
            return
        except Exception as e:
            _log_fallback("delete(dev)", e)
    if endpoint:
        subs = [s for s in _json_read()
                if not (s.get("device_id") == device_id
                        and s.get("endpoint") == endpoint)]
    else:
        subs = [s for s in _json_read() if s.get("device_id") != device_id]
    _json_write(subs)


def all_subs():
    """전 구독 리스트: [{device_id, endpoint, sub}]. 발송 파이프 소비용.

    Supabase 실패 시 JSON 폴백. 어떤 예외에도 빈 리스트(발송 파이프 무붕괴)."""
    if _supabase_enabled():
        try:
            return _sb_all()
        except Exception as e:
            _log_fallback("load(all)", e)
    try:
        return _json_all()
    except Exception:
        return []
