from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import os
import re
import logging
import random
import time
import threading
import sys
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from urllib.parse import urlparse, urlencode, quote
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import atexit

sys.setrecursionlimit(10000)

# ── DES Decrypt (JioSaavn 320kbps) ──────────────────────────────────────────
try:
    from Crypto.Cipher import DES
    DES_AVAILABLE = True
except ImportError:
    try:
        from Cryptodome.Cipher import DES
        DES_AVAILABLE = True
    except ImportError:
        DES_AVAILABLE = False

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
# REAL IP
# ═══════════════════════════════════════════════════════════════
def get_real_ip():
    return (
        request.headers.get('CF-Connecting-IP') or
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.remote_addr or '127.0.0.1'
    )

# ═══════════════════════════════════════════════════════════════
# RATE LIMITER
# ═══════════════════════════════════════════════════════════════
limiter = Limiter(get_real_ip, app=app, default_limits=[], storage_uri="memory://")

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
SAAVN_MIRRORS = [
    'https://jiosavan-kappa.vercel.app',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
]

INVIDIOUS_INSTANCES = [
    'https://invidious.io.lol',
    'https://inv.nadeko.net',
    'https://invidious.privacydev.net',
    'https://iv.datura.network',
    'https://invidious.fdn.fr',
    'https://invidious.lunar.icu',
    'https://yt.drgnz.club',
    'https://invidious.perennialte.ch',
]

PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
    'https://pipedapi.adminforge.de',
]

ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'cf.saavncdn.com', 'aac.saavncdn.com', 'static.saavncdn.com',
    'c.saavncdn.com', 'h.saavncdn.com', 'googlevideo.com',
    'youtube.com', 'ytimg.com',
    'invidious.io.lol', 'inv.nadeko.net', 'invidious.privacydev.net',
    'iv.datura.network', 'invidious.fdn.fr', 'invidious.lunar.icu',
    'yt.drgnz.club', 'invidious.perennialte.ch',
    'pipedapi.kavin.rocks', 'piped-api.garudalinux.org',
    'api.piped.yt', 'pipedapi.adminforge.de',
]

QUALITY_RANK = {
    '320kbps': 7, '320': 7,
    '160kbps': 5, '160': 5,
    '96kbps':  3, '96':  3,
    '48kbps':  2, '48':  2,
    '12kbps':  1, '12':  1,
}

# ═══════════════════════════════════════════════════════════════
# PREMIUM HOME SECTION QUERIES
# Har section ke liye curated Bollywood/Hindi queries
# ═══════════════════════════════════════════════════════════════
SECTION_QUERIES = {
    'featured': [
        'arijit singh best songs', 'bollywood superhits 2024',
        'atif aslam romantic songs', 'jubin nautiyal hits',
        'shreya ghoshal songs', 'armaan malik songs',
    ],
    'trending': [
        'trending bollywood songs 2024', 'new hindi songs hits',
        'latest bollywood chartbusters', 'top hindi songs right now',
        'viral bollywood songs', 'bollywood top 10 hits',
    ],
    'mood': [
        'arijit singh sad songs', 'heartbreak hindi songs',
        'emotional bollywood songs', 'sad hindi songs midnight',
        'breakup songs hindi', 'tere bina songs',
    ],
    'classic': [
        'kumar sanu 90s hits', 'udit narayan romantic songs',
        'lata mangeshkar classics', 'kishore kumar hits',
        '90s bollywood superhits', 'mohammad rafi songs',
        'asha bhosle hits', 'old is gold bollywood',
    ],
    'hiphop': [
        'divine rap songs', 'emiway bantai songs',
        'badshah rap hits', 'yo yo honey singh songs',
        'desi hip hop india', 'gully boy songs',
        'divine gully gang', 'rap songs hindi',
    ],
    'lofi': [
        'lofi hindi songs chill', 'bollywood lofi remix',
        'hindi lofi beats study', 'arijit singh lofi',
        'lofi indian songs relaxing', 'chill hindi songs night',
    ],
    'arijit': [
        'arijit singh romantic hits', 'arijit singh top songs',
        'arijit singh soulful songs', 'arijit singh tum hi ho',
        'arijit singh best 2024',
    ],
    'workout': [
        'badshah party songs', 'yo yo honey singh party',
        'bollywood gym songs', 'workout hindi songs energetic',
        'punjabi party songs dhol', 'dance hindi songs upbeat',
    ],
    'new': [
        'new hindi songs 2024', 'latest bollywood releases',
        'new songs this week hindi', 'fresh bollywood hits 2024',
        'new romantic hindi songs', 'latest arijit jubin shreya',
    ],
    'punjabi': [
        'sidhu moosewala songs', 'diljit dosanjh hits',
        'punjabi superhits songs', 'ap dhillon songs',
        'karan aujla songs', 'punjabi romantic songs',
    ],
    'romantic': [
        'romantic hindi songs evergreen', 'love songs bollywood',
        'tere bina bollywood', 'pyaar songs hindi',
        'romantic arijit shreya duets', 'love bollywood songs 2024',
    ],
    'indie': [
        'prateek kuhad songs', 'when we were young hindi',
        'the local train songs', 'zaeden songs',
        'ritviz songs', 'indian indie songs',
        'seedhe maut songs', 'parvaaz songs',
    ],
}

# Homepage ke liye genre-wise primary queries
GENRE_HOMEPAGE_QUERIES = {
    'all':       'arijit singh bollywood hits',
    'bollywood': 'bollywood romantic songs hits',
    'hiphop':    'divine emiway bantai desi rap',
    'pop':       'pop hits bollywood jubin armaan',
    'rock':      'indian rock songs hindi',
    'indie':     'prateek kuhad ritviz indie hindi',
    'rnb':       'bollywood rnb soul songs',
    'lofi':      'lofi hindi chill bollywood remix',
}

# ═══════════════════════════════════════════════════════════════
# JIOSAAVN DES DECRYPT — 320kbps magic
# ═══════════════════════════════════════════════════════════════
SAAVN_DES_KEY = b'38346591'

def _decrypt_saavn_url(enc_url: str) -> str:
    """
    JioSaavn encrypted_media_url → real CDN URL
    Same algo as official app uses internally
    """
    if not DES_AVAILABLE:
        return ''
    try:
        # Base64 decode
        enc_bytes = base64.b64decode(enc_url)

        # DES ECB decrypt
        cipher    = DES.new(SAAVN_DES_KEY, DES.MODE_ECB)
        decrypted = cipher.decrypt(enc_bytes)

        # Remove PKCS5 padding
        pad_len = decrypted[-1]
        if isinstance(pad_len, int) and 1 <= pad_len <= 8:
            decrypted = decrypted[:-pad_len]

        url = decrypted.decode('utf-8', errors='ignore').strip()

        # Quality upgrade — always try 320kbps first
        url = url.replace('_96.mp4',  '_320.mp4')
        url = url.replace('_160.mp4', '_320.mp4')
        url = url.replace('_48.mp4',  '_320.mp4')
        url = url.replace('_12.mp4',  '_320.mp4')
        url = url.replace('_96.mp3',  '_320.mp3')
        url = url.replace('_160.mp3', '_320.mp3')

        return url if url.startswith('http') else ''
    except Exception as e:
        log.warning(f"[DES] Decrypt failed: {e}")
        return ''

def _get_quality_from_url(url: str) -> str:
    """URL se quality detect karo"""
    if '_320' in url: return '320kbps'
    if '_160' in url: return '160kbps'
    if '_96'  in url: return '96kbps'
    if '_48'  in url: return '48kbps'
    return 'unknown'

# ─── JioSaavn ke naye API format se URLs extract karo ───────────────────────
def _extract_download_urls(song: dict) -> list:
    """
    Multiple formats try karo:
    1. downloadUrl array (mirror API format)
    2. download_url string
    3. more_info.encrypted_media_url → DES decrypt
    4. more_info.media_preview_url
    """
    urls = []

    # Format 1: downloadUrl array (mirror format)
    raw = song.get('downloadUrl') or song.get('download_url') or []
    if isinstance(raw, list) and raw:
        return raw  # Already in correct format

    if isinstance(raw, str) and raw.startswith('http'):
        return [{'url': raw, 'quality': _get_quality_from_url(raw)}]

    # Format 2: more_info.encrypted_media_url (direct JioSaavn API)
    more_info = song.get('more_info', {})
    if isinstance(more_info, dict):
        enc_url = more_info.get('encrypted_media_url', '')
        if enc_url and DES_AVAILABLE:
            decrypted = _decrypt_saavn_url(enc_url)
            if decrypted:
                urls.append({'url': decrypted, 'quality': '320kbps'})
                log.info(f"[DES] ✓ Decrypted 320kbps URL")

        # Also try 160kbps and 96kbps as fallbacks
        enc_160 = more_info.get('encrypted_media_url_160', '')
        if enc_160 and DES_AVAILABLE:
            d160 = _decrypt_saavn_url(enc_160)
            if d160:
                urls.append({'url': d160, 'quality': '160kbps'})

        # Preview URL as last resort
        preview = more_info.get('media_preview_url', '')
        if preview and preview.startswith('http'):
            urls.append({'url': preview, 'quality': '96kbps'})

    # Format 3: vlink (sometimes direct)
    vlink = song.get('vlink', '')
    if vlink and vlink.startswith('http'):
        urls.append({'url': vlink, 'quality': _get_quality_from_url(vlink)})

    return urls

# ═══════════════════════════════════════════════════════════════
# HEALTH TRACKING
# ═══════════════════════════════════════════════════════════════
_mirror_failures    = {}
_invidious_failures = {}
_piped_failures     = {}
_MIRROR_COOLDOWN    = 300
_INV_COOLDOWN       = 120

_yt_url_cache  = {}
_YT_CACHE_TTL  = 3600
_inv_url_cache = {}
_INV_CACHE_TTL = 1800

_yt_semaphore = threading.Semaphore(3)

_saavn_executor = ThreadPoolExecutor(max_workers=len(SAAVN_MIRRORS), thread_name_prefix='saavn')
_yt_executor    = ThreadPoolExecutor(max_workers=3, thread_name_prefix='ytdlp')
_inv_executor   = ThreadPoolExecutor(max_workers=4, thread_name_prefix='invidious')

atexit.register(_saavn_executor.shutdown, wait=False)
atexit.register(_yt_executor.shutdown,    wait=False)
atexit.register(_inv_executor.shutdown,   wait=False)

def _is_alive(store, key, cooldown):
    ts = store.get(key)
    return not ts or (time.time() - ts) >= cooldown

def _mark_failed(store, key):
    store[key] = time.time()
    log.warning(f"[Health] Dead: {key}")

def _mark_ok(store, key):
    store.pop(key, None)

def is_mirror_alive(m):    return _is_alive(_mirror_failures, m, _MIRROR_COOLDOWN)
def mark_mirror_failed(m): _mark_failed(_mirror_failures, m)
def mark_mirror_ok(m):     _mark_ok(_mirror_failures, m)

def is_inv_alive(i):    return _is_alive(_invidious_failures, i, _INV_COOLDOWN)
def mark_inv_failed(i): _mark_failed(_invidious_failures, i)
def mark_inv_ok(i):     _mark_ok(_invidious_failures, i)

def is_piped_alive(p):    return _is_alive(_piped_failures, p, _INV_COOLDOWN)
def mark_piped_failed(p): _mark_failed(_piped_failures, p)
def mark_piped_ok(p):     _mark_ok(_piped_failures, p)

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
    text = re.sub(r'\((OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?)\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def build_query_variants(title, artist='', fallback=''):
    title_c  = clean_query(title)
    artist_c = clean_query(artist)   if artist   else ''
    fb_c     = clean_query(fallback) if fallback else ''
    seen, variants = set(), []

    def add(v):
        v = v.strip()
        if v and v not in seen:
            seen.add(v); variants.append(v)

    add(title_c)
    if artist_c:
        add(f"{title_c} {artist_c}")
    if fb_c:
        add(fb_c)
    return variants

def normalize(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def _safe_year(date_str):
    try:
        return int(str(date_str or '')[:4])
    except (ValueError, TypeError):
        return 0

def detect_genre(q, artist=''):
    text = (q + ' ' + artist).lower()
    if any(k in text for k in ['bhojpuri', 'pawan singh', 'khesari', 'nirahua']):
        return 'bhojpuri'
    if any(k in text for k in ['dj remix', 'dj mix', ' dj ', 'remix']):
        return 'dj'
    if any(k in text for k in ['nepali', 'nepal']):
        return 'nepali'
    if any(k in text for k in ['haryanvi', 'haryana']):
        return 'haryanvi'
    if any(k in text for k in ['punjabi', 'punjab']):
        return 'punjabi'
    if any(k in text for k in ['maithili', 'mithila']):
        return 'maithili'
    if any(k in text for k in ['odia', 'oriya', 'odisha']):
        return 'odia'
    if any(k in text for k in ['assamese', 'assam']):
        return 'assamese'
    return 'default'

FORCE_YT_KEYWORDS = [
    'nepali', 'nepal', 'bhojpuri', 'maithili', 'awadhi',
    'pahadi', 'haryanvi', 'chhattisgarhi', 'garhwali',
    'kumaoni', 'dogri', 'odia', 'assamese',
    'dj remix', 'dj mix', 'remix', 'dj version',
]

CATEGORY_QUERY_TEMPLATES = {
    'bhojpuri':    '{title} {artist} bhojpuri full song audio',
    'dj':          '{title} {artist} dj remix full song',
    'remix':       '{title} {artist} remix full audio',
    'nepali':      '{title} {artist} nepali full song',
    'haryanvi':    '{title} {artist} haryanvi full song',
    'punjabi':     '{title} {artist} punjabi full song audio',
    'default':     '{title} {artist} full song audio official',
}

def build_yt_query(title, artist='', genre='default'):
    template = CATEGORY_QUERY_TEMPLATES.get(genre, CATEGORY_QUERY_TEMPLATES['default'])
    q = template.format(title=title, artist=artist).strip()
    return re.sub(r'\s+', ' ', q)

# ═══════════════════════════════════════════════════════════════
# SEARCH MATCHING
# ═══════════════════════════════════════════════════════════════
def levenshtein(s1, s2):
    if len(s1) < len(s2): s1, s2 = s2, s1
    if not s2: return len(s1)
    prev = list(range(len(s2) + 1))
    for c1 in s1:
        curr = [prev[0] + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(c1 != c2)))
        prev = curr
    return prev[-1]

def fuzzy_word_match(qw, tw):
    if tw.startswith(qw): return 1.0
    if qw in tw:          return 0.85
    max_len = max(len(qw), len(tw))
    if max_len == 0:      return 0.0
    if len(qw) <= 3:
        return 1.0 if tw.startswith(qw) else 0.0
    ratio = 1.0 - (levenshtein(qw, tw) / max_len)
    return ratio if ratio >= 0.55 else 0.0

def title_score(query, song_title, song_artist=''):
    q, t, a = normalize(query), normalize(song_title), normalize(song_artist)
    if not q:  return 0.0
    if q == t: return 3.0
    q_words, t_words, a_words = q.split(), t.split(), a.split()
    score = 0.0
    if t.startswith(q): score += 2.0
    title_match = sum(
        max((fuzzy_word_match(qw, tw) for tw in t_words), default=0.0)
        for qw in q_words
    )
    if q_words: score += (title_match / len(q_words)) * 1.5
    artist_match = sum(
        max((fuzzy_word_match(qw, aw) for aw in a_words), default=0.0)
        for qw in q_words
    )
    if q_words: score += (artist_match / len(q_words)) * 0.5
    return score

def dynamic_min_score(query):
    length = len(normalize(query).replace(' ', ''))
    if length <= 3:   return 0.10
    elif length <= 6: return 0.30
    else:             return 0.40

def has_word_match(query, song_title):
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words: return False
    for qw in q_words:
        if len(qw) <= 3:
            for tw in t_words:
                if tw.startswith(qw): return True
            continue
        for tw in t_words:
            if fuzzy_word_match(qw, tw) >= 0.50: return True
    return False

# ═══════════════════════════════════════════════════════════════
# QUALITY PICKERS
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
# JIOSAAVN DIRECT API — Primary source with DES decrypt
# ═══════════════════════════════════════════════════════════════
JIOSAAVN_DIRECT = 'https://www.jiosaavn.com/api.php'

JIOSAAVN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Referer':    'https://www.jiosaavn.com/',
    'Origin':     'https://www.jiosaavn.com',
    'Accept':     'application/json, text/plain, */*',
}

def _parse_jiosaavn_image(song):
    img = song.get('image', '')
    if isinstance(img, str) and img.startswith('http'):
        return img.replace('150x150', '500x500').replace('50x50', '500x500')
    return ''

def fetch_from_jiosaavn_direct(query, limit=20):
    """
    JioSaavn direct API se songs fetch karo.
    DES decrypt karke 320kbps URLs nikalo.
    """
    results = []

    # ── Method 1: search.getResults ──────────────────────────────
    try:
        params = {
            '__call':      'search.getResults',
            'q':           query,
            '_format':     'json',
            '_marker':     '0',
            'api_version': '4',
            'ctx':         'web6dot0',
            'n':           str(limit),
            'p':           '1',
        }
        r = requests.get(JIOSAAVN_DIRECT, params=params, timeout=10, headers=JIOSAAVN_HEADERS)
        if r.status_code == 200:
            data      = r.json()
            songs_raw = data.get('results', [])

            for song in songs_raw:
                more_info = song.get('more_info', {})

                # DES decrypt try karo
                best_url, quality = None, 'unknown'

                if DES_AVAILABLE:
                    enc_url = more_info.get('encrypted_media_url', '')
                    if enc_url:
                        decrypted = _decrypt_saavn_url(enc_url)
                        if decrypted:
                            best_url = decrypted
                            quality  = _get_quality_from_url(decrypted)

                # Fallback: preview URL
                if not best_url:
                    preview = more_info.get('media_preview_url', '')
                    if preview and preview.startswith('http'):
                        best_url = preview
                        quality  = '96kbps'

                if not best_url:
                    continue

                title   = song.get('title', '') or song.get('song', '')
                artist  = song.get('primary_artists', '') or song.get('singers', '')
                image   = _parse_jiosaavn_image(song)
                year    = _safe_year(song.get('year', ''))
                song_id = song.get('id', random.randint(10000, 99999))

                results.append({
                    'trackId':          song_id,
                    'trackName':        title,
                    'artistName':       artist,
                    'artworkUrl100':    image,
                    'artworkUrl600':    image,
                    'url':              best_url,
                    'previewUrl':       best_url,
                    'quality':          quality,
                    'releaseDate':      str(year),
                    'primaryGenreName': 'Bollywood',
                    'source':           'jiosaavn_direct',
                    'title':            title,
                    'artist':           artist,
                    'image':            image,
                    'score':            1.0,
                    '_raw_urls':        [{'url': best_url, 'quality': quality}],
                    'success':          True,
                })

            log.info(f"[JioSaavn Direct] ✓ {len(results)} songs for '{query}'")
    except Exception as e:
        log.warning(f"[JioSaavn Direct] Method1 error: {e}")

    # ── Method 2: autocomplete.get (more results) ────────────────
    if len(results) < 5:
        try:
            params2 = {
                '__call':      'autocomplete.get',
                'query':       query,
                '_format':     'json',
                '_marker':     '0',
                'api_version': '4',
                'ctx':         'web6dot0',
                'n':           '10',
                'p':           '1',
            }
            r2 = requests.get(JIOSAAVN_DIRECT, params=params2, timeout=8, headers=JIOSAAVN_HEADERS)
            if r2.status_code == 200:
                data2  = r2.json()
                songs2 = data2.get('songs', {}).get('data', [])
                for song in songs2:
                    more_info = song.get('more_info', {})
                    enc_url   = more_info.get('encrypted_media_url', '')
                    best_url, quality = None, 'unknown'

                    if enc_url and DES_AVAILABLE:
                        decrypted = _decrypt_saavn_url(enc_url)
                        if decrypted:
                            best_url = decrypted
                            quality  = _get_quality_from_url(decrypted)

                    if not best_url:
                        preview = more_info.get('media_preview_url', '')
                        if preview and preview.startswith('http'):
                            best_url = preview
                            quality  = '96kbps'

                    if not best_url: continue

                    title   = song.get('title', '') or song.get('song', '')
                    artist  = song.get('primary_artists', '') or song.get('more_info', {}).get('singers', '')
                    image   = _parse_jiosaavn_image(song)
                    song_id = song.get('id', random.randint(10000, 99999))

                    # Duplicate check
                    if any(str(r.get('trackId')) == str(song_id) for r in results):
                        continue

                    results.append({
                        'trackId':          song_id,
                        'trackName':        title,
                        'artistName':       artist,
                        'artworkUrl100':    image,
                        'artworkUrl600':    image,
                        'url':              best_url,
                        'previewUrl':       best_url,
                        'quality':          quality,
                        'releaseDate':      '',
                        'primaryGenreName': 'Bollywood',
                        'source':           'jiosaavn_direct',
                        'title':            title,
                        'artist':           artist,
                        'image':            image,
                        'score':            1.0,
                        '_raw_urls':        [{'url': best_url, 'quality': quality}],
                        'success':          True,
                    })
        except Exception as e:
            log.warning(f"[JioSaavn Direct] Method2 error: {e}")

    return results

def fetch_from_jiosaavn_direct_single(query, min_score=0.3):
    songs = fetch_from_jiosaavn_direct(query, limit=10)
    if not songs: return None

    best_song, best_score = None, -1
    for song in songs:
        score = title_score(query, song['trackName'], song['artistName'])
        if score > best_score:
            best_score = score
            best_song  = song

    if not best_song or best_score < min_score:
        return None

    best_song['score'] = round(best_score, 3)
    return best_song

# ═══════════════════════════════════════════════════════════════
# SAAVN MIRROR FETCH
# ═══════════════════════════════════════════════════════════════
def fetch_from_mirror(mirror, query, min_score=0.4):
    if not is_mirror_alive(mirror): return None

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

            data = r.json()
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

            # Try DES decrypt on mirror results too
            raw_urls = _extract_download_urls(best_song)
            if not raw_urls:
                raw_urls = best_song.get('downloadUrl') or best_song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]

            best_url, quality = pick_best_quality(raw_urls)
            if not best_url: continue

            mark_mirror_ok(mirror)
            return {
                'url':       best_url,
                'quality':   quality,
                'title':     best_song.get('name') or best_song.get('title', ''),
                'artist':    best_song.get('primaryArtists') or best_song.get('primary_artists') or '',
                'image':     pick_image(best_song),
                'score':     round(best_score, 3),
                'source':    'saavn',
                'success':   True,
                '_raw_urls': raw_urls,
            }

        except requests.Timeout:
            mark_mirror_failed(mirror)
            log.warning(f"[Mirror] Timeout: {mirror}{endpoint}")
            break
        except Exception as e:
            log.warning(f"[Mirror {mirror}] {endpoint} → {e}")
            continue

    return None

def fetch_saavn_parallel(query):
    threshold   = dynamic_min_score(query)
    all_results = []

    direct_future = _saavn_executor.submit(fetch_from_jiosaavn_direct_single, query, threshold)

    alive_mirrors = [m for m in SAAVN_MIRRORS if is_mirror_alive(m)]
    if not alive_mirrors:
        _mirror_failures.clear()
        alive_mirrors = SAAVN_MIRRORS[:]

    mirror_futures = {
        _saavn_executor.submit(fetch_from_mirror, m, query, threshold): m
        for m in alive_mirrors
    }

    try:
        direct_result = direct_future.result(timeout=10)
        if direct_result:
            all_results.append(direct_result)
            log.info(f"[Saavn] Direct ok: {direct_result['title']}")
    except Exception as e:
        log.warning(f"[Saavn] Direct failed: {e}")

    try:
        for future in as_completed(mirror_futures, timeout=12):
            try:
                result = future.result()
                if result: all_results.append(result)
            except Exception as e:
                log.warning(f"[Parallel] Future error: {e}")
    except FuturesTimeout:
        log.warning("[Parallel] Some mirrors timed out")

    if not all_results: return None

    def result_rank(r):
        score   = r.get('score', 0)
        quality = r.get('quality', '')
        q_bonus = 0.15 if '320' in str(quality) else 0.08 if '160' in str(quality) else 0
        return score + q_bonus

    all_results.sort(key=result_rank, reverse=True)
    best = all_results[0]
    log.info(f"[Parallel] Best -> '{best['title']}' score={best.get('score',0)} quality={best['quality']}")
    return best

# ═══════════════════════════════════════════════════════════════
# INVIDIOUS — Bot-free YT audio
# ═══════════════════════════════════════════════════════════════
def _inv_cache_get(key):
    entry = _inv_url_cache.get(key)
    if entry:
        url, ts = entry
        if time.time() - ts < _INV_CACHE_TTL: return url
        del _inv_url_cache[key]
    return None

def _inv_cache_set(key, url):
    if len(_inv_url_cache) >= 300:
        oldest = min(_inv_url_cache, key=lambda k: _inv_url_cache[k][1])
        del _inv_url_cache[oldest]
    _inv_url_cache[key] = (url, time.time())

def _fetch_from_invidious_instance(instance, query, genre='default'):
    try:
        r = requests.get(
            f'{instance}/api/v1/search',
            params={'q': query, 'type': 'video', 'fields': 'videoId,title,lengthSeconds,author'},
            timeout=8,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        if r.status_code != 200:
            mark_inv_failed(instance); return None

        results = r.json()
        if not results or not isinstance(results, list): return None

        video_id = None
        for item in results[:5]:
            duration = item.get('lengthSeconds', 0)
            if duration >= 30:  # 30s+ accept karo (60 too strict tha)
                video_id = item.get('videoId')
                break

        if not video_id: return None

        v = requests.get(
            f'{instance}/api/v1/videos/{video_id}',
            params={'fields': 'adaptiveFormats,formatStreams,title,lengthSeconds'},
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        if v.status_code != 200:
            mark_inv_failed(instance); return None

        vdata = v.json()
        adaptive = vdata.get('adaptiveFormats', [])
        audio_streams = [
            f for f in adaptive
            if f.get('type', '').startswith('audio/')
            and f.get('url', '').startswith('http')
        ]

        if audio_streams:
            audio_streams.sort(key=lambda f: f.get('bitrate', 0), reverse=True)
            url = audio_streams[0].get('url')
            if url:
                mark_inv_ok(instance)
                log.info(f"[Invidious] ✓ {instance} | {query[:40]}")
                return url

        for fmt in vdata.get('formatStreams', []):
            url = fmt.get('url', '')
            if url.startswith('http'):
                mark_inv_ok(instance)
                return url

        return None

    except requests.Timeout:
        mark_inv_failed(instance)
        return None
    except Exception as e:
        log.warning(f"[Invidious] {instance} error: {e}")
        return None

def fetch_via_invidious(title, artist='', genre='default'):
    cache_key = f"inv|{normalize(title)}|{normalize(artist)}|{genre}"
    cached = _inv_cache_get(cache_key)
    if cached:
        log.info(f"[Invidious] Cache hit: {title}")
        return cached

    query = build_yt_query(title, artist, genre)
    alive = [i for i in INVIDIOUS_INSTANCES if is_inv_alive(i)]

    if not alive:
        _invidious_failures.clear()
        alive = INVIDIOUS_INSTANCES[:]

    futures = {
        _inv_executor.submit(_fetch_from_invidious_instance, inst, query, genre): inst
        for inst in alive[:5]
    }

    try:
        for future in as_completed(futures, timeout=15):
            try:
                result = future.result()
                if result:
                    _inv_cache_set(cache_key, result)
                    for f in futures: f.cancel()
                    return result
            except Exception as e:
                log.warning(f"[Invidious] Future error: {e}")
    except FuturesTimeout:
        log.warning("[Invidious] All instances timed out")

    return None

# ═══════════════════════════════════════════════════════════════
# PIPED API
# ═══════════════════════════════════════════════════════════════
def fetch_via_piped(title, artist='', genre='default'):
    query = build_yt_query(title, artist, genre)
    alive = [p for p in PIPED_INSTANCES if is_piped_alive(p)]
    if not alive:
        _piped_failures.clear()
        alive = PIPED_INSTANCES[:]

    for instance in alive:
        try:
            r = requests.get(
                f'{instance}/search',
                params={'q': query, 'filter': 'music_songs'},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://piped.video'}
            )
            if r.status_code != 200:
                mark_piped_failed(instance); continue

            data  = r.json()
            items = data.get('items', [])
            if not items: continue

            for item in items[:3]:
                video_url = item.get('url', '')
                if not video_url: continue
                vid_id   = video_url.split('?v=')[-1] if '?v=' in video_url else video_url.split('/')[-1]
                duration = item.get('duration', 0)
                if duration < 30: continue

                streams_r = requests.get(f'{instance}/streams/{vid_id}', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                if streams_r.status_code != 200: continue

                sdata         = streams_r.json()
                audio_streams = sdata.get('audioStreams', [])

                if audio_streams:
                    audio_streams.sort(key=lambda x: x.get('bitrate', 0), reverse=True)
                    url = audio_streams[0].get('url', '')
                    if url.startswith('http'):
                        mark_piped_ok(instance)
                        log.info(f"[Piped] ✓ {instance} | {title[:40]}")
                        return url

        except requests.Timeout:
            mark_piped_failed(instance)
        except Exception as e:
            log.warning(f"[Piped] {instance}: {e}")

    return None

# ═══════════════════════════════════════════════════════════════
# YT-DLP — Last resort
# ═══════════════════════════════════════════════════════════════
def _yt_cache_get(key):
    entry = _yt_url_cache.get(key)
    if entry:
        url, ts = entry
        if time.time() - ts < _YT_CACHE_TTL: return url
        del _yt_url_cache[key]
    return None

def _yt_cache_set(key, url):
    if len(_yt_url_cache) >= 200:
        oldest = min(_yt_url_cache, key=lambda k: _yt_url_cache[k][1])
        del _yt_url_cache[oldest]
    _yt_url_cache[key] = (url, time.time())

def fetch_yt_url(title, artist='', genre='default'):
    if not YT_DLP_AVAILABLE: return None

    cache_key = f"yt|{normalize(title)}|{normalize(artist)}|{genre}"
    cached = _yt_cache_get(cache_key)
    if cached: return cached

    if not _yt_semaphore.acquire(blocking=True, timeout=3):
        log.warning("[YT-dlp] Semaphore busy")
        return None

    try:
        search_q  = build_yt_query(title, artist, genre)
        ydl_opts  = {
            'format':         'bestaudio[ext=m4a]/bestaudio/best',
            'quiet':          True,
            'no_warnings':    True,
            'noplaylist':     True,
            'extract_flat':   False,
            'skip_download':  True,
            'socket_timeout': 10,
            'playlist_items': '1',
            'geo_bypass':     True,
            'extractor_args': {
                'youtube': {
                    'skip': ['dash', 'hls'],
                    'player_skip': ['webpage', 'configs'],
                }
            },
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36'
            },
        }

        cookies_path = os.path.join(BASE_DIR, 'cookies.txt')
        if os.path.isfile(cookies_path):
            ydl_opts['cookiefile'] = cookies_path

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info    = ydl.extract_info(f"ytsearch1:{search_q}", download=False)
            if not info: return None
            entries = info.get('entries') or [info]
            if not entries: return None
            entry   = entries[0]
            if not entry: return None

            duration = entry.get('duration') or 0
            if duration < 30: return None

            url = entry.get('url')
            if not url or not url.startswith('http'):
                requested = entry.get('requested_formats') or entry.get('formats') or []
                audio_formats = [
                    f for f in requested
                    if f.get('acodec') != 'none' and f.get('url', '').startswith('http')
                ]
                if audio_formats:
                    audio_formats.sort(key=lambda f: f.get('abr', 0) or 0, reverse=True)
                    url = audio_formats[0]['url']

            if url and url.startswith('http'):
                _yt_cache_set(cache_key, url)
                log.info(f"[YT-dlp] ✓ {entry.get('title', title)[:50]} ({duration}s)")
                return url
            return None

    except Exception as e:
        log.warning(f"[YT-dlp] Failed '{title}': {type(e).__name__}: {e}")
        return None
    finally:
        _yt_semaphore.release()

# ═══════════════════════════════════════════════════════════════
# GODMODE FETCH — Full waterfall
# JioSaavn Direct (DES) → Mirrors → Invidious → Piped → yt-dlp
# ═══════════════════════════════════════════════════════════════
def godmode_fetch(title, artist='', fallback='', force_yt=False, low_quality=False, token=''):
    genre = detect_genre(title, artist)

    if not force_yt:
        text = (title + ' ' + artist).lower()
        for kw in FORCE_YT_KEYWORDS:
            if kw in text:
                force_yt = True
                log.info(f"[Godmode] Auto force_yt: {kw}")
                break

    saavn_result = None

    # ── STEP 1: JioSaavn (DES 320kbps) ──────────────────────────
    if not force_yt:
        for query in build_query_variants(title, artist, fallback):
            saavn_result = fetch_saavn_parallel(query)
            if saavn_result and not has_word_match(title, saavn_result['title']):
                log.warning(f"[Godmode] Saavn reject: '{saavn_result['title']}' for '{title}'")
                saavn_result = None
            if saavn_result:
                break

    if saavn_result:
        if low_quality:
            low_url, low_q = _pick_low_quality(saavn_result.get('_raw_urls', []))
            if low_url:
                saavn_result['url']     = low_url
                saavn_result['quality'] = low_q
        saavn_result.pop('_raw_urls', None)
        saavn_result['token'] = token
        log.info(f"[Godmode] Saavn ✓ '{title}' @ {saavn_result.get('quality')}")
        return saavn_result

    # ── STEP 2: Invidious ───────────────────────────────────────
    log.info(f"[Godmode] Saavn miss — Invidious for '{title}'")
    inv_url = fetch_via_invidious(title, artist, genre)
    if inv_url:
        log.info(f"[Godmode] Invidious ✓ '{title}'")
        return {
            'success': True, 'url': inv_url, 'source': 'invidious',
            'title': title, 'artist': artist, 'quality': 'full',
            'image': '', 'token': token,
        }

    # ── STEP 3: Piped ───────────────────────────────────────────
    log.info(f"[Godmode] Invidious miss — Piped for '{title}'")
    piped_url = fetch_via_piped(title, artist, genre)
    if piped_url:
        log.info(f"[Godmode] Piped ✓ '{title}'")
        return {
            'success': True, 'url': piped_url, 'source': 'piped',
            'title': title, 'artist': artist, 'quality': 'full',
            'image': '', 'token': token,
        }

    # ── STEP 4: yt-dlp ─────────────────────────────────────────
    if YT_DLP_AVAILABLE:
        log.info(f"[Godmode] Piped miss — yt-dlp for '{title}'")
        try:
            future = _yt_executor.submit(fetch_yt_url, title, artist, genre)
            yt_url = future.result(timeout=25)
            if yt_url:
                log.info(f"[Godmode] yt-dlp ✓ '{title}'")
                return {
                    'success': True, 'url': yt_url, 'source': 'youtube',
                    'title': title, 'artist': artist, 'quality': 'full',
                    'image': '', 'token': token,
                }
        except FuturesTimeout:
            log.warning(f"[Godmode] yt-dlp timeout for '{title}'")
        except Exception as e:
            log.error(f"[Godmode] yt-dlp error: {e}")

    log.warning(f"[Godmode] ✗ All sources failed for '{title}'")
    return {'success': False, 'url': None, 'token': token}

# ═══════════════════════════════════════════════════════════════
# /api/songs — Homepage — JioSaavn Direct PRIMARY
# Deezer completely removed — garbage tha
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q       = request.args.get('q', '').strip()
    section = request.args.get('section', '').strip()  # section-specific query
    era     = request.args.get('era', '').strip()

    # Section-wise curated query
    if section and section in SECTION_QUERIES:
        queries = SECTION_QUERIES[section]
        search_term = random.choice(queries)
    elif q:
        search_term = q
    else:
        search_term = random.choice(SECTION_QUERIES['featured'])

    results = []

    # ── PRIMARY: JioSaavn Direct (320kbps, best) ─────────────────
    try:
        direct_songs = fetch_from_jiosaavn_direct(search_term, limit=30)
        results.extend(direct_songs)
        log.info(f"[Songs] Direct: {len(direct_songs)} songs for '{search_term}'")
    except Exception as e:
        log.warning(f"[Songs] Direct error: {e}")

    # ── BACKUP: Mirrors (agar direct < 8 songs) ──────────────────
    if len(results) < 8:
        alive_mirrors = [m for m in SAAVN_MIRRORS if is_mirror_alive(m)]
        if not alive_mirrors:
            alive_mirrors = SAAVN_MIRRORS[:]

        for mirror in alive_mirrors[:3]:
            try:
                r = requests.get(
                    f'{mirror}/api/search/songs',
                    params={'query': search_term, 'limit': 30},
                    timeout=7,
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                if r.status_code != 200: continue
                data  = r.json()
                songs = (
                    data.get('data', {}).get('results') or
                    data.get('results') or
                    data.get('songs', {}).get('results') or []
                )
                for song in songs:
                    raw_urls = _extract_download_urls(song)
                    if not raw_urls:
                        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                    if not raw_urls: continue

                    if isinstance(raw_urls, str):
                        raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]

                    best_url, quality = pick_best_quality(raw_urls)
                    if not best_url: continue

                    title   = song.get('name') or song.get('title', '')
                    artist  = song.get('primaryArtists') or song.get('primary_artists') or ''
                    image   = pick_image(song)
                    year    = _safe_year(song.get('releaseDate') or song.get('year', ''))
                    song_id = song.get('id', random.randint(10000, 99999))

                    # Duplicate check
                    if any(str(r2.get('trackId')) == str(song_id) for r2 in results):
                        continue

                    results.append({
                        'trackId':          song_id,
                        'trackName':        title,
                        'artistName':       artist,
                        'artworkUrl100':    image,
                        'artworkUrl600':    image,
                        'previewUrl':       best_url,
                        'url':              best_url,
                        'quality':          quality,
                        'releaseDate':      str(year),
                        'primaryGenreName': 'Bollywood',
                        'source':           'saavn_mirror',
                    })

                mark_mirror_ok(mirror)
                if len(results) >= 15: break

            except Exception as e:
                log.warning(f"[Songs] Mirror {mirror} failed: {e}")
                mark_mirror_failed(mirror)

    # ── LAST RESORT: Try alternate query ──────────────────────────
    if len(results) < 5 and section and section in SECTION_QUERIES:
        alt_query = random.choice(SECTION_QUERIES[section])
        try:
            alt_songs = fetch_from_jiosaavn_direct(alt_query, limit=15)
            for s in alt_songs:
                if not any(str(r2.get('trackId')) == str(s.get('trackId')) for r2 in results):
                    results.append(s)
        except Exception as e:
            log.warning(f"[Songs] Alt query failed: {e}")

    # Filter: sirf valid previewUrl wale songs
    results = [s for s in results if s.get('previewUrl') or s.get('url')]

    random.shuffle(results)
    return jsonify({'results': results[:30]})

# ═══════════════════════════════════════════════════════════════
# /api/song — GODMODE MAIN
# ═══════════════════════════════════════════════════════════════
@app.route('/api/song')
@limiter.limit("60 per minute")
def get_song_smart():
    q           = request.args.get('q', '').strip()
    artist      = request.args.get('artist', '').strip()
    fallback    = request.args.get('fallback', '').strip()
    token       = request.args.get('token', '').strip()
    low_quality = request.args.get('low_quality', 'false').lower() == 'true'
    force_yt    = request.args.get('force_yt', 'false').lower() == 'true'

    if not q:
        return jsonify({'success': False, 'error': 'Missing query', 'token': token})

    result = godmode_fetch(q, artist, fallback, force_yt, low_quality, token)
    return jsonify(result)

# ═══════════════════════════════════════════════════════════════
# /api/saavn — Direct Saavn
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
            log.warning(f"[Saavn] Final reject — '{result['title']}' for '{q}'")
            result = None
        if result:
            if low_quality:
                low_url, low_q = _pick_low_quality(result.get('_raw_urls', []))
                if low_url:
                    result['url']     = low_url
                    result['quality'] = low_q
            result.pop('_raw_urls', None)
            result['token'] = token
            log.info(f"[Saavn] ✓ '{q}' → '{result['title']}' quality={result['quality']}")
            return jsonify(result)

    log.info(f"[Saavn] Miss — godmode fallback for '{q}'")
    result = godmode_fetch(q, artist, fallback, True, low_quality, token)
    return jsonify(result)

# ═══════════════════════════════════════════════════════════════
# /api/yt — Direct YT
# ═══════════════════════════════════════════════════════════════
@app.route('/api/yt')
@limiter.limit("30 per minute")
def get_yt_song():
    q      = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    token  = request.args.get('token', '').strip()

    if not q:
        return jsonify({'success': False, 'error': 'Missing query', 'token': token})

    genre = detect_genre(q, artist)

    inv_url = fetch_via_invidious(q, artist, genre)
    if inv_url:
        return jsonify({'success': True, 'url': inv_url, 'source': 'invidious', 'title': q, 'artist': artist, 'token': token})

    piped_url = fetch_via_piped(q, artist, genre)
    if piped_url:
        return jsonify({'success': True, 'url': piped_url, 'source': 'piped', 'title': q, 'artist': artist, 'token': token})

    if not YT_DLP_AVAILABLE:
        return jsonify({'success': False, 'error': 'All YT sources failed', 'token': token}), 503

    try:
        future = _yt_executor.submit(fetch_yt_url, q, artist, genre)
        url    = future.result(timeout=25)
        if url:
            return jsonify({'success': True, 'url': url, 'source': 'youtube', 'title': q, 'artist': artist, 'token': token})
        return jsonify({'success': False, 'url': None, 'token': token})
    except FuturesTimeout:
        return jsonify({'success': False, 'error': 'timeout', 'token': token})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/stream — Proxy streamer
# ═══════════════════════════════════════════════════════════════
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

        domain         = parsed.netloc.lower().split(':')[0]
        is_googlevideo = domain.endswith('.googlevideo.com')
        allowed = is_googlevideo or any(
            domain == d or domain.endswith('.' + d)
            for d in ALLOWED_STREAM_DOMAINS
        )
        if not allowed:
            log.warning(f"[Stream] Blocked domain: {domain}")
            return jsonify({'error': 'Domain not allowed'}), 403

    except Exception:
        return jsonify({'error': 'Invalid URL'}), 400

    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept':          'audio/mpeg,audio/webm,audio/mp4,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header

        upstream = requests.get(url, headers=req_headers, stream=True, timeout=30, allow_redirects=True)

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
# /api/download
# ═══════════════════════════════════════════════════════════════
@app.route('/api/download')
@limiter.limit("50 per minute")
def download_song():
    q      = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()

    if not q:
        return jsonify({'success': False, 'error': 'Missing query'}), 400

    result = godmode_fetch(q, artist)
    if not result.get('success') or not result.get('url'):
        return jsonify({'success': False, 'error': 'Not found'}), 404

    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept':          'audio/mpeg,audio/webm,audio/mp4,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }
        upstream = requests.get(result['url'], headers=req_headers, stream=True, timeout=30, allow_redirects=True)
        if upstream.status_code != 200:
            return jsonify({'success': False, 'error': 'Stream failed'}), 502

        title_safe  = re.sub(r'[^\w\s-]', '', result.get('title', 'song'))[:50]
        artist_safe = re.sub(r'[^\w\s-]', '', result.get('artist', 'unknown'))[:30]
        filename    = f"{title_safe} - {artist_safe}.mp3"

        excluded     = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers['Content-Disposition']         = f'attachment; filename="{filename}"'
        resp_headers['Access-Control-Allow-Origin'] = '*'
        resp_headers['Cache-Control']               = 'no-store'

        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally:
                upstream.close()

        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers, direct_passthrough=True)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════
@app.route('/health')
def health():
    alive_mirrors = [m for m in SAAVN_MIRRORS if is_mirror_alive(m)]
    alive_inv     = [i for i in INVIDIOUS_INSTANCES if is_inv_alive(i)]
    alive_piped   = [p for p in PIPED_INSTANCES if is_piped_alive(p)]
    return jsonify({
        'status':           'ok',
        'yt_dlp':           YT_DLP_AVAILABLE,
        'des_decrypt':      DES_AVAILABLE,
        'saavn_mirrors':    f"{len(alive_mirrors)}/{len(SAAVN_MIRRORS)}",
        'invidious':        f"{len(alive_inv)}/{len(INVIDIOUS_INSTANCES)}",
        'piped':            f"{len(alive_piped)}/{len(PIPED_INSTANCES)}",
        'yt_cache':         len(_yt_url_cache),
        'inv_cache':        len(_inv_url_cache),
    })

# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    log.info(f"🎵 Aurum Godmode | Port {port} | yt-dlp: {YT_DLP_AVAILABLE} | DES: {DES_AVAILABLE}")
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
