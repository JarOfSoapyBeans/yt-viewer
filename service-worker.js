const CACHE_NAME = 'yt-viewer-shell-v2';
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
      .then((cache) => cache.addAll([...SHELL_PATHS].map((path) => new Request(path, { cache: 'reload' }))))
      .catch(() => {})
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names
        .filter((name) => name.startsWith('yt-viewer-shell-') && name !== CACHE_NAME)
        .map((name) => caches.delete(name))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/stream' || url.pathname === '/download' || url.pathname === '/health' ||
      url.pathname.startsWith('/api/')) return;
  if (!SHELL_PATHS.has(url.pathname)) return;

  const cacheKey = url.pathname;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    try {
      const response = await fetch(request);
      if (response.ok) await cache.put(cacheKey, response.clone());
      return response;
    } catch (_) {
      return (await cache.match(cacheKey)) || Response.error();
    }
  })());
});