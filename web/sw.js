/* 미리(MIRI) service worker — 앱셸 캐시 + 오프라인 폴백.
   전략: 정적 자산은 cache-first, API(/api/*)는 network-only(항상 실시간 공시).
   설치 가능 요건(manifest + fetch 핸들러 + HTTPS/localhost)을 충족한다. */
const CACHE = 'miri-v30';   // v29→v30: ①뒤로가기 먹통 근본수정 — detailX/detailBack 의 addEventListener('click',closeDetail) 가 MouseEvent 를 1번 인자 fromPop(truthy)으로 넘겨 history.back() 이 통째로 건너뛰어짐 → openDetail 이 쌓은 엔트리가 스택에 유령으로 남아 닫은 뒤 뒤로가기 1회가 헛돌았다. 무인자 래퍼(()=>closeDetail())로 전환(Esc·closeMezz·closeAnalyst 는 이미 래퍼라 정상, 탭전환 closeDetail(true,true) 는 의도적 유지) ②간격체계 Phase4 — .tabpanel gap 하드코딩 12px → var(--sp-3). 5탭 섹션 간격이 표시밀도(6/12/18)에 편입돼 밀도 기능이 카드+섹션 전체에 작동(md=12px 동일 → 기본 밀도 픽셀 무변화). SHELL(index.html·shell.js) precache라 bump 필수(구셸 고착 방지)
// (이전) v28→v29: ①파란 세로선 잔상 근본수정 — .card.watched·.brief.hero-am 의 outset box-shadow(레이아웃 박스 밖 1.5px 띠 = 랭킹탭에선 아무 엘리먼트도 점유 안 하는 죽은 영역 → iOS 재도색 주체 없음)를 inset 으로 전환 ②간격체계 Phase0~2 — --sp-1~6/--inset-x/--pad-x 토큰 도입(--stack-gap=--sp-3 별칭, 밀도 6/12/18 렌더 불변), #formmsg:empty 가드, 세로 마이크로 패딩 제거(.pushrow·.rk-seg·.valwrap·.chips·.wl-grouprow) → 알람켜기↔관심피드 34px→12px. SHELL(index.html·shell.js) precache라 bump 필수(구셸 고착 방지)
// (이전) v27→v28: 랭킹 화면 파란 세로선 결함 4건 수정(①body/html 포커스링 차단 ②탭전환 시 상세·애널·시트 오버레이 즉시 언마운트(잔상 겹침) ③터치 포커스 잔존 차단(mousedown preventDefault, 키보드 링은 유지) ④.rk-row:focus-visible 명시). SHELL(index.html·shell.js) precache라 bump 필수(구셸 고착 방지)
// (이전) v26→v27: 애널리스트 종가 표기에 기준일 병기('7/24 종가 249,500원' — 지표카드 라벨·차트 라벨·안내문구 3곳 통일, current_asof 기준·연도 다르면 YY/M/D). SHELL(index.html·shell.js) precache라 bump 필수(구셸 고착 방지)
const DATA_CACHE = 'miri-data-v1';   // 읽기 API(/api/alerts·today·ranking·mezzanine) 응답 캐시(앱셸과 분리 → activate 정리에서 보존)
/* 41-a iOS 스플래시(11종) — 재방문·오프라인 즉시 렌더용 precache */
const SPLASH = [
  '/splash/splash-640x1136.png', '/splash/splash-750x1334.png', '/splash/splash-828x1792.png',
  '/splash/splash-1125x2436.png', '/splash/splash-1170x2532.png', '/splash/splash-1179x2556.png',
  '/splash/splash-1206x2622.png', '/splash/splash-1242x2688.png', '/splash/splash-1284x2778.png',
  '/splash/splash-1290x2796.png', '/splash/splash-1320x2868.png'
];
const SHELL = ['/', '/index.html', '/manifest.json', '/app/shell.js',
  '/icon.svg', '/icon-192.png', '/icon-512.png', '/icon-maskable-192.png', '/icon-maskable-512.png',
  '/apple-touch-icon.png'].concat(SPLASH);

self.addEventListener('install', (e) => {
  // 개별 add + catch: 스플래시 1종이 없어도 install 이 브릭되지 않게(구셸 고착 방지, 가드레일③)
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  const keep = [CACHE, DATA_CACHE];
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => keep.indexOf(k) === -1).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

/* /api/alerts 변경 감지용 시그니처: 접수번호 목록만 비교(generated_at 등 휘발 필드 무시). */
function alertsSig(text) {
  try { const d = JSON.parse(text); return (d.alerts || []).map((a) => a.rcept_no).join(','); }
  catch (_) { return text; }
}
async function notifyClients(msg) {
  const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const c of list) { try { c.postMessage(msg); } catch (_) {} }
}
/* stale-while-revalidate: 캐시가 있으면 즉시 반환하고 백그라운드로 갱신.
   갱신본이 캐시와 다르면(신규/삭제 공시) 클라이언트에 알림 → 조용히 교체. */
async function swrAlerts(request) {
  const cache = await caches.open(DATA_CACHE);
  const cached = await cache.match(request);
  const netP = fetch(request).then(async (res) => {
    if (res && res.ok) {
      const toStore = res.clone();
      let changed = true;
      if (cached) {
        try { changed = alertsSig(await cached.clone().text()) !== alertsSig(await res.clone().text()); }
        catch (_) { changed = true; }
      }
      await cache.put(request, toStore);
      if (cached && changed) notifyClients({ type: 'alerts-updated' });
    }
    return res;
  }).catch(() => null);
  if (cached) { netP.catch(() => {}); return cached; }        // 재방문: 캐시 즉시 표시
  const res = await netP;
  if (res) return res;                                         // 최초 방문: 네트워크 대기
  return new Response(JSON.stringify({ alerts: [], offline: true, errors: [] }),
    { status: 200, headers: { 'Content-Type': 'application/json' } });  // 오프라인+캐시없음
}
/* 버그D: /api/today 변경 감지용 시그니처. 전체 바디 비교는 무거우니 안정 필드만
   조합(dataset_as_of + generated_at + overnight.count) — alertsSig와 동일 취지. */
function dataSig(text) {
  try {
    const d = JSON.parse(text);
    const ov = (d.overnight && typeof d.overnight.count === 'number') ? d.overnight.count : '';
    return String(d.dataset_as_of || '') + '|' + String(d.generated_at || '') + '|' + String(ov);
  } catch (_) { return text; }
}
/* 범용 SWR(today·ranking·mezzanine): 캐시 즉시 반환 + 백그라운드 revalidate로 캐시 갱신.
   ranking·mezzanine은 알림 불요(폴링 대상 아님) — 다음 진입/재조회에서 갱신본 수렴.
   /api/today 는 버그D(캐시 즉시반환이 낡은 브리핑을 보여줄 수 있음) 수정 대상이라 alerts와
   동일하게 캐시본↔네트워크본 시그니처가 다를 때만 notify한다(최초 방문·캐시 없음은 알림 제외). */
async function swrData(request, pathname) {
  const cache = await caches.open(DATA_CACHE);
  const cached = await cache.match(request);
  const netP = fetch(request).then(async (res) => {
    if (res && res.ok) {
      const toStore = res.clone();
      if (pathname === '/api/today' && cached) {
        let changed = true;
        try { changed = dataSig(await cached.clone().text()) !== dataSig(await res.clone().text()); }
        catch (_) { changed = true; }
        await cache.put(request, toStore);
        if (changed) notifyClients({ type: 'data-updated', path: pathname });
      } else {
        await cache.put(request, toStore).catch(() => {});
      }
    }
    return res;
  }).catch(() => null);
  if (cached) { netP.catch(() => {}); return cached; }         // 재방문: 캐시 즉시 표시(+백그라운드 갱신)
  const res = await netP;
  if (res) return res;                                          // 최초 방문: 네트워크 대기
  return new Response(JSON.stringify({ offline: true }),
    { status: 200, headers: { 'Content-Type': 'application/json' } });
}
/* SWR 대상 읽기 API 화이트리스트(개인화 없는 공용 데이터만). watchlist 등은 절대 불포함. */
const SWR_DATA_PATHS = { '/api/today': 1, '/api/ranking': 1, '/api/mezzanine': 1 };

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;          // Umami 등 외부 트래픽은 SW 미개입
  if (e.request.method !== 'GET') return;              // 등록/삭제(POST/DELETE)는 통과
  // ⚠️ 개인화 API는 절대 캐시 금지(가드레일②): watchlist 는 항상 네트워크(no-store)
  if (url.pathname.startsWith('/api/watchlist')) return;
  if (url.pathname === '/api/alerts') {                // 피드: SWR + 변경 notify(재방문 즉시표시+오프라인)
    e.respondWith(swrAlerts(e.request));
    return;
  }
  if (SWR_DATA_PATHS[url.pathname]) {                   // today·ranking·mezzanine: 범용 SWR(캐시 즉시+백그라운드 갱신)
    e.respondWith(swrData(e.request, url.pathname));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;        // 그 외 API(개인화·기타)는 항상 네트워크(실시간)
  // HTML/내비게이션은 network-first(항상 최신 UI), 실패 시에만 캐시
  const isHTML = e.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html');
  if (isHTML) {
    e.respondWith(
      fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      }).catch(() => caches.match(e.request).then((h) => h || caches.match('/index.html')))
    );
    return;
  }
  // 그 외 정적 자산(아이콘 등)은 cache-first
  e.respondWith(
    caches.match(e.request).then((hit) =>
      hit || fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      }).catch(() => caches.match('/index.html'))
    )
  );
});

/* ---------- 웹푸시(관심종목 신규 공시) ----------
   서버가 {title, body, url, rcept} JSON 을 payload 로 보낸다. 관심종목 공시만
   발송(브로드캐스트 아님). 클릭 시 앱을 연다(외부 링크 아님). */
self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; }
  catch (_) { data = { body: (e.data && e.data.text) ? e.data.text() : '' }; }
  const title = data.title || 'MIRI 공시 알림';
  const opts = {
    body: data.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: { url: data.url || '/' },
    tag: data.rcept || undefined,   // 같은 공시 중복 알림 접힘
  };
  e.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        // 이미 열린 앱 창이 있으면 포커스(중복 탭 방지)
        if ('focus' in c) { if (c.navigate && url !== '/') { try { c.navigate(url); } catch (_) {} } return c.focus(); }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
