# -*- coding: utf-8 -*-
"""공시알리미(gongsi-alert) 텔레그램 구독 봇 — 골격(skeleton).

상태: **토큰 주입 대기.** 임포트만으로는 어떤 네트워크 동작도 하지 않는다.
발송/폴링은 `if __name__ == "__main__"` 안에서, 토큰이 설정된 명시적 실행일
때만 일어난다. 토큰이 없으면 "미기동"만 출력하고 종료한다.

안전 규칙(WS-17 게이트):
- 토큰은 오직 env(``TELEGRAM_BOT_TOKEN``)에서만 읽는다. 하드코딩 금지.
- 실유저 브로드캐스트 금지. push_disclosure 는 골격이며 토큰 없으면 no-op(로그만).
- 실행/발송/폴링은 자동으로 시작되지 않는다.

env 로딩은 config.py 와 동일한 방식(load_dotenv 로 os.environ 채움)을 재사용한다.
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path

# config 임포트만으로 kis-trading/.env + 로컬 .env 가 os.environ 에 로드된다.
# (네트워크 동작 없음; DATA 디렉터리 준비만.) 봇 토큰도 .env 에 있으면 여기서 노출된다.
try:
    import config  # noqa: F401  (side-effect: load_dotenv → os.environ)
    _DATA_DIR = config.DATA
except Exception:  # config 미가용 환경에서도 임포트가 깨지지 않게 방어
    _DATA_DIR = Path(__file__).parent / "data"
    _DATA_DIR.mkdir(exist_ok=True)

log = logging.getLogger("gongsi.telegram_bot")

# ── 토큰: env 참조만. 하드코딩 금지. 없으면 봇 미기동(graceful). ──────────────
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# 구독자 저장(스텁): data/* 는 .gitignore 로 제외되므로 커밋되지 않는다.
SUBSCRIBERS_FILE = _DATA_DIR / "bot_subscribers.jsonl"

# python-telegram-bot 이 있으면 사용, 없으면 requests 최소 래퍼로 폴백.
# 어느 쪽이든 import 실패로 모듈 로드가 깨지지 않게 가드한다.
try:
    from telegram import Update  # type: ignore
    from telegram.ext import Application, CommandHandler, ContextTypes  # type: ignore
    _PTB_AVAILABLE = True
except Exception:
    _PTB_AVAILABLE = False

try:
    import requests  # type: ignore
    _REQUESTS_AVAILABLE = True
except Exception:
    _REQUESTS_AVAILABLE = False


# ── 구독자 저장 스텁 (파일 기반, JSONL) ──────────────────────────────────────
def _load_subscribers() -> dict:
    """chat_id(str) -> 구독 레코드(dict). 파일 없으면 빈 dict."""
    subs: dict = {}
    if not SUBSCRIBERS_FILE.exists():
        return subs
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                cid = str(rec.get("chat_id", ""))
                if cid:
                    subs[cid] = rec  # 마지막 레코드가 최종 상태
    except Exception as e:
        log.warning("구독자 로드 실패: %s", e)
    return subs


def _append_subscriber(rec: dict) -> None:
    """append-only 로그. 최종 상태는 _load_subscribers 가 마지막 레코드로 판정."""
    try:
        with open(SUBSCRIBERS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("구독자 저장 실패: %s", e)


def _is_subscribed(chat_id) -> bool:
    rec = _load_subscribers().get(str(chat_id))
    return bool(rec and rec.get("active"))


# ── 명령 핸들러 뼈대 (프레임워크 무관 순수 로직; 반환값 = 응답 텍스트) ──────────
def handle_start(chat_id) -> str:
    return (
        "공시알리미 봇입니다.\n"
        "/subscribe 구독 등록 · /unsubscribe 해제 · /status 상태 확인\n"
        "ⓘ 정보 제공이며 투자권유가 아닙니다."
    )


def handle_subscribe(chat_id) -> str:
    if _is_subscribed(chat_id):
        return "이미 구독 중입니다. /status 로 확인하세요."
    _append_subscriber({
        "chat_id": str(chat_id),
        "active": True,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    return "구독 등록됨. 새 공시가 감지되면 알려드립니다."


def handle_unsubscribe(chat_id) -> str:
    if not _is_subscribed(chat_id):
        return "구독 내역이 없습니다."
    _append_subscriber({
        "chat_id": str(chat_id),
        "active": False,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    return "구독 해제됨."


def handle_status(chat_id) -> str:
    return "구독 중입니다." if _is_subscribed(chat_id) else "구독 중이 아닙니다."


# ── 저수준 발송 (골격: 토큰 없으면 no-op, 로그만) ────────────────────────────
def send_message(chat_id, text: str) -> bool:
    """단일 chat_id 로 메시지 발송. 토큰/requests 없으면 no-op(로그만).

    브로드캐스트 금지 — 반드시 단일 수신자 대상. 실발송은 토큰 주입 후에만.
    """
    if not TOKEN:
        log.info("[no-op] 토큰 미설정 — 발송 생략 (chat_id=%s)", chat_id)
        return False
    if not _REQUESTS_AVAILABLE:
        log.info("[no-op] requests 미가용 — 발송 생략 (chat_id=%s)", chat_id)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "disable_web_page_preview": True},
            timeout=8,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning("발송 실패(chat_id=%s): %s", chat_id, e)
        return False


def format_push(disclosure: dict) -> str:
    """공시 dict → 푸시 메시지 텍스트.

    통계/요약은 기존 모듈(summarize.summarize(item)->dict,
    impact.impact_for_tags(tags)->dict)을 재사용할 수 있으나, 골격 단계에서는
    시그니처 수준 참조만 하고 실제 호출은 하지 않는다(부작용/네트워크 회피).
    """
    corp = disclosure.get("corp_name", "")
    code = disclosure.get("stock_code", "")
    report = disclosure.get("report_nm", "")
    return f"[공시알리미] {corp} ({code})\n{report}"


def push_disclosure(subscriber: dict, disclosure: dict) -> bool:
    """단일 구독자에게 공시 1건 푸시(골격).

    토큰 없으면 no-op(로그만). 브로드캐스트 아님 — 호출자가 구독자별로 호출.
    """
    chat_id = subscriber.get("chat_id") if isinstance(subscriber, dict) else subscriber
    if not chat_id:
        return False
    return send_message(chat_id, format_push(disclosure))


# ── PTB 어댑터 (라이브러리 있을 때만; 순수 핸들러를 async 로 감쌈) ────────────
def _build_ptb_application():
    """python-telegram-bot Application 구성. TOKEN 필요."""
    async def _start(update, context):
        await update.message.reply_text(handle_start(update.effective_chat.id))

    async def _subscribe(update, context):
        await update.message.reply_text(handle_subscribe(update.effective_chat.id))

    async def _unsubscribe(update, context):
        await update.message.reply_text(handle_unsubscribe(update.effective_chat.id))

    async def _status(update, context):
        await update.message.reply_text(handle_status(update.effective_chat.id))

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", _start))
    app.add_handler(CommandHandler("subscribe", _subscribe))
    app.add_handler(CommandHandler("unsubscribe", _unsubscribe))
    app.add_handler(CommandHandler("status", _status))
    return app


def run_bot() -> None:
    """폴링 시작. **명시적 실행일 때만 호출.** 토큰 필수, 임포트 시 자동 호출 금지."""
    if not TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN 미설정 — 봇 미기동")
        return
    if not _PTB_AVAILABLE:
        log.warning(
            "python-telegram-bot 미설치 — 폴링 미기동. "
            "설치: pip install python-telegram-bot (requirements 에 명시만)."
        )
        return
    log.info("텔레그램 봇 폴링 시작")
    app = _build_ptb_application()
    app.run_polling()


if __name__ == "__main__":
    import sys
    # Windows 콘솔(cp949) 유니코드 출력 방어
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not TOKEN:
        # 토큰 없으면: 네트워크/발송/폴링 없이 즉시 종료.
        print("TELEGRAM_BOT_TOKEN 미설정 — 봇 미기동")
    else:
        # 토큰이 있어도 명시적 실행(__main__)일 때만 폴링 시작.
        run_bot()
