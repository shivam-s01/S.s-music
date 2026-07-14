// ─── Origins ─────────────────────────────────────────────────────────────────
const ORIGINS = [
  'https://ss-music-production.up.railway.app',
  'https://s-s-music-0uxa.onrender.com',
];

// ─── Cache TTLs (seconds) ────────────────────────────────────────────────────
const CACHE_TTL = {
  stream:  3600,  // Audio stream proxy — 1hr
  songs:   120,   // Song list — 2min (changes often)
  song:    300,   // Single song — 5min
  static:  0,     // No cache for static
  default: 60,    // Everything else — 1min
  // NEW TTLs:
  ytStream:  3000, // YT stream URL — 50min (matches Saavn CDN expiry)
  search:    180,  // Search results — 3min (balance freshness vs speed)
  health:    30,   // Instance health score — 30s TTL
};

// yt-stream is now cached, so removed from NO_CACHE
const NO_CACHE = ['/health', '/api/yt', '/api/play'];

// ─── OPTIMIZATION: Instance Health Scoring ───────────────────────────────────
// B2 FIX: Track which instances are slow/failing in memory.
// Worker memory persists across requests within the same isolate (~seconds to minutes).
// This means bad instances get penalized for subsequent requests on the same edge POP.
// Format: { [instanceUrl]: { failures: 0, lastFailure: 0, avgLatency: 0 } }
const instanceHealth = new Map();

function getScore(instance) {
  const h = instanceHealth.get(instance);
  if (!h) return 1000; // Unknown = assume healthy, high priority
  // Penalize recent failures heavily. Failure < 30s ago = skip.
  const timeSinceFailure = Date.now() - (h.lastFailure || 0);
  if (timeSinceFailure < 30000 && h.failures > 0) return 0; // Cooldown
  // Score = base - failures penalty - latency penalty
  return Math.max(0, 1000 - (h.failures * 200) - (h.avgLatency / 2));
}

function recordSuccess(instance, latencyMs) {
  const h = instanceHealth.get(instance) || { failures: 0, lastFailure: 0, avgLatency: 0 };
  // Exponential moving average for latency
  h.avgLatency = h.avgLatency === 0 ? latencyMs : (h.avgLatency * 0.7 + latencyMs * 0.3);
  h.failures = Math.max(0, h.failures - 1); // Recover slowly
  instanceHealth.set(instance, h);
}

function recordFailure(instance) {
  const h = instanceHealth.get(instance) || { failures: 0, lastFailure: 0, avgLatency: 0 };
  h.failures += 1;
  h.lastFailure = Date.now();
  instanceHealth.set(instance, h);
}

// Sort instances by health score — best instances first
function sortedInstances(instances) {
  return [...instances].sort((a, b) => getScore(b) - getScore(a));
}

// ─── Piped instances ──────────────────────────────────────────────────────────
const PIPED_INSTANCES = [
  'https://pipedapi.adminforge.de',
  'https://pipedapi.syncpundit.io',
  'https://piped-api.garudalinux.org',
  'https://api.piped.yt',
  'https://pipedapi.reallyaweso.me',
  'https://piped.smnz.de',
];

// ─── Invidious instances ──────────────────────────────────────────────────────
const INVIDIOUS_INSTANCES = [
  'https://invidious.adminforge.de',
  'https://yt.cdaut.de',
  'https://invidious.nerdvpn.de',
  'https://inv.nadeko.net',
  'https://invidious.privacyredirect.com',
  'https://iv.melmac.space',
];

// ─── OPTIMIZATION: Search via Piped (parallel blast) ────────────────────────
// B1 FIX: Instead of sequential instance tries, blast top 3 healthy instances
// simultaneously. First valid response wins. Worst case = single instance latency,
// not N * instance_latency. Dramatically reduces search P99 latency.
async function ytSearchPiped(query) {
  // Use health-sorted instances — best ones first
  const ranked = sortedInstances(PIPER_INSTANCES);

  // B9 FIX: Timeout reduced 5000→3000ms. If instance hasn't responded in 3s,
  // it's too slow for a good UX anyway. Move on.
  const searchOne = async (instance) => {
    const t0 = Date.now();
    const resp = await fetch(
      `${instance}/search?q=${encodeURIComponent(query)}&filter=music_songs`,
      { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(3000) }
    );
    if (!resp.ok) throw new Error(`${resp.status}`);
    const data = await resp.json();
    const items = data.items || [];
    for (const item of items) {
      if (item.url && item.duration > 60) {
        const videoId = item.url.replace('/watch?v=', '');
        recordSuccess(instance, Date.now() - t0);
        return { videoId, instance, title: item.title, thumbnail: item.thumbnail };
      }
    }
    throw new Error('no valid items');
  };

  // Blast top 3 instances in parallel — first valid result wins
  const top3 = ranked.slice(0, 3);
  try {
    return await Promise.any(top3.map(inst =>
      searchOne(inst).catch(e => { recordFailure(inst); throw e; })
    ));
  } catch (_) {
    // Top 3 all failed — try remaining sequentially as last resort
    for (const instance of ranked.slice(3)) {
      try {
        return await searchOne(instance);
      } catch (_) {
        recordFailure(instance);
      }
    }
  }
  return null;
}

// ─── OPTIMIZATION: Stream via Piped ─────────────────────────────────────────
// B2 FIX: Uses health-sorted instances. Preferred instance (from search) tried first
// since it was just healthy enough to return search results.
async function ytAudioPiped(videoId, preferredInstance) {
  const ranked = sortedInstances(PIPED_INSTANCES);
  const instances = preferredInstance
    ? [preferredInstance, ...ranked.filter(i => i !== preferredInstance)]
    : ranked;

  for (const instance of instances) {
    const t0 = Date.now();
    try {
      const resp = await fetch(
        `${instance}/streams/${videoId}`,
        { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(8000) }
      );
      if (!resp.ok) { recordFailure(instance); continue; }
      const data = await resp.json();
      const streams = (data.audioStreams || []).filter(s => s.url);
      if (!streams.length) { recordFailure(instance); continue; }
      streams.sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
      recordSuccess(instance, Date.now() - t0);
      return {
        url: streams[0].url,
        quality: streams[0].quality || 'unknown',
        title: data.title || '',
        thumbnail: data.thumbnailUrl || '',
        source: 'piped',
      };
    } catch (_) { recordFailure(instance); continue; }
  }
  return null;
}

// ─── OPTIMIZATION: Stream via Invidious ─────────────────────────────────────
// B7 FIX: Prioritize audio/mp4 (AAC/m4a) over audio/webm (opus).
// Android just_audio decodes m4a natively on all API levels.
// webm/opus requires software decode on API < 29 — slower startup + battery drain.
// Only fall back to webm if NO mp4 stream exists.
async function ytAudioInvidious(videoId) {
  const ranked = sortedInstances(INVIDIOUS_INSTANCES);

  for (const instance of ranked) {
    const t0 = Date.now();
    try {
      const resp = await fetch(
        `${instance}/api/v1/videos/${videoId}`,
        { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(8000) }
      );
      if (!resp.ok) { recordFailure(instance); continue; }
      const data = await resp.json();

      const adaptive = (data.adaptiveFormats || []).filter(f => f.url);

      // B7 FIX: Try m4a/mp4 first — best Android compatibility + faster decode
      const mp4Streams = adaptive
        .filter(f => f.type?.includes('audio/mp4'))
        .sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));

      if (mp4Streams.length) {
        recordSuccess(instance, Date.now() - t0);
        return {
          url: mp4Streams[0].url,
          quality: mp4Streams[0].audioQuality || 'unknown',
          title: data.title || '',
          thumbnail: data.videoThumbnails?.[0]?.url || '',
          source: 'invidious',
        };
      }

      // Fallback: webm/opus — only if no mp4 available
      const webmStreams = adaptive
        .filter(f => f.type?.includes('audio/webm') && f.url)
        .sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));

      if (webmStreams.length) {
        recordSuccess(instance, Date.now() - t0);
        return {
          url: webmStreams[0].url,
          quality: webmStreams[0].audioQuality || 'unknown',
          title: data.title || '',
          thumbnail: data.videoThumbnails?.[0]?.url || '',
          source: 'invidious',
        };
      }

      // formatStreams fallback (muxed — last resort)
      const fmtStreams = (data.formatStreams || []).slice().reverse();
      for (const f of fmtStreams) {
        if (f.url) {
          recordSuccess(instance, Date.now() - t0);
          return {
            url: f.url,
            quality: f.quality || 'unknown',
            title: data.title || '',
            thumbnail: data.videoThumbnails?.[0]?.url || '',
            source: 'invidious_fmt',
          };
        }
      }

      recordFailure(instance);
    } catch (_) { recordFailure(instance); continue; }
  }
  return null;
}

// ─── OPTIMIZATION: Request Coalescing Map ────────────────────────────────────
// B6 FIX: If 100 users request the same video simultaneously (e.g., trending song),
// without coalescing = 100 upstream Piped/Invidious calls.
// With coalescing = 1 upstream call, 99 users await the same Promise.
// Map is in-memory per isolate — safe, no cross-request state leaks.
const inflightStreams = new Map();

// ─── /api/yt-stream — with CF edge cache + request coalescing ────────────────
// B4 FIX: Use proper CF cache API with stable cache key.
// B6 FIX: Request coalescing for concurrent identical requests.
// B8 FIX: stale-while-revalidate header so CF serves stale while refreshing.
async function handleYtStream(videoId, ctx) {
  if (!videoId) {
    return jsonResp({ success: false, error: 'id required' }, 400);
  }

  // Stable cache key — no query param ambiguity
  const cacheKey = new Request(`https://aurum-cache/yt-stream/${videoId}`);

  // CF Edge cache check — instant return if cached at this POP
  const cached = await caches.default.match(cacheKey);
  if (cached) {
    const h = new Headers(cached.headers);
    h.set('X-Cache', 'HIT');
    return new Response(cached.body, { status: cached.status, headers: h });
  }

  // B6 FIX: Request coalescing — if same videoId already in-flight, await it
  if (inflightStreams.has(videoId)) {
    const result = await inflightStreams.get(videoId);
    // Clone because Response body can only be consumed once
    return result ? result.clone() : jsonResp({ success: false, error: 'No stream found' }, 502);
  }

  // Start resolution — store Promise for coalescing
  const resolutionPromise = (async () => {
    // B1 FIX: Piped + Invidious in parallel — fastest source wins
    // Promise.any = first success wins, ignores individual failures
    const audio = await Promise.any([
      ytAudioPiped(videoId, null),
      ytAudioInvidious(videoId),
    ]).catch(() => null);

    if (!audio) return null;

    // Build response
    const resp = jsonResp({ success: true, ...audio, videoId });

    // B8 FIX: stale-while-revalidate=300 means CF serves stale for 5min
    // while refreshing in background. User never waits for revalidation.
    // max-age=3000 (50min) = primary TTL matching Saavn CDN expiry.
    const toCache = resp.clone();
    const cacheHeaders = new Headers(toCache.headers);
    cacheHeaders.set('Cache-Control', 'public, max-age=3000, stale-while-revalidate=300');
    ctx.waitUntil(
      caches.default.put(cacheKey, new Response(toCache.body, {
        status: toCache.status,
        headers: cacheHeaders,
      }))
    );

    return resp;
  })();

  inflightStreams.set(videoId, resolutionPromise);

  // Clean up coalescing map after resolution (success or fail)
  resolutionPromise.finally(() => {
    inflightStreams.delete(videoId);
  });

  const result = await resolutionPromise;
  return result
    ? result.clone()
    : jsonResp({ success: false, error: 'No stream found' }, 502);
}

// ─── /api/yt-search — NEW dedicated search endpoint with caching ─────────────
// B3 FIX: Cache search results for 3 minutes.
// Same query from 1000 users = 1 upstream call per 3min window per CF POP.
// Response format unchanged — Flutter app can use this directly.
async function handleYtSearchCached(query, ctx) {
  if (!query) return jsonResp({ success: false, error: 'Missing q' }, 400);

  // Cache key for search
  const cacheKey = new Request(
    `https://aurum-cache/yt-search/${encodeURIComponent(query.toLowerCase().trim())}`
  );

  // Cache hit
  const cached = await caches.default.match(cacheKey);
  if (cached) {
    const h = new Headers(cached.headers);
    h.set('X-Cache', 'HIT');
    return new Response(cached.body, { status: cached.status, headers: h });
  }

  const found = await ytSearchPiped(query);
  if (!found) return jsonResp({ success: false, error: 'Search failed' }, 404);

  const audio = await ytAudioPiped(found.videoId, found.instance);
  if (audio) {
    const resp = jsonResp({ success: true, ...audio, videoId: found.videoId });
    // Cache search result for 3min — B3 fix
    const toCache = resp.clone();
    const ch = new Headers(toCache.headers);
    ch.set('Cache-Control', `public, max-age=${CACHE_TTL.search}`);
    ctx.waitUntil(caches.default.put(cacheKey, new Response(toCache.body, { status: toCache.status, headers: ch })));
    return resp;
  }

  const invAudio = await ytAudioInvidious(found.videoId);
  if (invAudio) {
    const resp = jsonResp({ success: true, ...invAudio, videoId: found.videoId });
    const toCache = resp.clone();
    const ch = new Headers(toCache.headers);
    ch.set('Cache-Control', `public, max-age=${CACHE_TTL.search}`);
    ctx.waitUntil(caches.default.put(cacheKey, new Response(toCache.body, { status: toCache.status, headers: ch })));
    return resp;
  }

  return jsonResp({ success: false, error: 'No audio URL' }, 502);
}

// ─── /api/yt — original search+stream (web app) — UNCHANGED behavior ─────────
// Kept exactly as before for backward compatibility.
// Now internally uses cached search path for speed.
async function handleYtSearch(query, ctx) {
  return handleYtSearchCached(query, ctx);
}

// ─── OPTIMIZATION: Parallel Origin Failover ──────────────────────────────────
// B5 FIX: v1 tried origins sequentially — if origin[0] took 9.9s to fail,
// origin[1] didn't even start until 10s had passed.
// v2: Race both origins simultaneously with a 4s head-start for primary.
// If primary responds within 4s → use it (origin preference maintained).
// If primary is slow → secondary already in-flight, no extra wait.
async function fetchFromOrigins(request) {
  const buildOriginRequest = (origin) => {
    const url = new URL(request.url);
    url.hostname = new URL(origin).hostname;
    url.protocol = 'https:';
    url.port = '';
    const headers = new Headers(request.headers);
    headers.set('X-Forwarded-For', request.headers.get('CF-Connecting-IP') || '127.0.0.1');
    headers.set('Host', new URL(origin).hostname);
    headers.delete('CF-Ray');
    headers.delete('CF-Visitor');
    return new Request(url.toString(), {
      method: request.method,
      headers,
      body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
      signal: AbortSignal.timeout(10000),
    });
  };

  // Try primary first with 4s head start
  // If primary doesn't respond in 4s, fire secondary in parallel
  let primaryResp = null;
  const primaryPromise = fetch(buildOriginRequest(ORIGINS[0]))
    .then(r => r.status < 500 ? r : Promise.reject(r.status))
    .catch(() => null);

  // 4s head-start timer — if primary is fast, secondary never fires
  const headStart = new Promise(resolve => setTimeout(resolve, 4000));

  primaryResp = await Promise.race([primaryPromise, headStart.then(() => null)]);

  if (primaryResp) return { resp: primaryResp, origin: ORIGINS[0] };

  // Primary slow/failed — race remaining origins + primary together
  const allPromises = ORIGINS.map((origin, i) => {
    if (i === 0) return primaryPromise.then(r => r ? { resp: r, origin } : Promise.reject());
    return fetch(buildOriginRequest(origin))
      .then(r => r.status < 500 ? { resp: r, origin } : Promise.reject(r.status))
      .catch(() => Promise.reject());
  });

  try {
    return await Promise.any(allPromises);
  } catch (_) {
    return null;
  }
}

// ─── Helper ──────────────────────────────────────────────────────────────────
function jsonResp(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'X-Cache': 'MISS',
    },
  });
}

// ─── Main handler ─────────────────────────────────────────────────────────────
export default {
  async fetch(request, env, ctx) {
    const { pathname, searchParams } = new URL(request.url);

    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
          'Access-Control-Allow-Headers': '*',
        },
      });
    }

    // Flutter app — stream by video ID (cached + coalesced)
    if (pathname === '/api/yt-stream') {
      return handleYtStream(searchParams.get('id') || '', ctx);
    }

    // Web app — search + stream (now cached internally)
    if (pathname === '/api/yt') {
      return handleYtSearch(searchParams.get('q') || '', ctx);
    }

    // NEW: Dedicated cached search endpoint (Flutter can use this for YT search)
    if (pathname === '/api/yt-search') {
      return handleYtSearchCached(searchParams.get('q') || '', ctx);
    }

    // ── Cache + origin proxy ─────────────────────────────────────────────────
    const skipCache = NO_CACHE.some(r => pathname.startsWith(r));
    const isStream  = pathname.startsWith('/api/stream');
    const hasRange  = request.headers.has('Range');

    // B8 FIX: Serve from CF cache before hitting origins
    if (!skipCache && !isStream && !hasRange && request.method === 'GET') {
      const cached = await caches.default.match(new Request(request.url, { method: 'GET' }));
      if (cached) {
        const h = new Headers(cached.headers);
        h.set('X-Cache', 'HIT');
        h.set('Access-Control-Allow-Origin', '*');
        return new Response(cached.body, { status: cached.status, headers: h });
      }
    }

    // B5 FIX: Parallel origin failover
    const result = await fetchFromOrigins(request);

    if (!result) {
      return jsonResp({ error: 'All origins failed' }, 502);
    }

    const { resp: originResp, origin: usedOrigin } = result;

    const h = new Headers(originResp.headers);
    h.set('Access-Control-Allow-Origin', '*');
    h.set('X-Cache', 'MISS');
    h.set('X-Origin', usedOrigin);
    if (isStream) h.set('Accept-Ranges', 'bytes');

    const response = new Response(originResp.body, {
      status: originResp.status,
      statusText: originResp.statusText,
      headers: h,
    });

    // Cache successful GET responses
    if (!skipCache && !isStream && !hasRange && request.method === 'GET' && originResp.status === 200) {
      const ttl = getTTL(pathname);
      const toCache = response.clone();
      // B8 FIX: stale-while-revalidate on origin responses too
      toCache.headers.set('Cache-Control', `public, max-age=${ttl}, stale-while-revalidate=60`);
      ctx.waitUntil(caches.default.put(new Request(request.url, { method: 'GET' }), toCache));
    }

    return response;
  },
};

function getTTL(p) {
  if (p.startsWith('/api/stream'))  return CACHE_TTL.stream;
  if (p.startsWith('/api/songs'))   return CACHE_TTL.songs;
  if (p.startsWith('/api/song'))    return CACHE_TTL.song;
  if (p.startsWith('/api/saavn'))   return CACHE_TTL.song;
  if (/\.(js|css|html|json|png|ico|webp|woff2?)$/.test(p)) return CACHE_TTL.static;
  return CACHE_TTL.default;
}
