# -*- coding: utf-8 -*-
"""(선택) 라이브 실증: dart_poll.fetch_markets(days=1) 1회로 '오늘'자 브리핑.

- 콜예산 ≤ 10 (이 모듈). 이 함수는 fetch_markets 1회만 호출한다
  (내부적으로 시장 Y/K 페이지네이션 → DART 콜 다수 소모 가능. max_pages 로 상한).
- 키(DART_API_KEY) 없으면 스킵. 네트워크/DART 실패도 graceful(브리핑은 캐시로 이미 생성됨).
- 실제 소모 콜수는 requests.get 을 감싸 실측 카운트한다(추정 금지).
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config      # noqa: E402
import dart_poll   # noqa: E402  (import 허용: 락파일 아님)

try:
    from features.morning_brief import collect
except ImportError:
    import collect  # type: ignore


class _CallCounter:
    """dart_poll.requests.get 을 감싸 실제 DART 호출수를 실측한다."""

    def __init__(self, real_get):
        self._real = real_get
        self.count = 0

    def __call__(self, *a, **k):
        self.count += 1
        return self._real(*a, **k)


def run_live_today(max_pages=2, budget=10):
    """오늘자 라이브 공시 수집 시도. 반환 dict.

    { 'ran': bool, 'skipped_reason': str|None, 'calls': int,
      'rows': list, 'errors': list }
    max_pages 를 낮춰(기본 2) 시장당 콜을 제한 → 총 콜수 예산 내로.
    """
    result = {"ran": False, "skipped_reason": None, "calls": 0,
              "rows": [], "errors": []}

    if not config.DART_API_KEY:
        result["skipped_reason"] = "no_dart_api_key"
        return result

    # requests.get 래핑(실측 콜카운터)
    real_get = dart_poll.requests.get
    counter = _CallCounter(real_get)
    dart_poll.requests.get = counter
    try:
        items, errors = dart_poll.fetch_markets(days=1, max_pages=max_pages)
        result["ran"] = True
        result["errors"] = errors or []
        # 유형분류 부착(캐시행과 동일 스키마화)
        rows = []
        for it in items:
            row = dict(it)
            cls = (it.get("corp_cls") or "").strip().upper()
            row["_market"] = {"Y": "코스피", "K": "코스닥"}.get(cls, cls or "?")
            row["_type"] = collect.classify(it.get("report_nm", ""))
            row["_report_body"] = collect._strip_brackets_prefix(
                it.get("report_nm", ""))
            rows.append(row)
        result["rows"] = rows
    except Exception as e:  # graceful: 라이브 실패해도 캐시 브리핑은 유효
        result["skipped_reason"] = f"error:{type(e).__name__}"
    finally:
        result["calls"] = counter.count
        dart_poll.requests.get = real_get  # 원복(전역 오염 방지)

    if result["calls"] > budget:
        result["errors"].append(
            f"budget_exceeded:{result['calls']}>{budget}")
    return result
