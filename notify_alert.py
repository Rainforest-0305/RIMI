# -*- coding: utf-8 -*-
"""알림 전송 — 텔레그램(테스트 채널 전용) + 콘솔/파일 폴백.

패턴 출처: kis-trading/notify.py 의 tg().
**안전 규칙: 실유저 브로드캐스트 금지.** 여기서는 오직 본인 테스트 채널
(config.TEST_CHAT_ID) 로만 보낸다. 토큰/채널이 없으면 조용히 콘솔·파일로만
출력한다(알림이 끊겨도 폴링은 계속 — fail-open).
"""
import time
from datetime import datetime

import requests

import config
from dart_poll import dart_url


def _tg_send(text: str) -> bool:
    if not config.TELEGRAM_TOKEN or not config.TEST_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": config.TEST_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=8)
        return r.status_code == 200
    except Exception:
        return False


def format_alert(item: dict, result: dict) -> str:
    """알림 메시지 포맷(텔레그램/콘솔 공통)."""
    tags = " ".join(f"#{t}" for t in result["tags"])
    lines = [
        f"[공시알리미] {item.get('corp_name','')} ({item.get('stock_code','')})",
        tags,
        "",
    ]
    lines += result["summary"]
    lines += [
        "",
        f"원문: {dart_url(item.get('rcept_no',''))}",
        "ⓘ 투자권유가 아닌 정보 제공이며, 원문을 대체하지 않습니다.",
    ]
    return "\n".join(lines)


def send(item: dict, result: dict) -> str:
    """알림 발송. 반환: 실제 사용된 채널('telegram' | 'console')."""
    msg = format_alert(item, result)
    channel = "telegram" if _tg_send(msg) else "console"
    # 항상 파일 로그(감사/폴백)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config.ALERT_LOG, "a", encoding="utf-8") as f:
        f.write(f"===== {stamp} [{channel}] =====\n{msg}\n\n")
    # 콘솔에도 항상 출력(작동 증명용)
    print(f"--- 알림({channel}) ---")
    print(msg)
    print()
    return channel


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    item = {"corp_name": "테스트기업", "stock_code": "000000",
            "report_nm": "주요사항보고서(자기주식취득결정)", "flr_nm": "테스트기업",
            "rcept_dt": "20260713", "rcept_no": "20260713000001", "rm": ""}
    from summarize import summarize
    print("사용 채널:", send(item, summarize(item)))
