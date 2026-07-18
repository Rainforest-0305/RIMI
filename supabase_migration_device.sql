-- ============================================================
-- gongsi-alert 관심종목 기기별(device_id) 분리 마이그레이션
--   (Supabase / PostgreSQL)  — A안(익명 기기 ID)
--
-- 목적: 지금까지 전 방문자가 공유하던 관심종목/그룹/키워드를 **기기 단위**로
--       격리한다. 기존 3개 테이블에 device_id 를 추가하고, PRIMARY KEY 를
--       (device_id, ...) 복합키로 재구성해 서로 다른 기기가 같은 종목코드/
--       그룹 id('default')/키워드를 각자 보유할 수 있게 한다.
--
-- 적용: Partner 가 실 Supabase SQL Editor 에서 1회 실행(에이전트 실행 금지).
--
-- ★ 배포 순서(중요): 이 DDL 을 **먼저** 적용한 뒤 코드(device_id 삽입)를
--   배포한다. 순서가 바뀌어 코드가 먼저 떠도 앱은 안전하다 — Supabase 쓰기/
--   읽기가 실패하면 watch_store 가 예외를 잡아 JSON 폴백으로 graceful 동작
--   (크래시 없음). 다만 DDL 적용 전까지 Supabase 영속은 반영되지 않는다.
--
-- 멱등: add column if not exists / drop constraint if exists 로 재실행 안전.
-- ============================================================

-- ------------------------------------------------------------
-- 1) device_id 컬럼 추가. 기존 행은 전부 'legacy'(=지금까지의 단일 공유 데이터).
--    default 'legacy' 라 기존 행/신규 삽입 누락 시에도 NOT NULL 위반 없음.
-- ------------------------------------------------------------
alter table watch_groups   add column if not exists device_id text not null default 'legacy';
alter table watch_stocks   add column if not exists device_id text not null default 'legacy';
alter table watch_keywords add column if not exists device_id text not null default 'legacy';

-- ------------------------------------------------------------
-- 2) PRIMARY KEY 를 (device_id, ...) 복합키로 재구성.
--    자식(watch_stocks)의 group_id FK 가 부모 PK 에 걸려 있으므로 FK 먼저 제거.
--
--    FK 는 재생성하지 않는다: 앱(watch_store)이 기기별 '스냅샷 전체 교체'
--    (해당 기기 delete-all → bulk insert, 삽입 순서 groups→stocks)로 참조
--    무결성을 항상 보장하고, normalize_state 가 default 그룹 존재를 강제한다.
--    (구 스키마의 on delete set default 는 복합키에서 device_id 까지 default
--    로 되돌려 타 기기로 행이 새는 위험이 있어 제거하는 편이 안전하다.)
-- ------------------------------------------------------------
alter table watch_stocks   drop constraint if exists watch_stocks_group_id_fkey;

alter table watch_groups   drop constraint if exists watch_groups_pkey;
alter table watch_stocks   drop constraint if exists watch_stocks_pkey;
alter table watch_keywords drop constraint if exists watch_keywords_pkey;

alter table watch_groups   add primary key (device_id, id);
alter table watch_stocks   add primary key (device_id, stock_code);
alter table watch_keywords add primary key (device_id, keyword);

-- ------------------------------------------------------------
-- 3) device_id 인덱스(기기 스코프 조회/삭제 가속).
--    복합 PK 의 선두 컬럼이 device_id 라 단독 인덱스가 없어도 되지만, 명시적
--    보조 인덱스로 계획을 안정화한다(if not exists 로 멱등).
-- ------------------------------------------------------------
create index if not exists idx_watch_groups_device   on watch_groups(device_id);
create index if not exists idx_watch_stocks_device    on watch_stocks(device_id);
create index if not exists idx_watch_keywords_device  on watch_keywords(device_id);

-- ------------------------------------------------------------
-- 4) RLS: 기존 정책 유지(3개 테이블 RLS 활성, anon 차단, service_role 우회).
--    device_id 는 서버(service_role)가 헤더에서 받아 필터하므로 정책 변경 불요.
--    (신규 프로젝트 재구성 시에만 아래 주석 해제)
-- alter table watch_groups   enable row level security;
-- alter table watch_stocks   enable row level security;
-- alter table watch_keywords enable row level security;

-- ============================================================
-- 5) 레거시 이관(claim): 기존 'legacy' 행을 President 기기 ID(=CLAIM_TOKEN)로
--    넘긴다. President 가 /?claim=<CLAIM_TOKEN> 로 1회 접속하면 프론트가 그
--    토큰을 자기 기기 ID 로 채택 → 아래 UPDATE 로 옮긴 데이터를 그대로 본다.
--
--    ★ 아래 __CLAIM_TOKEN__ 을 보고서에 별도 기재된 실제 토큰으로 치환 후 실행.
--      (토큰은 코드/레포에 하드코딩하지 않으며 보고서에만 있다.)
--    ★ 순서: 1~4 DDL 적용 → 이 UPDATE 실행 → President 가 claim 링크 접속.
-- ============================================================
update watch_groups   set device_id = '__CLAIM_TOKEN__' where device_id = 'legacy';
update watch_stocks   set device_id = '__CLAIM_TOKEN__' where device_id = 'legacy';
update watch_keywords set device_id = '__CLAIM_TOKEN__' where device_id = 'legacy';
