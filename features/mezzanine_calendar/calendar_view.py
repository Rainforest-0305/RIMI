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


def build_monthly_outlook(calendar_items, ref_date: date = None):
    """이번 달 / 다음 달 예상 전환·행사 개시 공시 건수 집계(순수함수, DART 0콜).

    근거(집계 소스): build_calendar(records, upcoming_only=True) 가 만든
    calendar_items 를 재사용한다. 각 item["date"] 는 'YYYY-MM-DD' 형식의
    전환/행사(청구) 개시일이다. 이미 메모리에 있는 리스트만 순회하므로
    신규 DART/시세 콜은 0.

    집계 로직:
      - month              : ref_date(기본 today)의 'YYYY-MM'
      - count              : item["date"] 의 연-월이 이번 달인 item 수
      - by_type            : 이번 달 item 을 sec_type(CB/BW/EB)별로 카운트
      - next_month.month   : 다음 달 'YYYY-MM'
      - next_month.count   : item["date"] 의 연-월이 다음 달인 item 수

    주의: calendar_items 가 upcoming_only=True 로 생성됐다면 이번 달 카운트는
    ref_date '이후' 개시분만 포함한다(이미 지난 이번 달 개시건은 build_calendar
    단계에서 제외됨). 과거 포함 전체월 집계가 필요하면 upcoming_only=False 로
    만든 items 를 넘기면 된다.

    반환: {"month":str, "count":int, "by_type":{"CB":int,"BW":int,"EB":int},
           "next_month":{"month":str, "count":int}}
    """
    ref = ref_date or date.today()
    cur_ym = f"{ref.year:04d}-{ref.month:02d}"
    if ref.month == 12:
        nxt_y, nxt_m = ref.year + 1, 1
    else:
        nxt_y, nxt_m = ref.year, ref.month + 1
    nxt_ym = f"{nxt_y:04d}-{nxt_m:02d}"

    by_type = {"CB": 0, "BW": 0, "EB": 0}
    cur_count = 0
    nxt_count = 0
    for it in calendar_items:
        ym = (it.get("date") or "")[:7]  # 'YYYY-MM'
        if ym == cur_ym:
            cur_count += 1
            st = it.get("sec_type")
            if st in by_type:
                by_type[st] += 1
        elif ym == nxt_ym:
            nxt_count += 1
    return {
        "month": cur_ym,
        "count": cur_count,
        "by_type": by_type,
        "next_month": {"month": nxt_ym, "count": nxt_count},
    }
