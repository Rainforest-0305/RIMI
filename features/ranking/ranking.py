# -*- coding: utf-8 -*-
"""랭킹/화제 탭 순수 데이터 모듈 (I/O 없음, 계산만).

입력: /api/alerts 피드(dict, app._get_feed 산출) + 선택적 price_fn(주입).
출력: {generated_at, disclosure_hot[], price_movers[], buzz[], meta}.

계산은 여기(순수함수), 외부호출(DART/TOSS)은 호출측(demo)에서. price_fn 은
주입식 의존성 → 이 파일은 네트워크를 모른다(테스트/재현 용이).

■ 피드 item 실측 스키마(app.py _build_feed 확인, 2026-07-21):
  rcept_no(14자리=YYYYMMDD+일련), corp_name, stock_code(6자리), corp_cls(Y/K),
  market('KOSPI'/'KOSDAQ'), report_nm, flr_nm, rcept_dt(YYYYMMDD, 분단위 없음),
  date, rm, tags(list), summary, bullets, scale_eligible, impact, url,
  is_new, is_watched.
  주의: 피드는 이미 IMPACT_TAGS 노이즈필터를 통과한 실질공시만 담긴다.
        rcept_dt 는 '일' 해상도뿐이라 일중 시각 프록시로 rcept_no(일련) 사용.

■ 프록시 정의(없는 데이터를 지어내지 않음):
  - '조회급등/화제도(buzz)'의 실제 조회수 데이터는 이 피드에 없다. 대신
    '최근 창 공시 급증(acceleration)'을 프록시로 쓴다. README 및 payload.meta
    의 buzz_proxy 필드에 명시한다.
"""
from datetime import date, datetime

# 유형(태그)별 중요도 가중(화제/핫 스코어 보정). 피드 tags 값과 동일 어휘.
_TAG_WEIGHT = {
    "유상증자": 2.0, "전환사채": 2.0, "합병분할": 2.0, "최대주주변경": 2.0, "소송": 1.8,
    "무상증자": 1.5, "주식소각": 1.5, "자사주": 1.5, "공급계약": 1.5, "임상": 1.5,
    "배당": 1.2, "실적": 1.0, "감사보고서": 1.0, "지분변동": 1.1,
}
_DEFAULT_TAG_WEIGHT = 1.0


def _parse_day(rcept_dt):
    """'YYYYMMDD' → date. 실패 시 None."""
    s = (rcept_dt or "").strip()
    if len(s) < 8 or not s[:8].isdigit():
        return None
    try:
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _material_weight(tags):
    """태그 리스트 → 최댓값 중요도 가중(가장 무거운 이벤트 기준)."""
    if not tags:
        return _DEFAULT_TAG_WEIGHT
    return max((_TAG_WEIGHT.get(t, _DEFAULT_TAG_WEIGHT) for t in tags),
               default=_DEFAULT_TAG_WEIGHT)


def _recency_weight(day, ref_day, half_life_days=2.0):
    """접수일 recency 가중. ref_day(피드 최신일) 기준 지수감쇠(반감기 기본 2일)."""
    if day is None or ref_day is None:
        return 0.5
    days_ago = max(0, (ref_day - day).days)
    return 0.5 ** (days_ago / half_life_days)


def _aggregate(alerts):
    """종목코드별 집계. 코드 없는(비상장/코드결측) 항목은 제외.
    반환: {code: {name, market, corp_cls, discs:[{day,tags,report_nm,rcept_no,mat}]}}"""
    agg = {}
    for it in alerts:
        if not isinstance(it, dict):
            continue
        code = (it.get("stock_code") or "").strip()
        if not code:
            continue  # 시세조회 불가·랭킹 대상 아님(코드결측 제외)
        day = _parse_day(it.get("rcept_dt"))
        tags = it.get("tags") or []
        rec = agg.setdefault(code, {
            "name": it.get("corp_name", ""),
            "market": it.get("market", ""),
            "corp_cls": it.get("corp_cls", ""),
            "discs": [],
        })
        rec["discs"].append({
            "day": day,
            "tags": tags,
            "report_nm": it.get("report_nm", ""),
            "rcept_no": it.get("rcept_no", ""),
            "mat": _material_weight(tags),
        })
    return agg


def build_disclosure_hot(agg, ref_day, top_n=15):
    """공시빈도 랭킹: 건수 + 최근성·중요도 가중 스코어 내림차순.
    score = Σ (mat * recency). count=총 건수. 동점은 최신 rcept_no 우선."""
    rows = []
    for code, rec in agg.items():
        discs = rec["discs"]
        score = sum(d["mat"] * _recency_weight(d["day"], ref_day) for d in discs)
        latest_no = max((d["rcept_no"] for d in discs), default="")
        types = {}
        for d in discs:
            for t in d["tags"]:
                types[t] = types.get(t, 0) + 1
        top_types = [t for t, _ in sorted(types.items(), key=lambda kv: kv[1], reverse=True)][:3]
        rows.append({
            "code": code,
            "name": rec["name"],
            "market": rec["market"],
            "count": len(discs),
            "score": round(score, 3),
            "types": top_types,
            "latest_rcept_no": latest_no,
        })
    rows.sort(key=lambda r: (r["score"], r["latest_rcept_no"]), reverse=True)
    return rows[:top_n]


def build_buzz(agg, ref_day, top_n=15, window_days=1):
    """화제도(buzz) — 프록시: 최근 window_days 창의 공시 급증(acceleration).

    실제 조회수 데이터가 없으므로 '최근 창 활동 / 이전 창 활동' 가속을 화제도로
    근사한다. recent = 최근 window_days 내 mat 가중 합, prior = 그 이전(피드창) 합.
    burst = recent 건수 - prior 건수(양수=가속). score = recent*(1 + max(0,burst)/(prior건수+1)).
    최근 창에 공시가 하나도 없는 종목은 화제도 후보에서 제외."""
    if ref_day is None:
        return []
    rows = []
    for code, rec in agg.items():
        recent_w = prior_w = 0.0
        recent_c = prior_c = 0
        for d in rec["discs"]:
            if d["day"] is None:
                prior_w += d["mat"] * 0.5
                prior_c += 1
                continue
            days_ago = (ref_day - d["day"]).days
            if 0 <= days_ago < window_days:
                recent_w += d["mat"]
                recent_c += 1
            else:
                prior_w += d["mat"] * _recency_weight(d["day"], ref_day)
                prior_c += 1
        if recent_c == 0:
            continue
        burst = recent_c - prior_c
        score = recent_w * (1.0 + max(0, burst) / (prior_c + 1))
        rows.append({
            "code": code,
            "name": rec["name"],
            "market": rec["market"],
            "recent_count": recent_c,
            "prior_count": prior_c,
            "buzz_score": round(score, 3),
        })
    rows.sort(key=lambda r: (r["buzz_score"], r["recent_count"]), reverse=True)
    return rows[:top_n]


def build_price_movers(candidates, price_fn, movers_n=15):
    """급등락: 후보 종목(candidates=[{code,name,market}...])에 price_fn 적용.

    price_fn(codes:list[str]) -> ({code:{price,prev_close,change_pct,volume}}, stats)
    price_fn 이 None 이거나 결과가 비면 graceful degrade(빈 movers + 사유).
    반환: (gainers[], losers[], price_meta)."""
    codes = [c["code"] for c in candidates]
    meta = {"requested": len(codes), "resolved": 0, "toss_calls": 0,
            "errors": [], "degraded": False, "reason": None}
    if not price_fn:
        meta["reason"] = "price_fn 미주입(가격데이터 없음)"
        meta["degraded"] = True
        return [], [], meta
    try:
        results, stats = price_fn(codes)
    except Exception as e:  # noqa: BLE001
        meta["reason"] = f"price_fn 예외: {type(e).__name__}: {e}"
        meta["degraded"] = True
        return [], [], meta
    meta.update({
        "resolved": stats.get("resolved", len(results)),
        "toss_calls": stats.get("toss_calls", 0),
        "errors": stats.get("errors", []),
        "degraded": stats.get("degraded", False),
    })
    name_by_code = {c["code"]: c for c in candidates}
    enriched = []
    for code, pr in results.items():
        if pr.get("change_pct") is None:
            continue
        base = name_by_code.get(code, {})
        enriched.append({
            "code": code,
            "name": base.get("name", ""),
            "market": base.get("market", ""),
            "price": pr.get("price"),
            "change_pct": pr.get("change_pct"),
            "volume": pr.get("volume"),
        })
    gainers = sorted(enriched, key=lambda r: r["change_pct"], reverse=True)[:movers_n]
    losers = sorted(enriched, key=lambda r: r["change_pct"])[:movers_n]
    if not enriched and not meta["reason"]:
        meta["reason"] = "유효 등락 산출 0건(일봉 부족/조회실패)"
    return gainers, losers, meta


def build_ranking_payload(feed, price_fn=None, hot_n=15, buzz_n=15,
                          movers_n=15, cand_cap=20, buzz_window_days=1):
    """랭킹 payload 조립(순수). feed=app._get_feed 산출 dict.

    price_movers 후보 = disclosure_hot ∪ buzz 상위에서 코드 유일화 후 cand_cap 개
    (TOSS 콜예산 = 후보 수. 앱키 thrash 회피용 상한)."""
    alerts = feed.get("alerts") if isinstance(feed, dict) else None
    alerts = alerts or []
    agg = _aggregate(alerts)
    ref_day = max((d for rec in agg.values() for d in
                   (x["day"] for x in rec["discs"]) if d is not None), default=None)

    disclosure_hot = build_disclosure_hot(agg, ref_day, top_n=hot_n)
    buzz = build_buzz(agg, ref_day, top_n=buzz_n, window_days=buzz_window_days)

    # 후보 = hot ∪ buzz 순서 보존 유일화, cand_cap 상한.
    seen, cands = set(), []
    for r in disclosure_hot + buzz:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        cands.append({"code": r["code"], "name": r["name"], "market": r["market"]})
        if len(cands) >= cand_cap:
            break

    gainers, losers, price_meta = build_price_movers(cands, price_fn, movers_n=movers_n)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "feed_generated_at": feed.get("generated_at") if isinstance(feed, dict) else None,
        "ref_day": ref_day.isoformat() if ref_day else None,
        "disclosure_hot": disclosure_hot,
        "buzz": buzz,
        "price_movers": {"gainers": gainers, "losers": losers},
        "meta": {
            "feed_alerts": len(alerts),
            "ranked_stocks": len(agg),
            "candidate_cap": cand_cap,
            "price_candidates": len(cands),
            "price_meta": price_meta,
            "buzz_proxy": (
                f"실제 조회수 데이터 없음 → 최근 {buzz_window_days}일 창 공시 급증"
                "(acceleration=recent/prior)으로 근사"),
            "recency_half_life_days": 2.0,
            "tag_weight": _TAG_WEIGHT,
        },
    }
