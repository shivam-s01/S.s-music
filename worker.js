const ORIGINS = [
  'https://ss-music-production.up.railway.app',
  'https://s-s-music-0uxa.onrender.com',
];

const CACHE_TTL = { stream: 3600, songs: 120, song: 300, static: 0, default: 60 };
const NO_CACHE = ['/health', '/api/yt', '/api/play', '/api/yt-stream'];

// ─── Piped instances — updated working list ───────────────────────────────────
const PIPED_INSTANCES = [
  'https://pipedapi.adminforge.de',
  'https://pipedapi.syncpundit.io',
  'https://piped-api.garudalinux.org',
  'https://api.piped.yt',
  'https://pipedapi.reallyaweso.me',
  'https://piped.smnz.de',
];

// ─── Invidious instances — YT stream fallback ────────────────────────────────
const INVIDIOUS_INSTANCES = [
  'https://invidious.adminforge.de',
  'https://yt.cdaut.de',
  'https://invidious.nerdvpn.de',
  'https://inv.nadeko.net',
  'https://invidious.privacyredirect.com',
  'https://iv.melmac.space',
];

// ─── Search via Piped ────────────────────────────────────────────────────────
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

// ─── Stream via Piped ────────────────────────────────────────────────────────
async function ytAudioPiped(videoId, preferredInstance) {
  const instances = preferredInstance
    ? [preferredInstance, ...PIPED_INSTANCES.filter(i => i !== preferredInstance)]
    : PIPED_INSTANCES;

  for (const instance of instances) {
    try {
      const resp = await fetch(
        `${instance}/streams/${videoId}`,
        { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(8000) }
      );
      if (!resp.ok) continue;
      const data = await resp.json();
      const streams = (data.audioStreams || []).filter(s => s.url);
      if (!streams.length) continue;
      streams.sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));
      return {
        url: streams[0].url,
        quality: streams[0].quality || 'unknown',
        title: data.title || '',
        thumbnail: data.thumbnailUrl || '',
        source: 'piped',
      };
    } catch (e) { continue; }
  }
  return null;
}

// ─── Stream via Invidious fallback ───────────────────────────────────────────
async function ytAudioInvidious(videoId) {
  for (const instance of INVIDIOUS_INSTANCES) {
    try {
      const resp = await fetch(
        `${instance}/api/v1/videos/${videoId}`,
        { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(8000) }
      );
      if (!resp.ok) continue;
      const data = await resp.json();

      // adaptiveFormats — audio only
      const adaptive = (data.adaptiveFormats || [])
        .filter(f => f.type && (f.type.includes('audio/mp4') || f.type.includes('audio/webm')) && f.url)
        .sort((a, b) => (b.bitrate || 0) - (a.bitrate || 0));

      if (adaptive.length) {
        return {
          url: adaptive[0].url,
          quality: adaptive[0].audioQuality || 'unknown',
          title: data.title || '',
          thumbnail: data.videoThumbnails?.[0]?.url || '',
          source: 'invidious',
        };
      }

      // formatStreams fallback
      const fmtStreams = data.formatStreams || [];
      for (const f of fmtStreams.reverse()) {
        if (f.url) {
          return {
            url: f.url,
            quality: f.quality || 'unknown',
            title: data.title || '',
            thumbnail: data.videoThumbnails?.[0]?.url || '',
            source: 'invidious_fmt',
          };
        }
      }
    } catch (e) { continue; }
  }
  return null;
}

// ─── /api/yt-stream — by video ID directly (Flutter app use karta hai) ───────
async function handleYtStream(videoId) {
  if (!videoId) {
    return jsonResp({ success: false, error: 'id required' }, 400);
  }

  // Piped first
  let audio = await ytAudioPiped(videoId, null);
  if (audio) return jsonResp({ success: true, ...audio, videoId });

  // Invidious fallback
  audio = await ytAudioInvidious(videoId);
  if (audio) return jsonResp({ success: true, ...audio, videoId });

  return jsonResp({ success: false, error: 'No stream found' }, 502);
}

// ─── /api/yt — search + stream (web app use karta hai) ───────────────────────
async function handleYtSearch(query) {
  if (!query) {
    return jsonResp({ success: false, error: 'Missing q' }, 400);
  }

  const found = await ytSearchPiped(query);
  if (!found) {
    return jsonResp({ success: false, error: 'Search failed' }, 404);
  }

  const audio = await ytAudioPiped(found.videoId, found.instance);
  if (audio) {
    return jsonResp({ success: true, ...audio, videoId: found.videoId });
  }

  // Invidious fallback
  const invAudio = await ytAudioInvidious(found.videoId);
  if (invAudio) {
    return jsonResp({ success: true, ...invAudio, videoId: found.videoId });
  }

  return jsonResp({ success: false, error: 'No audio URL' }, 502);
}

// ─── Helper ──────────────────────────────────────────────────────────────────
function jsonResp(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
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

    // Flutter app — stream by video ID
    if (pathname === '/api/yt-stream') {
      return handleYtStream(searchParams.get('id') || '');
    }

    // Web app — search + stream
    if (pathname === '/api/yt') {
      return handleYtSearch(searchParams.get('q') || '');
    }

    // ── Cache + origin proxy ─────────────────────────────────────────────────
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
      return jsonResp({ error: 'All origins failed' }, 502);
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
