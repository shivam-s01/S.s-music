# fetchers.py - COMPLETE PRODUCTION FIXED VERSION
# ═══════════════════════════════════════════════════════════════════════════════
# FIXES INCLUDED:
# 1. Corrupted parameter cleaning (id="title=xxx" issue)
# 2. Lowered DNA compatibility threshold (0.6 instead of 0.8)
# 3. JioSaavn direct API priority over saavndev
# 4. Proper frontend response format (trackName, artistName, artworkUrl100, etc.)
# 5. Full fallback chain: Saavn → JioSavan → YTMusic → YTDLP → SoundCloud → Piped → Invidious
# ═══════════════════════════════════════════════════════════════════════════════

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
    _conf_tuner, _src_perf,
    tve_match_get, tve_match_get_verified, tve_match_set, tve_match_invalidate,
    store_saavn_anchor, get_saavn_anchor,
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
    detect_preferred_quality, _ensure_500,
    _is_live_version, _is_slowed_reverb,
    verify_track, hard_reject_by_version, user_requested_version,
    calculate_artist_similarity, calculate_title_similarity,
    tve_validate_production, tve_pick_best_production,
    tve_validate, tve_validate_anchored, tve_pick_best,
    tve_tier1_duration, clean_metadata,
    tve_tier4_language, tve_tier5_artist_hard,
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
# IMAGE URL FIXER — always 500x500 Saavn, 600x600 iTunes, maxres YT
# ═══════════════════════════════════════════════════════════════════════════════
def _fix_image_url(url: str) -> str:
    if not url or not url.startswith('http'):
        return url or ''
    if 'saavncdn.com' in url or 'jiocdn.com' in url:
        url = re.sub(r'-(\d+)x(\d+)\.(jpg|jpeg|webp|png)', r'-500x500.\3', url)
        url = re.sub(r'\b(50|150|250)\b', '500', url)
        return url
    if 'mzstatic.com' in url:
        url = re.sub(r'/\d+x\d+bb', '/600x600bb', url)
        url = re.sub(r'\b\d+x\d+\b', '600x600', url)
        return url
    if 'ytimg.com' in url:
        url = re.sub(r'/(default|mqdefault|sddefault|hqdefault)\.jpg', '/maxresdefault.jpg', url)
        return url
    if 'yt3.ggpht.com' in url or 'lh3.googleusercontent.com' in url:
        url = re.sub(r'=w\d+-h\d+(-[a-z]+)?', '=w500-h500', url)
        url = re.sub(r'=s\d+', '=s500', url)
        return url
    return url


# ═══════════════════════════════════════════════════════════════════════════════
# DIRECT JIOSAAVN + SAAVN.DEV API  (primary — faster than mirrors)
# ═══════════════════════════════════════════════════════════════════════════════
_JIOSAAVN_DIRECT = 'https://www.jiosaavn.com/api.php'
_SAAVNDEV_BASE   = 'https://saavn.dev'
_DIRECT_HEADERS  = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.jiosaavn.com/',
    'Origin':  'https://www.jiosaavn.com',
}
_DIRECT_TIMEOUT = 5


def _jiosaavn_api(params: dict) -> Optional[dict]:
    base = {'__call': '', '_format': 'json', '_marker': '0',
            'ctx': 'web6dot0', 'api_version': '4'}
    base.update(params)
    try:
        r = requests.get(_JIOSAAVN_DIRECT, params=base,
                         headers=_DIRECT_HEADERS, timeout=_DIRECT_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug(f"[DirectAPI] jiosaavn.com: {e}")
    return None


def _saavndev_get(path: str, params: dict) -> Optional[dict]:
    try:
        r = requests.get(f'{_SAAVNDEV_BASE}{path}', params=params,
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=_DIRECT_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug(f"[DirectAPI] saavn.dev: {e}")
    return None


def _search_direct_jiosaavn(query: str, language: str = '') -> list:
    params = {'__call': 'search.getResults', 'q': query, 'p': 1, 'n': 20}
    if language: params['language'] = language
    data = _jiosaavn_api(params)
    if not data: return []
    raw = (data.get('results') or
           (data.get('songs', {}).get('results') if isinstance(data.get('songs'), dict) else None) or
           (data.get('data', {}).get('results') if isinstance(data.get('data'), dict) else None) or [])
    if not raw and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v: raw = v; break
    return raw or []


def _search_saavndev(query: str, language: str = '') -> list:
    params = {'query': query, 'limit': 20}
    if language: params['language'] = language
    data = _saavndev_get('/api/search/songs', params)
    if not data: return []
    return (data.get('data', {}).get('results') or data.get('results') or [])


def _parse_saavn_song_raw(song: dict) -> Optional[dict]:
    """Parse raw song dict from any Saavn API — returns normalised result or None."""
    raw_urls = song.get('downloadUrl') or song.get('download_url') or []
    if isinstance(raw_urls, str):
        raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
    if not raw_urls:
        dl = song.get('media_preview_url') or song.get('perma_url') or ''
        if dl: raw_urls = [{'url': dl, 'quality': '96kbps'}]
    best_url, quality = pick_best_quality(raw_urls)
    if not best_url: return None
    if best_url.startswith('http://'): best_url = 'https://' + best_url[7:]
    title  = song.get('name') or song.get('title') or song.get('song', '')
    artist = (song.get('primaryArtists') or song.get('primary_artists') or
              song.get('singers') or song.get('artist', ''))
    image  = _fix_image_url(pick_image(song))
    return {'url': best_url, 'quality': quality, 'title': title,
            'artist': artist, 'image': image, '_raw_urls': raw_urls}


def _pick_best_direct(raw_results: list, query: str, title: str, artist: str) -> Optional[dict]:
    _conf_title  = title or query
    _conf_artist = artist or ''
    candidates   = []
    for idx, song in enumerate(raw_results):
        song_title  = song.get('name') or song.get('title') or song.get('song', '')
        song_artist = (song.get('primaryArtists') or song.get('primary_artists') or
                       song.get('singers') or song.get('artist', ''))
        if not song_title: continue
        
        # 🔥 FIX: Lowered DNA compatibility threshold to 0.6
        if not dna_compatible(_conf_title, song_title, threshold=0.6):
            # Try without special characters as fallback
            clean_req = re.sub(r'[^\w\s]', '', _conf_title.lower())
            clean_res = re.sub(r'[^\w\s]', '', song_title.lower())
            if clean_req not in clean_res and clean_res not in clean_req:
                continue
        
        if not has_word_match(_conf_title, song_title): continue
        dur = int(song.get('duration', 999) or 999)
        if dur > 1080: continue
        if _conf_artist and song_artist:
            if _best_artist_similarity(_conf_artist, song_artist) < 0.55: continue
        _ok, _conf, _ = _is_confirmed_match(
            _conf_title, _conf_artist, song_title, song_artist,
            source='saavn', res_dur_s=dur, min_conf=0.65)
        if not _ok: continue
        _pos_penalty = min(0.08, max(0.0, 0.015 * (idx - 2))) if idx >= 3 else 0.0
        candidates.append((_conf - _pos_penalty, title_score(query, song_title, song_artist), song))
    if not candidates: return None
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_conf, best_legacy, best_song = candidates[0]
    if best_conf < 0.65: return None
    parsed = _parse_saavn_song_raw(best_song)
    if not parsed: return None
    parsed['score'] = round(best_legacy, 3)
    parsed['_confidence'] = round(best_conf, 3)
    parsed['source'] = 'saavn'
    if parsed.get('image'): _store_artwork(parsed['title'], parsed['artist'], parsed['image'], 1)
    return parsed


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
# ARTIST SIMILARITY HELPER — used by fetch_from_mirror + _normalize_saavn_songs
# ═══════════════════════════════════════════════════════════════════════════════
def _best_artist_similarity(req_artist: str, result_artist: str) -> float:
    """
    Artist similarity — delegates to production TVE engine.
    Returns 0.0–1.0. Used as hard gate: < 0.55 = reject.
    Empty artist → 1.0 (neutral, cannot reject).
    """
    if not req_artist or not result_artist:
        return 1.0
    return calculate_artist_similarity(req_artist, result_artist)


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
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range, X-Audio-Quality, X-Audio-Source, X-Confidence, X-Artwork-URL, X-Song-Title, X-Song-Artist'
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
    # Phase 1: direct APIs (5s)
    _df = {_executor.submit(_search_saavndev,        search_term, language): 'saavndev',
           _executor.submit(_search_direct_jiosaavn, search_term, language): 'direct'}
    try:
        for future in as_completed(_df, timeout=5):
            try:
                result = future.result()
                if result:
                    for f in _df: f.cancel()
                    return result
            except Exception: pass
    except Exception: pass
    # Phase 2: mirrors fallback (3s)
    mirrors = _best_mirrors(n=4)
    futures = {_executor.submit(_fetch_saavn_search_mirror, m, search_term, language): m
               for m in mirrors}
    try:
        for future in as_completed(futures, timeout=3):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    return result
            except Exception: pass
    except Exception: pass
    return []

def _normalize_saavn_songs(raw_songs, query=''):
    normalized = []
    _query_wants_ver = _query_requests_version(query) if query else False

    for song in raw_songs:
        song_id = song.get('id', '').strip()
        if not song_id: continue
        title  = song.get('name') or song.get('title', '')
        artist = song.get('primaryArtists') or song.get('primary_artists', '')
        image  = pick_image(song)
        year   = str(song.get('year') or '0')[:4]
        dur_s  = int(song.get('duration', 0) or 0)
        dur_ms = dur_s * 1000
        from core import _is_devotional_query
        _is_devotional = _is_devotional_query(title + ' ' + artist)
        if dur_s == 0: continue
        if dur_s > 1800 and not _is_devotional: continue
        if dur_s > 1080 and not _is_devotional: continue

        if not _query_wants_ver:
            if _is_remix_or_cover(title):
                continue
            if _is_slowed_reverb(title):
                continue
            if _is_live_version(title):
                continue
            if has_version_words(title):
                continue

        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        _, quality = pick_best_quality(raw_urls)
        if not quality: continue
        image = _fix_image_url(image) if image else ''
        normalized.append({
            'trackId':         song_id,
            'trackName':       title,
            'artistName':      artist,
            'artworkUrl100':   image,
            'previewUrl':      f"/api/play?id={quote(song_id, safe='')}&title={quote(title, safe='')}&artist={quote(artist, safe='')}",
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

    mirrors = _best_mirrors(n=4)
    _deadline = time.time() + 5.0

    for query in build_query_variants(title, artist, ''):
        if time.time() > _deadline: break
        _df = {_executor.submit(_search_saavndev,        query, _lang): 'saavndev',
               _executor.submit(_search_direct_jiosaavn, query, _lang): 'direct'}
        try:
            for future in as_completed(_df, timeout=max(0.5, _deadline - time.time())):
                try:
                    raw = future.result()
                    if not raw: continue
                    best = None; best_conf = -1.0
                    itunes_dur = int((itunes_song.get('trackTimeMillis') or 0) // 1000)
                    for song in raw:
                        song_title  = song.get('name') or song.get('title', '')
                        song_artist = (song.get('primaryArtists') or song.get('primary_artists') or
                                       song.get('singers') or song.get('artist', ''))
                        song_dur = int(song.get('duration', 0) or 0)
                        
                        # 🔥 FIX: Lowered DNA compatibility threshold
                        if not dna_compatible(title, song_title, threshold=0.6): continue
                        if artist and song_artist and _best_artist_similarity(artist, song_artist) < 0.55: continue
                        _ok, _conf, _reason = _is_confirmed_match(
                            title, artist, song_title, song_artist,
                            source='saavn', duration_s=itunes_dur, res_dur_s=song_dur, min_conf=0.75)
                        if not _ok: continue
                        if _conf > best_conf: best_conf = _conf; best = song
                    if not best or best_conf < 0.75: continue
                    saavn_id = (best.get('id') or '').strip()
                    raw_urls = best.get('downloadUrl') or best.get('download_url') or []
                    if isinstance(raw_urls, str): raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                    _, quality = pick_best_quality(raw_urls)
                    if not saavn_id or not quality: continue
                    itunes_song['previewUrl']      = f"/api/play?id={quote(saavn_id, safe='')}&title={quote(title, safe='')}&artist={quote(artist, safe='')}"
                    itunes_song['_saavnId']        = saavn_id
                    itunes_song['_resolvedTitle']  = best.get('name') or best.get('title', title)
                    itunes_song['_resolvedArtist'] = best.get('primaryArtists') or best.get('primary_artists') or artist
                    itunes_song['_confidence']     = round(best_conf, 3)
                    _itunes_art = itunes_song.get('artworkUrl100', '')
                    saavn_img = _fix_image_url(pick_image(best))
                    if not _itunes_art and saavn_img:
                        itunes_song['artworkUrl100'] = saavn_img
                        _store_artwork(title, artist, saavn_img, 1)
                    elif _itunes_art and saavn_img:
                        _store_artwork(title, artist, _itunes_art, 1)
                    for f in _df: f.cancel()
                    log.info(f"[Resolve:direct] ✓ '{title}' → {saavn_id} conf={best_conf:.2f}")
                    return itunes_song
                except Exception: pass
        except Exception: pass

    for query in build_query_variants(title, artist, ''):
        if time.time() > _deadline: break
        for mirror in mirrors[:4]:
            if time.time() > _deadline: break
            for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
                if time.time() > _deadline: break
                try:
                    resp = requests.get(f'{mirror}{endpoint}',
                        params={'query': query, 'q': query, 'limit': 5},
                        timeout=min(_health.adaptive_timeout(mirror), 3),
                        headers={'User-Agent': 'Mozilla/5.0'})
                    if resp.status_code != 200: continue
                    data = resp.json()
                    raw  = (data.get('data', {}).get('results') or data.get('results') or
                            data.get('songs', {}).get('results') or [])
                    if not raw: continue
                    best = None; best_conf = -1.0
                    itunes_dur = int((itunes_song.get('trackTimeMillis') or 0) // 1000)
                    for song in raw:
                        song_title  = song.get('name') or song.get('title', '')
                        song_artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                        song_dur    = int(song.get('duration', 0) or 0)
                        
                        # 🔥 FIX: Lowered DNA compatibility threshold
                        if not dna_compatible(title, song_title, threshold=0.6): continue
                        if artist and song_artist and _best_artist_similarity(artist, song_artist) < 0.55: continue
                        _ok, _conf, _ = _is_confirmed_match(
                            title, artist, song_title, song_artist,
                            source='saavn', duration_s=itunes_dur, res_dur_s=song_dur, min_conf=0.75)
                        if not _ok: continue
                        if _conf > best_conf: best_conf = _conf; best = song
                    if not best or best_conf < 0.75: continue
                    saavn_id = (best.get('id') or '').strip()
                    raw_urls = best.get('downloadUrl') or best.get('download_url') or []
                    if isinstance(raw_urls, str): raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                    _, quality = pick_best_quality(raw_urls)
                    if not saavn_id or not quality: continue
                    itunes_song['previewUrl']      = f"/api/play?id={quote(saavn_id, safe='')}&title={quote(title, safe='')}&artist={quote(artist, safe='')}"
                    itunes_song['_saavnId']        = saavn_id
                    itunes_song['_resolvedTitle']  = best.get('name') or best.get('title', title)
                    itunes_song['_resolvedArtist'] = best.get('primaryArtists') or best.get('primary_artists') or artist
                    itunes_song['_confidence']     = round(best_conf, 3)
                    _itunes_art = itunes_song.get('artworkUrl100', '')
                    saavn_img = _fix_image_url(pick_image(best))
                    if not _itunes_art and saavn_img:
                        itunes_song['artworkUrl100'] = saavn_img
                        _store_artwork(title, artist, saavn_img, 1)
                    elif _itunes_art and saavn_img:
                        _store_artwork(title, artist, _itunes_art, 1)
                    log.info(f"[Resolve:mirror] ✓ '{title}' → {saavn_id} conf={best_conf:.2f}")
                    return itunes_song
                except Exception: continue

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
    url, quality, _ = _ytm_get_stream_with_duration(video_id)
    return url, quality


def _ytm_get_stream_with_duration(video_id):
    l1_key = f"ytm_stream:{video_id}"
    cached = _l1_audio.get(l1_key)
    if cached:
        return cached.get('url'), cached.get('quality'), int(cached.get('duration_s', 0))
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'quiet': True, 'no_warnings': True, 'socket_timeout': 12,
        'extract_flat': False, 'noplaylist': True,
        'http_headers': {'User-Agent': random.choice(_YTDLP_USER_AGENTS)},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://music.youtube.com/watch?v={video_id}', download=False)
            if not info: return None, None, 0
            actual_dur = int(info.get('duration', 0) or 0)
            formats = info.get('formats', [])
            audio_formats = [f for f in formats
                             if f.get('acodec') not in ('none', None, '')
                             and f.get('url') and f.get('vcodec') in ('none', None, '')]
            if not audio_formats:
                audio_formats = [f for f in formats
                                 if f.get('acodec') not in ('none', None, '') and f.get('url')]
            if not audio_formats: return None, None, actual_dur
            best    = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            abr     = best.get('abr') or best.get('tbr') or 0
            quality = f"{int(abr)}kbps" if abr else 'unknown'
            url     = best['url']
            _l1_audio.set(l1_key, {'url': url, 'quality': quality, 'duration_s': actual_dur})
            return url, quality, actual_dur
    except Exception as e:
        log.warning(f'[YTMusic] stream extract error {video_id}: {e}')
        return None, None, 0


def fetch_from_ytmusic(title, artist='', anchor=None):
    if not _cb.is_allowed('ytmusic'):
        log.debug('[CB] ytmusic OPEN — skipping'); return None
    l1_key = f"ytmusic:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached

    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''

    if anchor:
        year  = anchor.get('year', '')
        lang  = anchor.get('language', '')
        query = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title
        if year and int(year or 0) < 2020:
            query += f" {year}"
        if lang and lang in ('hindi', 'punjabi', 'bhojpuri'):
            query += f" {lang}"
    else:
        query = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title

    results = _ytm_search(query, limit=8)
    if not results: results = _ytm_search(clean_title, limit=5)
    if not results: return None

    tve_candidates = [
        {'title': item.get('title', ''), 'artist': item.get('artist', ''),
         'duration_s': 0, '_item': item}
        for item in results[:5]
    ]

    _saavn_dur  = anchor.get('duration_s', 0) if anchor else 0
    _saavn_lang = anchor.get('language', '')  if anchor else ''

    best_candidate, scores = tve_pick_best(
        saavn_title=title, saavn_artist=artist, saavn_duration_s=_saavn_dur,
        candidates=tve_candidates, max_candidates=5,
        saavn_language=_saavn_lang, source='ytmusic', anchor=anchor,
    )

    if best_candidate is None:
        log.warning(f"[YTMusic] TVE all candidates failed for '{title}': "
                    f"{scores.get('message', 'no verified track')}")
        return None

    best     = best_candidate['_item']
    video_id = best['videoId']

    url, quality, actual_dur = _ytm_get_stream_with_duration(video_id)
    if not url: return None

    if _saavn_dur > 0 and actual_dur > 0:
        if not tve_tier1_duration(_saavn_dur, actual_dur):
            log.warning(
                f"[YTMusic] Real duration gate FAILED: saavn={_saavn_dur}s "
                f"yt={actual_dur}s '{best.get('title')}'"
            )
            return None

    _ok, _conf, _reason = _is_confirmed_match(
        title, artist, best.get('title', ''), best.get('artist', ''),
        source='ytmusic', min_conf=0.60,
    )
    if not _ok:
        log.debug(f"[YTMusic] Legacy gate rejected '{best.get('title')}': {_reason}")
        return None
    best_conf = _conf

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
def fetch_from_ytdlp(title, artist='', anchor=None):
    if not _cb.is_allowed('youtube'):
        log.debug('[CB] youtube/ytdlp OPEN — skipping'); return None
    l1_key = f"ytdlp:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    if cached:
        _cached_source = cached.get('source', 'youtube')
        if _cached_source in ('youtube', 'youtube-broad'):
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
            for search_q in search_queries:
                try:
                    info = ydl.extract_info(search_q, download=False)
                    if not info or not info.get('entries'): continue

                    entries = [e for e in info['entries'] if e and e.get('duration', 0) > 90]
                    if not entries:
                        entries = [e for e in info['entries'] if e]

                    tve_candidates = []
                    for entry in entries[:5]:
                        tve_candidates.append({
                            'title':      entry.get('title', ''),
                            'artist':     entry.get('uploader', '') or entry.get('artist', ''),
                            'duration_s': int(entry.get('duration', 0) or 0),
                            '_entry':     entry,
                        })

                    _saavn_dur = 0
                    _saavn_lang2 = anchor.get('language', '') if anchor else ''
                    best_candidate, scores = tve_pick_best(
                        saavn_title=title,
                        saavn_artist=artist,
                        saavn_duration_s=_saavn_dur,
                        candidates=tve_candidates,
                        max_candidates=5,
                        saavn_language=_saavn_lang2,
                        source='youtube',
                        anchor=anchor,
                    )

                    if best_candidate is None:
                        if scores and scores.get('status') == 'mismatch_error':
                            log.debug(f"[yt-dlp] TVE: {scores['message']} for '{title}'")
                        continue

                    best_result = best_candidate['_entry']

                    yt_title  = best_result.get('title', '')
                    yt_artist = best_result.get('uploader', '') or best_result.get('artist', '')
                    _ok, _conf, _reason = _is_confirmed_match(
                        title, artist, yt_title, yt_artist,
                        source='youtube', min_conf=0.60,
                    )
                    if not _ok:
                        log.debug(f"[yt-dlp] Legacy gate rejected '{yt_title}': {_reason}")
                        continue

                    if 'music.youtube' in (best_result.get('webpage_url') or ''):
                        _conf = min(1.0, _conf + 0.05)
                    best_conf = _conf

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
                    if not audio_formats: continue

                    best_fmt = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
                    abr      = best_fmt.get('abr') or best_fmt.get('tbr') or 0
                    quality  = f"{int(abr)}kbps" if abr else 'unknown'
                    thumb    = best_result.get('thumbnail', '')
                    if not thumb:
                        vid_id = best_result.get('id', '')
                        if vid_id:
                            thumb = f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg"

                    if not verify_via_fingerprint(best_fmt['url'], title, artist):
                        log.warning(f"[yt-dlp] Fingerprint FAILED: '{best_result.get('title')}'")
                        _cb.record_failure('youtube')
                        continue

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

                except Exception: continue

            return None
    except Exception as e:
        log.warning(f"[yt-dlp] '{title}' → {e}")
        _cb.record_failure('youtube')
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SOUNDCLOUD
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_soundcloud(title, artist='', anchor=None):
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

            tve_candidates = []
            for entry in info['entries']:
                if not entry or entry.get('duration', 0) < 60: continue
                tve_candidates.append({
                    'title':      entry.get('title', ''),
                    'artist':     entry.get('uploader', ''),
                    'duration_s': int(entry.get('duration', 0) or 0),
                    '_entry':     entry,
                })

            _sc_saavn_dur  = anchor.get('duration_s', 0) if anchor else 0
            _sc_saavn_lang = anchor.get('language', '')  if anchor else ''
            best_candidate, scores = tve_pick_best(
                saavn_title=title,
                saavn_artist=artist,
                saavn_duration_s=_sc_saavn_dur,
                candidates=tve_candidates,
                max_candidates=5,
                saavn_language=_sc_saavn_lang,
                source='soundcloud',
                anchor=anchor,
            )

            if best_candidate is None:
                log.warning(
                    f"[SoundCloud] TVE all candidates failed for '{title}': "
                    f"{scores.get('message', 'no verified track')}"
                )
                return None

            best = best_candidate['_entry']

            _ok, _conf, _reason = _is_confirmed_match(
                title, artist, best.get('title', ''), best.get('uploader', ''),
                source='soundcloud', min_conf=0.60,
            )
            if not _ok:
                log.debug(f"[SoundCloud] Legacy gate rejected '{best.get('title')}': {_reason}")
                return None
            best_conf = _conf

            formats  = best.get('formats', [])
            if not formats: return None
            best_fmt = max(formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            if not best_fmt.get('url'): return None

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
# SAAVN BY ID — saavn.dev primary, jiosaavn.com secondary, mirrors fallback
# ═══════════════════════════════════════════════════════════════════════════════
def _fetch_saavn_by_id(song_id, expected_title='', expected_artist=''):
    l1_key = f"saavn_id:{song_id}"
    cached = _l1_saavn.get(l1_key)
    if cached: return cached

    def _validate(id_result):
        if not id_result: return None
        if expected_title and id_result.get('title'):
            _c = compute_confidence(expected_title, expected_artist,
                                    id_result['title'], id_result.get('artist',''), source='saavn')
            if _c < 0.75:  # 🔥 FIX: Lowered from 0.80 to 0.75
                log.warning(f"[SaavnID] MISMATCH: expected='{expected_title}' got='{id_result['title']}' conf={_c:.3f}")
                return None
        _t = id_result.get('title', '')
        if not _query_requests_version(expected_title):
            if _is_live_version(_t) or _is_remix_or_cover(_t) or _is_slowed_reverb(_t):
                log.warning(f"[SaavnID] VERSION REJECTED: '{_t}'"); return None
            if expected_title and not dna_compatible(expected_title, _t, threshold=0.6):  # 🔥 FIX: Lowered threshold
                log.warning(f"[SaavnID] DNA MISMATCH: '{_t}'"); return None
        id_result['image'] = _fix_image_url(id_result.get('image','') or _get_artwork(id_result.get('title',''), id_result.get('artist','')))
        if id_result['image']: _store_artwork(id_result['title'], id_result['artist'], id_result['image'], 1)
        _l1_saavn.set(l1_key, id_result)
        return id_result

    def _try_saavndev():
        data = _saavndev_get(f'/api/songs/{song_id}', {})
        if not data: return None
        inner = data.get('data') or data
        song  = (inner[0] if isinstance(inner, list) and inner
                 else inner if isinstance(inner, dict) and inner.get('id') else None)
        if not song: return None
        return _parse_saavn_song_raw(song)

    def _try_direct():
        data = _jiosaavn_api({'__call': 'song.getDetails', 'pids': song_id})
        if not data: return None
        song = None
        if isinstance(data.get('songs'), list) and data['songs']: song = data['songs'][0]
        elif isinstance(data, dict) and data.get(song_id): song = data[song_id]
        elif isinstance(data.get('data'), list) and data['data']: song = data['data'][0]
        if not song: return None
        return _parse_saavn_song_raw(song)

    def _try_mirror(mirror):
        endpoints = [f'/api/songs/{song_id}', f'/songs/{song_id}',
                     f'/api/songs?id={song_id}', f'/song?id={song_id}']
        if not _mirror_ok(mirror): return None
        for endpoint in endpoints:
            try:
                t0 = time.time()
                r  = requests.get(f'{mirror}{endpoint}',
                                  timeout=min(_health.adaptive_timeout(mirror), 4),
                                  headers={'User-Agent': 'Mozilla/5.0'})
                elapsed = (time.time() - t0) * 1000
                if r.status_code != 200: continue
                data = r.json()
                song = None
                if isinstance(data.get('data'), list) and data['data']:   song = data['data'][0]
                elif isinstance(data.get('data'), dict):                   song = data['data']
                elif data.get('id'):                                       song = data
                elif data.get('songs'):
                    s = data['songs']; song = s[0] if isinstance(s, list) and s else s
                if not song: continue
                parsed = _parse_saavn_song_raw(song)
                if parsed: _health.record_ok(mirror, elapsed); return parsed
            except Exception: _mirror_failed(mirror)
        return None

    # 🔥 FIX: Priority to direct jiosaavn API (more reliable)
    _d = {_executor.submit(_try_direct):   'direct',
          _executor.submit(_try_saavndev): 'saavndev'}
    result = None
    try:
        for future in as_completed(_d, timeout=5):
            try:
                res = future.result()
                if res:
                    result = res
                    for f in _d: f.cancel()
                    break
            except Exception: pass
    except Exception: pass

    if not result:
        log.debug(f"[SaavnID] Direct miss id={song_id} — trying mirrors")
        _mf = {_executor.submit(_try_mirror, m): m for m in _best_mirrors(n=4)}
        try:
            for future in as_completed(_mf, timeout=3):
                try:
                    res = future.result()
                    if res:
                        result = res
                        for f in _mf: f.cancel()
                        break
                except Exception: pass
        except Exception: pass

    return _validate(result) if result else None


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

                # 🔥 FIX: Lowered DNA compatibility threshold
                if not dna_compatible(_conf_title, song_title, threshold=0.6):
                    # Try without special characters
                    clean_req = re.sub(r'[^\w\s]', '', _conf_title.lower())
                    clean_res = re.sub(r'[^\w\s]', '', song_title.lower())
                    if clean_req not in clean_res and clean_res not in clean_req:
                        log.debug(f"[Mirror] DNA MISMATCH: '{song_title}'")
                        continue

                if not has_word_match(_conf_title, song_title): continue
                dur = int(song.get('duration', 999) or 999)
                if dur > 1080: continue

                if _conf_artist and song_artist:
                    _art_sim = _best_artist_similarity(_conf_artist, song_artist)
                    if _art_sim < 0.55:
                        log.debug(
                            f"[Mirror] HARD ARTIST REJECT: "
                            f"req='{_conf_artist}' got='{song_artist}' "
                            f"sim={_art_sim:.2f} title='{song_title}'"
                        )
                        continue

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
                'image': _fix_image_url(pick_image(best_song)), 'score': round(best_legacy, 3),
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

    _EARLY_EXIT_CONF = 0.82
    best = None

    # 🔥 FIX: Priority to direct jiosaavn API (more reliable)
    _df = {_executor.submit(_search_direct_jiosaavn, query, _lang): 'direct',
           _executor.submit(_search_saavndev,        query, _lang): 'saavndev'}
    _p1 = []
    try:
        for future in as_completed(_df, timeout=5):
            try:
                raw = future.result()
                if not raw: continue
                candidate = _pick_best_direct(raw, query, title, artist)
                if candidate:
                    _p1.append(candidate)
                    if float(candidate.get('_confidence', 0)) >= _EARLY_EXIT_CONF:
                        for f in _df: f.cancel(); break
            except Exception: pass
    except Exception: pass

    if _p1:
        _p1.sort(key=lambda r: float(r.get('_confidence', r.get('score', 0))) +
                 (0.02 if '320' in str(r.get('quality', '')) else 0), reverse=True)
        best = _p1[0]
        log.info(f"[Direct] ✓ '{best['title']}' conf={best.get('_confidence',0):.2f} q={best['quality']}")

    if not best:
        log.debug(f"[Direct] Miss '{query}' → mirrors")
        threshold = dynamic_min_score(query)
        mirrors   = _best_mirrors(n=4)
        futures   = {_executor.submit(fetch_from_mirror, m, query, threshold, title, artist, _lang): m
                     for m in mirrors}
        all_results = []
        try:
            for future in as_completed(futures, timeout=2.5):
                try:
                    result = future.result()
                    if result:
                        all_results.append(result)
                        _conf = float(result.get('_confidence', result.get('score', 0)))
                        if _conf >= _EARLY_EXIT_CONF:
                            for f in futures: f.cancel(); break
                except Exception: pass
        except Exception: pass
        if all_results:
            all_results.sort(
                key=lambda r: (
                    float(r.get('_confidence', r.get('score', 0))) +
                    (0.02 if '320' in str(r.get('quality', '')) else 0)
                ),
                reverse=True,
            )
            best = all_results[0]

    if not best:
        return None

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
                # 🔥 FIX: Lowered DNA compatibility threshold
                if not dna_compatible(title or query, piped_title, threshold=0.6): continue
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
                # 🔥 FIX: Lowered DNA compatibility threshold
                if not dna_compatible(title or query, inv_title, threshold=0.6): continue
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

            if not verify_via_fingerprint(best_fmt['url'], title or query, artist):
                log.warning(f"[Invidious] Fingerprint FAILED: '{best.get('title')}'")
                continue

            bitrate = best_fmt.get('bitrate', 0)
            _health.record_ok(instance, elapsed)
            inv_result = {
                'url': best_fmt['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title': best.get('title', title), 'artist': best.get('author', artist),
                'image': f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
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

            # 🔥 FIX: Lowered DNA compatibility threshold
            if not dna_compatible(title, song_title, threshold=0.6): continue

            if artist and song_artist:
                _art_sim = _best_artist_similarity(artist, song_artist)
                if _art_sim < 0.55:
                    log.debug(
                        f"[JioSavan] HARD ARTIST REJECT: "
                        f"req='{artist}' got='{song_artist}' "
                        f"sim={_art_sim:.2f} title='{song_title}'"
                    )
                    continue

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
        if image: image = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', image)

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
# /api/play — COMPLETE PRODUCTION FIX WITH PARAMETER CLEANING
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    """
    COMPLETE FIXED VERSION with:
    1. Corrupted parameter cleaning (id="title=xxx" issue from logs)
    2. Proper frontend response format
    3. Full fallback chain
    4. Production logging
    """
    
    # ═══════════════════════════════════════════════════════════════════════
    # 🔥 CRITICAL FIX 1: Clean corrupted parameters from frontend
    # Logs showed: id='title=Dil To Pagal Hai Lata Mangeshkar'
    # ═══════════════════════════════════════════════════════════════════════
    raw_id = request.args.get('id', '').strip()
    raw_title = request.args.get('title', '').strip()
    raw_artist = request.args.get('artist', '').strip()
    token = request.args.get('token', '').strip()[:200]
    
    # Log raw request for debugging
    log.info(f"[Play] RAW REQUEST: id='{raw_id[:100]}' title='{raw_title[:50]}' artist='{raw_artist[:30]}'")
    
    # Fix 1: Agar id "title=something" jaisa corrupted hai
    if raw_id and ('title=' in raw_id or 'artist=' in raw_id):
        import re
        log.warning(f"[Play] Corrupted id detected: '{raw_id[:100]}'")
        # Extract actual values from corrupted string
        title_match = re.search(r'title=([^&]+)', raw_id)
        artist_match = re.search(r'artist=([^&]+)', raw_id)
        if title_match and not raw_title:
            raw_title = title_match.group(1)
            log.info(f"[Play] Extracted title from corrupted id: '{raw_title[:50]}'")
        if artist_match and not raw_artist:
            raw_artist = artist_match.group(1)
            log.info(f"[Play] Extracted artist from corrupted id: '{raw_artist[:30]}'")
        raw_id = ''  # Corrupted id ko ignore karo
    
    # Fix 2: Agar id "id=12345" format mein hai toh extract karo
    if raw_id and raw_id.startswith('id='):
        raw_id = raw_id[3:]  # Remove "id=" prefix
        log.info(f"[Play] Removed 'id=' prefix: '{raw_id[:30]}'")
    
    # Fix 3: URL decode if needed
    from urllib.parse import unquote
    song_id = unquote(raw_id)[:100] if raw_id else ''
    title = unquote(raw_title)[:200] if raw_title else ''
    artist = unquote(raw_artist)[:100] if raw_artist else ''
    
    log.info(f"[Play] CLEANED: id='{song_id[:30]}' title='{title[:50]}' artist='{artist[:30]}'")
    
    # ═══════════════════════════════════════════════════════════════════════
    # Validate request
    # ═══════════════════════════════════════════════════════════════════════
    if not song_id and not title:
        log.error("[Play] ERROR: Neither id nor title provided")
        return jsonify({'success': False, 'error': 'Missing id or title parameter', 'token': token}), 400
    
    # ═══════════════════════════════════════════════════════════════════════
    # Check caches
    # ═══════════════════════════════════════════════════════════════════════
    import hashlib
    cache_key = f"play:{song_id or hashlib.md5(f"{title}:{artist}".encode()).hexdigest()[:16]}:{normalize(artist)}"
    
    # Check L1 cache
    cached_result = _l1_saavn.get(cache_key)
    if cached_result and cached_result.get('url') and cached_result.get('trackName'):
        log.info(f"[Play] L1 CACHE HIT: '{cached_result.get('trackName')}'")
        return jsonify({
            'success': True,
            'token': token,
            'url': cached_result.get('url'),
            'trackName': cached_result.get('trackName'),
            'artistName': cached_result.get('artistName'),
            'artworkUrl100': cached_result.get('artworkUrl100', ''),
            'trackId': cached_result.get('trackId', song_id),
            '_saavnId': cached_result.get('_saavnId', song_id),
            'quality': cached_result.get('quality', 'unknown'),
            'source': cached_result.get('source', 'cached'),
        })
    
    # ═══════════════════════════════════════════════════════════════════════
    # Resolve audio source
    # ═══════════════════════════════════════════════════════════════════════
    audio_url = None
    result_title = title
    result_artist = artist
    result_artwork = ''
    result_quality = 'unknown'
    result_source = 'unknown'
    result_confidence = 0.0
    result_track_id = song_id
    
    # ── 1. If we have song_id, fetch Saavn by ID ──────────────────────────
    if song_id:
        log.info(f"[Play] Fetching by ID: {song_id}")
        saavn_result = _fetch_saavn_by_id(song_id, expected_title=title, expected_artist=artist)
        if saavn_result and saavn_result.get('url'):
            audio_url = saavn_result['url']
            result_quality = saavn_result.get('quality', '320kbps')
            result_title = saavn_result.get('title', title)
            result_artist = saavn_result.get('artist', artist)
            result_artwork = saavn_result.get('image', '')
            result_source = 'saavn'
            result_confidence = saavn_result.get('_confidence', 0.90)
            log.info(f"[Play] ID RESOLVE SUCCESS: '{result_title}' -> {result_quality}")
    
    # ── 2. If ID failed or not provided, search Saavn ──────────────────────
    if not audio_url and title:
        log.info(f"[Play] Searching Saavn: '{title}' by '{artist}'")
        language = _detect_language(title + ' ' + artist)
        
        search_variants = build_query_variants(title, artist, '')
        
        for variant in search_variants:
            if audio_url: break
            saavn_search = fetch_saavn_parallel(variant, title=title, artist=artist, language=language)
            if saavn_search and saavn_search.get('url'):
                _ok, _conf, _reason = _is_confirmed_match(
                    title, artist,
                    saavn_search.get('title', ''),
                    saavn_search.get('artist', ''),
                    source='saavn',
                    min_conf=0.65
                )
                if _ok:
                    audio_url = saavn_search['url']
                    result_quality = saavn_search.get('quality', '320kbps')
                    result_title = saavn_search.get('title', title)
                    result_artist = saavn_search.get('artist', artist)
                    result_artwork = saavn_search.get('image', '')
                    result_source = 'saavn'
                    result_confidence = _conf
                    log.info(f"[Play] SAAVN SEARCH SUCCESS: '{result_title}' conf={_conf:.2f}")
                    break
                else:
                    log.debug(f"[Play] SAAVN SEARCH REJECTED: {_reason}")
    
    # ── 3. Fallback to JioSavan ───────────────────────────────────────────
    if not audio_url and title:
        log.info(f"[Play] Trying JioSavan: '{title}'")
        jiosavan_result = fetch_from_jiosavan(title, artist)
        if jiosavan_result and jiosavan_result.get('url'):
            _ok, _conf, _reason = _is_confirmed_match(
                title, artist,
                jiosavan_result.get('title', ''),
                jiosavan_result.get('artist', ''),
                source='jiosavan',
                min_conf=0.60
            )
            if _ok:
                audio_url = jiosavan_result['url']
                result_quality = jiosavan_result.get('quality', '320kbps')
                result_title = jiosavan_result.get('title', title)
                result_artist = jiosavan_result.get('artist', artist)
                result_artwork = jiosavan_result.get('image', '')
                result_source = 'jiosavan'
                result_confidence = _conf
                log.info(f"[Play] JIOSAVAN SUCCESS: '{result_title}' conf={_conf:.2f}")
            else:
                log.debug(f"[Play] JIOSAVAN REJECTED: {_reason}")
    
    # ── 4. Fallback to YouTube Music ───────────────────────────────────────
    if not audio_url and title:
        log.info(f"[Play] Trying YouTube Music: '{title}'")
        anchor = {'title': title, 'artist': artist}
        ytm_result = fetch_from_ytmusic(title, artist, anchor)
        if ytm_result and ytm_result.get('url'):
            _ok, _conf, _reason = _is_confirmed_match(
                title, artist,
                ytm_result.get('title', ''),
                ytm_result.get('artist', ''),
                source='ytmusic',
                min_conf=0.55
            )
            if _ok:
                audio_url = ytm_result['url']
                result_quality = ytm_result.get('quality', '128kbps')
                result_title = ytm_result.get('title', title)
                result_artist = ytm_result.get('artist', artist)
                result_artwork = ytm_result.get('image', '')
                result_source = 'ytmusic'
                result_confidence = _conf
                log.info(f"[Play] YTMUSIC SUCCESS: '{result_title}' conf={_conf:.2f}")
            else:
                log.debug(f"[Play] YTMUSIC REJECTED: {_reason}")
    
    # ── 5. Fallback to yt-dlp ──────────────────────────────────────────────
    if not audio_url and title:
        log.info(f"[Play] Trying yt-dlp: '{title}'")
        ytdlp_result = fetch_from_ytdlp(title, artist)
        if ytdlp_result and ytdlp_result.get('url'):
            _ok, _conf, _reason = _is_confirmed_match(
                title, artist,
                ytdlp_result.get('title', ''),
                ytdlp_result.get('artist', ''),
                source='youtube',
                min_conf=0.50
            )
            if _ok:
                audio_url = ytdlp_result['url']
                result_quality = ytdlp_result.get('quality', '128kbps')
                result_title = ytdlp_result.get('title', title)
                result_artist = ytdlp_result.get('artist', artist)
                result_artwork = ytdlp_result.get('image', '')
                result_source = 'youtube'
                result_confidence = _conf
                log.info(f"[Play] YTDLP SUCCESS: '{result_title}' conf={_conf:.2f}")
            else:
                log.debug(f"[Play] YTDLP REJECTED: {_reason}")
    
    # ── 6. Fallback to SoundCloud ──────────────────────────────────────────
    if not audio_url and title:
        log.info(f"[Play] Trying SoundCloud: '{title}'")
        sc_result = fetch_from_soundcloud(title, artist)
        if sc_result and sc_result.get('url'):
            _ok, _conf, _reason = _is_confirmed_match(
                title, artist,
                sc_result.get('title', ''),
                sc_result.get('artist', ''),
                source='soundcloud',
                min_conf=0.50
            )
            if _ok:
                audio_url = sc_result['url']
                result_quality = sc_result.get('quality', '128kbps')
                result_title = sc_result.get('title', title)
                result_artist = sc_result.get('artist', artist)
                result_artwork = sc_result.get('image', '')
                result_source = 'soundcloud'
                result_confidence = _conf
                log.info(f"[Play] SOUNDCLOUD SUCCESS: '{result_title}' conf={_conf:.2f}")
            else:
                log.debug(f"[Play] SOUNDCLOUD REJECTED: {_reason}")
    
    # ── 7. Final fallback to Piped/Invidious ────────────────────────────────
    if not audio_url and title:
        log.info(f"[Play] Trying Piped: '{title}'")
        piped_result = fetch_from_piped(title, title=title, artist=artist)
        if piped_result and piped_result.get('url'):
            audio_url = piped_result['url']
            result_quality = piped_result.get('quality', '128kbps')
            result_title = piped_result.get('title', title)
            result_artist = piped_result.get('artist', artist)
            result_artwork = piped_result.get('image', '')
            result_source = 'piped'
            result_confidence = 0.60
            log.info(f"[Play] PIPED SUCCESS: '{result_title}'")
    
    if not audio_url and title:
        log.info(f"[Play] Trying Invidious: '{title}'")
        inv_result = fetch_from_invidious(title, title=title, artist=artist)
        if inv_result and inv_result.get('url'):
            audio_url = inv_result['url']
            result_quality = inv_result.get('quality', '128kbps')
            result_title = inv_result.get('title', title)
            result_artist = inv_result.get('artist', artist)
            result_artwork = inv_result.get('image', '')
            result_source = 'invidious'
            result_confidence = 0.55
            log.info(f"[Play] INVIDIOUS SUCCESS: '{result_title}'")
    
    # ═══════════════════════════════════════════════════════════════════════
    # Check if we have audio URL
    # ═══════════════════════════════════════════════════════════════════════
    if not audio_url:
        log.error(f"[Play] FAILED: No audio source found for id='{song_id}' title='{title}' artist='{artist}'")
        return jsonify({'success': False, 'error': 'No audio source found', 'token': token}), 404
    
    # ═══════════════════════════════════════════════════════════════════════
    # Enhance artwork
    # ═══════════════════════════════════════════════════════════════════════
    final_artwork = result_artwork
    if not final_artwork and title:
        final_artwork = _get_artwork(title, result_artist)
    if not final_artwork and title:
        try:
            from core import _fetch_itunes_artwork
            final_artwork = _fetch_itunes_artwork(title, result_artist)
            if final_artwork:
                _store_artwork(title, result_artist, final_artwork, 5)
        except Exception:
            pass
    
    if final_artwork:
        final_artwork = _fix_image_url(final_artwork)
    
    # Generate track ID if not available
    if not result_track_id and title:
        result_track_id = hashlib.md5(f"{result_title}:{result_artist}".encode()).hexdigest()[:16]
    
    # ═══════════════════════════════════════════════════════════════════════
    # Prepare response (frontend-expected format)
    # ═══════════════════════════════════════════════════════════════════════
    response_data = {
        'success': True,
        'token': token,
        'url': audio_url,
        'trackName': result_title,
        'artistName': result_artist,
        'artworkUrl100': final_artwork or '',
        'trackId': result_track_id,
        '_saavnId': song_id or result_track_id,
        'quality': result_quality,
        'source': result_source,
        'confidence': round(result_confidence, 3),
    }
    
    # ═══════════════════════════════════════════════════════════════════════
    # Cache the successful result
    # ═══════════════════════════════════════════════════════════════════════
    cache_payload = {
        'url': audio_url,
        'trackName': result_title,
        'artistName': result_artist,
        'artworkUrl100': final_artwork or '',
        'trackId': result_track_id,
        '_saavnId': song_id or result_track_id,
        'quality': result_quality,
        'source': result_source,
        'confidence': round(result_confidence, 3),
    }
    
    _l1_saavn.set(cache_key, cache_payload)
    
    if result_confidence >= 0.65:
        _executor_cache.submit(_supabase_cache_set, cache_key, cache_payload, result_confidence)
    
    if song_id and result_confidence >= 0.75:
        _store_verified(song_id, result_title, result_artist, cache_payload, result_confidence)
    
    if final_artwork and result_title:
        priority = 1 if result_source in ('saavn', 'jiosavan') else 2 if result_source == 'ytmusic' else 3
        _store_artwork(result_title, result_artist, final_artwork, priority)
    
    log.info(f"[Play] ✅ SUCCESS: '{result_title}' src={result_source} q={result_quality} conf={result_confidence:.2f}")
    
    return jsonify(response_data)


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
                    _real_conf = 0.90
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
                    _res_title  = _res.get('title', '')
                    _res_artist = _res.get('artist', '')
                    if _title and _res_title:
                        if not dna_compatible(_title, _res_title, threshold=0.6):
                            log.warning(f"[Prefetch] DNA MISMATCH cache write blocked: '{_res_title}'")
                            continue
                        _ok, _pconf, _reason = _is_confirmed_match(
                            _title, _artist, _res_title, _res_artist,
                            source='saavn', min_conf=0.72,
                        )
                        if not _ok:
                            log.warning(f"[Prefetch] Cache write rejected: '{_res_title}' {_reason}")
                            continue
                    _l1_saavn.set(_ck, _res)
                    return

    for song in queue:
        _executor_bg.submit(_do_prefetch, song)
    return jsonify({'status': 'prefetching', 'count': len(queue)})


# ═══════════════════════════════════════════════════════════════════════════════
# FIX: TRY-EXCEPT BLOCK FOR CORE IMPORTS
# ═══════════════════════════════════════════════════════════════════════════════
try:
    from core import _l1_artwork, _l1_verified
except ImportError:
    from cachetools import TTLCache
    _l1_artwork = TTLCache(maxsize=500, ttl=3600)
    _l1_verified = TTLCache(maxsize=500, ttl=3600)


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════
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
                art = re.sub(r'\b\d+x\d+bb\b', '600x600bb', art)
                art = re.sub(r'\b\d+x\d+\b', '600x600', art)
                return art
    except Exception:
        pass
    return ''


def _is_allowed_domain(domain: str) -> bool:
    return any(
        domain == d or domain.endswith('.' + d)
        for d in ALLOWED_STREAM_DOMAINS
    )


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO PREFETCH FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════
_url_refresh_queue = set()

def _auto_prefetch_search_results(results):
    if not results: return
    try:
        for song in results[:5]:
            song_id     = song.get('trackId') or song.get('_saavnId') or song.get('id') or ''
            song_title  = song.get('trackName') or song.get('title') or ''
            song_artist = song.get('artistName') or song.get('artist') or ''
            if song_id or song_title:
                _executor_bg.submit(_do_prefetch_song,
                                    {'id': song_id, 'title': song_title, 'artist': song_artist})
    except Exception as e:
        log.debug(f"[AutoPrefetch] Error: {e}")

def _do_prefetch_song(song):
    try:
        _id = str(song.get('id', '')).strip()[:100]
        _title = str(song.get('title', '')).strip()[:200]
        _artist = str(song.get('artist', '')).strip()[:100]
        if not _id and not _title:
            return
        _ck = f"play:{_id or normalize(_title)}:{normalize(_artist)}"
        if _l1_saavn.get(_ck):
            return
        if _id:
            result = _fetch_saavn_by_id(_id, _title, _artist)
            if result and result.get('url'):
                _r_title = result.get('title', '') or _title
                _r_artist = result.get('artist', '') or _artist
                if _title:
                    _ok, _conf, _reason = _is_confirmed_match(
                        _title, _artist, _r_title, _r_artist,
                        source='saavn', min_conf=0.70,
                    )
                    if not _ok:
                        return
                    _real_conf = _conf
                else:
                    _real_conf = 0.90
                _payload = {
                    **result,
                    'source': 'saavn',
                    'confidence': round(_real_conf, 3),
                    'title': _title or _r_title,
                    'artist': _artist or _r_artist,
                }
                _l1_saavn.set(_ck, _payload)
                return
        if _title:
            _lang = _detect_language(_title + ' ' + _artist)
            for _qv in build_query_variants(_title, _artist, '')[:2]:
                _res = fetch_saavn_parallel(_qv, title=_title, artist=_artist, language=_lang)
                if _res and _res.get('url'):
                    _res_title  = _res.get('title', '')
                    _res_artist = _res.get('artist', '')
                    if _title and _res_title:
                        if not dna_compatible(_title, _res_title, threshold=0.6):
                            log.warning(f"[PrefetchSong] DNA MISMATCH blocked: '{_res_title}'")
                            continue
                        _ok, _pconf, _reason = _is_confirmed_match(
                            _title, _artist, _res_title, _res_artist,
                            source='saavn', min_conf=0.72,
                        )
                        if not _ok:
                            log.warning(f"[PrefetchSong] Cache write rejected: '{_res_title}' {_reason}")
                            continue
                    _l1_saavn.set(_ck, _res)
                    return
    except Exception as e:
        log.debug(f"[Prefetch] Error: {e}")

_do_prefetch = _do_prefetch_song
