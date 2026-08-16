# -*- coding: utf-8 -*-
"""공시알리미(gongsi-alert) 설정 로더.

- API 키는 기존 트레이딩 시스템의 .env 를 **읽기만** 한다 (수정 금지):
  C:\\Users\\urimk\\kis-trading\\.env  (DART_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
- 로컬 .env 가 있으면 그것으로 오버라이드(개발/분리 배포용).
- 트레이딩 코드/키는 절대 변경하지 않는다. 실계좌와 무관.
"""
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

BASE = Path(__file__).parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

# 1) 트레이딩 .env 를 읽기전용으로 로드 (키 재사용)
#
# ★2026-08-16 R3 — 이 줄은 «세 가지 사고»의 뿌리다. 일원화 설계 진행 중
#   (features/_ops/R3_CONFIG_UNIFICATION.md). 아래는 그 1단계(격리 스위치)다.
#   (a) Render 는 리눅스 컨테이너라 이 경로가 «없다» → TELEGRAM 키 미주입 → 경보 배달 0건
#   (b) MIRI 프로세스가 «실계좌 자격증명»을 통째로 흡수한다(경계 붕괴)
#   (c) ★테스트 격리를 깨뜨린다 — 실측: 자식 프로세스에서 TELEGRAM_* 를 전부 비우고
#       띄웠는데 이 줄이 kis-trading\.env 에서 되살려, 「발송 불가」를 검증하려던
#       양성대조가 «실제 텔레그램 2건을 발송»했다(2026-08-16, 운영자 개인 DM).
#       env 를 비우는 것만으로는 외부 발송을 막을 수 없다는 뜻이다.
#
# ── 스위치: MIRI_ALLOW_KIS_ENV (ops-vm 안 · Partner 승인 극성. 단일 이름) ──
#   fail-safe 는 «차단»이다. "1/true/yes" 일 때«만» kis-trading\.env 를 읽는다.
#   미설정(Render·VM) = 차단. 「경로가 없어서 우연히 차단」을 「정책으로 명시 차단」으로.
#   ★같은 값에 두 이름을 두지 않는다 — 초안의 GONGSI_ISOLATED_ENV 는 폐기했다.
#
#   _DEFAULT_ALLOW_KIS 는 «전환 손잡이»다. 현재 True = 노트북 현행 동작 보존.
#   Partner 게이트 승인 시 이 한 줄을 False 로 바꾸면 전면 차단(1줄, 되돌림 가능).
#   지금 곧바로 False 로 하면 노트북의 DART/TOSS/TELEGRAM 이 즉시 끊긴다 — 그게 다음 사고다.
_DEFAULT_ALLOW_KIS = False       # ← Partner U1 집행 2026-08-16: 차단 전환 완료
                                 #   선행 U2 = DART_API_KEY·TOSS_APP_KEY·TOSS_SECRET 를
                                 #   gongsi-alert/.env 로 «복사»(이동 아님, 트레이딩 무영향).
                                 #   KRX_ID/KRX_PW 는 «복사하지 않았다» — 코드 미사용(주석 1건뿐)
                                 #   + data.krx 약관 미결이라 의도적으로 끊었다.
                                 #   되돌리려면 이 한 줄을 True 로. 임의 복원 금지(Partner 게이트).
_allow_raw = os.getenv("MIRI_ALLOW_KIS_ENV", "").strip().lower()
ALLOW_KIS_ENV = (_allow_raw in ("1", "true", "yes", "on")) if _allow_raw \
    else _DEFAULT_ALLOW_KIS
KIS_ENV = Path(os.getenv("MIRI_KIS_ENV_PATH", r"C:\Users\urimk\kis-trading\.env"))
LOCAL_ENV = BASE / ".env"

# ── 화이트리스트: «무엇을» 읽느냐 (플래그보다 «먼저»다 — Partner 구분) ──────────
#   플래그(ALLOW_KIS_ENV)는 「읽느냐 마느냐」. 화이트리스트는 「무엇을 읽느냐」.
#   load_dotenv 는 선택 로드가 없어 파일 «전체»를 넣는다. 그래서 dotenv_values 로
#   «읽기만» 하고 아래 목록에 있는 키만 os.environ 에 선별 주입한다.
#
#   ★근거 [실측 2026-08-16] kis-trading\.env 19종 × MIRI 코드 전수 스캔(ops_env_scan.py).
#     축: os.getenv 정적/동적 · os.environ[] · .get() · 전체순회 · dotenv · subprocess env
#         + «컨테이너에 담긴 문자열 상수»(A9) — 이 축이 없으면 _TG_PAIRS 처럼
#           동적으로 소비되는 이름을 통째로 놓친다(양성대조로 실증함)
#     자기오염 배제: ops_preflight.py/ops_env_scan.py 의 denylist «나열»은 사용이 아니다
#     주석/코드 분리: KIS_ENV·KRX_ID·KRX_PW 는 «주석에만» 등장 → 사용 아님
#
#     실사용 5종 → 통과.  나머지 14종 → 차단(코드 등장 0건 11종 + 주석만 3종)
#     ★차단되는 것에 KIS_APP_KEY·KIS_APP_SECRET·KIS_ACCOUNT_NO·KIS_ACCOUNT_CODE·
#       UPBIT×2·ALPACA×2 = «주문 권한을 가진 키 8종»이 포함된다.
#
#   ★이건 «전환기 조치»다. 최종 목표는 MIRI 가 kis-trading\.env 를 «아예 안 보는 것».
#     TELEGRAM_* 2종은 목표 상태에서 제거 대상(Render 는 이미 miri_bot_dm 짝으로 동작).
KIS_ENV_ALLOWLIST = {
    "DART_API_KEY",      # 공시 폴링(config.py)
    "TOSS_APP_KEY",      # read-only 시세(features/ranking/price_adapter.py)
    "TOSS_SECRET",       # 〃
    "TELEGRAM_TOKEN",    # 전환기 한정 — 목표 상태에서 제거
    "TELEGRAM_CHAT_ID",  # 전환기 한정 — 목표 상태에서 제거
}

ENV_SOURCES = []
KIS_ENV_ADMITTED, KIS_ENV_BLOCKED = [], []
if ALLOW_KIS_ENV and KIS_ENV.exists():
    # load_dotenv 가 아니라 dotenv_values — «읽기만» 하고 주입은 우리가 고른다.
    for _k, _v in (dotenv_values(KIS_ENV) or {}).items():
        if _k in KIS_ENV_ALLOWLIST:
            if _v is not None and _k not in os.environ:
                os.environ[_k] = _v
            KIS_ENV_ADMITTED.append(_k)
        else:
            KIS_ENV_BLOCKED.append(_k)
    ENV_SOURCES.append(f"kis-trading/.env(화이트리스트 {len(KIS_ENV_ADMITTED)}/"
                       f"{len(KIS_ENV_ADMITTED) + len(KIS_ENV_BLOCKED)}종)")
    if not _allow_raw:
        # 「기본값이라서」 열린 경우는 조용히 넘기지 않는다 — 전환 전임을 매 기동 알린다.
        print("[config][DEPRECATED] kis-trading/.env 를 «기본값»으로 로드했다. "
              "실계좌 자격증명이 MIRI 프로세스에 들어온다. "
              "MIRI_ALLOW_KIS_ENV 를 명시하라(Partner 게이트 U1 로 기본값 차단 전환 예정)")
elif not ALLOW_KIS_ENV:
    ENV_SOURCES.append("(kis-trading/.env 차단됨: MIRI_ALLOW_KIS_ENV)")
# 2) 로컬 .env 오버라이드 (있으면 우선). 이건 MIRI 자기 것이라 항상 읽는다.
if LOCAL_ENV.exists():
    load_dotenv(LOCAL_ENV, override=True)
    ENV_SOURCES.append("gongsi-alert/.env")

DART_API_KEY = os.getenv("DART_API_KEY", "")

# ── 운영 알림 목적지 해석 (2026-08-16 R3) ────────────────────────────────
# 왜 «짝(pair)»으로 푸는가:
#   토큰과 chat_id 를 각각 독립적으로 폴백시키면 「A 봇 토큰 + B 봇 전용 chat_id」
#   라는 어긋난 조합이 조용히 만들어진다. 봇은 자기가 모르는 chat 에 못 보내므로
#   전송이 실패하는데, 설정은 «채워져 있어» 보여서 원인 추적이 어렵다.
#   그래서 (토큰, chat) 을 «함께 있을 때만» 채택한다.
#
# 왜 이 폴백이 필요한가 [실측 2026-08-16]:
#   Render 라이브 env 20종에 TELEGRAM_TOKEN·TELEGRAM_CHAT_ID·GONGSI_TEST_CHAT_ID 가
#   «3종 전부 없다». 아래 19행의 kis-trading\.env 폴백은 리눅스 컨테이너엔 그 경로가
#   없어 무효다. 그 결과 신선도 경보 6건이 배달 0건으로 사라졌다(12일 사고의 축).
#   ★해법은 «새 시크릿 주입»이 아니다 — Render 에 이미 있는 이름을 읽으면 된다.
#
# 짝 3종 (앞선 것이 이긴다):
#   1) TELEGRAM_TOKEN     + GONGSI_TEST_CHAT_ID   명시 지정(최우선)
#   2) TELEGRAM_TOKEN     + TELEGRAM_CHAT_ID      노트북(kis-trading\.env 경유)
#   3) TELEGRAM_BOT_TOKEN + WAITLIST_TG_CHAT_ID   Render 라이브. 둘 다 이미 존재
#      [실측] getMe → @MIRI_Alert_Bot / getChat(WAITLIST_TG_CHAT_ID) → type=private, ok=True
#             = 봇이 이 개인 DM 을 «이미 알고 있다»(발송 가능). app.py:1493
#             _notify_waitlist_tg 가 쓰는 것과 «같은 조합»이라 신규 배선이 아니다.
#
# ★절대 금지: TG_CHANNEL_ID 는 목적지 후보가 «아니다».
#   [실측] getChat → type=channel, @rimismiri, 구독자 451명 = 실유저 공지채널.
#   운영 경보를 여기로 보내면 사고다. 후보 목록에 넣지 않는 것으로 구조적으로 막는다.
_TG_PAIRS = (
    ("gongsi_test", "TELEGRAM_TOKEN", "GONGSI_TEST_CHAT_ID"),
    ("operator_dm", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"),
    ("miri_bot_dm", "TELEGRAM_BOT_TOKEN", "WAITLIST_TG_CHAT_ID"),
)


def _resolve_tg_pair():
    """(token, chat_id, source_name, tried) 반환. 값은 로그에 남기지 않는다."""
    tried = []
    for name, tok_env, chat_env in _TG_PAIRS:
        tok = os.getenv(tok_env, "").strip()
        cid = os.getenv(chat_env, "").strip()
        if tok and cid:
            return tok, cid, name, tried
        tried.append(f"{name}({tok_env}:{'O' if tok else 'X'}/{chat_env}:{'O' if cid else 'X'})")
    return "", "", "", tried


TELEGRAM_TOKEN, TEST_CHAT_ID, TELEGRAM_SOURCE, TELEGRAM_TRIED = _resolve_tg_pair()

# Supabase(관심종목 영속) — 이름만 참조, 값은 os.getenv 로만 읽는다(하드코딩 0).
# 값이 비어 있으면 watch_store 가 JSON 폴백으로 동작한다(로컬/키없음 graceful).
# 키는 로그/응답/예외에 절대 노출 금지.
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
# 서비스롤 키: 로컬 .env 는 SUPABASE_SERVICE_ROLE(접미사 없음)을 쓰지만,
# 배포환경(예: Supabase 대시보드 복붙)은 SUPABASE_SERVICE_ROLE_KEY 로 줄 수
# 있어 두 이름 모두 폴백 조회한다(하드코딩 0, os.getenv 만).
SUPABASE_SERVICE_ROLE = (os.getenv("SUPABASE_SERVICE_ROLE", "")
                         or os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""))
SUPABASE_ACCESS_TOKEN = os.getenv("SUPABASE_ACCESS_TOKEN", "")

# 관심종목 영속 백엔드 선택. 키가 .env 에 있어도 기본은 'json'(안전).
# 실 Supabase 연결은 Partner 가 배포환경에서 GONGSI_WATCH_BACKEND=supabase 로
# 명시 opt-in 할 때만 활성 → 로컬/개발에서 실 DB 오접속 방지(Partner 게이트).
#   json     : 항상 JSON 파일(watchlist.json). 기본값.
#   supabase : Supabase REST 사용(키 필요). 실패 시 JSON 폴백.
#   auto     : 키가 있으면 supabase, 없으면 json.
WATCH_BACKEND = os.getenv("GONGSI_WATCH_BACKEND", "json").strip().lower()

# 안전장치: 실유저 브로드캐스트 금지.
# 알림은 운영자 «개인» 대상으로만 나간다(위 _TG_PAIRS 에서 해석). 어느 짝도 성립하지
# 않으면 TEST_CHAT_ID="" 가 되어 콘솔/파일로만 출력한다.
# ※ TEST_CHAT_ID / TELEGRAM_TOKEN 은 위 _resolve_tg_pair() 에서 «짝으로» 정해진다.
#    여기서 재정의하지 말 것 — 독립 폴백은 어긋난 조합을 만든다(R3 사고 원인).

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
