# -*- coding: utf-8 -*-
"""시장 테마 등락 카드 생성기 (WS-22 렌더 레인 · 무과금 Pillow 전용).

데이터 소스: theme_data.py(pykrx 실측)의 fetch_market_themes / NoTradingDataError
를 직접 import 한다. theme_card 는 렌더·CLI 전담이며 데이터 취득 로직이 없다.

card_render.py 의 MIRI 브랜드 프리미티브(팔레트·폰트폴백·그라디언트·텍스트
헬퍼·더블아크 마크)를 **자체 복제**하여 공시코어와 완전 독립으로 동작한다.
(card_render 를 import 하지 않는다 — summarize/impact 사이드이펙트 회피.)

카드: 1200x675 가로형 PNG. 오직 사실만 표기(날짜·테마명·등락률·출처).
판단·권유성 문구 배제 — 사실만 표기.

CLI:
  python theme_card.py --mode morning|evening --date YYYY-MM-DD --out <path>
    morning  -> 헤더 "전일 시장 테마"
    evening  -> 헤더 "금일 시장 테마"

데이터 계약(theme_data.py 구현 · pykrx):
  fetch_market_themes(mode, date) -> {
      'date':'YYYY-MM-DD', 'source':'KRX',
      'items':[{'name':str,'pct':float}, ... 3~5개], 'universe':str }
  휴장 시 NoTradingDataError raise.
"""
import sys
import argparse
import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 실데이터 소스(데이터팀 제작 theme_data.py, pykrx 실측). 실함수/실예외를 직접 사용.
# theme_card 는 렌더·CLI 전담 — 데이터 취득 로직을 갖지 않는다. 휴장/비거래일엔
# theme_data 가 NoTradingDataError 를 raise -> CLI 가 비0 exit + 파일 미생성 처리.
from theme_data import fetch_market_themes, NoTradingDataError


# ---------------------------------------------------------------------------
# 정본 팔레트 (card_render.py 31~43행 자체 복제)
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
UP = (0xff, 0x5b, 0x64)     # 상승(붉은색)
DOWN = (0x4d, 0x94, 0xff)   # 하락(파랑)
LOGO_A = (0x2f, 0x86, 0xff)
LOGO_B = (0x00, 0x61, 0xff)


# ---------------------------------------------------------------------------
# 폰트: 한글=Malgun(정본 폴백로직), 라틴/숫자=OFL(canvas-fonts) · 폴백 안전
# ---------------------------------------------------------------------------
_FONT_REG = Path(r"C:\Windows\Fonts\malgun.ttf")
_FONT_BLD = Path(r"C:\Windows\Fonts\malgunbd.ttf")

_OFL_DIR = Path(r"C:\Users\urimk\.claude\skills\canvas-design\canvas-fonts")
_FONT_DISPLAY = _OFL_DIR / "BigShoulders-Bold.ttf"        # 워드마크/라틴 디스플레이
_FONT_MONO_BLD = _OFL_DIR / "GeistMono-Bold.ttf"          # 등락률 숫자
_FONT_MONO_REG = _OFL_DIR / "GeistMono-Regular.ttf"       # 라틴 소자(출처 등)

_font_cache = {}


def _font(size, weight):
    """한글 Malgun. weight>=700 -> malgunbd. 폴백 안전(정본 55~71행 패턴)."""
    bold = weight >= 700
    key = ("ko", int(size), bold)
    if key in _font_cache:
        return _font_cache[key]
    path = _FONT_BLD if bold else _FONT_REG
    try:
        f = ImageFont.truetype(str(path), int(size))
    except Exception:
        try:
            f = ImageFont.truetype(str(_FONT_REG), int(size))
        except Exception:
            f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def _font_ofl(path, size, fallback_weight):
    """OFL(라틴/숫자) 로드. 부재 시 Malgun 으로 우아하게 폴백."""
    key = (str(path), int(size))
    if key in _font_cache:
        return _font_cache[key]
    try:
        f = ImageFont.truetype(str(path), int(size))
    except Exception:
        f = _font(size, fallback_weight)
    _font_cache[key] = f
    return f


def _font_display(size):
    return _font_ofl(_FONT_DISPLAY, size, 800)


def _font_mono(size, bold=True):
    return _font_ofl(_FONT_MONO_BLD if bold else _FONT_MONO_REG, size,
                     700 if bold else 500)


# ---------------------------------------------------------------------------
# 저수준 헬퍼 (card_render.py 자체 복제)
# ---------------------------------------------------------------------------
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


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
    """baseline 정렬 텍스트. align=left/center/right, anchor_v='s'/'m'."""
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


def _vgrad(w, h, c0, c1):
    """수직 선형 그라디언트 RGB 이미지."""
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / (h - 1)
        px[0, y] = tuple(int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3))
    return col.resize((w, h))


def _blend(img, fn):
    """투명 오버레이 레이어에 fn(draw)로 반투명 도형 후 알파 합성."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    fn(d)
    return Image.alpha_composite(img, layer)


# ---------------------------------------------------------------------------
# MIRI 더블아크 마크 (card_render.py 262~282행 인라인 로직 -> 함수 추출)
# ---------------------------------------------------------------------------
def _draw_mark(img, x, y, box, radius):
    """로고박스(그라디언트 rounded-rect 마스크 paste) + 이중호 마크.

    card_render 정본은 box=76, pad=14, sc=2.0 를 상수로 썼다. 여기선 box 에
    비례해 pad/sc/radius 를 스케일하여 임의 크기·좌표에 재배치 가능.
    반투명 바깥호(_blend) 때문에 새 img 를 반환하므로 반드시 재대입할 것.
    """
    # ---- 로고박스: 대각(근사 수직) 그라디언트를 rounded-rect 마스크로 paste
    logo = _vgrad(box, box, LOGO_A, LOGO_B)
    mask = Image.new("L", (box, box), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, box - 1, box - 1],
                                           radius=radius, fill=255)
    img.paste(logo, (x, y), mask)

    # ---- 이중호: 정본 geometry 를 sc(=box/38) 로 스케일
    sc = box / 38.0
    ox = x + box * 14.0 / 76.0
    oy = y + box * 14.0 / 76.0
    W = (255, 255, 255)

    d = ImageDraw.Draw(img)
    # 중심 원
    cx0, cy0 = ox + 7.5 * sc, oy + 12 * sc
    r0 = 2.3 * sc
    d.ellipse([cx0 - r0, cy0 - r0, cx0 + r0, cy0 + r0], fill=W)
    # 안쪽 호(불투명)
    a1x, a1y, r1 = ox + 8.15 * sc, oy + 12 * sc, 6 * sc
    aw = max(2, int(round(2.0 * sc)))
    d.arc([a1x - r1, a1y - r1, a1x + r1, a1y + r1], -50.06, 50.06,
          fill=W, width=aw)
    # 바깥 호(알파 128 반투명 · _blend 로 합성)
    a2x, a2y, r2 = ox + 9.615 * sc, oy + 12 * sc, 9.2 * sc
    img = _blend(img, lambda dd: dd.arc(
        [a2x - r2, a2y - r2, a2x + r2, a2y + r2], -48.6, 48.6,
        fill=(255, 255, 255, 128), width=aw))
    return img


# ---------------------------------------------------------------------------
# 포맷 헬퍼 (오직 사실 · 부호+1자리 소수)
# ---------------------------------------------------------------------------
def fmt_pct(v):
    """등락률 -> (표시문구, 방향). 판단 문구 없이 실측 숫자만."""
    s = ("+" if v > 0 else "") + f"{float(v):.1f}%"
    cls = "up" if v > 0 else ("down" if v < 0 else "flat")
    return s, cls


def _fmt_date_dot(date):
    """'YYYY-MM-DD' -> 'YYYY.MM.DD' (표기 통일)."""
    try:
        d = datetime.date.fromisoformat(date)
        return f"{d.year}.{d.month:02d}.{d.day:02d}"
    except Exception:
        return str(date).replace("-", ".")


HEADERS = {"morning": "전일 시장 테마", "evening": "금일 시장 테마"}


def _subtitle_for(universe):
    """universe 설명문으로 부제를 판정(설명문 원문은 카드에 출력하지 않음).

    폴백 경로(대표종목 거래대금 가중) 또는 업종지수 경로를 구분해 정직한 부제를
    돌려준다. 어떤 판단/권유 어휘도 포함하지 않는다(사실 라벨만)."""
    u = str(universe or "")
    if "업종지수" in u and "폴백" not in u:
        return "KRX 업종지수 등락률"
    # 폴백 경로 또는 업종지수 아님 -> 개별종목 거래대금 가중 실측
    return "등락률 실측 · 대표종목 거래대금 가중"


# ---------------------------------------------------------------------------
# 카드 렌더 (1200x675 가로형 · 새 그리드)
# ---------------------------------------------------------------------------
CARD_W, CARD_H = 1200, 675
MARGIN = 64
CONTENT_L = MARGIN
CONTENT_R = CARD_W - MARGIN   # 1136


def render_theme_card(mode, data):
    """mode('morning'|'evening') + data(dict) -> PIL.Image (1200x675 RGBA)."""
    items = list(data.get("items") or [])
    if not items:
        # 방어: 데이터 계약상 여기 도달 전에 NoTradingDataError 로 걸러진다.
        raise NoTradingDataError("표시할 테마 항목이 없음")
    items = items[:5]
    header = HEADERS.get(mode, HEADERS["evening"])
    date_txt = _fmt_date_dot(data.get("date", ""))
    source = str(data.get("source") or "KRX")

    # ---- 배경: 수직 그라디언트 ----
    img = _vgrad(CARD_W, CARD_H, BG0, BG1).convert("RGBA")

    # ---- 상단 블루 글로우(radial, center 상단) ----
    def _glow(d):
        cxg, cyg = CARD_W / 2, -80
        steps = 80
        for i in range(steps, 0, -1):
            frac = i / steps
            r = 60 + (820 - 60) * frac
            alpha = int(round(0.09 * (1 - frac) * 255))
            d.ellipse([cxg - r, cyg - r, cxg + r, cyg + r],
                      fill=(77, 148, 255, alpha))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    _glow(ImageDraw.Draw(layer))
    layer.paste((0, 0, 0, 0), (0, 380, CARD_W, CARD_H))
    img = Image.alpha_composite(img, layer)

    draw = ImageDraw.Draw(img)
    # ---- 외곽 라운드 보더 ----
    draw.rounded_rectangle([6, 6, CARD_W - 7, CARD_H - 7], radius=30,
                           outline=LINE, width=2)

    # ================= HEADER ROW (마크 + 워드마크 + 날짜) =================
    box = 64
    lx, ly = CONTENT_L, 46
    img = _draw_mark(img, lx, ly, box, radius=16)
    draw = ImageDraw.Draw(img)

    wm_x = lx + box + 20
    # "MIRI" 워드마크(BigShoulders-Bold), 서브 라틴 라벨
    _draw_text(draw, (wm_x, ly + 30), "MIRI", _font_display(44), T1, ls=1)
    _draw_text(draw, (wm_x + 2, ly + 58), "MARKET THEMES",
               _font_mono(15, bold=False), T3, ls=3)

    # 날짜(모노, 우측). 출처 소자와 상단 라벨.
    _draw_text(draw, (CONTENT_R, ly + 20), date_txt,
               _font_mono(30, bold=True), T2, align="right")
    _draw_text(draw, (CONTENT_R, ly + 50), source,
               _font_mono(16, bold=False), T4, align="right", ls=3)

    # ================= HEADER TITLE =================
    _draw_text(draw, (CONTENT_L, 188), header, _font(50, 800), T1, ls=-1)
    # 부제: universe 설명문을 판정 구동(설명문 원문 미출력)
    _draw_text(draw, (CONTENT_L + 2, 214),
               _subtitle_for(data.get("universe")),
               _font(22, 500), T3)

    # 헤더 divider
    draw.line([CONTENT_L, 250, CONTENT_R, 250], fill=LINE, width=1)

    # ================= LIST =================
    list_top, list_bot = 268, 588
    n = len(items)
    row_h = (list_bot - list_top) / n
    name_max_w = 640  # rank/pct 사이 여유

    for i, it in enumerate(items):
        cy = list_top + row_h * i + row_h / 2.0
        name = str(it.get("name", "") or "")
        pct = float(it.get("pct", 0.0))
        ptxt, pcls = fmt_pct(pct)
        color = UP if pcls == "up" else (DOWN if pcls == "down" else T3)

        # rank (모노, 소자)
        _draw_text(draw, (CONTENT_L, cy), f"{i + 1:02d}",
                   _font_mono(28, bold=True), T4, anchor_v="m")

        # theme name (Malgun bold), 길면 축소
        nm_size = 40
        nf = _font(nm_size, 800)
        name_x = CONTENT_L + 78
        if _text_w(draw, name, nf, ls=-0.5) > name_max_w:
            nm_size = 32
            nf = _font(nm_size, 800)
        _draw_text(draw, (name_x, cy), name, nf, T1, ls=-0.5, anchor_v="m")

        # 등락률 (GeistMono-Bold, 방향색)
        _draw_text(draw, (CONTENT_R, cy), ptxt,
                   _font_mono(46, bold=True), color, align="right", anchor_v="m")

        # 방향 삼각(소자)
        tri = "▲" if pcls == "up" else ("▼" if pcls == "down" else "")
        if tri:
            pw = _text_w(draw, ptxt, _font_mono(46, bold=True))
            _draw_text(draw, (CONTENT_R - pw - 16, cy), tri,
                       _font(20, 700), color, align="right", anchor_v="m")

        # row separator (마지막 제외)
        if i < n - 1:
            sy = list_top + row_h * (i + 1)
            draw.line([CONTENT_L, sy, CONTENT_R, sy], fill=LINE, width=1)

    # ================= FOOTER =================
    draw.line([CONTENT_L, 610, CONTENT_R, 610], fill=LINE, width=1)
    fy = 640
    # 채널 비종속 워드마크("미리"=Malgun 한글, "MIRI"=BigShoulders 라틴) + 규제 라벨
    # (핸들 제거: WS-25 텔레그램 등 재사용 시 채널 혼선 방지. 워드마크는 채널 무관)
    x0 = CONTENT_L
    ko_f = _font(22, 700)
    _draw_text(draw, (x0, fy), "미리 ", ko_f, T3, anchor_v="m")
    x0 += _text_w(draw, "미리 ", ko_f)
    lat_f = _font_display(24)
    _draw_text(draw, (x0, fy), "MIRI", lat_f, T3, ls=0.5, anchor_v="m")
    x0 += _text_w(draw, "MIRI", lat_f, ls=0.5)
    _draw_text(draw, (x0 + 10, fy), "· 정보 제공용, 투자 권유 아님",
               _font(22, 500), T4, anchor_v="m")
    # 출처(우측 소자): 한글 라벨=Malgun, 코드=mono (GeistMono 한글 글리프 부재)
    src_font = _font_mono(20, bold=False)
    _draw_text(draw, (CONTENT_R, fy), source, src_font, T4,
               align="right", anchor_v="m")
    _sw = _text_w(draw, source, src_font)
    _draw_text(draw, (CONTENT_R - _sw - 8, fy), "출처", _font(20, 500), T4,
               align="right", anchor_v="m")

    return img


def render_theme_card_file(mode, data, out_path):
    """렌더 후 PNG 저장. 저장 경로(str) 반환."""
    img = render_theme_card(mode, data)
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
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="MIRI 시장 테마 등락 카드 (1200x675 PNG)")
    ap.add_argument("--mode", required=True, choices=["morning", "evening"])
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out", required=True, help="출력 PNG 경로")
    args = ap.parse_args(argv)

    try:
        data = fetch_market_themes(args.mode, args.date)
    except NoTradingDataError as e:
        # 휴장/비거래일: 빈 카드 절대 생성 금지. 파일 미생성 + 비0 종료.
        print(f"[theme_card] NoTradingData: {e} "
              f"(mode={args.mode} date={args.date}) — 카드 생성하지 않음",
              file=sys.stderr)
        return 3

    path = render_theme_card_file(args.mode, data, args.out)
    print(f"[theme_card] mode={args.mode} date={data.get('date')} "
          f"items={len(data.get('items') or [])} source={data.get('source')}")
    print(f"[theme_card] saved -> {Path(path).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
