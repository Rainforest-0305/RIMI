# RIMI · morning_brief (아침 공시 브리핑 생성기)

격리 신규 모듈. `bench_cache` 캐시로 **0콜** 아침 브리핑을 생성하고, 과거
영향벤치(event study)와 매칭해 유형별 "과거 평균 영향" 한 줄을 붙인다.
선택적으로 예산 내에서 DART 라이브 1회 실증(`--live`)한다.

## 용도
- 최신 분기 공시 덤프에서 최근 공시를 **유형별로 분류·요약**한 플레인텍스트 브리핑.
- 두 섹션: (1) 최신 접수 top-N(recency), (2) 주요사항 하이라이트(유상증자·전환사채·
  자사주·합병분할·공급계약·최대주주변경 등 실질 이벤트만 최신순 — 마감일 분기보고서
  홍수에 묻히는 material 공시를 부각).
- 각 유형에 과거 벤치(익일/1개월 CAR, 상승확률, 표본, 신뢰) 한 줄 첨부.

## 실행법 (둘 다 지원)
```bash
# 1) 모듈 실행 (repo 루트에서)
python -m features.morning_brief.demo

# 2) 스크립트 직접 실행 (cwd 무관 — 절대경로 __file__ 기반 부트스트랩)
python features/morning_brief/demo.py

# 3) (선택) 라이브 실증: DART list.json 1회 조회로 '오늘'자 브리핑 시도
python -m features.morning_brief.demo --live
```
출력: 생성된 브리핑 전문 + 하단 스모크 실측(입력 공시수·카테고리/시장 분포·
top-N 유형분포·주요사항 유형분포·벤치 매칭수·라이브 DART 실측 콜수·소요초).
브리핑은 `features/morning_brief/out/morning_brief_latest.txt` 로도 저장(진짜 utf-8).

## 데이터 소스
- **소스1 (캐시, 0콜)**: `bench_cache/dart/` 최신 분기 덤프(파일명 끝 YYYYMMDD로 최신 선택).
  - 코스피: `A_`(정기공시) `B_`(주요사항) `I_`(거래소공시)
  - 코스닥: `K_A_` `K_B_` `K_I_` (동일 카테고리)
  - 주의: 파일 접두사 `A/B/I`는 **공시유형 카테고리**이지 시장이 아니다. 실제
    코스닥은 `K_*` 파일에만 있다(태스크 원문 "B_=코스닥"은 오기). 코스피+코스닥
    양시장 전부 사용 = 스코프 축소 없음.
  - row 키: `rcept_no, corp_code, corp_name, stock_code, report_nm, rcept_dt`.
- **소스2 (read-only)**: 과거 영향벤치. `config.IMPACT_BENCHMARK_FILE` 우선,
  없으면 repo 루트 `impact_benchmark.json` 폴백. dict, 키=공시유형(한국어)+`_meta`.
  각 유형에 `d/w/m` 구간별 `car_avg`(시장 EW 대비 초과수익)·`up_prob`·`n`·`conf`.
  없거나 매칭 안 되면 그 줄만 graceful 스킵.

## 공시유형 분류
`report_nm`(대괄호 정정 접두사 제거 후 본문)의 키워드 매칭으로 벤치 키에 정렬:
유상증자·무상증자·전환사채·자사주·주식소각·합병분할·배당·소송·공급계약·임상·
감사보고서·최대주주변경·지분변동·실적. 미매칭은 정정공시/기타공시.
키워드 방식이라 극소수 오분류 가능(예 "주권매매거래정지해제(감자…)"가 감자 키워드로
주식소각에 걸림) — 브리핑 요약 목적엔 허용 오차.

## 콜 예산 (실측)
- 기본(캐시): DART 라이브 **0콜**.
- `--live`: `dart_poll.fetch_markets(days=1, max_pages=2)` 1회 → 시장 2개(Y,K)
  페이지네이션. 실측 예시(2026-07-19 일요일, 무공시일): **2콜**(각 시장 013 무데이터
  1콜). `requests.get`을 래핑해 실측 카운트하며 예산 10 초과 시 errors에 표기.
  키(DART_API_KEY) 없으면 스킵. 네트워크/DART 실패도 graceful(캐시 브리핑은 유효).

## 격리 원칙
- 이 모듈은 `features/morning_brief/` 밖 파일을 편집하지 않는다.
- `build_impact_benchmark.py` / `dart_ownership.py` 는 import조차 하지 않는다(락).
- `config` / `dart_poll` 는 read-only import만. 모든 파일 IO `encoding='utf-8'` 명시.
