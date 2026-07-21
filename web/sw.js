/* 미리(MIRI) service worker — 앱셸 캐시 + 오프라인 폴백.
   전략: 정적 자산은 cache-first, API(/api/*)는 network-only(항상 실시간 공시).
   설치 가능 요건(manifest + fetch 핸들러 + HTTPS/localhost)을 충족한다. */
const CACHE = 'miri-v15';   // v14→v15: shell.js 개정(항목7 뒤로가기·검색→설정 라우팅·랭킹순번·관심배지 seen). shell.js는 cache-first 정적자산이라 버전 bump 없으면 구버전 고착
const DATA_CACHE = 'miri-data-v1';   // /api/alerts 응답 캐시(앱셸과 분리 → activate 정리에서 보존)
const SHELL = ['/', '/index.html', '/manifest.json', '/app/shell.js', '/icon.svg', '/icon-192.png', '/icon-512.png', '/icon-maskable-192.png', '/icon-maskable-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
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

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;          // Umami 등 외부 트래픽은 SW 미개입
  if (e.request.method !== 'GET') return;              // 등록/삭제(POST/DELETE)는 통과
  if (url.pathname === '/api/alerts') {                // 피드만 SWR(재방문 즉시표시+오프라인)
    e.respondWith(swrAlerts(e.request));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;        // 그 외 API는 항상 네트워크(실시간)
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
