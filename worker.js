// ═══════════════════════════════════════════════════════════════
// AURUM STREAM — Cloudflare Worker v2.1.0
// Primary  : https://s-s-music-0uxa.onrender.com
// Fallback : https://aurum-waves.up.railway.app
// ═══════════════════════════════════════════════════════════════

const ORIGINS = [
  'https://s-s-music-0uxa.onrender.com',  // Primary
  'https://aurum-waves.up.railway.app',   // Fallback
];

const WORKER_VERSION = '2.1.0';

// ── Cache TTL ──────────────────────────────────────────────────
const CACHE_TTL = {
  stream:  3600,   // Audio stream  — 1 hour
  song:    300,    // Song URL      — 5 min
  songs:   120,    // Song list     — 2 min
  static:  86400,  // HTML/CSS/JS   — 1 day
  default: 60,
};

// ── No-cache routes ───────────────────────────────────────────
const NO_CACHE_ROUTES = [
  '/health',
  '/api/yt',
  '/api/download',
];

// ── Rate limit config ─────────────────────────────────────────
const RATE_LIMIT = {
  requests: 100,  // per window
  window:   60,   // seconds
};

// ═══════════════════════════════════════════════════════════════
// MAIN HANDLER
// ═══════════════════════════════════════════════════════════════
export default {
  async fetch(request, env, ctx) {
    const url      = new URL(request.url);
    const pathname = url.pathname;

    // ── CORS Preflight ───────────────────────────────────────
    if (request.method === 'OPTIONS') {
      return corsResponse();
    }

    // ── Method check ─────────────────────────────────────────
    if (!['GET', 'POST', 'HEAD'].includes(request.method)) {
      return errorResponse(405, 'Method Not Allowed');
    }

    // ── Rate Limiting (KV chahiye — optional) ─────────────────
    if (env.RATE_LIMIT_KV) {
      const rateLimitResult = await checkRateLimit(request, env);
      if (!rateLimitResult.allowed) {
        return new Response(
          JSON.stringify({ error: 'Too Many Requests', retryAfter: rateLimitResult.retryAfter }),
          {
            status: 429,
            headers: {
              'Content-Type':                'application/json',
              'Access-Control-Allow-Origin': '*',
              'Retry-After':                 String(rateLimitResult.retryAfter),
              'X-RateLimit-Limit':           String(RATE_LIMIT.requests),
              'X-RateLimit-Remaining':       '0',
            },
          }
        );
      }
    }

    const skipCache = NO_CACHE_ROUTES.some(r => pathname.startsWith(r));
    const isStream  = pathname.startsWith('/api/stream');
    const hasRange  = request.headers.has('Range');

    // ── Cache check (GET only, no stream, no range) ───────────
    if (!skipCache && !hasRange && !isStream && request.method === 'GET') {
      const cache    = caches.default;
      const cacheKey = new Request(request.url, { method: 'GET' });
      const cached   = await cache.match(cacheKey);

      if (cached) {
        const resp = new Response(cached.body, cached);
        resp.headers.set('X-Cache',                    'HIT');
        resp.headers.set('Access-Control-Allow-Origin', '*');
        addSecurityHeaders(resp.headers);
        return resp;
      }
    }

    // ── Forward to Origin (with fallback) ────────────────────
    try {
      const originResp = await fetchWithFallback(request, pathname, hasRange);

      // ── Build response headers ───────────────────────────
      const respHeaders = new Headers(originResp.headers);
      setCORSHeaders(respHeaders);
      addSecurityHeaders(respHeaders);
      respHeaders.set('X-Cache',          'MISS');
      respHeaders.set('X-Worker',         'aurum-stream');
      respHeaders.set('X-Worker-Version', WORKER_VERSION);

      // Stream-specific headers
      if (isStream) {
        respHeaders.set('Accept-Ranges', 'bytes');
        respHeaders.set('Cache-Control', `public, max-age=${CACHE_TTL.stream}`);

        // Range request — pass through as-is (audio seek support)
        if (hasRange) {
          return new Response(originResp.body, {
            status:  originResp.status,
            headers: respHeaders,
          });
        }
      }

      const response = new Response(originResp.body, {
        status:     originResp.status,
        statusText: originResp.statusText,
        headers:    respHeaders,
      });

      // ── Cache store ──────────────────────────────────────
      if (
        !skipCache &&
        !isStream &&
        !hasRange &&
        request.method === 'GET' &&
        originResp.status === 200
      ) {
        const ttl     = getTTL(pathname);
        const toCache = response.clone();
        toCache.headers.set('Cache-Control', `public, max-age=${ttl}`);
        ctx.waitUntil(
          caches.default.put(new Request(request.url, { method: 'GET' }), toCache)
        );
      }

      return response;

    } catch (err) {
      const isTimeout = err.message?.includes('timeout');
      return errorResponse(
        isTimeout ? 504 : 502,
        isTimeout ? 'Gateway Timeout' : 'Origin Unreachable',
        err.message
      );
    }
  },
};

// ═══════════════════════════════════════════════════════════════
// DUAL ORIGIN FAILOVER
// ═══════════════════════════════════════════════════════════════
async function fetchWithFallback(request, pathname, hasRange) {
  let lastErr;

  for (const origin of ORIGINS) {
    try {
      const originUrl     = buildOriginUrl(request.url, origin);
      const originRequest = new Request(originUrl, {
        method:  request.method,
        headers: buildHeaders(request, origin),
        body:    ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
      });

      const resp = await fetchWithTimeout(originRequest, 30000);

      // 5xx aaya toh next origin try karo
      if (resp.status >= 500) {
        lastErr = new Error(`Origin ${origin} returned ${resp.status}`);
        continue;
      }

      // Konsa origin use hua — header mein dikhao
      resp.headers.set('X-Origin-Used', origin);
      return resp;

    } catch (err) {
      lastErr = err;
      // Timeout ya network error — next origin try karo
    }
  }

  throw lastErr;
}

// ═══════════════════════════════════════════════════════════════
// RATE LIMITER (Cloudflare KV based — optional)
// Setup: Dashboard → Workers → KV → namespace banao → bind karo
// ═══════════════════════════════════════════════════════════════
async function checkRateLimit(request, env) {
  const ip      = request.headers.get('CF-Connecting-IP') || 'unknown';
  const key     = `rl:${ip}:${Math.floor(Date.now() / 1000 / RATE_LIMIT.window)}`;
  const current = await env.RATE_LIMIT_KV.get(key);
  const count   = current ? parseInt(current) : 0;

  if (count >= RATE_LIMIT.requests) {
    return { allowed: false, retryAfter: RATE_LIMIT.window };
  }

  await env.RATE_LIMIT_KV.put(key, String(count + 1), {
    expirationTtl: RATE_LIMIT.window * 2,
  });

  return { allowed: true };
}

// ═══════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════

function buildOriginUrl(requestUrl, origin) {
  const url    = new URL(requestUrl);
  const org    = new URL(origin);
  url.hostname = org.hostname;
  url.protocol = 'https:';
  url.port     = '';
  return url.toString();
}

function buildHeaders(request, origin) {
  const headers  = new Headers(request.headers);
  const clientIP =
    request.headers.get('CF-Connecting-IP') ||
    request.headers.get('X-Forwarded-For')  ||
    '127.0.0.1';

  headers.set('X-Forwarded-For',   clientIP);
  headers.set('X-Real-IP',         clientIP);
  headers.set('X-Forwarded-Proto', 'https');
  headers.set('Host',              new URL(origin).hostname);

  // CF internal headers remove karo
  headers.delete('CF-Ray');
  headers.delete('CF-Visitor');
  headers.delete('CF-Worker');

  return headers;
}

function setCORSHeaders(headers) {
  headers.set('Access-Control-Allow-Origin',   '*');
  headers.set('Access-Control-Allow-Methods',  'GET, POST, OPTIONS');
  headers.set('Access-Control-Allow-Headers',  '*');
  headers.set('Access-Control-Expose-Headers', 'Content-Length, Content-Range, X-Cache, X-Worker, X-Origin-Used');
}

function addSecurityHeaders(headers) {
  headers.set('X-Content-Type-Options', 'nosniff');
  headers.set('X-Frame-Options',        'SAMEORIGIN');
  headers.set('Referrer-Policy',        'strict-origin-when-cross-origin');
}

function getTTL(pathname) {
  if (pathname.startsWith('/api/stream')) return CACHE_TTL.stream;
  if (pathname.startsWith('/api/songs'))  return CACHE_TTL.songs;   // songs pehle (longer prefix)
  if (pathname.startsWith('/api/song'))   return CACHE_TTL.song;
  if (pathname.startsWith('/api/saavn'))  return CACHE_TTL.song;
  if (/\.(js|css|html|json|png|ico|webp|woff2?)$/.test(pathname)) return CACHE_TTL.static;
  return CACHE_TTL.default;
}

async function fetchWithTimeout(request, ms) {
  const controller = new AbortController();
  const timer      = setTimeout(() => controller.abort(), ms);
  try {
    return await fetch(request, { signal: controller.signal });
  } catch (err) {
    if (err.name === 'AbortError') throw new Error(`Request timeout after ${ms}ms`);
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

function errorResponse(status, message, detail = null) {
  return new Response(
    JSON.stringify({ error: message, ...(detail && { detail }) }),
    {
      status,
      headers: {
        'Content-Type':                'application/json',
        'Access-Control-Allow-Origin': '*',
      },
    }
  );
}

function corsResponse() {
  return new Response(null, {
    status: 204,
    headers: {
      'Access-Control-Allow-Origin':  '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': '*',
      'Access-Control-Max-Age':       '86400',
    },
  });
}
