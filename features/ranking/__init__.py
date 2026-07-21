# -*- coding: utf-8 -*-
"""ranking 피처: 오늘 공시 많은 종목·급등락·화제도(조회급등 프록시) 랭킹 payload.

순수 데이터 모듈. app.py 무수정. /api/ranking 통합은 venture-backend 담당.
"""
from .ranking import build_ranking_payload  # noqa: F401
