# 미리(MIRI) — 프론트+API 단일 컨테이너. 배포 이식성용(Render/Fly/Cloud Run 등).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# DART_API_KEY 는 런타임 환경변수로 주입(하드코딩 금지). PORT 기본 8000.
ENV PORT=8000
EXPOSE 8000

# $PORT 확장을 위해 sh -c 사용.
CMD ["sh", "-c", "uvicorn app:api --host 0.0.0.0 --port ${PORT}"]
