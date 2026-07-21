# -*- coding: utf-8 -*-
"""①오늘 탭 피드 + ③랭킹 탭 빌더 (add-only, app.py 의 얇은 라우트가 호출).

- build_today_payload(alerts): ③과 동일한 live _get_feed 알럿을 소스로 ①오늘 overnight
    리스트 + 유형분포 산출(DART 0콜). ★bench_cache/morning_brief.load_latest 미사용
    (배포 빈값·stale 결함 해소). 큐레이션은 app.py 의 seam 이 주입한다.
- build_curation_fallback(alerts): 동일 live 알럿을 daily_curation 의 유형가중
    (_score, TYPE_WEIGHT)으로 중요도순 정렬 → CurationItem[]. daily_curation.py 는
    import 만(수정·build_curation 호출 금지).
- build_ranking_base(alerts)/apply_price_signal(): ③랭킹(공시중요도 + 급등락 병합).

모든 빌더는 신규 DART 폴링을 절대 하지 않고 이미 캐시된 알럿만 재사용한다. 알럿이
없을 때(콜드)는 empty_*_payload() 로 정상 빈 shape 를 반환한다. 과거통계 impact 는
알럿에 이미 실려오므로(impact.impact_for_tags→impact_benchmark.json) 재계산 없음.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))  # repo 루트


def _ensure_paths():
    """repo 루트를 sys.path 에 보장(daily_curation 등 루트 모듈 절대 import 용)."""
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)


def _alert_primary_type(tags, weights):
    """alert tags → 대표 유형 1개(중요도가중 최대). 분포 Σ==count 보장용 단일 라벨."""
    if not tags:
        return "기타공시"
    return max(tags, key=lambda t: weights.get(t, 1.0))


def _alert_item_dict(a, ptype):
    """live _get_feed 알럿 → ①오늘 overnight 아이템(프론트용 정제 dict)."""
    rcept_dt = str(a.get("rcept_dt") or "")
    if len(rcept_dt) == 8 and rcept_dt.isdigit():
        date = f"{rcept_dt[0:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}"
    else:
        date = rcept_dt
    return {
        "rcept_no": a.get("rcept_no") or "",
        "corp_name": a.get("corp_name") or "",
        "stock_code": (a.get("stock_code") or "").strip(),
        "report_nm": a.get("report_nm") or "",
        "rcept_dt": rcept_dt,
        "date": a.get("date") or date,
        "market": a.get("market") or "",     # 알럿은 이미 KOSPI/KOSDAQ 정본
        "category": "",                       # live 알럿엔 파일카테고리 없음(스키마 유지)
        "type": ptype,
    }


def _type_distribution(items):
    """표시 아이템의 단일 type 로 분포 산출(건수 내림차순). Σ == len(items)."""
    d = {}
    for it in items:
        t = it.get("type") or "기타공시"
        d[t] = d.get(t, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: kv[1], reverse=True))


def build_today_payload(alerts, overnight_n=20):
    """①오늘 탭: ③과 **동일한 live _get_feed 알럿 소스**(오늘자). DART 0콜(캐시된 알럿 재사용).

    알럿은 이미 IMPACT_TAGS 필터 + dedup + 최신순(rcept_dt·rcept_no desc)이라 그대로
    material. overnight = 최신 cap N. dataset_as_of = max(rcept_dt)(=오늘/최신 개장일).
    type_distribution 은 **표시되는 overnight 집합**에서 산출 → 불변식 Σ==overnight.count.
    bench_cache/morning_brief 미사용(배포 빈값·stale 결함 해소)."""
    _ensure_paths()
    import daily_curation as _dc   # TYPE_WEIGHT(대표유형 선정). 단방향 import, DART 0콜.
    weights = _dc.TYPE_WEIGHT
    alerts = [a for a in (alerts or []) if isinstance(a, dict)]

    overnight = alerts[:overnight_n]
    items = [_alert_item_dict(a, _alert_primary_type(a.get("tags") or [], weights))
             for a in overnight]
    dist = _type_distribution(items)          # 표시 overnight 스코프(Σ==count)

    # 전체 live feed 분포(참고). 대표유형 1개/알럿으로 카운트.
    total_dist = {}
    for a in alerts:
        t = _alert_primary_type(a.get("tags") or [], weights)
        total_dist[t] = total_dist.get(t, 0) + 1
    total_dist = dict(sorted(total_dist.items(), key=lambda kv: kv[1], reverse=True))

    as_of = max((str(a.get("rcept_dt") or "") for a in alerts), default="")
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dataset_as_of": as_of,               # live 알럿 최신 접수일(=오늘/최신 개장일)
        "market_scope": "코스피·코스닥",
        "overnight": {"items": items, "count": len(items)},
        "type_distribution": dist,            # 밤사이 스코프(Σ == overnight.count)
        "total_distribution": total_dist,     # 전체 live feed(참고)
        "meta": {"total": len(alerts), "source": "live_feed"},
        "disclaimer": "공시 기반 사실/통계 정보이며 투자권유가 아닙니다.",
    }


def build_curation_fallback(alerts, n=10):
    """①오늘 '오늘 공시 TOP 큐레이션' — live _get_feed 알럿 → **중요도순** CurationItem[].

    ★랭킹 로직은 secretary(daily_curation.build_curation) 단일소유다. 여기서는 절대
    build_curation 을 호출하지 않고(라이브 DART 폴링 금지) daily_curation.py 도 수정하지
    않는다 — 오직 _score/TYPE_WEIGHT 함수만 단방향 import 해 live alerts 에 적용(③
    build_ranking_base 와 동일한 랭킹함수, DART 0콜). rank_score=_score 값, rank_reason=
    "중요도순".

    ★정합: 알럿이 이미 tags/summary/impact(windows 포함)/rcept 를 품고 있으므로
    impact.windows 는 자동으로 /api/alerts 와 완전 동형(재계산·변환 불필요). market 만
    KOSPI/KOSDAQ 정본 유지. bench_cache/morning_brief 미사용."""
    _ensure_paths()
    import daily_curation as _dc   # _score / TYPE_WEIGHT (단방향 import, DART 0콜)
    weights = _dc.TYPE_WEIGHT
    alerts = [a for a in (alerts or []) if isinstance(a, dict)]

    scored = []
    for a in alerts:
        tags = a.get("tags") or []
        report_nm = a.get("report_nm") or ""
        try:
            sc = float(_dc._score(tags, a.get("impact"), report_nm))
        except Exception:
            sc = 0.0
        scored.append((sc, a))
    # 중요도 desc, 동점은 접수번호 desc(최신 우선) — build_ranking_base 와 동일 규칙
    scored.sort(key=lambda t: (t[0], str(t[1].get("rcept_no") or "")), reverse=True)

    items = []
    for rank, (sc, a) in enumerate(scored[:n], 1):
        tags = a.get("tags") or []
        block = a.get("impact")                      # 알럿에 이미 계산된 impact(windows 동형)
        imp = None
        if isinstance(block, dict) and block.get("status") == "ok":
            imp = {                                  # windows 그대로(=/api/alerts 완전 동형)
                "grade": block.get("grade"),
                "confidence": block.get("confidence"),
                "windows": block.get("windows"),
            }
        summary = a.get("summary") or None
        if summary and not any((str(s or "")).strip() for s in summary):
            summary = None
        items.append({
            "rank": rank,
            "rank_score": round(sc, 2),              # _score 값(중요도)
            "rank_reason": "중요도순",
            "corp_name": a.get("corp_name") or "",
            "stock_code": (a.get("stock_code") or "").strip(),
            "market": a.get("market") or "",         # 알럿은 이미 KOSPI/KOSDAQ
            "rcept_no": a.get("rcept_no") or "",
            "report_nm": a.get("report_nm") or "",
            "rcept_dt": str(a.get("rcept_dt") or ""),
            "disc_type": _alert_primary_type(tags, weights),
            "url": a.get("url") or "",
            "summary": summary,
            "impact": imp,
        })
    return {"status": "ranked_importance", "items": items}


def empty_curation():
    """폴백 빌드 실패/빈 캐시 시 graceful(빈 items, 명시 status)."""
    return {"status": "unavailable", "items": []}


def empty_today_payload():
    """빈 캐시(Render 등)에서도 200 으로 돌려줄 well-formed 빈 shape."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dataset_as_of": "",
        "market_scope": "코스피·코스닥",
        "overnight": {"items": [], "count": 0},
        "type_distribution": {},
        "total_distribution": {},
        "meta": {"total": 0, "source": "live_feed"},
        "disclaimer": "공시 기반 사실/통계 정보이며 투자권유가 아닙니다.",
    }


# ---------------- ③랭킹 ----------------
def _merge_price_signal(base_score, signal):
    """SEAM: data-lead 의 toss 기반 급등락 가격신호로 base_score 를 증강하는 지점.

    현재 구현: base_score + (급등락 크기 기여). data-lead 의 toss 급등락 등락률
    (change_pct)은 app.py 의 stock_code TTL 캐시(요청경로 라이브콜 0)에서 온 signal 로
    전달된다. 기여는 부호무관 '반응 크기'(급등·급락 모두 중요) min(|change%|,30)*0.1 →
    최대 +3.0. signal 없음/change_pct None 이면 no-op(공시중요도 단독) — additive·graceful.

    반환: (final_score, contributed_change_pct or None)
    """
    chg = signal.get("change_pct") if isinstance(signal, dict) else None
    if isinstance(chg, (int, float)) and not isinstance(chg, bool):
        return base_score + min(abs(float(chg)), 30.0) * 0.1, float(chg)
    return base_score, None


def build_ranking_base(alerts, pool_n=40):
    """이미 캐시된 피드 alerts 를 공시중요도(daily_curation._score)로 정렬한 **base 풀**.

    네트워크 0: alert 의 tags/impact/report_nm 만 사용(신규 DART/시세 조회 없음).
    급등락은 여기서 반영하지 않는다(캐시 대상). apply_price_signal 이 응답 시점에
    price 캐시로 재가중·재정렬한다 → 워머가 캐시를 채우는대로 즉시 수렴(payload TTL 무관).
    pool_n: 급등락이 top_n 밖 항목을 끌어올릴 여지를 위한 후보 풀(top_n 보다 크게).
    """
    _ensure_paths()
    import daily_curation as _dc  # TYPE_WEIGHT / _score 재사용(랭킹 공식 단일 출처)

    scored = []
    for a in (alerts or []):
        if not isinstance(a, dict):
            continue
        tags = a.get("tags") or []
        report_nm = a.get("report_nm") or ""
        try:
            base = round(float(_dc._score(tags, a.get("impact"), report_nm)), 4)
        except Exception:
            base = 0.0
        scored.append((base, a))
    scored.sort(key=lambda t: (t[0], str(t[1].get("rcept_no") or "")), reverse=True)

    pool = []
    for base, a in scored[:pool_n]:
        pool.append({
            "base_score": base,
            "corp_name": a.get("corp_name") or "",
            "stock_code": (a.get("stock_code") or "").strip(),
            "market": a.get("market") or "",
            "report_nm": a.get("report_nm") or "",
            "tags": a.get("tags") or [],
            "summary": a.get("summary") or [],
            "rcept_no": a.get("rcept_no") or "",
            "url": a.get("url") or "",
            "impact": a.get("impact"),
        })
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "market_scope": "코스피·코스닥",
        "basis": "disclosure_importance",
        "pool": pool,
    }


def apply_price_signal(base_payload, price_lookup=None, top_n=30):
    """base 풀에 급등락(price_lookup, 캐시 read-only)을 병합·재정렬해 최종 랭킹 payload.

    price_lookup(stock_code) -> {change_pct(float|null), price, prev_close, volume,
    source, as_of} | None (캐시 미스). 라이브콜 0(app.py 캐시만 읽음). 미스/None 은
    price_signal=null 로 강등(순위 성립).
    """
    pool = (base_payload or {}).get("pool") or []
    rescored = []
    for it in pool:
        base = it.get("base_score") or 0.0
        code = (it.get("stock_code") or "").strip()
        sig = None
        if price_lookup and code:
            try:
                sig = price_lookup(code)
            except Exception:
                sig = None
        final, chg = _merge_price_signal(base, sig)
        rescored.append((final, chg, sig, it))
    rescored.sort(key=lambda t: (t[0], str(t[3].get("rcept_no") or "")), reverse=True)

    out = []
    resolved = 0
    for rank, (final, chg, sig, it) in enumerate(rescored[:top_n], 1):
        reason = "공시중요도"
        ps = None
        if isinstance(sig, dict):
            ps = {"change_pct": chg,
                  "price": sig.get("price"),
                  "prev_close": sig.get("prev_close"),
                  "volume": sig.get("volume"),
                  "source": sig.get("source") or "toss",
                  "as_of": sig.get("as_of")}
        if chg is not None:
            resolved += 1
            reason += f" + 급등락 {chg:+.2f}%"
        out.append({
            "rank": rank,
            "score": round(float(final), 2),
            "base_score": round(float(base_or_zero(it)), 2),
            "rank_reason": reason,
            "corp_name": it.get("corp_name") or "",
            "stock_code": (it.get("stock_code") or "").strip(),
            "market": it.get("market") or "",
            "report_nm": it.get("report_nm") or "",
            "tags": it.get("tags") or [],
            "summary": it.get("summary") or [],
            "rcept_no": it.get("rcept_no") or "",
            "url": it.get("url") or "",
            "impact": it.get("impact"),
            "price_signal": ps,
        })
    return {
        "generated_at": (base_payload or {}).get("generated_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "market_scope": "코스피·코스닥",
        "basis": "disclosure_importance+price_move",
        # 급등락 세그 상태: 값이 하나라도 채워졌으면 active, 아니면 warming(수렴 중).
        "price_signal": {"status": "active" if resolved else "warming",
                         "resolved": resolved, "requested": len(out), "source": "toss"},
        "count": len(out),
        "items": out,
        "disclaimer": "공시중요도·급등락 기반 참고 순위이며 투자권유가 아닙니다.",
    }


def base_or_zero(it):
    v = it.get("base_score")
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def empty_ranking_payload():
    """빈 캐시/실패 시 200 으로 돌려줄 well-formed 빈 shape."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "market_scope": "코스피·코스닥",
        "basis": "disclosure_importance+price_move",
        "price_signal": {"status": "warming", "resolved": 0, "requested": 0, "source": "toss"},
        "count": 0,
        "items": [],
        "disclaimer": "공시중요도·급등락 기반 참고 순위이며 투자권유가 아닙니다.",
    }
