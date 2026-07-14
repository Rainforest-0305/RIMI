# -*- coding: utf-8 -*-
"""중복 이벤트 dedup — 같은 기업의 사실상 같은 사건 공시를 하나로 접는다.

문제의식: DART 피드에는 같은 기업의 **같은 실체 사건**이 여러 공시로 흩어진다.
  (1) 결정 ↔ 결과: '주요사항보고서(자기주식처분결정)' 와 '자기주식처분결과보고서',
      '유상증자결정' 과 '증자등의발행결과', '전환사채권발행결정' 과 '발행결과',
      '자기주식취득결정' 과 '취득결과보고서' 등.
  (2) 정정 ↔ 원본: '[기재정정]X' / '[첨부정정]X' 와 원본 'X'. 같은 사건의 정정본.
  (3) 부수 공시: '주권매매거래정지(무상증자)', '…를위한주주명부폐쇄(기준일)결정'
      같은 행정/부수 공시는 본 결정과 같은 사건.
이들을 묶어 **정보량이 큰 대표 1건만** 남기고 나머지는 접는다.

그룹핑 키 = (종목코드, 이벤트유형). 이벤트유형은 아래 두 단계로 정한다.
  · 티어A(명시 유형): 결정/결과/부수 공시가 report_nm 키워드는 달라도 같은 실체
    사건이면 하나의 유형으로 정규화한다(예: 유상증자결정·증자등의발행결과 →'유상증자').
    사채(전환/BW/EB)와 만기전취득은 **회차(제N회)** 를 유형에 포함해 서로 다른
    발행/취득을 섞지 않는다.
  · 티어B(폴백): 티어A에 안 걸리면 '정정프리픽스 제거 + 공백제거한 제목' 자체를
    유형으로 쓴다 → **제목이 완전히 같은 정정/원본만** 병합(소송·담보계약처럼
    괄호 안 소제목이 다르면 자연히 분리 유지 — 서로 다른 사건을 오병합하지 않음).

대표 선택 우선순위(높을수록 유지):
  (stage_rank, is_correction, rcept_dt, rcept_no)
  · stage_rank: 결정/체결(3) > 증권신고서(2) > 결과/실적보고(1) > 거래정지/명부폐쇄(0).
    → **정보량 큰 '결정'을 남기고 '결과'·부수공시는 접는다.**
  · is_correction: 같은 stage면 정정본(최신 권위본)을 남긴다.
  · 그다음 최신(rcept_dt·rcept_no) 순.
대표가 bullet(정량정보)이 없고 접히는 형제 중 bullet 있는 게 있으면 그 bullet을
대표로 옮겨 붙인다(정정본이 아직 구조화 미반영이라 숫자가 비어도 커버리지 유지).

주의(허용된 트레이드오프): 티어A 유형은 같은 기업이 같은 창(7일)에 회차 표기 없는
동일유형 사건 2건(예: 서로 다른 공급계약)을 낼 경우 오병합될 수 있다. 실무 빈도가
낮고 피드 가독성 이득이 크므로 허용. 회차 있는 사채류는 회차로 분리해 방지한다.
"""
import re

# 정정/첨부/연장/추가 등 앞머리 대괄호 마커(모두 '원본을 갱신하는' 공시로 간주).
_LEAD_BRACKET = re.compile(r"^(?:\s*\[[^\]]*\]\s*)+")
_SERIES = re.compile(r"제\s*(\d+)\s*회")
_WS = re.compile(r"\s+")


def _norm(nm: str) -> str:
    """제목 정규화: 앞머리 대괄호(정정마커) 제거 + 공백 전부 제거."""
    s = _LEAD_BRACKET.sub("", nm or "")
    return _WS.sub("", s)


def _is_correction(nm: str) -> bool:
    """앞머리에 대괄호 마커([기재정정]/[첨부정정]/[연장결정] 등)가 있으면 정정/갱신본."""
    return bool(re.match(r"^\s*\[", nm or ""))


def _series(nm0: str) -> str:
    """제N회(차) 회차 문자열(사채 발행/만기전취득 구분용). 없으면 ''."""
    m = _SERIES.search(nm0)
    return m.group(1) if m else ""


def _stage_rank(nm0: str) -> int:
    """공시 단계별 정보량 순위(클수록 원류·정보량 큼)."""
    if ("거래정지" in nm0) or ("명부폐쇄" in nm0) or ("기준일" in nm0):
        return 0          # 부수/행정 공시
    if ("결과" in nm0) or ("실적보고서" in nm0) or ("발행실적" in nm0):
        return 1          # 결과보고
    if "신고서" in nm0:
        return 2          # 증권신고서(결정과 결과 사이 단계)
    return 3              # 결정/체결/제기/신청(원류)


def _family(nm0: str) -> str:
    """티어A 명시 이벤트유형(+회차). 안 걸리면 None → 티어B 폴백."""
    s = _series(nm0)
    suf = ("#" + s) if s else ""
    # 소각(자기주식소각/이익소각 포함) — 자사주 규칙보다 먼저.
    if "소각" in nm0:
        return "주식소각"
    # 자기주식 신탁계약 체결/해지(반대 행위 → 분리)
    if "자기주식" in nm0 and "신탁계약" in nm0:
        if "해지" in nm0:
            return "자사주신탁해지"
        return "자사주신탁체결"
    if "자기주식처분" in nm0:
        return "자사주처분"
    if "자기주식취득" in nm0 or ("자기주식" in nm0 and "결과보고서" in nm0):
        return "자사주취득"
    # 유상증자(결정) + 증자등의발행결과 + 증권발행결과(…유상증자)
    if "유상증자" in nm0 or "증자등의발행결과" in nm0 or (
            "증권발행결과" in nm0 and "유상증자" in nm0):
        return "유상증자"
    if "무상증자" in nm0:
        return "무상증자"
    # 사채(전환/BW/EB): 만기전취득(회차별 별건) vs 발행(회차별 별건)
    bond = ("전환사채" in nm0 or "신주인수권부사채" in nm0 or "교환사채" in nm0)
    if bond and "만기전" in nm0:
        return "사채만기전취득" + suf
    if bond and "발행" in nm0:
        return "사채발행" + suf
    if "배당" in nm0:
        return "배당"
    if "합병" in nm0 or "분할" in nm0:
        return "합병분할"
    if "공급계약" in nm0 or "단일판매" in nm0:
        return "공급계약"
    return None


def _group_key(item: dict):
    corp = (item.get("stock_code") or "").strip() or (item.get("corp_name") or "").strip()
    nm = item.get("report_nm") or ""
    nm0 = _norm(nm)
    fam = _family(nm0) or ("RAW:" + nm0)
    return (corp, fam)


def dedup(items: list) -> list:
    """피드 아이템 리스트 -> 중복 이벤트 접힌 리스트(대표만).
    입력 순서를 대체로 보존한다(정렬은 호출부에서 별도 수행)."""
    groups = {}   # key -> list of items (원 순서 인덱스 포함)
    order = []    # 대표 등장 순서 유지용 키 리스트
    for it in items:
        k = _group_key(it)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)

    out = []
    for k in order:
        grp = groups[k]
        if len(grp) == 1:
            out.append(grp[0])
            continue

        def _sel_key(it):
            nm = it.get("report_nm") or ""
            nm0 = _norm(nm)
            return (_stage_rank(nm0),
                    1 if _is_correction(nm) else 0,
                    it.get("rcept_dt", ""),
                    it.get("rcept_no", ""))

        rep = max(grp, key=_sel_key)
        # bullet(정량정보) 보존: 대표에 없고 형제에 있으면 옮겨온다.
        if not rep.get("bullets"):
            best = max(grp, key=lambda it: len(it.get("bullets") or []))
            if best.get("bullets"):
                rep = dict(rep)
                rep["bullets"] = best["bullets"]
        rep = dict(rep)
        rep["dup_folded"] = len(grp) - 1   # 접힌 형제 수(투명성/디버깅용)
        out.append(rep)
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sample = [
        {"stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "주요사항보고서(자기주식처분결정)", "bullets": ["처분 10억"],
         "rcept_dt": "20260710", "rcept_no": "20260710000395"},
        {"stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "자기주식처분결과보고서", "bullets": [],
         "rcept_dt": "20260713", "rcept_no": "20260713000010"},
        {"stock_code": "084180", "corp_name": "수성",
         "report_nm": "전환사채(해외전환사채포함)발행후만기전사채취득 (제23회차)",
         "bullets": [], "rcept_dt": "20260713", "rcept_no": "1"},
        {"stock_code": "084180", "corp_name": "수성",
         "report_nm": "전환사채(해외전환사채포함)발행후만기전사채취득 (제24회차)",
         "bullets": [], "rcept_dt": "20260713", "rcept_no": "2"},
    ]
    for r in dedup(sample):
        print(r["report_nm"], "| folded", r.get("dup_folded", 0), "| b", r.get("bullets"))
