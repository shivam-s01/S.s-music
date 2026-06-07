// ─── AURUM SERVICE WORKER v3.1 · SHIVAM EDITION ──────────────────────────────
const CACHE_NAME = 'aurum-v4-20250607'; // Version badal diya taaki purana cache clear ho jaye

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

// ─── BACKGROUND AUDIO: Audio stream URLs jo kabhi cache nahi honge ────────────
// PWABuilder WebView mein SW audio streams ko intercept karta hai aur background
// mein unhe drop kar deta hai — yahi primary reason hai audio band hone ka.
// Solution: Inhe seedha network pe bhejo, SW ke beech mein aane do hi nahi.
const _AUDIO_EXTS  = ['.mp3', '.m4a', '.aac', '.ogg', '.flac', '.wav', '.opus', '.ts'];
const _AUDIO_HOSTS = ['saavncdn.com', 'jiosaavn.com', 'jiocdn.com', 'akamaized.net', 'googlevideo.com'];

function _isAudioRequest(url, request) {
  // Range request = audio seek — hamesha network se
  if (request.headers.get('range')) return true;
  // Audio file extension
  if (_AUDIO_EXTS.some(ext => url.pathname.includes(ext))) return true;
  // Known audio CDN hosts
  if (_AUDIO_HOSTS.some(host => url.hostname.includes(host))) return true;
  // Our own streaming API endpoints
  if (url.pathname.includes('/api/play') ||
      url.pathname.includes('/api/stream') ||
      url.pathname.includes('/api/saavn') ||
      url.pathname.includes('/api/prefetch') ||
      url.pathname.includes('/api/artwork')) return true;
  return false;
}

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

  // ── PRIORITY 0: Audio streams — SEEDHA network, SW beech mein NAHI ────────
  // Yeh check sabse pehle aana chahiye — kisi bhi caching logic se pehle.
  // Agar yeh miss hua toh background mein audio stream cut ho jaayega.
  if (_isAudioRequest(url, event.request)) {
    event.respondWith(
      fetch(event.request).catch(() => {
        // Network fail hone pe kuch nahi kar sakte audio ke liye
        return new Response('', { status: 503, statusText: 'Audio stream unavailable' });
      })
    );
    return;
  }

  // ── PRIORITY 1: API Calls ko cache MAT karo (Hamesha fresh data) ──────────
  if (url.pathname.includes('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }

  // ── PRIORITY 2: Baki files ke liye: Pehle Internet se mangao, nahi toh Cache
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

// ─── BACKGROUND AUDIO: Service Worker ko alive rakhna ────────────────────────
// app.js har 15 seconds mein yeh message bhejta hai jab audio chal raha ho.
// Isse SW idle nahi hota aur PWABuilder WebView audio pipeline live rehti hai.
self.addEventListener('message', event => {
  if (!event.data) return;

  if (event.data.type === 'AUDIO_PLAYING') {
    // SW ko pata hai audio active hai — event.waitUntil se SW alive rehta hai
    event.waitUntil(
      Promise.resolve(true)
    );
  }

  if (event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
