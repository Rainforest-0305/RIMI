# -*- coding: utf-8 -*-
"""규모보정(이벤트 규모 × 유형) 벤치마크 — 별도 산출 후 impact_benchmark.json 병합.

문제의식: 같은 '유상증자'라도 시총 3,000억 회사의 200억 증자(희석 ~6.7%)와
시총 100억 회사의 200억 증자(희석 ~200%)는 시장 반응이 다르다. 따라서 유형만이
아니라 (유형 × 규모버킷)으로 재집계한다.

상대규모 = 이벤트 금액 / 사건시점 시총 (%).
  - 유상증자: 조달금액(fdpp_* 합) / 시총  (≈ 희석률)
  - 전환사채·BW·EB: 사채 권면총액(bd_fta) / 시총  (≈ 잠재 희석률)
  - 자사주: 취득/처분/신탁 예정금액 / 시총  (≈ 매입/처분 수익률 규모)

금액은 DART '주요사항보고서 주요정보' 구조화 엔드포인트에서 rcept_no 단위로
추출(문서 전량 파싱 안 함). 시총은 pykrx get_market_cap_by_date 캐시.
CAR(초과등락)은 build_impact_benchmark 와 동일 방법(익일 시가 진입, 1/5/21거래일
보유, 자기시장 EW 보정)으로 재계산 → 유형 벤치마크와 일관.

산출: scale_buckets.json  (독립 파일; a08ba34가 impact_benchmark.json 갱신 완료
후 merge 서브커맨드로 병합). app.py/index.html 직접 수정 안 함 — 스키마만 제공.

서브커맨드:
  py scale_extract.py census        # 대상 사건/콜예산 산정(로컬)
  py scale_extract.py mcap          # pykrx 시총 수집(KRX, DART 비경합)
  py scale_extract.py amounts       # DART 금액추출(무겁다 — a08ba34 완료 후)
  py scale_extract.py aggregate     # 규모버킷 집계 -> scale_buckets.json
  py scale_extract.py merge         # impact_benchmark.json 에 scale 필드 병합
"""
import bisect
import io
import json
import re
import statistics
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests

import config

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent
CACHE = BASE / "bench_cache"
DART_CACHE = CACHE / "dart"
PX_CACHE = CACHE / "px"
AMT_CACHE = CACHE / "amounts"
MCAP_CACHE = CACHE / "mcap"
DOC_CACHE = CACHE / "docs"       # document.xml 파싱결과 캐시(rcept_no 단위, DART 0콜 재사용)
for d in (AMT_CACHE, MCAP_CACHE, DOC_CACHE):
    d.mkdir(parents=True, exist_ok=True)
LOG = CACHE / "scale_build.log"
OUT = BASE / "scale_buckets.json"
IMPACT = BASE / "impact_benchmark.json"
KEY = config.DART_API_KEY

HORIZONS = [("d", 0), ("w", 4), ("m", 20)]

# report_nm(공백제거) -> (endpoint, 대표유형). 순서 = 우선(구체 먼저).
ROUTES = [
    ("유상증자결정",              ("piicDecsn",            "유상증자")),
    ("자기주식취득신탁계약체결",  ("tsstkAqTrctrCcDecsn",  "자사주")),
    ("자기주식취득결정",          ("tsstkAqDecsn",         "자사주")),
    ("자기주식처분결정",          ("tsstkDpDecsn",         "자사주")),
    ("전환사채권발행결정",        ("cvbdIsDecsn",          "전환사채")),
    ("신주인수권부사채권발행결정",("bdwtIsDecsn",          "전환사채")),
    ("교환사채권발행결정",        ("exbdIsDecsn",          "전환사채")),
]

# 숫자 bullet 전용 추가 구조화 라우트(스케일버킷 대상 아님 — BUCKETS 미포함).
# 무상증자는 OpenDART 구조화 EP(fricDecsn)가 있어 캐시/구조화 경로 그대로 사용.
BULLET_STRUCT_ROUTES = [
    ("무상증자결정", ("fricDecsn", "무상증자")),
]

# 문서파싱 기반 bullet 라우트. 아래 3유형은 OpenDART 에 이벤트단위(rcept_no) 구조화
# JSON 엔드포인트가 없다(실측 확인 2026-07: 101 잘못된 URL). 대신 document.xml
# (KRX 표준양식, cp949) 을 1콜 받아 표준필드를 정규식 추출·캐시한다(피드는 캐시 0콜).
DOC_ROUTES = [
    ("공급계약체결", "공급계약"),   # 단일판매ㆍ공급계약체결
    ("현물배당결정", "배당"),       # 현금ㆍ현물배당결정
    ("소각결정",     "소각"),       # 주식소각결정
]

# 규모버킷 경계(상대규모 = 금액/시총, %). 유형별 적정 경계.
# 유상/전환은 희석 스케일(수십%까지), 자사주는 매입수익률 스케일(수%).
BUCKETS = {
    "유상증자": [(0, 10, "소<10%"), (10, 30, "중10-30%"), (30, 1e9, "대30%+")],
    "전환사채": [(0, 10, "소<10%"), (10, 25, "중10-25%"), (25, 1e9, "대25%+")],
    "자사주":   [(0, 1, "소<1%"),   (1, 3, "중1-3%"),     (3, 1e9, "대3%+")],
}

# 문서파싱/구조화 신규 유형 규모버킷. 분모(rel)가 유형마다 다르다(시총 아님):
#   공급계약: rel = 계약금액 / 최근연매출 %      (rev_pct)
#   소각:     rel = 소각주식 / 발행총수 %        (pct)
#   무상증자: rel = 무상신주 / 증자전발행총수 %  (nstk_ostk_cnt/bfic_tisstk_ostk)
#   배당:     rel = 시가배당률 %                 (yield)  ← Phase2(버킷 미집계, scale_only 유지)
# 경계 근거(census 표본 분포, 2026-07 산출):
#   공급계약(캐시 n=60):  p25=5.9 p50=15.0 p75=23.3 max=183  → 소<10 / 중10-30 / 대30+
#     (버킷별 표본 25/22/13 → 균형)
#   소각(파일럿 n=149):   p25=1.13 p50=2.50 p75=4.64 max=100 → 소<1 / 중1-3 / 대3+
#     (버킷별 28/56/65 → 균형, 자사주와 컷 일치)
#   무상증자(파일럿 n=156): p25=9.7 p50=96.6 p75=100 max=800 (이봉: <10% 소액, ~100% 1:1)
#     → 소<20 / 중20-100 / 대100+ (버킷별 53/41/62 → 균형, 100%=1주당1주 무상 경계)
#   배당(파일럿 n=1869, 2026-07): p25=1.0 p50=1.8 p75=3.0 max=22.2
#     → 소<1.5% / 중1.5-3% / 대3%+ (버킷별 757/636/476 → 균형; 3%+=고배당 top~25%,
#       p75 경계와 일치. 컷 후보 비교상 (1.5,3)이 표본 최균형)
BUCKETS_DOC = {
    "공급계약": [(0, 10, "소<10%"),  (10, 30, "중10-30%"),  (30, 1e9, "대30%+")],
    "소각":     [(0, 1, "소<1%"),    (1, 3, "중1-3%"),      (3, 1e9, "대3%+")],
    "무상증자": [(0, 20, "소<20%"),  (20, 100, "중20-100%"), (100, 1e9, "대100%+")],
    "배당":     [(0, 1.5, "소<1.5%"), (1.5, 3, "중1.5-3%"),  (3, 1e9, "대3%+")],
}
# scale_lookup 이 status:ok 시 반환할 rel_label(분모 설명).
REL_LABELS = {
    "공급계약": "최근매출 대비",
    "소각":     "발행주식 대비",
    "배당":     "시가배당률",
    "무상증자": "무상신주 비율",
}
# stype(집계 라벨) -> impact_benchmark.json 최상위 키. 소각만 라벨 상이.
STYPE_BENCH_KEY = {"소각": "주식소각"}

MIN_N = 20  # 버킷 자기표본 최소. 미만이면 conf=참고(앱은 유형레벨로 폴백 가능).


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _num(v):
    """'186,516,861,300' / '-' / '' -> float | None."""
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "해당사항없음", "미해당"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def route(report_nm):
    nm = (report_nm or "").replace(" ", "")
    for kw, ep_typ in ROUTES:
        if kw in nm:
            return ep_typ
    return None, None


def struct_route(report_nm):
    """숫자 bullet용 구조화 라우트(스케일 ROUTES + 무상증자 등 bullet전용). (ep, typ)."""
    nm = (report_nm or "").replace(" ", "")
    for kw, ep_typ in ROUTES:
        if kw in nm:
            return ep_typ
    for kw, ep_typ in BULLET_STRUCT_ROUTES:
        if kw in nm:
            return ep_typ
    return None, None


def doc_route(report_nm):
    """document.xml 파싱 대상 유형 라벨. 없으면 None."""
    nm = (report_nm or "").replace(" ", "")
    for kw, typ in DOC_ROUTES:
        if kw in nm:
            return typ
    return None


def bullet_eligible(report_nm):
    """숫자 bullet 생성 가능 유형인가(구조화 or 문서파싱). 피드 게이트용."""
    return bool(struct_route(report_nm)[0]) or bool(doc_route(report_nm))


# ---------------- 이벤트 로드(로컬 캐시) ----------------
def load_events():
    by = {}
    for f in DART_CACHE.glob("*.json"):
        mkt = "KOSDAQ" if f.name.startswith("K_") else "KOSPI"
        for it in json.loads(f.read_text(encoding="utf-8")):
            rno = it.get("rcept_no")
            if not rno:
                continue
            it.setdefault("market", mkt)  # 파일 접두어로 시장 확정(원본 필드 신뢰X)
            it["market"] = mkt
            by.setdefault(rno, it)
    return by


def target_events(by):
    """대상유형 사건만 (endpoint/type 라우팅 성공분)."""
    out = []
    for it in by.values():
        ep, typ = route(it.get("report_nm", ""))
        if not ep:
            continue
        it2 = dict(it)
        it2["endpoint"] = ep
        it2["stype"] = typ
        out.append(it2)
    return out


# ---------------- DART 금액추출 ----------------
def _detail_range(ep, corp, bgn, end):
    """한 corp·endpoint·기간 구조화 조회 -> {rcept_no: row}. HTTP 1콜(재시도)."""
    url = f"https://opendart.fss.or.kr/api/{ep}.json"
    params = {"crtfc_key": KEY, "corp_code": corp,
              "bgn_de": bgn, "end_de": end}
    rows = {}
    for attempt in range(5):
        try:
            d = requests.get(url, params=params, timeout=25).json()
        except Exception:
            time.sleep(2)
            continue
        st = d.get("status")
        if st == "013":
            break
        if st == "020":
            log(f"    rate-limit(020) {ep}/{corp} sleep30")
            time.sleep(30)
            continue
        if st != "000":
            log(f"    status {st} {ep}/{corp}: {d.get('message')}")
            break
        for r in d.get("list", []):
            rno = r.get("rcept_no")
            if rno:
                rows[rno] = r
        break
    return rows


def dart_detail(ep, corp):
    """배치용 풀스팬(2021~2026-05) 구조화 조회 -> {rcept_no: row}. 디스크 캐시."""
    cf = AMT_CACHE / f"{ep}_{corp}.json"
    if cf.exists():
        return json.loads(cf.read_text(encoding="utf-8"))
    rows = _detail_range(ep, corp, "20210101", "20260515")
    cf.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    time.sleep(0.12)
    return rows


def detail_row_for(ep, corp, rcept_no, rcept_dt=None):
    """온디맨드: 특정 rcept_no 상세행 1건. **온탭 DART 최대 1콜** 보장.
    - 배치 풀스팬 캐시가 '디스크에 이미 있으면' 그걸로 조회(DART 0콜).
    - 캐시에 없거나 해당 rcept 미포함이면(신규/배치범위 밖) 접수일 근방만 1콜.
    (온디맨드 경로에서 새 풀스팬 HTTP 는 절대 트리거하지 않는다 — 과부하 방지)"""
    cf_batch = AMT_CACHE / f"{ep}_{corp}.json"
    if cf_batch.exists():
        try:
            rows = json.loads(cf_batch.read_text(encoding="utf-8"))
            if rcept_no in rows:
                return rows[rcept_no]
        except Exception:
            pass
    from datetime import timedelta
    if rcept_dt and len(str(rcept_dt)) == 8 and str(rcept_dt).isdigit():
        d0 = datetime.strptime(str(rcept_dt), "%Y%m%d")
        bgn = (d0 - timedelta(days=7)).strftime("%Y%m%d")
        end = (d0 + timedelta(days=7)).strftime("%Y%m%d")
    else:
        bgn = "20260101"
        end = datetime.now().strftime("%Y%m%d")
    cf = AMT_CACHE / f"live_{ep}_{corp}_{bgn}.json"
    if cf.exists():
        r2 = json.loads(cf.read_text(encoding="utf-8"))
    else:
        r2 = _detail_range(ep, corp, bgn, end)   # DART 1콜
        cf.write_text(json.dumps(r2, ensure_ascii=False), encoding="utf-8")
    return r2.get(rcept_no)


def shares_from_row(ep, row):
    """endpoint별 '발행주식총수' 역산(주). 시총=발행총수×종가 산출용.
    pykrx 시가총액 엔드포인트가 KRX 로그인오류로 불가 → 상세행에서 자체 산출.
      - 유상증자: bfic_tisstk_ostk(증자전 발행총수) 직접.
      - 자사주(취득/처분/신탁): aq_wtn_div_ostk / (aq_wtn_div_ostk_rt/100).
        (aq_wtn_div_ostk_rt = 해당주식의 발행주식총수 대비 비율%)
      - 전환/BW/EB: cvisstk_cnt / (cvisstk_tisstk_vs/100)
        (cvisstk_tisstk_vs = 전환가능주식의 발행총수 대비 비율%).
    실패 시 None."""
    if ep == "piicDecsn":
        return _num(row.get("bfic_tisstk_ostk"))
    if ep in ("tsstkAqDecsn", "tsstkDpDecsn", "tsstkAqTrctrCcDecsn"):
        cnt = _num(row.get("aq_wtn_div_ostk"))
        rt = _num(row.get("aq_wtn_div_ostk_rt"))
        if cnt and rt and rt > 0:
            return cnt / (rt / 100.0)
        return None
    if ep in ("cvbdIsDecsn", "bdwtIsDecsn", "exbdIsDecsn"):
        cnt = _num(row.get("cvisstk_cnt"))
        vs = _num(row.get("cvisstk_tisstk_vs"))
        if cnt and vs and vs > 0:
            return cnt / (vs / 100.0)
        return None
    return None


def amount_from_row(ep, row):
    """endpoint별 이벤트 금액(원) 추출. 실패 시 None."""
    if ep == "piicDecsn":
        parts = [_num(row.get(k)) for k in
                 ("fdpp_fclt", "fdpp_bsninh", "fdpp_op",
                  "fdpp_dtrp", "fdpp_ocsa", "fdpp_etc")]
        vals = [p for p in parts if p]
        return sum(vals) if vals else None
    if ep in ("cvbdIsDecsn", "bdwtIsDecsn", "exbdIsDecsn"):
        return _num(row.get("bd_fta"))
    if ep == "tsstkAqDecsn":
        a = _num(row.get("aqpln_prc_ostk")) or 0
        b = _num(row.get("aqpln_prc_estk")) or 0
        return (a + b) or None
    if ep == "tsstkDpDecsn":
        a = _num(row.get("dppln_prc_ostk")) or 0
        b = _num(row.get("dppln_prc_estk")) or 0
        return (a + b) or None
    if ep == "tsstkAqTrctrCcDecsn":
        return _num(row.get("ctr_prc_atcc")) or _num(row.get("ctr_prc_bfcc"))
    return None


def cmd_amounts():
    by = load_events()
    evs = target_events(by)
    pairs = sorted({(e["corp_code"], e["endpoint"]) for e in evs})
    log(f"=== 금액추출 시작: {len(evs)}사건, {len(pairs)} (corp×endpoint) 콜 ===")
    done = 0
    for corp, ep in pairs:
        dart_detail(ep, corp)
        done += 1
        if done % 200 == 0:
            log(f"  진행 {done}/{len(pairs)}")
    log("=== 금액추출 완료 ===")


# ---------------- pykrx 시총 ----------------
def load_mcap(code):
    """{ 'YYYY-MM-DD': 시가총액(원) }. 캐시. 실패 시 {}."""
    cf = MCAP_CACHE / f"{code}.json"
    if cf.exists():
        return json.loads(cf.read_text())
    try:
        from pykrx import stock
        end = datetime.now().strftime("%Y%m%d")
        df = stock.get_market_cap_by_date("20201101", end, code)
    except Exception as e:
        log(f"    mcap fail {code}: {repr(e)[:70]}")
        cf.write_text("{}")
        return {}
    out = {}
    if df is not None and len(df):
        for d, row in df.iterrows():
            try:
                mc = float(row["시가총액"])
            except (KeyError, ValueError, TypeError):
                continue
            if mc > 0:
                out[d.strftime("%Y-%m-%d")] = mc
    cf.write_text(json.dumps(out))
    time.sleep(0.2)
    return out


def cmd_mcap():
    by = load_events()
    evs = target_events(by)
    codes = sorted({e["stock_code"] for e in evs if e.get("stock_code")})
    log(f"=== 시총수집 시작: {len(codes)}종목 (pykrx) ===")
    ok = 0
    for i, c in enumerate(codes):
        m = load_mcap(c)
        if m:
            ok += 1
        if (i + 1) % 100 == 0:
            log(f"  진행 {i+1}/{len(codes)} (성공 {ok})")
    log(f"=== 시총수집 완료: 성공 {ok}/{len(codes)} ===")


# ---------------- 가격(EW 시장보정) ----------------
def load_px(code):
    cf = PX_CACHE / f"{code}.json"
    if cf.exists():
        try:
            return json.loads(cf.read_text())
        except Exception:
            return {}
    return {}


def build_ew_market(px_all):
    """build_impact_benchmark.build_ew_market 와 동일 로직(자기시장 EW 지수)."""
    sum_oc, cnt_oc, sum_co, cnt_co = {}, {}, {}, {}
    all_dates = set()
    for px in px_all.values():
        ds = sorted(px.keys())
        prev = None
        for d in ds:
            o, c = px[d][0], px[d][1]
            all_dates.add(d)
            if o > 0 and c > 0:
                sum_oc[d] = sum_oc.get(d, 0.0) + c / o
                cnt_oc[d] = cnt_oc.get(d, 0) + 1
            if prev is not None:
                pc = px[prev][1]
                if pc > 0 and o > 0:
                    sum_co[d] = sum_co.get(d, 0.0) + o / pc
                    cnt_co[d] = cnt_co.get(d, 0) + 1
            prev = d
    dates = sorted(all_dates)
    V_open, V_close = {}, {}
    prev_close = None
    for d in dates:
        r_oc = (sum_oc[d] / cnt_oc[d]) if cnt_oc.get(d) else 1.0
        r_co = (sum_co[d] / cnt_co[d]) if cnt_co.get(d) else 1.0
        vo = 1.0 if prev_close is None else prev_close * r_co
        vc = vo * r_oc
        V_open[d] = vo
        V_close[d] = vc
        prev_close = vc
    return V_open, V_close


# ---------------- 집계 ----------------
def _agg(vals):
    n = len(vals)
    if n == 0:
        return {"n": 0}
    raws = [v[0] for v in vals]
    mkts = [v[1] for v in vals]
    cars = [v[2] for v in vals]
    return {
        "n": n,
        "raw_avg": round(statistics.mean(raws) * 100, 2),
        "raw_med": round(statistics.median(raws) * 100, 2),
        "market_avg": round(statistics.mean(mkts) * 100, 2),
        "car_avg": round(statistics.mean(cars) * 100, 2),
        "car_med": round(statistics.median(cars) * 100, 2),
        "raw_up_prob": round(sum(1 for x in raws if x > 0) / n, 3),
        "up_prob": round(sum(1 for c in cars if c > 0) / n, 3),
    }


def _grade(n):
    if n >= 80:
        return "높음"
    if n >= MIN_N:
        return "보통"
    return "참고"


def bucket_of(stype, rel_pct):
    for lo, hi, label in BUCKETS[stype]:
        if lo <= rel_pct < hi:
            return label
    return None


def bucket_of_doc(stype, rel_pct):
    bounds = BUCKETS_DOC.get(stype)
    if not bounds:
        return None
    for lo, hi, label in bounds:
        if lo <= rel_pct < hi:
            return label
    return None


def cmd_aggregate():
    LOG.write_text("", encoding="utf-8")
    by = load_events()
    evs = target_events(by)
    log(f"대상 사건(라우팅): {len(evs)}")

    # 1) 금액 조인(캐시된 DART 상세에서 rcept_no 매칭)
    detail_cache = {}  # (ep,corp) -> rows
    matched = 0
    for e in evs:
        keyc = (e["endpoint"], e["corp_code"])
        rows = detail_cache.get(keyc)
        if rows is None:
            cf = AMT_CACHE / f"{e['endpoint']}_{e['corp_code']}.json"
            rows = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {}
            detail_cache[keyc] = rows
        row = rows.get(e["rcept_no"])
        if row:
            e["amount"] = amount_from_row(e["endpoint"], row)
            e["_shares"] = shares_from_row(e["endpoint"], row)
            if e["amount"]:
                matched += 1
        else:
            e["amount"] = None
            e["_shares"] = None
    log(f"금액 매칭: {matched}/{len(evs)} (rcept_no 조인·금액>0)")

    # 2) 가격 로드 + 자기시장 EW 구축
    codes = sorted({e["stock_code"] for e in evs})
    px_all = {c: load_px(c) for c in codes}
    px_all = {c: p for c, p in px_all.items() if p}
    code_mkt = {}
    for e in evs:
        code_mkt.setdefault(e["stock_code"], e["market"])
    V = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        sub = {c: p for c, p in px_all.items() if code_mkt.get(c) == mkt}
        V[mkt] = build_ew_market(sub)
        log(f"  [{mkt}] EW 유니버스 {len(sub)}종목")

    # 3) 사건별 수익률 + 상대규모
    priced = 0
    scaled = 0
    for e in evs:
        e["ret"] = {}
        e["rel"] = None
        px = px_all.get(e["stock_code"])
        if not px or not e.get("amount"):
            continue
        r = e["rcept_dt"]
        if len(r) != 8:
            continue
        riso = f"{r[0:4]}-{r[4:6]}-{r[6:8]}"
        dates = sorted(px.keys())
        i0 = bisect.bisect_right(dates, riso)
        if i0 >= len(dates):
            continue
        t0 = dates[i0]
        entry = px[t0][0]
        V_open, V_close = V[e["market"]]
        mkt_o = V_open.get(t0)
        if entry <= 0 or not mkt_o:
            continue
        any_h = False
        for label, k in HORIZONS:
            j = i0 + k
            if j >= len(dates):
                continue
            exd = dates[j]
            exit_c = px[exd][1]
            raw = (exit_c - entry) / entry
            mkt_c = V_close.get(exd)
            mret = (mkt_c - mkt_o) / mkt_o if mkt_c else 0.0
            e["ret"][label] = (raw, mret, raw - mret)
            any_h = True
        if not any_h:
            continue
        priced += 1
        # 상대규모 = 금액 / 시총. 시총 = 발행주식총수 × 진입일 종가.
        # (pykrx 시가총액 엔드포인트 KRX 로그인오류 → 상세행 발행총수로 자체산출)
        shares = e.get("_shares")
        entry_close = px[t0][1]
        if shares and shares > 0 and entry_close > 0:
            e["mcap"] = shares * entry_close
            e["rel"] = (e["amount"] / e["mcap"]) * 100.0
            scaled += 1
    log(f"수익률 계산 {priced} / 상대규모 산출 {scaled}")

    # 4) (유형×버킷) 집계
    out = {}
    summary = []
    for stype in BUCKETS:
        sevs = [e for e in evs if e["stype"] == stype and e.get("rel") is not None]
        rels = sorted(e["rel"] for e in sevs)
        out[stype] = {"n_total": len(sevs), "buckets": {}}
        if rels:
            out[stype]["rel_pctl"] = {
                "p25": round(rels[len(rels)//4], 2),
                "p50": round(rels[len(rels)//2], 2),
                "p75": round(rels[3*len(rels)//4], 2),
                "min": round(rels[0], 2), "max": round(rels[-1], 2),
            }
        for lo, hi, label in BUCKETS[stype]:
            bevs = [e for e in sevs if lo <= e["rel"] < hi]
            brow = {"rel_range": [lo, hi if hi < 1e8 else None]}
            for hlabel, _ in HORIZONS:
                vals = [e["ret"][hlabel] for e in bevs if hlabel in e.get("ret", {})]
                a = _agg(vals)
                brow[hlabel] = {
                    "raw_avg": a.get("raw_avg", 0.0),
                    "raw_med": a.get("raw_med", 0.0),
                    "market_avg": a.get("market_avg", 0.0),
                    "car_avg": a.get("car_avg", 0.0),
                    "car_med": a.get("car_med", 0.0),
                    "raw_up_prob": a.get("raw_up_prob", 0.0),
                    "up_prob": a.get("up_prob", 0.0),
                    "n": a.get("n", 0),
                    "conf": _grade(a.get("n", 0)),
                }
            out[stype]["buckets"][label] = brow
            m = brow["m"]
            summary.append((stype, label, m["n"], m["raw_avg"], m["car_avg"],
                            m["car_med"], m["up_prob"], brow["m"]["conf"]))

    meta = {
        "purpose": "이벤트 규모(금액/시총) × 유형 재집계. 앱 리더가 종목 이벤트의 "
                   "상대규모(금액/시총%)로 해당 규모버킷 통계를 선택하도록 제공.",
        "rel_size_def": "상대규모 = 이벤트금액 / 사건시점 시가총액 × 100(%). "
                        "유상증자=조달금액(fdpp_*합), 전환사채/BW/EB=권면총액(bd_fta), "
                        "자사주=취득/처분/신탁 예정금액.",
        "amount_source": "DART 주요사항보고서 주요정보 구조화 API(piicDecsn/cvbdIsDecsn/"
                         "bdwtIsDecsn/exbdIsDecsn/tsstkAqDecsn/tsstkDpDecsn/"
                         "tsstkAqTrctrCcDecsn), rcept_no 단위 조인.",
        "mcap_source": "시총 = 발행주식총수 × 진입일 종가. 발행총수는 구조화 상세행에서 "
                       "역산(유상=bfic_tisstk_ostk, 자사=aq_wtn_div_ostk/rt, "
                       "전환=cvisstk_cnt/cvisstk_tisstk_vs). pykrx 시가총액 엔드포인트는 "
                       "KRX 로그인오류로 사용불가 → 자체산출(동일자, 일관).",
        "car_method": "build_impact_benchmark 와 동일(익일 시가진입, 1/5/21거래일 보유, "
                      "자기시장 EW 보정). 유형 벤치마크와 일관.",
        "bucket_bounds": {k: [b[2] for b in v] for k, v in BUCKETS.items()},
        "min_n_bucket": MIN_N,
        "conf_grades": "n>=80 높음 / 20-79 보통 / <20 참고",
        "caveat_자사주": "자사주 태그는 취득(매입,+)·처분(+공급/희석성 매도,-)·신탁을 "
                       "함께 집계(앱 분류가 단일 '자사주'). 규모 sanity는 취득계열 위주. "
                       "처분 혼입으로 부호가 희석될 수 있음 — 리포트에 분리 표기.",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    result = {"_meta": meta, "scale_buckets": out}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, OUT)
    log(f"저장: {OUT}")

    # sanity 요약
    log("=== (유형×버킷) 1개월(21거래일) 요약 ===")
    log(f"{'유형':<8}{'버킷':<12}{'N':>6}{'raw%':>8}{'CAR%':>8}"
        f"{'CARmed%':>9}{'C>0':>6}  conf")
    for (st, lb, n, raw, car, cmed, up, conf) in summary:
        log(f"{st:<8}{lb:<12}{n:>6}{raw:>8}{car:>8}{cmed:>9}{up:>6}  {conf}")
    return result


# ---------------- 온디맨드 조회(앱 /api/scale) ----------------
_NET_CLOSE = {}   # code -> (ts, close) 인메모리 TTL 캐시(배포 중복콜 억제)
_NET_CLOSE_TTL = 1800  # 30분


def _net_close(code):
    """네트워크 종가. 배포환경(KRX 지오차단·px캐시 없음)에서 시총 산출용.
    네이버 금융(주) → 야후(백업). 둘 다 해외 IP에서 접근 가능. 실패 시 None."""
    hit = _NET_CLOSE.get(code)
    if hit and (time.time() - hit[0]) < _NET_CLOSE_TTL:
        return hit[1]
    hdr = {"User-Agent": "Mozilla/5.0"}
    close = None
    # 1) 네이버: 시장구분 불필요(6자리 코드만). closePrice='263,000'
    try:
        j = requests.get(f"https://m.stock.naver.com/api/stock/{code}/basic",
                         headers=hdr, timeout=6).json()
        cp = str(j.get("closePrice", "")).replace(",", "")
        if cp and float(cp) > 0:
            close = float(cp)
    except Exception:
        pass
    # 2) 야후 백업: .KS(코스피)·.KQ(코스닥) 순차 시도
    if close is None:
        for sfx in (".KS", ".KQ"):
            try:
                j = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{sfx}",
                    params={"interval": "1d", "range": "5d"},
                    headers=hdr, timeout=6).json()
                q = j["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                vals = [c for c in q if c]
                if vals and vals[-1] > 0:
                    close = float(vals[-1])
                    break
            except Exception:
                continue
    if close is not None:
        _NET_CLOSE[code] = (time.time(), close)
    return close


def _latest_close(code):
    """진입 근사 종가. px 캐시(있으면) 최종 종가 → 네트워크(네이버/야후) →
    pykrx OHLCV. 배포(px캐시 없음·KRX 차단)에서도 네트워크 폴백으로 산출."""
    px = load_px(code)
    if px:
        ds = sorted(px.keys())
        if ds:
            return px[ds[-1]][1]
    nc = _net_close(code)
    if nc:
        return nc
    try:
        from pykrx import stock
        from datetime import timedelta
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=15)).strftime("%Y%m%d")
        df = stock.get_market_ohlcv_by_date(start, end, code)
        if df is not None and len(df):
            return float(df["종가"].iloc[-1])
    except Exception:
        pass
    return None


_SB_CACHE = {"key": None, "data": None}


def load_scale_buckets():
    """impact_benchmark.json 의 유형별 scale_buckets 블록 -> {유형: block}. mtime 캐시."""
    if not IMPACT.exists():
        return {}
    key = IMPACT.stat().st_mtime
    if _SB_CACHE["key"] == key and _SB_CACHE["data"] is not None:
        return _SB_CACHE["data"]
    try:
        bench = json.loads(IMPACT.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for t, v in bench.items():
        if isinstance(v, dict) and isinstance(v.get("scale_buckets"), dict):
            out[t] = v["scale_buckets"]
    _SB_CACHE["key"], _SB_CACHE["data"] = key, out
    return out


def _eok(won):
    """원 -> 사람이 읽는 억/조 문자열."""
    if not won:
        return "-"
    eok = won / 1e8
    if eok >= 10000:
        return f"{eok/10000:.2f}조원"
    if eok >= 1:
        return f"{eok:,.0f}억원"
    return f"{won/1e4:,.0f}만원"


def _doc_fields_cached(rcept_no, doctype, allow_fetch=True):
    """document.xml 파싱필드(dict) — 캐시 우선, 없으면 <=1콜 파싱·캐시. 실패 시 None."""
    cf = DOC_CACHE / f"{rcept_no}.json"
    if cf.exists():
        try:
            return json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not allow_fetch:
        return None
    txt = _fetch_doc_text(rcept_no)     # <=1 DART콜
    if txt is None:
        return None
    fields = parse_doc(doctype, txt)
    try:
        cf.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    time.sleep(0.1)
    return fields


def _scale_only(report_nm, rcept_no, corp_code, stock_code, rcept_dt=None):
    """규모버킷(유상/자사/전환) 외의 금액추출 가능 유형(무상증자·공급계약·배당·
    소각)에 대해 **이벤트 자체 규모**(금액/도메인 비율)를 반환. 과거 규모버킷 통계는
    없으므로(문서파싱 유형은 대량 히스토리 수집이 DART 부하상 비현실적) status=
    'scale_only' 로 신호하고 프론트는 유형레벨 통계를 유지한다. 해당없으면 None."""
    ep2, _ = struct_route(report_nm)
    # 무상증자(구조화 fricDecsn): 현금유입 없음 → 규모 = 무상신주 희석률.
    if ep2 == "fricDecsn":
        row = None
        if corp_code:
            try:
                row = detail_row_for("fricDecsn", corp_code, rcept_no, rcept_dt)
            except Exception:
                row = None
        if not row:
            # 구조화 미반영(신규)일 수 있음 → pending(프론트는 유형통계 유지)
            return {"status": "pending", "stype": "무상증자",
                    "reason": "구조화 상세 미반영(신규)"}
        new = _num(row.get("nstk_ostk_cnt"))
        pre = _num(row.get("bfic_tisstk_ostk"))
        if new and pre and pre > 0:
            return {"status": "scale_only", "stype": "무상증자",
                    "rel_label": "무상신주 비율(기존주식 대비)",
                    "rel_pct": round(new / pre * 100, 1),
                    "note": "무상증자는 현금유입 없이 주식수만 증가. 규모별 과거통계는 "
                            "준비중 — 위 유형 통계를 참고하세요."}
        return {"status": "pending", "stype": "무상증자",
                "reason": "무상신주/발행총수 미기재"}
    # 문서파싱 유형(공급계약/배당/소각)
    dtype = doc_route(report_nm)
    if not dtype:
        return None
    fields = _doc_fields_cached(rcept_no, dtype, allow_fetch=True)
    if not fields:
        return {"status": "pending", "stype": dtype, "reason": "문서 파싱 실패/미기재"}
    if dtype == "공급계약":
        amt = fields.get("amount")
        return {"status": "scale_only", "stype": "공급계약",
                "amount": amt, "amount_txt": _eok(amt) if amt else "-",
                "rel_label": "최근매출 대비", "rel_pct": fields.get("rev_pct"),
                "note": "공급계약 규모는 계약금액·매출대비로 표시. 규모별 과거통계는 "
                        "준비중 — 위 유형 통계를 참고하세요."}
    if dtype == "배당":
        tot = fields.get("total")
        return {"status": "scale_only", "stype": "배당",
                "amount": tot, "amount_txt": _eok(tot) if tot else "-",
                "rel_label": "시가배당률", "rel_pct": fields.get("yield"),
                "note": "배당 규모는 배당총액·시가배당률로 표시. 규모별 과거통계는 "
                        "준비중 — 위 유형 통계를 참고하세요."}
    if dtype == "소각":
        amt = fields.get("amount")
        return {"status": "scale_only", "stype": "소각",
                "amount": amt, "amount_txt": _eok(amt) if amt else "-",
                "rel_label": "발행주식 대비 소각비율", "rel_pct": fields.get("pct"),
                "note": "소각 규모는 소각금액·발행주식대비로 표시. 규모별 과거통계는 "
                        "준비중 — 위 유형 통계를 참고하세요."}
    return None


def _upgrade_scale_only(so):
    """_scale_only 결과(status:scale_only, rel_pct 보유)를 규모버킷 통계가 있으면
    status:ok(+windows+rel_label)로 승격. 버킷 미집계/저표본(n<MIN_N)이면 원본 유지."""
    if not so or so.get("status") != "scale_only":
        return so
    stype = so.get("stype")
    rel = so.get("rel_pct")
    if stype not in BUCKETS_DOC or rel is None:
        return so     # 배당 등 버킷 미대상 → scale_only 유지
    bkey = STYPE_BENCH_KEY.get(stype, stype)
    block = load_scale_buckets().get(bkey)
    if not block or not block.get("buckets"):
        return so     # 아직 미집계 → scale_only 폴백
    label = bucket_of_doc(stype, rel)
    brow = (block.get("buckets") or {}).get(label) if label else None
    if not brow:
        return so
    m = brow.get("m") or {}
    if (m.get("n") or 0) < MIN_N:
        return so     # 저표본(참고) → 유형레벨 폴백(scale_only)
    windows = {}
    for k, _ in HORIZONS:
        w = brow.get(k) or {}
        windows[k] = {
            "raw_avg": w.get("raw_avg"),
            "up_prob": w.get("raw_up_prob", w.get("up_prob")),
            "n": w.get("n"), "conf": w.get("conf"),
        }
    return {
        "status": "ok",
        "stype": stype,
        "amount": so.get("amount"),
        "amount_txt": so.get("amount_txt", "-"),
        "rel_pct": rel,
        "rel_label": REL_LABELS.get(stype, so.get("rel_label")),
        "bucket": label,
        "bucket_size": label[:1],
        "n": m.get("n"),
        "conf": m.get("conf"),
        "windows": windows,
    }


def scale_lookup(rcept_no, corp_code, stock_code, report_nm, rcept_dt=None):
    """온디맨드 규모 조회: 공시 1건 -> (금액 -> 시총대비 상대규모 -> 규모버킷 통계).
    DART 최대 1콜(과거 사건은 배치 캐시로 0콜). 항상 dict(status 로 폴백 신호).

    반환(ok): {status, stype, amount, amount_txt, mcap, rel_pct, bucket, bucket_size,
               n, conf, windows{d,w,m:{car_avg,car_med,up_prob,raw_avg,n,conf}}}
    반환(폴백): {status: unsupported|pending|no_detail|no_amount|no_mcap, reason, stype?}
    """
    ep, stype = route(report_nm)
    if not ep:
        # 규모버킷(유상/자사/전환) 외의 금액추출 가능 유형(무상증자·공급계약·배당·
        # 소각)은 이벤트 자체 규모(금액/비율)를 scale_only 로 반환 → bullet_eligible
        # 과 scale 대상 목록 일치. (과거 규모버킷 통계는 준비중 — 유형레벨 통계 참고.)
        so = _scale_only(report_nm, rcept_no, corp_code, stock_code, rcept_dt)
        if so is not None:
            # 규모버킷 집계된 신규유형(공급계약/소각/무상증자)이면 status:ok 승격.
            return _upgrade_scale_only(so)
        return {"status": "unsupported", "reason": "규모보정 미지원 유형"}
    buckets_block = load_scale_buckets().get(stype)
    if not buckets_block or not buckets_block.get("buckets"):
        return {"status": "pending", "stype": stype, "reason": "규모버킷 미집계"}
    if not corp_code:
        return {"status": "no_detail", "stype": stype, "reason": "corp_code 미해결"}
    row = detail_row_for(ep, corp_code, rcept_no, rcept_dt)
    if not row:
        return {"status": "no_detail", "stype": stype, "reason": "DART 구조화 상세 없음"}
    amount = amount_from_row(ep, row)
    shares = shares_from_row(ep, row)
    if not amount:
        return {"status": "no_amount", "stype": stype, "reason": "공시에 금액 미기재"}
    close = _latest_close(stock_code) if stock_code else None
    if not (shares and shares > 0 and close and close > 0):
        return {"status": "no_mcap", "stype": stype, "amount": amount,
                "amount_txt": _eok(amount), "reason": "시총(발행총수×종가) 산출 실패"}
    mcap = shares * close
    rel = amount / mcap * 100.0
    label = bucket_of(stype, rel)
    brow = (buckets_block.get("buckets") or {}).get(label) if label else None
    if not brow:
        return {"status": "no_bucket", "stype": stype, "rel_pct": round(rel, 1),
                "reason": "해당 규모버킷 없음"}
    windows = {}
    for k, _ in HORIZONS:
        w = brow.get(k) or {}
        windows[k] = {
            "raw_avg": w.get("raw_avg"),
            "up_prob": w.get("raw_up_prob", w.get("up_prob")),
            "n": w.get("n"), "conf": w.get("conf"),
        }
    m = windows.get("m", {})
    return {
        "status": "ok",
        "stype": stype,
        "amount": amount,
        "amount_txt": _eok(amount),
        "mcap": mcap,
        "mcap_txt": _eok(mcap),
        "rel_pct": round(rel, 1),
        "bucket": label,
        "bucket_size": label[:1],           # 소/중/대
        "n": m.get("n"),
        "conf": m.get("conf"),
        "windows": windows,
    }


# ---------------- 숫자 bullet(앱 피드 .facts) ----------------
def _parse_kdate(s):
    """'2026년 01월 30일' / '2026-01-30' / '20260130' -> date | None."""
    if not s:
        return None
    from datetime import date
    s = str(s).strip()
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def _months(bgd, edd):
    a, b = _parse_kdate(bgd), _parse_kdate(edd)
    if not a or not b:
        return None
    days = (b - a).days
    if days <= 0:
        return None
    return max(1, round(days / 30.44))


def bullets_from_row(ep, row):
    """endpoint별 숫자 bullet 리스트. 볼드 대상 숫자는 **...** 로 감싼다
    (프론트가 <b> 로 변환). 값 없으면 해당 bullet 생략."""
    B = []
    b = lambda x: "**" + x + "**"     # noqa: E731
    if ep == "piicDecsn":
        amt = amount_from_row(ep, row)
        new = _num(row.get("nstk_ostk_cnt"))
        pre = _num(row.get("bfic_tisstk_ostk"))
        parts = []
        if amt:
            parts.append("조달 " + b(_eok(amt)))
        if new:
            s = "신주 " + b(f"{new:,.0f}주")
            if pre and pre > 0:
                s += " (기존 대비 " + b(f"+{new/pre*100:.1f}%") + ")"
            parts.append(s)
        if parts:
            B.append(" · ".join(parts))
    elif ep in ("tsstkAqDecsn", "tsstkDpDecsn"):
        is_aq = ep == "tsstkAqDecsn"
        amt = amount_from_row(ep, row)
        stk = _num(row.get("aqpln_stk_ostk" if is_aq else "dppln_stk_ostk"))
        total = shares_from_row(ep, row)
        seg = []
        if amt:
            seg.append(("취득 규모 " if is_aq else "처분 규모 ") + b(_eok(amt)))
        if stk and total and total > 0:
            seg.append("발행주식의 " + b(f"{stk/total*100:.2f}%"))
        if seg:
            B.append(" — ".join(seg))
        mo = _months(row.get("aqexpd_bgd" if is_aq else "dpprpd_bgd"),
                     row.get("aqexpd_edd" if is_aq else "dpprpd_edd"))
        if mo:
            B.append(("취득기간 " if is_aq else "처분기간 ") + b(f"{mo}개월"))
    elif ep == "tsstkAqTrctrCcDecsn":
        amt = amount_from_row(ep, row)
        if amt:
            B.append("신탁계약 " + b(_eok(amt)))
        mo = _months(row.get("ctr_pd_bfcc_bgd"), row.get("ctr_pd_bfcc_edd"))
        if mo:
            B.append("계약기간 " + b(f"{mo}개월"))
    elif ep in ("cvbdIsDecsn", "bdwtIsDecsn", "exbdIsDecsn"):
        amt = amount_from_row(ep, row)
        if amt:
            s = "발행 " + b(_eok(amt))
            dil = _num(row.get("cvisstk_tisstk_vs"))
            if dil is not None:
                s += " (희석 " + b(f"{dil:.1f}%") + ")"
            B.append(s)
    elif ep == "fricDecsn":
        new = _num(row.get("nstk_ostk_cnt"))          # 무상 신주(보통주)
        pre = _num(row.get("bfic_tisstk_ostk"))       # 증자전 발행총수(보통주)
        per = _num(row.get("nstk_ascnt_ps_ostk"))     # 1주당 신주배정수
        parts = []
        if per:
            parts.append("배정 " + b(f"1주당 {per:g}주"))
        if new:
            s = "무상신주 " + b(f"{new:,.0f}주")
            if pre and pre > 0:
                s += " (기존 대비 " + b(f"+{new/pre*100:.1f}%") + ")"
            parts.append(s)
        if parts:
            B.append(" · ".join(parts))
    return B


# ---------------- 문서파싱 bullet(공급계약·배당·소각) ----------------
# OpenDART 이벤트단위 구조화 EP 없음 → document.xml(KRX 표준양식) 정규식 추출.
_NUM = r"([\d,]+(?:\.\d+)?)"


def _fetch_doc_text(rcept_no):
    """document.xml 1콜 → 태그제거·공백정규화 텍스트. cp949/utf-8 자동판별.
    zip 아님(레이트리밋/오류)이면 None(캐시 안함→다음 poll 재시도)."""
    url = "https://opendart.fss.or.kr/api/document.xml"
    for _ in range(3):
        try:
            r = requests.get(url, params={"crtfc_key": KEY, "rcept_no": rcept_no},
                             timeout=25)
        except Exception:
            time.sleep(1)
            continue
        if r.status_code != 200 or not r.content or not r.content.startswith(b"PK"):
            return None
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            raw = z.read(z.namelist()[0])
        except Exception:
            return None
        try:
            t = raw.decode("utf-8")
        except UnicodeDecodeError:
            t = raw.decode("cp949", "replace")
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))
    return None


def _last_num(label_pat, txt, hi=None):
    """label_pat 뒤 숫자 중 **마지막(=정정후/최종양식)** 유효값. hi 초과는 배제.
    정정공시(정정전/정정후 혼합표)에서 앞쪽 정정전 셀 오매칭을 피하기 위함."""
    vals = []
    for m in re.finditer(label_pat + r"\s*" + _NUM, txt):
        v = _num(m.group(1))
        if v is not None and (hi is None or v <= hi):
            vals.append(v)
    return vals[-1] if vals else None


def parse_doc(doctype, txt):
    """유형별 표준필드 추출 → dict. 실패 필드는 생략(빈 dict 가능).
    라벨은 구양식('계약금액(원)')·신양식('계약금액 총액(원)') 띄어쓰기 모두 허용,
    값은 마지막 매치(정정후) + sanity 상한으로 정정공시 오매칭 방지."""
    f = {}
    if doctype == "공급계약":
        # 신양식 '계약금액 총액(원)' 우선, 없으면 구양식 '계약금액(원)'.
        f["amount"] = (_last_num(r"계약금액\s*총액\s*\(원\)", txt)
                       or _last_num(r"계약금액\s*\(원\)", txt))
        f["revenue"] = _last_num(r"최근\s*매출액\s*\(원\)", txt)
        f["rev_pct"] = _last_num(r"매출액\s*대비\s*\(%\)", txt, hi=100000)
    elif doctype == "배당":
        f["dps"] = _last_num(r"1주당\s*배당금\s*\(원\)\s*보통주식", txt)
        f["yield"] = _last_num(r"시가배당[율률]\s*\(%\)\s*보통주식", txt, hi=1000)
        f["total"] = _last_num(r"배당금\s*총액\s*\(원\)", txt)
    elif doctype == "소각":
        f["amount"] = _last_num(r"소각예정금액\s*\(원\)", txt)
        # 소각수/발행총수(보통주 우선, 없으면 종류주). 공란 '-' 허용, 마지막 매치.
        nd = r"([\d,]+(?:\.\d+)?|-)"
        cpat = r"소각할 주식의 종류와 수\s*보통주식\s*\(주\)\s*" + nd \
            + r"\s*종류주식\s*\(주\)\s*" + nd
        tpat = r"발행주식\s*총수\s*보통주식\s*\(주\)\s*" + nd \
            + r"\s*종류주식\s*\(주\)\s*" + nd
        mcs = list(re.finditer(cpat, txt))
        mts = list(re.finditer(tpat, txt))
        mc = mcs[-1] if mcs else None
        mt = mts[-1] if mts else None
        canc_o = _num(mc.group(1)) if mc else None
        canc_e = _num(mc.group(2)) if mc else None
        tot_o = _num(mt.group(1)) if mt else None
        tot_e = _num(mt.group(2)) if mt else None
        if canc_o and tot_o and tot_o > 0:
            f["pct"] = round(canc_o / tot_o * 100, 2)
        elif canc_e and tot_e and tot_e > 0:
            f["pct"] = round(canc_e / tot_e * 100, 2)
    # ----- 구조화 주요정보 API가 아직 미반영(013)인 신규 공시 대비: 표준양식 파싱 -----
    elif doctype == "유상증자":
        # 조달금액 = 자금목적 6개 항목 합(있는 것만). 라벨은 '…자금 (원) N'.
        amt = 0.0
        for lab in (r"시설자금", r"영업양수자금", r"운영자금", r"채무상환자금",
                    r"타법인\s*증권\s*취득\s*자금", r"기타자금"):
            v = _last_num(lab + r"\s*\(원\)", txt)
            if v:
                amt += v
        f["amount"] = amt or None
        f["new"] = _last_num(r"신주의\s*종류와\s*수\s*보통주식\s*\(주\)", txt)
        # 증자전 발행총수: 라벨 인접(엄격) 매칭으로 오셀 방지.
        f["pre"] = _last_num(r"증자전\s*발행주식총수\s*\(주\)\s*보통주식\s*\(주\)", txt)
    elif doctype == "전환사채":
        # 권면(전자등록)총액(원). BW/EB 도 동일 라벨.
        f["amount"] = _last_num(r"권면\(전자등록\)?총액\s*\(원\)", txt)
        # 발행주식총수 대비 비율(%) = 잠재 희석률(마지막 유효값, sanity 상한).
        f["dil"] = _last_num(r"발행주식총수\s*대비\s*비율\s*\(%\)", txt, hi=100000)
    elif doctype == "자사주신탁":
        # 자기주식취득/처분 신탁계약 체결·해지: 계약금액(원) + 계약기간.
        f["amount"] = _last_num(r"계약금액\s*\(원\)", txt)
        m = re.search(r"계약기간\s*시작일\s*(.{4,20}?)\s*종료일\s*(.{4,20}?)\s*\d?\.", txt)
        if m:
            mo = _months(m.group(1), m.group(2))
            if mo:
                f["months"] = mo
    return f


def bullets_from_doc(doctype, f):
    """파싱필드 → 볼드 bullet 리스트."""
    B = []
    b = lambda x: "**" + x + "**"     # noqa: E731
    if doctype == "공급계약":
        parts = []
        if f.get("amount"):
            parts.append("계약금액 " + b(_eok(f["amount"])))
        if f.get("rev_pct"):
            parts.append("최근매출 대비 " + b(f"{f['rev_pct']:g}%"))
        if parts:
            B.append(" · ".join(parts))
    elif doctype == "배당":
        parts = []
        if f.get("dps"):
            parts.append("주당배당 " + b(f"{f['dps']:,.0f}원"))
        if f.get("yield"):
            parts.append("시가배당률 " + b(f"{f['yield']:g}%"))
        if parts:
            B.append(" · ".join(parts))
        if f.get("total"):
            B.append("배당총액 " + b(_eok(f["total"])))
    elif doctype == "소각":
        parts = []
        if f.get("amount"):
            parts.append("소각 규모 " + b(_eok(f["amount"])))
        if f.get("pct") is not None:
            parts.append("발행주식의 " + b(f"{f['pct']:g}%"))
        if parts:
            B.append(" — ".join(parts))
    elif doctype == "유상증자":
        new = f.get("new")
        pre = f.get("pre")
        parts = []
        if f.get("amount"):
            parts.append("조달 " + b(_eok(f["amount"])))
        if new:
            s = "신주 " + b(f"{new:,.0f}주")
            if pre and pre > 0:
                s += " (기존 대비 " + b(f"+{new/pre*100:.1f}%") + ")"
            parts.append(s)
        if parts:
            B.append(" · ".join(parts))
    elif doctype == "전환사채":
        if f.get("amount"):
            s = "발행 " + b(_eok(f["amount"]))
            if f.get("dil") is not None:
                s += " (희석 " + b(f"{f['dil']:g}%") + ")"
            B.append(s)
    elif doctype == "자사주신탁":
        if f.get("amount"):
            B.append("신탁계약 " + b(_eok(f["amount"])))
        if f.get("months"):
            B.append("계약기간 " + b(f"{f['months']}개월"))
    return B


def doc_bullets_cached(rcept_no, doctype, allow_fetch=False, budget=None):
    """문서파싱 bullet. **기본 캐시전용(DART 0콜).** allow_fetch+budget 시 1콜 파싱·캐시.
    파싱결과 dict 를 rcept_no.json 으로 캐시(빈 dict 도 캐시 → 재시도 방지)."""
    cf = DOC_CACHE / f"{rcept_no}.json"
    fields = None
    if cf.exists():
        try:
            fields = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            fields = None
    if fields is None and allow_fetch and budget and budget[0] > 0:
        budget[0] -= 1
        txt = _fetch_doc_text(rcept_no)
        if txt is not None:                       # 성공(파싱실패라도 {} 캐시)
            fields = parse_doc(doctype, txt)
            try:
                cf.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            time.sleep(0.1)
    if not fields:
        return []
    try:
        return bullets_from_doc(doctype, fields)
    except Exception:
        return []


def _cached_detail_row(ep, corp, rcept_no, known_files=None):
    """디스크 캐시에서만 상세행 조회(DART 0콜). 배치 풀스팬 캐시 + 라이브 캐시.
    known_files: AMT_CACHE 파일명 set(있으면 디렉토리 재스캔 없이 멤버십 검사)."""
    fname = f"{ep}_{corp}.json"
    if known_files is None or fname in known_files:
        cf = AMT_CACHE / fname
        if cf.exists():
            try:
                rows = json.loads(cf.read_text(encoding="utf-8"))
                if rcept_no in rows:
                    return rows[rcept_no]
            except Exception:
                pass
    prefix = f"live_{ep}_{corp}_"
    if known_files is not None:
        live = [n for n in known_files if n.startswith(prefix)]
    else:
        live = [p.name for p in AMT_CACHE.glob(prefix + "*")]
    for n in live:
        try:
            rows = json.loads((AMT_CACHE / n).read_text(encoding="utf-8"))
            if rcept_no in rows:
                return rows[rcept_no]
        except Exception:
            pass
    return None


# 구조화 주요정보 API가 013(미반영/신규건 지연)일 때 document.xml 표준양식으로
# 폴백 파싱할 endpoint→doctype 매핑. 자사주 취득/처분(tsstkAqDecsn/tsstkDpDecsn)은
# 구조화 API가 안정적이라 폴백 불필요(제외).
_STRUCT_DOC_FALLBACK = {
    "piicDecsn": "유상증자",
    "cvbdIsDecsn": "전환사채",
    "bdwtIsDecsn": "전환사채",
    "exbdIsDecsn": "전환사채",
    "tsstkAqTrctrCcDecsn": "자사주신탁",
}


def bullets_for_item(corp_code, stock_code, report_nm, rcept_no, rcept_dt="",
                     allow_fetch=False, budget=None, known_files=None):
    """공시 1건 -> 숫자 bullet 리스트. **기본은 캐시전용(DART 0콜).**
    구조화 유형(유상/자사/전환/무상)은 구조화 JSON, 문서파싱 유형(공급계약/배당/
    소각)은 document.xml 캐시. allow_fetch=True(=poll)이고 budget 남으면 미캐시
    신규건만 1콜로 추출·캐시(상한 초과분은 bullet 생략).

    커버리지 보완: 구조화 주요정보 API가 013(신규건 T+1 지연/미반영)이라 row 가
    없으면, 같은 rcept 의 document.xml(KRX 표준양식)로 폴백 파싱한다(신탁계약·신규
    유상/전환 등이 접수 당일에도 bullet 을 갖게 됨). 폴백 결과는 DOC_CACHE 에
    캐시되어 다음 poll 부터 0콜."""
    ep, _ = struct_route(report_nm)
    if ep:
        row = _cached_detail_row(ep, corp_code, rcept_no, known_files)
        if row is None and allow_fetch and corp_code and budget and budget[0] > 0:
            budget[0] -= 1
            try:
                row = detail_row_for(ep, corp_code, rcept_no, rcept_dt)  # <=1 DART콜
            except Exception:
                row = None
        if row:
            try:
                b = bullets_from_row(ep, row)
                if b:
                    return b
            except Exception:
                pass
        # 구조화 row 없음/빈결과 → document.xml 표준양식 폴백(캐시 또는 <=1콜).
        fdoc = _STRUCT_DOC_FALLBACK.get(ep)
        if fdoc:
            try:
                return doc_bullets_cached(rcept_no, fdoc, allow_fetch, budget)
            except Exception:
                return []
        return []
    doctype = doc_route(report_nm)
    if doctype:
        return doc_bullets_cached(rcept_no, doctype, allow_fetch, budget)
    return []


# ---------------- 신규유형(문서/구조화) 규모버킷 집계 ----------------
def _new_type_events(by):
    """공급계약/소각(문서파싱) + 무상증자(구조화 fricDecsn) 이벤트. stype 부여."""
    out = defaultdict(list)
    for it in by.values():
        nm = it.get("report_nm", "")
        dt = doc_route(nm)
        if dt in ("공급계약", "소각", "배당"):
            e = dict(it)
            e["stype"] = dt
            out[dt].append(e)
            continue
        ep, _ = struct_route(nm)
        if ep == "fricDecsn":
            e = dict(it)
            e["stype"] = "무상증자"
            out["무상증자"].append(e)
    return out


def _doc_rel_cached(stype, e):
    """캐시에서 이벤트 rel(%) 추출(DART 0콜). 없으면 None.
      공급계약=rev_pct, 소각=pct(캐시 doc), 무상증자=nstk/pre(캐시 fricDecsn)."""
    if stype == "무상증자":
        cf = AMT_CACHE / f"fricDecsn_{e.get('corp_code')}.json"
        if not cf.exists():
            return None
        try:
            rows = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            return None
        row = rows.get(e.get("rcept_no"))
        if not row:
            return None
        new = _num(row.get("nstk_ostk_cnt"))
        pre = _num(row.get("bfic_tisstk_ostk"))
        if new and pre and pre > 0:
            return new / pre * 100.0
        return None
    cf = DOC_CACHE / f"{e.get('rcept_no')}.json"
    if not cf.exists():
        return None
    try:
        f = json.loads(cf.read_text(encoding="utf-8"))
    except Exception:
        return None
    if stype == "공급계약":
        return f.get("rev_pct")
    if stype == "소각":
        return f.get("pct")
    if stype == "배당":
        return f.get("yield")   # _scale_only(L808) rel_pct 와 동일 필드(버킷배치==조회 일치)
    return None


# 공급계약 표본: 연도층화 + 최근3년(2024~2026) 가중(현시장 반응 관련성).
# 2021~2023 최소표본 유지(구간편향 방지). 근거 로그.
GONGIB_YEAR_TARGET = {
    "2021": 1100, "2022": 1100, "2023": 1100,   # 구(각 ~29% 샘플, 편향방지 최소)
    "2024": 2200, "2025": 2500, "2026": 1500,   # 최근(가중; 2026 전량)
}
PHASE1_DART_CAP = 16000   # Phase1 배치 총 DART 콜 상한(라이브 폴링 여유 확보)


def cmd_fetch_phase1():
    """Phase1 배치: 무상증자(구조화 full) + 소각(문서 full) + 공급계약(연도층화·
    최근가중 표본). 캐시 우선(재실행시 스킵). DART 콜 상한 PHASE1_DART_CAP 엄수.
    배당은 Phase2(별도 일자)로 분리."""
    budget = [PHASE1_DART_CAP]
    by = load_events()
    groups = _new_type_events(by)
    used = {"무상증자": 0, "소각": 0, "공급계약": 0}
    log(f"=== Phase1 fetch 시작 (DART 상한 {PHASE1_DART_CAP}) ===")

    # 1) 무상증자: corp 단위 구조화 fricDecsn full (1콜/corp)
    corps = sorted({e["corp_code"] for e in groups.get("무상증자", []) if e.get("corp_code")})
    log(f"[무상] corp {len(corps)} 구조화 fetch")
    for c in corps:
        cf = AMT_CACHE / f"fricDecsn_{c}.json"
        if cf.exists():
            continue
        if budget[0] <= 0:
            log("  예산 소진 — 무상 중단"); break
        dart_detail("fricDecsn", c)      # 1콜 + 캐시
        budget[0] -= 1
        used["무상증자"] += 1
        if used["무상증자"] % 50 == 0:
            log(f"  무상 진행 {used['무상증자']} (예산 {budget[0]})")
    log(f"[무상] DART콜 {used['무상증자']}, 예산잔여 {budget[0]}")

    # 2) 소각: 문서파싱 full (1콜/이벤트)
    sog = groups.get("소각", [])
    log(f"[소각] 이벤트 {len(sog)} 문서 fetch")
    for e in sog:
        rno = e.get("rcept_no")
        cf = DOC_CACHE / f"{rno}.json"
        if cf.exists():
            continue
        if budget[0] <= 0:
            log("  예산 소진 — 소각 중단"); break
        _doc_fields_cached(rno, "소각", allow_fetch=True)   # <=1콜 + 캐시
        budget[0] -= 1
        used["소각"] += 1
        if used["소각"] % 100 == 0:
            log(f"  소각 진행 {used['소각']} (예산 {budget[0]})")
    log(f"[소각] DART콜 {used['소각']}, 예산잔여 {budget[0]}")

    # 3) 공급계약: 연도층화 + 최근가중 표본
    gong = groups.get("공급계약", [])
    byy = defaultdict(list)
    for e in gong:
        y = str(e.get("rcept_dt", ""))[:4]
        byy[y].append(e)
    sel = []
    for y in sorted(byy):
        tgt = GONGIB_YEAR_TARGET.get(y, 0)
        # 접수일순(안정) 앞에서부터 tgt건 — px 전량커버 확인됨
        sel.extend(sorted(byy[y], key=lambda e: e.get("rcept_dt", ""))[:tgt])
    log(f"[공급] 표본 {len(sel)} (연도별 목표 {GONGIB_YEAR_TARGET}); "
        f"연도분포 { {y: min(len(byy[y]), GONGIB_YEAR_TARGET.get(y,0)) for y in sorted(byy)} }")
    for e in sel:
        rno = e.get("rcept_no")
        cf = DOC_CACHE / f"{rno}.json"
        if cf.exists():
            continue
        if budget[0] <= 0:
            log("  예산 소진 — 공급 중단"); break
        _doc_fields_cached(rno, "공급계약", allow_fetch=True)   # <=1콜 + 캐시
        budget[0] -= 1
        used["공급계약"] += 1
        if used["공급계약"] % 200 == 0:
            log(f"  공급 진행 {used['공급계약']} (예산 {budget[0]})")
    log(f"[공급] DART콜 {used['공급계약']}, 예산잔여 {budget[0]}")

    total = sum(used.values())
    log(f"=== Phase1 fetch 완료: 총 DART콜 {total} "
        f"(무상 {used['무상증자']} + 소각 {used['소각']} + 공급 {used['공급계약']}), "
        f"예산잔여 {budget[0]} ===")


PHASE2_DART_CAP = 12000   # Phase2(배당) 배치 DART 콜 상한(9957+마진, 라이브폴링 여유)


def cmd_fetch_phase2():
    """Phase2 배치: 배당(현물배당결정 등 배당 유형) document.xml 문서 full 페치.
    캐시 우선(재실행시 스킵=재개형). 1콜/이벤트. 예산상한 PHASE2_DART_CAP 엄수.
    _fetch_doc_text 는 020 백오프가 없어(document.xml은 rate-limit시 PK(zip) 아닌
    응답→None 반환·캐시안함) _doc_fields_cached None = fetch레벨 실패로 간주하고
    배치 자체에서 백오프한다(consec_fail>=3 sleep30, >=10 일한도소진 추정 중단)."""
    budget = [PHASE2_DART_CAP]
    by = load_events()
    groups = _new_type_events(by)
    divs = groups.get("배당", [])
    ok_n = 0
    fail_n = 0
    tried = 0
    consec_fail = 0
    backoff_seen = 0   # 020(rate-limit) 백오프 발동 횟수 관측
    log(f"=== Phase2 fetch 시작: 배당 이벤트 {len(divs)} (DART 상한 {PHASE2_DART_CAP}) ===")
    for e in divs:
        rno = e.get("rcept_no")
        if not rno:
            continue
        cf = DOC_CACHE / f"{rno}.json"
        if cf.exists():
            continue   # 재개형: 이미 캐시된 건 스킵(0콜)
        if budget[0] <= 0:
            log(f"  예산 소진(PHASE2_DART_CAP {PHASE2_DART_CAP}) — 배당 중단"); break
        fields = _doc_fields_cached(rno, "배당", allow_fetch=True)   # <=1 DART콜(성공시 캐시)
        budget[0] -= 1
        tried += 1
        if fields is None:
            # fetch레벨 실패(rate-limit/네트워크). 파싱된 빈 doc은 {}로 캐시되므로
            # None만이 fetch실패 신호다.
            fail_n += 1
            consec_fail += 1
            if consec_fail >= 10:
                backoff_seen += 1
                log(f"  연속실패 {consec_fail} — 일한도 소진 추정, 배치 중단"
                    f"(익일 DOC_CACHE 재개). 예산잔여 {budget[0]}")
                break
            if consec_fail >= 3:
                backoff_seen += 1
                log(f"  연속실패 {consec_fail}(rate-limit 추정) — sleep30 백오프"
                    f" (성공 {ok_n} / 실패 {fail_n}, 예산잔여 {budget[0]})")
                time.sleep(30)
        else:
            ok_n += 1
            consec_fail = 0   # 성공시 리셋
        if tried % 100 == 0:
            log(f"  배당 진행 시도 {tried} (성공 {ok_n} / 실패 {fail_n}, 예산잔여 {budget[0]})")
    log(f"=== Phase2 fetch 완료: 시도 {tried} (성공 {ok_n} / 실패 {fail_n}), "
        f"020백오프 관측 {backoff_seen}회, 예산잔여 {budget[0]} ===")


def cmd_aggregate_doc():
    """신규유형(공급계약/소각/무상증자) (유형×버킷) 집계 → scale_buckets.json 에
    **추가**(기존 3유형 블록 비파괴). rel=도메인비율, CAR=기존과 동일(익일 시가진입
    1/5/21거래일, 자기시장 EW 보정). DART 0콜(캐시만)."""
    by = load_events()
    groups = _new_type_events(by)
    allevs = []
    rel_missing = defaultdict(int)
    for stype in BUCKETS_DOC:
        for e in groups.get(stype, []):
            rel = _doc_rel_cached(stype, e)
            if rel is None:
                rel_missing[stype] += 1
                continue
            e["rel"] = rel
            allevs.append(e)
    for stype in BUCKETS_DOC:
        got = sum(1 for e in allevs if e["stype"] == stype)
        log(f"[{stype}] rel 확보 {got} / 캐시미스 {rel_missing[stype]}")

    # px + 자기시장 EW
    codes = sorted({e["stock_code"] for e in allevs if e.get("stock_code")})
    px_all = {c: load_px(c) for c in codes}
    px_all = {c: p for c, p in px_all.items() if p}
    code_mkt = {}
    for e in allevs:
        code_mkt.setdefault(e["stock_code"], e["market"])
    V = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        sub = {c: p for c, p in px_all.items() if code_mkt.get(c) == mkt}
        V[mkt] = build_ew_market(sub)
        log(f"  [{mkt}] EW 유니버스 {len(sub)}종목")
    px_miss = sorted({e["stock_code"] for e in allevs if e["stock_code"] not in px_all})
    if px_miss:
        log(f"  px 미커버 종목 {len(px_miss)} (해당 이벤트 CAR 제외)")

    # 사건별 CAR
    priced = 0
    for e in allevs:
        e["ret"] = {}
        px = px_all.get(e["stock_code"])
        if not px:
            continue
        r = e.get("rcept_dt", "")
        if len(r) != 8:
            continue
        riso = f"{r[0:4]}-{r[4:6]}-{r[6:8]}"
        dates = sorted(px.keys())
        i0 = bisect.bisect_right(dates, riso)
        if i0 >= len(dates):
            continue
        t0 = dates[i0]
        entry = px[t0][0]
        V_open, V_close = V[e["market"]]
        mkt_o = V_open.get(t0)
        if entry <= 0 or not mkt_o:
            continue
        any_h = False
        for label, k in HORIZONS:
            j = i0 + k
            if j >= len(dates):
                continue
            exd = dates[j]
            exit_c = px[exd][1]
            raw = (exit_c - entry) / entry
            mkt_c = V_close.get(exd)
            mret = (mkt_c - mkt_o) / mkt_o if mkt_c else 0.0
            e["ret"][label] = (raw, mret, raw - mret)
            any_h = True
        if any_h:
            priced += 1
    log(f"CAR 산출 이벤트 {priced}/{len(allevs)}")

    # (유형×버킷) 집계
    out = {}
    summary = []
    for stype, bounds in BUCKETS_DOC.items():
        sevs = [e for e in allevs if e["stype"] == stype and e.get("ret")]
        rels = sorted(e["rel"] for e in sevs)
        block = {"n_total": len(sevs), "rel_label": REL_LABELS.get(stype),
                 "buckets": {}}
        if rels:
            n = len(rels)
            block["rel_pctl"] = {
                "p25": round(rels[n // 4], 2), "p50": round(rels[n // 2], 2),
                "p75": round(rels[3 * n // 4], 2),
                "min": round(rels[0], 2), "max": round(rels[-1], 2),
            }
        for lo, hi, label in bounds:
            bevs = [e for e in sevs if lo <= e["rel"] < hi]
            brow = {"rel_range": [lo, hi if hi < 1e8 else None]}
            for hlabel, _ in HORIZONS:
                vals = [e["ret"][hlabel] for e in bevs if hlabel in e.get("ret", {})]
                a = _agg(vals)
                brow[hlabel] = {
                    "raw_avg": a.get("raw_avg", 0.0),
                    "raw_med": a.get("raw_med", 0.0),
                    "market_avg": a.get("market_avg", 0.0),
                    "car_avg": a.get("car_avg", 0.0),
                    "car_med": a.get("car_med", 0.0),
                    "raw_up_prob": a.get("raw_up_prob", 0.0),
                    "up_prob": a.get("up_prob", 0.0),
                    "n": a.get("n", 0),
                    "conf": _grade(a.get("n", 0)),
                }
            block["buckets"][label] = brow
            m = brow["m"]
            summary.append((stype, label, m["n"], m["raw_avg"], m["car_avg"],
                            m["car_med"], m["up_prob"], m["conf"]))
        out[stype] = block

    # 기존 scale_buckets.json 에 비파괴 병합(3유형 블록 불변)
    if OUT.exists():
        result = json.loads(OUT.read_text(encoding="utf-8"))
    else:
        result = {"_meta": {}, "scale_buckets": {}}
    result["scale_buckets"].update(out)
    result.setdefault("_meta", {})["scale_doc_types"] = {
        "added": sorted(out.keys()),
        "rel_size_def": {
            "공급계약": "계약금액/최근연매출 %(rev_pct)",
            "소각": "소각주식/발행총수 %(pct)",
            "무상증자": "무상신주/증자전발행총수 %",
            "배당": "시가배당률 %(yield)",
        },
        "rel_labels": {k: REL_LABELS[k] for k in out},
        "bucket_bounds": {k: [b[2] for b in v] for k, v in BUCKETS_DOC.items()},
        "car_method": "익일 시가진입, 1/5/21거래일 보유, 자기시장 EW 보정(3유형과 동일).",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, OUT)
    log(f"저장(신규유형 병합): {OUT}")

    log("=== (신규유형×버킷) 1개월 요약 ===")
    log(f"{'유형':<8}{'버킷':<12}{'N':>6}{'raw%':>8}{'CAR%':>8}"
        f"{'CARmed%':>9}{'C>0':>6}  conf")
    for (st, lb, n, raw, car, cmed, up, conf) in summary:
        log(f"{st:<8}{lb:<12}{n:>6}{raw:>8}{car:>8}{cmed:>9}{up:>6}  {conf}")
    return result


# ---------------- merge ----------------
def cmd_merge():
    """impact_benchmark.json 에 각 유형별 scale_buckets 필드 추가(비파괴)."""
    if not OUT.exists():
        log("scale_buckets.json 없음 — 먼저 aggregate 실행")
        return
    if not IMPACT.exists():
        log("impact_benchmark.json 없음")
        return
    sb = json.loads(OUT.read_text(encoding="utf-8"))["scale_buckets"]
    bench = json.loads(IMPACT.read_text(encoding="utf-8"))
    added = 0
    for stype, block in sb.items():
        # stype(집계 라벨) -> impact_benchmark 최상위 키. 소각만 라벨 상이(주식소각).
        bkey = STYPE_BENCH_KEY.get(stype, stype)
        if bkey in bench and isinstance(bench[bkey], dict):
            bench[bkey]["scale_buckets"] = block
            added += 1
        else:
            log(f"  경고: '{stype}'(bench키 '{bkey}') 유형이 impact_benchmark 에 없음 — 스킵")
    all_bounds = {k: [b[2] for b in v] for k, v in BUCKETS.items()}
    all_bounds.update({k: [b[2] for b in v] for k, v in BUCKETS_DOC.items()})
    bench.setdefault("_meta", {})["scale_adjustment"] = {
        "added": sorted(sb.keys()),
        "rel_size_def": "유형별 상대규모(분모 상이): 유상/자사/전환=금액/시총%, "
                        "공급계약=계약금액/최근연매출%, 소각=소각주식/발행총수%, "
                        "무상증자=무상신주/증자전발행총수%, 배당=시가배당률%. 앱: 이벤트 규모로 "
                        "유형.scale_buckets[label] 선택, 버킷 표본부족(conf=참고,n<20) 시 "
                        "유형레벨 통계로 폴백.",
        "bucket_bounds": all_bounds,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = IMPACT.with_suffix(".merged.tmp")
    tmp.write_text(json.dumps(bench, ensure_ascii=False, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, IMPACT)
    log(f"병합 완료: {added}유형 scale_buckets 추가 -> {IMPACT}")


def cmd_census():
    by = load_events()
    evs = target_events(by)
    pairs = defaultdict(set)
    typ = defaultdict(int)
    for e in evs:
        pairs[e["endpoint"]].add(e["corp_code"])
        typ[e["stype"]] += 1
    log(f"고유공시 {len(by)} / 대상사건 {len(evs)}")
    for ep in sorted(pairs, key=lambda k: -len(pairs[k])):
        log(f"  {ep:22} corps={len(pairs[ep])}")
    log(f"유형별 사건: {dict(typ)}")
    log(f"DART 콜예산(corp×endpoint) = {sum(len(v) for v in pairs.values())}")
    log(f"pykrx 종목 = {len({e['stock_code'] for e in evs})}")


def cmd_bullet_prefetch():
    """현재 피드창(최근7일 KOSPI+KOSDAQ) bullet 대상 전건의 캐시를 미리 채운다.
    이후 라이브 피드(/api/alerts)는 캐시로 0콜 서빙 → 커버리지 즉시 상승.
    (오프라인 배치: budget 무제한, 피드경로와 분리 — 노트북 부하는 순차·sleep 로 관리)"""
    import dart_poll
    raw, errs = dart_poll.fetch_markets(days=7, markets=("Y", "K"),
                                        page_count=100, max_pages=5)
    log(f"피드원본 {len(raw)}건 (errors={errs})")
    budget = [10 ** 9]
    by_type = Counter()
    made = 0
    eligible = 0
    for it in raw:
        if not isinstance(it, dict):
            continue
        nm = (it.get("report_nm") or "").strip()
        if not bullet_eligible(nm):
            continue
        eligible += 1
        code = (it.get("stock_code") or "").strip()
        rno = (it.get("rcept_no") or "").strip()
        try:
            corp = (dart_poll.resolve_corp(code) or "") if code else ""
        except Exception:
            corp = ""
        try:
            bl = bullets_for_item(corp, code, nm, rno, it.get("rcept_dt", ""),
                                  allow_fetch=True, budget=budget, known_files=None)
        except Exception as e:
            log(f"  err {rno}: {repr(e)[:80]}")
            bl = []
        if bl:
            made += 1
            st = struct_route(nm)[1] or doc_route(nm) or "?"
            by_type[st] += 1
        if eligible % 25 == 0:
            log(f"  진행 대상 {eligible} / bullet {made}")
    log(f"=== 프리페치 완료: 대상 {eligible} / bullet 생성 {made} / 유형별 {dict(by_type)} ===")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "census"
    {"census": cmd_census, "mcap": cmd_mcap, "amounts": cmd_amounts,
     "aggregate": cmd_aggregate, "merge": cmd_merge,
     "fetch_phase1": cmd_fetch_phase1, "fetch_phase2": cmd_fetch_phase2,
     "aggregate_doc": cmd_aggregate_doc,
     "prefetch": cmd_bullet_prefetch}[cmd]()
