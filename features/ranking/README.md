# features/ranking — 랭킹/화제 탭 데이터 모듈

"오늘 공시 많은 종목 · 급등락 · 화제도(조회급등 프록시)" 랭킹 payload를 만드는
**순수 데이터 모듈**. `app.py` 무수정. `/api/ranking` 통합은 venture-backend 담당.

## 파일
- `ranking.py` — 순수 계산(`build_ranking_payload(feed, price_fn=None, ...)`). I/O 없음.
- `price_adapter.py` — TOSS 시세 read-only 어댑터(kis-trading/toss_data.py 경량 복사판).
- `demo.py` — 실제 피드 + 실제 TOSS 시세로 payload 생성 후 실측 콘솔 출력.
- `__init__.py` — `build_ranking_payload` 재노출.

## 실행
```
python features/ranking/demo.py
```

## 데이터 소스 & 콜예산
| 파트 | 소스 | 방식 | 콜수(실측 2026-07-21) |
|------|------|------|------|
| disclosure_hot / buzz | `app._get_feed(force=False)` (DART list.json 피드) | 캐시 피드 1회 취득. force 폴링 안 함 | DART GET 10 (콜드빌드 시 시장 2 × 페이지 ≤5) |
| price_movers | `price_adapter.movers_for` (TOSS `/api/v1/candles`) | 후보 상위 N(cand_cap=20)만 종목당 일봉 1콜 | TOSS GET 20 |

- 피드가 캐시 신선(60s TTL)이면 DART 0콜. 콜드/만료 시에만 위 콜드빌드 콜.
- TOSS 토큰은 **재사용**(kis-trading/.toss_token.json read-only). 신규발급 필요 시에만
  1콜 추가하며 저장은 이 폴더 로컬 캐시(`.toss_token.local.json`)로 — kis-trading 무수정.
- 앱키 thrash 회피: 후보를 `cand_cap`(기본 20)으로 상한, 토큰 캐시 재사용.

## payload 스키마 (확정)
```jsonc
{
  "generated_at": "YYYY-MM-DD HH:MM:SS",   // payload 생성시각
  "feed_generated_at": "...",              // 원 피드 생성시각
  "ref_day": "YYYY-MM-DD",                 // 피드 내 최신 접수일(=랭킹 기준일)
  "disclosure_hot": [                       // 공시빈도 랭킹(최근성·중요도 가중)
    {"code","name","market","count","score","types":[..],"latest_rcept_no"}
  ],
  "buzz": [                                 // 화제도(조회급등 프록시)
    {"code","name","market","recent_count","prior_count","buzz_score"}
  ],
  "price_movers": {                         // 급등락(전일대비)
    "gainers": [{"code","name","market","price","change_pct","volume"}],
    "losers":  [{"code","name","market","price","change_pct","volume"}]
  },
  "meta": {
    "feed_alerts","ranked_stocks","candidate_cap","price_candidates",
    "price_meta": {"requested","resolved","toss_calls","errors","degraded","reason"},
    "buzz_proxy": "...",                    // 프록시 정의 문구(아래)
    "recency_half_life_days": 2.0,
    "tag_weight": { ... }                   // 유형별 중요도 가중표
  }
}
```

### 피드 item 실측 스키마(app.py `_build_feed`, 참고)
`rcept_no`(14자리=YYYYMMDD+일련), `corp_name`, `stock_code`(6자리), `corp_cls`(Y/K),
`market`(KOSPI/KOSDAQ), `report_nm`, `flr_nm`, `rcept_dt`(YYYYMMDD, **일 해상도**),
`tags`(list), `summary`, `impact`, `url`, `is_new` 등. 피드는 이미 IMPACT_TAGS
노이즈필터를 통과한 실질공시만 담는다.

## 스코어 정의
- **recency 가중**: `0.5 ** (days_ago / 2.0)` (반감기 2일). 기준일=피드 최신 접수일.
- **중요도(materiality) 가중**: 유형(tags)별 `tag_weight`의 최댓값. 유상증자·전환사채·
  합병분할·최대주주변경 2.0, 공급계약·임상·자사주 등 1.5, 실적·감사 1.0.
- **disclosure_hot.score** = Σ(중요도 × recency). `count`는 순수 건수. 동점은 최신 rcept_no.
- **buzz_score** = recent_w × (1 + max(0, recent_c − prior_c) / (prior_c + 1)).

## 프록시 명시 (없는 데이터를 지어내지 않음)
- **조회급등/화제도(buzz)**: 이 피드에는 **실제 조회수 데이터가 없다.** 대신
  "최근 창(기본 1일) 공시 급증(acceleration)"을 프록시로 쓴다 — 최근 창 공시활동이
  이전 창 대비 가속된 종목을 화제도로 근사한다. `meta.buzz_proxy`에 명시된다.
  실 조회수 소스가 생기면 이 파트만 교체하면 된다.

## 한계
- `rcept_dt`가 일 해상도라 일중(intraday) 급증은 rcept_no 일련으로만 근사한다.
- 코드 결측(비상장/코드 없는) 공시는 랭킹·시세 대상에서 제외한다.
- `price_movers`는 TOSS 일봉 2개 이상 필요. 신규상장/거래정지 등은 산출 제외.
- TOSS 401/네트워크 실패 시 movers 파트만 graceful degrade(빈 movers + 사유),
  disclosure_hot/buzz는 실측 완주(`meta.price_meta.degraded/reason` 참조).

## 실측 (demo, 2026-07-21 17:22, 장중)
- 표본: feed alerts 127, 랭킹 대상 종목 116, 시세조회 후보 20, 시세 산출 20/20.
- 콜수: DART GET 10(force=False, cached=False 콜드빌드), TOSS GET 20(token 재발급 0).
- 소요: feed 5.82s + payload(시세) 11.49s = 총 17.74s.
