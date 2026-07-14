# -*- coding: utf-8 -*-
"""수정된 실 벤치마크(impact_benchmark.json)가 앱에 제대로 통합됐는지 E2E 재검증.

배경: strat-data 가 루트 `impact_benchmark.json` 에 실측 CAR 벤치마크(광의시장
동일가중 EW 보정)를 생성. 앱(impact.py)이 이를 읽어 /api/alerts 에 반영하는지,
값이 실데이터인지(시드 아님), 규제 프레이밍이 유지되는지 실측한다.

운영 파일은 읽기/실행만. 새 테스트 파일. 실 DART 1회 조회.

⚠️ 스키마 동반수정 규칙(중요):
    impact.py 의 window 출력 스키마가 진화하면 이 테스트도 같은 커밋에서
    맞춰라. impact.py 실동작이 정답이다(테스트만 손댄다).
    - 폐지: `excess`(단일 초과등락 필드). 참조 금지.
    - 현행 window 필드: raw_avg / raw_med / car_avg /
      raw_up_prob / car_up_prob / up_prob / down_prob / car_down_prob / n.
    - 구 `excess`(시장보정 CAR) 의 대체 = `car_avg`(초과등락 평균 = raw-market).
      루트 impact_benchmark.json 의 <유형>.{d|w|m}.car_avg 와 1:1 일치해야 한다.
    - 리더는 car_med(중앙값)를 노출하지 않는다 → 검증에 쓰지 말 것.
    TODO: window 필드 추가/폐지 시 아래 _WIN_FIELD_CHECKS 와 대조 로직 갱신.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    return ok


def free_port(pref=8151):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", pref)); s.close(); return pref
    except OSError:
        s.close()
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.bind(("127.0.0.1", 0)); p = s2.getsockname()[1]; s2.close(); return p


def _load_root_bench():
    f = ROOT / "impact_benchmark.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def test_reader_reads_real_data():
    """impact.py 가 (경로/스키마 상관없이) 실 루트 벤치마크를 읽는지 직접 검증."""
    import importlib, config, impact
    importlib.reload(config); importlib.reload(impact)
    bench = impact.load_benchmark()
    root = _load_root_bench()

    # 실데이터 식별: strat 스키마는 top-level 유형 + _meta.method 존재
    is_strat = ("method" in (bench.get("_meta") or {})) and ("자사주" in bench)
    check("reader: 앱이 실측(strat) 벤치마크를 로드(시드 아님)", is_strat,
          f"_meta.source={ (bench.get('_meta') or {}).get('source')} "
          f"has_method={'method' in (bench.get('_meta') or {})}")

    # impact_for_tags 가 ok 를 반환하고, 신스키마 window 필드가 온전한지 +
    # 대표 초과등락(car_avg)·원자료(raw_avg)가 루트 실데이터와 1:1 일치하는지.
    _WIN_FIELD_CHECKS = ("raw_avg", "raw_med", "car_avg",
                         "raw_up_prob", "car_up_prob", "up_prob", "n")
    for tag in ["자사주", "전환사채", "최대주주변경"]:
        r = impact.impact_for_tags([tag])
        ok = r.get("status") == "ok"
        m1 = (r.get("windows") or {}).get("m1", {})
        # 폐지된 excess 미노출 + 신스키마 필드 존재 확인(하드크래시 방지)
        no_excess = "excess" not in m1
        has_fields = all(k in m1 for k in _WIN_FIELD_CHECKS)
        rm = root.get(tag, {}).get("m", {})
        car_avg, raw_avg = rm.get("car_avg"), rm.get("raw_avg")
        # 구 excess 대체 = car_avg(시장보정 초과등락 평균). 원자료 raw_avg 도 대조.
        match = ok and no_excess and has_fields and \
            (m1.get("car_avg") == car_avg) and (m1.get("raw_avg") == raw_avg)
        check(f"reader: {tag} 1개월 car_avg/raw_avg 가 루트 실데이터와 일치(신스키마)",
              match,
              f"앱 car_avg={m1.get('car_avg')} raw_avg={m1.get('raw_avg')} | "
              f"루트 car_avg={car_avg} raw_avg={raw_avg} | "
              f"no_excess={no_excess} has_fields={has_fields}")


def test_live_api(base):
    h = requests.get(base + "/api/health", timeout=10).json()
    a = requests.get(base + "/api/alerts", timeout=60).json()

    src_h = ""  # health 에는 source 없음
    src_a = a.get("benchmark_source", None)
    check("live: /api/alerts benchmark_source 가 'seed-placeholder' 가 아님",
          src_a != "seed-placeholder",
          f"benchmark_source='{str(src_a)[:40]}' (실 strat 파일은 _meta.method/generated_at 노출)")

    alerts = a.get("alerts", [])
    ok_imp = [x for x in alerts if x.get("impact", {}).get("status") == "ok"]
    pend = [x for x in alerts if x.get("impact", {}).get("status") == "pending"]
    ratio = (len(ok_imp) / len(alerts)) if alerts else 0
    check("live: impact.status=ok 비율 >=50% (실데이터 매핑)",
          ratio >= 0.5, f"ok={len(ok_imp)} pending={len(pend)} total={len(alerts)} "
                        f"({ratio*100:.0f}%)")

    # benchmark_ready 플래그 정합성: ok 임팩트가 있는데 ready=False 면 앱 플래그 버그
    br = a.get("benchmark_ready")
    consistent = (br is True) if ok_imp else True
    check("live: benchmark_ready 플래그가 실데이터 상태와 정합",
          consistent,
          f"benchmark_ready={br} 인데 ok임팩트 {len(ok_imp)}건 존재 "
          f"(False면 app.py 플래그가 신스키마 미반영 버그)")

    # 실 카드에서 impact 값이 루트 실데이터와 일치하는지 표본 확인
    root = _load_root_bench()
    sample = None
    for x in ok_imp:
        qt = x["impact"].get("query_tag") or (x.get("tags") or [None])[0]
        mt = x["impact"].get("matched_tag")
        key = mt if mt in root else qt
        if key in root:
            sample = (x, key); break
    if sample:
        x, key = sample
        m1 = x["impact"]["windows"]["m1"]
        shown = m1.get("car_avg")          # 신스키마: 구 excess 대체(시장보정 초과등락 평균)
        car_avg = root[key]["m"]["car_avg"]
        check(f"live: 카드 impact car_avg 가 루트 실데이터와 일치 [{key}]",
              ("excess" not in m1) and (shown == car_avg),
              f"카드표시 car_avg={shown} 루트car_avg={car_avg} corp={x.get('corp_name')}")
    else:
        check("live: ok 카드 표본으로 실데이터 대조", False, "대조할 ok 카드 없음")
    return h, a


def test_regulatory_still_holds():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    # 예측/추천 긍정표현 부재(면책 문맥 제외) — 간이 재확인
    bad = []
    for i, line in enumerate(html.splitlines(), 1):
        for tok in ["추천", "매수하", "매도하", "오를 것", "급등", "수익보장"]:
            if tok in line and "아니" not in line and "않" not in line and "배제" not in line:
                bad.append((i, tok, line.strip()[:70]))
    check("regcopy: 예측·추천 긍정표현 부재 유지", not bad, str(bad[:2]) if bad else "")
    check("regcopy: '과거 N건 중 M건 상승/하락' 사실 프레이밍 유지",
          "과거 유사공시" in html and "상승" in html and "하락" in html)
    # 면책 문구는 카피 진화(띄어쓰기 등)에 견고하게 판정: 공백 제거 후 부분매칭.
    # (라이브 HTML: "미래를 보장하지 않습니다. 정보 제공이며 투자 권유가 아닙니다.")
    flat = "".join(html.split())
    check("regcopy: 면책(투자권유 아님·미래 미보장) 유지",
          ("투자권유가아닙니다" in flat) and ("보장하지않" in flat),
          "면책 문구 부재" if not (("투자권유가아닙니다" in flat) and ("보장하지않" in flat)) else "")
    # '예시(집계 전)' 배너 안전조건: 실데이터에 노출되면 안 된다.
    #  - 배너가 아예 제거됨(실데이터 전용 앱) → 안전(권장)
    #  - 배너가 있으면 benchmark_source==='seed-placeholder' 로 게이팅돼야 함
    #  - 무조건 노출(present & 미게이팅)만 FAIL.
    import re
    present = "예시(집계 완료 전)" in html
    gated = present and bool(re.search(r"benchmark_source==='seed-placeholder'", html))
    unconditional = present and not gated
    check("banner: '예시(집계 전)' 배너가 실데이터에 미노출(제거 or seed 게이팅)",
          not unconditional,
          "배너 미존재(실데이터 전용 — 안전)" if not present
          else ("seed-placeholder 게이팅됨" if gated else "무조건 노출 구조 — 위험"))


def main():
    print("=== [정적/리더] 실 벤치마크 통합 검증 ===")
    test_reader_reads_real_data()
    print()
    test_regulatory_still_holds()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"\n=== [LIVE] uvicorn app:api (port {port}) ===")
    env = dict(os.environ); env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:api", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        t0 = time.time(); up = False
        while time.time() - t0 < 40:
            if proc.poll() is not None:
                print(proc.stdout.read()); break
            try:
                if requests.get(base + "/api/health", timeout=3).status_code == 200:
                    up = True; break
            except Exception:
                time.sleep(0.5)
        check("server: uvicorn 기동", up)
        if up:
            h, a = test_live_api(base)
            print("\n  [live 스냅샷] health.benchmark_ready =", h.get("benchmark_ready"),
                  "| alerts.benchmark_ready =", a.get("benchmark_ready"),
                  "| benchmark_source =", repr(a.get("benchmark_source")))
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except Exception: proc.kill()

    print("\n================ 결과 요약 ================")
    p = sum(1 for _, ok, _ in RESULTS if ok)
    for n, ok, d in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print(f"  {p}/{len(RESULTS)} PASS, {len(RESULTS)-p} FAIL")
    sys.exit(0 if p == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
