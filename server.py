import os
import re
import hmac
import hashlib
import time
import random
import threading
import secrets
import string
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import as_completed
from flask import request, jsonify, send_file, Response, stream_with_context
from urllib.parse import urlparse, quote
from datetime import datetime, timedelta
from typing import Optional

_http_session = requests.Session()
_retry_strategy = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
    raise_on_status=False,
)
_adapter = HTTPAdapter(
    max_retries=_retry_strategy,
    pool_connections=20,
    pool_maxsize=40,
    pool_block=False,
)
_http_session.mount("https://", _adapter)
_http_session.mount("http://",  _adapter)
_http_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (compatible; Aurum/3.1)',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
})
from core import (
    app, limiter, log,
    GOOGLE_CLIENT_ID, ADMIN_KEY, SUPABASE_URL, SUPABASE_KEY,
    _verify_google_jwt, _extract_bearer_sub,
    sb_select, sb_upsert, sb_update, sb_delete,
    _l1_meta, _l1_audio, _l1_saavn, _l1_popular,
    _cb, _src_perf, _conf_tuner,
    _cache_get_l2, _cache_put_l2,
    _get_artwork, _store_artwork,
    _get_verified, _store_verified,
    _song_index_get, _song_index_put,
    _executor, _executor_bg, _executor_cache,
    _supabase_cache_get_with_refresh, _supabase_cache_set,
    _CACHE_MIN_CONFIDENCE,
    _is_remix_or_cover, _is_live_version, _is_slowed_reverb,
    compute_confidence,
)
from match_engine import (
    normalize, clean_query, build_query_variants,
    _detect_language, _is_devotional_query,
    _query_requests_version, NINETIES_TRIGGERS, NINETIES_SEEDS,
    ALLOWED_STREAM_DOMAINS, dna_compatible,
    _safe_year, _pick_low_quality, _ensure_500,
    is_likely_duplicate,
)
from sources import (
    SAAVN_MIRRORS, PIPED_INSTANCES, INVIDIOUS_INSTANCES,
    _best_mirrors, _health,
    _mirror_lock, _piped_lock, _invidious_lock, _sc_client_id_lock,
    SOUNDCLOUD_CLIENT_ID,
    _discover_mirrors, _heal_piped, _heal_invidious,
    _refresh_soundcloud_client_id,
    _maybe_reactive_heal,
)
from fetchers import (
    fetch_saavn_parallel, fetch_from_ytmusic, fetch_from_ytdlp,
    fetch_from_soundcloud, fetch_from_piped, fetch_from_invidious,
    fetch_from_jiosavan,
    _fetch_saavn_search_parallel, _normalize_saavn_songs,
    _resolve_itunes_to_saavn,
    _is_allowed_domain,
    _do_prefetch, _auto_prefetch_search_results,
    _url_refresh_queue,
    _l1_artwork, _l1_verified,
    _fetch_itunes_artwork,
    _fetch_saavn_by_id,
)

def _sb_headers():
    return {
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type':  'application/json',
    }

def _cache_get(key):
    return _l1_meta.get(f"legacy:{key}")

def _cache_set(key, value):
    _l1_meta.set(f"legacy:{key}", value)


# ═══════════════════════════════════════════════════════════════════════════════
# /api/songs — iTunes PRIMARY (fast), Saavn resolve HATA DIYA
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q           = request.args.get('q', 'top bollywood songs').strip()
    era         = request.args.get('era', '').strip()
    is_90s      = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    cache_key = f"songs:{search_term.lower()}"
    cached    = _l1_meta.get(cache_key)
    if cached is not None:
        _executor_bg.submit(_auto_prefetch_search_results, cached)
        return jsonify({'results': cached, '_cached': True})

    results = []

    # ── iTunes se seedha results lo — no Saavn resolve ───────────────────────
    try:
        r = _http_session.get(
            'https://itunes.apple.com/search',
            params={'term': search_term, 'media': 'music', 'entity': 'song',
                    'limit': 50, 'country': 'IN'},
            timeout=8
        )
        r.raise_for_status()
        raw = r.json().get('results', [])

        if is_90s:
            filtered = [s for s in raw if s.get('trackName') and
                        1990 <= _safe_year(s.get('releaseDate')) <= 1999]
            if len(filtered) < 5:
                filtered = [s for s in raw if s.get('trackName')]
            random.shuffle(filtered)
            candidates = filtered[:30]
        else:
            candidates = [s for s in raw if s.get('trackName')][:30]

        for s in candidates:
            # artwork upgrade karo
            art = s.get('artworkUrl100', '')
            if art:
                art = re.sub(r'\b\d+x\d+bb\b', '600x600bb', art)
                art = re.sub(r'\b\d+x\d+\b', '600x600', art)
                s['artworkUrl100'] = art
            # previewUrl → /api/saavn route pe bhejo (title+artist se)
            title  = s.get('trackName', '')
            artist = s.get('artistName', '')
            s['previewUrl'] = f"/api/saavn?q={quote(title, safe='')}&artist={quote(artist, safe='')}"
            results.append(s)

    except Exception as e:
        log.warning(f'[Songs] iTunes failed: {e}')

    # ── iTunes fail — Saavn direct fallback ──────────────────────────────────
    if not results:
        try:
            raw = _fetch_saavn_search_parallel(search_term)
            if raw:
                results = _normalize_saavn_songs(raw, query=search_term)[:30]
        except Exception:
            pass

    if results:
        _l1_meta.set(cache_key, results)
        _executor_bg.submit(_auto_prefetch_search_results, results)
        return jsonify({'results': results})

    return jsonify({'results': [], 'error': 'No results found'})


# ═══════════════════════════════════════════════════════════════════════════════
# /api/songs/90s
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed      = random.choice(NINETIES_SEEDS)
    cache_key = f"songs:{seed.lower()}"
    cached    = _l1_meta.get(cache_key)
    if cached is not None:
        _executor_bg.submit(_auto_prefetch_search_results, cached)
        return jsonify({'results': cached, 'seed': seed, '_cached': True})

    raw = _fetch_saavn_search_parallel(seed)
    if raw:
        normalized = _normalize_saavn_songs(raw, query=seed)
        filtered   = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        result     = (filtered if len(filtered) >= 5 else normalized)[:30]
        random.shuffle(result)
        _l1_meta.set(cache_key, result)
        _executor_bg.submit(_auto_prefetch_search_results, result)
        return jsonify({'results': result, 'seed': seed})

    try:
        r = _http_session.get('https://itunes.apple.com/search',
                         params={'term': seed, 'media': 'music', 'entity': 'song',
                                 'limit': 50, 'country': 'IN'}, timeout=6)
        r.raise_for_status()
        results  = r.json().get('results', [])
        filtered = [s for s in results if s.get('trackName') and
                    1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        if len(filtered) < 5: filtered = [s for s in results if s.get('trackName')]
        random.shuffle(filtered)
        result = filtered[:30]
        _l1_meta.set(cache_key, result)
        _executor_bg.submit(_auto_prefetch_search_results, result)
        return jsonify({'results': result, 'seed': seed})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


# ═══════════════════════════════════════════════════════════════════════════════
# /api/saavn
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/saavn')
@limiter.limit("100 per minute")
def get_saavn_song():
    q           = request.args.get('q', '').strip()[:200]
    artist      = request.args.get('artist', '').strip()[:100]
    fallback    = request.args.get('fallback', '').strip()[:200]
    token       = request.args.get('token', '').strip()[:200]
    low_quality = request.args.get('low_quality', 'false').lower() == 'true'
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})

    _ck   = f"saavn:{normalize(q)}:{normalize(artist)}"
    _lang = _detect_language(q + ' ' + artist)

    def _saavn_cache_valid(entry):
        if not entry or not entry.get('url'): return None
        _user_wants_ver = _query_requests_version(q) or _query_requests_version(artist)
        _ct = entry.get('title', '')
        if not _user_wants_ver:
            if (_is_remix_or_cover(_ct) or _is_live_version(_ct) or _is_slowed_reverb(_ct)):
                return None
            if _ct and not dna_compatible(q, _ct):
                return None
        return entry

    if not low_quality:
        l1_hit = _saavn_cache_valid(_l1_saavn.get(_ck))
        if l1_hit:
            _best_art = _get_artwork(q, artist) or l1_hit.get('image', '')
            if _best_art: l1_hit = dict(l1_hit); l1_hit['image'] = _best_art
            return jsonify({'success': True, 'token': token, **l1_hit})
        elif _l1_saavn.get(_ck):
            _l1_saavn.delete(_ck)

    if not low_quality:
        _l2_fut = _executor_cache.submit(_supabase_cache_get_with_refresh, _ck)
        try:
            _l2_raw = _l2_fut.result(timeout=0.20)
            _cached = _saavn_cache_valid(_l2_raw)
            if _cached:
                _best_art = _get_artwork(q, artist) or _cached.get('image', '')
                if _best_art: _cached = dict(_cached); _cached['image'] = _best_art
                _l1_saavn.set(_ck, _cached)
                return jsonify({'success': True, 'token': token, **_cached})
        except Exception:
            pass

    def _best_image(res_img):
        art = _get_artwork(q, artist)
        if art: return art
        if res_img and res_img.startswith('http'): return res_img
        try:
            _f = _executor_bg.submit(_fetch_itunes_artwork, q, artist)
            _a = _f.result(timeout=1.5)
            if _a: _store_artwork(q, artist, _a, 2); return _a
        except Exception: pass
        return res_img or ''

    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query, title=q, artist=artist, language=_lang)
        if result:
            if low_quality:
                low_url, low_q = _pick_low_quality(result.get('_raw_urls', []))
                if low_url: result['url'] = low_url; result['quality'] = low_q
            conf = float(result.get('_confidence', result.get('score', 0.5)))
            result['image'] = _best_image(result.get('image', ''))
            if conf >= _CACHE_MIN_CONFIDENCE:
                _l1_saavn.set(_ck, result)
            _executor_cache.submit(_supabase_cache_set, _ck, result, conf)
            return jsonify({'success': True, 'token': token, **result})

    ytm = fetch_from_ytmusic(q, artist)
    if ytm and ytm.get('url'):
        conf = float(ytm.get('_confidence', 0.0))
        ytm['image'] = _best_image(ytm.get('image', ''))
        if conf >= _CACHE_MIN_CONFIDENCE: _l1_saavn.set(_ck, ytm)
        _executor_cache.submit(_supabase_cache_set, _ck, ytm, conf)
        return jsonify({'success': True, 'token': token, **ytm})

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        conf = float(yt.get('_confidence', 0.0))
        yt['image'] = _best_image(yt.get('image', ''))
        if conf >= _CACHE_MIN_CONFIDENCE: _l1_saavn.set(_ck, yt)
        _executor_cache.submit(_supabase_cache_set, _ck, yt, conf)
        return jsonify({'success': True, 'token': token, **yt})

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
        conf = float(sc.get('_confidence', 0.0))
        sc['image'] = _best_image(sc.get('image', ''))
        if conf >= _CACHE_MIN_CONFIDENCE: _l1_saavn.set(_ck, sc)
        _executor_cache.submit(_supabase_cache_set, _ck, sc, conf)
        return jsonify({'success': True, 'token': token, **sc})

    return jsonify({'success': False, 'url': None, 'token': token})


# /api/play — fetchers.py mein defined hai (full pipeline: Saavn → YTMusic → YouTube → SC → Piped → Invidious)
# server.py se remove kiya — duplicate route tha jo fetchers.py wale smart pipeline ko override kar raha tha


# ═══════════════════════════════════════════════════════════════════════════════
# /api/resolve
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/resolve')
@limiter.limit("100 per minute")
def resolve_song():
    q        = request.args.get('q', '').strip()[:200]
    artist   = request.args.get('artist', '').strip()[:100]
    fallback = request.args.get('fallback', '').strip()[:200]
    token    = request.args.get('token', '').strip()[:200]
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})

    _lang = _detect_language(q + ' ' + artist)

    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query, title=q, artist=artist, language=_lang)
        if result:
            _rart = _get_artwork(q, artist) or result.get('image', '')
            return jsonify({
                'success': True, 'token': token,
                'url':     f"/api/stream?url={quote(result['url'], safe='')}",
                'quality': result['quality'], 'title': result['title'],
                'artist':  result['artist'],  'image': _rart,
                'source':  'saavn',
            })

    for fetcher, src_name in [
        (lambda: fetch_from_ytmusic(q, artist),               'ytmusic'),
        (lambda: fetch_from_ytdlp(q, artist),                 'youtube'),
        (lambda: fetch_from_soundcloud(q, artist),            'soundcloud'),
        (lambda: fetch_from_piped(q, title=q, artist=artist), 'piped'),
        (lambda: fetch_from_invidious(q, title=q, artist=artist), 'invidious'),
    ]:
        res = fetcher()
        if res and res.get('url'):
            _rart = _get_artwork(q, artist) or res.get('image', '')
            url_val = (f"/api/stream?url={quote(res['url'], safe='')}"
                       if src_name in ('ytmusic', 'youtube', 'soundcloud') else res['url'])
            return jsonify({
                'success': True, 'token': token,
                'url': url_val, 'quality': res['quality'],
                'title': res['title'], 'artist': res['artist'],
                'image': _rart, 'source': src_name,
            })

    return jsonify({'success': False, 'url': None, 'token': token})


# ═══════════════════════════════════════════════════════════════════════════════
# STREAM PROXY
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/stream')
@limiter.limit("200 per minute")
def stream_audio():
    url = request.args.get('url', '').strip()
    if not url: return jsonify({'error': 'Missing URL'}), 400
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'): return jsonify({'error': 'Invalid scheme'}), 400
        domain = parsed.netloc.lower().split(':')[0]
        if not _is_allowed_domain(domain): return jsonify({'error': 'Domain not allowed'}), 403
    except Exception:
        return jsonify({'error': 'Invalid URL'}), 400
    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0',
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header: req_headers['Range'] = range_header
        upstream = requests.get(url, headers=req_headers, stream=True,
                                timeout=(10, None), allow_redirects=True)
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges':  'bytes',
            'Cache-Control':  'no-store',
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally: upstream.close()

        return Response(stream_with_context(generate()), status=upstream.status_code,
                        headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/download')
@limiter.limit("20 per minute")
def download_song():
    q       = request.args.get('q', '').strip()
    artist  = request.args.get('artist', '').strip()
    quality = request.args.get('quality', 'full').strip()
    if not q: return jsonify({'error': 'Missing query'}), 400

    stream_url    = None
    content_type  = 'audio/mpeg'
    filename_base = f"{q} - {artist}".strip(' -') if artist else q
    _lang         = _detect_language(q + ' ' + artist)

    for query in build_query_variants(q, artist, ''):
        result = fetch_saavn_parallel(query, title=q, artist=artist, language=_lang)
        if result and result.get('url'):
            raw_urls = result.get('_raw_urls', [])
            if quality == 'gift' and raw_urls:
                for item in raw_urls:
                    if '320' in str(item.get('quality', '')):
                        stream_url = item.get('url') or item.get('link'); break
            if not stream_url: stream_url = result['url']
            filename_base = f"{result['title']} - {result['artist']}".strip(' -')
            break

    if not stream_url:
        yt = fetch_from_ytdlp(q, artist)
        if yt and yt.get('url'):
            stream_url    = yt['url']
            filename_base = f"{yt['title']} - {yt['artist']}".strip(' -')
            content_type  = 'audio/webm'

    if not stream_url:
        sc = fetch_from_soundcloud(q, artist)
        if sc and sc.get('url'):
            stream_url    = sc['url']
            filename_base = f"{sc['title']} - {sc['artist']}".strip(' -')

    if not stream_url: return jsonify({'error': 'Song not found'}), 404

    try:
        _dl_parsed = urlparse(stream_url)
        if _dl_parsed.scheme not in ('http', 'https'):
            return jsonify({'error': 'Invalid stream URL scheme'}), 400
        _dl_domain = _dl_parsed.netloc.lower().split(':')[0]
        if not _is_allowed_domain(_dl_domain):
            return jsonify({'error': 'Stream domain not allowed'}), 403
    except Exception:
        return jsonify({'error': 'Invalid stream URL'}), 400

    try:
        clean_name = re.sub(r'[/\\?%*:|"<>]', '-', filename_base)
        upstream   = requests.get(stream_url,
                                  headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'},
                                  stream=True, timeout=(10, None), allow_redirects=True)
        if not upstream.ok: return jsonify({'error': f'Upstream {upstream.status_code}'}), 502
        actual_ct  = upstream.headers.get('Content-Type', content_type)
        ext        = 'webm' if 'webm' in actual_ct else ('m4a' if ('mp4' in actual_ct or 'm4a' in actual_ct) else 'mp3')
        resp_headers = {
            'Content-Disposition': f'attachment; filename="{clean_name}.{ext}"',
            'Content-Type':        actual_ct,
            'Accept-Ranges':       'bytes',
            'Access-Control-Allow-Origin': '*',
        }
        if 'Content-Length' in upstream.headers:
            resp_headers['Content-Length'] = upstream.headers['Content-Length']

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally: upstream.close()

        return Response(stream_with_context(generate()), status=200, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# /api/godmode/status
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/godmode/status')
@limiter.limit("10 per minute")
def godmode_status():
    secret = request.headers.get('X-Admin-Key', '')
    if not secret or not hmac.compare_digest(secret, ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({
        'circuit_breakers':     _cb.status(),
        'source_performance':   _src_perf.status(),
        'conf_tuner_floors':    _conf_tuner.status(),
        'url_refresh_pending':  list(_url_refresh_queue),
        'mirror_count': len(SAAVN_MIRRORS),
        'piped_count':  len(PIPED_INSTANCES),
        'inv_count':    len(INVIDIOUS_INSTANCES),
        'cache_sizes': {
            'l1_saavn':    _l1_saavn.size(),
            'l1_audio':    _l1_audio.size(),
            'l1_artwork':  _l1_artwork.size(),
            'l1_verified': _l1_verified.size(),
            'l1_meta':     _l1_meta.size(),
        },
        'timestamp': round(time.time()),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# /api/health
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/health')
@limiter.limit("30 per minute")
def health_status():
    with _mirror_lock:    saavn_list = list(SAAVN_MIRRORS)
    with _piped_lock:     piped_list = list(PIPED_INSTANCES)
    with _invidious_lock: inv_list   = list(INVIDIOUS_INSTANCES)

    def summarize(urls):
        return sorted(
            [_health.summary(u) for u in urls],
            key=lambda x: x['reputation'], reverse=True,
        )

    with _sc_client_id_lock:
        sc_id = SOUNDCLOUD_CLIENT_ID

    return jsonify({
        'saavn':      {'count': len(saavn_list),  'instances': summarize(saavn_list)},
        'piped':      {'count': len(piped_list),  'instances': summarize(piped_list)},
        'invidious':  {'count': len(inv_list),    'instances': summarize(inv_list)},
        'soundcloud': {'client_id_prefix': sc_id[:8] + '...' if sc_id else 'missing'},
        'cache': {
            'l1_meta_size':     _l1_meta.size(),
            'l1_audio_size':    _l1_audio.size(),
            'l1_popular_size':  _l1_popular.size(),
            'l1_saavn_size':    _l1_saavn.size(),
            'l1_artwork_size':  _l1_artwork.size(),
            'l1_verified_size': _l1_verified.size(),
        },
        'circuit_breakers':       _cb.status(),
        'source_performance':     _src_perf.status(),
        'conf_tuner_floors':      _conf_tuner.status(),
        'url_refresh_queue_size': len(_url_refresh_queue),
        'timestamp': round(time.time()),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/auth/google', methods=['POST'])
@limiter.limit("20 per minute")
def handle_google_auth():
    data       = request.get_json() or {}
    credential = data.get('credential', '').strip()
    if not credential: return jsonify({'error': 'Missing credential'}), 400
    profile = _verify_google_jwt(credential)
    if not profile: return jsonify({'error': 'Invalid credential'}), 401
    sub = profile.get('sub', '').strip()
    if not sub: return jsonify({'error': 'Missing sub'}), 400
    sb_upsert('users', {
        'google_sub': sub,
        'name':       profile.get('name', ''),
        'email':      profile.get('email', ''),
        'picture':    profile.get('picture', ''),
    }, on_conflict='google_sub')
    return jsonify({
        'success': True, 'sub': sub,
        'name':    profile.get('name', ''),
        'email':   profile.get('email', ''),
        'picture': profile.get('picture', ''),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/sync/state', methods=['POST'])
@limiter.limit("60 per minute")
def save_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'error': 'Unauthorized'}), 401
    data    = request.get_json() or {}
    song_id = (data.get('songId') or '').strip()
    try: progress = max(0.0, min(float(data.get('progress', 0)), 3600.0))
    except: progress = 0.0
    device = data.get('device', 'mobile')
    if device not in ('mobile', 'tv'): device = 'mobile'
    if not song_id: return jsonify({'status': 'ignored'}), 200
    raw_art_url = str(data.get('artUrl', '') or '').strip()
    _ART_ALLOWED = (
        'saavncdn.com', 'cf.saavncdn.com', 'c.saavncdn.com',
        'aac.saavncdn.com', 'static.saavncdn.com', 'h.saavncdn.com',
        'i.scdn.co', 'img.youtube.com', 'i.ytimg.com',
        'i1.sndcdn.com', 'i2.sndcdn.com', 'cf-media.sndcdn.com',
        'is1-ssl.mzstatic.com', 'is2-ssl.mzstatic.com',
        'is3-ssl.mzstatic.com', 'is4-ssl.mzstatic.com', 'is5-ssl.mzstatic.com',
    )
    art_url = ''
    if raw_art_url.startswith('https://'):
        try:
            _art_domain = urlparse(raw_art_url).netloc.lower().split(':')[0]
            if any(_art_domain == d or _art_domain.endswith('.' + d) for d in _ART_ALLOWED):
                art_url = raw_art_url
        except Exception: pass
    sb_upsert('playback_state', {
        'google_sub': sub, 'song_id': song_id[:100],
        'song_title': str(data.get('songTitle', '') or '')[:200],
        'artist':     str(data.get('artist', '') or '')[:100],
        'art_url':    art_url, 'progress': progress,
        'device':     device, 'updated_at': datetime.utcnow().isoformat(),
    }, on_conflict='google_sub')
    return jsonify({'status': 'ok'})

@app.route('/api/sync/state', methods=['GET'])
@limiter.limit("60 per minute")
def get_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'error': 'Unauthorized'}), 401
    rows = sb_select('playback_state', {'google_sub': sub})
    if rows:
        r = rows[0]
        return jsonify({
            'success': True, 'songId': r.get('song_id'),
            'songTitle': r.get('song_title'), 'artist': r.get('artist'),
            'artUrl': r.get('art_url'), 'progress': r.get('progress'),
            'device': r.get('device'), 'updatedAt': r.get('updated_at'),
        })
    return jsonify({'success': False})


# ═══════════════════════════════════════════════════════════════════════════════
# TV PAIRING
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/auth/tv-generate-code', methods=['POST'])
@limiter.limit("10 per minute")
def generate_tv_code():
    data       = request.get_json() or {}
    session_id = data.get('sessionId') or secrets.token_hex(8)
    code       = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    expiry     = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
    sb_delete('tv_pairing', {'tv_session_id': session_id})
    sb_upsert('tv_pairing', {
        'pairing_code': code, 'tv_session_id': session_id, 'expires_at': expiry,
    }, on_conflict='pairing_code')
    return jsonify({'code': code, 'sessionId': session_id, 'expiresIn': 300})

@app.route('/api/auth/tv-poll')
@limiter.limit("60 per minute")
def poll_tv_pairing():
    code    = request.args.get('code', '').strip().upper()
    now_str = datetime.utcnow().isoformat()
    if not code: return jsonify({'status': 'pending'}), 400
    url = (f"{SUPABASE_URL}/rest/v1/tv_pairing"
           f"?pairing_code=eq.{quote(code, safe='')}"
           f"&expires_at=gt.{quote(now_str, safe='')}")
    try:
        r    = requests.get(url, headers=_sb_headers(), timeout=10)
        rows = r.json() if r.status_code == 200 else []
    except Exception: rows = []
    if not rows: return jsonify({'status': 'expired'})
    row = rows[0]
    if row.get('google_sub'):
        user_rows = sb_select('users', {'google_sub': row['google_sub']})
        sb_delete('tv_pairing', {'pairing_code': code})
        if user_rows:
            user = user_rows[0]
            return jsonify({'status': 'authorized', 'user': {
                'sub': user['google_sub'], 'name': user['name'],
                'email': user['email'], 'picture': user['picture'],
            }})
    return jsonify({'status': 'pending'})

@app.route('/api/auth/tv-verify-mobile', methods=['POST'])
@limiter.limit("20 per minute")
def mobile_verify_tv():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data    = request.get_json() or {}
    code    = data.get('code', '').strip().upper()
    now_str = datetime.utcnow().isoformat()
    if not code: return jsonify({'success': False, 'error': 'Missing code'}), 400
    url = (f"{SUPABASE_URL}/rest/v1/tv_pairing"
           f"?pairing_code=eq.{quote(code, safe='')}"
           f"&expires_at=gt.{quote(now_str, safe='')}")
    try:
        r    = requests.get(url, headers=_sb_headers(), timeout=10)
        rows = r.json() if r.status_code == 200 else []
    except Exception: rows = []
    if not rows: return jsonify({'success': False, 'error': 'Invalid or expired code'}), 404
    sb_update('tv_pairing', {'google_sub': sub}, {'pairing_code': code})
    return jsonify({'success': True})


# ═══════════════════════════════════════════════════════════════════════════════
# GHOST PIN
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/auth/verify-ghost-pin', methods=['POST'])
@limiter.limit("10 per minute")
def verify_ghost_pin():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    pin  = data.get('pin', '').strip()
    if not pin: return jsonify({'success': False}), 400
    h_input = hashlib.pbkdf2_hmac(
        'sha256', pin.encode('utf-8'), sub.encode('utf-8'), iterations=300_000
    ).hex()
    rows = sb_select('users', {'google_sub': sub}, columns='ghost_pin_hash')
    if not rows: return jsonify({'success': False}), 404
    stored_hash = rows[0].get('ghost_pin_hash')
    if not stored_hash:
        sb_update('users', {'ghost_pin_hash': h_input}, {'google_sub': sub})
        return jsonify({'success': True})
    if hmac.compare_digest(stored_hash, h_input):
        return jsonify({'success': True})
    return jsonify({'success': False})


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/admin/users')
@limiter.limit("10 per minute")
def admin_users():
    secret = request.headers.get('X-Admin-Key', '')
    if not secret or not hmac.compare_digest(secret, ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    rows = sb_select('users', columns='name,email,picture,created_at')
    return jsonify({'users': rows, 'total': len(rows)})


# ═══════════════════════════════════════════════════════════════════════════════
# ARTWORK PROXY
# ═══════════════════════════════════════════════════════════════════════════════
_ARTWORK_ALLOWED_DOMAINS = [
    'saavncdn.com', 'cf.saavncdn.com', 'c.saavncdn.com', 'aac.saavncdn.com',
    'static.saavncdn.com', 'h.saavncdn.com',
    'is1-ssl.mzstatic.com', 'is2-ssl.mzstatic.com', 'is3-ssl.mzstatic.com',
    'is4-ssl.mzstatic.com', 'is5-ssl.mzstatic.com',
    'a1.mzstatic.com', 'a2.mzstatic.com', 'a3.mzstatic.com',
    'a4.mzstatic.com', 'a5.mzstatic.com', 'mzstatic.com',
    'i.scdn.co', 'img.youtube.com', 'i.ytimg.com',
    'cf-media.sndcdn.com', 'i1.sndcdn.com', 'i2.sndcdn.com',
]

@app.route('/api/artwork')
@limiter.limit("300 per minute")
def artwork_proxy():
    url = request.args.get('url', '').strip()
    if not url: return jsonify({'error': 'Missing url'}), 400
    if 'img.youtube.com' in url or 'i.ytimg.com' in url:
        url = re.sub(r'/(default|mqdefault|sddefault|hqdefault)\.jpg', '/maxresdefault.jpg', url)
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'): return jsonify({'error': 'Invalid scheme'}), 400
        domain  = parsed.netloc.lower().split(':')[0]
        allowed = any(domain == d or domain.endswith('.' + d) for d in _ARTWORK_ALLOWED_DOMAINS)
        if not allowed: return jsonify({'error': 'Domain not allowed'}), 403
    except Exception:
        return jsonify({'error': 'Invalid URL'}), 400
    try:
        r = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'}, stream=True)
        if not r.ok: return jsonify({'error': f'Upstream {r.status_code}'}), 502
        content_type = r.headers.get('Content-Type', 'image/jpeg')
        def generate():
            try:
                for chunk in r.iter_content(chunk_size=32768):
                    if chunk: yield chunk
            finally: r.close()
        return Response(stream_with_context(generate()), status=200,
                        content_type=content_type,
                        headers={
                            'Access-Control-Allow-Origin': '*',
                            'Cache-Control': 'public, max-age=604800, immutable',
                        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# /api/suggest
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/suggest')
@limiter.limit("120 per minute")
def get_suggestions():
    q = request.args.get('q', '').strip()[:100]
    if not q or len(q) < 2:
        return jsonify({'suggestions': []})
    cache_key = f"suggest:{q.lower()}"
    cached = _l1_meta.get(cache_key)
    if cached is not None:
        return jsonify({'suggestions': cached})
    try:
        r = _http_session.get(
            'https://itunes.apple.com/search',
            params={'term': q, 'media': 'music', 'entity': 'song',
                    'limit': 8, 'country': 'IN'},
            timeout=4
        )
        r.raise_for_status()
        results = r.json().get('results', [])
        suggestions = []
        for s in results:
            if not s.get('trackName'): continue
            raw_art = s.get('artworkUrl100') or ''
            art_url = _ensure_500(
                raw_art.replace('100x100bb', '500x500bb').replace('100x100', '500x500')
            ) if raw_art else ''
            suggestions.append({
                'trackName':  s.get('trackName', ''),
                'artistName': s.get('artistName', ''),
                'artworkUrl': art_url,
                'trackId':    s.get('trackId'),
            })
        suggestions = suggestions[:6]
        _l1_meta.set(cache_key, suggestions)
        return jsonify({'suggestions': suggestions})
    except Exception:
        return jsonify({'suggestions': []})


# ═══════════════════════════════════════════════════════════════════════════════
# /health
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/health')
def health():
    return jsonify({
        'status':  'ok',
        'sources': ['saavn', 'jiosavan', 'ytmusic', 'piped', 'invidious', 'soundcloud', 'youtube'],
        'auth':    'google-oauth',
        'db':      'supabase',
        'version': '3.3',
    })


# ── Startup warmup ────────────────────────────────────────────────────────────
def _startup_warmup():
    try:
        _http_session.get(
            'https://itunes.apple.com/search',
            params={'term': 'arijit singh', 'media': 'music', 'entity': 'song', 'limit': 1},
            timeout=10
        )
        log.info('[Warmup] iTunes connection established')
    except Exception as e:
        log.warning(f'[Warmup] iTunes failed: {e}')
    try:
        _fetch_saavn_search_parallel('arijit singh')
        log.info('[Warmup] Saavn connection established')
    except Exception as e:
        log.warning(f'[Warmup] Saavn failed: {e}')

if os.environ.get('SERVER_SOFTWARE', '').startswith('gunicorn') or __name__ == '__main__':
    _warmup_timer = threading.Timer(3.0, _startup_warmup)
    _warmup_timer.daemon = True
    _warmup_timer.start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
