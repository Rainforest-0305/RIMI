# RIMI 메자닌(CB/BW/EB) 전환·행사 캘린더

전환사채(CB)·신주인수권부사채(BW)·교환사채(EB)의 전환/행사/교환 일정과
잠재 희석 물량을 한 곳에 모으는 독립 모듈. **라이브 DART 콜 = 0** (소스는
로컬 파싱본 캐시).

## 용도
- (a) 청구/행사 개시일 기준 시간순 **캘린더** — "언제 어느 종목의 전환/행사
  물량이 풀리는가"
- (b) 종목별 전환/행사 가능 **물량 집계** — 잠재 희석(오버행) 규모 파악
- (c) (선택) **price parity 스모크** — 전환/행사가 vs 현재가 괴리율(비-DART)

## 실행법 (둘 다 동작)
repo 루트(`gongsi-alert/`)에서:

```
python features/mezzanine_calendar/demo.py
python -m features.mezzanine_calendar.demo
```

옵션: `--no-parity` 를 붙이면 라이브 시세 조회(price parity)를 건너뛴다.

산출물: 콘솔 요약 + `features/mezzanine_calendar/mezz_demo_summary.json`
(utf-8, 격리 준수를 위해 모듈 디렉터리 안에만 기록).

## 데이터 소스
- `bench_cache/amounts/cvbdIsDecsn_*.json` — 전환사채(CB)
- `bench_cache/amounts/bdwtIsDecsn_*.json` — 신주인수권부사채(BW)
- `bench_cache/amounts/exbdIsDecsn_*.json` — 교환사채(EB)
- `data/corp_map.json` — stock_code→DART corp_code (역매핑으로
  corp_code→stock_code 산출, price parity용). **로컬 캐시, DART 콜 없음.**
- price parity 시세: pykrx(1순위)·FinanceDataReader(2순위) — **비-DART**.

각 공시 JSON은 `dict`(키=rcept_no, 값=공시 dict). 파일 1개에 복수 공시가
들어있어 파일 수(1219)보다 레코드 수(2547)가 많다.

### 필드 매핑 (실제 JSON key 확인 완료)
| 항목 | CB(cvbd) | BW(bdwt) | EB(exbd) |
|---|---|---|---|
| 전환/행사/교환가 | `cv_prc` | `ex_prc` | `ex_prc` |
| 가능 주식수 | `cvisstk_cnt` | `nstk_isstk_cnt` | `extg_stkcnt` |
| 발행주식대비% | `cvisstk_tisstk_vs` | `nstk_isstk_tisstk_vs` | `extg_tisstk_vs` |
| 개시일 | `cvrqpd_bgd` | `expd_bgd` | `exrqpd_bgd` |
| 종료일 | `cvrqpd_edd` | `expd_edd` | `exrqpd_edd` |
| 발행총액(공통) | `bd_fta` | | |
| 만기(공통) | `bd_mtd` | | |
| 종류(공통) | `bd_knd` | | |

한글 날짜("YYYY년 MM월 DD일")는 `collect.parse_kdate` 로 date 파싱, 실패 시
None + 카운트. 금액/주식수 콤마 문자열은 `parse_amount` 로 int 변환.
`bd_fta='-'` 같은 플레이스홀더 로우(정정/철회·해외분)는 파싱실패로 집계됨(정상).

## 콜 예산 실측
- **라이브 DART 콜 = 0** (모든 공시 데이터는 로컬 파싱본에서 읽음).
- price parity 시세만 pykrx/FDR 로 상위 N종목(기본 5) 라이브 조회 —
  DART 아님. `--no-parity` 로 완전 오프라인 실행 가능.

## 파일 구성
- `collect.py` — 세 타입 순회·정규화(`MezzRecord`), 파싱 유틸, corp_code→stock_code 역매핑
- `calendar_view.py` — `build_calendar`(시간순), `build_holdings`(종목별 물량 집계)
- `price_parity.py` — `run_parity_smoke`(비-DART 시세 괴리, try/except+스킵 카운트)
- `demo.py` — 전체 파이프라인 + 요약 출력/저장

## 격리 원칙
- 이 모듈은 `features/mezzanine_calendar/` 안에만 파일을 쓴다.
- `build_impact_benchmark.py`, `dart_ownership.py` 는 import·편집하지 않는다.
- features 밖 모듈(config 등)은 sys.path 삽입 후 절대 import(읽기 전용).
