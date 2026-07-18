-- ============================================================
-- gongsi-alert 웹푸시 구독(push_subs) 스키마 (Supabase / PostgreSQL)
--
-- 적용: Partner 가 실 Supabase 프로젝트의 SQL Editor 에서 1회 실행.
--       (에이전트는 실 연결/실행 금지 = Partner 게이트)
--
-- push_store.py 가 REST(PostgREST)로 이 테이블을 읽고/쓴다.
--   - device_id  : 구독 소유 기기(X-Device-Id). 발송 대상 매핑에 사용.
--   - endpoint   : 브라우저 푸시 서비스 엔드포인트(고유). 재구독 시 upsert 기준.
--   - sub_json   : pushManager.subscribe() 전체 구독 객체(keys.p256dh/auth 포함).
--   - created_at : 생성시각.
--
-- 저장 전략: 엔드포인트 PK 로 upsert(Prefer: resolution=merge-duplicates).
--            발송 실패(410/404) 구독은 앱이 endpoint 로 자동 삭제한다.
-- ============================================================

create table if not exists push_subs (
    device_id   text not null,
    endpoint    text primary key,
    sub_json    jsonb not null,
    created_at  timestamptz not null default now()
);

-- 기기별 조회(발송 대상 매핑) 가속.
create index if not exists idx_push_subs_device on push_subs(device_id);

-- ------------------------------------------------------------
-- RLS(행수준 보안) — watch_* 테이블과 동일 정책:
--   RLS 활성. 서버(push_store)는 SERVICE_ROLE 키로 접근하여 RLS 를 우회한다.
--   ANON 키로는 정책이 없어 접근 차단(구독 데이터는 서버 경유로만 읽고/쓴다).
-- ------------------------------------------------------------
alter table push_subs enable row level security;
