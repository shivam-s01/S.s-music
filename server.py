from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS
import os
import logging
import threading
import time

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__, static_folder='static', static_url_path='')
CORS(app, resources={r"/*": {"origins": "*"}})

# Import core modules
from core import (
    BASE_DIR, SUPABASE_URL, SUPABASE_KEY, GOOGLE_CLIENT_ID, ADMIN_KEY,
    log as core_log, _executor, _executor_bg, _executor_cache,
    _l1_meta, _l1_audio, _l1_saavn, _l1_popular, _l1_artwork, _l1_verified,
    _LRUCache, limiter, get_real_ip, _cb, _src_perf, _conf_tuner,
    sb_select, sb_upsert, sb_delete, sb_update, init_db,
    normalize, compute_confidence, _is_confirmed_match, dna_compatible,
    _query_requests_version, _is_remix_or_cover, _is_live_version, _is_slowed_reverb,
    _get_artwork, _store_artwork, _store_verified, _get_verified,
    build_query_variants, pick_image, pick_best_quality,
    _fix_image_url, _ensure_500,
    tve_match_get, tve_match_get_verified, tve_match_set, tve_match_invalidate,
    store_saavn_anchor, get_saavn_anchor,
    _CACHE_MIN_CONFIDENCE, _VOLATILE_SOURCES,
    _supabase_cache_set, _supabase_cache_get_with_refresh,
    _song_index_get, _song_index_put,
)

# ============================================================
# STATIC FILE ROUTES
# ============================================================

@app.route('/')
def index():
    """Serve main index page"""
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/<path:filename>')
def serve_static(filename):
    """Serve static files"""
    file_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(file_path):
        return send_file(file_path)
    return jsonify({'error': 'Not found'}), 404

@app.route('/manifest.json')
def manifest():
    """Serve manifest.json for PWA"""
    resp = send_file(os.path.join(BASE_DIR, 'manifest.json'), mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp

@app.route('/sw.js')
def service_worker():
    """Serve service worker"""
    resp = send_file(os.path.join(BASE_DIR, 'sw.js'), mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp

@app.route('/.well-known/assetlinks.json')
def assetlinks():
    """Serve assetlinks for Android app linking"""
    return send_file(os.path.join(BASE_DIR, 'assetlinks.json'))

# ============================================================
# SEARCH ROUTE
# ============================================================

@app.route('/api/search')
@limiter.limit("100 per minute")
def search_songs():
    """Search songs from multiple sources"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'results': [], 'error': 'No query provided'}), 400
    
    try:
        # Try to import fetchers functions (import here to avoid circular import)
        from fetchers import (
            _normalize_saavn_songs, _fetch_saavn_search_parallel,
            _resolve_itunes_to_saavn, _fetch_itunes_artwork
        )
        
        # Search Saavn
        raw_results = _fetch_saavn_search_parallel(query)
        results = _normalize_saavn_songs(raw_results, query)
        
        # Also search iTunes for additional results (max 5)
        itunes_results = []
        try:
            import requests
            import re
            itunes_url = 'https://itunes.apple.com/search'
            itunes_params = {
                'term': query,
                'media': 'music',
                'entity': 'song',
                'limit': 5,
                'country': 'IN'
            }
            itunes_resp = requests.get(itunes_url, params=itunes_params, timeout=5)
            if itunes_resp.status_code == 200:
                itunes_data = itunes_resp.json()
                for item in itunes_data.get('results', []):
                    # Resolve iTunes to Saavn
                    resolved = _resolve_itunes_to_saavn(item)
                    if resolved and resolved.get('_saavnId'):
                        itunes_results.append(resolved)
        except Exception as e:
            log.debug(f"[Search] iTunes error: {e}")
        
        # Combine results (Saavn first, then iTunes)
        all_results = results + itunes_results
        
        # Remove duplicates
        seen_ids = set()
        unique_results = []
        for song in all_results:
            song_id = song.get('trackId') or song.get('_saavnId') or song.get('id', '')
            if song_id and song_id not in seen_ids:
                seen_ids.add(song_id)
                unique_results.append(song)
            elif not song_id and song.get('trackName') not in [s.get('trackName') for s in unique_results]:
                unique_results.append(song)
        
        return jsonify({
            'results': unique_results[:50],
            'count': len(unique_results),
            'query': query
        })
        
    except Exception as e:
        log.error(f"[Search] Error: {e}")
        return jsonify({'results': [], 'error': str(e)}), 500

# ============================================================
# SONG DETAILS ROUTE
# ============================================================

@app.route('/api/song')
@limiter.limit("200 per minute")
def song_details():
    """Get detailed song information"""
    song_id = request.args.get('id', '').strip()
    title = request.args.get('title', '').strip()
    artist = request.args.get('artist', '').strip()
    
    if not song_id and not title:
        return jsonify({'error': 'Missing id or title'}), 400
    
    try:
        from fetchers import _fetch_saavn_by_id, fetch_saavn_parallel, _fix_image_url
        
        result = None
        
        # Try by ID first
        if song_id:
            result = _fetch_saavn_by_id(song_id, title, artist)
        
        # Then by search
        if not result and title:
            for query_var in build_query_variants(title, artist, '')[:3]:
                result = fetch_saavn_parallel(query_var, title=title, artist=artist)
                if result:
                    break
        
        if result and result.get('url'):
            # Format response
            image = result.get('image', '')
            if image:
                image = _fix_image_url(image)
            
            return jsonify({
                'trackId': song_id or result.get('id', ''),
                'trackName': result.get('title', title),
                'artistName': result.get('artist', artist),
                'artworkUrl100': image,
                'previewUrl': f"/api/play?id={song_id or result.get('id', '')}&title={result.get('title', title)}&artist={result.get('artist', artist)}",
                '_saavnId': song_id or result.get('id', ''),
                '_quality': result.get('quality', 'unknown'),
                '_source': result.get('source', 'saavn')
            })
        
        return jsonify({'error': 'Song not found'}), 404
        
    except Exception as e:
        log.error(f"[Song] Error: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================
# /api/play - COMPLETE PLAYBACK ROUTE (FULLY FUNCTIONAL)
# ============================================================

@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    """
    Play song endpoint - returns full audio stream
    Handles corrupted parameters, multi-source fallback
    """
    
    # Get parameters
    raw_id = request.args.get('id', '').strip()
    raw_title = request.args.get('title', '').strip()
    raw_artist = request.args.get('artist', '').strip()
    
    log.info(f"[Play] Request - id: '{raw_id[:50]}', title: '{raw_title[:50]}', artist: '{raw_artist[:50]}'")
    
    # ========== CLEAN CORRUPTED PARAMETERS ==========
    
    # Fix: id contains 'title=...' pattern
    if raw_id and 'title=' in raw_id:
        import re
        match = re.search(r'title=([^&]+)', raw_id)
        if match:
            from urllib.parse import unquote
            extracted = unquote(match.group(1))
            if not raw_title:
                raw_title = extracted
                log.info(f"[Play] Extracted title from corrupted id: {extracted[:50]}")
        raw_id = ''
    
    # Fix: id contains spaces (looks like title)
    if raw_id and (' ' in raw_id or '%20' in raw_id):
        if not raw_title:
            raw_title = raw_id
            log.info(f"[Play] Using id as title: {raw_title[:50]}")
        raw_id = ''
    
    # URL decode
    from urllib.parse import unquote
    try:
        if raw_title:
            raw_title = unquote(raw_title)
        if raw_artist:
            raw_artist = unquote(raw_artist)
        if raw_id:
            raw_id = unquote(raw_id)
    except:
        pass
    
    # Remove 'title=' prefix if present
    if raw_title and raw_title.startswith('title='):
        raw_title = raw_title[6:]
    
    song_id = raw_id[:100] if raw_id else ''
    title = raw_title[:200] if raw_title else ''
    artist = raw_artist[:100] if raw_artist else ''
    
    # Validate
    if not song_id and not title:
        log.error(f"[Play] Missing both id and title")
        return jsonify({'error': 'Missing id or title'}), 400
    
    log.info(f"[Play] Cleaned - id: '{song_id}', title: '{title}', artist: '{artist}'")
    
    # ========== CHECK CACHE ==========
    audio_url = None
    quality = 'unknown'
    source = 'unknown'
    confidence = 0.0
    result_title = title
    result_artist = artist
    result_image = ''
    result_saavn_id = song_id
    
    cache_key = f"play:{song_id or normalize(title)}:{normalize(artist)}"
    cache_key_id = f"play:{song_id}:{normalize(artist)}" if song_id else None
    cache_key_title = f"play:{normalize(title)}:{normalize(artist)}" if title else None
    
    def check_cache(entry):
        if not entry or not entry.get('url'):
            return None
        if not _query_requests_version(title or ''):
            if (_is_remix_or_cover(entry.get('title', '')) or
                _is_slowed_reverb(entry.get('title', '')) or
                _is_live_version(entry.get('title', ''))):
                return None
        return entry
    
    # Check L1 cache
    for ck in [cache_key_id, cache_key_title, cache_key]:
        if ck:
            cached = _l1_saavn.get(ck)
            if check_cache(cached):
                audio_url = cached['url']
                quality = cached.get('quality', 'unknown')
                source = cached.get('source', 'unknown')
                confidence = float(cached.get('confidence', cached.get('_confidence', 0.85)))
                result_title = cached.get('title', title)
                result_artist = cached.get('artist', artist)
                result_image = cached.get('image', '')
                result_saavn_id = cached.get('_saavnId', song_id)
                log.info(f"[Play] Cache L1 hit: {source}")
                break
    
    # ========== FETCH FROM SAAVN ==========
    if not audio_url:
        try:
            from fetchers import _fetch_saavn_by_id, fetch_saavn_parallel, _fix_image_url
            
            # Try by ID
            if song_id:
                import re
                if re.match(r'^[a-zA-Z0-9_-]{5,50}$', song_id):
                    result = _fetch_saavn_by_id(song_id, title, artist)
                    if result and result.get('url'):
                        audio_url = result['url']
                        quality = result.get('quality', 'unknown')
                        source = 'saavn'
                        confidence = float(result.get('_confidence', 0.92))
                        result_title = result.get('title', title)
                        result_artist = result.get('artist', artist)
                        result_image = result.get('image', '')
                        result_saavn_id = song_id
                        log.info(f"[Play] Saavn ID success: {result_title}")
            
            # Try search
            if not audio_url and title:
                for query_var in build_query_variants(title, artist, '')[:3]:
                    result = fetch_saavn_parallel(query_var, title=title, artist=artist)
                    if result and result.get('url'):
                        conf = float(result.get('_confidence', result.get('score', 0)))
                        if conf >= 0.65:
                            audio_url = result['url']
                            quality = result.get('quality', 'unknown')
                            source = 'saavn'
                            confidence = conf
                            result_title = result.get('title', title)
                            result_artist = result.get('artist', artist)
                            result_image = result.get('image', '')
                            log.info(f"[Play] Saavn search success: {result_title} conf={conf:.2f}")
                            break
        except Exception as e:
            log.error(f"[Play] Saavn fetch error: {e}")
    
    # ========== FALLBACK: JIOSAVAN ==========
    if not audio_url and title:
        try:
            from fetchers import fetch_from_jiosavan
            result = fetch_from_jiosavan(title, artist)
            if result and result.get('url'):
                conf = float(result.get('_confidence', 0.7))
                if conf >= 0.60:
                    audio_url = result['url']
                    quality = result.get('quality', 'unknown')
                    source = 'jiosavan'
                    confidence = conf
                    result_title = result.get('title', title)
                    result_artist = result.get('artist', artist)
                    result_image = result.get('image', '')
                    log.info(f"[Play] JioSavan success: {result_title}")
        except Exception as e:
            log.error(f"[Play] JioSavan error: {e}")
    
    # ========== FALLBACK: YOUTUBE MUSIC ==========
    if not audio_url and title:
        try:
            from fetchers import fetch_from_ytmusic
            result = fetch_from_ytmusic(title, artist)
            if result and result.get('url'):
                conf = float(result.get('_confidence', 0.7))
                if conf >= 0.55:
                    audio_url = result['url']
                    quality = result.get('quality', 'unknown')
                    source = 'ytmusic'
                    confidence = conf
                    result_title = result.get('title', title)
                    result_artist = result.get('artist', artist)
                    result_image = result.get('image', '')
                    log.info(f"[Play] YTMusic success: {result_title}")
        except Exception as e:
            log.error(f"[Play] YTMusic error: {e}")
    
    # ========== FALLBACK: YT-DLP ==========
    if not audio_url and title:
        try:
            from fetchers import fetch_from_ytdlp
            result = fetch_from_ytdlp(title, artist)
            if result and result.get('url'):
                conf = float(result.get('_confidence', 0.65))
                if conf >= 0.55:
                    audio_url = result['url']
                    quality = result.get('quality', 'unknown')
                    source = 'youtube'
                    confidence = conf
                    result_title = result.get('title', title)
                    result_artist = result.get('artist', artist)
                    result_image = result.get('image', '')
                    log.info(f"[Play] yt-dlp success: {result_title}")
        except Exception as e:
            log.error(f"[Play] yt-dlp error: {e}")
    
    # ========== FALLBACK: SOUNDCLOUD ==========
    if not audio_url and title:
        try:
            from fetchers import fetch_from_soundcloud
            result = fetch_from_soundcloud(title, artist)
            if result and result.get('url'):
                conf = float(result.get('_confidence', 0.6))
                if conf >= 0.55:
                    audio_url = result['url']
                    quality = result.get('quality', 'unknown')
                    source = 'soundcloud'
                    confidence = conf
                    result_title = result.get('title', title)
                    result_artist = result.get('artist', artist)
                    result_image = result.get('image', '')
                    log.info(f"[Play] SoundCloud success: {result_title}")
        except Exception as e:
            log.error(f"[Play] SoundCloud error: {e}")
    
    # ========== FINAL VALIDATION ==========
    if not audio_url:
        log.warning(f"[Play] All sources failed for: {title}")
        return jsonify({'error': 'No audio source found', 'title': title}), 404
    
    # Confidence check
    min_confidence = 0.55 if source in ('saavn', 'jiosavan') else 0.50
    if confidence < min_confidence:
        log.warning(f"[Play] Low confidence: {confidence:.3f} < {min_confidence}")
        return jsonify({'error': 'Low confidence match', 'confidence': confidence}), 404
    
    # DNA check
    if title and result_title:
        if not dna_compatible(title, result_title):
            log.warning(f"[Play] DNA mismatch: '{title}' vs '{result_title}'")
            return jsonify({'error': 'Song DNA mismatch'}), 404
    
    # ========== GET ARTWORK ==========
    final_image = result_image
    if not final_image:
        final_image = _get_artwork(result_title, result_artist)
    if not final_image and title:
        try:
            from fetchers import _fetch_itunes_artwork
            final_image = _fetch_itunes_artwork(result_title, result_artist)
        except:
            pass
    
    if final_image:
        final_image = _fix_image_url(final_image) if '_fix_image_url' in dir() else final_image
    
    # ========== CACHE RESULT ==========
    cache_payload = {
        'url': audio_url,
        'quality': quality,
        'source': source,
        'title': result_title,
        'artist': result_artist,
        'image': final_image,
        'confidence': round(confidence, 3),
        '_saavnId': result_saavn_id or song_id,
    }
    
    if confidence >= 0.70:
        for ck in [cache_key_id, cache_key_title, cache_key]:
            if ck:
                _l1_saavn.set(ck, cache_payload)
        try:
            _executor_cache.submit(_supabase_cache_set, cache_key, cache_payload, confidence)
        except:
            pass
    
    if final_image and result_title:
        _store_artwork(result_title, result_artist, final_image, 1)
    
    # ========== STREAM AUDIO ==========
    response_headers = {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Expose-Headers': 'Content-Length, Content-Range, X-Audio-Quality, X-Audio-Source, X-Confidence, X-Artwork-URL, X-Song-Title, X-Song-Artist, X-Track-Id, X-Track-Name, X-Artist-Name',
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'no-store',
        'X-Audio-Quality': quality,
        'X-Audio-Source': source,
        'X-Confidence': str(round(confidence, 3)),
        'X-Artwork-URL': final_image or '',
        'X-Song-Title': result_title[:200],
        'X-Song-Artist': result_artist[:100],
        'X-Track-Id': result_saavn_id or song_id or '',
        'X-Track-Name': result_title[:200],
        'X-Artist-Name': result_artist[:100],
    }
    
    try:
        req_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive',
        }
        
        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header
        
        upstream = requests.get(audio_url, headers=req_headers, stream=True,
                                timeout=(10, None), allow_redirects=True)
        
        # Copy response headers
        excluded = {'content-encoding', 'transfer-encoding', 'connection', 'content-length'}
        for k, v in upstream.headers.items():
            if k.lower() not in excluded:
                response_headers[k] = v
        
        if 'content-type' not in {k.lower() for k in response_headers}:
            response_headers['Content-Type'] = 'audio/mpeg'
        
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=32768):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
        
        return Response(stream_with_context(generate()),
                       status=upstream.status_code,
                       headers=response_headers,
                       direct_passthrough=True)
                       
    except Exception as e:
        log.error(f"[Play] Stream error: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================================
# PREFETCH ROUTE
# ============================================================

@app.route('/api/prefetch', methods=['POST'])
@limiter.limit("60 per minute")
def prefetch_songs():
    """Prefetch songs to cache"""
    songs = request.get_json(silent=True) or {}
    queue = songs.get('songs', [])[:5]
    
    if not queue:
        return jsonify({'status': 'empty'})
    
    def do_prefetch(s):
        try:
            from fetchers import _fetch_saavn_by_id, fetch_saavn_parallel
            
            song_id = str(s.get('id', '')).strip()[:100]
            song_title = str(s.get('title', '')).strip()[:200]
            song_artist = str(s.get('artist', '')).strip()[:100]
            
            if not song_id and not song_title:
                return
            
            cache_key = f"play:{song_id or normalize(song_title)}:{normalize(song_artist)}"
            
            if _l1_saavn.get(cache_key):
                return
            
            if song_id:
                result = _fetch_saavn_by_id(song_id, song_title, song_artist)
                if result and result.get('url'):
                    _l1_saavn.set(cache_key, {
                        **result,
                        'source': 'saavn',
                        'confidence': 0.95,
                        'title': song_title or result.get('title', ''),
                        'artist': song_artist or result.get('artist', ''),
                    })
                    return
            
            if song_title:
                for qv in build_query_variants(song_title, song_artist, '')[:2]:
                    result = fetch_saavn_parallel(qv, title=song_title, artist=song_artist)
                    if result and result.get('url'):
                        _l1_saavn.set(cache_key, result)
                        return
        except Exception as e:
            log.debug(f"[Prefetch] Error: {e}")
    
    for song in queue:
        _executor_bg.submit(do_prefetch, song)
    
    return jsonify({'status': 'prefetching', 'count': len(queue)})

# ============================================================
# HEALTH CHECK ROUTE
# ============================================================

@app.route('/api/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'timestamp': time.time(),
        'cache_size': _l1_saavn.size() if hasattr(_l1_saavn, 'size') else 0
    })

# ============================================================
# CORS OPTIONS HANDLER
# ============================================================

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', '*')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return Response(status=200)

# ============================================================
# MAIN ENTRY POINT
# ============================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    log.info(f"Starting server on port {port}")
    log.info(f"Debug mode: {debug}")
    
    app.run(host='0.0.0.0', port=port, debug=debug, threaded=True)
