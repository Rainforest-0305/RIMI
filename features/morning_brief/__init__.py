# -*- coding: utf-8 -*-
"""RIMI morning_brief 모듈.

아침 공시 브리핑 생성기(격리 모듈). 기본은 캐시(bench_cache)로 0콜.
- collect  : 최신 분기 캐시 덤프 로드 + 최근 top-N 추출 + 유형 분류
- brief    : top-N + 과거 영향벤치를 사람이 읽는 요약 텍스트로 생성
- live_today: (선택) 예산 남을 때 dart_poll.fetch_markets(days=1) 1회 실증
- demo     : 단독 실행 데모(두 실행법 모두 지원)

이 모듈은 features/morning_brief/ 밖 파일을 편집하지 않는다.
build_impact_benchmark.py / dart_ownership.py 는 import조차 하지 않는다.
"""
