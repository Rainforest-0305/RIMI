/* 미리(MIRI) service worker — 앱셸 캐시 + 오프라인 폴백.
   전략: 정적 자산은 cache-first, API(/api/*)는 network-only(항상 실시간 공시).
   설치 가능 요건(manifest + fetch 핸들러 + HTTPS/localhost)을 충족한다. */
const CACHE = 'miri-v12';
const SHELL = ['/', '/index.html', '/manifest.json', '/icon.svg', '/icon-192.png', '/icon-512.png', '/icon-maskable-192.png', '/icon-maskable-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;          // Umami 등 외부 트래픽은 SW 미개입
  if (e.request.method !== 'GET') return;              // 등록/삭제(POST/DELETE)는 통과
  if (url.pathname.startsWith('/api/')) return;        // API는 항상 네트워크(실시간)
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
