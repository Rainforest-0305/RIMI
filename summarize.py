# -*- coding: utf-8 -*-
"""공시 요약·분류.

MVP 정책:
- **매매추천 절대 없음.** 순수 정보·요약·분류만. (자문규제 밖 유지)
- 분류(영향 태그)는 공시 제목(report_nm) 키워드 규칙 기반 — 결정적/설명가능.
- 3줄 요약은 지금은 규칙기반 스텁으로 동작한다. 실제 LLM 요약이 들어갈
  자리는 `llm_summarize()` 하나로 명확히 격리 — 나중에 이 함수 본문만
  교체하면 원문 기반 고품질 요약으로 승격된다(인터페이스 고정).
"""
from typing import Callable, Optional

# 영향 태그 규칙: (표시명, [제목에 포함되면 매칭되는 키워드])
# 순서 = 우선순위(위쪽이 더 중요/구체적). 여러 개 매칭 가능.
TAG_RULES = [
    ("유상증자",    ["유상증자"]),
    ("무상증자",    ["무상증자"]),
    ("전환사채",    ["전환사채", "신주인수권부사채", "교환사채", "CB발행", "BW발행"]),
    ("자사주",      ["자기주식", "자사주"]),
    ("최대주주변경", ["최대주주변경", "최대주주 변경", "경영권"]),
    ("주식소각",    ["주식소각", "이익소각"]),
    ("배당",        ["배당", "현금ㆍ현물배당", "분기배당"]),
    ("실적",        ["영업(잠정)실적", "잠정실적", "매출액또는손익구조",
                    "영업실적", "분기보고서", "반기보고서", "사업보고서"]),
    ("합병분할",    ["합병", "분할", "주식교환", "영업양수도", "자산양수도"]),
    ("공급계약",    ["공급계약", "수주", "단일판매"]),
    ("소송",        ["소송", "가처분", "회생", "파산"]),
    ("감사보고서",  ["감사보고서", "감사의견"]),
    ("임상",        ["임상", "품목허가", "판매허가"]),
    ("지분변동",    ["주식등의대량보유", "임원ㆍ주요주주", "지분", "특수관계인"]),
    ("정정공시",    ["정정", "기재정정", "첨부정정"]),
]


def classify(report_nm: str):
    """공시 제목 -> 영향 태그 리스트. 매칭 없으면 ['기타공시']."""
    title = (report_nm or "").replace(" ", "")
    tags = []
    for name, kws in TAG_RULES:
        for kw in kws:
            if kw.replace(" ", "") in title:
                tags.append(name)
                break
    return tags or ["기타공시"]


# ---------- LLM 요약 인터페이스 (지금은 스텁) ----------
def _rule_based_summary(item: dict, tags) -> list:
    """LLM 없이 메타데이터만으로 만드는 결정적 3줄 요약(스텁).
    순수 사실 프레이밍만(투자권유/예측 표현 배제). 항상 정확히 3줄 반환."""
    corp = (item.get("corp_name", "") or "").strip()
    title = (item.get("report_nm", "") or "").strip()
    filer = (item.get("flr_nm", "") or "").strip()
    date = (item.get("rcept_dt", "") or "").strip()
    rm = (item.get("rm", "") or "").strip()

    # 1줄: 회사 + 공시 제목(사실)
    line1 = f"{corp} · {title}" if corp and title else (title or corp or "공시")
    # 2줄: 접수일 + 제출인(사실)
    if len(date) == 8 and date.isdigit():
        d = f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
    else:
        d = date
    parts2 = []
    if d:
        parts2.append(f"{d} 접수")
    if filer:
        parts2.append(f"제출 {filer}")
    line2 = " · ".join(parts2) or "접수 정보 없음"
    # 3줄: 영향 분류 태그(사실) + 비고(정정 등) 표시
    tag_txt = " · ".join(tags) if tags else "기타공시"
    line3 = f"분류: {tag_txt}"
    if rm:
        line3 += f" · 비고 {rm}"
    return [line1, line2, line3]


# 실제 LLM 요약 훅. 주입되면(runtime) 원문/제목을 받아 3줄 요약을 반환.
# 시그니처: fn(item: dict, tags: list[str]) -> list[str]  (3줄)
_llm_hook: Optional[Callable[[dict, list], list]] = None


def set_llm_hook(fn: Callable[[dict, list], list]):
    """멀티에이전트/Claude 요약 로직을 여기 주입하면 스텁을 대체한다."""
    global _llm_hook
    _llm_hook = fn


def llm_summarize(item: dict, tags) -> list:
    """3줄 요약 생성. LLM 훅이 있으면 사용, 없으면 규칙기반 스텁.
    항상 정확히 3줄(list[str])을 반환하도록 보정."""
    if _llm_hook is not None:
        try:
            out = _llm_hook(item, tags)
            if isinstance(out, list) and out:
                return (out + ["", "", ""])[:3]
        except Exception:
            pass  # LLM 실패해도 알림은 끊기지 않게(fail-open) 스텁으로 폴백
    return _rule_based_summary(item, tags)


def summarize(item: dict) -> dict:
    """공시 1건 -> {tags, summary(3줄)}. 알림 payload 생성용."""
    tags = classify(item.get("report_nm", ""))
    return {"tags": tags, "summary": llm_summarize(item, tags)}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    samples = [
        {"corp_name": "삼성전자", "report_nm": "주요사항보고서(자기주식취득결정)",
         "flr_nm": "삼성전자", "rcept_dt": "20260713", "rm": ""},
        {"corp_name": "카카오", "report_nm": "[기재정정]유상증자결정",
         "flr_nm": "카카오", "rcept_dt": "20260713", "rm": "정"},
    ]
    for s in samples:
        r = summarize(s)
        print("태그:", r["tags"])
        for ln in r["summary"]:
            print("  ", ln)
        print()
