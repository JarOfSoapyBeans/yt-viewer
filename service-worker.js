const CACHE_NAME = 'yt-viewer-shell-v1';
const SHELL_PATHS = new Set([
  '/',
  '/static/player-ui.css',
  '/static/player-ui.js',
  '/static/manifest.webmanifest',
  '/static/icons/icon-192.svg',
  '/static/icons/icon-512.svg'
]);

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll([...SHELL_PATHS]))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((name) => name.startsWith('yt-viewer-shell-') && name !== CACHE_NAME).map((name) => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || ['/stream', '/download', '/api'].includes(url.pathname) || url.pathname.startsWith('/api/')) return;
  if (!SHELL_PATHS.has(url.pathname)) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(url.pathname === '/' ? '/' : request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok) await cache.put(url.pathname === '/' ? '/' : request, response.clone());
    return response;
  })());
});
