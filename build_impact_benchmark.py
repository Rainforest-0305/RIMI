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
    main()
