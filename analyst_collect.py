# -*- coding: utf-8 -*-
"""애널리스트 전망(한경컨센서스) 수집기 + 토스 종가 궤적 (하루 1회 배치, 저강도).

검증된 로직(analyst_proto/hk_collect.py, prep_data.py) 이식.

핵심 함정(필수 준수):
  - 한경 검색은 종목명 부분일치가 변덕스러워 정식명은 실패하고 부분명은 성공한다.
    → 반환행 제목에 박힌 (6자리코드) 로 타깃종목을 재검증한다(row_code==code 만 채택).
  - 정본 도메인은 consensus.hankyung.com. 요청 간 >=1초 슬립(저강도), 하루 1회.

대상 = 시총 Top100(top100.json) ∪ 최근 /api/ranking 등장 코드(로컬 서버 가동 시 best-effort).
종목별 payload: reports(목표가>0 & window 이후) + prices(toss candles) + current + avg_tp
             + n_total + n_tp + window_start.

저장: Supabase analyst_consensus(code upsert) + 로컬 data/analyst_cache.json(폴백).
모든 네트워크/파싱 실패는 종목 단위로 격리(한 종목 실패가 배치를 멈추지 않음)."""
import re
import sys
import time
from datetime import date, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

import config
import miri_cache as mc

# toss candles: prep_data.py 방식(kis-trading 경로 삽입 후 toss_data import)
sys.path.insert(0, r"C:/Users/urimk/kis-trading")
try:
    import toss_data as _toss
except Exception as _e:  # noqa: BLE001
    _toss = None
    print(f"[analyst] toss_data import 실패(가격 생략): {type(_e).__name__}",
          file=sys.stderr)

_HK_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 Safari/537.36",
         "Referer": "https://consensus.hankyung.com/"}
_HK_LIST = "https://consensus.hankyung.com/analysis/list"
_TABLE = "analyst_consensus"
_CACHE_FILE = config.DATA / "analyst_cache.json"
_CORP_INDEX_FILE = config.DATA / "corp_index.json"
_ALIAS_FILE = config.DATA / "corp_alias.json"
_DISCLAIMER = "증권사 전망을 정리한 참고 자료이며 투자 권유가 아닙니다"

_DDL = """
CREATE TABLE IF NOT EXISTS analyst_consensus (
  code text PRIMARY KEY,
  name text,
  current bigint,
  avg_tp bigint,
  n_total int,
  n_tp int,
  window_start text,
  updated_at text,
  prices jsonb,
  reports jsonb
);"""


# ----------------- 종목명↔코드 마스터 + 별칭 -----------------
def _load_corp_index():
    rows = mc.load_json(_CORP_INDEX_FILE, default=[]) or []
    return rows if isinstance(rows, list) else []


def build_name_map():
    """code→name, name→code(+별칭) 조회 맵. corp_alias.json 이 있으면 별칭 병합."""
    idx = _load_corp_index()
    code2name, name2code = {}, {}
    for r in idx:
        if not isinstance(r, dict):
            continue
        code = str(r.get("code") or "").strip()
        name = str(r.get("name") or "").strip()
        if code:
            code2name[code] = name
        if name and code:
            name2code.setdefault(name.lower(), code)
    alias = mc.load_json(_ALIAS_FILE, default={}) or {}
    if isinstance(alias, dict):
        for a, c in alias.items():
            if a and c:
                name2code.setdefault(str(a).strip().lower(), str(c).strip())
    return code2name, name2code


def resolve_code(term, name2code):
    """별칭/구명/영문명 → 코드. 6자리 숫자면 그대로. 없으면 None."""
    t = (term or "").strip()
    if re.fullmatch(r"\d{6}", t):
        return t
    return name2code.get(t.lower())


# ----------------- 한경 리포트 수집(코드 재검증) -----------------
def _rows(term, sdate, edate, pagenum=40):
    from bs4 import BeautifulSoup
    p = {"sdate": sdate, "edate": edate, "now_page": 1, "search_value": "BUSINESS",
         "report_type": "CO", "pagenum": pagenum, "business_code": "",
         "order_type": "", "search_text": term}
    r = requests.get(_HK_LIST, params=p, headers=_HK_H, timeout=25)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    return [tr for tr in (soup.select("table tbody tr") or soup.select("table tr"))
            if len(tr.find_all("td")) >= 6
            and tr.find_all("td")[0].get_text(strip=True)[:4].isdigit()]


def fetch_reports(code, name, sdate="2025-01-01", edate=None):
    """타깃종목 리포트 전량(목표가 0 포함). row_code==code 재검증. 날짜 오름차순."""
    if edate is None:
        edate = (date.today() + timedelta(days=1)).isoformat()
    terms = [name]
    if len(name) >= 3:
        terms += [name[:3], name[:2]]
    seen, out = set(), []
    for term in terms:
        if not term:
            continue
        try:
            rows = _rows(term, sdate, edate)
        except Exception as e:  # noqa: BLE001
            print(f"[analyst] {code} term='{term}' rows 실패: {type(e).__name__}",
                  file=sys.stderr)
            rows = []
        for tr in rows:
            tds = tr.find_all("td")
            rdate = tds[0].get_text(strip=True)
            title = tds[1].get_text(" ", strip=True)
            m = re.search(r"\((\d{6})\)", title)
            row_code = m.group(1) if m else ""
            if row_code != code:                # ★ 코드 재검증 — 타깃종목만
                continue
            tp = tds[2].get_text(strip=True).replace(",", "")
            opinion = tds[3].get_text(strip=True)
            broker = tds[5].get_text(strip=True)
            title_clean = re.sub(r"\(\d{6}\)\s*", "", title)
            # 제목 3중 반복 렌더 제거
            half = title_clean[:len(title_clean)//3] if len(title_clean) > 30 else title_clean
            # 제목 앞 중복 종목명 제거
            half = re.sub(r"^(" + re.escape(name) + r")+", "", half).strip()
            key = (rdate, broker, tp)
            if key in seen:
                continue
            seen.add(key)
            try:
                tp_val = int(tp)
            except Exception:
                tp_val = 0
            out.append({"date": rdate, "title": half[:45],
                        "target_price": tp_val, "opinion": opinion, "broker": broker})
        if out:            # 정식명으로 잡혔으면 프리픽스 재시도 생략
            break
        time.sleep(1.0)
    out.sort(key=lambda x: x["date"])
    return out


def _prices(code):
    """toss candles → [[YYYY-MM-DD, close_int], ...] 오름차순. 실패 시 []."""
    if _toss is None:
        return []
    try:
        df = _toss.candles(code, "1d")
    except Exception as e:  # noqa: BLE001
        print(f"[analyst] {code} candles 실패: {type(e).__name__}", file=sys.stderr)
        return []
    if df is None or len(df) == 0 or "close" not in df:
        return []
    out = []
    for d, c in df["close"].items():
        try:
            out.append([d.strftime("%Y-%m-%d"), round(float(c))])
        except Exception:
            continue
    return out


def build_payload(code, name):
    """단일 종목 payload(계약 준수). 실패 요소는 graceful(빈/None)."""
    prices = _prices(code)
    window_start = prices[0][0] if prices else "2025-01-01"
    current = prices[-1][1] if prices else None
    all_reports = fetch_reports(code, name)
    n_total = len(all_reports)
    # 목표가>0 & window_start 이후만, 날짜 오름차순
    tp_reps = [r for r in all_reports
               if r.get("target_price", 0) > 0 and r["date"] >= window_start]
    tp_reps.sort(key=lambda x: x["date"])
    tps = [r["target_price"] for r in tp_reps]
    avg_tp = round(sum(tps) / len(tps)) if tps else None
    return {
        "code": code,
        "name": name,
        "current": current,
        "avg_tp": avg_tp,
        "n_total": n_total,
        "n_tp": len(tp_reps),
        "window_start": window_start,
        "updated_at": date.today().isoformat(),
        "prices": prices,
        "reports": tp_reps,
        "disclaimer": _DISCLAIMER,
    }


# ----------------- 대상 코드 산출 -----------------
def target_codes(limit=None, extra=None):
    """top100.json 코드 ∪ 최근 랭킹 코드(로컬 서버 best-effort) ∪ extra."""
    codes = []
    seen = set()

    def add(c):
        c = (c or "").strip()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)

    snap = mc.load_json(config.DATA / "top100.json", default={}) or {}
    for it in (snap.get("items") or []):
        add(it.get("code"))
    # 최근 /api/ranking 등장 코드(서버 가동 중이면). 실패 무시.
    try:
        r = requests.get("http://127.0.0.1:8891/api/ranking?top_n=40", timeout=3)
        if r.ok:
            for it in (r.json().get("items") or []):
                add(it.get("stock_code") or it.get("code"))
    except Exception:
        pass
    for c in (extra or []):
        add(c)
    if limit:
        codes = codes[:limit]
    return codes


def save_one(payload):
    """단일 종목 upsert(Supabase) — 배치 종료 시 로컬 전량 저장은 별도."""
    row = dict(payload)
    row.pop("disclaimer", None)  # 로컬/응답에서 상수 부착(테이블엔 미저장)
    mc.upsert(_TABLE, [row], on_conflict="code")


def main(limit=15, sleep_sec=1.2, extra=None):
    code2name, name2code = build_name_map()
    codes = target_codes(limit=limit, extra=extra)
    if not codes:
        print("[analyst] 대상 코드 0 — top100.json 먼저 생성 필요", file=sys.stderr)
        return 1
    mc.ensure_table(_DDL)
    cache = mc.load_json(_CACHE_FILE, default={}) or {}
    if not isinstance(cache, dict):
        cache = {}
    ok = 0
    for code in codes:
        name = code2name.get(code, code)
        try:
            payload = build_payload(code, name)
            cache[code] = payload
            save_one(payload)
            ok += 1
            print(f"[analyst] {code} {name}: n_total={payload['n_total']} "
                  f"n_tp={payload['n_tp']} avg_tp={payload['avg_tp']} "
                  f"cur={payload['current']} prices={len(payload['prices'])} "
                  f"win={payload['window_start']}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"[analyst] {code} {name}: ERR {type(e).__name__} {e}",
                  file=sys.stderr)
        time.sleep(sleep_sec)
    # 로컬 전량 스냅샷 저장(폴백)
    mc.save_json(_CACHE_FILE, cache)
    print(f"[analyst] 완료: {ok}/{len(codes)}종목, 캐시 {len(cache)}종목 -> {_CACHE_FILE}")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--sleep", type=float, default=1.2)
    ap.add_argument("--codes", default="")  # 쉼표구분 추가 코드
    a = ap.parse_args()
    extra = [c.strip() for c in a.codes.split(",") if c.strip()]
    raise SystemExit(main(limit=a.limit, sleep_sec=a.sleep, extra=extra))
