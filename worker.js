const ORIGINS = [
  'https://ss-music-production.up.railway.app',
  'https://s-s-music-0uxa.onrender.com',
];

const CACHE_TTL = { stream: 3600, songs: 120, song: 300, static: 0, default: 60 };
const NO_CACHE = ['/health', '/api/yt', '/api/play'];

// Piped instances — free YouTube proxy
const PIPED_INSTANCES = [
  'https://pipedapi.kavin.rocks',
  'https://piped-api.garudalinux.org',
  'https://api.piped.projectsegfau.lt',
  'https://piped.tokhmi.xyz',
];

async function ytSearchPiped(query) {
  for (const instance of PIPED_INSTANCES) {
    try {
      const resp = await fetch(
        `${instance}/search?q=${encodeURIComponent(query)}&filter=music_songs`,
        { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(5000) }
      );
      if (!resp.ok) continue;
      const data = await resp.json();
      const items = data.items || [];
      for (const item of items) {
        if (item.url && item.duration > 60) {
          const videoId = item.url.replace('/watch?v=', '');
          return { videoId, instance, title: item.title, thumbnail: item.thumbnail };
        }
      }
    } catch (e) { continue; }
  }
  return null;
}

async function ytAudioPiped(videoId, instance) {
  try {
    const resp = await fetch(
      `${instance}/streams/${videoId}`,
      { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(8000) }
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    const streams = (data.audioStreams || []).filter(s => s.url);
    if (!streams.length) return null;
    streams.sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
    return {
      url: streams[0].url,
      quality: streams[0].quality || 'unknown',
      title: data.title || '',
      thumbnail: data.thumbnailUrl || '',
    };
  } catch (e) { return null; }
}

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

    // /api/yt — Piped se YouTube audio
    if (pathname === '/api/yt') {
      const q = searchParams.get('q') || '';
      if (!q) {
        return new Response(JSON.stringify({ success: false, error: 'Missing q' }), {
          status: 400,
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }

      const found = await ytSearchPiped(q);
      if (!found) {
        return new Response(JSON.stringify({ success: false, error: 'Not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }

      const audio = await ytAudioPiped(found.videoId, found.instance);
      if (!audio) {
        return new Response(JSON.stringify({ success: false, error: 'No audio URL' }), {
          status: 502,
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }

      return new Response(JSON.stringify({ success: true, ...audio, videoId: found.videoId }), {
        status: 200,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      });
    }

    // Baaki sab Railway/Render pe forward
    const skipCache = NO_CACHE.some(r => pathname.startsWith(r));
    const isStream = pathname.startsWith('/api/stream');
    const hasRange = request.headers.has('Range');

    if (!skipCache && !isStream && !hasRange && request.method === 'GET') {
      const cached = await caches.default.match(new Request(request.url, { method: 'GET' }));
      if (cached) {
        const h = new Headers(cached.headers);
        h.set('X-Cache', 'HIT');
        h.set('Access-Control-Allow-Origin', '*');
        return new Response(cached.body, { status: cached.status, headers: h });
      }
    }

    let originResp, usedOrigin;
    for (const origin of ORIGINS) {
      try {
        const url = new URL(request.url);
        url.hostname = new URL(origin).hostname;
        url.protocol = 'https:';
        url.port = '';
        const headers = new Headers(request.headers);
        headers.set('X-Forwarded-For', request.headers.get('CF-Connecting-IP') || '127.0.0.1');
        headers.set('Host', new URL(origin).hostname);
        headers.delete('CF-Ray');
        headers.delete('CF-Visitor');
        const resp = await fetch(new Request(url.toString(), {
          method: request.method,
          headers,
          body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
          signal: AbortSignal.timeout(10000),
        }));
        if (resp.status >= 500) continue;
        originResp = resp;
        usedOrigin = origin;
        break;
      } catch (e) { continue; }
    }

    if (!originResp) {
      return new Response(JSON.stringify({ error: 'All origins failed' }), {
        status: 502,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      });
    }

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

    if (!skipCache && !isStream && !hasRange && request.method === 'GET' && originResp.status === 200) {
      const ttl = getTTL(pathname);
      const toCache = response.clone();
      toCache.headers.set('Cache-Control', `public, max-age=${ttl}`);
      ctx.waitUntil(caches.default.put(new Request(request.url, { method: 'GET' }), toCache));
    }

    return response;
  },
};

function getTTL(p) {
  if (p.startsWith('/api/stream')) return CACHE_TTL.stream;
  if (p.startsWith('/api/songs')) return CACHE_TTL.songs;
  if (p.startsWith('/api/song')) return CACHE_TTL.song;
  if (p.startsWith('/api/saavn')) return CACHE_TTL.song;
  if (/\.(js|css|html|json|png|ico|webp|woff2?)$/.test(p)) return CACHE_TTL.static;
  return CACHE_TTL.default;
}
