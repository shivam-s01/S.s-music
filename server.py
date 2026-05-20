from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import os
import re
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════
# APP INIT
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

def get_real_ip():
    return (
        request.headers.get('CF-Connecting-IP') or
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.remote_addr or '127.0.0.1'
    )

limiter = Limiter(get_real_ip, app=app, default_limits=[], storage_uri="memory://")

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
SAAVN_MIRRORS = [
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
]

PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.tokhmi.xyz',
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
]

INVIDIOUS_INSTANCES = [
    'https://invidious.snopyta.org',
    'https://vid.puffyan.us',
    'https://invidious.kavin.rocks',
    'https://y.com.sb',
]

ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'cf.saavncdn.com', 'aac.saavncdn.com', 'static.saavncdn.com',
    'c.saavncdn.com', 'h.saavncdn.com',
    # YouTube/Piped CDN domains
    'googlevideo.com', 'youtube.com', 'ytimg.com',
    'rr1.sn-', 'rr2.sn-', 'rr3.sn-', 'rr4.sn-',
    'r1.sn-', 'r2.sn-', 'r3.sn-', 'r4.sn-',
    'r5.sn-', 'r6.sn-', 'r7.sn-',
]

QUALITY_RANK = {
    '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
    '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
}

NINETIES_SEEDS = [
    "Kumar Sanu hits", "Udit Narayan 90s", "Alka Yagnik 90s",
    "Lata Mangeshkar 90s", "Sonu Nigam 90s hits",
    "Kavita Krishnamurthy songs", "Asha Bhosle 90s",
    "Abhijeet Bhattacharya hits", "Shankar Mahadevan 90s",
    "AR Rahman 90s", "Anu Malik 90s hits",
    "Nadeem Shravan songs", "Jatin Lalit songs",
    "Kumar Sanu Alka Yagnik duets", "90s Bollywood superhits",
]

NINETIES_TRIGGERS = [
    '90', 'purane', 'purana', 'purani', 'old', 'retro',
    'classic', 'nineties', 'throwback', 'evergreen', 'gaane',
]

_executor = ThreadPoolExecutor(max_workers=8)

# ═══════════════════════════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════════════════════════
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin']   = '*'
    resp.headers['Access-Control-Allow-Methods']  = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers']  = '*'
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
    return resp

@app.after_request
def after_request(resp):
    return add_cors(resp)

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return add_cors(Response(status=200))

# ═══════════════════════════════════════════════════════════════
# FRONTEND ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/manifest.json')
def manifest():
    resp = send_file(os.path.join(BASE_DIR, 'manifest.json'), mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp

@app.route('/sw.js')
def service_worker():
    resp = send_file(os.path.join(BASE_DIR, 'sw.js'), mimetype='application/javascript')
    resp.headers['Cache-Control']          = 'no-cache, no-store, must-revalidate'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp

@app.route('/.well-known/assetlinks.json')
def assetlinks():
    return app.send_static_file('assetlinks.json')

@app.route('/<path:filename>')
def serve_static(filename):
    file_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(file_path):
        return send_file(file_path)
    return jsonify({'error': 'Not found'}), 404

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
def clean_query(text):
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(\s*(OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?|Hindi|English|Japanese|Bhojpuri|Version|Remix|Cover|HD|HQ|Original|Soundtrack|Motion\s*Picture|Remastered|Extended|Radio\s*Edit)\s*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*[-–]\s*(official|audio|video|lyrics|full\s*song|hd|hq|remastered).*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def build_query_variants(title, artist='', fallback=''):
    title_c  = clean_query(title)
    artist_c = clean_query(artist) if artist else ''
    fb_c     = clean_query(fallback) if fallback else ''
    artist_first = artist_c.split()[0] if artist_c else ''
    seen, variants = set(), []

    def add(v):
        v = re.sub(r'\s+', ' ', v).strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    add(title_c)
    if artist_first: add(f"{title_c} {artist_first}")
    if artist_c:     add(f"{title_c} {artist_c}")
    if fb_c and fb_c != title_c: add(fb_c)
    if artist_c and fb_c: add(f"{artist_c} {title_c}")
    return variants

def normalize(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def levenshtein(s1, s2):
    if len(s1) < len(s2): return levenshtein(s2, s1)
    if not s2: return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(c1 != c2)))
        prev = curr
    return prev[-1]

def fuzzy_word_match(qw, tw):
    if tw.startswith(qw): return 1.0
    if qw in tw: return 0.85
    max_len = max(len(qw), len(tw))
    if max_len == 0: return 0.0
    ratio = 1.0 - (levenshtein(qw, tw) / max_len)
    return ratio if ratio >= 0.60 else 0.0

def title_score(query, song_title, song_artist=''):
    q, t, a = normalize(query), normalize(song_title), normalize(song_artist)
    if not q: return 0.0
    if q == t: return 3.0
    q_words = q.split(); t_words = t.split(); a_words = a.split() if a else []
    score = 0.0
    if t.startswith(q): score += 2.0
    title_match = sum(max((fuzzy_word_match(qw, tw) for tw in t_words), default=0.0) for qw in q_words)
    if q_words: score += (title_match / len(q_words)) * 1.5
    if a_words:
        artist_match = sum(max((fuzzy_word_match(qw, aw) for aw in a_words), default=0.0) for qw in q_words)
        if q_words: score += (artist_match / len(q_words)) * 0.5
    return score

def dynamic_min_score(query):
    length = len(normalize(query).replace(' ', ''))
    if length <= 2:  return 0.20
    elif length <= 5: return 0.35
    elif length <= 10: return 0.50
    else: return 0.55

def has_word_match(query, song_title):
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words: return True
    q_main = [w for w in q_words if len(w) >= 3]
    t_main = [w for w in t_words if len(w) >= 3]
    if not q_main: return True
    if t_main and q_main[0] == t_main[0]: return True
    for qw in q_main:
        for tw in t_main:
            if fuzzy_word_match(qw, tw) >= 0.55: return True
    return False

def pick_best_quality(urls):
    if not urls: return None, None
    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK: return QUALITY_RANK[q]
        m = re.search(r'(\d+)', q)
        return int(m.group(1)) if m else 0
    for item in sorted(urls, key=rank, reverse=True):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'):
            return url, item.get('quality', 'unknown')
    return None, None

def pick_image(song):
    images = song.get('image') or []
    if isinstance(images, list) and images:
        for item in reversed(images):
            url = item.get('url') or item.get('link') or ''
            if url.startswith('http'):
                return re.sub(r'\b(50|150)x(50|150)\b', '500x500', url)
    if isinstance(images, str) and images.startswith('http'):
        return re.sub(r'\b(50|150)x(50|150)\b', '500x500', images)
    return ''

def _pick_low_quality(urls):
    if not urls: return None, None
    LOW_PREFERENCE = ['96kbps', '96', '128kbps', '128', '48kbps', '48']
    for preferred in LOW_PREFERENCE:
        for item in urls:
            q = (item.get('quality') or '').lower().strip()
            if q == preferred or preferred in q:
                url = item.get('url') or item.get('link') or ''
                if url.startswith('http'):
                    return url, item.get('quality', preferred)
    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK: return QUALITY_RANK[q]
        m = re.search(r'(\d+)', q)
        return int(m.group(1)) if m else 999
    for item in sorted(urls, key=rank):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'):
            return url, item.get('quality', 'low')
    return None, None

def _safe_year(date_str):
    try: return int((date_str or '')[:4])
    except (ValueError, TypeError): return 0

# ═══════════════════════════════════════════════════════════════
# SAAVN MIRROR FETCH
# ═══════════════════════════════════════════════════════════════
def fetch_from_mirror(mirror, query, min_score=0.4):
    endpoints = ['/api/search/songs', '/api/search', '/search/songs']
    for endpoint in endpoints:
        try:
            r = requests.get(
                f'{mirror}{endpoint}',
                params={'query': query, 'q': query, 'limit': 10},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200: continue
            data    = r.json()
            results = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or []
            )
            best_song, best_score = None, -1
            for song in results:
                song_title  = song.get('name') or song.get('title', '')
                song_artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                if not has_word_match(query, song_title): continue
                score = title_score(query, song_title, song_artist)
                if score > best_score:
                    best_score = score
                    best_song  = song
            if not best_song or best_score < min_score: continue
            raw_urls = best_song.get('downloadUrl') or best_song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            best_url, quality = pick_best_quality(raw_urls)
            if not best_url: continue
            return {
                'url':       best_url,
                'quality':   quality,
                'title':     best_song.get('name') or best_song.get('title', ''),
                'artist':    best_song.get('primaryArtists') or best_song.get('primary_artists') or '',
                'image':     pick_image(best_song),
                'score':     round(best_score, 3),
                'source':    'saavn',
                '_raw_urls': raw_urls,
            }
        except Exception as e:
            log.warning(f"[Mirror {mirror}] {endpoint} → {e}")
            continue
    return None

def fetch_saavn_parallel(query):
    threshold = dynamic_min_score(query)
    futures   = {_executor.submit(fetch_from_mirror, mirror, query, threshold): mirror for mirror in SAAVN_MIRRORS}
    all_results = []
    try:
        for future in as_completed(futures, timeout=12):
            try:
                result = future.result()
                if result: all_results.append(result)
            except Exception as e:
                log.warning(f"[Parallel] Future error: {e}")
    except Exception as e:
        log.error(f"[Parallel] Timeout: {e}")
    if not all_results: return None
    def result_rank(r):
        score   = r.get('score', 0)
        quality = r.get('quality', '')
        return score + (0.05 if '320' in str(quality) else 0)
    all_results.sort(key=result_rank, reverse=True)
    best = all_results[0]
    log.info(f"[Parallel] Best → '{best['title']}' score={best['score']} quality={best['quality']}")
    return best

# ═══════════════════════════════════════════════════════════════
# PIPED API FETCH  — YouTube full songs
# ═══════════════════════════════════════════════════════════════
def fetch_from_piped(query, title='', artist=''):
    """Search Piped API and get audio stream URL."""
    search_q = f"{title} {artist}".strip() if title else query

    for instance in PIPED_INSTANCES:
        try:
            # Step 1: Search
            r = requests.get(
                f'{instance}/search',
                params={'q': search_q, 'filter': 'music_songs'},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200:
                continue

            results = r.json().get('items', [])
            if not results:
                continue

            # Step 2: Pick best result
            best = None
            best_score = -1
            for item in results[:5]:
                if item.get('type') != 'stream':
                    continue
                item_title  = item.get('title', '')
                item_artist = item.get('uploaderName', '')
                if not has_word_match(query, item_title):
                    continue
                score = title_score(query, item_title, item_artist)
                if score > best_score:
                    best_score = score
                    best = item

            if not best or best_score < 0.3:
                continue

            video_id = best.get('url', '').replace('/watch?v=', '').strip()
            if not video_id:
                continue

            # Step 3: Get streams
            sr = requests.get(
                f'{instance}/streams/{video_id}',
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if sr.status_code != 200:
                continue

            stream_data = sr.json()
            audio_streams = stream_data.get('audioStreams', [])

            if not audio_streams:
                continue

            # Pick highest quality audio
            best_audio = None
            best_bitrate = 0
            for stream in audio_streams:
                bitrate = stream.get('bitrate', 0)
                fmt     = stream.get('format', '').lower()
                # Prefer m4a/mp4 over webm for better compatibility
                if bitrate > best_bitrate or (bitrate == best_bitrate and 'm4a' in fmt):
                    best_bitrate = bitrate
                    best_audio   = stream

            if not best_audio or not best_audio.get('url'):
                continue

            quality_label = f"{best_bitrate // 1000}kbps" if best_bitrate > 0 else 'unknown'

            log.info(f"[Piped] ✓ '{best.get('title')}' via {instance} bitrate={best_bitrate}")
            return {
                'url':     best_audio['url'],
                'quality': quality_label,
                'title':   best.get('title', title),
                'artist':  best.get('uploaderName', artist),
                'image':   best.get('thumbnail', ''),
                'score':   round(best_score, 3),
                'source':  'piped',
            }

        except Exception as e:
            log.warning(f"[Piped {instance}] {e}")
            continue

    return None

# ═══════════════════════════════════════════════════════════════
# INVIDIOUS FETCH  — YouTube fallback
# ═══════════════════════════════════════════════════════════════
def fetch_from_invidious(query, title='', artist=''):
    """Invidious fallback for YouTube audio."""
    search_q = f"{title} {artist}".strip() if title else query

    for instance in INVIDIOUS_INSTANCES:
        try:
            r = requests.get(
                f'{instance}/api/v1/search',
                params={'q': search_q, 'type': 'video', 'page': 1},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200:
                continue

            results = r.json()
            if not results:
                continue

            best = None
            best_score = -1
            for item in results[:5]:
                item_title  = item.get('title', '')
                item_author = item.get('author', '')
                if not has_word_match(query, item_title):
                    continue
                score = title_score(query, item_title, item_author)
                if score > best_score:
                    best_score = score
                    best = item

            if not best or best_score < 0.3:
                continue

            video_id = best.get('videoId', '')
            if not video_id:
                continue

            # Get video details with audio formats
            vr = requests.get(
                f'{instance}/api/v1/videos/{video_id}',
                params={'fields': 'adaptiveFormats,title,author'},
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if vr.status_code != 200:
                continue

            vdata = vr.json()
            formats = vdata.get('adaptiveFormats', [])

            # Filter audio only
            audio_formats = [f for f in formats if f.get('type', '').startswith('audio')]
            if not audio_formats:
                continue

            # Pick highest bitrate
            best_fmt = max(audio_formats, key=lambda f: f.get('bitrate', 0))
            if not best_fmt.get('url'):
                continue

            bitrate = best_fmt.get('bitrate', 0)
            quality_label = f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown'

            log.info(f"[Invidious] ✓ '{best.get('title')}' via {instance}")
            return {
                'url':     best_fmt['url'],
                'quality': quality_label,
                'title':   best.get('title', title),
                'artist':  best.get('author', artist),
                'image':   f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
                'score':   round(best_score, 3),
                'source':  'invidious',
            }

        except Exception as e:
            log.warning(f"[Invidious {instance}] {e}")
            continue

    return None

# ═══════════════════════════════════════════════════════════════
# ITUNES SEARCH
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q   = request.args.get('q', 'top songs').strip()
    era = request.args.get('era', '').strip()
    is_90s      = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q
    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={'term': search_term, 'media': 'music', 'entity': 'song', 'limit': 50, 'country': 'IN'},
            timeout=15
        )
        r.raise_for_status()
        results = r.json().get('results', [])
        if is_90s:
            filtered = [s for s in results if s.get('previewUrl') and 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
            if len(filtered) < 5:
                filtered = [s for s in results if s.get('previewUrl')]
            random.shuffle(filtered)
            return jsonify({'results': filtered[:30]})
        return jsonify({'results': [s for s in results if s.get('previewUrl')]})
    except Exception as e:
        log.error(f"[iTunes] Search failed '{search_term}': {e}")
        return jsonify({'results': [], 'error': str(e)})

@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed = random.choice(NINETIES_SEEDS)
    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={'term': seed, 'media': 'music', 'entity': 'song', 'limit': 50, 'country': 'IN'},
            timeout=15
        )
        r.raise_for_status()
        results = r.json().get('results', [])
        filtered = [s for s in results if s.get('previewUrl') and 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        if len(filtered) < 5:
            filtered = [s for s in results if s.get('previewUrl')]
        random.shuffle(filtered)
        return jsonify({'results': filtered[:30], 'seed': seed})
    except Exception as e:
        log.error(f"[iTunes/90s] Seed '{seed}' failed: {e}")
        return jsonify({'results': [], 'error': str(e)})

# ═══════════════════════════════════════════════════════════════
# JIOSAAVN ENDPOINT
# ═══════════════════════════════════════════════════════════════
@app.route('/api/saavn')
@limiter.limit("80 per minute")
def get_saavn_song():
    q           = request.args.get('q', '').strip()
    artist      = request.args.get('artist', '').strip()
    fallback    = request.args.get('fallback', '').strip()
    token       = request.args.get('token', '').strip()
    low_quality = request.args.get('low_quality', 'false').lower() == 'true'
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})
    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query)
        if result and not has_word_match(q, result['title']):
            if result.get('score', 0) < 0.8:
                log.warning(f"[Saavn] Final reject — query='{q}' returned '{result['title']}' score={result.get('score')}")
                result = None
        if result:
            if low_quality:
                low_url, low_q = _pick_low_quality(result.get('_raw_urls', []))
                if low_url:
                    result['url']     = low_url
                    result['quality'] = low_q
            log.info(f"[Saavn] ✓ q='{q}' → '{result['title']}' quality={result['quality']} score={result['score']}")
            return jsonify({'success': True, 'token': token, **result})
    log.info(f"[Saavn] ✗ No match — q='{q}'")
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/resolve  — SMART FALLBACK CHAIN
# JioSaavn → Piped → Invidious → fail
# ═══════════════════════════════════════════════════════════════
@app.route('/api/resolve')
@limiter.limit("80 per minute")
def resolve_song():
    """
    Master resolver — tries JioSaavn first, then Piped, then Invidious.
    Frontend calls this instead of /api/saavn directly.
    """
    q        = request.args.get('q', '').strip()
    artist   = request.args.get('artist', '').strip()
    fallback = request.args.get('fallback', '').strip()
    token    = request.args.get('token', '').strip()

    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})

    # ── Step 1: JioSaavn ──────────────────────────────────────
    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query)
        if result and not has_word_match(q, result['title']):
            if result.get('score', 0) < 0.8:
                result = None
        if result:
            log.info(f"[Resolve] ✓ JioSaavn — '{result['title']}' quality={result['quality']}")
            proxy_url = f"/api/stream?url={quote(result['url'], safe='')}"
            return jsonify({
                'success': True,
                'token':   token,
                'url':     proxy_url,
                'quality': result['quality'],
                'title':   result['title'],
                'artist':  result['artist'],
                'image':   result.get('image', ''),
                'source':  'saavn',
            })

    # ── Step 2: Piped (YouTube) ───────────────────────────────
    log.info(f"[Resolve] JioSaavn miss → trying Piped for '{q}'")
    piped_result = fetch_from_piped(q, title=q, artist=artist)
    if piped_result and piped_result.get('url'):
        log.info(f"[Resolve] ✓ Piped — '{piped_result['title']}' quality={piped_result['quality']}")
        # Piped URLs are direct CDN — proxy them
        proxy_url = f"/api/stream?url={quote(piped_result['url'], safe='')}"
        return jsonify({
            'success': True,
            'token':   token,
            'url':     proxy_url,
            'quality': piped_result['quality'],
            'title':   piped_result['title'],
            'artist':  piped_result['artist'],
            'image':   piped_result.get('image', ''),
            'source':  'piped',
        })

    # ── Step 3: Invidious (YouTube fallback) ──────────────────
    log.info(f"[Resolve] Piped miss → trying Invidious for '{q}'")
    inv_result = fetch_from_invidious(q, title=q, artist=artist)
    if inv_result and inv_result.get('url'):
        log.info(f"[Resolve] ✓ Invidious — '{inv_result['title']}' quality={inv_result['quality']}")
        proxy_url = f"/api/stream?url={quote(inv_result['url'], safe='')}"
        return jsonify({
            'success': True,
            'token':   token,
            'url':     proxy_url,
            'quality': inv_result['quality'],
            'title':   inv_result['title'],
            'artist':  inv_result['artist'],
            'image':   inv_result.get('image', ''),
            'source':  'invidious',
        })

    # ── Step 4: All failed ────────────────────────────────────
    log.info(f"[Resolve] ✗ All sources failed for '{q}'")
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# STREAM PROXY  — handles Saavn + YouTube CDN domains
# ═══════════════════════════════════════════════════════════════
def _is_allowed_domain(domain):
    for allowed in ALLOWED_STREAM_DOMAINS:
        if domain == allowed or domain.endswith('.' + allowed) or allowed in domain:
            return True
    return False

@app.route('/api/stream')
@limiter.limit("120 per minute")
def stream_audio():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Missing URL'}), 400
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return jsonify({'error': 'Invalid URL scheme'}), 400
        domain = parsed.netloc.lower().split(':')[0]
        if not _is_allowed_domain(domain):
            log.warning(f"[Stream] Blocked domain: {domain}")
            return jsonify({'error': 'Domain not allowed'}), 403
    except Exception:
        return jsonify({'error': 'Invalid URL'}), 400
    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header
        upstream = requests.get(url, headers=req_headers, stream=True, timeout=60, allow_redirects=True)
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers['Access-Control-Allow-Origin'] = '*'
        resp_headers['Accept-Ranges']               = 'bytes'
        resp_headers['Cache-Control']               = 'no-store'
        resp_headers['X-Content-Type-Options']      = 'nosniff'
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally:
                upstream.close()
        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=resp_headers,
            direct_passthrough=True
        )
    except Exception as e:
        log.error(f"[Stream] Error → {url[:80]}: {e}")
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# DOWNLOAD ENDPOINT  — Fixed & working
# ═══════════════════════════════════════════════════════════════
@app.route('/api/download')
@limiter.limit("20 per minute")
def download_song():
    """
    Download full song as MP3/audio file.
    Tries JioSaavn → Piped → Invidious chain.
    quality param: 'full' (best) or 'gift' (320kbps preferred)
    """
    q       = request.args.get('q', '').strip()
    artist  = request.args.get('artist', '').strip()
    quality = request.args.get('quality', 'full').strip()

    if not q:
        return jsonify({'error': 'Missing query'}), 400

    stream_url  = None
    content_type = 'audio/mpeg'
    filename_base = f"{q} - {artist}".strip(' -') if artist else q

    # ── Try JioSaavn ──────────────────────────────────────────
    for query in build_query_variants(q, artist, ''):
        result = fetch_saavn_parallel(query)
        if result and result.get('url'):
            raw_urls = result.get('_raw_urls', [])
            if quality == 'gift' and raw_urls:
                # Force 320kbps for gift quality
                for item in raw_urls:
                    if '320' in str(item.get('quality', '')):
                        stream_url = item.get('url') or item.get('link')
                        break
            if not stream_url:
                stream_url = result['url']
            log.info(f"[Download] JioSaavn → '{result['title']}' quality={result.get('quality')}")
            filename_base = f"{result['title']} - {result['artist']}".strip(' -')
            break

    # ── Try Piped if Saavn failed ─────────────────────────────
    if not stream_url:
        log.info(f"[Download] Saavn miss → Piped for '{q}'")
        piped = fetch_from_piped(q, title=q, artist=artist)
        if piped and piped.get('url'):
            stream_url    = piped['url']
            filename_base = f"{piped['title']} - {piped['artist']}".strip(' -')
            content_type  = 'audio/webm'
            log.info(f"[Download] Piped → '{piped['title']}'")

    # ── Try Invidious if Piped failed ─────────────────────────
    if not stream_url:
        log.info(f"[Download] Piped miss → Invidious for '{q}'")
        inv = fetch_from_invidious(q, title=q, artist=artist)
        if inv and inv.get('url'):
            stream_url    = inv['url']
            filename_base = f"{inv['title']} - {inv['artist']}".strip(' -')
            content_type  = 'audio/webm'
            log.info(f"[Download] Invidious → '{inv['title']}'")

    if not stream_url:
        log.warning(f"[Download] All sources failed for '{q}'")
        return jsonify({'error': 'Song not found on any source'}), 404

    # ── Stream download to client ─────────────────────────────
    try:
        clean_name = re.sub(r'[/\\?%*:|"<>]', '-', filename_base)
        ext        = 'webm' if 'webm' in content_type else 'mp3'
        filename   = f"{clean_name}.{ext}"

        headers = {
            'User-Agent':      'Mozilla/5.0',
            'Accept':          'audio/*,*/*;q=0.8',
            'Accept-Encoding': 'identity',
        }

        upstream = requests.get(stream_url, headers=headers, stream=True, timeout=60, allow_redirects=True)
        if not upstream.ok:
            return jsonify({'error': f'Upstream error {upstream.status_code}'}), 502

        # Detect actual content type
        actual_ct = upstream.headers.get('Content-Type', content_type)
        if 'webm' in actual_ct:
            ext = 'webm'
            filename = f"{clean_name}.webm"
        elif 'mp4' in actual_ct or 'm4a' in actual_ct:
            ext = 'm4a'
            filename = f"{clean_name}.m4a"

        resp_headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
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
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate()),
            status=200,
            headers=resp_headers
        )

    except Exception as e:
        log.error(f"[Download] Stream error: {e}")
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'sources': ['saavn', 'piped', 'invidious']})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
