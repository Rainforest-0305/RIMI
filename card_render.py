# -*- coding: utf-8 -*-
"""과거영향 카드 자동 생성기 (서버측 PIL/Pillow 렌더).

클라이언트 Canvas 정본(web/index.html `renderCardCanvas`, 라인 468~599)의
다크 1080x1080 디자인을 Pillow로 재현한다. 데모(Canvas)==배포(서버) 동일 출력.

파이프라인(이 경로만):
  disclosure(corp_name·stock_code·report_nm·rcept_no)
    -> summarize.classify(report_nm) -> tags
    -> impact.impact_for_tags(tags)  -> impact 블록
    -> render_card(...)              -> PIL.Image / PNG 저장

impact.py 는 impact_benchmark.json 만 읽는다(bench_cache/dart_cache 무의존).
이 모듈도 그 외 캐시/DART 파일에 절대 접근하지 않는다.

CLI:
  python card_render.py <stock_code> <report_nm> <corp_name> <rcept_no>
        [out.png] [--valmode avg|med|car] [--win d1|w1|m1]
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import summarize
import impact

# ---------------------------------------------------------------------------
# 정본 팔레트 (renderCardCanvas 라인 476~478 그대로)
# ---------------------------------------------------------------------------
BG0 = (0x0e, 0x11, 0x16)
BG1 = (0x0a, 0x0c, 0x10)
CARD = (0x17, 0x1b, 0x21)
T1 = (0xe9, 0xed, 0xf2)
T2 = (0xaa, 0xb3, 0xbf)
T3 = (0x7d, 0x87, 0x94)
T4 = (0x56, 0x5f, 0x6b)
LINE = (0x25, 0x2b, 0x34)
BLUEINK = (0x8f, 0xbc, 0xff)
UP = (0xff, 0x5b, 0x64)     # 상승
DOWN = (0x4d, 0x94, 0xff)   # 하락
LOGO_A = (0x2f, 0x86, 0xff)
LOGO_B = (0x00, 0x61, 0xff)

WL = {"d1": "1일", "w1": "1주", "m1": "1개월"}
DEFWIN = "m1"
VALCAP = {"avg": "평균 등락", "med": "중앙값 등락", "car": "시장대비 초과등락"}

# 한글 폰트. Windows(malgun) 우선, Linux(배포=나눔) 폴백 — 서버 CJK 두부박스 방지.
# 존재하는 첫 경로를 정본으로 채택(플랫폼 무관).
def _first_existing(paths):
    for p in paths:
        if Path(p).exists():
            return Path(p)
    return None

_FONT_REG = _first_existing([
    r"C:\Windows\Fonts\malgun.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",       # fonts-nanum (Debian slim)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
])
_FONT_BLD = _first_existing([
    r"C:\Windows\Fonts\malgunbd.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]) or _FONT_REG
_font_cache = {}


def _font(size, weight):
    """weight>=700 이면 볼드, 아니면 레귤러. 플랫폼별 CJK 폰트 자동 채택·폴백 안전."""
    bold = weight >= 700
    key = (int(size), bold)
    if key in _font_cache:
        return _font_cache[key]
    path = _FONT_BLD if bold else _FONT_REG
    try:
        f = ImageFont.truetype(str(path), int(size))
    except Exception:
        try:
            f = ImageFont.truetype(str(_FONT_REG), int(size))
        except Exception:
            f = ImageFont.load_default()  # 최후 폴백(CJK 미지원 — 폰트 부재 환경)
    _font_cache[key] = f
    return f


# ---------------------------------------------------------------------------
# valPick / fmtPct 정본 재현 (index.html 라인 263~276, 335~339)
# ---------------------------------------------------------------------------
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def val_pick(w, valmode):
    """창 객체 -> {val, up}. 정본 valPick 로직 그대로."""
    if valmode == "car":
        up = w.get("car_up_prob")
        if not _num(up):
            up = w.get("up_prob")
        return w.get("car_avg"), (up if _num(up) else None)
    if valmode == "med":
        v = w.get("raw_med")
        if v is None:
            v = w.get("raw_avg")
        up = w.get("raw_up_prob")
        if not _num(up):
            up = w.get("up_prob")
        return v, (up if _num(up) else None)
    # avg (기본)
    up = w.get("raw_up_prob")
    if not _num(up):
        up = w.get("up_prob")
    return w.get("raw_avg"), (up if _num(up) else None)


def fmt_pct(v):
    """값 -> (표시문구, 방향). 정본 fmtPct 그대로."""
    if not _num(v):
        return "—", "flat"   # em-dash
    s = ("+" if v > 0 else "") + f"{float(v):.1f}%"
    cls = "up" if v > 0.05 else ("down" if v < -0.05 else "flat")
    return s, cls


# ---------------------------------------------------------------------------
# 저수준 렌더 헬퍼
# ---------------------------------------------------------------------------
def _text_w(draw, text, font, ls=0.0):
    """letterSpacing 반영 폭 측정(gap = len-1)."""
    s = str(text or "")
    if not s:
        return 0.0
    total = sum(draw.textlength(ch, font=font) for ch in s)
    if ls:
        total += ls * (len(s) - 1)
    return total


def _draw_text(draw, xy, text, font, fill, ls=0.0, align="left", anchor_v="s"):
    """baseline 정렬 텍스트. align=left/center/right, anchor_v='s'(baseline)/'m'(middle).
    ls != 0 이면 글자단위로 그려 letterSpacing 재현."""
    s = str(text or "")
    x, y = xy
    if not s:
        return
    if not ls:
        anc = {"left": "l", "center": "m", "right": "r"}[align] + anchor_v
        draw.text((x, y), s, font=font, fill=fill, anchor=anc)
        return
    total = _text_w(draw, s, font, ls)
    if align == "center":
        cx = x - total / 2.0
    elif align == "right":
        cx = x - total
    else:
        cx = x
    for ch in s:
        draw.text((cx, y), ch, font=font, fill=fill, anchor="l" + anchor_v)
        cx += draw.textlength(ch, font=font) + ls


def _wrap_lines(draw, text, font, max_w, max_lines):
    """char 단위 줄바꿈 + 말줄임(정본 _cardTitle 라인 451~465)."""
    lines, cur = [], ""
    for ch in str(text or ""):
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[max_lines - 1]
        while draw.textlength(last + "…", font=font) > max_w and last:
            last = last[:-1]
        lines[max_lines - 1] = last + "…"
    return lines


def _vgrad(w, h, c0, c1):
    """수직 선형 그라디언트 RGB 이미지."""
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / (h - 1)
        px[0, y] = tuple(int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3))
    return col.resize((w, h))


def _rrect(box, radius):
    return box, radius


def _blend(img, fn):
    """투명 오버레이 레이어에 fn(draw) 로 반투명 도형을 그린 뒤 알파 합성."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    fn(d)
    return Image.alpha_composite(img, layer)


# ---------------------------------------------------------------------------
# 카드 렌더 (renderCardCanvas 정본 좌표 그대로, 논리 1080x1080)
# ---------------------------------------------------------------------------
def render_card(item, valmode="avg", win=None):
    """disclosure/item dict -> PIL.Image (1080x1080 RGBA).

    item 필수: corp_name, stock_code, report_nm, rcept_no.
    'impact' 키가 없으면 파이프라인(classify -> impact_for_tags)으로 생성한다.
    """
    corp_name = str(item.get("corp_name", "") or "")
    stock_code = str(item.get("stock_code", "") or "")
    report_nm = str(item.get("report_nm", "") or "")

    imp = item.get("impact")
    if not imp:
        tags = summarize.classify(report_nm)
        imp = impact.impact_for_tags(tags)
    imp = imp or {}

    h = win or DEFWIN
    if h not in WL:
        h = DEFWIN
    wlab = WL[h]
    windows = imp.get("windows") or {}
    w = windows.get(h) or {}

    ok = imp.get("status") == "ok"
    pick_val, pick_up = val_pick(w, valmode) if ok else (None, None)
    ptxt, pcls = fmt_pct(pick_val)
    hero_color = UP if pcls == "up" else (DOWN if pcls == "down" else T3)
    n = w.get("n") if _num(w.get("n")) else None
    up_pct = int(round(pick_up * 100)) if _num(pick_up) else None
    dn_pct = None if up_pct is None else 100 - up_pct
    grade = imp.get("grade") or "na"
    g_txt = "신뢰도 " + str(imp.get("confidence") or "참고")
    if grade == "A":
        g_fg, g_bg = (0x2d, 0xd4, 0x8a), (45, 212, 138, int(0.14 * 255))
    elif grade == "B":
        g_fg, g_bg = (0x8f, 0xbc, 0xff), (77, 148, 255, int(0.14 * 255))
    else:
        g_fg, g_bg = (0x7d, 0x87, 0x94), (125, 135, 148, int(0.14 * 255))

    R = 1008
    CX = 540

    # ---- background: vertical gradient ----
    img = _vgrad(1080, 1080, BG0, BG1).convert("RGBA")

    # ---- top blue glow (radial, rgba(77,148,255,0.10) -> 0, center(540,-60)) ----
    def _glow(d):
        steps = 90
        for i in range(steps, 0, -1):   # 큰 원부터(알파낮음) -> 작은 원(알파높음) 덮어쓰기
            frac = i / steps
            r = 40 + (760 - 40) * frac
            alpha = int(round(0.10 * (1 - frac) * 255))
            d.ellipse([540 - r, -60 - r, 540 + r, -60 + r],
                      fill=(77, 148, 255, alpha))
        # 정본은 상단 600px 영역에만 채움(fillRect(0,0,1080,600))
        d.rectangle([0, 600, 1080, 1080], fill=(0, 0, 0, 0))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(layer)
    _glow(gd)
    layer.paste((0, 0, 0, 0), (0, 600, 1080, 1080))
    img = Image.alpha_composite(img, layer)

    draw = ImageDraw.Draw(img)

    # ---- outer border rounded rect ----
    draw.rounded_rectangle([6, 6, 6 + 1068, 6 + 1068], radius=34,
                           outline=LINE, width=2)

    # ---- HEADER: logo box (diagonal gradient) ----
    lx, ly, lsz, lr = 72, 60, 76, 20
    logo = _vgrad(lsz, lsz, LOGO_A, LOGO_B)  # 근사(수직) 그라디언트
    mask = Image.new("L", (lsz, lsz), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, lsz - 1, lsz - 1],
                                           radius=lr, fill=255)
    img.paste(logo, (lx, ly), mask)
    draw = ImageDraw.Draw(img)

    # double-arc mark (translate(86,74), scale2)
    ox, oy, sc = lx + 14, ly + 14, 2.0
    cx0, cy0 = ox + 7.5 * sc, oy + 12 * sc
    draw.ellipse([cx0 - 4.6, cy0 - 4.6, cx0 + 4.6, cy0 + 4.6], fill=(255, 255, 255))
    a1x, a1y, r1 = ox + 8.15 * sc, oy + 12 * sc, 6 * sc
    draw.arc([a1x - r1, a1y - r1, a1x + r1, a1y + r1], -50.06, 50.06,
             fill=(255, 255, 255), width=4)
    a2x, a2y, r2 = ox + 9.615 * sc, oy + 12 * sc, 9.2 * sc
    img = _blend(img, lambda d: d.arc(
        [a2x - r2, a2y - r2, a2x + r2, a2y + r2], -48.6, 48.6,
        fill=(255, 255, 255, 128), width=4))
    draw = ImageDraw.Draw(img)

    # brand text
    _draw_text(draw, (170, 96), "MIRI", _font(40, 800), T1, ls=1)
    _draw_text(draw, (170, 124), "공시 통계 카드", _font(22, 600), T3)

    # period pill (히어로 창 라벨)
    pf = _font(26, 700)
    pw = _text_w(draw, wlab, pf)
    ph, ppx = 48, 22
    pW = pw + ppx * 2
    pX, pY = R - pW, 62
    img = _blend(img, lambda d: d.rounded_rectangle(
        [pX, pY, pX + pW, pY + ph], radius=ph / 2, fill=(77, 148, 255, int(0.12 * 255))))
    img = _blend(img, lambda d: d.rounded_rectangle(
        [pX, pY, pX + pW, pY + ph], radius=ph / 2,
        outline=(77, 148, 255, int(0.35 * 255)), width=2))
    draw = ImageDraw.Draw(img)
    _draw_text(draw, (pX + ppx, pY + ph / 2 + 1), wlab, pf, BLUEINK, anchor_v="m")

    # divider
    draw.line([72, 170, 1008, 170], fill=LINE, width=1)

    # ---- STOCK: 종목명(폭 초과 시 56->48) + 코드 ----
    code = stock_code
    cf = _font(30, 600)
    code_w = _text_w(draw, code, cf)
    nm_size = 56
    nf = _font(nm_size, 800)
    if _text_w(draw, corp_name, nf, ls=-0.5) > 936 - code_w - 18:
        nm_size = 48
        nf = _font(nm_size, 800)
    _draw_text(draw, (72, 252), corp_name, nf, T1, ls=-0.5)
    nmw = _text_w(draw, corp_name, nf, ls=-0.5)
    _draw_text(draw, (72 + nmw + 18, 252), code, cf, T3)

    # ---- TITLE (report_nm, up to 2 lines) ----
    tf = _font(34, 600)
    lines = _wrap_lines(draw, report_nm, tf, 936, 2)
    for i, ln in enumerate(lines):
        _draw_text(draw, (72, 286 + 46 * (i + 1) - 10), ln, tf, T2)

    # ---- TRUST ROW: 표본 N + 등급 뱃지 ----
    trf = _font(30, 600)
    t_txt = ("과거 유사공시 " + str(n) + "건") if n is not None else "과거 유사공시 집계 중"
    _draw_text(draw, (72, 388), t_txt, trf, T2)
    tw = _text_w(draw, t_txt, trf)
    gf = _font(26, 700)
    gw = _text_w(draw, g_txt, gf)
    gh, gpx, gX, gY = 44, 18, 72 + tw + 20, 364
    img = _blend(img, lambda d: d.rounded_rectangle(
        [gX, gY, gX + gw + gpx * 2, gY + gh], radius=gh / 2, fill=g_bg))
    draw = ImageDraw.Draw(img)
    _draw_text(draw, (gX + gpx, gY + gh / 2 + 1), g_txt, gf, g_fg, anchor_v="m")

    # ---- HERO PANEL ----
    hx, hy, hw, hh, hr = 72, 430, 936, 280, 28
    draw.rounded_rectangle([hx, hy, hx + hw, hy + hh], radius=hr, fill=CARD)
    # 상단 화이트 미세 그라디언트 오버레이 rgba(255,255,255,0.03)->0
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ovd = ImageDraw.Draw(ov)
    for yy in range(hh):
        a = int(round(0.03 * 255 * (1 - yy / hh)))
        if a <= 0:
            break
        ovd.line([hx, hy + yy, hx + hw, hy + yy], fill=(255, 255, 255, a))
    ovmask = Image.new("L", img.size, 0)
    ImageDraw.Draw(ovmask).rounded_rectangle([hx, hy, hx + hw, hy + hh],
                                             radius=hr, fill=255)
    ov.putalpha(Image.composite(ov.getchannel("A"),
                                Image.new("L", img.size, 0), ovmask))
    img = Image.alpha_composite(img, ov)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([hx, hy, hx + hw, hy + hh], radius=hr,
                           outline=LINE, width=2)

    _draw_text(draw, (CX, 500), wlab + " 뒤 " + (VALCAP.get(valmode) or "평균 등락"),
               _font(30, 600), T3, align="center")
    _draw_text(draw, (CX, 648), ptxt, _font(148, 800), hero_color,
               ls=-2, align="center")

    # ---- PROBABILITY (up 없으면 전체 생략) ----
    if up_pct is not None:
        pbf = _font(32, 700)
        _draw_text(draw, (72, 758), "▲ 상승 " + str(up_pct) + "%", pbf, UP)
        _draw_text(draw, (1008, 758), "하락 " + str(dn_pct) + "% ▼", pbf, DOWN,
                   align="right")
        bx, by, bw, bh, br, gap = 72, 782, 936, 42, 21, 6
        up_w = int(round(bw * (up_pct / 100))) - gap // 2
        dn_x = bx + up_w + gap
        dn_w = bw - up_w - gap
        if up_w > 0:
            draw.rounded_rectangle([bx, by, bx + up_w, by + bh], radius=br, fill=UP)
        if dn_w > 0:
            draw.rounded_rectangle([dn_x, by, dn_x + dn_w, by + bh], radius=br, fill=DOWN)

    # ---- FOOTER ----
    draw.line([72, 986, 1008, 986], fill=LINE, width=1)
    _draw_text(draw, (72, 1026),
               "과거 통계이며 미래 수익을 보장하지 않습니다 · 출처 DART",
               _font(23, 500), T4)
    _draw_text(draw, (1008, 1026), "rimi-s76t.onrender.com",
               _font(23, 700), T4, align="right")

    return img


def render_card_file(item, out_path, valmode="avg", win=None):
    """카드 렌더 후 PNG 저장. 저장 경로(str) 반환."""
    img = render_card(item, valmode=valmode, win=win)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(str(out), "PNG")
    return str(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    valmode, win = "avg", DEFWIN
    pos = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--valmode" and i + 1 < len(argv):
            valmode = argv[i + 1]
            i += 2
            continue
        if a == "--win" and i + 1 < len(argv):
            win = argv[i + 1]
            i += 2
            continue
        pos.append(a)
        i += 1

    if len(pos) < 4:
        print("usage: python card_render.py <stock_code> <report_nm> <corp_name> "
              "<rcept_no> [out.png] [--valmode avg|med|car] [--win d1|w1|m1]")
        return 2
    if valmode not in VALCAP:
        valmode = "avg"
    if win not in WL:
        win = DEFWIN

    stock_code, report_nm, corp_name, rcept_no = pos[0], pos[1], pos[2], pos[3]
    out = pos[4] if len(pos) >= 5 else f"card_{rcept_no}.png"

    item = {"corp_name": corp_name, "stock_code": stock_code,
            "report_nm": report_nm, "rcept_no": rcept_no}

    tags = summarize.classify(report_nm)
    imp = impact.impact_for_tags(tags)
    item["impact"] = imp

    status = imp.get("status")
    w = (imp.get("windows") or {}).get(win) or {}
    pv, pu = val_pick(w, valmode)
    ptxt, _ = fmt_pct(pv)
    print(f"[card] corp={corp_name} code={stock_code} rcept={rcept_no}")
    print(f"[card] classify('{report_nm}') -> tags={tags}")
    print(f"[card] impact status={status} "
          f"matched={imp.get('matched_tag')} source={imp.get('source')} "
          f"grade={imp.get('grade')} conf={imp.get('confidence')}")
    if status == "ok":
        print(f"[card] win={win}({WL[win]}) valmode={valmode} "
              f"n={w.get('n')} val={pv} up_prob={pu} hero='{ptxt}'")
    else:
        print(f"[card] pending: {imp.get('message')}")

    path = render_card_file(item, out, valmode=valmode, win=win)
    print(f"[card] saved -> {Path(path).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
