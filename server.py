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
from urllib.parse import urlparse
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
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
]

# ── YT ke liye bhi domains allow karo stream proxy mein ────────
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
]

QUALITY_RANK = {
    '320kbps': 7, '320': 7,
    '160kbps': 5, '160': 5,
    '96kbps':  3, '96':  3,
    '48kbps':  2, '48':  2,
    '12kbps':  1, '12':  1,
}

# ── 90s seeds — Saavn search ke liye ───────────────────────────
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

# ── Ye genres Saavn pe nahi milte — seedha YT pe bhejo ────────
FORCE_YT_KEYWORDS = [
    'nepali', 'nepal', 'bhojpuri', 'maithili', 'awadhi',
    'pahadi', 'haryanvi', 'chhattisgarhi', 'garhwali',
    'kumaoni', 'dogri', 'odia', 'assamese',
]

# ── Mirror health tracking ──────────────────────────────────────
_mirror_failures = {}
_MIRROR_COOLDOWN = 300  # 5 min baad dubara try karega

# ── YT cache + semaphore ────────────────────────────────────────
_yt_semaphore = threading.Semaphore(3)
_yt_url_cache = {}
_YT_CACHE_TTL = 3600  # 1 hour

# ── Thread pools ────────────────────────────────────────────────
_saavn_executor = ThreadPoolExecutor(max_workers=len(SAAVN_MIRRORS), thread_name_prefix='saavn')
_yt_executor    = ThreadPoolExecutor(max_workers=3, thread_name_prefix='ytdlp')

atexit.register(_saavn_executor.shutdown, wait=False)
atexit.register(_yt_executor.shutdown,    wait=False)

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
# MIRROR HEALTH
# ═══════════════════════════════════════════════════════════════
def is_mirror_alive(mirror):
    fail_ts = _mirror_failures.get(mirror)
    if fail_ts and (time.time() - fail_ts) < _MIRROR_COOLDOWN:
        return False
    return True

def mark_mirror_failed(mirror):
    _mirror_failures[mirror] = time.time()
    log.warning(f"[Mirror] Dead for {_MIRROR_COOLDOWN}s: {mirror}")

def mark_mirror_ok(mirror):
    _mirror_failures.pop(mirror, None)

# ═══════════════════════════════════════════════════════════════
# HELPERS — Query cleaning
# ═══════════════════════════════════════════════════════════════
def clean_query(text):
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\((OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?)\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def build_query_variants(title, artist='', fallback=''):
    title_c  = clean_query(title)
    artist_c = clean_query(artist)  if artist   else ''
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

# ═══════════════════════════════════════════════════════════════
# SEARCH MATCHING — FIXED (1-word accurate)
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
    # SHORT words (3 char ya kam) ke liye exact prefix kaafi hai
    if len(qw) <= 3:
        return 1.0 if tw.startswith(qw) else 0.0
    ratio = 1.0 - (levenshtein(qw, tw) / max_len)
    return ratio if ratio >= 0.55 else 0.0  # FIX: 0.65 → 0.55

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
    """
    FIX: Thresholds loose kiye — 1-2 word search pe bhi result milega
    Pehle: <=2 → 0.25, <=5 → 0.45, else → 0.60
    Ab:    <=3 → 0.10, <=6 → 0.30, else → 0.45
    """
    length = len(normalize(query).replace(' ', ''))
    if length <= 3:   return 0.10   # "Dil", "Jai", "Tum"
    elif length <= 6: return 0.30   # "Tum Hi", "Dilwale"
    else:             return 0.45   # longer queries

def has_word_match(query, song_title):
    """
    FIX: Short words ke liye prefix match use karo, threshold 0.60→0.50
    """
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words: return False
    for qw in q_words:
        if len(qw) <= 3:
            # Short word — sirf prefix check
            for tw in t_words:
                if tw.startswith(qw):
                    return True
            continue
        for tw in t_words:
            if fuzzy_word_match(qw, tw) >= 0.50:  # FIX: 0.60 → 0.50
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
# YT-DLP — Full song fetch with cache
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

def fetch_yt_url(title, artist='', genre_hint=''):
    """
    YT se full song URL fetch karo.
    genre_hint = 'nepali'/'bhojpuri' etc. → search query tailor hoga
    """
    if not YT_DLP_AVAILABLE:
        return None

    cache_key = f"{normalize(title)}|{normalize(artist)}|{genre_hint}"
    cached = _yt_cache_get(cache_key)
    if cached:
        log.info(f"[YT] Cache hit: {title}")
        return cached

    if not _yt_semaphore.acquire(blocking=True, timeout=3):
        log.warning("[YT] Semaphore busy — skipping")
        return None

    try:
        # Genre ke hisaab se search query banao
        if genre_hint:
            search_q = f"{title} {genre_hint} full song official".strip()
        elif artist:
            search_q = f"{title} {artist} official audio full song".strip()
        else:
            search_q = f"{title} official audio full song".strip()

        ydl_opts = {
            'format':             'bestaudio[ext=m4a]/bestaudio/best',
            'quiet':              True,
            'no_warnings':        True,
            'noplaylist':         True,
            'extract_flat':       False,
            'skip_download':      True,
            'socket_timeout':     10,
            'playlist_items':     '1',
            'geo_bypass':         True,
            'http_headers': {
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/124.0.0.0 Safari/537.36'
                )
            },
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{search_q}", download=False)
            if not info: return None
            entries = info.get('entries') or [info]
            if not entries: return None
            entry = entries[0]
            if not entry: return None

            # Duration check — 60 sec se kam = preview/short, skip karo
            duration = entry.get('duration') or 0
            if duration < 60:
                log.warning(f"[YT] Too short ({duration}s), skipping: {title}")
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
                log.info(f"[YT] ✓ {entry.get('title', title)[:50]} ({duration}s)")
                return url
            return None

    except Exception as e:
        log.warning(f"[YT] Failed '{title}': {type(e).__name__}: {e}")
        return None
    finally:
        _yt_semaphore.release()

# ═══════════════════════════════════════════════════════════════
# SAAVN MIRROR FETCH
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
    threshold = dynamic_min_score(query)
    alive_mirrors = [m for m in SAAVN_MIRRORS if is_mirror_alive(m)]

    if not alive_mirrors:
        log.warning("[Saavn] All mirrors dead — resetting")
        _mirror_failures.clear()
        alive_mirrors = SAAVN_MIRRORS[:]

    futures = {
        _saavn_executor.submit(fetch_from_mirror, m, query, threshold): m
        for m in alive_mirrors
    }
    all_results = []

    try:
        for future in as_completed(futures, timeout=12):
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
    log.info(f"[Parallel] Best → '{best['title']}' score={best['score']} quality={best['quality']}")
    return best

# ═══════════════════════════════════════════════════════════════
# /api/songs — Song listing for homepage
# Sirf metadata return karta hai (title, artist, image, query)
# Frontend /api/song se full URL fetch karega
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q   = request.args.get('q', 'bollywood hits').strip()
    era = request.args.get('era', '').strip()

    is_90s      = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    # Saavn se metadata fetch karo
    results = []
    alive_mirrors = [m for m in SAAVN_MIRRORS if is_mirror_alive(m)]
    if not alive_mirrors:
        alive_mirrors = SAAVN_MIRRORS[:]

    for mirror in alive_mirrors[:3]:  # Pehle 3 mirrors enough hain listing ke liye
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
                    'url':              best_url,      # Full song URL — preview nahi
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
# /api/songs/90s — 90s dedicated endpoint
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
                    'quality':       quality,
                    'releaseDate':   str(year),
                    'source':        'saavn',
                })
            if results:
                break
        except Exception as e:
            log.warning(f"[90s] Mirror {mirror} failed: {e}")
            continue

    # Agar Saavn se 90s songs kam mile to YT se bhi try karo
    if len(results) < 5 and YT_DLP_AVAILABLE:
        log.info(f"[90s] Saavn se kam results, YT try kar raha hun")
        try:
            yt_seed = f"90s {seed} full song"
            future  = _yt_executor.submit(fetch_yt_url, yt_seed, '', '90s bollywood')
            yt_url  = future.result(timeout=20)
            if yt_url:
                results.append({
                    'trackId':       random.randint(10000, 99999),
                    'trackName':     seed,
                    'artistName':    '90s Bollywood',
                    'artworkUrl100': '',
                    'artworkUrl600': '',
                    'url':           yt_url,
                    'quality':       'full',
                    'releaseDate':   '1995',
                    'source':        'youtube',
                })
        except Exception as e:
            log.warning(f"[90s] YT fallback failed: {e}")

    filtered = [s for s in results if 1990 <= int(s.get('releaseDate') or 0) <= 1999]
    if len(filtered) < 5:
        filtered = results
    random.shuffle(filtered)
    return jsonify({'results': filtered[:30], 'seed': seed})

# ═══════════════════════════════════════════════════════════════
# /api/song — MAIN SMART ENDPOINT
# Saavn first → YT fallback
# Nepali/Bhojpuri → seedha YT
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

    # Nepali/Bhojpuri → auto force YT (Saavn pe nahi milta)
    genre_hint = ''
    q_lower = q.lower()
    for kw in FORCE_YT_KEYWORDS:
        if kw in q_lower or (artist and kw in artist.lower()):
            force_yt   = True
            genre_hint = kw
            log.info(f"[Smart] Auto force_yt for genre: {kw}")
            break

    saavn_result = None

    # Saavn try karo (force_yt nahi hai to)
    if not force_yt:
        for query in build_query_variants(q, artist, fallback):
            saavn_result = fetch_saavn_parallel(query)
            if saavn_result and not has_word_match(q, saavn_result['title']):
                log.warning(f"[Smart] Reject '{saavn_result['title']}' for query '{q}'")
                saavn_result = None
            if saavn_result:
                break

    # Saavn se mila
    if saavn_result:
        if low_quality:
            low_url, low_q = _pick_low_quality(saavn_result.get('_raw_urls', []))
            if low_url:
                saavn_result['url']     = low_url
                saavn_result['quality'] = low_q
        saavn_result.pop('_raw_urls', None)
        log.info(f"[Smart] Saavn ✓ '{q}' → '{saavn_result['title']}'")
        return jsonify({'success': True, 'token': token, **saavn_result})

    # YT fallback
    if YT_DLP_AVAILABLE:
        log.info(f"[Smart] Saavn miss — YT try for '{q}'")
        try:
            future = _yt_executor.submit(fetch_yt_url, q, artist, genre_hint)
            yt_url = future.result(timeout=25)
            if yt_url:
                log.info(f"[Smart] YT ✓ '{q}'")
                return jsonify({
                    'success': True,
                    'url':     yt_url,
                    'source':  'youtube',
                    'title':   q,
                    'artist':  artist,
                    'quality': 'full',
                    'image':   '',
                    'token':   token,
                })
        except FuturesTimeout:
            log.warning(f"[Smart] YT timeout for '{q}'")
        except Exception as e:
            log.error(f"[Smart] YT error: {e}")

    log.info(f"[Smart] ✗ No result for '{q}'")
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/saavn — Direct Saavn endpoint (frontend compatibility)
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
            log.info(f"[Saavn] ✓ '{q}' → '{result['title']}' quality={result['quality']}")
            return jsonify({'success': True, 'token': token, **result})

    log.info(f"[Saavn] ✗ No match — '{q}'")
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/yt — Direct YT endpoint
# ═══════════════════════════════════════════════════════════════
@app.route('/api/yt')
@limiter.limit("30 per minute")
def get_yt_song():
    q      = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    token  = request.args.get('token', '').strip()

    if not q:
        return jsonify({'success': False, 'error': 'Missing query', 'token': token})

    if not YT_DLP_AVAILABLE:
        return jsonify({'success': False, 'error': 'yt-dlp not installed', 'token': token}), 503

    try:
        # Genre detect karo
        genre_hint = ''
        for kw in FORCE_YT_KEYWORDS:
            if kw in q.lower():
                genre_hint = kw
                break

        future = _yt_executor.submit(fetch_yt_url, q, artist, genre_hint)
        url    = future.result(timeout=25)

        if url:
            return jsonify({'success': True, 'url': url, 'source': 'youtube', 'title': q, 'artist': artist, 'token': token})
        return jsonify({'success': False, 'url': None, 'token': token})

    except FuturesTimeout:
        return jsonify({'success': False, 'error': 'timeout', 'token': token})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/stream — Proxy streamer (Saavn CDN + googlevideo)
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
# /api/download — Direct download endpoint
# ═══════════════════════════════════════════════════════════════
@app.route('/api/download')
@limiter.limit("50 per minute")
def download_song():
    q      = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()

    if not q:
        return jsonify({'success': False, 'error': 'Missing query'}), 400

    # Smart endpoint se URL lo
    result = None
    for query in build_query_variants(q, artist, ''):
        result = fetch_saavn_parallel(query)
        if result and has_word_match(q, result['title']):
            break
        result = None

    # YT fallback
    if not result and YT_DLP_AVAILABLE:
        genre_hint = ''
        for kw in FORCE_YT_KEYWORDS:
            if kw in q.lower():
                genre_hint = kw
                break
        try:
            future = _yt_executor.submit(fetch_yt_url, q, artist, genre_hint)
            yt_url = future.result(timeout=25)
            if yt_url:
                result = {'url': yt_url, 'title': q, 'artist': artist, 'quality': 'full'}
        except Exception as e:
            log.warning(f"[Download] YT failed: {e}")

    if not result:
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
        resp_headers['Content-Disposition']          = f'attachment; filename="{filename}"'
        resp_headers['Access-Control-Allow-Origin']  = '*'
        resp_headers['Cache-Control']                = 'no-store'

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
    alive = [m for m in SAAVN_MIRRORS if is_mirror_alive(m)]
    return jsonify({
        'status':        'ok',
        'yt_dlp':        YT_DLP_AVAILABLE,
        'mirrors_alive': len(alive),
        'mirrors_total': len(SAAVN_MIRRORS),
        'yt_cache_size': len(_yt_url_cache),
    })

# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    log.info(f"Server starting on port {port} | yt-dlp: {YT_DLP_AVAILABLE}")
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
