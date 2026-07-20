# -*- coding: utf-8 -*-
"""WS-34 순잔량(net remaining) 오버행 집계 (격리 모듈).

세 소스 결합으로 종목별 '현재 실제 남은 전환/행사 가능 주식수'를 산출:
  A. 발행결정(bench_cache/amounts, 0콜)     : 회차별 최초 발행 주식수/가(gross)
  B. 리픽싱(refixing.collect_refixing)       : 회차별 조정후 가/잔량(최신 채택)
  C. 행사실적(conversion.collect_conversion) : 최근 행사공시 잔액표 = 현재 순잔량

우선순위(회차별):
  1) sec_type 에 행사공시가 있으면 → 그 최신 잔액표가 권위(리픽싱/전환 반영).
       - 잔액표에 있는 회차: net = 전환가능주식수(잔량), price = 잔액표 전환가액.
       - 잔액표에 없는 회차: 완전전환/상환으로 소멸(net=0). 단, 그 회차 리픽싱이
         잔액표 공시일보다 최신이면 아직 살아있다고 보고 refix 잔량 채택.
  2) 행사공시가 없으면 → 리픽싱 조정후 잔량(있으면), 없으면 발행 gross.

산출은 gross 와 함께 저장해 교정 전/후 대조 가능. DART 콜은 refix/conv 수집분만.
"""
import glob
import json
import os
import re

try:
    import refixing
    import conversion
except ImportError:  # pragma: no cover
    from features.mezzanine_calendar import refixing, conversion

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))

# 발행결정 파일 prefix -> (sec_type, price_key, shares_key, end_key)
#   end_key = 전환/행사/교환 청구 종료일 → 이 날이 지나면 더는 전환불가 = 오버행 소멸.
_ISSUE_SPECS = {
    "cvbdIsDecsn": ("CB", "cv_prc", "cvisstk_cnt", "cvrqpd_edd"),
    "bdwtIsDecsn": ("BW", "ex_prc", "nstk_isstk_cnt", "expd_edd"),
    "exbdIsDecsn": ("EB", "ex_prc", "extg_stkcnt", "exrqpd_edd"),
}

_KDATE_RE = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})")


def _to_int(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    m = re.search(r"\d[\d,]*", str(s))
    return int(m.group(0).replace(",", "")) if m else None


def _kdate_iso(s):
    """'YYYY년 MM월 DD일' 또는 유사 -> 'YYYY-MM-DD' | None."""
    if not s:
        return None
    m = _KDATE_RE.search(str(s))
    if not m:
        return None
    try:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    except ValueError:
        return None


def load_issuance_tranches(corp_code, amounts_dir=None):
    """발행결정 캐시에서 회차별 최초 발행량/가/청구종료일.
    {sec_type:{회차:{orig_shares,orig_price,end_date}}}."""
    amounts_dir = amounts_dir or os.path.join(_REPO_ROOT, "bench_cache", "amounts")
    universe = {}
    cc = str(corp_code).zfill(8)
    for prefix, (sec, pkey, skey, ekey) in _ISSUE_SPECS.items():
        fp = os.path.join(amounts_dir, f"{prefix}_{cc}.json")
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for row in data.values():
            if not isinstance(row, dict):
                continue
            tr = _to_int(row.get("bd_tm"))
            if tr is None:
                continue
            shares = _to_int(row.get(skey))
            price = _to_int(row.get(pkey))
            end_date = _kdate_iso(row.get(ekey))
            d = universe.setdefault(sec, {})
            # 동일 회차 중복이면 주식수 큰 쪽(원발행) 유지
            if tr not in d or (shares or 0) > (d[tr].get("orig_shares") or 0):
                d[tr] = {"orig_shares": shares, "orig_price": price,
                         "end_date": end_date}
    return universe


def _latest_refix_by_tranche(refix_events):
    """{(sec,회차): {price_after, shares_after, adjust_date}} 최신(adjust_date max)."""
    best = {}
    for e in refix_events:
        sec = e.get("sec_type", "CB")
        tr = e.get("tranche")
        if tr is None:
            continue
        key = (sec, tr)
        ad = e.get("adjust_date") or ""
        if key not in best or ad > (best[key].get("adjust_date") or ""):
            best[key] = {
                "price_after": e.get("conv_price_after"),
                "shares_after": e.get("shares_after"),
                "adjust_date": ad,
            }
    return best


def compute_overhang(corp_code, corp_name="", stock_code=None,
                     bgn_de="20220101", end_de=None, use_cache=True,
                     refresh_list=True, as_of=None):
    """종목 순잔량 오버행 스냅샷 dict 산출."""
    issuance = load_issuance_tranches(corp_code)
    refix = refixing.collect_refixing(corp_code, bgn_de=bgn_de, end_de=end_de,
                                      use_cache=use_cache, refresh_list=refresh_list)
    conv = conversion.collect_conversion(corp_code, bgn_de=bgn_de, end_de=end_de,
                                         use_cache=use_cache, refresh_list=refresh_list)

    refix_best = _latest_refix_by_tranche(refix.get("events", []))
    bal_by_tr = conv.get("balance_by_tranche", {})   # {sec:{회차:{remaining,unconv,price,date}}}
    as_of_cmp = (as_of or "9999-99-99")

    sec_types = set(issuance) | {s for (s, _t) in refix_best} | set(bal_by_tr)

    tranches_out = {}   # "SEC:회차" -> {...}
    net_total = 0
    gross_total = 0
    expired_dropped = 0
    for sec in sorted(sec_types):
        secbal = bal_by_tr.get(sec, {})
        trs = set(issuance.get(sec, {})) | set(secbal) \
            | {t for (s, t) in refix_best if s == sec}

        for tr in sorted(trs):
            orig = issuance.get(sec, {}).get(tr, {})
            orig_shares = orig.get("orig_shares")
            orig_price = orig.get("orig_price")
            end_date = orig.get("end_date")
            rf = refix_best.get((sec, tr))
            bal = secbal.get(tr)
            gross_total += (orig_shares or 0)

            # 오버행 잣대: 전환청구기간이 끝났으면(end_date < as_of) 더는 주식발행
            # 불가 → 순잔량 0(현금상환/만료). 발행정보가 없어 end_date 미상이면
            # 보수적으로 살아있다고 본다(과소계상 금지).
            window_ended = bool(end_date) and end_date < as_of_cmp

            net = price = source = as_of_date = None
            if window_ended:
                net, price, source = 0, (orig_price if not rf else rf.get("price_after")), "expired"
                as_of_date = end_date
                expired_dropped += (orig_shares or 0)
            elif bal is not None:
                # 회차별 최근언급 잔액이 권위. 단 리픽싱이 그보다 최신이면 리픽싱.
                if rf and (rf.get("adjust_date") or "") > (bal.get("date") or ""):
                    net, price, source = rf.get("shares_after"), rf.get("price_after"), "refixing"
                    as_of_date = rf.get("adjust_date")
                else:
                    net, price, source = bal["remaining_conv_shares"], bal["conv_price"], "conversion"
                    as_of_date = bal.get("date")
            elif rf:
                net, price, source = rf.get("shares_after"), rf.get("price_after"), "refixing"
                as_of_date = rf.get("adjust_date")
            else:
                # 발행됐고 잔액/리픽싱 기록 없음 = 미전환 전량(기간 미개시/개시 후 무행사).
                net, price, source = orig_shares, orig_price, "issuance"

            net_total += (net or 0)
            tranches_out[f"{sec}:{tr}"] = {
                "sec_type": sec, "tranche": tr,
                "net_remaining_shares": net,
                "latest_conv_price": price,
                "orig_shares": orig_shares,
                "orig_price": orig_price,
                "end_date": end_date,
                "source": source,
                "as_of_date": as_of_date,
                "unconv_balance": bal["unconv_balance"] if bal else None,
            }

    # 최신 전환가(가장 낮은, net>0 트랜치 기준) — moneyness/premium 재계산용
    live_prices = [v["latest_conv_price"] for v in tranches_out.values()
                   if (v.get("net_remaining_shares") or 0) > 0 and v.get("latest_conv_price")]
    min_conv_price = min(live_prices) if live_prices else None

    return {
        "corp_code": str(corp_code).zfill(8),
        "corp_name": corp_name,
        "stock_code": stock_code,
        "as_of": as_of,
        "sec_types": sorted(sec_types),
        "tranches": tranches_out,
        "net_remaining_total": net_total,
        "gross_shares_total": gross_total,
        "expired_shares_dropped": expired_dropped,
        "min_conv_price": min_conv_price,
        "latest_conv_price_by_tranche": {
            k: v["latest_conv_price"] for k, v in tranches_out.items()},
        "net_remaining_shares_by_tranche": {
            k: v["net_remaining_shares"] for k, v in tranches_out.items()},
        "coverage": {
            "refix_reports": refix.get("report_count", 0),
            "refix_doc_ok": refix.get("doc_ok", 0),
            "refix_doc_fail": refix.get("doc_fail", 0),
            "conv_reports": conv.get("report_count", 0),
            "conv_doc_ok": conv.get("doc_ok", 0),
            "conv_doc_fail": conv.get("doc_fail", 0),
            "refix_list_err": refix.get("list_err"),
            "conv_list_err": conv.get("list_err"),
            "dart_calls": (refix.get("list_calls", 0) + refix.get("doc_calls", 0)
                           + conv.get("list_calls", 0) + conv.get("doc_calls", 0)),
        },
    }
