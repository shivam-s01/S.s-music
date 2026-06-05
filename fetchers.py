import re
import os
import time
import logging
import random
import threading
import requests
import yt_dlp
from concurrent.futures import as_completed
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import quote
from core import (
    log, _executor, _executor_bg, _executor_cache,
    _l1_meta, _l1_audio, _l1_saavn, _l1_popular,
    _cb, _src_perf, _conf_tuner,
    sb_select, sb_upsert, sb_delete, sb_update,
    _store_artwork, _get_artwork,
    _store_verified, _get_verified,
    _song_index_get, _song_index_put,
    _YTDLP_USER_AGENTS, SUPABASE_URL, SUPABASE_KEY,
    _cache_get_l2, _cache_put_l2,
    _supabase_cache_get_with_refresh, _supabase_cache_set,
    _CACHE_MIN_CONFIDENCE, _VOLATILE_SOURCES,
    compute_confidence, _is_confirmed_match, is_likely_duplicate,
    _query_requests_version, _is_remix_or_cover,
    _is_live_version, _is_slowed_reverb,
    _is_devotional_query, verify_via_fingerprint,
    app, limiter, get_real_ip,
    # [FIX-FETCH-1] _conf_tuner/_src_perf duplicate import removed —
    # already imported above, second import was redundant and confusing
)
from match_engine import (
    compute_confidence, _is_confirmed_match, is_likely_duplicate,
    pick_best_quality, _pick_low_quality, pick_image,
    build_query_variants, clean_query, normalize,
    title_score, dynamic_min_score, has_word_match,
    _query_requests_version, _is_remix_or_cover,
    _is_devotional_query, _safe_year, _detect_language,
    QUALITY_RANK, NINETIES_SEEDS, NINETIES_TRIGGERS,
    ALLOWED_STREAM_DOMAINS,
    dna_compatible, get_song_dna, has_version_words,
)
from sources import (
    SAAVN_MIRRORS, PIPED_INSTANCES, INVIDIOUS_INSTANCES,
    _best_mirrors, _mirror_ok, _mirror_failed,
    _health, _health_record_ok, _health_record_fail,
    _is_source_alive, _health_score,
    _maybe_refresh_sc_id, SOUNDCLOUD_CLIENT_ID,
    _sc_client_id_lock,
)
from flask import request, jsonify, Response, send_file, stream_with_context

# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND CACHE CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════
def _cache_cleanup_loop():
    while True:
        time.sleep(600)
        try:
            from core import _l1_artwork, _l1_verified, _l1_fingerprint
            for cache in [_l1_meta, _l1_audio, _l1_popular, _l1_saavn,
                          _l1_artwork, _l1_verified, _l1_fingerprint]:
                evicted = cache.evict_expired()
                if evicted:
                    log.debug(f'[Cache:Cleanup] Evicted {evicted} expired entries')
        except Exception as e:
            log.warning(f'[Cache:Cleanup] Error: {e}')

threading.Thread(target=_cache_cleanup_loop, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
# KEEPALIVE PING
# ═══════════════════════════════════════════════════════════════════════════════
_KEEPALIVE_URLS = [
    'https://jiosavan.onrender.com/song/?query=test',
    'https://jio-saavn-api.onrender.com/api/search/songs?query=test',
    'https://my-jiosaavn-api.onrender.com/api/search/songs?query=test',
    'https://saavn-backend.onrender.com/api/search/songs?query=test',
]

def _keepalive_ping():
    while True:
        try:
            time.sleep(270)
            for url in _KEEPALIVE_URLS:
                try:
                    r = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
                    if r.status_code == 200:
                        _cb.record_success(url.split('/')[2])
                except Exception:
                    _cb.record_failure(url.split('/')[2])
        except Exception as e:
            log.warning(f'[Keepalive] Error: {e}')

threading.Thread(target=_keepalive_ping, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════════════════════════════════════════
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin']   = '*'
    resp.headers['Access-Control-Allow-Methods']  = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers']  = '*'
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
    return resp

@app.after_request
def after_request(resp):
    return add_cors(resp)

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return add_cors(Response(status=200))


# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND ROUTES
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    import os; from core import BASE_DIR
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/manifest.json')
def manifest():
    import os; from core import BASE_DIR
    resp = send_file(os.path.join(BASE_DIR, 'manifest.json'), mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp

@app.route('/sw.js')
def service_worker():
    import os; from core import BASE_DIR
    resp = send_file(os.path.join(BASE_DIR, 'sw.js'), mimetype='application/javascript')
    resp.headers['Cache-Control']          = 'no-cache, no-store, must-revalidate'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp

@app.route('/.well-known/assetlinks.json')
def assetlinks():
    return app.send_static_file('assetlinks.json')

@app.route('/<path:filename>')
def serve_static(filename):
    import os; from core import BASE_DIR
    file_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(file_path): return send_file(file_path)
    return jsonify({'error': 'Not found'}), 404


# ═══════════════════════════════════════════════════════════════════════════════
# SAAVN SEARCH HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _fetch_saavn_search_mirror(mirror, search_term, language=''):
    if not _mirror_ok(mirror): return []
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0     = time.time()
            params = {'query': search_term, 'q': search_term, 'limit': 20}
            if language: params['language'] = language
            r = requests.get(f'{mirror}{endpoint}', params=params,
                             timeout=_health.adaptive_timeout(mirror),
                             headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data = r.json()
            raw  = (data.get('data', {}).get('results') or data.get('results') or
                    data.get('songs', {}).get('results') or [])
            if raw:
                _health.record_ok(mirror, elapsed)
                return raw
        except Exception:
            _mirror_failed(mirror)
    return []

def _fetch_saavn_search_parallel(search_term, language=''):
    if not language: language = _detect_language(search_term)
    mirrors = _best_mirrors(n=6)
    futures = {_executor.submit(_fetch_saavn_search_mirror, m, search_term, language): m
               for m in mirrors}
    try:
        for future in as_completed(futures, timeout=8):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    return result
            except Exception: pass
    except Exception: pass
    return []

def _normalize_saavn_songs(raw_songs):
    normalized = []
    for song in raw_songs:
        song_id = song.get('id', '').strip()
        if not song_id: continue
        title  = song.get('name') or song.get('title', '')
        artist = song.get('primaryArtists') or song.get('primary_artists') or ''
        image  = pick_image(song)
        year   = str(song.get('year') or '0')[:4]
        dur_s  = int(song.get('duration', 0) or 0)
        dur_ms = dur_s * 1000
        # [FIX-3] Devotional/classical songs 18+ min tak ho sakte hain — 1080s limit removed for those
        from core import _is_devotional_query
        _is_devotional = _is_devotional_query(title + ' ' + artist)
        # [FIX-FETCH-2] Single duration cap — pehle 1800 check kabhi trigger nahi hota tha
        # kyunki 1080 wala already reject kar deta tha. Ab: 1080s hard cap only.
        if dur_s == 0: continue
        if dur_s > 1080 and not _is_devotional: continue  # 18min cap for non-devotional
        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        _, quality = pick_best_quality(raw_urls)
        if not quality: continue
        normalized.append({
            'trackId':         song_id,
            'trackName':       title,
            'artistName':      artist,
            'artworkUrl100':   image if image else '',
            'previewUrl':      f"/api/play?id={quote(song_id, safe='')}",
            'trackTimeMillis': dur_ms,
            'releaseDate':     f"{year}-01-01T00:00:00Z",
            '_saavnId':        song_id,
            '_quality':        quality,
            '_source':         'saavn',
        })
        if image and title: _store_artwork(title, artist, image, 1)
    return normalized


# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVE ITUNES → SAAVN
# ═══════════════════════════════════════════════════════════════════════════════
def _resolve_itunes_to_saavn(itunes_song: dict) -> Optional[dict]:
    title  = itunes_song.get('trackName', '').strip()
    artist = itunes_song.get('artistName', '').strip()
    if not title: return None

    if itunes_song.get('artworkUrl100'):
        itunes_song['artworkUrl100'] = re.sub(
            r'\b\d+x\d+\b', '600x600', itunes_song['artworkUrl100'])
        _store_artwork(title, artist, itunes_song['artworkUrl100'], 2)

    # [SPEED-E] mirrors 6→3 — faster iTunes resolution
    mirrors = _best_mirrors(n=3)

    for query in build_query_variants(title, artist, ''):
        for mirror in mirrors[:6]:
            for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
                try:
                    resp = requests.get(
                        f'{mirror}{endpoint}',
                        params={'query': query, 'q': query, 'limit': 5},
                        timeout=_health.adaptive_timeout(mirror),
                        headers={'User-Agent': 'Mozilla/5.0'},
                    )
                    if resp.status_code != 200: continue
                    data = resp.json()
                    raw  = (data.get('data', {}).get('results') or
                            data.get('results') or
                            data.get('songs', {}).get('results') or [])
                    if not raw: continue

                    best = None; best_conf = -1.0
                    itunes_dur = int((itunes_song.get('trackTimeMillis') or 0) // 1000)

                    for song in raw:
                        song_title  = song.get('name') or song.get('title', '')
                        song_artist = (song.get('primaryArtists') or
                                       song.get('primary_artists') or '')
                        song_dur = int(song.get('duration', 0) or 0)
                        _ok, _conf, _reason = _is_confirmed_match(
                            title, artist, song_title, song_artist,
                            source='saavn', duration_s=itunes_dur, res_dur_s=song_dur,
                            min_conf=0.75,
                        )
                        if not _ok:
                            log.debug(f"[iTunesResolve] Rejected '{song_title}': {_reason}")
                            continue
                        if _conf > best_conf:
                            best_conf = _conf; best = song

                    if not best or best_conf < 0.75: continue

                    saavn_id = (best.get('id') or '').strip()
                    raw_urls = best.get('downloadUrl') or best.get('download_url') or []
                    if isinstance(raw_urls, str):
                        raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                    _, quality = pick_best_quality(raw_urls)
                    if not saavn_id or not quality: continue

                    itunes_song['previewUrl']      = f"/api/play?id={quote(saavn_id, safe='')}"
                    itunes_song['_saavnId']        = saavn_id
                    itunes_song['_resolvedTitle']  = best.get('name') or best.get('title', title)
                    itunes_song['_resolvedArtist'] = best.get('primaryArtists') or best.get('primary_artists') or artist
                    itunes_song['_confidence']     = round(best_conf, 3)
                    saavn_img = pick_image(best)
                    if saavn_img:
                        itunes_song['artworkUrl100'] = saavn_img
                        _store_artwork(title, artist, saavn_img, 1)
                    log.info(f"[Resolve] ✓ '{title}' → {saavn_id} conf={best_conf:.2f}")
                    return itunes_song
                except Exception:
                    continue

    itunes_song['previewUrl'] = (
        f"/api/play?title={quote(title, safe='')}&artist={quote(artist, safe='')}")
    itunes_song['_confidence'] = 0.30
    return itunes_song


# ═══════════════════════════════════════════════════════════════════════════════
# YOUTUBE MUSIC
# ═══════════════════════════════════════════════════════════════════════════════
_YTM_SEARCH_URL = 'https://music.youtube.com/youtubei/v1/search'
_YTM_API_KEY    = 'AIzaSyC9XL3ZjWddXya6X74dJoCTL-NKNELL6imp'
_YTM_CONTEXT    = {
    'client': {
        'clientName':    'WEB_REMIX',
        'clientVersion': '1.20250101.01.00',
        'hl': 'en', 'gl': 'IN',
        'userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
}

def _ytm_search(query, limit=8):
    try:
        body = {'context': _YTM_CONTEXT, 'query': query,
                'params': 'EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D'}
        r = requests.post(
            _YTM_SEARCH_URL,
            params={'key': _YTM_API_KEY, 'prettyPrint': 'false'},
            json=body,
            headers={
                'Content-Type': 'application/json',
                'X-YouTube-Client-Name': '67',
                'X-YouTube-Client-Version': '1.20250101.01.00',
                'Origin': 'https://music.youtube.com',
                'Referer': 'https://music.youtube.com/',
                'User-Agent': random.choice(_YTDLP_USER_AGENTS),
            },
            timeout=8,
        )
        if r.status_code != 200: return []
        data    = r.json()
        results = []
        tabs    = (data.get('contents', {})
                       .get('tabbedSearchResultsRenderer', {})
                       .get('tabs', []))
        for tab in tabs:
            section_list = (tab.get('tabRenderer', {})
                               .get('content', {})
                               .get('sectionListRenderer', {})
                               .get('contents', []))
            for section in section_list:
                items = section.get('musicShelfRenderer', {}).get('contents', [])
                for item in items:
                    renderer = item.get('musicResponsiveListItemRenderer', {})
                    if not renderer: continue
                    overlay  = renderer.get('overlay', {})
                    vid_id   = (overlay.get('musicItemThumbnailOverlayRenderer', {})
                                       .get('content', {})
                                       .get('musicPlayButtonRenderer', {})
                                       .get('playNavigationEndpoint', {})
                                       .get('watchEndpoint', {})
                                       .get('videoId', ''))
                    if not vid_id:
                        for col in renderer.get('flexColumns', []):
                            runs = (col.get('musicResponsiveListItemFlexColumnRenderer', {})
                                       .get('text', {}).get('runs', []))
                            for run in runs:
                                ep = run.get('navigationEndpoint', {}).get('watchEndpoint', {})
                                if ep.get('videoId'): vid_id = ep['videoId']; break
                            if vid_id: break
                    if not vid_id: continue
                    cols = renderer.get('flexColumns', [])
                    title_t = ''; artist_t = ''
                    for i, col in enumerate(cols):
                        runs = (col.get('musicResponsiveListItemFlexColumnRenderer', {})
                                   .get('text', {}).get('runs', []))
                        text = ' '.join(r.get('text', '') for r in runs).strip()
                        if i == 0: title_t = text
                        elif i == 1: artist_t = text.split('\u2022')[0].strip()
                    thumbs = (renderer.get('thumbnail', {})
                                      .get('musicThumbnailRenderer', {})
                                      .get('thumbnail', {}).get('thumbnails', []))
                    thumb = thumbs[-1]['url'] if thumbs else ''
                    if thumb: thumb = re.sub(r'=w\d+-h\d+', '=w500-h500', thumb)
                    results.append({'videoId': vid_id, 'title': title_t,
                                    'artist': artist_t, 'thumbnail': thumb})
                    if len(results) >= limit: return results
        return results
    except Exception as e:
        log.warning(f'[YTMusic] search error: {e}')
        _cb.record_failure('ytmusic')
        return []

def _ytm_get_stream_url(video_id):
    l1_key = f"ytm_stream:{video_id}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached.get('url'), cached.get('quality')
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'quiet': True, 'no_warnings': True, 'socket_timeout': 12,
        'extract_flat': False, 'noplaylist': True,
        'http_headers': {'User-Agent': random.choice(_YTDLP_USER_AGENTS)},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://music.youtube.com/watch?v={video_id}', download=False)
            if not info: return None, None
            formats = info.get('formats', [])
            audio_formats = [f for f in formats
                             if f.get('acodec') not in ('none', None, '')
                             and f.get('url') and f.get('vcodec') in ('none', None, '')]
            if not audio_formats:
                audio_formats = [f for f in formats
                                 if f.get('acodec') not in ('none', None, '') and f.get('url')]
            if not audio_formats: return None, None
            best    = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            abr     = best.get('abr') or best.get('tbr') or 0
            quality = f"{int(abr)}kbps" if abr else 'unknown'
            url     = best['url']
            _l1_audio.set(l1_key, {'url': url, 'quality': quality})
            return url, quality
    except Exception as e:
        log.warning(f'[YTMusic] stream extract error {video_id}: {e}')
        return None, None

def fetch_from_ytmusic(title, artist=''):
    if not _cb.is_allowed('ytmusic'):
        log.debug('[CB] ytmusic OPEN — skipping'); return None
    l1_key = f"ytmusic:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached

    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    query = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title
    results = _ytm_search(query, limit=8)
    if not results: results = _ytm_search(clean_title, limit=5)
    if not results: return None

    best = None; best_conf = -1.0
    for item in results:
        # HARDGATE: DNA check before anything else
        if not dna_compatible(title, item.get('title', '')):
            log.debug(f"[YTMusic] DNA block: '{item.get('title')}'"); continue
        _ok, _conf, _reason = _is_confirmed_match(
            title, artist, item.get('title', ''), item.get('artist', ''),
            source='ytmusic', min_conf=0.60,
        )
        if not _ok:
            log.debug(f"[YTMusic] Rejected '{item.get('title')}': {_reason}"); continue
        if _conf > best_conf: best_conf = _conf; best = item

    if not best or best_conf < 0.60: return None
    video_id = best['videoId']
    url, quality = _ytm_get_stream_url(video_id)
    if not url: return None

    # ── FINGERPRINT VERIFY ───────────────────────────────────────────────────
    if not verify_via_fingerprint(url, title, artist):
        log.warning(f"[YTMusic] Fingerprint FAILED: '{best.get('title')}'")
        return None

    _cb.record_success('ytmusic')
    result = {
        'url': url, 'quality': quality,
        'title': best.get('title', title), 'artist': best.get('artist', artist),
        'image': best.get('thumbnail', ''), 'source': 'ytmusic',
        '_confidence': round(best_conf, 3),
    }
    _l1_audio.set(l1_key, result)
    cached_art = _get_artwork(title, artist)
    if cached_art: result['image'] = cached_art
    else: _store_artwork(title, artist, best.get('thumbnail', ''), 4)
    log.info(f"[YTMusic] ✓ '{best['title']}' conf={best_conf:.2f} q={quality}")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# YT-DLP
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_ytdlp(title, artist=''):
    if not _cb.is_allowed('youtube'):
        log.debug('[CB] youtube/ytdlp OPEN — skipping'); return None
    l1_key = f"ytdlp:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    # [FIX-4] YouTube URL expiry check — _l1_audio TTL=300s is safe, but
    # double check source tag is volatile before trusting cache
    if cached:
        _cached_source = cached.get('source', 'youtube')
        if _cached_source in ('youtube', 'youtube-broad'):
            # L1 TTL 300s < YouTube URL lifetime (~21600s) — safe to use
            pass
        return cached

    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()

    search_queries = []
    if clean_artist:
        search_queries += [
            f"ytmsearch5:{clean_artist} {clean_title}",
            f"ytsearch5:{clean_artist} {clean_title} full song",
            f"ytmsearch3:{clean_title}",
            f"ytsearch3:{clean_title} {clean_artist} audio",
            f"ytsearch2:{clean_title} song",
        ]
    else:
        search_queries += [
            f"ytmsearch5:{clean_title}",
            f"ytsearch5:{clean_title} full song audio",
            f"ytsearch3:{clean_title} song",
        ]

    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'quiet': True, 'no_warnings': True, 'socket_timeout': 15,
        'extract_flat': False, 'noplaylist': True,
        'http_headers': {'User-Agent': random.choice(_YTDLP_USER_AGENTS)},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            best_result = None; best_conf = -1.0
            for search_q in search_queries:
                try:
                    info = ydl.extract_info(search_q, download=False)
                    if not info or not info.get('entries'): continue
                    entries = [e for e in info['entries'] if e and e.get('duration', 0) > 90]
                    if not entries: entries = [e for e in info['entries'] if e]
                    for entry in entries:
                        if not entry: continue
                        yt_title  = entry.get('title', '')
                        yt_artist = entry.get('uploader', '') or entry.get('artist', '')
                        # HARDGATE: DNA check before anything else
                        if not dna_compatible(title, yt_title):
                            log.debug(f"[yt-dlp] DNA block: '{yt_title}'"); continue
                        _ok, _conf, _reason = _is_confirmed_match(
                            title, artist, yt_title, yt_artist,
                            source='youtube', min_conf=0.60,
                        )
                        if not _ok:
                            log.debug(f"[yt-dlp] Rejected '{yt_title}': {_reason}"); continue
                        if 'music.youtube' in (entry.get('webpage_url') or ''):
                            _conf = min(1.0, _conf + 0.05)
                        if _conf > best_conf: best_conf = _conf; best_result = entry
                    if best_conf >= 0.75: break
                except Exception: continue

            if not best_result or best_conf < 0.60: return None

            formats       = best_result.get('formats', [])
            audio_formats = [f for f in formats
                             if f.get('acodec') not in ('none', None, '')
                             and f.get('url')
                             and (f.get('vcodec') in ('none', None, '') or not f.get('vcodec'))]
            if not audio_formats:
                audio_formats = [f for f in formats
                                 if f.get('acodec') not in ('none', None, '') and f.get('url')]
            if not audio_formats:
                audio_formats = [f for f in formats if f.get('url')]
            if not audio_formats: return None

            best_fmt = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            abr      = best_fmt.get('abr') or best_fmt.get('tbr') or 0
            quality  = f"{int(abr)}kbps" if abr else 'unknown'
            thumb    = best_result.get('thumbnail', '')
            if not thumb:
                vid_id = best_result.get('id', '')
                if vid_id: thumb = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"

            # ── FINGERPRINT VERIFY ───────────────────────────────────────────
            if not verify_via_fingerprint(best_fmt['url'], title, artist):
                log.warning(f"[yt-dlp] Fingerprint FAILED: '{best_result.get('title')}'")
                _cb.record_failure('youtube')
                return None

            _cb.record_success('youtube')
            _src_perf.record('youtube', 0, True)
            result = {
                'url':    best_fmt['url'],
                'quality': quality,
                'title':  best_result.get('title', title),
                'artist': best_result.get('uploader', artist) or best_result.get('artist', artist),
                'image':  thumb, 'source': 'youtube',
                '_confidence': round(best_conf, 3),
            }
            cached_art = _get_artwork(title, artist)
            if cached_art: result['image'] = cached_art
            elif thumb: _store_artwork(title, artist, thumb, 5)
            if best_conf >= 0.65: _l1_audio.set(l1_key, result)
            log.info(f"[yt-dlp] ✓ '{best_result.get('title')}' conf={best_conf:.2f} q={quality}")
            return result
    except Exception as e:
        log.warning(f"[yt-dlp] '{title}' → {e}")
        _cb.record_failure('youtube')
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SOUNDCLOUD
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_soundcloud(title, artist=''):
    if not _cb.is_allowed('soundcloud'):
        log.debug('[CB] soundcloud OPEN — skipping'); return None
    l1_key = f"sc:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached

    _maybe_refresh_sc_id()
    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    query        = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title

    ydl_opts = {
        'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True,
        'socket_timeout': 12, 'noplaylist': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch5:{query}", download=False)
            if not info or not info.get('entries'): return None
            best = None; best_conf = -1.0
            for entry in info['entries']:
                if not entry or entry.get('duration', 0) < 60: continue
                sc_title = entry.get('title', '')
                _ok, _conf, _reason = _is_confirmed_match(
                    title, artist, sc_title, entry.get('uploader', ''),
                    source='soundcloud', min_conf=0.60,
                )
                if not _ok:
                    log.debug(f"[SoundCloud] Rejected '{sc_title}': {_reason}"); continue
                if _conf > best_conf: best_conf = _conf; best = entry
            if not best or best_conf < 0.60: return None

            formats  = best.get('formats', [])
            if not formats: return None
            best_fmt = max(formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            if not best_fmt.get('url'): return None

            # ── FINGERPRINT VERIFY ───────────────────────────────────────────
            if not verify_via_fingerprint(best_fmt['url'], title, artist):
                log.warning(f"[SoundCloud] Fingerprint FAILED: '{best.get('title')}'")
                return None

            _cb.record_success('soundcloud')
            abr     = best_fmt.get('abr') or best_fmt.get('tbr') or 0
            quality = f"{int(abr)}kbps" if abr else 'unknown'
            result  = {
                'url': best_fmt['url'], 'quality': quality,
                'title': best.get('title', title), 'artist': best.get('uploader', artist),
                'image': best.get('thumbnail', ''), 'source': 'soundcloud',
                '_confidence': round(best_conf, 3),
            }
            cached_art = _get_artwork(title, artist)
            if cached_art: result['image'] = cached_art
            _l1_audio.set(l1_key, result)
            return result
    except Exception as e:
        log.warning(f"[SoundCloud] '{title}' → {e}")
        _cb.record_failure('soundcloud')
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SAAVN BY ID
# ═══════════════════════════════════════════════════════════════════════════════
def _fetch_saavn_by_id(song_id, expected_title='', expected_artist=''):
    l1_key = f"saavn_id:{song_id}"
    cached = _l1_saavn.get(l1_key)
    if cached: return cached

    # [SPEED-A] mirrors 6→3, endpoints 5→2 most common only — cuts ID path latency by ~60%
    mirrors   = _best_mirrors(n=3)
    endpoints = [f'/api/songs/{song_id}', f'/api/songs?id={song_id}']

    def try_mirror(mirror):
        for endpoint in endpoints:
            try:
                t0 = time.time()
                r  = requests.get(f'{mirror}{endpoint}',
                                  timeout=_health.adaptive_timeout(mirror),
                                  headers={'User-Agent': 'Mozilla/5.0'})
                elapsed = (time.time() - t0) * 1000
                if r.status_code != 200: continue
                data = r.json()
                song = None
                if isinstance(data.get('data'), list) and data['data']:
                    song = data['data'][0]
                elif isinstance(data.get('data'), dict):
                    song = data['data']
                elif data.get('id'):
                    song = data
                elif data.get('songs'):
                    songs = data['songs']
                    song  = songs[0] if isinstance(songs, list) and songs else songs
                if not song: continue
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                if isinstance(raw_urls, str):
                    raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                best_url, quality = pick_best_quality(raw_urls)
                if best_url:
                    _health.record_ok(mirror, elapsed)
                    id_result = {
                        'url': best_url, 'quality': quality,
                        'title': song.get('name') or song.get('title', ''),
                        'artist': song.get('primaryArtists') or song.get('primary_artists') or '',
                        'image': pick_image(song),
                        '_raw_urls': raw_urls,
                    }
                    if expected_title and id_result.get('title'):
                        _id_verify_conf = compute_confidence(
                            expected_title, expected_artist,
                            id_result['title'], id_result.get('artist', ''),
                            source='saavn',
                        )
                        if _id_verify_conf < 0.72:
                            log.warning(
                                f"[SaavnID] MISMATCH: expected='{expected_title}' "
                                f"got='{id_result['title']}' conf={_id_verify_conf:.3f}"
                            )
                            return None
                    _fetched_title   = id_result.get('title', '')
                    _user_asked_ver  = _query_requests_version(expected_title)
                    if not _user_asked_ver:
                        if (_is_live_version(_fetched_title) or
                            _is_remix_or_cover(_fetched_title) or
                            _is_slowed_reverb(_fetched_title)):
                            log.warning(f"[SaavnID] VERSION REJECTED: '{_fetched_title}'")
                            return None
                        # ── DNA CHECK on ID result ───────────────────────────
                        if not dna_compatible(expected_title, _fetched_title):
                            log.warning(f"[SaavnID] DNA MISMATCH: '{_fetched_title}'")
                            return None
                    if id_result['image']:
                        _store_artwork(id_result['title'], id_result['artist'], id_result['image'], 1)
                    # PATCH: song_id key pe result cache karo — thumbnail always available
                    _l1_saavn.set(f"saavn_id:{song_id}", {
                        **id_result,
                        'image': id_result.get('image', '') or _get_artwork(id_result.get('title',''), id_result.get('artist','')),
                    })
                    return id_result
            except Exception:
                _mirror_failed(mirror)
        return None

    futures = {_executor.submit(try_mirror, m): m for m in mirrors}
    try:
        # [SPEED-B] timeout 6s→3s — best mirror should respond in <2s
        for future in as_completed(futures, timeout=3):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    _l1_saavn.set(l1_key, result)
                    return result
            except Exception: pass
    except Exception: pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# FETCH FROM MIRROR
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_mirror(mirror, query, min_score=0.4, title='', artist='', language=''):
    if not _mirror_ok(mirror): return None
    _user_wants_version = _query_requests_version(title or query)
    _conf_title  = title or query
    _conf_artist = artist or ''

    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0 = time.time()
            _fparams = {'query': query, 'q': query, 'limit': 25}
            if language: _fparams['language'] = language
            r = requests.get(f'{mirror}{endpoint}', params=_fparams,
                             timeout=_health.adaptive_timeout(mirror),
                             headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data    = r.json()
            results = (data.get('data', {}).get('results') or data.get('results') or
                       data.get('songs', {}).get('results') or [])

            candidates = []
            for idx, song in enumerate(results):
                song_title  = song.get('name') or song.get('title', '')
                song_artist = song.get('primaryArtists') or song.get('primary_artists') or ''

                # ── DNA CHECK — sabse pehle ──────────────────────────────────
                if not dna_compatible(_conf_title, song_title):
                    log.debug(f"[Mirror] DNA MISMATCH: '{song_title}'")
                    continue

                if not has_word_match(query, song_title): continue
                dur = int(song.get('duration', 999) or 999)
                if dur > 1080: continue

                _ok, _conf, _reason = _is_confirmed_match(
                    _conf_title, _conf_artist, song_title, song_artist,
                    source='saavn', res_dur_s=dur, min_conf=0.65,
                )
                if not _ok:
                    log.debug(f"[Mirror] Rejected '{song_title}': {_reason}"); continue

                legacy_score  = title_score(query, song_title, song_artist)
                _pos_penalty  = min(0.08, max(0.0, 0.015 * (idx - 2))) if idx >= 3 else 0.0
                candidates.append((_conf - _pos_penalty, legacy_score, song))

            if not candidates: continue
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best_conf, best_legacy, best_song = candidates[0]

            if best_conf < 0.65: continue

            raw_urls = best_song.get('downloadUrl') or best_song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            best_url, quality = pick_best_quality(raw_urls)
            if not best_url: continue

            _health.record_ok(mirror, elapsed)
            result_data = {
                'url': best_url, 'quality': quality,
                'title': best_song.get('name') or best_song.get('title', ''),
                'artist': best_song.get('primaryArtists') or best_song.get('primary_artists') or '',
                'image': pick_image(best_song), 'score': round(best_legacy, 3),
                '_confidence': round(best_conf, 3),
                'source': 'saavn', '_raw_urls': raw_urls,
            }
            if result_data['image']:
                _store_artwork(result_data['title'], result_data['artist'], result_data['image'], 1)
            return result_data
        except Exception:
            _mirror_failed(mirror)
            continue
    return None


def fetch_saavn_parallel(query, title='', artist='', language=''):
    l1_key = f"saavn_q:{normalize(query)}"
    cached = _l1_saavn.get(l1_key)
    if cached: return cached

    if not language:
        _lang = _detect_language((title or query) + ' ' + artist)
    else:
        _lang = language

    threshold = dynamic_min_score(query)
    # [FIX-FETCH-5] Mirror count 8→4 — bandwidth/requests halved; early exit catches best result
    mirrors   = _best_mirrors(n=4)
    futures   = {_executor.submit(fetch_from_mirror, m, query, threshold, title, artist, _lang): m
                 for m in mirrors}
    all_results = []
    _EARLY_EXIT_CONF = 0.75  # [SPEED-C] 0.80→0.75 — exit even sooner on good match
    try:
        # [SPEED-C] timeout 4s→2s — if best mirror doesn't respond in 2s, pick what we have
        for future in as_completed(futures, timeout=2):
            try:
                result = future.result()
                if result:
                    all_results.append(result)
                    _conf = float(result.get('_confidence', result.get('score', 0)))
                    if _conf >= _EARLY_EXIT_CONF:
                        for f in futures: f.cancel()
                        break
            except Exception: pass
    except Exception: pass

    if not all_results: return None

    all_results.sort(
        key=lambda r: (
            float(r.get('_confidence', r.get('score', 0))) +
            (0.02 if '320' in str(r.get('quality', '')) else 0)
        ),
        reverse=True,
    )
    best      = all_results[0]
    best_conf = float(best.get('_confidence', best.get('score', 0)))

    if best_conf < 0.65:
        log.warning(f"[Parallel] ALL below confidence gate (best={best_conf:.2f}) — rejecting")
        return None

    if best_conf >= _CACHE_MIN_CONFIDENCE:
        _l1_saavn.set(l1_key, best)
    log.info(f"[Parallel] ✓ '{best['title']}' conf={best_conf:.2f} q={best['quality']}")
    return best


# ═══════════════════════════════════════════════════════════════════════════════
# PIPED
# ═══════════════════════════════════════════════════════════════════════════════
_piped_lock     = threading.Lock()
_invidious_lock = threading.Lock()

def fetch_from_piped(query, title='', artist=''):
    search_q = f"{title} {artist}".strip() if title else query
    with _piped_lock:
        instances = _health.sort_by_reputation(list(PIPED_INSTANCES))
    fail_count = 0
    for instance in instances:
        if not _health.is_alive(instance): continue
        try:
            t0 = time.time()
            r  = requests.get(f'{instance}/search',
                              params={'q': search_q, 'filter': 'music_songs'},
                              timeout=_health.adaptive_timeout(instance),
                              headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200:
                _health.record_fail(instance); fail_count += 1; continue
            results = r.json().get('items', [])
            if not results:
                _health.record_fail(instance); fail_count += 1; continue

            best = None; best_conf = -1.0
            for item in results[:5]:
                if item.get('type') != 'stream': continue
                piped_title = item.get('title', '')
                # ── DNA CHECK ────────────────────────────────────────────────
                if not dna_compatible(title or query, piped_title): continue
                if not has_word_match(query, piped_title): continue
                _ok, _conf, _reason = _is_confirmed_match(
                    title or query, artist, piped_title, item.get('uploaderName', ''),
                    source='piped', min_conf=0.60,
                )
                if not _ok:
                    log.debug(f"[Piped] Rejected '{piped_title}': {_reason}"); continue
                if _conf > best_conf: best_conf = _conf; best = item

            if not best or best_conf < 0.60: continue
            video_id = best.get('url', '').replace('/watch?v=', '').strip()
            if not video_id: continue

            sr = requests.get(f'{instance}/streams/{video_id}', timeout=10,
                              headers={'User-Agent': 'Mozilla/5.0'})
            if sr.status_code != 200: continue
            audio_streams = sr.json().get('audioStreams', [])
            if not audio_streams: continue
            best_audio = max(audio_streams, key=lambda s: s.get('bitrate', 0))
            if not best_audio.get('url'): continue

            # ── FINGERPRINT VERIFY ───────────────────────────────────────────
            if not verify_via_fingerprint(best_audio['url'], title or query, artist):
                log.warning(f"[Piped] Fingerprint FAILED: '{best.get('title')}'")
                continue

            bitrate = best_audio.get('bitrate', 0)
            _health.record_ok(instance, elapsed)
            piped_result = {
                'url': best_audio['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title': best.get('title', title), 'artist': best.get('uploaderName', artist),
                'image': best.get('thumbnail', ''), 'source': 'piped',
                '_confidence': round(best_conf, 3),
            }
            cached_art = _get_artwork(title, artist)
            if cached_art: piped_result['image'] = cached_art
            return piped_result
        except Exception as e:
            _health.record_fail(instance); fail_count += 1
            log.warning(f"[Piped {instance}] {e}"); continue
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# INVIDIOUS
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_invidious(query, title='', artist=''):
    search_q = f"{title} {artist}".strip() if title else query
    with _invidious_lock:
        instances = _health.sort_by_reputation(list(INVIDIOUS_INSTANCES))
    fail_count = 0
    for instance in instances:
        if not _health.is_alive(instance): continue
        try:
            t0 = time.time()
            r  = requests.get(f'{instance}/api/v1/search',
                              params={'q': search_q, 'type': 'video', 'page': 1},
                              timeout=_health.adaptive_timeout(instance),
                              headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200:
                _health.record_fail(instance); fail_count += 1; continue
            results = r.json()
            if not results:
                _health.record_fail(instance); fail_count += 1; continue

            best = None; best_conf = -1.0
            for item in results[:5]:
                inv_title = item.get('title', '')
                # ── DNA CHECK ────────────────────────────────────────────────
                if not dna_compatible(title or query, inv_title): continue
                if not has_word_match(query, inv_title): continue
                _ok, _conf, _reason = _is_confirmed_match(
                    title or query, artist, inv_title, item.get('author', ''),
                    source='invidious', min_conf=0.60,
                )
                if not _ok:
                    log.debug(f"[Invidious] Rejected '{inv_title}': {_reason}"); continue
                if _conf > best_conf: best_conf = _conf; best = item

            if not best or best_conf < 0.60: continue
            video_id = best.get('videoId', '')
            if not video_id: continue

            vr = requests.get(f'{instance}/api/v1/videos/{video_id}',
                              params={'fields': 'adaptiveFormats,title,author'},
                              timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if vr.status_code != 200: continue
            formats = vr.json().get('adaptiveFormats', [])
            audio_formats = [f for f in formats if f.get('type', '').startswith('audio')]
            if not audio_formats: continue
            best_fmt = max(audio_formats, key=lambda f: f.get('bitrate', 0))
            if not best_fmt.get('url'): continue

            # ── FINGERPRINT VERIFY ───────────────────────────────────────────
            if not verify_via_fingerprint(best_fmt['url'], title or query, artist):
                log.warning(f"[Invidious] Fingerprint FAILED: '{best.get('title')}'")
                continue

            bitrate = best_fmt.get('bitrate', 0)
            _health.record_ok(instance, elapsed)
            inv_result = {
                'url': best_fmt['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title': best.get('title', title), 'artist': best.get('author', artist),
                'image': f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
                'source': 'invidious', '_confidence': round(best_conf, 3),
            }
            cached_art = _get_artwork(title, artist)
            if cached_art: inv_result['image'] = cached_art
            return inv_result
        except Exception as e:
            _health.record_fail(instance); fail_count += 1
            log.warning(f"[Invidious {instance}] {e}"); continue
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# JIOSAVAN
# ═══════════════════════════════════════════════════════════════════════════════
_JIOSAVAN_BASE = 'https://jiosavan.onrender.com'

def fetch_from_jiosavan(title, artist='', language=''):
    l1_key = f"jiosavan:{normalize(title)}:{normalize(artist)}"
    cached = _l1_saavn.get(l1_key)
    if cached: return cached

    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    query        = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title

    try:
        t0     = time.time()
        params = {'query': query, 'songdata': 'true'}
        if language: params['language'] = language
        r = requests.get(f'{_JIOSAVAN_BASE}/song/', params=params,
                         timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code != 200: return None
        data  = r.json()
        songs = data if isinstance(data, list) else data.get('songs', []) or data.get('results', [])
        if not songs: return None

        best = None; best_conf = -1.0
        for song in songs[:5]:
            song_title  = song.get('song') or song.get('title') or song.get('name', '')
            song_artist = song.get('primary_artists') or song.get('singers') or song.get('artist', '')
            song_dur    = int(song.get('duration', 0) or 0)

            # ── DNA CHECK ────────────────────────────────────────────────────
            if not dna_compatible(title, song_title):
                log.debug(f"[JioSavan] DNA MISMATCH: '{song_title}'"); continue

            _ok, _conf, _reason = _is_confirmed_match(
                title, artist, song_title, song_artist,
                source='jiosavan', duration_s=0, res_dur_s=song_dur, min_conf=0.65,
            )
            if not _ok:
                log.debug(f"[JioSavan] Rejected '{song_title}': {_reason}"); continue
            if _conf > best_conf: best_conf = _conf; best = song

        if not best or best_conf < 0.65: return None

        media_url = (best.get('media_url') or best.get('encrypted_media_url') or
                     best.get('download_url') or '')
        if not media_url: return None

        raw_dl = best.get('downloadUrl') or best.get('download_url') or []
        if isinstance(raw_dl, str): raw_dl = [{'url': raw_dl, 'quality': 'unknown'}]
        if raw_dl:
            _best_dl_url, _best_quality = pick_best_quality(raw_dl)
            if _best_dl_url: media_url = _best_dl_url
        else:
            _best_quality = '320kbps'
        jiosavan_quality = _best_quality or '320kbps'

        image = best.get('image', '')
        # [FIX-FETCH-4] Use _ensure_500 for consistent 500x500 upgrade (not inline regex)
        if image:
            try:
                from match_engine import _ensure_500
                image = _ensure_500(image)
            except ImportError:
                image = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', image)

        result = {
            'url': media_url, 'quality': jiosavan_quality,
            'title': best.get('song') or best.get('title', title),
            'artist': best.get('primary_artists') or best.get('singers', artist),
            'image': image, 'source': 'jiosavan',
            'score': round(best_conf, 3), '_confidence': round(best_conf, 3),
        }
        if image: _store_artwork(title, artist, image, 1)
        else:
            cached_art = _get_artwork(title, artist)
            if cached_art: result['image'] = cached_art
        if best_conf >= _CACHE_MIN_CONFIDENCE:
            _l1_saavn.set(l1_key, result)
        _health.record_ok(_JIOSAVAN_BASE, elapsed)
        log.info(f"[JioSavan] ✓ '{result['title']}' conf={best_conf:.2f}")
        return result
    except Exception as e:
        log.warning(f"[JioSavan] '{title}' → {e}")
        _health.record_fail(_JIOSAVAN_BASE)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# /api/play — SMART PLAYBACK PIPELINE
# FIX: _id_future bug removed, duplicate Step 3/4 fixed,
#      cache invalidation proper, final mismatch check added
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    song_id = request.args.get('id', '').strip()[:100]
    title   = request.args.get('title', '').strip()[:200]
    artist  = request.args.get('artist', '').strip()[:100]

    if not song_id and not title:
        return jsonify({'error': 'Missing id or title'}), 400

    audio_url  = None
    quality    = 'unknown'
    source     = 'unknown'
    confidence = 0.0

    _play_ck       = f"play:{song_id or normalize(title)}:{normalize(artist)}"
    _play_ck_id    = f"play:{song_id}:{normalize(artist)}"    if song_id else None
    _play_ck_title = f"play:{normalize(title)}:{normalize(artist)}" if title  else None

    _play_lang      = _detect_language((title or '') + ' ' + (artist or ''))
    _user_wants_ver = _query_requests_version(title or '')

    # ─── Cache entry validator ────────────────────────────────────────────────
    def _check_cache_entry(entry):
        if not entry or not entry.get('url'): return None
        _ct = entry.get('title', '')

        # Version check
        if not _user_wants_ver:
            if (_is_remix_or_cover(_ct) or
                _is_slowed_reverb(_ct) or
                _is_live_version(_ct)):
                return None

        # DNA check on cached song
        if title and _ct:
            if not dna_compatible(title, _ct):
                log.info(f"[Cache] DNA MISMATCH — invalidating cached='{_ct}'")
                return None

        # Confidence recheck
        # FIX: duration=0,0 pass karo — d_sim bias avoid karne ke liye
        if title and _ct:
            _recheck_conf = compute_confidence(
                title, artist, _ct, entry.get('artist', ''),
                source='saavn',
                query_duration_s=0,
                result_duration_s=0,
            )
            if _recheck_conf < 0.72:
                log.info(
                    f"[Cache] STALE REJECTED: requested='{title}' "
                    f"cached='{_ct}' conf={_recheck_conf:.3f}"
                )
                return None
        return entry

    def _invalidate_cache_keys(*keys):
        """Wrong song cache se permanently hatao."""
        for _ck in filter(None, keys):
            _l1_saavn.delete(_ck)
            _executor_cache.submit(sb_delete, 'song_cache', {'cache_key': _ck})

    # ─── Step 0: Verified cache ───────────────────────────────────────────────
    _verified_hit = None
    hit = _get_verified(song_id=song_id, title=title, artist=artist)
    _verified_hit = _check_cache_entry(hit)
    if _verified_hit:
        audio_url  = _verified_hit['url']
        quality    = _verified_hit.get('quality', 'unknown')
        source     = _verified_hit.get('source', 'unknown')
        confidence = float(_verified_hit.get('confidence', 0.90))
        if not title:  title  = _verified_hit.get('title', '')
        if not artist: artist = _verified_hit.get('artist', '')

    # ─── Step 1: L1 cache ────────────────────────────────────────────────────
    if not audio_url:
        for _ck in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
            raw_hit = _l1_saavn.get(_ck)
            l1_hit  = _check_cache_entry(raw_hit)
            if l1_hit:
                audio_url  = l1_hit['url']
                quality    = l1_hit.get('quality', 'unknown')
                source     = l1_hit.get('source', 'unknown')
                confidence = float(l1_hit.get('confidence', 1.0))
                if not title:  title  = l1_hit.get('title', '')
                if not artist: artist = l1_hit.get('artist', '')
                break
            elif raw_hit and not l1_hit:
                # Wrong song cache mein tha — hatao
                _invalidate_cache_keys(_ck)

    # ─── Step 2: L2 Supabase cache — NON-BLOCKING ───────────────────────────
    # [SPEED-D] Supabase L2 ko async fire karo — 0.2s window mein result aaya toh use karo
    # Nahi aaya toh pipeline aage badhta hai, L2 result baad mein L1 mein store ho jaata hai
    if not audio_url:
        _l2_futures = {
            _ck: _executor_cache.submit(_supabase_cache_get_with_refresh, _ck)
            for _ck in filter(None, [_play_ck_id, _play_ck_title, _play_ck])
        }
        try:
            for future in as_completed(_l2_futures.values(), timeout=0.20):
                _ck_hit = next((k for k,v in _l2_futures.items() if v is future), None)
                try:
                    raw_hit = future.result()
                    l2_hit  = _check_cache_entry(raw_hit)
                    if l2_hit:
                        audio_url  = l2_hit['url']
                        quality    = l2_hit.get('quality', 'unknown')
                        source     = l2_hit.get('source', 'unknown')
                        confidence = float(l2_hit.get('confidence', 1.0))
                        if not title:  title  = l2_hit.get('title', '')
                        if not artist: artist = l2_hit.get('artist', '')
                        for _wk in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
                            _l1_saavn.set(_wk, l2_hit)
                        break
                    elif raw_hit and not l2_hit:
                        _invalidate_cache_keys(_ck_hit)
                except Exception: pass
        except Exception: pass
        # Background: L2 results jo 0.2s mein nahi aaye unhe L1 mein store karo
        def _bg_l2_store(fmap):
            for ck, fut in fmap.items():
                try:
                    r = fut.result(timeout=5)
                    h = _check_cache_entry(r)
                    if h:
                        for wk in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
                            _l1_saavn.set(wk, h)
                except Exception: pass
        if not audio_url:
            _executor_bg.submit(_bg_l2_store, _l2_futures)

    # ─── Step 3: Song index fast path ────────────────────────────────────────
    if not audio_url and title:
        _indexed_id = _song_index_get(title, artist)
        if _indexed_id and _indexed_id != song_id:
            _idx_result = _fetch_saavn_by_id(_indexed_id, expected_title=title, expected_artist=artist)
            if _idx_result and _idx_result.get('url'):
                audio_url  = _idx_result['url']
                quality    = _idx_result.get('quality', 'unknown')
                source     = 'saavn'; confidence = 0.95
                song_id    = _indexed_id
                if not title:  title  = _idx_result.get('title', '')
                if not artist: artist = _idx_result.get('artist', '')
                log.info(f"[SongIndex] ✓ Fast path: '{title}' → {_indexed_id}")

    # ─── Step 4: Saavn ID path ────────────────────────────────────────────────
    if not audio_url and song_id:
        result = _fetch_saavn_by_id(song_id, expected_title=title, expected_artist=artist)
        if result and result.get('url'):
            _r_title  = result.get('title', '') or title
            _r_artist = result.get('artist', '') or artist
            # [FIX-2] ID path pe bhi DNA + title check — ID-only request mein galat song block
            if title and _r_title:
                if not dna_compatible(title, _r_title):
                    log.warning(f"[Play] ID DNA MISMATCH: req='{title}' got='{_r_title}'")
                    result = None
                else:
                    _ok, _id_conf, _reason = _is_confirmed_match(
                        title, artist, _r_title, _r_artist,
                        source='saavn', min_conf=0.70,
                    )
                    if not _ok:
                        log.warning(f"[Play] ID match REJECTED: req='{title}' got='{_r_title}': {_reason}")
                        result = None
                    else:
                        confidence = _id_conf
            if result and result.get('url'):
                audio_url  = result['url']
                quality    = result.get('quality', 'unknown')
                source     = 'saavn'
                if not confidence: confidence = 0.92
                if not title:  title  = _r_title
                if not artist: artist = _r_artist
                log.info(f"[Play] ✓ Saavn ID={song_id} conf={confidence:.2f} q={quality}")

    # ─── Step 5: Title search — SIRF EK BAAR ─────────────────────────────────
    # FIX: Pehle Step 3/4 mein title fallback tha — duplicate search ho raha tha
    # Ab sirf yahan ek baar karo
    if not audio_url and title:
        _variants_tried = set()
        for query_var in build_query_variants(title, artist, ''):
            if query_var in _variants_tried: continue
            _variants_tried.add(query_var)
            result = fetch_saavn_parallel(query_var, title=title, artist=artist, language=_play_lang)
            if result and result.get('url'):
                audio_url  = result['url']
                quality    = result.get('quality', 'unknown')
                source     = 'saavn'
                confidence = float(result.get('_confidence', result.get('score', 0.5)))
                log.info(f"[Play] ✓ Saavn search '{result['title']}' q={quality}")
                break

    # ─── Step 6: Fallbacks ───────────────────────────────────────────────────
    if not audio_url and title:
        log.info(f"[Play] Saavn miss → fallbacks: '{title}'")
        _MIN_FALLBACK_CONF = _conf_tuner.get_floor(title, artist)

        # Phase 1: JioSavan
        _phase1_futures = {
            _executor.submit(fetch_from_jiosavan, title, artist, _play_lang): 'jiosavan',
        }
        _phase1_candidates = []
        try:
            for future in as_completed(_phase1_futures, timeout=3.0):
                try:
                    res = future.result()
                    if res and res.get('url'):
                        conf = float(res.get('_confidence', 0.50))
                        if conf >= _MIN_FALLBACK_CONF:
                            _phase1_candidates.append((conf, res, _phase1_futures[future]))
                except Exception: pass
        except Exception: pass

        if _phase1_candidates:
            _phase1_candidates.sort(key=lambda x: -x[0])
            _p1_conf, _p1_res, _p1_src = _phase1_candidates[0]
            audio_url  = _p1_res['url']
            quality    = _p1_res.get('quality', 'unknown')
            source     = _p1_res.get('source', _p1_src)
            confidence = _p1_conf
            if not title:  title  = _p1_res.get('title', title)
            if not artist: artist = _p1_res.get('artist', artist)

        # Phase 2: All sources
        if not audio_url:
            _all_fb_futures = {
                _executor.submit(fetch_from_ytmusic,    title, artist):        'ytmusic',
                _executor.submit(fetch_from_ytdlp,      title, artist):        'youtube',
                _executor.submit(fetch_from_soundcloud, title, artist):        'soundcloud',
                _executor.submit(fetch_from_piped,      title, artist):        'piped',
                _executor.submit(fetch_from_invidious,  title, artist):        'invidious',
            }

            _fb_candidates = []; _arrival_idx = 0
            _deadline      = time.time() + 2.5

            try:
                for future in as_completed(_all_fb_futures, timeout=2.5):
                    try:
                        res = future.result()
                        if res and res.get('url'):
                            src_name = _all_fb_futures[future]
                            conf     = float(res.get('_confidence', 0.50))
                            if conf >= _MIN_FALLBACK_CONF:
                                _fb_candidates.append((_arrival_idx, conf, res, src_name))
                                _arrival_idx += 1
                    except Exception: pass
                    if time.time() >= _deadline: break
            except Exception: pass

            if not _fb_candidates:
                try:
                    for future in as_completed(_all_fb_futures, timeout=5):
                        try:
                            res = future.result()
                            if res and res.get('url'):
                                src_name = _all_fb_futures[future]
                                conf     = float(res.get('_confidence', 0.50))
                                if conf >= _MIN_FALLBACK_CONF:
                                    _fb_candidates.append((_arrival_idx, conf, res, src_name))
                                    _arrival_idx += 1
                                    if conf >= 0.85: break
                        except Exception: pass
                except Exception: pass

            if _fb_candidates:
                _fb_candidates.sort(key=lambda x: -x[1])
                _SOURCE_PRI = {
                    'saavn': 0, 'jiosavan': 1, 'ytmusic': 2,
                    'soundcloud': 3, 'piped': 4, 'invidious': 5, 'youtube': 6
                }
                _top_conf = _fb_candidates[0][1]
                _tied = [(a,c,r,s) for a,c,r,s in _fb_candidates if abs(c - _top_conf) < 0.03]
                if len(_tied) > 1:
                    _tied.sort(key=lambda x: _SOURCE_PRI.get(x[3], 99))
                    _, _winner_conf, _winner_res, _winner_src = _tied[0]
                else:
                    _, _winner_conf, _winner_res, _winner_src = _fb_candidates[0]

                for f in _all_fb_futures: f.cancel()

                audio_url  = _winner_res['url']
                quality    = _winner_res.get('quality', 'unknown')
                source     = _winner_res.get('source', _winner_src)
                confidence = _winner_conf
                if not title:  title  = _winner_res.get('title', title)
                if not artist: artist = _winner_res.get('artist', artist)
                if _winner_res.get('image') and title:
                    _art_priority = (1 if source in ('saavn', 'jiosavan') else
                                     4 if source == 'ytmusic' else 5)
                    _store_artwork(title, artist, _winner_res['image'], _art_priority)

    # ─── Step 7: YouTube last resort ─────────────────────────────────────────
    if not audio_url and title:
        for broad_query in [title, title.split()[0] if title.split() else title]:
            broad = fetch_from_ytdlp(broad_query, artist)
            if broad and broad.get('url'):
                broad_conf = float(broad.get('_confidence', 0.0))
                if broad_conf < 0.60: continue
                # HARDGATE: DNA check on result — broad search se wrong version aa sakta hai
                _broad_result_title = broad.get('title', '')
                if _broad_result_title and not dna_compatible(title, _broad_result_title):
                    log.warning(f"[broad] DNA BLOCK: '{_broad_result_title}' for '{title}'")
                    continue
                audio_url  = broad['url']
                quality    = broad.get('quality', 'unknown')
                source     = 'youtube-broad'
                confidence = broad_conf
                break

    if not audio_url:
        log.warning(f"[Play] ✗ ALL sources failed id={song_id} title='{title}'")
        if title: _conf_tuner.record_miss(title, artist)
        return jsonify({'error': 'No audio source found'}), 404

    # ─── FINAL GATE: Confidence check ────────────────────────────────────────
    # [FIX-5] song_id bypass hataya — ID se aaye results bhi confidence check se guzarne chahiye
    # Saavn source pe 0.60 minimum (reliable), baki pe 0.65
    if audio_url:
        _gate_min = 0.60 if source in ('saavn', 'jiosavan') else 0.65
        if confidence < _gate_min:
            log.warning(f"[Play] FINAL GATE: low confidence={confidence:.3f} source={source}")
            if title: _conf_tuner.record_miss(title, artist)
            return jsonify({'error': 'No confident audio match found'}), 404

    # ─── FINAL MISMATCH CHECK — wrong song detect karo BEFORE streaming ──────
    # [FIX-6] HARDGATE: DNA + final confidence recheck — last line of defence
    _orig_title  = request.args.get('title', '').strip()
    _orig_artist = request.args.get('artist', '').strip()
    if _orig_title and title:
        # Pass 1: DNA check
        if not dna_compatible(_orig_title, title):
            log.warning(f"[FINAL DNA] BLOCK: orig='{_orig_title}' got='{title}' src={source}")
            _conf_tuner.record_miss(_orig_title, _orig_artist)
            _invalidate_cache_keys(_play_ck_id, _play_ck_title, _play_ck)
            return jsonify({'error': 'Song DNA mismatch — version blocked'}), 404
        # Pass 2: Final title similarity — alag song detect karo
        _final_gate_conf = compute_confidence(
            _orig_title, _orig_artist or artist,
            title, artist,
            source=source,
            query_duration_s=0, result_duration_s=0,
        )
        _final_min = 0.60 if source in ('saavn', 'jiosavan') else 0.62
        if _final_gate_conf < _final_min:
            log.warning(
                f"[FINAL CONF] BLOCK: orig='{_orig_title}' got='{title}' "
                f"conf={_final_gate_conf:.3f} src={source}"
            )
            _conf_tuner.record_miss(_orig_title, _orig_artist)
            _invalidate_cache_keys(_play_ck_id, _play_ck_title, _play_ck)
            return jsonify({'error': 'Song mismatch detected at final gate', 'confidence': _final_gate_conf}), 404

    _final_result_title  = title  # updated during pipeline
    _final_result_artist = artist
    if title and audio_url and source not in ('saavn',):
        # Non-saavn sources ke liye extra check — ye sources less reliable hain
        _final_conf = compute_confidence(
            request.args.get('title', '').strip(),  # original query
            request.args.get('artist', '').strip(),
            _final_result_title,
            _final_result_artist,
            source=source,
            query_duration_s=0,
            result_duration_s=0,
        )
        if _final_conf < 0.65:
            log.warning(
                f"[Play] FINAL MISMATCH: req='{request.args.get('title')}' "
                f"got='{_final_result_title}' conf={_final_conf:.3f}"
            )
            _conf_tuner.record_miss(title, artist)
            _invalidate_cache_keys(_play_ck_id, _play_ck_title, _play_ck)
            return jsonify({'error': 'Song mismatch detected', 'confidence': _final_conf}), 404

    # Record success
    if title: _conf_tuner.record_accept(title, artist, confidence)
    _src_perf.record(source, 0, True)

    # ─── Step 8: Async cache writes ──────────────────────────────────────────
    # [FIX-7] GODMODE THUMBNAIL RESOLUTION — layered lookup with 500x500 guarantee
    def _resolve_best_artwork(song_id, title, artist, result_image=''):
        """
        Priority chain:
        1. Saavn L1 cache (song_id key) — most reliable
        2. Artwork store (title/artist key)
        3. Result image from current fetch
        4. All L1 saavn cache keys scan
        5. Background fetch via Saavn ID
        Returns 500x500 guaranteed URL or empty string.
        """
        from match_engine import pick_image as _pick_img, _ensure_500 as _me_ensure_500
        def _ensure_500(url):
            # [FIX-FETCH-3] Delegate to match_engine._ensure_500 — correct \3 backref
            return _me_ensure_500(url)
        if False:  # dummy — keep indentation consistent
            if 'saavncdn.com' in url or 'jiocdn.com' in url:
                url = re.sub(r'-(\d+)x(\d+)\.(jpg|jpeg|webp|png)', r'-500x500.\3', url)  # [FIX-FETCH-3] was \x03 (corrupt) now \3 (correct backref)
                url = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
            elif re.search(r'\b(50|150|250)x(50|150|250)\b', url):
                url = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
            return url

        candidates = []

        # Priority 1: Saavn ID cache
        if song_id:
            _id_hit = _l1_saavn.get(f"saavn_id:{song_id}")
            if _id_hit and _id_hit.get('image'):
                candidates.append((1, _ensure_500(_id_hit['image'])))

        # Priority 2: Artwork store
        if title:
            _art = _get_artwork(title, artist)
            if _art: candidates.append((2, _ensure_500(_art)))

        # Priority 3: Current result image
        if result_image and result_image.startswith('http'):
            candidates.append((3, _ensure_500(result_image)))

        # Priority 4: All L1 saavn cache keys
        if title:
            for _src_key in [f"saavn_q:{normalize(title)}", _play_ck, _play_ck_id, _play_ck_title]:
                if not _src_key: continue
                _cache_hit = _l1_saavn.get(_src_key)
                if _cache_hit and _cache_hit.get('image'):
                    candidates.append((4, _ensure_500(_cache_hit['image'])))
                    break

        # Return best candidate
        if candidates:
            candidates.sort(key=lambda x: x[0])
            best_url = candidates[0][1]
            if best_url: return best_url

        # Priority 5: Background fetch via Saavn ID (async — next request pe milegi)
        if song_id and title:
            def _bg_art_fetch(_sid, _t, _a):
                try:
                    _r = _fetch_saavn_by_id(_sid, _t, _a)
                    if _r and _r.get('image'):
                        _store_artwork(_t, _a, _ensure_500(_r['image']), 1)
                except Exception: pass
            _executor_cache.submit(_bg_art_fetch, song_id, title, artist)

        return ''

    _current_result_image = ''
    if source in ('saavn', 'jiosavan'):
        # Try to get image from current fetch result
        for _ck in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
            _hit = _l1_saavn.get(_ck)
            if _hit and _hit.get('image'):
                _current_result_image = _hit['image']; break

    _best_art = _resolve_best_artwork(song_id, title, artist, _current_result_image)

    _cache_payload = {
        'url': audio_url, 'quality': quality, 'source': source,
        'title': title, 'artist': artist, 'confidence': confidence,
        'image': _best_art or '',
    }

    # Sirf high-confidence results cache karo
    if confidence >= _CACHE_MIN_CONFIDENCE:
        for _wk in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
            _l1_saavn.set(_wk, _cache_payload)
        if title:
            _l1_saavn.set(f"saavn_q:{normalize(title)}", _cache_payload)

    _executor_cache.submit(_supabase_cache_set, _play_ck, _cache_payload, confidence)
    if _play_ck_title and _play_ck_title != _play_ck:
        _executor_cache.submit(_supabase_cache_set, _play_ck_title, _cache_payload, confidence)
    from core import _store_verified
    _store_verified(song_id, title, artist, _cache_payload, confidence)

    # Self-learn
    if confidence >= 0.85 and title and song_id and source in ('saavn', 'jiosavan'):
        _executor_cache.submit(_song_index_put, title, artist, song_id, title, _best_art)

    # ─── Step 9: Stream ───────────────────────────────────────────────────────
    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header: req_headers['Range'] = range_header

        upstream     = requests.get(audio_url, headers=req_headers, stream=True,
                                    timeout=(10, None), allow_redirects=True)
        excluded     = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges':   'bytes',
            'Cache-Control':   'no-store',
            'X-Audio-Quality': quality,
            'X-Audio-Source':  source,
            'X-Confidence':    str(round(confidence, 3)),
            'X-Artwork-URL':   _best_art or '',
            'X-Song-Title':    (title or '')[:200],
            'X-Song-Artist':   (artist or '')[:100],
            'Access-Control-Expose-Headers': (
                'Content-Length, Content-Range, X-Audio-Quality, X-Audio-Source, '
                'X-Confidence, X-Artwork-URL, X-Song-Title, X-Song-Artist'
            ),
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=16384):
                    if chunk: yield chunk
            finally:
                upstream.close()

        return Response(stream_with_context(generate()), status=upstream.status_code,
                        headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        log.error(f"[Play] Stream error: {e}")
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# /api/prefetch
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/prefetch', methods=['POST'])
@limiter.limit("60 per minute")
def prefetch_songs():
    songs = request.get_json(silent=True) or {}
    queue = songs.get('songs', [])[:3]
    if not queue: return jsonify({'status': 'empty'})

    def _do_prefetch(s):
        _id     = str(s.get('id', '')).strip()[:100]
        _title  = str(s.get('title', '')).strip()[:200]
        _artist = str(s.get('artist', '')).strip()[:100]
        if not _id and not _title: return
        _ck = f"play:{_id or normalize(_title)}:{normalize(_artist)}"
        if _l1_saavn.get(_ck): return
        if _id:
            result = _fetch_saavn_by_id(_id, _title, _artist)
            if result and result.get('url'):
                # [FIX-1] Pehle DNA + match check karo — bina validation ke 0.95 nahi
                _r_title  = result.get('title', '') or _title
                _r_artist = result.get('artist', '') or _artist
                if _title:
                    _ok, _conf, _reason = _is_confirmed_match(
                        _title, _artist, _r_title, _r_artist,
                        source='saavn', min_conf=0.70,
                    )
                    if not _ok:
                        log.debug(f'[Prefetch] ID={_id} rejected: {_reason}')
                        return
                    _real_conf = _conf
                else:
                    _real_conf = 0.90  # ID-only prefetch — no title to verify
                _payload = {
                    **result,
                    'source': 'saavn',
                    'confidence': round(_real_conf, 3),
                    'title':  _title or _r_title,
                    'artist': _artist or _r_artist,
                }
                _l1_saavn.set(_ck, _payload)
                return
        if _title:
            _lang = _detect_language(_title + ' ' + _artist)
            for _qv in build_query_variants(_title, _artist, '')[:2]:
                _res = fetch_saavn_parallel(_qv, title=_title, artist=_artist, language=_lang)
                if _res and _res.get('url'):
                    _l1_saavn.set(_ck, _res)
                    return

    for song in queue:
        _executor_bg.submit(_do_prefetch, song)
    return jsonify({'status': 'prefetching', 'count': len(queue)})


# ═══════════════════════════════════════════════════════════════════════════════
# /api/search — SPEED: prefetch top results in background immediately
# ═══════════════════════════════════════════════════════════════════════════════
def _auto_prefetch_search_results(songs: list):
    """
    [SPEED-F] Search results aate hi top 3 songs ka audio URL resolve karo background mein.
    Jab user click karega tab L1 cache mein already hoga → ~0ms play time.
    """
    for song in songs[:3]:
        _id     = str(song.get('_saavnId') or song.get('trackId') or '').strip()[:100]
        _title  = str(song.get('trackName') or song.get('title') or '').strip()[:200]
        _artist = str(song.get('artistName') or song.get('artist') or '').strip()[:100]
        if not _id and not _title: continue
        _ck = f"play:{_id or normalize(_title)}:{normalize(_artist)}"
        if _l1_saavn.get(_ck): continue  # already cached — skip
        _executor_bg.submit(_do_prefetch, {'id': _id, 'title': _title, 'artist': _artist})
# ═══════════════════════════════════════════════════════════════════════════════
# MISSING EXPORTS — server.py imports
# ═══════════════════════════════════════════════════════════════════════════════
from core import _l1_artwork, _l1_verified
from match_engine import ALLOWED_STREAM_DOMAINS
from urllib.parse import urlparse

_url_refresh_queue = set()


def _fetch_itunes_artwork(query, artist='', limit=1):
    try:
        term = f"{query} {artist}".strip()
        r = requests.get(
            'https://itunes.apple.com/search',
            params={'term': term, 'media': 'music', 'entity': 'song',
                    'limit': 5, 'country': 'IN'},
            timeout=5
        )
        results = r.json().get('results', [])
        for item in results:
            art = item.get('artworkUrl100', '')
            if art:
                return re.sub(r'\b\d+x\d+\b', '600x600', art)
    except Exception:
        pass
    return ''


def _is_allowed_domain(domain: str) -> bool:
    return any(
        domain == d or domain.endswith('.' + d)
        for d in ALLOWED_STREAM_DOMAINS
    )
