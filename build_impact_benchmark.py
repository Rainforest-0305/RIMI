# -*- coding: utf-8 -*-
"""공시유형별 과거 주가영향 벤치마크 — 시장보정 사건연구(event study).

방법(핵심, 미화 없음):
- DART list.json 시장전체 조회(corp_cls=Y, 유형 A/B/I, 3개월 청킹)로 과거
  ~5년 KOSPI 공시를 페이지네이션 수집. 종목별 개별조회 안 함(유량 배려).
- 각 공시를 summarize.classify 규칙으로 유형 태깅(제목 키워드, 결정적).
- 각 사례: 공시 접수일(rcept_dt) '다음' 거래일 시가 진입 → 보유
  1거래일(d)/5거래일(w)/21거래일(m) 후 종가 매도의 raw 등락 계산.
- 같은 구간 시장(KODEX200 069500, KOSPI200 근사) 시가→종가 등락을 차감한
  초과등락(CAR)이 핵심 지표. up_prob = CAR>0 비율.
- 표본 부족(<30) 시 상위 버킷 → 전체 순으로 폴백. scope 라벨로 기록.

한계(정직하게):
- 시장대용은 KOSPI 전체가 아니라 KOSPI200(069500 ETF). 소형주엔 베타 오차.
- 다중 태그 허용(예: [기재정정]유상증자 = 정정+유상증자 동시 집계). 유형 간
  독립 집계라 이중집계 아님(전체는 rcept_no 기준 dedup).
- 상장폐지·데이터부족 종목의 사건은 해당 구간 skip(생존편향 일부 잔존).
- rcept_dt는 날짜만(시각 없음). 장중/장후 접수 혼재를 익일 시가 진입으로 흡수.
- 진입/청산 무비용(수수료·슬리피지 미반영). 정보용 벤치마크이지 전략성과 아님.

산출: impact_benchmark.json  ({유형:{d/w/m:{car_avg,raw_avg,up_prob,car_med,n,conf,scope}}} + _meta)
KIS 무관. DART+pykrx 오프라인.
"""
import bisect
import json
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

import config
from summarize import classify

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent
CACHE = BASE / "bench_cache"
DART_CACHE = CACHE / "dart"
PX_CACHE = CACHE / "px"
for d in (CACHE, DART_CACHE, PX_CACHE):
    d.mkdir(parents=True, exist_ok=True)

KIS_PX_CACHE = Path(r"C:\Users\urimk\kis-trading\bt_kr_cache")  # 재사용(있으면)
LOG = CACHE / "build.log"
OUT = BASE / "impact_benchmark.json"

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
KEY = config.DART_API_KEY

# 사건 수집 기간. 익일진입+21거래일 전방창 확보 위해 종료를 과거로 둔다.
BGN = "20210101"
END = "20260515"
PBLNTF_TYPES = ["A", "B", "I"]  # 정기/주요사항/거래소(수시). C(발행)·D(지분)은 볼륨↑·신호↓로 제외
# 시장구분: DART corp_cls -> 시장명. 코스피(Y)·코스닥(K) 각각 시장전체 조회.
MARKETS = {"Y": "KOSPI", "K": "KOSDAQ"}
PX_START = "20201101"          # 2021초 사건의 진입 전 계산 여유
HORIZONS = [("d", 0), ("w", 4), ("m", 20)]  # 진입 익일 시가 기준 보유 거래일수-1

# 표본 부족 시 상위 버킷(방향 유사 유형 묶음). 없으면 전체로 폴백.
BUCKET = {
    "자사주": "주주환원", "주식소각": "주주환원", "배당": "주주환원",
    "유상증자": "자본조달", "전환사채": "자본조달",
    "최대주주변경": "지배구조", "지분변동": "지배구조",
    "실적": "실적", "감사보고서": "실적",
    "합병분할": "구조재편", "소송": "구조재편",
}
MIN_N = 30  # 이 미만이면 폴백

# ---- WS-32A 시장레짐 조건부 벤치마크(분류축 전용 — baseline CAR 산식 무변경) ----
REGIME_ETF = {"KOSPI": "069500", "KOSDAQ": "229200"}  # 국면 라벨링 ETF(baseline과 무관)
R20_WINDOW = 20             # r20 = C[t]/C[t-20]-1 (20거래일 모멘텀)
REGIME_BULL = 0.03          # r20 >= +3% → bull
REGIME_CRASH = -0.08        # r20 <= -8% → crash (그 외 neutral)
REGIME_MAX_REF_GAP_DAYS = 10  # 참조일-공시일 캘린더 갭 상한(초과=ETF 커버 밖/장기정지 → unknown)
REGIME_MIN_N = MIN_N        # 30: 미만이면 평균/확률 미노출(참고만)
REGIME_DROP_N = 5           # 미만이면 셀 생략


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------- 1) DART 사건 수집 ----------------
def _add_months(y, m, n):
    """(y,m) + n개월 -> (y,m)."""
    idx = (y * 12 + (m - 1)) + n
    return idx // 12, idx % 12 + 1


def month_chunks(bgn, end):
    """[bgn,end]를 달력 3개월(분기) 창으로. DART는 corp_code 없을 때 검색기간
    3개월 이하만 허용하므로 '월 1일 시작 → 3개월 뒤 1일 직전'으로 정확히 자른다."""
    b = datetime.strptime(bgn, "%Y%m%d")
    e = datetime.strptime(end, "%Y%m%d")
    out = []
    y, m = b.year, b.month
    cur = datetime(y, m, 1)
    while cur <= e:
        ny, nm = _add_months(cur.year, cur.month, 3)
        nxt = datetime(ny, nm, 1)
        chunk_end = min(nxt - timedelta(days=1), e)
        b_de = max(cur, b).strftime("%Y%m%d")   # 첫 창은 실제 bgn 존중
        out.append((b_de, chunk_end.strftime("%Y%m%d")))
        cur = nxt
    return out


def dart_fetch(cls, ty, bgn_de, end_de):
    """한 시장(cls)·유형(ty)·한 창의 시장전체 공시 목록(페이지네이션). 캐시.
    종목별 개별조회 없이 corp_cls 시장전체 list.json 만 사용(유량 배려).
    KOSPI(Y) 캐시는 기존 파일명 그대로 재사용, 코스닥(K)은 접두어 분리."""
    prefix = "" if cls == "Y" else f"{cls}_"
    cf = DART_CACHE / f"{prefix}{ty}_{bgn_de}_{end_de}.json"
    mkt = MARKETS[cls]
    if cf.exists():
        cached = json.loads(cf.read_text(encoding="utf-8"))
        for it in cached:  # 구버전 캐시(market 필드 없음) 보정
            it["market"] = mkt
        return cached
    items = []
    page = 1
    total_page = 1
    while page <= total_page:
        params = {
            "crtfc_key": KEY, "corp_cls": cls, "pblntf_ty": ty,
            "bgn_de": bgn_de, "end_de": end_de,
            "page_no": page, "page_count": 100,
        }
        for attempt in range(4):
            try:
                d = requests.get(LIST_URL, params=params, timeout=25).json()
            except Exception as e:
                log(f"    req err {cls}/{ty} {bgn_de} p{page}: {repr(e)[:80]}")
                time.sleep(2)
                continue
            st = d.get("status")
            if st == "013":       # 데이터 없음 = 정상
                d = {"list": [], "total_page": 0}
                break
            if st == "020":       # 유량 초과 → 대기 재시도
                log(f"    rate-limit(020) {cls}/{ty} {bgn_de} p{page}, sleep 30")
                time.sleep(30)
                continue
            if st != "000":
                log(f"    status {st} {cls}/{ty} {bgn_de} p{page}: {d.get('message')}")
                d = {"list": [], "total_page": 0}
                break
            break
        total_page = int(d.get("total_page") or 0)
        for it in d.get("list", []):
            if it.get("corp_cls") != cls:
                continue
            sc = (it.get("stock_code") or "").strip()
            if len(sc) != 6 or not sc.isdigit():
                continue
            items.append({
                "rcept_no": it.get("rcept_no"),
                "corp_code": it.get("corp_code"),
                "corp_name": it.get("corp_name"),
                "stock_code": sc,
                "market": MARKETS[cls],
                "report_nm": (it.get("report_nm") or "").strip(),
                "rcept_dt": (it.get("rcept_dt") or "").strip(),
            })
        page += 1
        time.sleep(0.12)
    cf.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return items


def collect_events():
    chunks = month_chunks(BGN, END)
    log(f"수집: {len(MARKETS)}시장 x {len(PBLNTF_TYPES)}유형 x {len(chunks)}창(3개월)")
    by_rno = {}
    for cls, mkt in MARKETS.items():
        mkt_cnt = 0
        for ty in PBLNTF_TYPES:
            cnt = 0
            for (b, e) in chunks:
                items = dart_fetch(cls, ty, b, e)
                cnt += len(items)
                for it in items:
                    rno = it["rcept_no"]
                    if not rno:
                        continue
                    by_rno.setdefault(rno, it)  # dedup by rcept_no
            log(f"  [{mkt}] 유형 {ty}: {cnt}건 수집")
            mkt_cnt += cnt
        log(f"  [{mkt}] 소계: {mkt_cnt}건")
    log(f"고유 공시(dedup): {len(by_rno)}건")
    return list(by_rno.values())


# ---------------- 2) 가격 로드(pykrx, 캐시 재사용) ----------------
def load_px(code):
    """{ 'YYYY-MM-DD': [open, close] }. 실패 시 {}."""
    cf = PX_CACHE / f"{code}.json"
    if cf.exists():
        return json.loads(cf.read_text())
    # kis-trading bt_kr_cache 재사용(있고 충분히 최신이면)
    kis = KIS_PX_CACHE / f"ohlcv_{code}.json"
    if kis.exists():
        try:
            raw = json.loads(kis.read_text())
            dates = sorted(raw.keys())
            if dates and dates[-1] >= "2026-06-01":  # 전방창 커버되면 재사용
                out = {d: [v[0], v[1]] for d, v in raw.items()}
                cf.write_text(json.dumps(out))
                return out
        except Exception:
            pass
    try:
        from pykrx import stock
        end = datetime.now().strftime("%Y%m%d")
        df = stock.get_market_ohlcv_by_date(PX_START, end, code)
    except Exception as e:
        log(f"    pykrx fail {code}: {repr(e)[:80]}")
        cf.write_text("{}")
        return {}
    out = {}
    if df is not None and len(df):
        for d, row in df.iterrows():
            try:
                o = float(row["시가"]); c = float(row["종가"])
            except (KeyError, ValueError, TypeError):
                continue
            if o > 0 and c > 0:
                out[d.strftime("%Y-%m-%d")] = [o, c]
    cf.write_text(json.dumps(out))
    time.sleep(0.25)
    return out


def build_ew_market(px_all):
    """캐시된 전 종목(≈전체 KOSPI)으로 '동일가중(EW) 전체시장' 가치 시계열 구축.
    KOSPI200(069500)·종합지수(1001, KRX사일로 사망)로는 소형주 편향을 못 없애므로,
    보유 유니버스 자체의 EW 포트폴리오(일별 리밸런스)를 시장대용으로 쓴다.

    반환: (V_open, V_close) — 각 date -> 시장가치(임의단위). 진입 T0 시가~청산 exd
    종가 시장등락 = V_close[exd]/V_open[T0]-1.
    """
    sum_oc, cnt_oc = {}, {}   # 당일 시가->종가 EW 비율
    sum_co, cnt_co = {}, {}   # 전일종가->당일시가(overnight) EW 비율
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
        if prev_close is None:
            vo = 1.0
        else:
            vo = prev_close * r_co
        vc = vo * r_oc
        V_open[d] = vo
        V_close[d] = vc
        prev_close = vc
    if dates:
        log(f"EW 지수: {dates[0]}~{dates[-1]} ({len(dates)}거래일), "
            f"평균구성 {int(sum(cnt_oc.values())/max(len(dates),1))}종목/일")
    return V_open, V_close


# ---------------- 3) 사건별 초과등락 계산(시장별 baseline) ----------------
def compute_returns(events):
    codes = sorted({e["stock_code"] for e in events})
    # 종목->시장(사건 corp_cls 기준). 시장이전 종목은 마지막 관측 시장으로 귀속.
    code_market = {}
    for e in events:
        code_market[e["stock_code"]] = e["market"]
    log(f"가격 필요 종목: {len(codes)}개 "
        f"(KOSPI {sum(1 for m in code_market.values() if m=='KOSPI')}, "
        f"KOSDAQ {sum(1 for m in code_market.values() if m=='KOSDAQ')})")
    px_all = {}
    for i, c in enumerate(codes):
        px_all[c] = load_px(c)
        if (i + 1) % 100 == 0:
            log(f"  가격 로드 {i+1}/{len(codes)}")

    # 시장별 EW 전체시장 지수를 각 시장 유니버스로 독립 구축.
    # CAR = 종목 raw - 같은구간 '자기 시장' EW 등락 (시장 mismatch 편향 방지).
    V = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        sub = {c: p for c, p in px_all.items()
               if code_market.get(c) == mkt and p}
        log(f"  [{mkt}] EW 구축 (유니버스 {len(sub)}종목)")
        V[mkt] = build_ew_market(sub)

    ok = 0
    skip_no_px = 0
    skip_fwd = 0
    for e in events:
        e["ret"] = {}
        px = px_all.get(e["stock_code"]) or {}
        if not px:
            skip_no_px += 1
            continue
        V_open, V_close = V[e["market"]]
        dates = sorted(px.keys())
        r = e["rcept_dt"]
        if len(r) != 8:
            continue
        riso = f"{r[0:4]}-{r[4:6]}-{r[6:8]}"
        i0 = bisect.bisect_right(dates, riso)  # 접수일 다음 거래일
        if i0 >= len(dates):
            skip_fwd += 1
            continue
        t0 = dates[i0]
        entry = px[t0][0]
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
            mret = (mkt_c - mkt_o) / mkt_o if mkt_c else None
            car = raw - mret if mret is not None else raw
            e["ret"][label] = (raw, mret if mret is not None else 0.0, car)
            any_h = True
        if any_h:
            ok += 1
        else:
            skip_fwd += 1
    log(f"계산 완료: 유효 {ok} / 가격없음 {skip_no_px} / 전방부족 {skip_fwd}")
    return events


# ---------------- 4) 집계 ----------------
def agg(evlist, horizon):
    vals = [e["ret"].get(horizon) for e in evlist if e.get("ret", {}).get(horizon)]
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


def grade(n):
    if n >= 80:
        return "높음"
    if n >= 30:
        return "보통"
    return "참고"


def _pools(evsubset):
    """이벤트 부분집합 -> (유형별 사건, 버킷별 사건, dedup 전체)."""
    type_events = {}
    for e in evsubset:
        for t in e["tags"]:
            type_events.setdefault(t, []).append(e)
    bucket_events = {}
    for t, evs in type_events.items():
        b = BUCKET.get(t)
        if b:
            bucket_events.setdefault(b, []).extend(evs)
    uniq = list({e["rcept_no"]: e for e in evsubset}.values())
    return type_events, bucket_events, uniq


def _row_for(t, type_events, bucket_events, uniq):
    """유형 t의 d/w/m 창별 통계(self→bucket→전체 폴백). 해당 풀 기준."""
    evs = type_events.get(t, [])
    b = BUCKET.get(t)
    bpool = bucket_events.get(b, []) if b else []
    row = {}
    for label, _ in HORIZONS:
        self_a = agg(evs, label)
        if self_a["n"] >= MIN_N:
            used, scope = self_a, t
        else:
            b_a = agg(bpool, label) if b else {"n": 0}
            if b and b_a["n"] >= MIN_N:
                used, scope = b_a, b
            else:
                used, scope = agg(uniq, label), "전체"
        row[label] = {
            "raw_avg": used.get("raw_avg", 0.0),
            "raw_med": used.get("raw_med", 0.0),
            "market_avg": used.get("market_avg", 0.0),
            "car_avg": used.get("car_avg", 0.0),
            "car_med": used.get("car_med", 0.0),
            "raw_up_prob": used.get("raw_up_prob", 0.0),
            "up_prob": used.get("up_prob", 0.0),
            "n": used.get("n", 0),
            "conf": grade(used.get("n", 0)),
            "scope": scope,
        }
    return row


def build_output(events):
    for e in events:
        e["tags"] = classify(e["report_nm"])

    # 통합(코스피+코스닥, 각 사건은 '자기 시장' EW로 보정됨) 풀
    type_all, bucket_all, uniq_all = _pools(events)
    # 시장별 풀
    per_mkt = {m: _pools([e for e in events if e["market"] == m])
               for m in ("KOSPI", "KOSDAQ")}

    types_out = {}
    summary_rows = []
    for t in sorted(type_all.keys()):
        # 상위: 통합(시장별 baseline 보정) — impact.py 리더가 그대로 읽는 스키마.
        row = _row_for(t, type_all, bucket_all, uniq_all)
        # 시장별 세부(추가 필드, 리더는 무시). 해당 시장에 표본 있으면만.
        bm = {}
        for m in ("KOSPI", "KOSDAQ"):
            te, be, uq = per_mkt[m]
            if t in te:
                bm[m] = _row_for(t, te, be, uq)
        row["by_market"] = bm
        types_out[t] = row

        m_all = row.get("m", {})
        km = bm.get("KOSPI", {}).get("m", {})
        qm = bm.get("KOSDAQ", {}).get("m", {})
        summary_rows.append((
            t, m_all.get("n"), m_all.get("raw_avg"), m_all.get("market_avg"),
            m_all.get("car_avg"), m_all.get("car_med"), m_all.get("up_prob"),
            m_all.get("conf"), m_all.get("scope"),
            km.get("n"), km.get("car_avg"), km.get("up_prob"),
            qm.get("n"), qm.get("car_avg"), qm.get("up_prob"),
        ))

    n_kospi = sum(1 for e in events if e["market"] == "KOSPI")
    n_kosdaq = sum(1 for e in events if e["market"] == "KOSDAQ")
    meta = {
        "method": "market-adjusted event study. 진입=공시 접수일 익일 시가, "
                  "보유 1/5/21거래일 후 종가. CAR = 종목 raw등락 - 같은구간 "
                  "'자기 시장' 동일가중(EW) 전체시장 등락(코스피 종목은 코스피 EW, "
                  "코스닥 종목은 코스닥 EW로 보정 → 시장 mismatch 편향 방지).",
        "index_proxy": "시장별 동일가중(EW) 전체시장 지수 — 각 시장의 보유 유니버스"
                       "(과거 5년 공시발생 종목, ≈전체시장) 일별 리밸런스 EW 포트폴리오를 "
                       "코스피/코스닥 독립 구축. KOSPI200(069500)·KOSDAQ150은 대형주 "
                       "편향, 종합지수(1001 등)는 KRX사일로 사망으로 pykrx 조회 불가 → "
                       "시장별 EW 자체구축이 size편향 제거에 최선.",
        "markets": "KOSPI(corp_cls=Y) + KOSDAQ(corp_cls=K) 시장전체 수집·집계. "
                   "상위 유형통계는 두 시장 통합(각 사건 자기시장 baseline 보정), "
                   "by_market 에 코스피/코스닥 각각의 d/w/m 별도 제공.",
        "span": f"{BGN}~{END}",
        "pblntf_types": PBLNTF_TYPES,
        "n_unique_disclosures": len(uniq_all),
        "n_disclosures_kospi": n_kospi,
        "n_disclosures_kosdaq": n_kosdaq,
        "min_n_for_selfscope": MIN_N,
        "conf_grades": "n>=80 높음 / 30-79 보통 / <30 참고",
        "fields": "raw_avg=실제평균등락%, raw_med=실제등락 중앙값%, "
                  "market_avg=같은구간 시장평균등락%, "
                  "car_avg=초과등락(raw-market)%, car_med=초과등락 중앙값%, "
                  "raw_up_prob=raw>0비율, up_prob=CAR>0비율, n=표본, "
                  "scope=집계에 실제로 쓴 라벨(폴백 시 상위버킷/전체), "
                  "by_market={KOSPI/KOSDAQ}.{d/w/m} 시장별 동일 필드. "
                  "제품 표시 권장: '실제 raw_avg% / 같은기간 시장 market_avg% "
                  "→ 초과 car_avg%'.",
        "limitations": "시장대용=시장별 자체구축 EW(전체시장 근사, 종합지수 직접조회 "
                       "불가), 진입/청산 무비용(전략성과 아닌 정보용 벤치마크), "
                       "상폐/데이터부족 사건 일부 skip(생존편향 일부 잔존 — 특히 "
                       "코스닥은 상폐율 높아 생존편향이 코스피보다 큼), "
                       "다중태그 허용(정정+원공시 동시집계), rcept_dt 날짜단위(장중/"
                       "장후 혼재를 익일시가 진입으로 흡수), 시총 size-matched "
                       "벤치마크는 KRX 시총 스냅샷 사망으로 미적용.",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    out = {"_meta": meta}
    out.update(types_out)
    return out, summary_rows


# ---------------- 5) WS-32A 시장레짐 조건부 벤치마크 ----------------
def _soft_check_etf(code, series):
    """(b)2018~현재 커버 (c)전행 len=2 (d)종가>0 실측. 반환 bool, 로그 남김."""
    if not series:
        log(f"[ASSERT {code}] (data) 시계열 없음 → FAIL")
        return False
    dates = sorted(series.keys())
    cov_ok = (dates[0] <= "2018-01-31") and (dates[-1] >= "2026-05-01")
    len_ok = all(isinstance(v, list) and len(v) == 2 for v in series.values())
    pos_ok = all(v[1] > 0 for v in series.values())
    log(f"[ASSERT {code}] (b)커버 {dates[0]}~{dates[-1]}: {'PASS' if cov_ok else 'FAIL'} | "
        f"(c)전행len=2: {'PASS' if len_ok else 'FAIL'} | (d)종가>0: {'PASS' if pos_ok else 'FAIL'} "
        f"(n={len(series)})")
    return cov_ok and len_ok and pos_ok


def fetch_and_assert_kosdaq_etf():
    """229200(KODEX 코스닥150) 2018~현재 fetch + 4중 assert.
    실패 시 None → KOSDAQ 레짐만 unknown 폴백(크래시 금지, 무조건부·by_market 무영향).
    캐시 우선(오프라인 재현). 신규 fetch는 229200 1종뿐."""
    code = "229200"
    cf = PX_CACHE / f"{code}.json"
    if cf.exists():
        series = json.loads(cf.read_text())
        log(f"[ASSERT {code}] 캐시 사용 ({len(series)}일)")
    else:
        try:
            from pykrx import stock
            end = datetime.now().strftime("%Y%m%d")
            df = stock.get_market_ohlcv_by_date("20180101", end, code)
        except Exception as e:
            log(f"[ASSERT {code}] FETCH FAIL: {repr(e)[:120]} → KOSDAQ unknown 폴백")
            return None
        series = {}
        if df is not None and len(df):
            for d, row in df.iterrows():
                try:
                    o = float(row["시가"]); c = float(row["종가"])
                except (KeyError, ValueError, TypeError):
                    continue
                if o > 0 and c > 0:
                    series[d.strftime("%Y-%m-%d")] = [o, c]
        cf.write_text(json.dumps(series))
        log(f"[ASSERT {code}] fetch 완료 ({len(series)}일) → 캐시 {cf.name}")
    # (a) 티커명: KRX 로그인 필요라 조회 불가 시 SKIP(폴백 유발 안 함). 조회되면 불일치 시만 FAIL.
    name_ok = None
    try:
        from pykrx import stock
        nm = stock.get_market_ticker_name(code)
        nm = nm if isinstance(nm, str) else ""
        if nm.strip():
            name_ok = ("코스닥150" in nm.replace(" ", "")) or ("코스닥" in nm)
            log(f"[ASSERT {code}] (a)티커명='{nm}': "
                f"{'PASS' if name_ok else 'FAIL(KODEX 코스닥150 불일치)'}")
        else:
            log(f"[ASSERT {code}] (a)티커명 조회 빈값(KRX 로그인 필요 추정) → SKIP")
    except Exception as e:
        log(f"[ASSERT {code}] (a)티커명 조회 불가(KRX 로그인 필요 추정) → SKIP: {repr(e)[:80]}")
    if name_ok is False:
        log(f"[ASSERT {code}] (a) FAIL → KOSDAQ unknown 폴백")
        return None
    if not _soft_check_etf(code, series):
        log(f"[ASSERT {code}] (b/c/d) FAIL → KOSDAQ unknown 폴백")
        return None
    log(f"[ASSERT {code}] 4중 assert 통과(a=SKIP허용, b/c/d PASS) → KOSDAQ 레짐 활성")
    return series


def load_regime_etfs():
    """{'KOSPI':series|None, 'KOSDAQ':series|None}. 069500=캐시(신규fetch 0), 229200=fetch+assert."""
    etfs = {}
    k200 = load_px("069500")  # 캐시 히트(2092일) — 신규 fetch 없음
    etfs["KOSPI"] = k200 if _soft_check_etf("069500", k200) else None
    if etfs["KOSPI"] is None:
        log("[ASSERT 069500] soft check FAIL → KOSPI 레짐 unknown 폴백")
    etfs["KOSDAQ"] = fetch_and_assert_kosdaq_etf()
    return etfs


def _regime_index(series):
    dates = sorted(series.keys())
    closes = [series[d][1] for d in dates]
    return dates, closes


def classify_regime_r20(r20):
    if r20 <= REGIME_CRASH:
        return "crash"
    if r20 >= REGIME_BULL:
        return "bull"
    return "neutral"


def tag_regimes(events, etfs):
    """각 이벤트에 e['regime'] in {bull,neutral,crash,unknown}. 참조일=rcept_dt 이하
    마지막 ETF 거래일(bisect_right-1). ri<20/갭>N/ETF부재 → unknown. baseline 무변경(라벨만)."""
    idx = {m: (_regime_index(s) if s else None) for m, s in etfs.items()}
    counts = {"bull": 0, "neutral": 0, "crash": 0, "unknown": 0}
    reasons = {"no_etf": 0, "ri_lt_20": 0, "gap": 0, "bad_date": 0}
    for e in events:
        ent = idx.get(e["market"])
        r = e["rcept_dt"]
        if ent is None:
            e["regime"] = "unknown"; counts["unknown"] += 1; reasons["no_etf"] += 1; continue
        if len(r) != 8:
            e["regime"] = "unknown"; counts["unknown"] += 1; reasons["bad_date"] += 1; continue
        dates, closes = ent
        riso = f"{r[0:4]}-{r[4:6]}-{r[6:8]}"
        ri = bisect.bisect_right(dates, riso) - 1
        if ri < R20_WINDOW:
            e["regime"] = "unknown"; counts["unknown"] += 1; reasons["ri_lt_20"] += 1; continue
        try:
            gap = (datetime.strptime(riso, "%Y-%m-%d")
                   - datetime.strptime(dates[ri], "%Y-%m-%d")).days
        except ValueError:
            gap = 0
        if gap > REGIME_MAX_REF_GAP_DAYS:
            e["regime"] = "unknown"; counts["unknown"] += 1; reasons["gap"] += 1; continue
        c_now, c_prev = closes[ri], closes[ri - R20_WINDOW]
        if c_prev <= 0:
            e["regime"] = "unknown"; counts["unknown"] += 1; reasons["bad_date"] += 1; continue
        reg = classify_regime_r20(c_now / c_prev - 1.0)
        e["regime"] = reg; counts[reg] += 1
    return counts, reasons


def _full_cell(a):
    """agg() 출력 → 리더 호환 전체필드 셀(+conf)."""
    return {
        "raw_avg": a["raw_avg"], "raw_med": a["raw_med"],
        "market_avg": a["market_avg"], "car_avg": a["car_avg"],
        "car_med": a["car_med"], "raw_up_prob": a["raw_up_prob"],
        "up_prob": a["up_prob"], "n": a["n"], "conf": grade(a["n"]),
    }


def _regime_cell(evlist, horizon):
    """레짐 부분집합 셀. n<5 → None(생략), 5<=n<30 → {n,conf:'참고'}, n>=30 → 전체필드."""
    a = agg(evlist, horizon)
    n = a.get("n", 0)
    if n < REGIME_DROP_N:
        return None
    if n < REGIME_MIN_N:
        return {"n": n, "conf": "참고"}
    return _full_cell(a)


def build_regime_overlay(events):
    """유형별 by_regime + 시장×레짐 교차(min_n 통과 셀만). baseline agg 무변경(부분집합 필터만)."""
    for e in events:
        if "tags" not in e:
            e["tags"] = classify(e["report_nm"])
    type_events = {}
    for e in events:
        for t in e["tags"]:
            type_events.setdefault(t, []).append(e)
    REGS = ("bull", "neutral", "crash")
    overlay, cross = {}, {}
    cstats = {"regime_cells": 0, "cross_cells": 0}
    for t, evs in type_events.items():
        by_reg = {}
        for reg in REGS:
            sub = [e for e in evs if e.get("regime") == reg]
            cells = {}
            for label, _ in HORIZONS:
                c = _regime_cell(sub, label)
                if c is not None:
                    cells[label] = c
                    cstats["regime_cells"] += 1
            if cells:
                by_reg[reg] = cells
        if by_reg:
            overlay[t] = by_reg
        for mkt in ("KOSPI", "KOSDAQ"):
            mevs = [e for e in evs if e["market"] == mkt]
            mby = {}
            for reg in REGS:
                sub = [e for e in mevs if e.get("regime") == reg]
                cells = {}
                for label, _ in HORIZONS:
                    a = agg(sub, label)
                    if a.get("n", 0) >= REGIME_MIN_N:
                        cells[label] = _full_cell(a)
                        cstats["cross_cells"] += 1
                if cells:
                    mby[reg] = cells
            if mby:
                cross.setdefault(t, {})[mkt] = mby
    return overlay, cross, cstats


def _strip_regime(obj):
    """재귀적으로 'by_regime' 키 제거(모든 깊이). _meta.regime_axis는 호출부 별도 처리."""
    if isinstance(obj, dict):
        return {k: _strip_regime(v) for k, v in obj.items() if k != "by_regime"}
    if isinstance(obj, list):
        return [_strip_regime(x) for x in obj]
    return obj


def merge_regime(overlay, cross, regime_axis):
    """비파괴 머지 + 회귀 바이트대조 assert. 루트 impact_benchmark.json만 갱신(data/ 금지).
    실패 시 원본 미변경(쓰기 안 함)."""
    import hashlib
    import os
    root = OUT
    orig_text = root.read_text(encoding="utf-8")
    orig = json.loads(orig_text)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    premerge = root.with_name(root.name + f".premerge_{ts}")
    premerge.write_text(orig_text, encoding="utf-8")
    log(f"premerge 백업: {premerge.name} "
        f"(sha256={hashlib.sha256(orig_text.encode('utf-8')).hexdigest()[:24]}, {len(orig_text.encode('utf-8'))}B)")
    base_norm = json.dumps(orig, sort_keys=True, ensure_ascii=False)

    merged = json.loads(orig_text)  # 독립 사본
    added = 0
    for t, by_reg in overlay.items():
        if t not in merged:
            log(f"  [WARN] overlay 유형 '{t}' 루트에 없음 — skip")
            continue
        merged[t]["by_regime"] = by_reg
        added += 1
    for t, mkts in cross.items():
        if t not in merged:
            continue
        bm = merged[t].get("by_market")
        if not isinstance(bm, dict):
            continue
        for mkt, mby in mkts.items():
            if mkt in bm and isinstance(bm[mkt], dict):
                bm[mkt]["by_regime"] = mby
    merged["_meta"]["regime_axis"] = regime_axis

    # 회귀 바이트대조: 머지본에서 by_regime + _meta.regime_axis 제거 → 정규화 → premerge와 완전일치
    check = _strip_regime(merged)
    check["_meta"] = {k: v for k, v in check["_meta"].items() if k != "regime_axis"}
    if json.dumps(check, sort_keys=True, ensure_ascii=False) != base_norm:
        log("[REGRESSION] FAIL: 정규화 문자열 불일치 → 머지 중단(원본 미변경)")
        return False, premerge
    log("[REGRESSION] PASS: by_regime/_meta.regime_axis 제거본 == premerge 정규화본 (바이트 동등)")

    m = merged["_meta"]
    for name, want in (("n_unique_disclosures", 342584),
                       ("n_disclosures_kospi", 143176),
                       ("n_disclosures_kosdaq", 199408)):
        assert m[name] == want, f"anchor {name} {m[name]}!={want}"
        log(f"[ANCHOR] {name}={m[name]} == {want} PASS")
    div = merged["배당"]
    kp = div["by_market"]["KOSPI"]["d"]["n"]
    kq = div["by_market"]["KOSDAQ"]["d"]["n"]
    assert div["d"]["n"] == 14431 and kp + kq == 14431, "배당 d.n 변동"
    log(f"[ANCHOR] 배당 d.n=14431 (KOSPI {kp}+KOSDAQ {kq}) PASS")
    for t in [k for k in orig if k != "_meta"]:
        assert merged[t]["d"]["n"] == orig[t]["d"]["n"], f"{t} d.n 변동"
    log("[ANCHOR] 16유형 d.n 전부 불변 PASS")

    tmp = root.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, root)
    new_text = root.read_text(encoding="utf-8")
    log(f"머지 저장: {root} (by_regime 유형 {added}, "
        f"sha256={hashlib.sha256(new_text.encode('utf-8')).hexdigest()[:24]}, "
        f"{len(new_text.encode('utf-8'))}B)")
    return True, premerge


def main_regime():
    log("=== WS-32A 레짐 조건부 벤치마크 머지 시작 ===")
    etfs = load_regime_etfs()
    events = collect_events()        # DART 캐시(오프라인)
    events = compute_returns(events)  # px 캐시(오프라인) — raw/mret/car 계산
    for e in events:
        e["tags"] = classify(e["report_nm"])
    counts, reasons = tag_regimes(events, etfs)
    total = len(events)
    unknown = counts["unknown"]
    unk_share = unknown / total if total else 0.0
    log(f"[REGIME] 태깅: bull {counts['bull']} / neutral {counts['neutral']} / "
        f"crash {counts['crash']} / unknown {unknown} (총 {total})")
    log(f"[REGIME] unknown 비율 {unk_share*100:.2f}% (사유: no_etf {reasons['no_etf']}, "
        f"ri<20 {reasons['ri_lt_20']}, gap {reasons['gap']}, bad_date {reasons['bad_date']})")
    if unk_share > 0.05:
        log(f"[ALERT] unknown 비율 {unk_share*100:.2f}% > 5% — 표면화 필요")

    overlay, cross, cstats = build_regime_overlay(events)
    log(f"[REGIME] by_regime 셀 {cstats['regime_cells']}, 교차(min_n) 셀 {cstats['cross_cells']}, "
        f"by_regime 유형수 {len(overlay)}")

    def share(x):
        return round(x / total, 4) if total else 0.0
    regime_axis = {
        "definition": "시장국면 라벨. 각 공시의 참조일(rcept_dt 이하 마지막 시장ETF 거래일) "
                      "종가의 20거래일 수익률 r20으로 3구간 분류. baseline CAR(raw-market) "
                      "산식과 완전 무관 — 분류축 전용(개별 이벤트 raw/mret/car 무변경).",
        "metric": "r20 = C[t]/C[t-20] - 1 (20거래일 모멘텀)",
        "source_etf": {"KOSPI": "069500 (KODEX200)", "KOSDAQ": "229200 (KODEX 코스닥150)"},
        "boundaries_numeric": {"crash": "r20 <= -0.08", "neutral": "-0.08 < r20 < 0.03",
                               "bull": "r20 >= 0.03"},
        "internal_keys": ["bull", "neutral", "crash"],
        "empirical_event_share": {"bull": share(counts["bull"]),
                                  "neutral": share(counts["neutral"]),
                                  "crash": share(counts["crash"]),
                                  "unknown": share(counts["unknown"])},
        "min_n": REGIME_MIN_N,
        "drop_below_n": REGIME_DROP_N,
        "unknown_policy": "ri<20(전방 20거래일 미확보) 또는 참조일-공시일 갭>10캘린더일"
                          "(ETF 커버 밖/장기정지) 또는 시장ETF assert 실패 → unknown. "
                          "조건부집계에서만 제외, 무조건부·by_market 무영향.",
        "unknown_events": unknown,
        "unknown_share": share(unknown),
        "kospi_etf_status": "OK" if etfs["KOSPI"] else "FALLBACK_UNKNOWN",
        "kosdaq_etf_status": "OK" if etfs["KOSDAQ"] else "FALLBACK_UNKNOWN(229200 assert 실패)",
        "proposed_labels_ko": {"bull": "강세", "neutral": "중립/보합", "crash": "급락"},
        "labels_status": "제안(미확정). 최종 한글 wording은 President 전담. 중요: "
                         "neutral(-8%<r20<+3%)은 완만한 상승(+0~+3%)을 포함하므로 "
                         "'약세'로 표기 금지.",
        "president_note": "President 원문 라벨은 강세/약세/급락 3자. 본 구현 중간대(neutral)를 "
                          "'약세'로 쓰려면 경계를 r20<0 등으로 재정의 필요(데이터 근거 별도 산출). "
                          "현재 제안: 강세=r20>=+3%, 급락=r20<=-8%, 중간대='중립/보합'.",
        "baseline_note": "레짐 ETF(069500/229200)는 국면 라벨 전용. CAR baseline은 시장별 "
                         "EW 자체구축 지수(index_proxy 참조)로 별개 계열.",
    }

    ok, premerge = merge_regime(overlay, cross, regime_axis)
    if ok:
        log("=== 레짐 머지 완료(회귀 assert 통과) ===")
    else:
        log(f"=== 레짐 머지 중단(회귀 실패, 원본 미변경). premerge={premerge.name} ===")
    return ok


# ---------------- main ----------------
def main():
    LOG.write_text("", encoding="utf-8")
    log("=== 공시영향 벤치마크 빌드 시작 ===")
    events = collect_events()
    events = compute_returns(events)
    out, rows = build_output(events)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, OUT)
    log(f"저장: {OUT}")
    log("=== 유형별 요약(1개월=21거래일, CAR=자기시장 EW 보정 초과등락) ===")
    log(f"{'유형':<10}{'N':>7}{'raw%':>7}{'CAR%':>7}{'C>0':>6}"
        f" | {'KP_N':>6}{'KP_CAR':>7}{'KP>0':>6}"
        f" | {'KQ_N':>6}{'KQ_CAR':>7}{'KQ>0':>6}  scope")
    for (t, n, raw, mkt, car, cmed, up, conf, scope,
         kn, kcar, kup, qn, qcar, qup) in sorted(
            rows, key=lambda x: -((x[4] if x[4] is not None else -99))):
        log(f"{t:<10}{_f(n):>7}{_f(raw):>7}{_f(car):>7}{_f(up):>6}"
            f" | {_f(kn):>6}{_f(kcar):>7}{_f(kup):>6}"
            f" | {_f(qn):>6}{_f(qcar):>7}{_f(qup):>6}  {conf}/{scope}")
    log("=== 완료 ===")


def _f(v):
    return v if v is not None else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--regime-merge":
        main_regime()  # WS-32A: 레짐 조건부 벤치마크만 비파괴 머지(무조건부 재빌드 안 함)
    else:
        main()
