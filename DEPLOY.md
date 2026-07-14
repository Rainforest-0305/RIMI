# 미리(MIRI) 배포 · 공유 가이드

프론트(web/) + API(app:api) 를 **uvicorn 단일 서비스**로 서빙한다.
공유 방법은 두 가지: (A) 즉석 공개 URL(퀵터널), (B) 영구 배포(Render/Docker).

---

## A. 즉석 공개 URL — Cloudflare Quick Tunnel (무계정, 검증 완료)

로그인·도메인·과금 없이 `https://<랜덤>.trycloudflare.com` 공개 URL을 즉시 발급한다.
타인에게 그 URL만 주면 바로 접속된다. (계정 없는 터널은 **임시·데모용**. 창을 닫으면 URL 소멸.)

### 실행
```powershell
powershell -ExecutionPolicy Bypass -File .\tunnel_share.ps1
# 포트 변경:  -Port 9000
```
스크립트가 하는 일:
1. `uvicorn app:api --host 127.0.0.1 --port 8137` 기동 → `/api/health` 200 대기
2. `bin\cloudflared.exe tunnel --url http://localhost:8137` 로 공개 URL 발급
3. 콘솔에 공개 URL을 강조 출력. **Ctrl+C** 로 uvicorn·cloudflared 동시 정리.

### cloudflared 준비
- 포함된 포터블 바이너리: `bin\cloudflared.exe` (스크립트가 자동 사용, PATH 불필요).
- 없거나 다른 PC라면 아래를 `bin\cloudflared.exe` 로 저장:
  `https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe`
- winget MSI(`winget install Cloudflare.cloudflared`) 는 **관리자 권한(UAC)** 이 필요해
  비대화식 환경에서 실패한다(포터블 바이너리 권장).

### 대안: ngrok
```powershell
ngrok http 8137
```
단, ngrok 은 **무료라도 authtoken 가입이 필수**(`ngrok config add-authtoken <TOKEN>`).
cloudflared 퀵터널은 그런 가입이 없어 더 간단하다.

---

## B. 영구 배포 — Render.com 원클릭 (권장)

레포에 `render.yaml`(청사진)이 있어 Render가 설정을 자동 인식한다.

1. 이 폴더를 GitHub 레포로 push (`.env`·`bin/` 은 `.gitignore`로 제외됨).
2. Render 대시보드 → **New → Blueprint** → 레포 선택 → `render.yaml` 자동 감지.
3. 환경변수 **`DART_API_KEY`** 입력(대시보드에서만; `sync:false`라 레포엔 안 들어감).
   - 값 발급: https://opendart.fss.or.kr
4. Deploy. 빌드 `pip install -r requirements.txt`, 구동
   `uvicorn app:api --host 0.0.0.0 --port $PORT`, 헬스체크 `/api/health`.
5. 발급된 `https://miri-gongsi.onrender.com` 류 URL을 공유.

환경변수(코드 하드코딩 금지 — `config.py`가 런타임에 읽음):

| 키 | 필수 | 설명 |
|---|---|---|
| `DART_API_KEY` | 예 | DART OpenAPI 인증키 |
| `GONGSI_POLL_SEC` | 아니오 | 폴링 주기(초), 기본 300 |
| `PYTHON_VERSION` | 아니오 | 런타임, 기본 3.12.10 |

---

## C. Docker (Fly.io / Cloud Run / 자체 서버 이식용)

```bash
docker build -t miri .
docker run -p 8000:8000 -e DART_API_KEY=xxxx miri
# http://localhost:8000
```
`Dockerfile`은 `$PORT`(기본 8000)를 존중하므로 대부분의 PaaS에 그대로 올라간다.

---

## 배포 관련 파일

| 파일 | 용도 |
|---|---|
| `tunnel_share.ps1` | 퀵터널 공개 URL 발급 스크립트 |
| `render.yaml` | Render 원클릭 청사진 |
| `Dockerfile` | 컨테이너 이미지 |
| `Procfile` | Heroku 계열 프로세스 정의 |
| `.env.example` | 환경변수 템플릿 |
| `.gitignore` | `.env`·`bin/`·캐시 제외 |
| `bin/cloudflared.exe` | 포터블 터널 바이너리(커밋 제외) |

## 주의 / 잔여 한계
- **상태 영속성**: `watchlist.json`, `data/seen.json` 은 로컬 파일. Render 무료 티어의
  디스크는 재배포 시 **초기화**된다(관심종목/본 공시 기록 소실). 데모엔 무방하나,
  영구 유지가 필요하면 Render Disk(유료) 또는 외부 DB로 이전 필요.
- **키 소스**: 로컬에선 `config.py`가 `kis-trading\.env`를 읽지만, 클라우드엔 그 파일이
  없으므로 반드시 플랫폼 환경변수 `DART_API_KEY`를 주입해야 피드가 채워진다.
- 계정 없는 trycloudflare 터널은 가동시간 보장이 없다(영구용 아님).
