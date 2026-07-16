# -*- coding: utf-8 -*-
"""미리(MIRI) PWA 아이콘 생성.

정본 그라디언트 프로필(miri_profile_gradient_icon_512.png)을 소스로 사용해
Pillow만으로(무과금) 아이콘 세트를 재생성한다.

소스 특성(실측):
  - 512x512 RGBA, 풀블리드 라운드 스퀘어(코너 반경 ~105px ≈ 20.5%, 코너 투명)
  - 좌상=진파랑(#0061FF 계열) → 우하=연파랑 대각 그라디언트
  - 중앙에 밝은 마크(더블아크). 마크 범위 x30-66% / y27-73% → 세이프존 80% 내

생성물(web/):
  any(투명 라운드): icon-192.png, icon-512.png            = 소스 그대로(라운드·투명)
  maskable(풀블리드): icon-maskable-192.png, icon-maskable-512.png
                       = 대각 그라디언트 배경(불투명)으로 코너까지 채우고 소스 합성
  apple-touch-icon.png(180): 불투명 풀블리드(iOS는 투명 코너를 검게 처리하므로)
  favicon-32.png / favicon-16.png: 라운드 축소본
"""
from PIL import Image
from pathlib import Path

HERE = Path(__file__).parent / "web"
SRC = Path(r"C:\Users\urimk\Downloads\MIRI_브랜드킷\profile\miri_profile_gradient_icon_512.png")

# 소스 4코너 근방 불투명 색(실측) — maskable 배경 그라디언트용
TL = (7, 103, 255)
TR = (40, 128, 255)
BL = (0, 97, 255)
BR = (27, 118, 255)


def load_src():
    im = Image.open(SRC).convert("RGBA")
    if im.size != (512, 512):
        im = im.resize((512, 512), Image.LANCZOS)
    return im


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def bilinear_bg(S):
    """4코너 색을 쌍선형 보간한 불투명 그라디언트 배경(RGBA)."""
    bg = Image.new("RGBA", (S, S))
    px = bg.load()
    for y in range(S):
        v = y / (S - 1)
        for x in range(S):
            u = x / (S - 1)
            top = lerp(TL, TR, u)
            bot = lerp(BL, BR, u)
            c = lerp(top, bot, v)
            px[x, y] = (c[0], c[1], c[2], 255)
    return bg


def make_maskable(src, S):
    """풀블리드 불투명: 그라디언트 배경 + 소스 합성. 소스 투명코너는 배경이 메움.
    마크는 소스 기준 세이프존(중앙 80%) 내에 이미 위치."""
    bg = bilinear_bg(512)
    out = bg.copy()
    out.alpha_composite(src)  # 소스 불투명영역(그라디언트+마크) 위에 얹힘
    if S != 512:
        out = out.resize((S, S), Image.LANCZOS)
    return out


def main():
    HERE.mkdir(parents=True, exist_ok=True)
    src = load_src()

    # any: 소스 그대로(라운드·투명)
    src.save(HERE / "icon-512.png")
    src.resize((192, 192), Image.LANCZOS).save(HERE / "icon-192.png")

    # maskable: 풀블리드 불투명
    mask512 = make_maskable(src, 512)
    mask512.save(HERE / "icon-maskable-512.png")
    make_maskable(src, 192).save(HERE / "icon-maskable-192.png")

    # apple-touch-icon: 불투명 풀블리드(180). 알파채널 완전 제거(RGB)로 저장 —
    # iOS 홈화면 검은 코너 방지. index.html은 여전히 /icon-192.png 참조.
    make_maskable(src, 180).convert("RGB").save(HERE / "apple-touch-icon.png")

    # favicon 래스터 폴백(32, 그라디언트 마크). 풀블리드 불투명본을 축소.
    make_maskable(src, 32).save(HERE / "favicon.png")

    # 기존 라운드 투명 래스터 폴백도 유지. index.html은 icon.svg 참조 유지.
    src.resize((32, 32), Image.LANCZOS).save(HERE / "favicon-32.png")
    src.resize((16, 16), Image.LANCZOS).save(HERE / "favicon-16.png")

    print("wrote: icon-192, icon-512, icon-maskable-192, icon-maskable-512,")
    print("       apple-touch-icon(180,RGB), favicon(32), favicon-32, favicon-16")


if __name__ == "__main__":
    main()
