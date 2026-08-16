# -*- coding: utf-8 -*-
"""시가총액 Top100 수집기 (KOSPI+KOSDAQ, 주 1회 배치).

소스: 네이버 모바일 증권 공개 랭킹 API(시가총액 상위). data.krx.or.kr 이 이
      환경에서 DNS 불가라 실측되는 공개 소스로 대체(marketValueRaw=원 단위 시총).
        GET https://m.stock.naver.com/api/stocks/marketValue/{KOSPI|KOSDAQ}?page=N&pageSize=100
      시장별 상위 200(2페이지)을 받아 ETF 를 제외하고 병합·정렬해 상위 100 을
      산출한다(각 시장 상위 200 의 합집합이 통합 top100 을 반드시 포함).

ETF 제외(President 지시 2026-08-16): 응답의 stockEndType 이 'etf' 인 행을 뺀다.
      제외 후에도 100칸을 채우기 위해 시장당 200행을 받는다(_PAGES_PER_MARKET).

저장: Supabase 테이블 market_cap_top100(전량 교체) + 로컬 data/top100.json 폴백.

모든 네트워크/파싱 실패는 격리한다(수집 실패해도 기존 스냅샷 유지, 크래시 없음)."""
import sys
import time
from datetime import date

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import requests

import config
import miri_cache as mc

_URL = ("https://m.stock.naver.com/api/stocks/marketValue/{market}"
        "?page={page}&pageSize={size}")
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://m.stock.naver.com/"}

# pageSize 상한은 100 이다(실측 2026-08-16: pageSize=200 → HTTP 400, 빈 본문).
# 더 받으려면 page 를 넘겨야 한다.
_PAGE_SIZE = 100
# 시장별 수집 페이지 수. ETF 를 «제외한 뒤» 통합 100칸을 채워야 하므로 100행으로는
# 부족하다. 실측 2026-08-16(원본 400행 기준):
#   - 통합 non-ETF 100위 = 028300 HLB(KOSDAQ) 5.75조
#   - 그 시총 이상인 원본 행: KOSPI 108행 / KOSDAQ 6행
#   - 즉 KOSPI 는 100행으로는 «8행 모자란다». 2페이지(200행)면 92행 여유.
#   - 200행 안의 non-ETF: KOSPI 160 + KOSDAQ 200 = 360 ≫ 100 이라 구조적으로 충분.
# 요청은 시장당 2회(총 4회) — 기존 2회 대비 2회 증가, 주 1회 배치라 부담 무시 가능.
_PAGES_PER_MARKET = 2

# ETF 판별: 네이버 응답이 이미 내려주는 stockEndType 필드를 쓴다(실측 2026-08-16:
# KOSPI 200행 = stock 160 / etf 40, KOSDAQ 200행 = stock 200 / etf 0. 혼재 없음).
# 화이트리스트(=stock 만 채택)로 둔 이유: 블랙리스트('etf' 만 제외)로 두면 앞으로
# 새 타입 문자열(etn 등)이 생겼을 때 조용히 통과한다.
# 화이트리스트가 소스 스키마 변경으로 «전량 탈락」하면 수집 0건이 되는데, main() 이
# 0건일 때 저장을 스킵하므로 기존 스냅샷이 보존된다(fail-safe). 추가로 _fetch_market
# 이 '행은 있는데 전량 탈락' 상황을 예외로 올려 시장 단위로 격리한다.
_ALLOWED_END_TYPE = "stock"
_TOP100_FILE = config.DATA / "top100.json"
_TABLE = "market_cap_top100"

_DDL = """
CREATE TABLE IF NOT EXISTS market_cap_top100 (
  code text PRIMARY KEY,
  rank int,
  name text,
  market text,
  market_cap bigint,
  cap_label text,
  updated_at text
);"""


def cap_label(won):
    """원 정수 시총 → 한국식 조/억 라벨. 예: 467조 / 1,522조 9,556억 / 8,320억."""
    try:
        won = int(won)
    except Exception:
        return ""
    if won <= 0:
        return ""
    jo = won // 10**12
    eok = round((won % 10**12) / 10**8)
    if eok >= 10000:  # 반올림 자리올림 방지
        jo += 1
        eok = 0
    if jo >= 1:
        return f"{jo:,}조" if eok == 0 else f"{jo:,}조 {eok:,}억"
    return f"{eok:,}억"


def _fetch_page(market, page):
    """단일 페이지 원본 stocks 배열. 실패 시 예외 전파."""
    r = requests.get(_URL.format(market=market, page=page, size=_PAGE_SIZE),
                     headers=_H, timeout=25)
    r.encoding = "utf-8"
    r.raise_for_status()
    return r.json().get("stocks") or []


def _fetch_market(market, pages=None):
    """단일 시장 시총 상위 리스트(ETF 제외) 반환. 실패 시 예외 전파(호출부에서 격리).

    반환: (items, stats). stats = {"raw": 원본행, "etf": 제외행, "other": 기타탈락}.
    """
    pages = pages or _PAGES_PER_MARKET
    raw_rows = []
    for pg in range(1, pages + 1):
        rows = _fetch_page(market, pg)
        raw_rows.extend(rows)
        if len(rows) < _PAGE_SIZE:      # 마지막 페이지
            break
        if pg < pages:
            time.sleep(1.0)
    out, n_etf, n_other = [], 0, 0
    for s in raw_rows:
        code = str(s.get("itemCode") or "").strip()
        raw = s.get("marketValueRaw")
        end_type = str(s.get("stockEndType") or "").strip().lower()
        if end_type != _ALLOWED_END_TYPE:
            # ★ETF 제외(President 지시). etf 외의 값도 여기서 걸린다.
            if end_type == "etf":
                n_etf += 1
            else:
                n_other += 1
            continue
        if not code or raw in (None, ""):
            continue
        try:
            cap = int(float(raw))
        except Exception:
            continue
        if cap <= 0:
            continue
        # sosok: "0"=KOSPI, "1"=KOSDAQ. 안전하게 인자 market 을 신뢰(요청한 시장).
        out.append({
            "code": code,
            "name": str(s.get("stockName") or "").strip(),
            "market": market,
            "market_cap": cap,
        })
    # 행은 받았는데 전량 탈락 = stockEndType 스키마 변경 의심. 조용히 0건으로
    # 넘기면 그 시장이 통째로 사라진 채 저장된다 — 예외로 올려 시장 단위 격리.
    if raw_rows and not out:
        raise RuntimeError(
            f"{market}: 원본 {len(raw_rows)}행 전량 탈락(stockEndType 스키마 변경 의심)")
    return out, {"raw": len(raw_rows), "etf": n_etf, "other": n_other}


def collect():
    """KOSPI+KOSDAQ 시총 상위 병합 → 통합 상위 100 items 반환.
    부분 실패는 격리(그 시장만 스킵). 전부 실패면 빈 리스트."""
    merged = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            rows, st = _fetch_market(market)
            print(f"[top100] {market}: 원본 {st['raw']}행 → ETF 제외 {st['etf']}건"
                  f"(기타 제외 {st['other']}건) → {len(rows)}건 취득", file=sys.stderr)
            merged.extend(rows)
        except Exception as e:  # noqa: BLE001
            print(f"[top100] {market} 취득 실패(건너뜀): {type(e).__name__} {e}",
                  file=sys.stderr)
        time.sleep(1.0)
    merged.sort(key=lambda x: x["market_cap"], reverse=True)
    if merged and len(merged) < 100:
        # ETF 제외 후 100칸이 안 채워지면 _PAGES_PER_MARKET 을 늘려야 한다.
        # 조용히 90칸짜리 top100 을 저장하지 않도록 반드시 표면화한다.
        print(f"[top100] 경고: ETF 제외 후 {len(merged)}건 < 100 — "
              f"_PAGES_PER_MARKET({_PAGES_PER_MARKET}) 상향 필요", file=sys.stderr)
    top = merged[:100]
    updated = date.today().isoformat()
    items = []
    for i, it in enumerate(top, start=1):
        items.append({
            "rank": i,
            "code": it["code"],
            "name": it["name"],
            "market": it["market"],
            "market_cap": it["market_cap"],
            "cap_label": cap_label(it["market_cap"]),
            "updated_at": updated,
        })
    return items, updated


def save(items, updated):
    """Supabase 전량 교체 + 로컬 JSON 스냅샷. Supabase 실패해도 로컬은 반드시 저장."""
    snapshot = {"updated_at": updated, "count": len(items), "items": items}
    ok_local = mc.save_json(_TOP100_FILE, snapshot)
    ok_sb = False
    if items:
        mc.ensure_table(_DDL)
        ok_sb = mc.replace_all(_TABLE, items, on_conflict="code")
    print(f"[top100] 저장: 로컬={ok_local} supabase={ok_sb} count={len(items)}",
          file=sys.stderr)
    return ok_local, ok_sb


def main():
    items, updated = collect()
    if not items:
        print("[top100] 수집 0건 — 기존 스냅샷 유지(저장 스킵)", file=sys.stderr)
        return 1
    save(items, updated)
    # 요약 실측 출력
    print(f"[top100] updated_at={updated} count={len(items)}")
    for it in items[:5]:
        print(f"  #{it['rank']} {it['code']} {it['name']} {it['market']} "
              f"{it['cap_label']} ({it['market_cap']:,}원)")
    mk = {}
    for it in items:
        mk[it["market"]] = mk.get(it["market"], 0) + 1
    print(f"[top100] 시장분포 {mk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
