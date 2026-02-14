const CACHE_NAME = 'crypto-signals-v1';
const STATIC_ASSETS = ['/', '/static/style.css', '/static/app.js'];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS))
    );
});

self.addEventListener('fetch', event => {
    if (event.request.url.includes('/api/')) return;
    event.respondWith(
        caches.match(event.request).then(cached => cached || fetch(event.request))
    );
});
