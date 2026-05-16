// ─── AURUM SERVICE WORKER v3.0 · SHIVAM EDITION ──────────────────────────────
const CACHE_NAME = 'aurum-v3-final'; // Version badal diya taaki purana cache clear ho jaye

// In files ka naam GitHub pe bilkul yahi hona chahiye (no brackets/spaces)
const ASSETS_TO_CACHE = [
  '/',
  '/index.html',
  '/app.js',
  '/style.css',
  '/settings.css',
  '/settings_addon.js',
  '/manifest.json'
];

// INSTALL: Files ko cache mein daalo
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('Aurum: Caching Shell Assets');
      return cache.addAll(ASSETS_TO_CACHE);
    })
  );
});

// ACTIVATE: Purana kachra saaf karo
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => {
      return Promise.all(
        keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

// FETCH: Smart logic - Network first, then Cache
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // 1. API Calls ko cache MAT karo (Hamesha fresh data)
  if (url.pathname.includes('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // 2. Baki files ke liye: Pehle Internet se mangao, nahi toh Cache
  event.respondWith(
    fetch(event.request)
      .then(networkResponse => {
        // Agar net chal raha hai, toh cache ko update kar do
        return caches.open(CACHE_NAME).then(cache => {
          cache.put(event.request, networkResponse.clone());
          return networkResponse;
        });
      })
      .catch(() => {
        // Agar net nahi hai, toh purana cached version dikhao
        return caches.match(event.request);
      })
  );
});
