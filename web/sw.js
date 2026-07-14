/* 미리(MIRI) service worker — 앱셸 캐시 + 오프라인 폴백.
   전략: 정적 자산은 cache-first, API(/api/*)는 network-only(항상 실시간 공시).
   설치 가능 요건(manifest + fetch 핸들러 + HTTPS/localhost)을 충족한다. */
const CACHE = 'miri-v8';
const SHELL = ['/', '/index.html', '/manifest.json', '/icon.svg', '/icon-192.png', '/icon-512.png'];

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
