# -*- coding: utf-8 -*-
"""시가총액 Top100 수집기 (KOSPI+KOSDAQ, 주 1회 배치).

소스: 네이버 모바일 증권 공개 랭킹 API(시가총액 상위). data.krx.or.kr 이 이
      환경에서 DNS 불가라 실측되는 공개 소스로 대체(marketValueRaw=원 단위 시총).
        GET https://m.stock.naver.com/api/stocks/marketValue/{KOSPI|KOSDAQ}?page=1&pageSize=100
      시장별 상위 100 을 받아 병합·정렬해 상위 100 을 산출한다(각 시장 top100 의
      합집합이 통합 top100 을 반드시 포함하므로 정확).

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

_URL = "https://m.stock.naver.com/api/stocks/marketValue/{market}?page=1&pageSize=100"
_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://m.stock.naver.com/"}
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


def _fetch_market(market):
    """단일 시장 시총 상위 리스트 반환. 실패 시 예외 전파(호출부에서 격리)."""
    r = requests.get(_URL.format(market=market), headers=_H, timeout=25)
    r.encoding = "utf-8"
    r.raise_for_status()
    j = r.json()
    out = []
    for s in (j.get("stocks") or []):
        code = str(s.get("itemCode") or "").strip()
        raw = s.get("marketValueRaw")
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
    return out


def collect():
    """KOSPI+KOSDAQ 시총 상위 병합 → 통합 상위 100 items 반환.
    부분 실패는 격리(그 시장만 스킵). 전부 실패면 빈 리스트."""
    merged = []
    for market in ("KOSPI", "KOSDAQ"):
        try:
            rows = _fetch_market(market)
            print(f"[top100] {market}: {len(rows)}건 취득", file=sys.stderr)
            merged.extend(rows)
        except Exception as e:  # noqa: BLE001
            print(f"[top100] {market} 취득 실패(건너뜀): {type(e).__name__} {e}",
                  file=sys.stderr)
        time.sleep(1.0)
    merged.sort(key=lambda x: x["market_cap"], reverse=True)
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
