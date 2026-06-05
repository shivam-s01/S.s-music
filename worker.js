const ORIGINS = [
  'https://aurum-wave.up.railway.app/',   // PRIMARY - Railway
  'https://s-s-music-0uxa.onrender.com',  // FALLBACK - Render
];

const CACHE_TTL = {
  stream: 3600,
  songs: 120,
  song: 300,
  static: 0,
  default: 60,
};

const NO_CACHE = ['/health', '/api/yt', '/api/download'];

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': '*',
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    const skipCache = NO_CACHE.some(r => pathname.startsWith(r));
    const isStream = pathname.startsWith('/api/stream');
    const hasRange = request.headers.has('Range');

    // cache check
    if (!skipCache && !isStream && !hasRange && request.method === 'GET') {
      const cached = await caches.default.match(new Request(request.url, { method: 'GET' }));
      if (cached) {
        const h = new Headers(cached.headers);
        h.set('X-Cache', 'HIT');
        h.set('Access-Control-Allow-Origin', '*');
        return new Response(cached.body, { status: cached.status, headers: h });
      }
    }

    // origin pe bhejo
    let originResp, usedOrigin;
    for (const origin of ORIGINS) {
      try {
        const url = new URL(request.url);
        url.hostname = new URL(origin).hostname;
        url.protocol = 'https:';
        url.port = '';

        const headers = new Headers(request.headers);
        const ip = request.headers.get('CF-Connecting-IP') || '127.0.0.1';
        headers.set('X-Forwarded-For', ip);
        headers.set('X-Real-IP', ip);
        headers.set('X-Forwarded-Proto', 'https');
        headers.set('Host', new URL(origin).hostname);
        headers.delete('CF-Ray');
        headers.delete('CF-Visitor');

        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 8000);

        const resp = await fetch(new Request(url.toString(), {
          method: request.method,
          headers,
          body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
          signal: controller.signal,
        }));

        clearTimeout(timer);

        if (resp.status >= 500) continue;

        originResp = resp;
        usedOrigin = origin;
        break;
      } catch (e) {
        continue;
      }
    }

    if (!originResp) {
      return new Response(JSON.stringify({ error: 'All origins failed' }), {
        status: 502,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      });
    }

    const h = new Headers(originResp.headers);
    h.set('Access-Control-Allow-Origin', '*');
    h.set('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    h.set('Access-Control-Allow-Headers', '*');
    h.set('Access-Control-Expose-Headers', 'Content-Length, Content-Range');
    h.set('X-Cache', 'MISS');
    h.set('X-Origin', usedOrigin);

    if (isStream) {
      h.set('Accept-Ranges', 'bytes');
      h.set('Cache-Control', `public, max-age=${CACHE_TTL.stream}`);
    }

    const response = new Response(originResp.body, {
      status: originResp.status,
      statusText: originResp.statusText,
      headers: h,
    });

    // cache store
    if (!skipCache && !isStream && !hasRange && request.method === 'GET' && originResp.status === 200) {
      const ttl = getTTL(pathname);
      const toCache = response.clone();
      toCache.headers.set('Cache-Control', `public, max-age=${ttl}`);
      ctx.waitUntil(caches.default.put(new Request(request.url, { method: 'GET' }), toCache));
    }

    return response;
  },
};

function getTTL(pathname) {
  if (pathname.startsWith('/api/stream')) return CACHE_TTL.stream;
  if (pathname.startsWith('/api/songs')) return CACHE_TTL.songs;
  if (pathname.startsWith('/api/song')) return CACHE_TTL.song;
  if (pathname.startsWith('/api/saavn')) return CACHE_TTL.song;
  if (/\.(js|css|html|json|png|ico|webp|woff2?)$/.test(pathname)) return CACHE_TTL.static;
  return CACHE_TTL.default;
}
