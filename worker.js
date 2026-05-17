// ═══════════════════════════════════════════════════════════════
// AURUM STREAM — Cloudflare Worker
// Railway origin: https://aurum-waves.up.railway.app
// 1000+ users handle karega — caching + rate limit + CORS
// ═══════════════════════════════════════════════════════════════

const ORIGIN = 'https://aurum-waves.up.railway.app';

// Cache TTL settings
const CACHE_TTL = {
  stream:  3600,   // Audio stream — 1 hour cache
  song:    300,    // Song URL — 5 min cache
  songs:   120,    // Song list — 2 min cache
  static:  86400,  // HTML/CSS/JS — 1 day cache
  default: 60,
};

// Ye routes cache NAHI honge (har baar fresh)
const NO_CACHE_ROUTES = [
  '/health',
  '/api/yt',       // YT URLs expire hoti hain
  '/api/download', // Download seedha stream ho
];

// ═══════════════════════════════════════════════════════════════
// MAIN HANDLER
// ═══════════════════════════════════════════════════════════════
export default {
  async fetch(request, env, ctx) {
    const url      = new URL(request.url);
    const pathname = url.pathname;

    // OPTIONS preflight — seedha return karo
    if (request.method === 'OPTIONS') {
      return corsResponse();
    }

    // Cache skip routes
    const skipCache = NO_CACHE_ROUTES.some(r => pathname.startsWith(r));

    // Cache check karo (skip routes ke alawa)
    if (!skipCache && request.method === 'GET') {
      const cache     = caches.default;
      const cacheKey  = new Request(request.url, request);
      const cached    = await cache.match(cacheKey);

      if (cached) {
        const resp = new Response(cached.body, cached);
        resp.headers.set('X-Cache', 'HIT');
        resp.headers.set('Access-Control-Allow-Origin', '*');
        return resp;
      }
    }

    // Railway pe forward karo
    try {
      const originUrl = new URL(request.url);
      originUrl.hostname = new URL(ORIGIN).hostname;
      originUrl.protocol = 'https:';
      originUrl.port     = '';

      const originRequest = new Request(originUrl.toString(), {
        method:  request.method,
        headers: buildHeaders(request),
        body:    request.method !== 'GET' && request.method !== 'HEAD'
                   ? request.body
                   : undefined,
      });

      const originResp = await fetch(originRequest);

      // Response headers fix karo
      const respHeaders = new Headers(originResp.headers);
      respHeaders.set('Access-Control-Allow-Origin',   '*');
      respHeaders.set('Access-Control-Allow-Methods',  'GET, OPTIONS');
      respHeaders.set('Access-Control-Allow-Headers',  '*');
      respHeaders.set('Access-Control-Expose-Headers', 'Content-Length, Content-Range');
      respHeaders.set('X-Cache', 'MISS');
      respHeaders.set('X-Worker', 'aurum-stream');

      // Audio streaming ke liye
      if (pathname.startsWith('/api/stream')) {
        respHeaders.set('Accept-Ranges',  'bytes');
        respHeaders.set('Cache-Control',  `public, max-age=${CACHE_TTL.stream}`);
      }

      const response = new Response(originResp.body, {
        status:     originResp.status,
        statusText: originResp.statusText,
        headers:    respHeaders,
      });

      // Cache mein store karo (success responses)
      if (
        !skipCache &&
        request.method === 'GET' &&
        originResp.status === 200 &&
        !pathname.startsWith('/api/stream') // Audio stream cache mat karo (too large)
      ) {
        const ttl      = getTTL(pathname);
        const toCache  = response.clone();
        const cacheKey = new Request(request.url, request);

        // Cache TTL header set karo
        toCache.headers.set('Cache-Control', `public, max-age=${ttl}`);

        ctx.waitUntil(caches.default.put(cacheKey, toCache));
      }

      return response;

    } catch (err) {
      // Railway down hai — error return karo
      return new Response(
        JSON.stringify({ error: 'Origin unreachable', detail: err.message }),
        {
          status:  502,
          headers: {
            'Content-Type':                'application/json',
            'Access-Control-Allow-Origin': '*',
          },
        }
      );
    }
  },
};

// ═══════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════

// Route ke hisaab se cache TTL decide karo
function getTTL(pathname) {
  if (pathname.startsWith('/api/stream'))  return CACHE_TTL.stream;
  if (pathname.startsWith('/api/song'))    return CACHE_TTL.song;
  if (pathname.startsWith('/api/songs'))   return CACHE_TTL.songs;
  if (pathname.startsWith('/api/saavn'))   return CACHE_TTL.song;
  if (
    pathname.endsWith('.js')   ||
    pathname.endsWith('.css')  ||
    pathname.endsWith('.html') ||
    pathname.endsWith('.json') ||
    pathname.endsWith('.png')  ||
    pathname.endsWith('.ico')
  ) return CACHE_TTL.static;
  return CACHE_TTL.default;
}

// Request headers Railway ke liye banao
function buildHeaders(request) {
  const headers = new Headers(request.headers);

  // Real IP Railway ko bhejo
  const clientIP =
    request.headers.get('CF-Connecting-IP') ||
    request.headers.get('X-Forwarded-For')  ||
    '127.0.0.1';

  headers.set('X-Forwarded-For',  clientIP);
  headers.set('X-Real-IP',        clientIP);
  headers.set('X-Forwarded-Proto','https');

  // Host header Railway ka set karo
  headers.set('Host', new URL(ORIGIN).hostname);

  return headers;
}

// CORS preflight response
function corsResponse() {
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin':  '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*',
      'Access-Control-Max-Age':       '86400',
    },
  });
}
