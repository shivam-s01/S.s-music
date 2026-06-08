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
    # ── TVE Match Cache + Saavn Anchor ──────────────────────────────────────
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
    # ── Track Verification Engine ──────────────────────────────────────────
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
_DIRECT_TIMEOUT = 8


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
        if not dna_compatible(_conf_title, song_title): continue
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
    # [FIX-SEARCH-FILTER] Query mein version request hai ya nahi
    _query_wants_ver = _query_requests_version(query) if query else False

    # Extract request artist from query if present (format: "artist title" or "title artist")
    # Used for soft artist pre-filter on search results
    _req_artist_hint = ''
    if query:
        # If query looks like it has an artist (contains known artist patterns), extract it
        # But we don't want to be too aggressive here — this is search, not play
        # So we use a loose threshold: 0.45 (vs 0.55 in fetch_from_mirror)
        pass  # artist hint extracted per-song below

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

        # [FIX-SEARCH-FILTER] Lofi/Remix/Cover/Instrumental search results mein nahi aane chahiye
        # Jab tak user explicitly "lofi" ya "remix" type na kare query mein
        if not _query_wants_ver:
            if _is_remix_or_cover(title):
                log.debug(f"[SearchFilter] REMIX/COVER blocked: '{title}'")
                continue
            if _is_slowed_reverb(title):
                log.debug(f"[SearchFilter] SLOWED/REVERB blocked: '{title}'")
                continue
            if _is_live_version(title):
                log.debug(f"[SearchFilter] LIVE blocked: '{title}'")
                continue
            # Extra: has_version_words check — "Instrumental", "Karaoke", "Flip" etc
            if has_version_words(title):
                log.debug(f"[SearchFilter] VERSION WORD blocked: '{title}'")
                continue
            # Query se title DNA match — "Bahara" search pe "Badra Bahaar" ya "Bahara X" block
            if query and not dna_compatible(query, title):
                log.debug(f"[SearchFilter] DNA MISMATCH blocked: '{title}' for query='{query}'")
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
    _deadline = time.time() + 5.0  # 5s hard deadline
    _lang = _detect_language(title + ' ' + artist)  # [FIX] _lang was undefined — NameError fix

    # ── Phase 1: saavn.dev + direct (parallel) ────────────────────────────────
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
                        if not dna_compatible(title, song_title): continue
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

    # ── Phase 2: mirror fallback ───────────────────────────────────────────────
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
                        if not dna_compatible(title, song_title): continue
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
    """
    Stream URL + actual duration — fixes YTM Tier 1 duration gap.
    YTMusic search API returns duration=0; this fetches the real value.
    Returns (url, quality, duration_s).
    """
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
    """
    anchor: Saavn ground-truth metadata dict — if provided, TVE uses
    tve_validate_anchored (language/duration from Saavn, not guessed).
    """
    if not _cb.is_allowed('ytmusic'):
        log.debug('[CB] ytmusic OPEN — skipping'); return None
    l1_key = f"ytmusic:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached

    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''

    # Anchor-based search query — more specific than generic "title artist"
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

    # ── TVE: build candidate list (duration=0 initially — fixed post-selection) ──
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

    # ── Real duration fetch — fixes Tier 1 YTM duration=0 gap ────────────────
    url, quality, actual_dur = _ytm_get_stream_with_duration(video_id)
    if not url: return None

    # Post-stream Tier 1 re-check with actual duration
    if _saavn_dur > 0 and actual_dur > 0:
        if not tve_tier1_duration(_saavn_dur, actual_dur):
            log.warning(
                f"[YTMusic] Real duration gate FAILED: saavn={_saavn_dur}s "
                f"yt={actual_dur}s '{best.get('title')}'"
            )
            return None

    # Legacy confidence cross-check
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

                    # ── TVE: build candidate list (up to 5) ──────────────────
                    entries = [e for e in info['entries'] if e and e.get('duration', 0) > 90]
                    if not entries:
                        entries = [e for e in info['entries'] if e]

                    # Convert entries → TVE candidate dicts
                    tve_candidates = []
                    for entry in entries[:5]:
                        tve_candidates.append({
                            'title':      entry.get('title', ''),
                            'artist':     entry.get('uploader', '') or entry.get('artist', ''),
                            'duration_s': int(entry.get('duration', 0) or 0),
                            '_entry':     entry,  # preserve full entry for URL extraction
                        })

                    # ── Retrieve Saavn duration hint if available ─────────────
                    # Pass 0 if unknown — Tier 1 skips gracefully
                    _saavn_dur = 0  # ytdlp path: no Saavn reference duration

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
                        # Also check scores for mismatch_error — all 5 failed
                        if scores and scores.get('status') == 'mismatch_error':
                            log.debug(f"[yt-dlp] TVE: {scores['message']} for '{title}'")
                        # Try next search query
                        continue

                    best_result = best_candidate['_entry']

                    # ── Legacy confidence cross-check (keep existing gate) ────
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

                    # ── Extract audio formats ─────────────────────────────────
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
                            # [FIX-THUMB] maxresdefault (1280x720) best quality
                            thumb = f"https://img.youtube.com/vi/{vid_id}/maxresdefault.jpg"

                    if not verify_via_fingerprint(best_fmt['url'], title, artist):
                        log.warning(f"[yt-dlp] Fingerprint FAILED: '{best_result.get('title')}'")
                        _cb.record_failure('youtube')
                        continue  # try next query rather than returning None

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

            # All search queries exhausted — genuine not-found
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

            # ── TVE: build candidate list (up to 5) ──────────────────────────
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
                source='soundcloud',  # Tier 5 artist check skipped for SC
                anchor=anchor,
            )

            if best_candidate is None:
                # Strict fallback — all 5 failed
                log.warning(
                    f"[SoundCloud] TVE all candidates failed for '{title}': "
                    f"{scores.get('message', 'no verified track')}"
                )
                return None

            best = best_candidate['_entry']

            # Legacy confidence cross-check
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
            if _c < 0.80:
                log.warning(f"[SaavnID] MISMATCH: expected='{expected_title}' got='{id_result['title']}' conf={_c:.3f}")
                return None
        _t = id_result.get('title', '')
        if not _query_requests_version(expected_title):
            if _is_live_version(_t) or _is_remix_or_cover(_t) or _is_slowed_reverb(_t):
                log.warning(f"[SaavnID] VERSION REJECTED: '{_t}'"); return None
            if expected_title and not dna_compatible(expected_title, _t):
                log.warning(f"[SaavnID] DNA MISMATCH: '{_t}'"); return None
        id_result['image'] = _fix_image_url(id_result.get('image','') or _get_artwork(id_result.get('title',''), id_result.get('artist','')))
        if id_result['image']: _store_artwork(id_result['title'], id_result['artist'], id_result['image'], 1)
        _l1_saavn.set(l1_key, id_result)
        return id_result

    # ── 1. saavn.dev (fastest, pre-decoded URLs) ──────────────────────────────
    def _try_saavndev():
        data = _saavndev_get(f'/api/songs/{song_id}', {})
        if not data: return None
        inner = data.get('data') or data
        song  = (inner[0] if isinstance(inner, list) and inner
                 else inner if isinstance(inner, dict) and inner.get('id') else None)
        if not song: return None
        return _parse_saavn_song_raw(song)

    # ── 2. jiosaavn.com internal API ─────────────────────────────────────────
    def _try_direct():
        data = _jiosaavn_api({'__call': 'song.getDetails', 'pids': song_id})
        if not data: return None
        song = None
        if isinstance(data.get('songs'), list) and data['songs']: song = data['songs'][0]
        elif isinstance(data, dict) and data.get(song_id): song = data[song_id]
        elif isinstance(data.get('data'), list) and data['data']: song = data['data'][0]
        if not song: return None
        return _parse_saavn_song_raw(song)

    # ── 3. Mirror fallback ────────────────────────────────────────────────────
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

    # Phase 1: saavn.dev + jiosaavn direct (parallel, 5s)
    _d = {_executor.submit(_try_saavndev): 'saavndev',
          _executor.submit(_try_direct):   'direct'}
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

    # Phase 2: mirrors (parallel, 3s) if direct missed
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

                if not dna_compatible(_conf_title, song_title):
                    log.debug(f"[Mirror] DNA MISMATCH: '{song_title}'")
                    continue

                # [FIX-QUERY-TITLE] query (variant) nahi, _conf_title (original) use karo
                if not has_word_match(_conf_title, song_title): continue
                dur = int(song.get('duration', 999) or 999)
                if dur > 1080: continue

                # ── HARD ARTIST GATE ─────────────────────────────────────────
                # Agar request mein artist diya hai toh artist match MANDATORY hai.
                # Title match 100% hone pe bhi wrong artist = reject.
                # Ye Bhojpuri/Bhakti/Hindi cross-contamination ka primary fix hai.
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

    # ── Phase 1: saavn.dev + jiosaavn direct (parallel, 5s) ──────────────────
    _df = {_executor.submit(_search_saavndev,        query, _lang): 'saavndev',
           _executor.submit(_search_direct_jiosaavn, query, _lang): 'direct'}
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

    # ── Phase 2: mirror fallback (2.5s) if direct missed ─────────────────────
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

            if not dna_compatible(title, song_title):
                log.debug(f"[JioSavan] DNA MISMATCH: '{song_title}'"); continue

            # ── HARD ARTIST GATE (same logic as fetch_from_mirror) ───────────
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
# /api/play — SMART PLAYBACK PIPELINE
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

    def _check_cache_entry(entry):
        if not entry or not entry.get('url'): return None
        _ct = entry.get('title', '')

        if not _user_wants_ver:
            if (_is_remix_or_cover(_ct) or
                _is_slowed_reverb(_ct) or
                _is_live_version(_ct)):
                return None

        if title and _ct:
            if not dna_compatible(title, _ct):
                log.info(f"[Cache] DNA MISMATCH — invalidating cached='{_ct}'")
                return None

        # [FIX-CACHE-ARTIST] Artist mismatch fast reject — same title different artist
        # compute_confidence internally check karta hai lekin duration=0 force se
        # false pass ho sakta tha. Yahan explicit fast reject.
        _cached_artist = entry.get('artist', '')
        if artist and _cached_artist:
            from core import _seq_ratio, _normalize_artist, normalize as _cn
            _qa = _normalize_artist(_cn(artist))
            _ra = _normalize_artist(_cn(_cached_artist))
            if _qa and _ra and _seq_ratio(_qa, _ra) < 0.35:
                log.info(f"[Cache] ARTIST MISMATCH — invalidating cached artist='{_cached_artist}' req='{artist}'")
                return None

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
        for _ck in filter(None, keys):
            _l1_saavn.delete(_ck)
            _executor_cache.submit(sb_delete, 'song_cache', {'cache_key': _ck})

    _verified_hit = None

    # ── TVE Match Cache Fast-Path ──────────────────────────────────────────────
    # If this Saavn ID was already verified against a YT/SC source,
    # skip the full search + validation pipeline entirely.
    if song_id and not audio_url:
        _tve_hit = tve_match_get_verified(song_id, req_title=title, req_artist=artist)
        if _tve_hit and _tve_hit.get('url'):
            _tve_entry = _check_cache_entry(_tve_hit)
            if _tve_entry:
                audio_url  = _tve_entry['url']
                quality    = _tve_entry.get('quality', 'unknown')
                source     = _tve_entry.get('source', 'unknown')
                confidence = float(_tve_entry.get('_confidence', _tve_entry.get('confidence', 0.80)))
                if not title:  title  = _tve_entry.get('title', '')
                if not artist: artist = _tve_entry.get('artist', '')
                log.info(f"[TVECache] ✓ Fast-path hit: id={song_id} src={source}")
            else:
                # URL likely expired — evict and re-resolve
                tve_match_invalidate(song_id)
                log.debug(f"[TVECache] Evicted expired entry for id={song_id}")

    hit = _get_verified(song_id=song_id, title=title, artist=artist)
    _verified_hit = _check_cache_entry(hit)
    if _verified_hit:
        audio_url  = _verified_hit['url']
        quality    = _verified_hit.get('quality', 'unknown')
        source     = _verified_hit.get('source', 'unknown')
        confidence = float(_verified_hit.get('confidence', 0.90))
        if not title:  title  = _verified_hit.get('title', '')
        if not artist: artist = _verified_hit.get('artist', '')

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
                _invalidate_cache_keys(_ck)

    if not audio_url:
        for _ck in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
            raw_hit = _supabase_cache_get_with_refresh(_ck)
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
                _invalidate_cache_keys(_ck)

    if not audio_url and title:
        _indexed_id = _song_index_get(title, artist)
        if _indexed_id and _indexed_id != song_id:
            _idx_result = _fetch_saavn_by_id(_indexed_id, expected_title=title, expected_artist=artist)
            if _idx_result and _idx_result.get('url'):
                # [FIX-INDEX-STALE] Song index mein purana ID ho sakta hai same-title different-artist ka
                # _fetch_saavn_by_id ke andar 0.65 conf check hoti hai lekin
                # yahan stricter 0.75 + explicit confirm — stale index entry block karo
                _ok, _idx_conf, _idx_reason = _is_confirmed_match(
                    title, artist,
                    _idx_result.get('title', ''), _idx_result.get('artist', ''),
                    source='saavn', min_conf=0.75,
                )
                if not _ok:
                    log.warning(f"[SongIndex] Stale ID rejected: req='{title}' got='{_idx_result.get('title')}' reason={_idx_reason}")
                    _idx_result = None
            if _idx_result and _idx_result.get('url'):
                audio_url  = _idx_result['url']
                quality    = _idx_result.get('quality', 'unknown')
                source     = 'saavn'; confidence = 0.95
                song_id    = _indexed_id
                if not title:  title  = _idx_result.get('title', '')
                if not artist: artist = _idx_result.get('artist', '')
                log.info(f"[SongIndex] ✓ Fast path: '{title}' → {_indexed_id}")

    if not audio_url and song_id:
        result = _fetch_saavn_by_id(song_id, expected_title=title, expected_artist=artist)
        if result and result.get('url'):
            _r_title  = result.get('title', '') or title
            _r_artist = result.get('artist', '') or artist
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
                # Store Saavn anchor for YT/SC fallback reference
                if result: store_saavn_anchor(song_id, {**result, 'duration_s': result.get('duration', 0)})

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
                # Store anchor from search result
                if result and song_id:
                    store_saavn_anchor(song_id, {**result, 'duration_s': int(result.get('duration', 0))})
                elif result:
                    # No song_id — store by title+artist key
                    store_saavn_anchor('title:' + normalize(title), {**result, 'duration_s': 0})
                break

    if not audio_url and title:
        log.info(f"[Play] Saavn miss → fallbacks: '{title}'")
        _MIN_FALLBACK_CONF = _conf_tuner.get_floor(title, artist)

        _phase1_futures = {
            _executor.submit(fetch_from_jiosavan, title, artist, _play_lang): 'jiosavan',
        }
        _phase1_candidates = []
        try:
            for future in as_completed(_phase1_futures, timeout=1.5):  # 3.0→1.5s
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

        if not audio_url:
            # Retrieve Saavn anchor for anchor-based TVE validation
            _fb_anchor = get_saavn_anchor(song_id=song_id, title=title, artist=artist)
            if _fb_anchor:
                log.debug(f"[Play] Anchor found: dur={_fb_anchor.get('duration_s')}s "
                          f"lang={_fb_anchor.get('language')} for '{title}'")
            _all_fb_futures = {
                _executor.submit(fetch_from_ytmusic,    title, artist, _fb_anchor): 'ytmusic',
                _executor.submit(fetch_from_ytdlp,      title, artist, _fb_anchor): 'youtube',
                _executor.submit(fetch_from_soundcloud, title, artist, _fb_anchor): 'soundcloud',
                _executor.submit(fetch_from_piped,      title, title=title,
                                 artist=artist):                                     'piped',
                _executor.submit(fetch_from_invidious,  title, title=title,
                                 artist=artist):                                     'invidious',
            }

            _fb_candidates = []; _arrival_idx = 0
            _deadline      = time.time() + 1.8  # 2.5→1.8s

            try:
                for future in as_completed(_all_fb_futures, timeout=1.8):  # 2.5→1.8s
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

    if not audio_url and title:
        for broad_query in [title, title.split()[0] if title.split() else title]:
            broad = fetch_from_ytdlp(broad_query, artist)
            if broad and broad.get('url'):
                broad_conf = float(broad.get('_confidence', 0.0))
                if broad_conf < 0.60: continue
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

    if audio_url:
        _gate_min = 0.60 if source in ('saavn', 'jiosavan') else 0.65
        if confidence < _gate_min:
            log.warning(f"[Play] FINAL GATE: low confidence={confidence:.3f} source={source}")
            if title: _conf_tuner.record_miss(title, artist)
            return jsonify({'error': 'No confident audio match found'}), 404

    _orig_title  = request.args.get('title', '').strip()
    _orig_artist = request.args.get('artist', '').strip()
    if _orig_title and title:
        if not dna_compatible(_orig_title, title):
            log.warning(f"[FINAL DNA] BLOCK: orig='{_orig_title}' got='{title}' src={source}")
            _conf_tuner.record_miss(_orig_title, _orig_artist)
            _invalidate_cache_keys(_play_ck_id, _play_ck_title, _play_ck)
            return jsonify({'error': 'Song DNA mismatch — version blocked'}), 404
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

    _final_result_title  = title
    _final_result_artist = artist
    if title and audio_url and source not in ('saavn',):
        _final_conf = compute_confidence(
            request.args.get('title', '').strip(),
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

    if title: _conf_tuner.record_accept(title, artist, confidence)
    _src_perf.record(source, 0, True)

    def _resolve_best_artwork(song_id, title, artist, result_image=''):
        from match_engine import pick_image as _pick_img
        _ensure_500 = _fix_image_url

        candidates = []

        # [FIX-ITUNES-PRIORITY] iTunes (mzstatic.com) = priority 1 hamesha
        # Saavn se pehle check karo — iTunes 600x600 best quality hai
        if title:
            _art = _get_artwork(title, artist)
            if _art:
                _pri = 1 if 'mzstatic.com' in _art else 2
                candidates.append((_pri, _ensure_500(_art)))

        if song_id:
            _id_hit = _l1_saavn.get(f"saavn_id:{song_id}")
            if _id_hit and _id_hit.get('image'):
                _img = _id_hit['image']
                _pri = 1 if 'mzstatic.com' in _img else 2
                candidates.append((_pri, _ensure_500(_img)))

        if result_image and result_image.startswith('http'):
            _pri = 1 if 'mzstatic.com' in result_image else 3
            candidates.append((_pri, _ensure_500(result_image)))

        if title:
            for _src_key in [f"saavn_q:{normalize(title)}", _play_ck, _play_ck_id, _play_ck_title]:
                if not _src_key: continue
                _cache_hit = _l1_saavn.get(_src_key)
                if _cache_hit and _cache_hit.get('image'):
                    _img = _cache_hit['image']
                    _pri = 1 if 'mzstatic.com' in _img else 4
                    candidates.append((_pri, _ensure_500(_img)))
                    break

        if candidates:
            candidates.sort(key=lambda x: x[0])
            best_url = candidates[0][1]
            if best_url: return best_url

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

    # ── TVE Match Cache Write ──────────────────────────────────────────────────
    # If the final source is a YT/SC fallback (not native Saavn), store the
    # Saavn ID → result mapping so future plays skip the TVE pipeline entirely.
    if song_id and source not in ('saavn', 'jiosavan') and confidence >= 0.80:
        tve_match_set(song_id, _cache_payload,
                      req_title=request.args.get('title', '').strip(),
                      req_artist=request.args.get('artist', '').strip())

    if confidence >= 0.85 and title and song_id and source in ('saavn', 'jiosavan'):
        _executor_cache.submit(_song_index_put, title, artist, song_id, title, _best_art)

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
                    # [FIX-PREFETCH-CACHE] Validate before cache write — prevent cache pollution
                    # fetch_saavn_parallel internal confidence check hoti hai lekin
                    # yahan explicit DNA + confirm = wrong song cache mein nahi jayega
                    _res_title  = _res.get('title', '')
                    _res_artist = _res.get('artist', '')
                    if _title and _res_title:
                        if not dna_compatible(_title, _res_title):
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
# FIX: TRY-EXCEPT BLOCK FOR CORE IMPORTS (SYNTAX ERROR FIXED)
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
                # [FIX-ITUNES-MB] 100x100 → 600x600bb — best iTunes quality
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
# AUTO PREFETCH FUNCTIONS FOR server.py
# ═══════════════════════════════════════════════════════════════════════════════
_url_refresh_queue = set()

def _auto_prefetch_search_results(results):
    """Prefetch top 5 songs in parallel — warms cache before user clicks play."""
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
    """Single song prefetch - wrapper for existing logic"""
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
                    # [FIX-PREFETCH-CACHE] Same fix as inline _do_prefetch
                    # Cache pollution prevent karo — wrong song cache mein nahi jayega
                    _res_title  = _res.get('title', '')
                    _res_artist = _res.get('artist', '')
                    if _title and _res_title:
                        if not dna_compatible(_title, _res_title):
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

# Alias for backward compatibility
_do_prefetch = _do_prefetch_song
