-- ============================================================
-- gongsi-alert 관심종목 영속 스키마 (Supabase / PostgreSQL)
--
-- 적용: Partner 가 실 Supabase 프로젝트의 SQL Editor 에서 1회 실행.
--       (에이전트는 실 연결/실행 금지 = Partner 게이트)
--
-- watch_store.py 가 REST(PostgREST)로 아래 3개 테이블을 읽고/쓴다.
--   - watch_groups  : 그룹 (id="default" 는 시스템 기본, 삭제 불가는 앱에서 보장)
--   - watch_stocks  : 관심종목 (group_id 로 그룹 소속)
--   - watch_keywords: 제목 부분매칭 추가 알림 키워드
--
-- 저장 전략: 앱은 스냅샷 전체 교체(delete-all + bulk insert)를 쓴다.
-- ============================================================

create table if not exists watch_groups (
    id          text primary key,
    name        text not null,
    sort_order  integer not null default 0
);

create table if not exists watch_stocks (
    stock_code  text primary key,
    name        text not null,
    group_id    text not null default 'default'
                 references watch_groups(id) on delete set default,
    sort_order  integer not null default 0
);

create table if not exists watch_keywords (
    keyword     text primary key
);

create index if not exists idx_watch_stocks_group on watch_stocks(group_id);

-- 시스템 기본 그룹 시드(존재하지 않을 때만).
insert into watch_groups (id, name, sort_order)
values ('default', '기본', 0)
on conflict (id) do nothing;

-- ------------------------------------------------------------
-- RLS(행수준 보안) — 실 적용 상태(2026-07, Partner 적용·왕복검증 완료):
--   3개 테이블 모두 RLS **활성**이며 필수다. 서버(watch_store)는 SERVICE_ROLE
--   키로 접근하여 RLS 를 우회(전권)하고, ANON 키로는 정책이 없어 SELECT 가
--   차단된다(빈 배열 반환). 즉 관심종목 데이터는 서버(service_role) 경유로만
--   읽고/쓴다. 아래 ALTER 는 신규 프로젝트 재구성 시 RLS 를 다시 켜기 위한 것.
--   (정책을 별도로 열지 않는 한 anon/public 접근은 차단 유지)
-- ------------------------------------------------------------
alter table watch_groups   enable row level security;
alter table watch_stocks   enable row level security;
alter table watch_keywords enable row level security;
