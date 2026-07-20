# -*- coding: utf-8 -*-
"""메자닌 레코드 -> 캘린더/집계 뷰.

(a) build_calendar      : 청구/행사 개시일 기준 시간순 캘린더
(b) build_holdings      : 종목별 전환/행사 가능 물량 집계 뷰
DART 콜 0. 순수 in-memory 가공.
"""
from datetime import date
from typing import List

# collect 은 flat import (demo 가 sys.path 에 모듈 디렉터리 삽입)로도,
# 패키지 import 로도 동작하게 이중 시도.
try:
    from collect import MezzRecord  # type: ignore
except ImportError:  # pragma: no cover
    from features.mezzanine_calendar.collect import MezzRecord  # type: ignore


def build_calendar(records: List["MezzRecord"], upcoming_only: bool = False,
                   ref_date: date = None):
    """청구/행사 개시일(start_date) 기준 오름차순 캘린더.

    반환: (calendar_items: list[dict], skipped_no_start: int)
    start_date 가 None 인 레코드는 제외하고 카운트.
    upcoming_only=True 면 ref_date(기본 today) 이후 개시분만.
    """
    ref = ref_date or date.today()
    items = []
    skipped = 0
    for r in records:
        if r.start_date is None:
            skipped += 1
            continue
        if upcoming_only and r.start_date < ref:
            continue
        items.append({
            "date": r.start_date.isoformat(),
            "sec_type": r.sec_type,
            "corp_name": r.corp_name,
            "corp_code": r.corp_code,
            "stock_code": r.stock_code,
            "rcept_no": r.rcept_no,
            "conv_price": r.conv_price,
            "shares": r.shares,
            "vs_pct": r.vs_pct,
            "issue_amount": r.issue_amount,
            "end_date": r.end_date.isoformat() if r.end_date else None,
            "maturity_date": r.maturity_date.isoformat() if r.maturity_date else None,
            "event": "전환/행사 개시",
        })
    items.sort(key=lambda x: (x["date"], x["corp_name"]))
    return items, skipped


def build_holdings(records: List["MezzRecord"], ref_date: date = None):
    """종목(corp_code)별 전환/행사 가능 물량 집계.

    - total_shares       : 발행 전환/행사가능 주식수 합
    - active_shares      : ref_date 가 [start,end] 구간 내인 트랜치의 주식수 합
                           (=현재 청구/행사 가능 = 미상환·희석대기 물량 근사)
    - total_issue_amount : 발행총액 합
    - tranches           : 개별 트랜치 요약 리스트
    반환: list[dict] (active_shares desc 정렬)
    """
    ref = ref_date or date.today()
    agg = {}
    for r in records:
        key = r.corp_code
        a = agg.get(key)
        if a is None:
            a = {
                "corp_code": r.corp_code,
                "corp_name": r.corp_name,
                "stock_code": r.stock_code,
                "sec_types": set(),
                "tranche_count": 0,
                "total_shares": 0,
                "active_shares": 0,
                "total_issue_amount": 0,
                "min_conv_price": None,
                "tranches": [],
            }
            agg[key] = a
        a["sec_types"].add(r.sec_type)
        a["tranche_count"] += 1
        if r.shares:
            a["total_shares"] += r.shares
        if r.issue_amount:
            a["total_issue_amount"] += r.issue_amount
        if r.conv_price:
            if a["min_conv_price"] is None or r.conv_price < a["min_conv_price"]:
                a["min_conv_price"] = r.conv_price
        active = (
            r.start_date is not None and r.end_date is not None
            and r.start_date <= ref <= r.end_date
        )
        if active and r.shares:
            a["active_shares"] += r.shares
        a["tranches"].append({
            "sec_type": r.sec_type,
            "rcept_no": r.rcept_no,
            "conv_price": r.conv_price,
            "shares": r.shares,
            "vs_pct": r.vs_pct,
            "start_date": r.start_date.isoformat() if r.start_date else None,
            "end_date": r.end_date.isoformat() if r.end_date else None,
            "active": active,
        })

    out = []
    for a in agg.values():
        a["sec_types"] = sorted(a["sec_types"])
        out.append(a)
    out.sort(key=lambda x: (-x["active_shares"], -x["total_shares"]))
    return out
