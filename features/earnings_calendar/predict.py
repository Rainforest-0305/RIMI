# -*- coding: utf-8 -*-
"""소스1 파생: 종목별 '다음 예상 발표일' 추정(데이터 기반).

방법(우선순위):
  1) 'season_lag'  — 과거 원본(정정 제외) 정기보고서의 결산기말(fy.fm) 대비
     실제 접수일(rcept_dt) 지연일수(lag) 의 계절성을 사용. 종목이 이미 제출한
     가장 최근 회계기간의 '다음 분기 슬롯'을 정하고, 그 슬롯 리포트 종류의
     과거 median lag 를 결산기말에 더해 예측한다.
  2) 'yoy'         — 같은 슬롯(같은 fm)의 전년 접수일 + 연간주기(median) 로 교차검증.
  3) 'statutory'   — 이력/괄호 파싱이 부족할 때 한국 법정기한 폴백
     (사업보고서 결산후 90일, 분기·반기 45일). 신뢰도 low.

각 예측 레코드에 method(방식) 와 confidence(신뢰도) 라벨을 남긴다.
DART 콜 없음(순수 계산).
"""
import os
import sys
import calendar
import statistics
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(os.path.dirname(_HERE))):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import collect  # noqa: E402  (동일 패키지 모듈, 경로 부트스트랩 후 import)

# 회계기말(fm) → 리포트 종류. 한국 상장사 분기공시 체계.
FM_TO_TYPE = {3: "분기보고서", 6: "반기보고서", 9: "분기보고서", 12: "사업보고서"}
# 법정 제출기한(결산기말 이후 일수). 폴백용.
STATUTORY_LAG = {"사업보고서": 90, "반기보고서": 45, "분기보고서": 45}
# 분기 슬롯 진행: 현재 fm → (다음 fm, 연도증가)
_NEXT_SLOT = {3: (6, 0), 6: (9, 0), 9: (12, 0), 12: (3, 1)}


def _to_date(yyyymmdd):
    try:
        return date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))
    except (ValueError, TypeError, IndexError):
        return None


def _period_end(fy, fm):
    """회계기말의 말일 date."""
    last = calendar.monthrange(fy, fm)[1]
    return date(fy, fm, last)


def predict_corp(rows):
    """단일 종목(그룹 rows: rcept_dt 오름차순) → 예측 레코드 dict 또는 None.

    rows 는 collect.group_by_corp 의 한 그룹(정기보고서만, amend 포함).
    """
    if not rows:
        return None
    meta = rows[-1]  # 최신 레코드를 종목 대표 메타로
    # 원본(정정 제외) + 괄호 파싱 성공분만 계절성 계산에 사용
    orig = [r for r in rows
            if not r.get("amend") and r.get("fy") and r.get("fm")
            and _to_date(r.get("rcept_dt"))]

    base = {
        "corp_code": meta.get("corp_code", ""),
        "corp_name": meta.get("corp_name", ""),
        "stock_code": meta.get("stock_code", ""),
        "market": meta.get("market", ""),
        "history_count": len(rows),
        "orig_count": len(orig),
    }

    if not orig:
        # 이력 없음 → 예측 불가(메타만). 호출측에서 스킵 카운트.
        base.update({"predicted_date": None, "method": "no_history",
                     "confidence": "none", "basis": "괄호 파싱 가능한 원본 이력 없음"})
        return base

    # 슬롯별(report_type, fm) lag 히스토리
    lags_by_type = {}   # report_type -> [lag_days...]
    by_slot = {}        # fm -> [(fy, rcept_date)...]
    for r in orig:
        rd = _to_date(r["rcept_dt"])
        pe = _period_end(r["fy"], r["fm"])
        lag = (rd - pe).days
        if -5 <= lag <= 200:  # 비정상치(정정 잔재 등) 배제
            lags_by_type.setdefault(r["report_type"], []).append(lag)
        by_slot.setdefault(r["fm"], []).append((r["fy"], rd))

    # 이미 제출한 가장 최근 회계기간(fy, fm)
    last_fy, last_fm = max((r["fy"], r["fm"]) for r in orig)
    nfm, dy = _NEXT_SLOT.get(last_fm, (3, 1))
    target_fy, target_fm = last_fy + dy, nfm
    target_type = FM_TO_TYPE[target_fm]
    target_pe = _period_end(target_fy, target_fm)

    # ---- 방법1: season_lag ----
    lags = lags_by_type.get(target_type, [])
    method = None
    confidence = "low"
    predicted = None
    basis = ""
    lag_used = None
    if lags:
        lag_used = int(round(statistics.median(lags)))
        predicted = target_pe + timedelta(days=lag_used)
        method = "season_lag"
        n = len(lags)
        std = statistics.pstdev(lags) if n >= 2 else 999
        if n >= 3 and std <= 7:
            confidence = "high"
        elif n >= 2 and std <= 15:
            confidence = "medium"
        else:
            confidence = "low"
        basis = (f"{target_type} 과거 {n}건의 결산기말 대비 접수지연 "
                 f"median={lag_used}일(σ={std:.1f}) 를 {target_fy}.{target_fm:02d} "
                 f"결산기말({target_pe.isoformat()})에 가산")

    # ---- 방법2: yoy 교차검증(같은 fm 전년 접수일 + 연주기) ----
    yoy_pred = None
    slot_hist = sorted(by_slot.get(target_fm, []), key=lambda x: x[0])
    if len(slot_hist) >= 1:
        prev_fy, prev_rd = slot_hist[-1]
        # 연주기 median (같은 슬롯 연속 접수일 간격)
        if len(slot_hist) >= 2:
            deltas = [(slot_hist[i][1] - slot_hist[i - 1][1]).days
                      for i in range(1, len(slot_hist))]
            yearly = int(round(statistics.median(deltas)))
        else:
            yearly = 365
        # 전년 대비 목표연도까지 반복 가산
        step = max(1, target_fy - prev_fy)
        yoy_pred = prev_rd + timedelta(days=yearly * step)

    if predicted is None and yoy_pred is not None:
        predicted = yoy_pred
        method = "yoy"
        confidence = "medium" if len(slot_hist) >= 2 else "low"
        basis = (f"{target_type}({target_fm:02d}월기) 전년 접수일 {prev_rd.isoformat()} "
                 f"+ 연주기 {yearly}일×{step}")

    # ---- 방법3: statutory 폴백 ----
    if predicted is None:
        lag_used = STATUTORY_LAG[target_type]
        predicted = target_pe + timedelta(days=lag_used)
        method = "statutory"
        confidence = "low"
        basis = (f"이력 부족 → 법정기한 폴백: {target_type} 결산기말"
                 f"({target_pe.isoformat()}) + {lag_used}일")

    base.update({
        "target_period": f"{target_fy}.{target_fm:02d}",
        "target_type": target_type,
        "period_end": target_pe.isoformat(),
        "predicted_date": predicted.isoformat(),
        "predicted_lag_days": lag_used,
        "yoy_crosscheck": yoy_pred.isoformat() if yoy_pred else None,
        "method": method,
        "confidence": confidence,
        "basis": basis,
        "last_filed_period": f"{last_fy}.{last_fm:02d}",
        "last_filed_rcept": max(orig, key=lambda r: r["rcept_dt"])["rcept_dt"],
    })
    return base


def predict_all(groups):
    """그룹 dict → 예측 레코드 list(예측 성공분만) + 스킵수."""
    preds = []
    skipped = 0
    for key, rows in groups.items():
        p = predict_corp(rows)
        if p and p.get("predicted_date"):
            preds.append(p)
        else:
            skipped += 1
    # 예상 발표일 오름차순
    preds.sort(key=lambda x: x["predicted_date"])
    return preds, skipped


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    recs, stats = collect.load_all()
    g = collect.group_by_corp(recs)
    preds, skipped = predict_all(g)
    print("stats:", stats, "corps:", len(g),
          "preds:", len(preds), "skipped:", skipped)
    for p in preds[:3]:
        print(p["stock_code"], p["corp_name"], p["predicted_date"],
              p["method"], p["confidence"])
