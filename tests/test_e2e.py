# -*- coding: utf-8 -*-
"""미리(MIRI) 공시앱 end-to-end 실측 검증 (requests 기반).

목적: app.py(FastAPI api)의 모든 서비스가 실제로 동작하는지 로컬 uvicorn 기동
후 실 HTTP 로 검증한다. 운영 파일은 읽기/실행만 하며, watchlist.json 은
테스트에서 임시 변경 후 원본 바이트로 **반드시 원복**한다.

실행:
    python tests/test_e2e.py              # 전체 검증(실 DART 네트워크 사용)
    python tests/test_e2e.py --port 8137  # 포트 지정

각 항목 PASS/FAIL 를 실측 출력하고, 실패 시 재현 정보를 남긴다.
종료코드: 0=전부 PASS, 1=하나라도 FAIL.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8")

# 테스트 대상 종목: 005380(현대차) = corp_map 에 존재하나 기본 워치리스트엔 없음.
TEST_ADD_CODE = "005380"
BAD_6DIGIT = "999999"       # 6자리지만 DART corp_map 에 없음 -> 404 기대
NON_6DIGIT = "12"           # 6자리 아님 -> 400 기대

# ---------------- 결과 수집 ----------------
RESULTS = []  # (name, ok, detail)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""))
    return ok


# ---------------- 서버 기동 ----------------
def free_port(preferred):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", preferred))
        s.close()
        return preferred
    except OSError:
        s.close()
        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.bind(("127.0.0.1", 0))
        p = s2.getsockname()[1]
        s2.close()
        return p


def start_server(port):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:api",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return proc


def wait_up(base, proc, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"uvicorn 프로세스 조기종료(code={proc.returncode}).\n{out}")
        try:
            r = requests.get(base + "/api/health", timeout=3)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ---------------- 1. API 엔드포인트 검증 ----------------
def test_health(base):
    r = requests.get(base + "/api/health", timeout=10)
    ok = r.status_code == 200
    d = r.json() if ok else {}
    check("health: HTTP 200", ok, f"status={r.status_code}")
    check("health: dart_key=true", d.get("dart_key") is True, f"dart_key={d.get('dart_key')}")
    check("health: watchlist_count 존재", isinstance(d.get("watchlist_count"), int),
          f"watchlist_count={d.get('watchlist_count')}")
    check("health: seen_count 존재", isinstance(d.get("seen_count"), int),
          f"seen_count={d.get('seen_count')}")
    check("health: benchmark_ready 필드", "benchmark_ready" in d,
          f"benchmark_ready={d.get('benchmark_ready')}")
    return d


def _alert_shape_ok(a):
    """알림 1건이 요구 필드를 갖췄는지."""
    if not isinstance(a.get("summary"), list) or len(a["summary"]) != 3:
        return False, f"summary 3줄 아님: {a.get('summary')}"
    if not isinstance(a.get("tags"), list) or not a["tags"]:
        return False, f"tags 비어있음: {a.get('tags')}"
    url = a.get("url", "")
    if "dart.fss.or.kr" not in url:
        return False, f"원문 URL 이상: {url}"
    if not isinstance(a.get("impact"), dict) or "status" not in a["impact"]:
        return False, f"impact 블록 없음: {a.get('impact')}"
    return True, ""


def test_alerts(base):
    r = requests.get(base + "/api/alerts", timeout=60)
    ok = r.status_code == 200
    check("alerts: HTTP 200", ok, f"status={r.status_code}")
    if not ok:
        return None
    d = r.json()
    alerts = d.get("alerts", [])
    errs = d.get("errors", [])
    check("alerts: errors=[] (DART 조회 성공)", errs == [], f"errors={errs}")
    check("alerts: 실 DART 공시 >=1건", len(alerts) >= 1, f"count={len(alerts)}")

    if alerts:
        bad = []
        for a in alerts[:30]:
            good, why = _alert_shape_ok(a)
            if not good:
                bad.append((a.get("rcept_no"), why))
        check("alerts: 각 항목 요약(3줄)·태그·원문URL·impact 필드 완비",
              not bad, "" if not bad else f"결함 {len(bad)}건 예: {bad[:2]}")
        # 과거영향 반영 여부 — 스키마/플래그 무관하게 impact 블록 형태로 판정.
        # (benchmark_ready 플래그는 신스키마 미반영 버그가 있어 신뢰하지 않는다;
        #  플래그 정합성은 tests/test_benchmark_integration.py 가 별도 검증.)
        statuses = [a.get("impact", {}).get("status") for a in alerts]
        ok_cnt = statuses.count("ok")
        pend_cnt = statuses.count("pending")
        well = all(s in ("ok", "pending") for s in statuses) and ok_cnt >= 1
        check("alerts: impact 블록 정상(ok/pending) & 통계 반영(ok>=1)", well,
              f"ok={ok_cnt} pending={pend_cnt} total={len(statuses)} "
              f"benchmark_ready={d.get('benchmark_ready')}(참고)")
    return d


def test_poll_cache(base):
    # poll = 강제 재조회(cached=false 기대)
    r1 = requests.post(base + "/api/poll", timeout=60)
    ok1 = r1.status_code == 200
    d1 = r1.json() if ok1 else {}
    check("poll: HTTP 200 & cached=false(캐시 무효화)",
          ok1 and d1.get("cached") is False, f"status={r1.status_code} cached={d1.get('cached')}")
    # 직후 alerts = TTL(60s) 내이므로 캐시 히트(cached=true) 기대
    r2 = requests.get(base + "/api/alerts", timeout=30)
    d2 = r2.json() if r2.status_code == 200 else {}
    check("poll->alerts: 직후 조회는 캐시 히트(cached=true)",
          d2.get("cached") is True, f"cached={d2.get('cached')} (캐시 TTL 동작 증명)")


def _current_watchlist(base):
    return requests.get(base + "/api/watchlist", timeout=10).json()


def test_watchlist(base):
    before = _current_watchlist(base)
    n0 = len(before.get("stocks", []))

    # (a) 잘못된 형식(6자리 아님) -> 400
    r = requests.post(base + "/api/watchlist", json={"stock_code": NON_6DIGIT}, timeout=15)
    check("watchlist: 비6자리 코드 -> 400", r.status_code == 400, f"status={r.status_code}")

    # (b) 6자리지만 DART 미존재 -> 404
    r = requests.post(base + "/api/watchlist", json={"stock_code": BAD_6DIGIT}, timeout=30)
    check("watchlist: 존재하지 않는 6자리 코드 -> 404", r.status_code == 404,
          f"status={r.status_code} body={r.text[:120]}")

    # (c) 정상 추가 -> 200, 이름 자동해석, 목록 +1
    r = requests.post(base + "/api/watchlist", json={"stock_code": TEST_ADD_CODE}, timeout=60)
    ok_add = r.status_code == 200
    d = r.json() if ok_add else {}
    added = next((s for s in d.get("stocks", []) if s.get("stock_code") == TEST_ADD_CODE), None)
    check("watchlist: 임의 코스피 코드 추가 -> 200 & 목록 +1",
          ok_add and added is not None and len(d.get("stocks", [])) == n0 + 1,
          f"status={r.status_code} added={added}")
    name_ok = bool(added and added.get("name"))
    check("watchlist: 이름 자동해석(비어있지 않음)", name_ok,
          f"name='{(added or {}).get('name')}' (실명 해석 실패 시 코드로 폴백)")

    # (d) 중복 추가 -> 409
    r = requests.post(base + "/api/watchlist", json={"stock_code": TEST_ADD_CODE}, timeout=30)
    check("watchlist: 중복 추가 -> 409", r.status_code == 409, f"status={r.status_code}")

    # (e) 삭제 -> 200 & 원복(목록 -1)
    r = requests.delete(base + "/api/watchlist/" + TEST_ADD_CODE, timeout=15)
    ok_del = r.status_code == 200
    d = r.json() if ok_del else {}
    restored = all(s.get("stock_code") != TEST_ADD_CODE for s in d.get("stocks", []))
    check("watchlist: 삭제 -> 200 & 목록 원복",
          ok_del and restored and len(d.get("stocks", [])) == n0,
          f"status={r.status_code} n0={n0} now={len(d.get('stocks', []))}")

    # (f) 미등록 삭제 -> 404
    r = requests.delete(base + "/api/watchlist/" + BAD_6DIGIT, timeout=15)
    check("watchlist: 미등록 코드 삭제 -> 404", r.status_code == 404, f"status={r.status_code}")


def test_static(base):
    r = requests.get(base + "/", timeout=15)
    ok = r.status_code == 200 and ("미리" in r.text or "MIRI" in r.text)
    check("static: / 페이지 서빙(index.html)", ok,
          f"status={r.status_code} len={len(r.text)}")
    for path in ("/manifest.json", "/sw.js", "/icon-192.png"):
        rr = requests.get(base + path, timeout=15)
        check(f"static: {path} 서빙", rr.status_code == 200, f"status={rr.status_code}")


# ---------------- 2. 규제 카피(투자권유성 표현) 점검 ----------------
def test_regulatory_copy():
    """정적 문구(index.html) 및 백엔드 요약 스텁에서 투자권유성 표현을 grep.
    금칙 토큰이 나오면 '부정(면책) 문맥'인지 검사해, 긍정적 권유만 위반으로 판정."""
    targets = [ROOT / "web" / "index.html", ROOT / "summarize.py"]
    forbidden = ["추천", "매수", "매도", "사세요", "파세요", "오를 것", "오를것",
                 "급등", "수익보장", "수익을 보장", "예측", "강력추천", "지금 사"]
    # 부정/면책 표지. 금칙 토큰이 이 표지들과 같은 '문맥 창'(±2줄) 안에 있으면
    # 정당한 면책/부정 문맥으로 간주(투자권유가 아님).
    negations = ["아닙니다", "아니며", "아니라", "아닌", "가 아", "배제", "권유가 아",
                 "않습니다", "않으며", "않는", "않은", "보장하지 않", "절대 없", "없음",
                 "목적이며"]

    violations = []
    occurrences = []
    for f in targets:
        if not f.exists():
            continue
        lines = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            for tok in forbidden:
                if tok in line:
                    # ±2줄 문맥 창으로 면책 여부 판정(면책 문장이 여러 줄에 걸침)
                    ctx = "".join(lines[max(0, i - 3):min(len(lines), i + 2)])
                    negated = any(neg in ctx for neg in negations)
                    occurrences.append((f.name, i, tok, negated, line.strip()[:90]))
                    if not negated:
                        violations.append((f.name, i, tok, line.strip()[:90]))

    check("regcopy: 투자권유성 표현(긍정 문맥) 없음",
          not violations,
          "" if not violations else f"위반 {len(violations)}건: {violations[:3]}")

    # 사실 프레이밍 '과거 N건 중 M건' 존재
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    fact_frame = ("과거 유사공시" in html and "건" in html and "상승" in html
                  and "하락" in html)
    check("regcopy: '과거 N건 중 M건 상승/하락' 사실 프레이밍 존재", fact_frame,
          "factline/probbar 확인" if fact_frame else "사실 프레이밍 미발견")

    # 면책(disclaimer) 존재 — 표기 띄어쓰기 변형에 견고하도록 공백무시 매칭.
    norm = html.replace(" ", "")
    disclaimer_ok = ("투자권유가아닙니다" in norm
                     and ("보장하지않" in norm or "미래" in norm))
    check("regcopy: 면책(투자권유 아님·미래 미보장) 문구 존재", disclaimer_ok,
          "" if disclaimer_ok else "면책 문구 미발견")

    if occurrences:
        print("    (참고) 금칙 토큰 출현 및 면책여부:")
        for fn, ln, tok, neg, txt in occurrences:
            print(f"      {fn}:{ln} '{tok}' 면책={neg} | {txt}")


# ---------------- 3. 모바일/PWA/테마 파일 확인 ----------------
def test_pwa_responsive():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    check("pwa: viewport(모바일 반응형) 메타 존재",
          "viewport" in html and "width=device-width" in html)
    check("pwa: 라이트/다크 테마 대응(color-scheme/prefers-color-scheme)",
          "color-scheme" in html and "prefers-color-scheme" in html)
    check("pwa: manifest 링크 & 파일 존재",
          'rel="manifest"' in html and (ROOT / "web" / "manifest.json").exists())
    check("pwa: service worker 등록 & sw.js 존재",
          "serviceWorker" in html and (ROOT / "web" / "sw.js").exists())
    # manifest 필수 키
    try:
        man = json.loads((ROOT / "web" / "manifest.json").read_text(encoding="utf-8"))
        need = all(k in man for k in ("name", "start_url", "display", "icons"))
        check("pwa: manifest 필수 키(name/start_url/display/icons)", need,
              f"display={man.get('display')} icons={len(man.get('icons', []))}")
    except Exception as e:
        check("pwa: manifest 파싱", False, str(e))
    for ic in ("icon-192.png", "icon-512.png", "icon-maskable-512.png"):
        check(f"pwa: 아이콘 {ic} 존재", (ROOT / "web" / ic).exists())


def test_search(base):
    """/api/search 로컬인덱스 검색 + 시장칸(KOSPI/KOSDAQ) 실측.
    빌드타임 KRX 매핑으로 corp_index.json 의 market 이 실제 시장명으로 채워졌는지 검증."""
    # 1) 삼성전자(005930) -> KOSPI 로 뜨는가
    r = requests.get(f"{base}/api/search", params={"q": "삼성전자"}, timeout=10)
    check("search: 삼성전자 HTTP 200", r.status_code == 200, f"status={r.status_code}")
    res = r.json().get("results", [])
    sam = next((x for x in res if x.get("code") == "005930"), None)
    check("search: 삼성전자(005930) 결과 존재", sam is not None)
    check("search: 삼성전자 market=KOSPI (시장칸 실채움)",
          bool(sam) and sam.get("market") == "KOSPI",
          f"market={sam.get('market') if sam else None}")
    # 2) 코스닥 대표종목(에코프로비엠 247540) -> KOSDAQ
    r = requests.get(f"{base}/api/search", params={"q": "247540"}, timeout=10)
    res = r.json().get("results", [])
    eco = next((x for x in res if x.get("code") == "247540"), None)
    check("search: 에코프로비엠(247540) market=KOSDAQ",
          bool(eco) and eco.get("market") == "KOSDAQ",
          f"market={eco.get('market') if eco else None}")
    # 3) 시장칸이 전부 '-' 는 아니어야 한다(회귀 방지: KRX 매핑 유실 감지)
    r = requests.get(f"{base}/api/search", params={"q": "삼성"}, timeout=10)
    res = r.json().get("results", [])
    filled = [x for x in res if x.get("market") not in ("-", "", None)]
    check("search: 시장칸 실채움 결과 다수(>=1, 전부 '-' 아님)",
          len(filled) >= 1, f"filled={len(filled)}/{len(res)}")
    # 4) 빈 q -> 200 & count 0 (graceful)
    r = requests.get(f"{base}/api/search", params={"q": ""}, timeout=10)
    d = r.json()
    check("search: 빈 q -> 200 & count 0",
          r.status_code == 200 and d.get("count") == 0, f"count={d.get('count')}")


# ---------------- 러너 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8137)
    args = ap.parse_args()

    # 정적/규제 검사는 서버 없이도 가능 → 먼저 실행
    print("=== [정적] 규제 카피 / PWA / 반응형 ===")
    test_regulatory_copy()
    test_pwa_responsive()

    # watchlist.json 원본 백업(테스트가 변경 → finally 에서 원복)
    wl_path = ROOT / "watchlist.json"
    wl_backup = wl_path.read_bytes() if wl_path.exists() else None

    port = free_port(args.port)
    base = f"http://127.0.0.1:{port}"
    print(f"\n=== [E2E] uvicorn app:api 기동 (port {port}) ===")
    proc = start_server(port)
    try:
        if not wait_up(base, proc):
            check("server: uvicorn 기동", False, "40초 내 health 응답 없음")
        else:
            check("server: uvicorn 기동 & health 응답", True, base)
            print("\n--- /api/health ---")
            test_health(base)
            print("\n--- /api/alerts (실 DART) ---")
            test_alerts(base)
            print("\n--- /api/poll (캐시 무효화) ---")
            test_poll_cache(base)
            print("\n--- /api/watchlist CRUD ---")
            test_watchlist(base)
            print("\n--- 정적 프론트 서빙 ---")
            test_static(base)
            print("--- /api/search (시장칸) ---")
            test_search(base)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        # 운영 파일 원복(반드시)
        if wl_backup is not None:
            wl_path.write_bytes(wl_backup)
            print("\n[cleanup] watchlist.json 원본 바이트로 원복 완료.")

    # 요약표
    print("\n================ 결과 요약 ================")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"------------------------------------------")
    print(f"  {passed}/{total} PASS, {total - passed} FAIL")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
