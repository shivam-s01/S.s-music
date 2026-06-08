const ORIGINS = [
  'https://ss-music-production.up.railway.app',
  'https://s-s-music-0uxa.onrender.com',
];

const CACHE_TTL = {
  stream: 3600, songs: 120, song: 300, static: 0, default: 60,
};
const NO_CACHE = ['/health', '/api/yt', '/api/play'];

// ── YouTube InnerTube API — Cloudflare IP se kaam karta hai ──────────────────
const YT_INNERTUBE_KEY = 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8';
const YT_CONTEXT = {
  client: {
    clientName: 'ANDROID_MUSIC',
    clientVersion: '6.42.52',
    androidSdkVersion: 30,
    hl: 'en', gl: 'IN',
  }
};

async function ytSearch(query) {
  try {
    const resp = await fetch(
      `https://www.youtube.com/youtubei/v1/search?key=${YT_INNERTUBE_KEY}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0' },
        body: JSON.stringify({
          context: YT_CONTEXT,
          query: query,
          params: 'EgWKAQIIAWoKEAkQBRAKEAMQBA==',
        }),
      }
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    const items = data?.contents?.sectionListRenderer?.contents?.[0]
      ?.musicShelfRenderer?.contents || [];
    for (const item of items) {
      const r = item?.musicResponsiveListItemRenderer;
      if (!r) continue;
      const videoId = r?.overlay?.musicItemThumbnailOverlayRenderer
        ?.content?.musicPlayButtonRenderer?.playNavigationEndpoint
        ?.watchEndpoint?.videoId;
      if (videoId) return videoId;
    }
  } catch (e) {}
  return null;
}

async function ytAudioUrl(videoId) {
  try {
    const resp = await fetch(
      `https://www.youtube.com/youtubei/v1/player?key=${YT_INNERTUBE_KEY}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0' },
        body: JSON.stringify({
          context: YT_CONTEXT,
          videoId: videoId,
          params: '2AMBCgIQBg==',
        }),
      }
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    const formats = data?.streamingData?.adaptiveFormats || [];
    // Sirf audio formats
    const audio = formats.filter(f => f.mimeType?.startsWith('audio/') && f.url);
    if (!audio.length) return null;
    // Best bitrate
    audio.sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
    const best = audio[0];
    return {
      url: best.url,
      quality: best.bitrate ? `${Math.round(best.bitrate / 1000)}kbps` : 'unknown',
      title: data?.videoDetails?.title || '',
      thumbnail: data?.videoDetails?.thumbnail?.thumbnails?.slice(-1)[0]?.url || '',
    };
  } catch (e) {}
  return null;
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
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    // ── /api/yt — YouTube audio directly from Cloudflare ─────────────────────
    if (pathname === '/api/yt') {
      const q = searchParams.get('q') || '';
      const videoId = searchParams.get('id') || await ytSearch(q);
      if (!videoId) {
        return new Response(JSON.stringify({ success: false, error: 'Not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }
      const result = await ytAudioUrl(videoId);
      if (!result) {
        return new Response(JSON.stringify({ success: false, error: 'No audio URL' }), {
          status: 502,
          headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
        });
      }
      return new Response(JSON.stringify({ success: true, ...result }), {
        status: 200,
        headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
      });
    }

    // ── Baaki sab Railway/Render pe forward ───────────────────────────────────
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
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 10000);
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
    if (isStream) {
      h.set('Accept-Ranges', 'bytes');
      h.set('Cache-Control', `public, max-age=${CACHE_TTL.stream}`);
    }

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

function getTTL(pathname) {
  if (pathname.startsWith('/api/stream')) return CACHE_TTL.stream;
  if (pathname.startsWith('/api/songs')) return CACHE_TTL.songs;
  if (pathname.startsWith('/api/song')) return CACHE_TTL.song;
  if (pathname.startsWith('/api/saavn')) return CACHE_TTL.song;
  if (/\.(js|css|html|json|png|ico|webp|woff2?)$/.test(pathname)) return CACHE_TTL.static;
  return CACHE_TTL.default;
}
