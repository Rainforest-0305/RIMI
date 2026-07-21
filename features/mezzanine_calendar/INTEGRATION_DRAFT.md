# 메자닌 캘린더 앱 연결 초안 (WS-34) — 반영 전 게이트 대기

> 상태: **초안(배포 아님)**. 실제 `app.py` / `web/index.html` 은 **미편집**.
> Partner 게이트 승인 후 아래 스니펫을 반영한다.
> 표기: 코스피·코스닥. **투자권유 표현 금지 — 사실·통계만.** DART 라이브 콜 0
> (발행데이터=로컬 캐시, 시세/시총=pykrx·FDR 비-DART, 상위 N종목 라이브만).

---

## 1) `app.py` 에 붙일 엔드포인트 초안

FastAPI(`@api.get`, `JSONResponse`) 기존 패턴을 따른다. 인리치는 라이브 시세를
호출(데모 실측 약 8초)하므로 **TTL 캐시 필수**. 무거운 호출은 스레드로 오프로드.

```python
# --- app.py 상단 import 구역에 추가 (features 격리 모듈, 읽기 전용 사용) ---
import os, sys, time, threading
_MEZZ_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "features", "mezzanine_calendar")
if _MEZZ_DIR not in sys.path:
    sys.path.insert(0, _MEZZ_DIR)
import collect as _mezz_collect          # noqa: E402
import calendar_view as _mezz_cal         # noqa: E402
import enrich as _mezz_enrich             # noqa: E402

# --- 캐시 (시세 라이브 호출 비용 큼 → TTL 15분) ---
_MEZZ_CACHE = {"data": None, "ts": 0.0, "lock": threading.Lock()}
_MEZZ_TTL_SEC = 900

def _build_mezzanine_payload(top_n: int = 5, upcoming_only: bool = True):
    """collect → calendar/holdings → enrich(③④). DART 콜 0."""
    records, stats = _mezz_collect.collect_all()
    calendar, skipped = _mezz_cal.build_calendar(records, upcoming_only=upcoming_only)
    holdings = _mezz_cal.build_holdings(records)
    enriched = _mezz_enrich.enrich_top_holdings(holdings, top_n=top_n)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dart_live_calls": 0,
        "market_scope": "코스피·코스닥",
        "disclaimer": "공시·시세 기반 사실/통계 정보이며 투자권유가 아닙니다.",
        "calendar": {
            "items": calendar[:50],
            "count_total": len(calendar),
            "skipped_no_start_date": skipped,
        },
        "moneyness_summary": enriched["moneyness_dist"],          # ③ 종목 분포
        "tranche_moneyness_summary": enriched["tranche_moneyness_dist"],
        "dilution_summary": enriched["dilution_stats"],           # ④ 시총희석 분포
        "top_holdings": enriched["results"],                      # ③④ 종목별 카드용
        "enrich_quality": {
            "checked": enriched["checked"],
            "price_fail": enriched["price_fail"],
            "mktcap_fail": enriched["mktcap_fail"],
            "skipped_no_code": enriched["skipped_no_code"],
            "skipped_no_price": enriched["skipped_no_price"],
        },
    }

@api.get("/api/mezzanine")
def get_mezzanine(top_n: int = 5, upcoming_only: bool = True):
    now = time.time()
    with _MEZZ_CACHE["lock"]:
        fresh = (_MEZZ_CACHE["data"] is not None
                 and now - _MEZZ_CACHE["ts"] < _MEZZ_TTL_SEC)
        if fresh:
            return JSONResponse(_MEZZ_CACHE["data"])
    # 캐시 미스: 빌드(라이브 시세). 실패해도 500 대신 마지막 캐시/빈 응답.
    try:
        data = _build_mezzanine_payload(top_n=top_n, upcoming_only=upcoming_only)
    except Exception as e:  # noqa: BLE001
        if _MEZZ_CACHE["data"] is not None:
            return JSONResponse(_MEZZ_CACHE["data"])
        return JSONResponse({"error": "mezzanine_build_failed",
                             "detail": str(e)[:200]}, status_code=503)
    with _MEZZ_CACHE["lock"]:
        _MEZZ_CACHE["data"] = data
        _MEZZ_CACHE["ts"] = now
    return JSONResponse(data)
```

권장: 콜드스타트 지연(약 8초) 회피를 위해 기존 프리웜(`_PREWARM`) 훅에서
`_build_mezzanine_payload()` 를 1회 선호출해 캐시를 채워둔다(선택).

### 응답 필드 계약 (프런트가 쓰는 키)
- `top_holdings[]`:
  - `corp_name`, `stock_code`, `market`("KOSPI"/"KOSDAQ")
  - `current_price`, `min_conv_price`
  - `moneyness`: `"in"`(전환가<현재가·희석 임박) / `"out"`(전환가>현재가) / `"at"`(±0.5%) / `null`
  - `premium_pct`: (전환가−현재가)/현재가×100. 음수=in, 양수=out
  - `market_cap`, `listed_shares`
  - `dilution_vs_mktcap_pct`: 전환주식수×현재가÷시총×100 (총물량 기준)
  - `active_dilution_vs_mktcap_pct`: 현재 청구가능 물량 기준
  - `vs_pct_disclosure`: 공시 발행주식대비%(비교용, 공시시점 기준)
- `moneyness_summary`: `{in, out, at}` 카운트
- `dilution_summary`: `{min, max, median, n}`

> **데이터 해석 주의(프런트 표기 시 함께 노출 권장)**: `dilution_vs_mktcap_pct`
> 와 `vs_pct_disclosure` 는 **공시 누적 총합(gross)** 기준이라 상환·기전환분이
> 차감되지 않아 100%를 넘을 수 있다(원천 공시 vs_pct 자체가 동일 성격).
> 즉 "순 미상환 잔량"이 아니라 "역대 발행 총 전환가능 물량"의 상대 규모다.
> `active_*`(청구기간 내)가 더 보수적 근사. UI엔 "누적 발행 기준"임을 명시한다.

---

## 2) `web/index.html` 에 넣을 카드 렌더 스니펫

기존 카드 UI 톤에 맞춘 바닐라 JS. 투자권유 문구 없이 사실·통계만 표기.

```html
<!-- 섹션 컨테이너 (원하는 위치에 삽입) -->
<section id="mezz-section" class="card">
  <h2>메자닌(CB/BW/EB) 전환 캘린더 <span class="scope">코스피·코스닥</span></h2>
  <p class="disclaimer" id="mezz-disclaimer"></p>
  <div id="mezz-summary" class="mezz-summary"></div>
  <div id="mezz-cards" class="mezz-cards"></div>
</section>

<script>
async function loadMezzanine() {
  const box = document.getElementById('mezz-cards');
  const sum = document.getElementById('mezz-summary');
  try {
    const res = await fetch('/api/mezzanine');
    if (!res.ok) throw new Error('http ' + res.status);
    const d = await res.json();
    document.getElementById('mezz-disclaimer').textContent = d.disclaimer || '';

    const m = d.moneyness_summary || {}, di = d.dilution_summary || {};
    sum.innerHTML =
      '<span>in-the-money ' + (m.in||0) + '종목</span>' +
      '<span>out-of-the-money ' + (m.out||0) + '종목</span>' +
      '<span>시총대비 희석 중앙값 ' +
        (di.median != null ? di.median + '%' : '—') + '</span>';

    box.innerHTML = (d.top_holdings || []).map(function (h) {
      var isIn = h.moneyness === 'in';
      var badge = h.moneyness === 'in'  ? 'in-the-money'
                : h.moneyness === 'out' ? 'out-of-the-money'
                : h.moneyness === 'at'  ? '등가' : '시세없음';
      var mkt = h.market === 'KOSPI' ? '코스피'
              : h.market === 'KOSDAQ' ? '코스닥' : (h.market || '');
      var prem = (h.premium_pct != null) ? h.premium_pct.toFixed(1) + '%' : '—';
      var dil  = (h.dilution_vs_mktcap_pct != null)
                   ? h.dilution_vs_mktcap_pct.toFixed(1) + '%' : '—';
      return (
        '<div class="mezz-card ' + (isIn ? 'is-in' : 'is-out') + '">' +
          '<div class="mezz-head">' +
            '<b>' + h.corp_name + '</b> <small>' + h.stock_code +
            ' · ' + mkt + '</small>' +
            '<span class="mezz-badge ' + (isIn?'b-in':'b-out') + '">' +
              badge + '</span>' +
          '</div>' +
          '<div class="mezz-row">현재가 ' +
            (h.current_price!=null?h.current_price.toLocaleString():'—') +
            '원 · 최저전환가 ' +
            (h.min_conv_price!=null?h.min_conv_price.toLocaleString():'—') +
            '원 (괴리 ' + prem + ')</div>' +
          '<div class="mezz-row">시총대비 희석 ' + dil +
            ' <small>(누적 발행 기준)</small></div>' +
        '</div>'
      );
    }).join('') || '<div class="empty">표시할 종목이 없습니다.</div>';
  } catch (e) {
    box.innerHTML = '<div class="error">메자닌 데이터를 불러오지 못했습니다.</div>';
  }
}
document.addEventListener('DOMContentLoaded', loadMezzanine);
</script>

<style>
.mezz-summary{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0;font-size:13px}
.mezz-cards{display:grid;gap:8px}
.mezz-card{border:1px solid #e3e3e3;border-radius:10px;padding:10px}
.mezz-card.is-in{border-left:4px solid #d9534f}   /* 희석 임박 강조(경고색, 권유 아님) */
.mezz-card.is-out{border-left:4px solid #9aa0a6}
.mezz-head{display:flex;align-items:center;gap:6px}
.mezz-badge{margin-left:auto;font-size:11px;padding:2px 8px;border-radius:999px}
.mezz-badge.b-in{background:#fde8e7;color:#b02a25}
.mezz-badge.b-out{background:#eef0f2;color:#5f6368}
.mezz-row{font-size:13px;color:#333;margin-top:4px}
.disclaimer{font-size:11px;color:#888}
.scope{font-size:12px;color:#888;font-weight:400}
</style>
```

---

## 3) 반영 체크리스트 (Partner 게이트)
- [ ] `/api/mezzanine` 엔드포인트 삽입 + 프리웜 훅 연결(선택)
- [ ] `index.html` 섹션/스크립트/스타일 삽입, 기존 카드 톤과 정렬
- [ ] 캐시 TTL(기본 900초) 확인, 라이브 시세 실패 시 graceful 확인
- [ ] "누적 발행 기준" 주석 UI 노출(순잔량 오해 방지)
- [ ] 문구 재검수: 투자권유·매수/매도 유도 표현 0, 코스피·코스닥 표기
- [ ] DART 라이브 콜 0 유지(발행=캐시, 시세=pykrx·FDR)
```
