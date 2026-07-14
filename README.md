# 미리 · MIRI — 코스피 공시 알림 웹앱

코스피 **전 종목** 신규 공시를 감지해 **핵심 3줄 요약 + 영향 태그 + 과거 유사공시 통계**
(1일/1주/1개월 시장보정 초과등락·상승/하락 건수·표본수·신뢰도 등급)로 보여주는 설치형(PWA)
웹앱. **매매추천 없음 — 순수 정보·요약·분류·과거 사실 통계.**

> 규제 톤: "○○% 오른다" 같은 예측·권유 표현을 배제하고 **"과거 N건 중 M건 상승(시장보정
> 중앙값 +X%)"** 사실 통계로만 프레이밍. 모든 카드에 "예측·투자권유 아님 / 원문 대체 아님"
> 상시 고지 + DART 원문 링크.

## 핵심 기능
- **시장 전체 폴링**: DART `list.json` 을 `corp_cls=Y`(코스피 전체)로 폴링(관심목록 한정 아님).
  단일 요청으로 최근 수백 건을 조회 → 노트북/DART 유량에 유리. 신규(seen 원자적) 감지·중복방지.
- **관심종목**: 아무 코스피 6자리 코드나 등록(corp_code 로 유효성 검증·이름 자동해석). 피드에서 ★강조 + "관심종목" 탭 필터.
- **요약·분류**: 규칙기반 영향 태그(15종, 결정적) + 3줄 요약. LLM 훅(`summarize.set_llm_hook`) 유지, 없으면 규칙 폴백.
- **과거 영향 분석**: strat-data 산출 `data/impact_benchmark.json` 을 읽어 공시 유형별 과거 통계를
  카드에 매핑(유형 정확매칭 → 버킷 대분류 폴백 → 없으면 "집계 중"). 파일 없어도 에러 없음.
- **UI**: 드롭박스 블루(#0061FF)·더블아크 아이콘. 공시카드 + 1일/1주/1개월 토글 + 상승/하락 막대 + 신뢰도 배지. 모바일 우선 반응형, 라이트/다크, 하단 규제 고지.
- **PWA**: `manifest.json` + service worker + 더블아크 아이콘(192/512 png + svg + maskable) → "홈 화면에 추가" 설치 가능.

## 구성
| 파일 | 역할 |
|---|---|
| `config.py` | 키 로드(env `DART_API_KEY`; 로컬은 kis-trading/.env 읽기전용 폴백), 경로/주기 |
| `dart_poll.py` | DART 공시검색(list.json) — `fetch_market_disclosures`(corp_cls=Y 시장전체) + `fetch_disclosures`(종목별) + corp_code 매핑 |
| `summarize.py` | 영향 태그 분류(규칙) + 3줄 요약(`set_llm_hook` 으로 LLM 주입) |
| `impact.py` | 과거영향 벤치마크 리더(파일 없거나 유형 미집계 시 "집계 중" 폴백) |
| `notify_alert.py` | 텔레그램(테스트채널) + 콘솔/파일 폴백 알림 |
| `main.py` | 오케스트레이션(폴링→요약→알림), 중복방지(seen 원자적 저장) |
| `app.py` | **웹 API(FastAPI)** — 시장 피드 + 관심종목 CRUD + `web/` 정적 서빙(단일 프로세스) |
| `web/` | PWA 프론트(index.html·manifest.json·sw.js·아이콘) — 바닐라 JS SPA |
| `data/impact_benchmark.json` | 과거영향 벤치마크(현재 시드 placeholder — strat-data 산출로 교체 예정) |
| `render.yaml` · `Procfile` · `Dockerfile` | 배포 청사진 |

## 로컬 실행 (프론트+API 단일 uvicorn)
```bash
pip install -r requirements.txt
# DART 키: 로컬은 kis-trading/.env 를 자동으로 읽음. 분리 배포 시 아래처럼 주입:
#   (bash)  export DART_API_KEY=xxxx
#   (pwsh)  $env:DART_API_KEY="xxxx"
uvicorn app:api --host 127.0.0.1 --port 8137
```
브라우저에서 **http://127.0.0.1:8137/** 접속. 정적 프론트가 같은 오리진의 `/api/*` 를
호출하므로 CORS·별도 웹서버 불필요. 휴대폰 테스트는 `--host 0.0.0.0` 후 PC의 LAN IP:8137.

### API 엔드포인트
| 메서드·경로 | 역할 |
|---|---|
| `GET /api/alerts` | 코스피 시장 전체 최근 공시 피드(요약·태그·과거영향·NEW·관심여부). 60초 캐시 |
| `POST /api/poll` | 수동 새로고침(캐시 무효화 후 실 DART 재조회) |
| `GET /api/watchlist` | 관심종목·키워드 조회 |
| `POST /api/watchlist` | 관심종목 추가 `{"stock_code":"005930"}` (6자리, corp_code 검증·이름 자동해석) |
| `DELETE /api/watchlist/{code}` | 관심종목 삭제 |
| `GET /api/health` | 상태 점검(DART키·워치리스트·seen·benchmark_ready) |

## 원클릭 배포
DART 키는 **환경변수 `DART_API_KEY`** 로만 주입(코드/레포 하드코딩 금지).

### A) Render.com (render.yaml)
1. 이 폴더를 GitHub 레포로 푸시(`.env` 는 `.gitignore` 로 제외됨).
2. Render → New → Blueprint → 레포 선택 → `render.yaml` 자동 인식.
3. `DART_API_KEY` 환경변수만 대시보드에 입력 → Deploy. 헬스체크 `/api/health`.

### B) Docker (어디서나)
```bash
docker build -t miri .
docker run -e DART_API_KEY=xxxx -p 8000:8000 miri
# http://localhost:8000
```

### C) Heroku 계열(Procfile)
`Procfile` = `web: uvicorn app:api --host 0.0.0.0 --port $PORT`. 환경변수 `DART_API_KEY` 설정 후 배포.

> 참고: `data/corp_map.json`(코드→corp_code 캐시, ~87KB)·`data/impact_benchmark.json` 은
> 레포에 포함되어 최초 부팅부터 동작. `seen.json`/`watchlist.json` 은 런타임 파일(무상태 배포 시 초기화 무해).

## 배치 CLI (알림 상주/검증)
```bash
python main.py --status     # 설정/워치리스트 상태
python main.py --demo -n 3  # 실 DART 최근 공시 요약·분류 출력(콘솔 작동증명)
python main.py --once       # 1회 폴링(신규만 알림) — 크론/검증용
python main.py --loop       # 주기 상주 폴링(기본 5분)
```

## 안전/격리 원칙
- 트레이딩 시스템(`kis-trading`)과 **완전 격리**. `.env` 키는 **읽기만**, 프로세스·포트·상태파일 분리. 실계좌·주문 무관.
- 알림은 **본인 테스트 채널로만**(`GONGSI_TEST_CHAT_ID`). 외부 실유저 브로드캐스트 금지(Partner 승인 전).

## 과거영향 벤치마크 인터페이스 (strat-data 연동점)
`data/impact_benchmark.json` 스키마: `{ "_meta", "buckets": {대분류:[태그...]}, "types": {태그:{ "windows": {"d1"/"w1"/"m1": {"excess": %, "up_prob": 0~1, "n": 표본}}, "confidence": "A|B|C" }} }`.
strat-data 가 이 파일만 덮어쓰면 앱이 자동 반영. 현재 파일은 **시드 placeholder**(수치 예시)이며,
삭제/부재 시 앱은 카드에 "집계 중" 을 표시(에러 없음).

## 아이콘 재생성
`python gen_icons.py` → `web/icon-192.png`, `web/icon-512.png`, `web/icon-maskable-512.png` 생성(PIL).

## LLM 요약 승격 지점
```python
from summarize import set_llm_hook
set_llm_hook(lambda item, tags: ["요약1", "요약2", "요약3"])  # Claude/멀티에이전트 연결
```
