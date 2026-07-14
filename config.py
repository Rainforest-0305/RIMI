# -*- coding: utf-8 -*-
"""공시알리미(gongsi-alert) 설정 로더.

- API 키는 기존 트레이딩 시스템의 .env 를 **읽기만** 한다 (수정 금지):
  C:\\Users\\urimk\\kis-trading\\.env  (DART_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
- 로컬 .env 가 있으면 그것으로 오버라이드(개발/분리 배포용).
- 트레이딩 코드/키는 절대 변경하지 않는다. 실계좌와 무관.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

# 1) 트레이딩 .env 를 읽기전용으로 로드 (키 재사용)
KIS_ENV = Path(r"C:\Users\urimk\kis-trading\.env")
if KIS_ENV.exists():
    load_dotenv(KIS_ENV)
# 2) 로컬 .env 오버라이드 (있으면 우선)
LOCAL_ENV = BASE / ".env"
if LOCAL_ENV.exists():
    load_dotenv(LOCAL_ENV, override=True)

DART_API_KEY = os.getenv("DART_API_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")

# 안전장치: 실유저 브로드캐스트 금지.
# 알림은 본인 테스트 채널로만 나간다. 테스트 채널 chat_id 를 로컬 .env 의
# GONGSI_TEST_CHAT_ID 로 지정하면 그 값을 쓰고, 없으면 트레이딩 채널 chat_id
# 를 폴백으로 쓰되(=본인 폰), 그래도 없으면 콘솔/파일로만 출력한다.
TEST_CHAT_ID = os.getenv("GONGSI_TEST_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")

# 상태 파일
WATCHLIST_FILE = BASE / "watchlist.json"
SEEN_FILE = DATA / "seen.json"          # 중복방지: 이미 처리한 rcept_no
ALERT_LOG = DATA / "alerts.log"         # 콘솔 폴백 겸 감사 로그
CORP_MAP_FILE = DATA / "corp_map.json"  # stock_code -> corp_code 캐시
# 과거 영향 벤치마크(strat-data 산출). 없으면 impact.py 가 "집계 중" 폴백.
IMPACT_BENCHMARK_FILE = DATA / "impact_benchmark.json"

# 폴링 주기(초). 노트북 부하/DART 유량 배려 — 기본 5분.
POLL_INTERVAL_SEC = int(os.getenv("GONGSI_POLL_SEC", "300"))

# seen.json 무한증가 방지 상한. rcept_no 는 YYYYMMDD+일련 → 사전식=시간순이므로
# 최신 SEEN_MAX 개만 보존(오래된 것부터 정리). 중복방지엔 최근분만 있으면 충분.
SEEN_MAX = int(os.getenv("GONGSI_SEEN_MAX", "5000"))
