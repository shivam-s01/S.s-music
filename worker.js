const CACHE_TTL_SECONDS = 3000; // 50 min — stream URLs typically valid for 1hr

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

// ─── Stream via Piped ────────────────────────────────────────────────────────
async function ytAudioPiped(videoId) {
  for (const instance of PIPED_INSTANCES) {
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
    } catch (_) { continue; }
  }
  return null;
}

// ─── Stream via Invidious ────────────────────────────────────────────────────
async function ytAudioInvidious(videoId) {
  for (const instance of INVIDIOUS_INSTANCES) {
    try {
      const resp = await fetch(
        `${instance}/api/v1/videos/${videoId}`,
        { headers: { 'User-Agent': 'Mozilla/5.0' }, signal: AbortSignal.timeout(8000) }
      );
      if (!resp.ok) continue;
      const data = await resp.json();

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
      const fmtStreams = (data.formatStreams || []).slice().reverse();
      for (const f of fmtStreams) {
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
    } catch (_) { continue; }
  }
  return null;
}

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
      for (const item of (data.items || [])) {
        if (item.url && item.duration > 60) {
          return {
            videoId: item.url.replace('/watch?v=', ''),
            instance,
            title: item.title,
            thumbnail: item.thumbnail,
          };
        }
      }
    } catch (_) { continue; }
  }
  return null;
}

// ─── /api/yt-stream — Flutter app (cached) ───────────────────────────────────
async function handleYtStream(videoId, ctx) {
  if (!videoId) {
    return jsonResp({ success: false, error: 'id required' }, 400);
  }

  // Cache check — same video ID = instant response
  const cacheKey = new Request(`https://aurum-cache/yt-stream/${videoId}`);
  const cached = await caches.default.match(cacheKey);
  if (cached) {
    const h = new Headers(cached.headers);
    h.set('X-Cache', 'HIT');
    return new Response(cached.body, { status: cached.status, headers: h });
  }

  // Piped + Invidious parallel — fastest wins
  const audio = await Promise.any([
    ytAudioPiped(videoId),
    ytAudioInvidious(videoId),
  ]).catch(() => null);

  if (!audio) {
    return jsonResp({ success: false, error: 'No stream found' }, 502);
  }

  const resp = jsonResp({ success: true, ...audio, videoId });

  // Store in Cloudflare cache for 50 min
  const toCache = resp.clone();
  const cacheHeaders = new Headers(toCache.headers);
  cacheHeaders.set('Cache-Control', `public, max-age=${CACHE_TTL_SECONDS}`);
  ctx.waitUntil(
    caches.default.put(cacheKey, new Response(toCache.body, {
      status: toCache.status,
      headers: cacheHeaders,
    }))
  );

  return resp;
}

// ─── /api/yt — Web app search + stream ───────────────────────────────────────
async function handleYtSearch(query) {
  if (!query) {
    return jsonResp({ success: false, error: 'Missing q' }, 400);
  }

  const found = await ytSearchPiped(query);
  if (!found) {
    return jsonResp({ success: false, error: 'Search failed' }, 404);
  }

  const audio = await Promise.any([
    ytAudioPiped(found.videoId),
    ytAudioInvidious(found.videoId),
  ]).catch(() => null);

  if (!audio) {
    return jsonResp({ success: false, error: 'No audio URL' }, 502);
  }

  return jsonResp({ success: true, ...audio, videoId: found.videoId });
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

    // Flutter app — stream by video ID
    if (pathname === '/api/yt-stream') {
      return handleYtStream(searchParams.get('id') || '', ctx);
    }

    // Web app — search + stream
    if (pathname === '/api/yt') {
      return handleYtSearch(searchParams.get('q') || '');
    }

    // Health check
    if (pathname === '/health') {
      return jsonResp({ status: 'ok', worker: 'aurum-stream' });
    }

    return jsonResp({ error: 'Not found' }, 404);
  },
};
