# 미리(MIRI) — 프론트+API 단일 컨테이너. 배포 이식성용(Render/Fly/Cloud Run 등).
FROM python:3.12-slim

WORKDIR /app
# 한글 폰트: 서버측 카드 렌더(card_render/tg_channel sendPhoto)의 CJK 두부박스 방지.
# slim 이미지는 CJK 폰트 미포함 → 나눔 폰트 설치(설치 후 apt 캐시 정리).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum \
    && rm -rf /var/lib/apt/lists/* && fc-cache -f
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# DART_API_KEY 는 런타임 환경변수로 주입(하드코딩 금지). PORT 기본 8000.
ENV PORT=8000
EXPOSE 8000

# $PORT 확장을 위해 sh -c 사용.
CMD ["sh", "-c", "uvicorn app:api --host 0.0.0.0 --port ${PORT}"]
