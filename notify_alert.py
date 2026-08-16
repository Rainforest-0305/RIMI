# -*- coding: utf-8 -*-
"""알림 전송 — 텔레그램(테스트 채널 전용) + 콘솔/파일 폴백.

패턴 출처: kis-trading/notify.py 의 tg().
**안전 규칙: 실유저 브로드캐스트 금지.** 여기서는 오직 운영자 «개인» 대상
(config.TEST_CHAT_ID) 으로만 보낸다. 실유저 공지채널(TG_CHANNEL_ID / @rimismiri,
구독자 451명 실측)은 목적지 후보에 «없다» — config._TG_PAIRS 참조.

토큰/채널이 없으면 콘솔·파일로 폴백한다(알림이 끊겨도 폴링은 계속 — fail-open).
★단 «조용히»는 아니다. 2026-08-16 R3 이전에는 조용했고, 그래서 경보 6건이
배달 0건인 채로 12일간 발견되지 않았다. 지금은 모든 실패가 로그로 남는다.
"""
import time
from datetime import datetime

import requests

import config
from dart_poll import dart_url


# 미설정 경고 1회 억제용(로그 스팸 방지). 「조용히」가 아니라 「한 번은 크게」가 목적.
_WARNED_NO_TARGET = False


def _tg_send(text: str) -> bool:
    """텔레그램 전송. 반환은 기존 계약대로 bool 이지만, **실패는 전부 로그로
    표면화**한다.

    ★2026-08-16 R3 — 여기가 12일 사고의 실행 지점이었다.
      이전 구현은 토큰/chat 이 없으면 `return False` 로 «조용히» 빠져나갔다.
      예외도 로그도 없어서, 신선도 경보 6건이 발화하고도 배달 0건인 사실을
      아무도 알 수 없었다. 「조용한 실패」가 사고를 12일로 늘렸다.
      이제 실패 사유를 반드시 남긴다 — 로그가 있어야 다음 사람이 볼 수 있다.
    """
    global _WARNED_NO_TARGET
    if not config.TELEGRAM_TOKEN or not config.TEST_CHAT_ID:
        if not _WARNED_NO_TARGET:
            _WARNED_NO_TARGET = True
            print("[notify][MISCONFIG] 텔레그램 목적지 미해석 — 알림이 «배달되지 않는다». "
                  f"시도한 짝: {', '.join(config.TELEGRAM_TRIED) or '(없음)'}"
                  " · 필요: (TELEGRAM_TOKEN+GONGSI_TEST_CHAT_ID) 또는 "
                  "(TELEGRAM_TOKEN+TELEGRAM_CHAT_ID) 또는 "
                  "(TELEGRAM_BOT_TOKEN+WAITLIST_TG_CHAT_ID)")
        print("[notify][UNDELIVERED] 콘솔로만 출력됨: " + text.replace("\n", " | ")[:300])
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": config.TEST_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=8)
        if r.status_code != 200:
            # 비200 도 조용히 넘기지 않는다. 토큰↔chat 짝 불일치가 여기서 드러난다.
            body = ""
            try:
                body = str(r.json().get("description"))[:200]
            except Exception:  # noqa: BLE001
                body = r.text[:200]
            print(f"[notify][SEND_FAIL] http={r.status_code} src={config.TELEGRAM_SOURCE} "
                  f"reason={body}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify][SEND_EXC] {type(e).__name__}: {str(e)[:200]} "
              f"src={config.TELEGRAM_SOURCE}")
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
