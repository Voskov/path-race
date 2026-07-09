/* App-shell service worker. Cache-first for the shell (instant cold start,
 * works offline); network-only for /api (the app owns its own offline queue,
 * so we never want stale API responses served from cache). */
const PREFIX = self.location.pathname.replace(/\/sw\.js$/, '');
const CACHE = 'path-race-v10';
const SHELL = [
  `${PREFIX}/`,
  `${PREFIX}/static/app.js`,
  `${PREFIX}/static/styles.css`,
  `${PREFIX}/static/icon.svg`,
  `${PREFIX}/manifest.webmanifest`,
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.includes('/api/')) return; // let it hit network
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
      return res;
    }).catch(() => hit))
  );
});
