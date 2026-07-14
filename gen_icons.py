# -*- coding: utf-8 -*-
"""미리(MIRI) 더블아크 아이콘 생성(PIL). 512에서 그린 뒤 축소.
purpose=any(둥근 사각형) / maskable(풀블리드) 두 종류."""
from PIL import Image, ImageDraw
from pathlib import Path

HERE = Path(__file__).parent / "web"
BRAND = (0, 97, 255, 255)      # #0061FF
WHITE = (255, 255, 255, 255)
LTBLUE = (159, 192, 255, 255)  # #9FC0FF


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def draw_arcs(d, S):
    """두 개의 동심 아크(더블아크) + 끝점 도트."""
    sw = int(S * 0.066)
    # 바깥/안쪽 아크 bounding box (중심 살짝 우하향)
    cx, cy = S * 0.52, S * 0.5
    for (r, col) in ((S * 0.30, WHITE), (S * 0.40, LTBLUE)):
        box = [cx - r, cy - r, cx + r, cy + r]
        d.arc(box, start=150, end=300, fill=col, width=sw)
    # 끝점 도트(시작점 150도 근방)
    import math
    for (r, col) in ((S * 0.30, WHITE), (S * 0.40, LTBLUE)):
        a = math.radians(150)
        x = cx + r * math.cos(a)
        y = cy + r * math.sin(a)
        rr = sw * 0.62
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=col)


def make(fullbleed=False):
    S = 512
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if fullbleed:
        d.rectangle([0, 0, S, S], fill=BRAND)          # maskable: 풀블리드
        # 마스크 세이프존 안에 아크가 들어오도록 살짝 축소해서 그림
        inner = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        di = ImageDraw.Draw(inner)
        draw_arcs(di, S)
        inner = inner.resize((int(S * 0.78), int(S * 0.78)), Image.LANCZOS)
        img.alpha_composite(inner, (int(S * 0.11), int(S * 0.11)))
    else:
        bg = Image.new("RGBA", (S, S), BRAND)
        bg.putalpha(rounded_mask(S, int(S * 0.22)))
        img.alpha_composite(bg)
        draw_arcs(d, S)
    return img


def main():
    any512 = make(fullbleed=False)
    any512.save(HERE / "icon-512.png")
    any512.resize((192, 192), Image.LANCZOS).save(HERE / "icon-192.png")
    make(fullbleed=True).save(HERE / "icon-maskable-512.png")
    print("wrote icon-192.png, icon-512.png, icon-maskable-512.png")


if __name__ == "__main__":
    main()
