# -*- coding: utf-8 -*-
"""a08ba34(build_impact_benchmark.py) 완료를 감지한 뒤 규모보정 파이프라인을
자동 실행: amounts(DART) -> aggregate -> merge. DART 경합/과부하 방지 목적.

완료 감지: python 프로세스 중 커맨드라인에 'build_impact_benchmark' 가 있으면
아직 진행중으로 본다. 사라지면 완료로 간주(=impact_benchmark.json 최종본 기록됨).
안전상 감지 후 20초 추가 대기(파일 flush).
"""
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).parent
LOG = BASE / "bench_cache" / "scale_run_after.log"


def log(m):
    line = f"[{datetime.now():%H:%M:%S}] {m}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def a08_running():
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*build_impact_benchmark*' } | "
             "Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=30)
        return int((out.stdout or "0").strip() or "0") > 0
    except Exception as e:
        log(f"proc check err: {repr(e)[:60]}")
        return True  # 불확실하면 계속 대기(안전측)


def run(step):
    log(f"--- run: scale_extract.py {step} ---")
    r = subprocess.run(
        [sys.executable, str(BASE / "scale_extract.py"), step],
        capture_output=True, text=True)
    tail = "\n".join((r.stdout or "").splitlines()[-25:])
    log(f"[{step} stdout tail]\n{tail}")
    if r.returncode != 0:
        log(f"[{step} STDERR]\n{(r.stderr or '')[-1500:]}")
    return r.returncode == 0


def main():
    LOG.write_text("", encoding="utf-8")
    log("대기 시작: a08ba34(build_impact_benchmark) 완료 감시")
    waited = 0
    while a08_running():
        time.sleep(60)
        waited += 60
        if waited % 300 == 0:
            log(f"  아직 진행중... {waited//60}분 경과")
    log("a08ba34 종료 감지 → 20초 flush 대기")
    time.sleep(20)
    if not run("amounts"):
        log("amounts 실패 — 중단"); return
    if not run("aggregate"):
        log("aggregate 실패 — 중단"); return
    if not run("merge"):
        log("merge 실패 — 중단"); return
    log("=== 규모보정 파이프라인 완료 ===")


if __name__ == "__main__":
    main()
