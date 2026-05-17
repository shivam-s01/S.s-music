from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import os
import re
import logging
import random
import time
import sys
from urllib.parse import urlparse
from functools import wraps
import atexit

sys.setrecursionlimit(10000)

# YT-DLP optional - agar nahi hai to sirf Saavn kaam karega
try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════
# APP INIT
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# SIMPLE RATE LIMITER (without flask-limiter)
# ═══════════════════════════════════════════════════════════════
_rate_limit_store = {}
def simple_rate_limit(limit=60, window=60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
            key = f"{ip}:{f.__name__}"
            now = time.time()
            
            if key in _rate_limit_store:
                requests_list = _rate_limit_store[key]
                requests_list = [t for t in requests_list if now - t < window]
                if len(requests_list) >= limit:
                    return jsonify({'error': 'Rate limit exceeded'}), 429
                requests_list.append(now)
                _rate_limit_store[key] = requests_list
            else:
                _rate_limit_store[key] = [now]
            
            return f(*args, **kwargs)
        return wrapped
    return decorator

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
SAAVN_MIRRORS = [
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
]

ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com', 'cf.saavncdn.com',
    'aac.saavncdn.com', 'static.saavncdn.com', 'c.saavncdn.com',
    'h.saavncdn.com', 'googlevideo.com', 'youtube.com', 'ytimg.com',
]

QUALITY_RANK = {
    '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
    '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
}

NINETIES_SEEDS = [
    "Kumar Sanu hits", "Udit Narayan 90s", "Alka Yagnik 90s",
    "Lata Mangeshkar 90s", "Sonu Nigam 90s hits", "AR Rahman 90s",
    "Nadeem Shravan songs", "Jatin Lalit songs", "90s Bollywood superhits",
]

NINETIES_TRIGGERS = ['90', 'purane', 'old', 'retro', 'classic', 'nineties', 'evergreen']

FORCE_YT_KEYWORDS = ['nepali', 'bhojpuri', 'haryanvi', 'odia', 'assamese']

_mirror_failures = {}
_MIRROR_COOLDOWN = 300
_yt_cache = {}
_YT_CACHE_TTL = 3600

# ═══════════════════════════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════════════════════════
@app.after_request
def after_request(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = '*'
    return resp

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return Response(status=200)

# ═══════════════════════════════════════════════════════════════
# FRONTEND
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/<path:filename>')
def serve_static(filename):
    file_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(file_path):
        return send_file(file_path)
    return jsonify({'error': 'Not found'}), 404

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
def is_mirror_alive(mirror):
    fail_ts = _mirror_failures.get(mirror)
    if fail_ts and (time.time() - fail_ts) < _MIRROR_COOLDOWN:
        return False
    return True

def mark_mirror_failed(mirror):
    _mirror_failures[mirror] = time.time()

def clean_query(text):
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'["\'()]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def build_query_variants(title, artist='', fallback=''):
    title_c = clean_query(title)
    artist_c = clean_query(artist) if artist else ''
    variants = [title_c]
    if artist_c:
        variants.append(f"{title_c} {artist_c}")
    if fallback:
        variants.append(clean_query(fallback))
    return list(dict.fromkeys(variants))

def normalize(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def has_word_match(query, song_title):
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words:
        return False
    for qw in q_words:
        if len(qw) <= 3:
            for tw in t_words:
                if tw.startswith(qw):
                    return True
        else:
            for tw in t_words:
                if qw in tw or tw in qw:
                    return True
    return False

def pick_best_quality(urls):
    if not urls:
        return None, None
    if isinstance(urls, str):
        return urls, 'unknown'
    for item in urls:
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

def _safe_year(date_str):
    try:
        return int(str(date_str or '')[:4])
    except:
        return 0

# ═══════════════════════════════════════════════════════════════
# SAAVN FETCH
# ═══════════════════════════════════════════════════════════════
def fetch_saavn_song(query):
    for mirror in SAAVN_MIRRORS:
        if not is_mirror_alive(mirror):
            continue
        try:
            r = requests.get(
                f'{mirror}/api/search/songs',
                params={'query': query, 'limit': 10},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200:
                continue
            
            data = r.json()
            results = data.get('data', {}).get('results') or data.get('results') or []
            
            for song in results:
                song_title = song.get('name') or song.get('title', '')
                if not has_word_match(query, song_title):
                    continue
                
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                best_url, quality = pick_best_quality(raw_urls if isinstance(raw_urls, list) else [{'url': raw_urls}])
                
                if best_url:
                    return {
                        'url': best_url,
                        'quality': quality or '320kbps',
                        'title': song_title,
                        'artist': song.get('primaryArtists') or song.get('primary_artists') or '',
                        'image': pick_image(song),
                        'source': 'saavn'
                    }
        except Exception as e:
            mark_mirror_failed(mirror)
            continue
    return None

# ═══════════════════════════════════════════════════════════════
# YT-DLP FETCH (optional)
# ═══════════════════════════════════════════════════════════════
def fetch_yt_url(title, artist=''):
    if not YT_DLP_AVAILABLE:
        return None
    
    cache_key = f"{normalize(title)}|{normalize(artist)}"
    if cache_key in _yt_cache:
        cached_url, cached_time = _yt_cache[cache_key]
        if time.time() - cached_time < _YT_CACHE_TTL:
            return cached_url
    
    try:
        search_q = f"{title} {artist} official audio" if artist else f"{title} official audio"
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'skip_download': True,
            'socket_timeout': 15,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{search_q}", download=False)
            if info and info.get('entries'):
                entry = info['entries'][0]
                duration = entry.get('duration', 0)
                if duration >= 60:
                    url = entry.get('url')
                    if url:
                        _yt_cache[cache_key] = (url, time.time())
                        return url
        return None
    except Exception as e:
        log.warning(f"YT error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# API ENDPOINTS
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@simple_rate_limit(60, 60)
def get_songs():
    q = request.args.get('q', 'bollywood hits').strip()
    era = request.args.get('era', '').strip()
    
    is_90s = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q
    
    results = []
    for mirror in SAAVN_MIRRORS[:3]:
        try:
            r = requests.get(
                f'{mirror}/api/search/songs',
                params={'query': search_term, 'limit': 30},
                timeout=8
            )
            if r.status_code != 200:
                continue
            data = r.json()
            songs = data.get('data', {}).get('results') or data.get('results') or []
            
            for song in songs[:30]:
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                best_url, quality = pick_best_quality(raw_urls if isinstance(raw_urls, list) else [{'url': raw_urls}])
                if not best_url:
                    continue
                
                results.append({
                    'trackId': song.get('id', random.randint(10000, 99999)),
                    'trackName': song.get('name') or song.get('title', ''),
                    'artistName': song.get('primaryArtists') or '',
                    'artworkUrl100': pick_image(song),
                    'artworkUrl600': pick_image(song),
                    'url': best_url,
                    'quality': quality,
                    'releaseDate': str(_safe_year(song.get('releaseDate') or song.get('year'))),
                    'source': 'saavn'
                })
            if results:
                break
        except:
            continue
    
    random.shuffle(results)
    return jsonify({'results': results[:30]})

@app.route('/api/song')
@simple_rate_limit(60, 60)
def get_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    token = request.args.get('token', '').strip()
    force_yt = request.args.get('force_yt', 'false').lower() == 'true'
    
    if not q:
        return jsonify({'success': False, 'error': 'Missing query', 'token': token})
    
    # Check for forced YT genres
    q_lower = q.lower()
    for kw in FORCE_YT_KEYWORDS:
        if kw in q_lower:
            force_yt = True
            break
    
    result = None
    if not force_yt:
        for query in build_query_variants(q, artist):
            result = fetch_saavn_song(query)
            if result:
                break
    
    if not result and YT_DLP_AVAILABLE:
        yt_url = fetch_yt_url(q, artist)
        if yt_url:
            result = {
                'url': yt_url,
                'source': 'youtube',
                'title': q,
                'artist': artist,
                'quality': 'full',
                'image': ''
            }
    
    if result:
        return jsonify({'success': True, 'token': token, **result})
    
    return jsonify({'success': False, 'url': None, 'token': token})

@app.route('/api/stream')
@simple_rate_limit(120, 60)
def stream_audio():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Missing URL'}), 400
    
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().split(':')[0]
        allowed = any(domain == d or domain.endswith('.' + d) for d in ALLOWED_STREAM_DOMAINS)
        if not allowed:
            return jsonify({'error': 'Domain not allowed'}), 403
        
        req_headers = {'User-Agent': 'Mozilla/5.0'}
        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header
        
        upstream = requests.get(url, headers=req_headers, stream=True, timeout=30)
        
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers['Access-Control-Allow-Origin'] = '*'
        resp_headers['Accept-Ranges'] = 'bytes'
        resp_headers['Cache-Control'] = 'no-store'
        
        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
            upstream.close()
        
        return Response(generate(), status=upstream.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'yt_dlp': YT_DLP_AVAILABLE})

# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    log.info(f"Server starting on port {port} | yt-dlp: {YT_DLP_AVAILABLE}")
    app.run(host='0.0.0.0', port=port, threaded=True)
