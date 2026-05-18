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
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from urllib.parse import urlparse, urlencode, quote
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import atexit

sys.setrecursionlimit(10000)

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

# ── Invidious public instances — bot-free YT audio ─────────────
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

# ── Piped API instances — extra fallback ────────────────────────
PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
    'https://pipedapi.adminforge.de',
]

ALLOWED_STREAM_DOMAINS = [
    'akamaized.net',
    'jiocdn.com',
    'saavncdn.com',
    'cf.saavncdn.com',
    'aac.saavncdn.com',
    'static.saavncdn.com',
    'c.saavncdn.com',
    'h.saavncdn.com',
    'googlevideo.com',
    'youtube.com',
    'ytimg.com',
    'invidious.io.lol',
    'inv.nadeko.net',
    'invidious.privacydev.net',
    'iv.datura.network',
    'invidious.fdn.fr',
    'invidious.lunar.icu',
    'yt.drgnz.club',
    'invidious.perennialte.ch',
    'pipedapi.kavin.rocks',
    'piped-api.garudalinux.org',
    'api.piped.yt',
    'pipedapi.adminforge.de',
]

QUALITY_RANK = {
    '320kbps': 7, '320': 7,
    '160kbps': 5, '160': 5,
    '96kbps':  3, '96':  3,
    '48kbps':  2, '48':  2,
    '12kbps':  1, '12':  1,
}

# ── 90s seeds ───────────────────────────────────────────────────
NINETIES_SEEDS = [
    "Kumar Sanu hits", "Udit Narayan 90s", "Alka Yagnik 90s",
    "Lata Mangeshkar 90s", "Sonu Nigam 90s hits",
    "Kavita Krishnamurthy songs", "Asha Bhosle 90s",
    "Abhijeet Bhattacharya hits", "Shankar Mahadevan 90s",
    "AR Rahman 90s", "Anu Malik 90s hits",
    "Nadeem Shravan songs", "Jatin Lalit songs",
    "Kumar Sanu Alka Yagnik duets", "90s Bollywood superhits",
    "Kishore Kumar hits", "Mohammed Rafi songs",
    "Dilwale Dulhania Le Jayenge songs", "Hum Aapke Hain Koun songs",
    "Raja Hindustani songs", "Dil To Pagal Hai songs",
]

NINETIES_TRIGGERS = [
    '90', 'purane', 'purana', 'purani', 'old', 'retro',
    'classic', 'nineties', 'throwback', 'evergreen', 'gaane',
    'vintage', 'purani yaadein',
]

# ── Category → smart YT search query builder ────────────────────
CATEGORY_QUERY_TEMPLATES = {
    'bhojpuri':    '{title} {artist} bhojpuri full song audio',
    'dj':          '{title} {artist} dj remix full song',
    'remix':       '{title} {artist} remix full audio',
    'nepali':      '{title} {artist} nepali full song',
    'haryanvi':    '{title} {artist} haryanvi full song',
    'maithili':    '{title} {artist} maithili full audio',
    'awadhi':      '{title} {artist} awadhi full song',
    'pahadi':      '{title} {artist} pahadi full song',
    'chhattisgarhi': '{title} {artist} chhattisgarhi full song',
    'odia':        '{title} {artist} odia full song audio',
    'assamese':    '{title} {artist} assamese full song',
    'punjabi':     '{title} {artist} punjabi full song audio',
    '90s':         '{title} {artist} 90s bollywood full song audio',
    'default':     '{title} {artist} full song audio official',
}

# ── Keywords that force YT (Saavn pe nahi milta) ────────────────
FORCE_YT_KEYWORDS = [
    'nepali', 'nepal', 'bhojpuri', 'maithili', 'awadhi',
    'pahadi', 'haryanvi', 'chhattisgarhi', 'garhwali',
    'kumaoni', 'dogri', 'odia', 'assamese',
    'dj remix', 'dj mix', 'remix', 'dj version',
]

# ── Mirror + Invidious health tracking ──────────────────────────
_mirror_failures    = {}
_invidious_failures = {}
_piped_failures     = {}
_MIRROR_COOLDOWN    = 300   # 5 min
_INV_COOLDOWN       = 120   # 2 min (faster recovery)

# ── YT cache ────────────────────────────────────────────────────
_yt_url_cache  = {}
_YT_CACHE_TTL  = 3600
_inv_url_cache = {}
_INV_CACHE_TTL = 1800

# ── Semaphores ──────────────────────────────────────────────────
_yt_semaphore = threading.Semaphore(3)

# ── Thread pools ────────────────────────────────────────────────
_saavn_executor = ThreadPoolExecutor(max_workers=len(SAAVN_MIRRORS), thread_name_prefix='saavn')
_yt_executor    = ThreadPoolExecutor(max_workers=3, thread_name_prefix='ytdlp')
_inv_executor   = ThreadPoolExecutor(max_workers=4, thread_name_prefix='invidious')

atexit.register(_saavn_executor.shutdown, wait=False)
atexit.register(_yt_executor.shutdown,    wait=False)
atexit.register(_inv_executor.shutdown,   wait=False)

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
# HEALTH TRACKING
# ═══════════════════════════════════════════════════════════════
def _is_alive(store, key, cooldown):
    ts = store.get(key)
    return not ts or (time.time() - ts) >= cooldown

def _mark_failed(store, key, cooldown_label=''):
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
    """Detect genre/category from query string"""
    text = (q + ' ' + artist).lower()
    if any(k in text for k in ['bhojpuri', 'pawan singh', 'khesari', 'nirahua']):
        return 'bhojpuri'
    if any(k in text for k in ['dj remix', 'dj mix', ' dj ', 'remix', 'mix']):
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
    if any(k in text for k in NINETIES_TRIGGERS):
        return '90s'
    return 'default'

def build_yt_query(title, artist='', genre='default'):
    template = CATEGORY_QUERY_TEMPLATES.get(genre, CATEGORY_QUERY_TEMPLATES['default'])
    q = template.format(title=title, artist=artist).strip()
    return re.sub(r'\s+', ' ', q)

# ═══════════════════════════════════════════════════════════════
# SEARCH MATCHING
# ═══════════════════════════════════════════════════════════════
def levenshtein(s1, s2):
    if len(s1) < len(s2):
        s1, s2 = s2, s1
    if not s2:
        return len(s1)
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
    else:             return 0.45

def has_word_match(query, song_title):
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words: return False
    for qw in q_words:
        if len(qw) <= 3:
            for tw in t_words:
                if tw.startswith(qw):
                    return True
            continue
        for tw in t_words:
            if fuzzy_word_match(qw, tw) >= 0.50:
                return True
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
# INVIDIOUS — Bot-free YT audio (PRIMARY YT METHOD)
# ═══════════════════════════════════════════════════════════════
def _inv_cache_get(key):
    entry = _inv_url_cache.get(key)
    if entry:
        url, ts = entry
        if time.time() - ts < _INV_CACHE_TTL:
            return url
        del _inv_url_cache[key]
    return None

def _inv_cache_set(key, url):
    if len(_inv_url_cache) >= 300:
        oldest = min(_inv_url_cache, key=lambda k: _inv_url_cache[k][1])
        del _inv_url_cache[oldest]
    _inv_url_cache[key] = (url, time.time())

def _fetch_from_invidious_instance(instance, query, genre='default'):
    """Single Invidious instance try"""
    try:
        # Step 1: Search
        r = requests.get(
            f'{instance}/api/v1/search',
            params={'q': query, 'type': 'video', 'fields': 'videoId,title,lengthSeconds,author'},
            timeout=8,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        if r.status_code != 200:
            mark_inv_failed(instance)
            return None

        results = r.json()
        if not results or not isinstance(results, list):
            return None

        # Duration filter — pick first result with >60s
        video_id = None
        for item in results[:5]:
            duration = item.get('lengthSeconds', 0)
            if duration >= 60:
                video_id = item.get('videoId')
                break

        if not video_id:
            return None

        # Step 2: Get audio stream URL
        v = requests.get(
            f'{instance}/api/v1/videos/{video_id}',
            params={'fields': 'adaptiveFormats,formatStreams,title,lengthSeconds'},
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        if v.status_code != 200:
            mark_inv_failed(instance)
            return None

        vdata = v.json()

        # Try adaptiveFormats first (audio only streams)
        adaptive = vdata.get('adaptiveFormats', [])
        audio_streams = [
            f for f in adaptive
            if f.get('type', '').startswith('audio/')
            and f.get('url', '').startswith('http')
        ]

        if audio_streams:
            # Sort by bitrate descending
            audio_streams.sort(key=lambda f: f.get('bitrate', 0), reverse=True)
            url = audio_streams[0].get('url')
            if url:
                mark_inv_ok(instance)
                log.info(f"[Invidious] ✓ {instance} | {query[:40]}")
                return url

        # Fallback to formatStreams (muxed video+audio, still works for audio playback)
        format_streams = vdata.get('formatStreams', [])
        for fmt in format_streams:
            url = fmt.get('url', '')
            if url.startswith('http'):
                mark_inv_ok(instance)
                log.info(f"[Invidious] ✓ formatStream {instance} | {query[:40]}")
                return url

        return None

    except requests.Timeout:
        mark_inv_failed(instance)
        log.warning(f"[Invidious] Timeout: {instance}")
        return None
    except Exception as e:
        log.warning(f"[Invidious] {instance} error: {e}")
        return None

def fetch_via_invidious(title, artist='', genre='default'):
    """Try all Invidious instances in parallel"""
    cache_key = f"inv|{normalize(title)}|{normalize(artist)}|{genre}"
    cached = _inv_cache_get(cache_key)
    if cached:
        log.info(f"[Invidious] Cache hit: {title}")
        return cached

    query = build_yt_query(title, artist, genre)
    alive = [i for i in INVIDIOUS_INSTANCES if is_inv_alive(i)]

    if not alive:
        log.warning("[Invidious] All instances dead — resetting")
        _invidious_failures.clear()
        alive = INVIDIOUS_INSTANCES[:]

    futures = {
        _inv_executor.submit(_fetch_from_invidious_instance, inst, query, genre): inst
        for inst in alive[:5]  # Parallel top 5
    }

    try:
        for future in as_completed(futures, timeout=15):
            try:
                result = future.result()
                if result:
                    _inv_cache_set(cache_key, result)
                    # Cancel remaining
                    for f in futures:
                        f.cancel()
                    return result
            except Exception as e:
                log.warning(f"[Invidious] Future error: {e}")
    except FuturesTimeout:
        log.warning("[Invidious] All instances timed out")

    return None

# ═══════════════════════════════════════════════════════════════
# PIPED API — Second YT fallback
# ═══════════════════════════════════════════════════════════════
def fetch_via_piped(title, artist='', genre='default'):
    """Piped API — another bot-free YT frontend"""
    query = build_yt_query(title, artist, genre)
    alive = [p for p in PIPED_INSTANCES if is_piped_alive(p)]

    if not alive:
        _piped_failures.clear()
        alive = PIPED_INSTANCES[:]

    for instance in alive:
        try:
            # Search
            r = requests.get(
                f'{instance}/search',
                params={'q': query, 'filter': 'music_songs'},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://piped.video'}
            )
            if r.status_code != 200:
                mark_piped_failed(instance)
                continue

            data = r.json()
            items = data.get('items', [])
            if not items:
                continue

            # First valid item
            for item in items[:3]:
                video_url = item.get('url', '')
                if not video_url:
                    continue
                vid_id = video_url.split('?v=')[-1] if '?v=' in video_url else video_url.split('/')[-1]
                duration = item.get('duration', 0)
                if duration < 60:
                    continue

                # Get streams
                streams_r = requests.get(
                    f'{instance}/streams/{vid_id}',
                    timeout=10,
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                if streams_r.status_code != 200:
                    continue

                sdata = streams_r.json()
                audio_streams = sdata.get('audioStreams', [])

                if audio_streams:
                    # Highest quality
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
# YT-DLP — Last resort (cookies-free attempt)
# ═══════════════════════════════════════════════════════════════
def _yt_cache_get(key):
    entry = _yt_url_cache.get(key)
    if entry:
        url, ts = entry
        if time.time() - ts < _YT_CACHE_TTL:
            return url
        del _yt_url_cache[key]
    return None

def _yt_cache_set(key, url):
    if len(_yt_url_cache) >= 200:
        oldest = min(_yt_url_cache, key=lambda k: _yt_url_cache[k][1])
        del _yt_url_cache[oldest]
    _yt_url_cache[key] = (url, time.time())

def fetch_yt_url(title, artist='', genre='default'):
    if not YT_DLP_AVAILABLE:
        return None

    cache_key = f"yt|{normalize(title)}|{normalize(artist)}|{genre}"
    cached = _yt_cache_get(cache_key)
    if cached:
        log.info(f"[YT-dlp] Cache hit: {title}")
        return cached

    if not _yt_semaphore.acquire(blocking=True, timeout=3):
        log.warning("[YT-dlp] Semaphore busy — skipping")
        return None

    try:
        search_q = build_yt_query(title, artist, genre)

        ydl_opts = {
            'format':         'bestaudio[ext=m4a]/bestaudio/best',
            'quiet':          True,
            'no_warnings':    True,
            'noplaylist':     True,
            'extract_flat':   False,
            'skip_download':  True,
            'socket_timeout': 10,
            'playlist_items': '1',
            'geo_bypass':     True,
            # Try without cookies first — bot detection bypass via extractor args
            'extractor_args': {
                'youtube': {
                    'skip': ['dash', 'hls'],
                    'player_skip': ['webpage', 'configs'],
                }
            },
            'http_headers': {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                )
            },
        }

        # Use cookies.txt if available
        cookies_path = os.path.join(BASE_DIR, 'cookies.txt')
        if os.path.isfile(cookies_path):
            ydl_opts['cookiefile'] = cookies_path
            log.info("[YT-dlp] Using cookies.txt")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{search_q}", download=False)
            if not info: return None
            entries = info.get('entries') or [info]
            if not entries: return None
            entry = entries[0]
            if not entry: return None

            duration = entry.get('duration') or 0
            if duration < 60:
                log.warning(f"[YT-dlp] Too short ({duration}s), skipping: {title}")
                return None

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
# GODMODE FETCH — Full waterfall chain
# Saavn → Invidious (parallel) → Piped → yt-dlp
# ═══════════════════════════════════════════════════════════════
def godmode_fetch(title, artist='', fallback='', force_yt=False, low_quality=False, token=''):
    """
    The ultimate fetch chain. Nothing should fall through.
    Returns dict with url, source, quality etc.
    """
    genre = detect_genre(title, artist)

    # Auto force YT for genres Saavn doesn't have
    if not force_yt:
        text = (title + ' ' + artist).lower()
        for kw in FORCE_YT_KEYWORDS:
            if kw in text:
                force_yt = True
                log.info(f"[Godmode] Auto force_yt: {kw}")
                break

    saavn_result = None

    # ── STEP 1: Saavn (fast, 320kbps, best for Bollywood) ───────
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
        log.info(f"[Godmode] Saavn ✓ '{title}'")
        return saavn_result

    # ── STEP 2: Invidious (bot-free, parallel, primary YT) ──────
    log.info(f"[Godmode] Saavn miss — trying Invidious for '{title}'")
    inv_url = fetch_via_invidious(title, artist, genre)
    if inv_url:
        log.info(f"[Godmode] Invidious ✓ '{title}'")
        return {
            'success': True,
            'url':     inv_url,
            'source':  'invidious',
            'title':   title,
            'artist':  artist,
            'quality': 'full',
            'image':   '',
            'token':   token,
        }

    # ── STEP 3: Piped API ───────────────────────────────────────
    log.info(f"[Godmode] Invidious miss — trying Piped for '{title}'")
    piped_url = fetch_via_piped(title, artist, genre)
    if piped_url:
        log.info(f"[Godmode] Piped ✓ '{title}'")
        return {
            'success': True,
            'url':     piped_url,
            'source':  'piped',
            'title':   title,
            'artist':  artist,
            'quality': 'full',
            'image':   '',
            'token':   token,
        }

    # ── STEP 4: yt-dlp (last resort, slower) ───────────────────
    if YT_DLP_AVAILABLE:
        log.info(f"[Godmode] Piped miss — trying yt-dlp for '{title}'")
        try:
            future = _yt_executor.submit(fetch_yt_url, title, artist, genre)
            yt_url = future.result(timeout=25)
            if yt_url:
                log.info(f"[Godmode] yt-dlp ✓ '{title}'")
                return {
                    'success': True,
                    'url':     yt_url,
                    'source':  'youtube',
                    'title':   title,
                    'artist':  artist,
                    'quality': 'full',
                    'image':   '',
                    'token':   token,
                }
        except FuturesTimeout:
            log.warning(f"[Godmode] yt-dlp timeout for '{title}'")
        except Exception as e:
            log.error(f"[Godmode] yt-dlp error: {e}")

    # ── STEP 5: Nothing worked ──────────────────────────────────
    log.warning(f"[Godmode] ✗ All sources failed for '{title}'")
    return {'success': False, 'url': None, 'token': token}

# ═══════════════════════════════════════════════════════════════
# JIOSAAVN DIRECT API — Primary source, never goes down
# ═══════════════════════════════════════════════════════════════
JIOSAAVN_DIRECT = 'https://www.jiosaavn.com/api.php'

def _parse_jiosaavn_image(song):
    """Extract best image from JioSaavn direct API response"""
    img = song.get('image', '')
    if isinstance(img, str) and img.startswith('http'):
        return img.replace('150x150', '500x500').replace('50x50', '500x500')
    return ''

def _parse_jiosaavn_url(song):
    """Extract encrypted_media_url from JioSaavn direct API"""
    # Direct API returns encrypted_media_url — we need to decrypt
    # But also returns media_preview_url as fallback
    enc = song.get('more_info', {}).get('encrypted_media_url', '')
    prev = song.get('more_info', {}).get('media_preview_url', '')
    # Return preview URL directly — it's a working MP3
    if prev and prev.startswith('http'):
        return prev, 'preview'
    return None, None

def fetch_from_jiosaavn_direct(query, limit=20):
    """
    Fetch songs directly from JioSaavn's internal API.
    Returns list of normalized song dicts.
    """
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
        r = requests.get(
            JIOSAAVN_DIRECT,
            params=params,
            timeout=10,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer':    'https://www.jiosaavn.com/',
                'Origin':     'https://www.jiosaavn.com',
            }
        )
        if r.status_code != 200:
            log.warning(f"[JioSaavn Direct] HTTP {r.status_code}")
            return []

        data = r.json()
        songs_raw = data.get('results', [])
        if not songs_raw:
            return []

        results = []
        for song in songs_raw:
            more_info = song.get('more_info', {})

            # Multiple URL sources — pick best available
            # 1. encrypted_media_url (320kbps but needs decryption — skip for now)
            # 2. media_preview_url (128kbps, directly playable)
            preview_url = more_info.get('media_preview_url', '')
            vlink       = song.get('vlink', '')  # sometimes has direct url

            # Pick working URL
            best_url = None
            quality  = 'unknown'

            if preview_url and preview_url.startswith('http'):
                best_url = preview_url
                quality  = '128kbps'

            if not best_url:
                continue

            title  = song.get('title', '') or song.get('song', '')
            artist = song.get('primary_artists', '') or song.get('singers', '')
            image  = _parse_jiosaavn_image(song)
            year   = _safe_year(song.get('year', ''))
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
                # For fetch_saavn_parallel compat
                'title':            title,
                'artist':           artist,
                'image':            image,
                'score':            1.0,
                '_raw_urls':        [{'url': best_url, 'quality': quality}],
                'success':          True,
            })

        log.info(f"[JioSaavn Direct] ✓ {len(results)} songs for '{query}'")
        return results

    except Exception as e:
        log.warning(f"[JioSaavn Direct] Error: {e}")
        return []

def fetch_from_jiosaavn_direct_single(query, min_score=0.3):
    """Single song fetch from JioSaavn direct — for godmode chain"""
    songs = fetch_from_jiosaavn_direct(query, limit=10)
    if not songs:
        return None

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
# SAAVN MIRROR FETCH (unchanged core logic)
# ═══════════════════════════════════════════════════════════════
def fetch_from_mirror(mirror, query, min_score=0.4):
    if not is_mirror_alive(mirror):
        return None

    endpoints = ['/api/search/songs', '/api/search', '/search/songs']

    for endpoint in endpoints:
        try:
            r = requests.get(
                f'{mirror}{endpoint}',
                params={'query': query, 'q': query, 'limit': 10},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200:
                continue

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
                if not has_word_match(query, song_title):
                    continue
                score = title_score(query, song_title, song_artist)
                if score > best_score:
                    best_score = score
                    best_song  = song

            if not best_song or best_score < min_score:
                continue

            raw_urls = best_song.get('downloadUrl') or best_song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]

            best_url, quality = pick_best_quality(raw_urls)
            if not best_url:
                continue

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

    # ── STEP A: JioSaavn Direct (primary) ───────────────────────
    direct_future = _saavn_executor.submit(fetch_from_jiosaavn_direct_single, query, threshold)

    # ── STEP B: Mirrors in parallel (backup) ────────────────────
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
                if result:
                    all_results.append(result)
            except Exception as e:
                log.warning(f"[Parallel] Future error: {e}")
    except FuturesTimeout:
        log.warning("[Parallel] Some mirrors timed out")

    if not all_results:
        return None

    def result_rank(r):
        score   = r.get('score', 0)
        quality = r.get('quality', '')
        return score + (0.05 if '320' in str(quality) else 0)

    all_results.sort(key=result_rank, reverse=True)
    best = all_results[0]
    log.info(f"[Parallel] Best -> '{best['title']}' score={best.get('score',0)} quality={best['quality']}")
    return best

# ═══════════════════════════════════════════════════════════════
# /api/songs — Homepage song listing
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q   = request.args.get('q', 'bollywood hits').strip()
    era = request.args.get('era', '').strip()

    is_90s      = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    results = []

    # ── Primary: JioSaavn Direct ─────────────────────────────────
    direct_songs = fetch_from_jiosaavn_direct(search_term, limit=40)
    results.extend(direct_songs)
    log.info(f"[Songs] Direct got {len(direct_songs)} songs for '{search_term}'")

    # ── Backup: Mirrors (if direct gave less than 10) ─────────────
    if len(results) < 10:
        alive_mirrors = [m for m in SAAVN_MIRRORS if is_mirror_alive(m)]
        if not alive_mirrors:
            alive_mirrors = SAAVN_MIRRORS[:]

        for mirror in alive_mirrors[:3]:
            try:
                r = requests.get(
                    f'{mirror}/api/search/songs',
                    params={'query': search_term, 'limit': 40},
                    timeout=8,
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                if r.status_code != 200:
                    continue
                data = r.json()
                songs = (
                    data.get('data', {}).get('results') or
                    data.get('results') or
                    data.get('songs', {}).get('results') or []
                )
                for song in songs:
                    raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                    if not raw_urls:
                        continue
                    best_url, quality = pick_best_quality(raw_urls if isinstance(raw_urls, list) else [{'url': raw_urls, 'quality': 'unknown'}])
                    if not best_url:
                        continue

                    title  = song.get('name') or song.get('title', '')
                    artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                    image  = pick_image(song)
                    year   = _safe_year(song.get('releaseDate') or song.get('year', ''))

                    results.append({
                        'trackId':          song.get('id', random.randint(10000, 99999)),
                        'trackName':        title,
                        'artistName':       artist,
                        'artworkUrl100':    image,
                        'artworkUrl600':    image,
                        'url':              best_url,
                        'previewUrl':       best_url,
                        'quality':          quality,
                        'releaseDate':      str(year),
                        'primaryGenreName': 'Bollywood',
                        'source':           'saavn',
                    })
                if results:
                    break
            except Exception as e:
                log.warning(f"[Songs] Mirror {mirror} failed: {e}")
                continue

    if is_90s and results:
        filtered = [s for s in results if 1990 <= int(s.get('releaseDate') or 0) <= 1999]
        if len(filtered) >= 5:
            random.shuffle(filtered)
            return jsonify({'results': filtered[:30]})

    random.shuffle(results)
    return jsonify({'results': results[:30]})

# ═══════════════════════════════════════════════════════════════
# /api/songs/90s
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed    = random.choice(NINETIES_SEEDS)
    results = []

    alive_mirrors = [m for m in SAAVN_MIRRORS if is_mirror_alive(m)]
    if not alive_mirrors:
        alive_mirrors = SAAVN_MIRRORS[:]

    for mirror in alive_mirrors[:3]:
        try:
            r = requests.get(
                f'{mirror}/api/search/songs',
                params={'query': seed, 'limit': 40},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200:
                continue
            data = r.json()
            songs = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or []
            )
            for song in songs:
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                if not raw_urls:
                    continue
                best_url, quality = pick_best_quality(raw_urls if isinstance(raw_urls, list) else [{'url': raw_urls, 'quality': 'unknown'}])
                if not best_url:
                    continue

                title  = song.get('name') or song.get('title', '')
                artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                image  = pick_image(song)
                year   = _safe_year(song.get('releaseDate') or song.get('year', ''))

                results.append({
                    'trackId':       song.get('id', random.randint(10000, 99999)),
                    'trackName':     title,
                    'artistName':    artist,
                    'artworkUrl100': image,
                    'artworkUrl600': image,
                    'url':           best_url,
                    'previewUrl':    best_url,
                    'quality':       quality,
                    'releaseDate':   str(year),
                    'source':        'saavn',
                })
            if results:
                break
        except Exception as e:
            log.warning(f"[90s] Mirror {mirror} failed: {e}")
            continue

    # YT fallback for 90s if Saavn thin
    if len(results) < 5:
        log.info("[90s] Saavn thin — Invidious try")
        inv_url = fetch_via_invidious(seed, '', '90s')
        if inv_url:
            results.append({
                'trackId':       random.randint(10000, 99999),
                'trackName':     seed,
                'artistName':    '90s Bollywood',
                'artworkUrl100': '',
                'artworkUrl600': '',
                'url':           inv_url,
                'quality':       'full',
                'releaseDate':   '1995',
                'source':        'invidious',
            })

    filtered = [s for s in results if 1990 <= int(s.get('releaseDate') or 0) <= 1999]
    if len(filtered) < 5:
        filtered = results
    random.shuffle(filtered)
    return jsonify({'results': filtered[:30], 'seed': seed})

# ═══════════════════════════════════════════════════════════════
# /api/song — GODMODE MAIN ENDPOINT
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
# /api/saavn — Direct Saavn (kept for frontend compat)
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

    # Saavn miss — godmode fallback
    log.info(f"[Saavn] Miss — godmode fallback for '{q}'")
    result = godmode_fetch(q, artist, fallback, True, low_quality, token)
    return jsonify(result)

# ═══════════════════════════════════════════════════════════════
# /api/yt — Direct YT endpoint (now uses Invidious first)
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

    # Invidious first
    inv_url = fetch_via_invidious(q, artist, genre)
    if inv_url:
        return jsonify({'success': True, 'url': inv_url, 'source': 'invidious', 'title': q, 'artist': artist, 'token': token})

    # Piped second
    piped_url = fetch_via_piped(q, artist, genre)
    if piped_url:
        return jsonify({'success': True, 'url': piped_url, 'source': 'piped', 'title': q, 'artist': artist, 'token': token})

    # yt-dlp last
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
                    if chunk:
                        yield chunk
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

        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
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
    log.info(f"🎵 Aurum Godmode | Port {port} | yt-dlp: {YT_DLP_AVAILABLE}")
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
