from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import os
import re
import logging
import random
import sqlite3
import string
import secrets
import hmac
import hashlib
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Google JWT verification
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, 'aurum_cloud.db')

# ── Read once at startup; crash early if missing ──────────────────────────────
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
ADMIN_KEY        = os.environ.get('ADMIN_KEY', '')

if not GOOGLE_CLIENT_ID:
    raise RuntimeError('GOOGLE_CLIENT_ID env var is required')
if not ADMIN_KEY:
    raise RuntimeError('ADMIN_KEY env var is required — do not use a default')

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

limiter     = Limiter(get_real_ip, app=app, default_limits=[], storage_uri="memory://")
_executor   = ThreadPoolExecutor(max_workers=8)
_google_req = google_requests.Request()

# ═══════════════════════════════════════════════════════════════
# JWT HELPERS
# ═══════════════════════════════════════════════════════════════
def _verify_google_jwt(credential: str) -> dict | None:
    try:
        payload = id_token.verify_oauth2_token(
            credential, _google_req, GOOGLE_CLIENT_ID, clock_skew_in_seconds=10,
        )
        if payload.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
            log.warning('[Auth] JWT iss mismatch')
            return None
        return payload
    except Exception as e:
        log.warning(f'[Auth] JWT verify failed: {e}')
        return None

def _extract_bearer_sub(auth_header: str) -> str | None:
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    token = auth_header[7:].strip()
    if not token:
        return None
    payload = _verify_google_jwt(token)
    if not payload:
        return None
    return payload.get('sub', '') or None

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                google_sub     TEXT PRIMARY KEY,
                name           TEXT,
                email          TEXT,
                picture        TEXT,
                ghost_pin_hash TEXT,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS playback_state (
                google_sub  TEXT PRIMARY KEY,
                song_id     TEXT,
                song_title  TEXT,
                artist      TEXT,
                art_url     TEXT,
                progress    REAL DEFAULT 0,
                device      TEXT DEFAULT 'mobile',
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tv_pairing (
                pairing_code  TEXT PRIMARY KEY,
                tv_session_id TEXT,
                google_sub    TEXT,
                expires_at    TIMESTAMP
            )
        ''')
        conn.commit()

init_db()

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

# Cobalt instances — tried in order, first success wins
COBALT_INSTANCES = [
    'https://api.cobalt.tools',
    'https://cobalt.api.lostluma.dev',
    'https://cobalt.ggtyler.dev',
]

ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'cf.saavncdn.com', 'aac.saavncdn.com', 'static.saavncdn.com',
    'c.saavncdn.com', 'h.saavncdn.com',
    'googlevideo.com', 'youtube.com', 'ytimg.com',
    'rr1.sn-', 'rr2.sn-', 'rr3.sn-', 'rr4.sn-',
    'r1.sn-', 'r2.sn-', 'r3.sn-', 'r4.sn-',
    'r5.sn-', 'r6.sn-', 'r7.sn-',
    # Cobalt CDN domains
    'cobalt.tools', 'lostluma.dev', 'ggtyler.dev',
    'cdn.discordapp.com', 'media.discordapp.net',
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

# YouTube trending music queries (India-focused)
YT_TRENDING_QUERIES = [
    'trending hindi songs 2024',
    'bollywood hits trending',
    'top india music chart',
    'new hindi songs trending',
    'viral bollywood songs',
]

# ═══════════════════════════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════════════════════════
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
    title_c      = clean_query(title)
    artist_c     = clean_query(artist) if artist else ''
    fb_c         = clean_query(fallback) if fallback else ''
    artist_first = artist_c.split()[0] if artist_c else ''
    seen, variants = set(), []
    def add(v):
        v = re.sub(r'\s+', ' ', v).strip()
        if v and v not in seen:
            seen.add(v); variants.append(v)
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
    if length <= 2:    return 0.20
    elif length <= 5:  return 0.35
    elif length <= 10: return 0.50
    else:              return 0.55

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
                    best_score = score; best_song = song
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
# COBALT — YouTube audio extraction (NEW)
# ═══════════════════════════════════════════════════════════════
def fetch_from_cobalt(video_id: str) -> dict | None:
    """
    Extract audio-only stream URL from YouTube via Cobalt API.
    Tries multiple Cobalt instances for reliability.
    Returns dict with url, quality, source='cobalt' or None.
    """
    if not video_id:
        return None

    yt_url = f'https://www.youtube.com/watch?v={video_id}'
    payload = {
        'url': yt_url,
        'downloadMode': 'audio',
        'audioFormat': 'mp3',
        'audioBitrate': '128',
    }
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0',
    }

    for instance in COBALT_INSTANCES:
        try:
            r = requests.post(
                f'{instance}/',
                json=payload,
                headers=headers,
                timeout=12,
            )
            if r.status_code not in (200, 201):
                log.warning(f"[Cobalt {instance}] HTTP {r.status_code}")
                continue

            data   = r.json()
            status = data.get('status', '')

            # Direct stream URL
            if status in ('stream', 'tunnel', 'redirect') and data.get('url'):
                log.info(f"[Cobalt] ✓ videoId={video_id} via {instance} status={status}")
                return {
                    'url':     data['url'],
                    'quality': '128kbps',
                    'source':  'cobalt',
                }

            # Picker response (multiple streams) — pick first audio
            if status == 'picker' and data.get('picker'):
                for item in data['picker']:
                    if item.get('url'):
                        log.info(f"[Cobalt] ✓ picker videoId={video_id} via {instance}")
                        return {
                            'url':     item['url'],
                            'quality': '128kbps',
                            'source':  'cobalt',
                        }

            log.warning(f"[Cobalt {instance}] Unexpected status: {status} — {data.get('error', {})}")

        except Exception as e:
            log.warning(f"[Cobalt {instance}] {e}")
            continue

    log.info(f"[Cobalt] ✗ All instances failed for videoId={video_id}")
    return None

# ═══════════════════════════════════════════════════════════════
# PIPED — YouTube full songs
# ═══════════════════════════════════════════════════════════════
def fetch_from_piped(query, title='', artist=''):
    search_q = f"{title} {artist}".strip() if title else query
    for instance in PIPED_INSTANCES:
        try:
            r = requests.get(f'{instance}/search', params={'q': search_q, 'filter': 'music_songs'}, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200: continue
            results = r.json().get('items', [])
            if not results: continue
            best = None; best_score = -1
            for item in results[:5]:
                if item.get('type') != 'stream': continue
                item_title  = item.get('title', '')
                item_artist = item.get('uploaderName', '')
                if not has_word_match(query, item_title): continue
                score = title_score(query, item_title, item_artist)
                if score > best_score:
                    best_score = score; best = item
            if not best or best_score < 0.3: continue
            video_id = best.get('url', '').replace('/watch?v=', '').strip()
            if not video_id: continue
            sr = requests.get(f'{instance}/streams/{video_id}', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if sr.status_code != 200: continue
            audio_streams = sr.json().get('audioStreams', [])
            if not audio_streams: continue
            best_audio = None; best_bitrate = 0
            for stream in audio_streams:
                bitrate = stream.get('bitrate', 0)
                fmt     = stream.get('format', '').lower()
                if bitrate > best_bitrate or (bitrate == best_bitrate and 'm4a' in fmt):
                    best_bitrate = bitrate; best_audio = stream
            if not best_audio or not best_audio.get('url'): continue
            quality_label = f"{best_bitrate // 1000}kbps" if best_bitrate > 0 else 'unknown'
            log.info(f"[Piped] ✓ '{best.get('title')}' via {instance}")
            return {
                'url': best_audio['url'], 'quality': quality_label,
                'title': best.get('title', title), 'artist': best.get('uploaderName', artist),
                'image': best.get('thumbnail', ''), 'score': round(best_score, 3),
                'source': 'piped', 'videoId': video_id,
            }
        except Exception as e:
            log.warning(f"[Piped {instance}] {e}"); continue
    return None

# ═══════════════════════════════════════════════════════════════
# INVIDIOUS — YouTube fallback
# ═══════════════════════════════════════════════════════════════
def fetch_from_invidious(query, title='', artist=''):
    search_q = f"{title} {artist}".strip() if title else query
    for instance in INVIDIOUS_INSTANCES:
        try:
            r = requests.get(f'{instance}/api/v1/search', params={'q': search_q, 'type': 'video', 'page': 1}, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200: continue
            results = r.json()
            if not results: continue
            best = None; best_score = -1
            for item in results[:5]:
                item_title  = item.get('title', '')
                item_author = item.get('author', '')
                if not has_word_match(query, item_title): continue
                score = title_score(query, item_title, item_author)
                if score > best_score:
                    best_score = score; best = item
            if not best or best_score < 0.3: continue
            video_id = best.get('videoId', '')
            if not video_id: continue
            vr = requests.get(f'{instance}/api/v1/videos/{video_id}', params={'fields': 'adaptiveFormats,title,author'}, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if vr.status_code != 200: continue
            formats = vr.json().get('adaptiveFormats', [])
            audio_formats = [f for f in formats if f.get('type', '').startswith('audio')]
            if not audio_formats: continue
            best_fmt = max(audio_formats, key=lambda f: f.get('bitrate', 0))
            if not best_fmt.get('url'): continue
            bitrate = best_fmt.get('bitrate', 0)
            quality_label = f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown'
            log.info(f"[Invidious] ✓ '{best.get('title')}' via {instance}")
            return {
                'url': best_fmt['url'], 'quality': quality_label,
                'title': best.get('title', title), 'artist': best.get('author', artist),
                'image': f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
                'score': round(best_score, 3), 'source': 'invidious', 'videoId': video_id,
            }
        except Exception as e:
            log.warning(f"[Invidious {instance}] {e}"); continue
    return None

# ═══════════════════════════════════════════════════════════════
# YOUTUBE SEARCH via Piped (no API key needed) (NEW)
# ═══════════════════════════════════════════════════════════════
def _piped_search_raw(query: str, limit: int = 20) -> list:
    """
    Search YouTube via Piped instances.
    Returns list of raw video items with videoId, title, uploaderName, thumbnail, duration.
    """
    for instance in PIPED_INSTANCES:
        try:
            r = requests.get(
                f'{instance}/search',
                params={'q': query, 'filter': 'music_songs'},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if r.status_code != 200:
                continue
            items = r.json().get('items', [])
            results = []
            for item in items:
                if item.get('type') != 'stream':
                    continue
                raw_id = item.get('url', '')
                video_id = raw_id.replace('/watch?v=', '').strip()
                if not video_id:
                    continue
                duration_s = item.get('duration', 0) or 0
                results.append({
                    'videoId':      video_id,
                    'title':        item.get('title', ''),
                    'artist':       item.get('uploaderName', ''),
                    'thumbnail':    item.get('thumbnail', f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg'),
                    'duration':     duration_s,
                    'durationText': _fmt_seconds(duration_s),
                    'views':        item.get('views', 0),
                })
                if len(results) >= limit:
                    break
            if results:
                log.info(f"[YT Search] ✓ '{query}' → {len(results)} results via {instance}")
                return results
        except Exception as e:
            log.warning(f"[YT Search {instance}] {e}")
            continue
    return []

def _invidious_search_raw(query: str, limit: int = 20) -> list:
    """
    Fallback YouTube search via Invidious.
    """
    for instance in INVIDIOUS_INSTANCES:
        try:
            r = requests.get(
                f'{instance}/api/v1/search',
                params={'q': query, 'type': 'video', 'page': 1},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if r.status_code != 200:
                continue
            items = r.json()
            results = []
            for item in items:
                video_id = item.get('videoId', '')
                if not video_id:
                    continue
                duration_s = item.get('lengthSeconds', 0) or 0
                results.append({
                    'videoId':      video_id,
                    'title':        item.get('title', ''),
                    'artist':       item.get('author', ''),
                    'thumbnail':    f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg',
                    'duration':     duration_s,
                    'durationText': _fmt_seconds(duration_s),
                    'views':        item.get('viewCount', 0),
                })
                if len(results) >= limit:
                    break
            if results:
                log.info(f"[YT Search Inv] ✓ '{query}' → {len(results)} results via {instance}")
                return results
        except Exception as e:
            log.warning(f"[YT Search Inv {instance}] {e}")
            continue
    return []

def _fmt_seconds(s: int) -> str:
    s = int(s or 0)
    m, sec = divmod(s, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"

# ═══════════════════════════════════════════════════════════════
# ITUNES SEARCH
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q           = request.args.get('q', 'top songs').strip()
    era         = request.args.get('era', '').strip()
    is_90s      = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q
    try:
        r = requests.get('https://itunes.apple.com/search', params={'term': search_term, 'media': 'music', 'entity': 'song', 'limit': 50, 'country': 'IN'}, timeout=15)
        r.raise_for_status()
        results = r.json().get('results', [])
        if is_90s:
            filtered = [s for s in results if s.get('previewUrl') and 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
            if len(filtered) < 5: filtered = [s for s in results if s.get('previewUrl')]
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
        r = requests.get('https://itunes.apple.com/search', params={'term': seed, 'media': 'music', 'entity': 'song', 'limit': 50, 'country': 'IN'}, timeout=15)
        r.raise_for_status()
        results = r.json().get('results', [])
        filtered = [s for s in results if s.get('previewUrl') and 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        if len(filtered) < 5: filtered = [s for s in results if s.get('previewUrl')]
        random.shuffle(filtered)
        return jsonify({'results': filtered[:30], 'seed': seed})
    except Exception as e:
        log.error(f"[iTunes/90s] Seed '{seed}' failed: {e}")
        return jsonify({'results': [], 'error': str(e)})

# ═══════════════════════════════════════════════════════════════
# YOUTUBE ENDPOINTS (NEW)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/yt/search')
@limiter.limit("40 per minute")
def yt_search():
    """
    Search YouTube music. Returns list of video items.
    Query params: q (required), limit (optional, default 20)
    """
    q     = request.args.get('q', '').strip()
    limit = min(int(request.args.get('limit', 20)), 50)
    if not q:
        return jsonify({'success': False, 'results': [], 'error': 'Missing query'}), 400

    # Try Piped first, fallback to Invidious
    results = _piped_search_raw(q, limit)
    if not results:
        log.info(f"[YT Search] Piped failed → trying Invidious for '{q}'")
        results = _invidious_search_raw(q, limit)

    return jsonify({'success': True, 'results': results, 'query': q, 'count': len(results)})


@app.route('/api/yt/trending')
@limiter.limit("20 per minute")
def yt_trending():
    """
    Get trending YouTube music for India.
    Uses Piped /trending endpoint, falls back to search.
    """
    # Try Piped trending endpoint first
    for instance in PIPED_INSTANCES:
        try:
            r = requests.get(
                f'{instance}/trending',
                params={'region': 'IN'},
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if r.status_code != 200:
                continue
            items = r.json()
            results = []
            for item in items:
                if item.get('type') != 'stream':
                    continue
                raw_id   = item.get('url', '')
                video_id = raw_id.replace('/watch?v=', '').strip()
                if not video_id:
                    continue
                # Only include music-ish content (filter by duration 2-10 min)
                duration_s = item.get('duration', 0) or 0
                if not (120 <= duration_s <= 600):
                    continue
                results.append({
                    'videoId':      video_id,
                    'title':        item.get('title', ''),
                    'artist':       item.get('uploaderName', ''),
                    'thumbnail':    item.get('thumbnail', f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg'),
                    'duration':     duration_s,
                    'durationText': _fmt_seconds(duration_s),
                    'views':        item.get('views', 0),
                })
                if len(results) >= 30:
                    break
            if results:
                log.info(f"[YT Trending] ✓ {len(results)} results via {instance}")
                return jsonify({'success': True, 'results': results, 'source': 'piped_trending'})
        except Exception as e:
            log.warning(f"[YT Trending {instance}] {e}")
            continue

    # Fallback: search for trending queries
    log.info("[YT Trending] Piped trending failed → using search fallback")
    q       = random.choice(YT_TRENDING_QUERIES)
    results = _piped_search_raw(q, 20)
    if not results:
        results = _invidious_search_raw(q, 20)

    return jsonify({'success': bool(results), 'results': results, 'source': 'search_fallback', 'query': q})


@app.route('/api/yt/suggestions')
@limiter.limit("40 per minute")
def yt_suggestions():
    """
    Get related videos for a given videoId (for Up Next / autoplay).
    Query params: videoId (required)
    """
    video_id = request.args.get('videoId', '').strip()
    if not video_id:
        return jsonify({'success': False, 'results': [], 'error': 'Missing videoId'}), 400

    for instance in PIPED_INSTANCES:
        try:
            r = requests.get(
                f'{instance}/streams/{video_id}',
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if r.status_code != 200:
                continue
            data         = r.json()
            related_raw  = data.get('relatedStreams', [])
            results      = []
            for item in related_raw:
                if item.get('type') != 'stream':
                    continue
                raw_id   = item.get('url', '')
                vid      = raw_id.replace('/watch?v=', '').strip()
                if not vid:
                    continue
                duration_s = item.get('duration', 0) or 0
                results.append({
                    'videoId':      vid,
                    'title':        item.get('title', ''),
                    'artist':       item.get('uploaderName', ''),
                    'thumbnail':    item.get('thumbnail', f'https://img.youtube.com/vi/{vid}/mqdefault.jpg'),
                    'duration':     duration_s,
                    'durationText': _fmt_seconds(duration_s),
                    'views':        item.get('views', 0),
                })
                if len(results) >= 15:
                    break
            if results:
                log.info(f"[YT Suggestions] ✓ {len(results)} for videoId={video_id} via {instance}")
                return jsonify({'success': True, 'results': results})
        except Exception as e:
            log.warning(f"[YT Suggestions {instance}] {e}")
            continue

    return jsonify({'success': False, 'results': [], 'error': 'No suggestions found'})


@app.route('/api/yt/stream')
@limiter.limit("60 per minute")
def yt_stream_url():
    """
    Get audio stream URL for a YouTube videoId.
    Chain: Cobalt → Piped → Invidious
    Query params: videoId (required), title (optional), artist (optional)
    """
    video_id = request.args.get('videoId', '').strip()
    title    = request.args.get('title', '').strip()
    artist   = request.args.get('artist', '').strip()

    if not video_id:
        return jsonify({'success': False, 'error': 'Missing videoId'}), 400

    # ── Step 1: Cobalt (most reliable audio extraction) ──────
    cobalt_result = fetch_from_cobalt(video_id)
    if cobalt_result and cobalt_result.get('url'):
        log.info(f"[YT Stream] ✓ Cobalt — videoId={video_id}")
        return jsonify({
            'success':  True,
            'url':      f"/api/stream?url={quote(cobalt_result['url'], safe='')}",
            'quality':  cobalt_result['quality'],
            'source':   'cobalt',
            'videoId':  video_id,
            'title':    title,
            'artist':   artist,
            'image':    f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg',
        })

    # ── Step 2: Piped audio streams ──────────────────────────
    log.info(f"[YT Stream] Cobalt miss → Piped for videoId={video_id}")
    for instance in PIPED_INSTANCES:
        try:
            r = requests.get(
                f'{instance}/streams/{video_id}',
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if r.status_code != 200:
                continue
            audio_streams = r.json().get('audioStreams', [])
            if not audio_streams:
                continue
            best_audio   = None
            best_bitrate = 0
            for stream in audio_streams:
                bitrate = stream.get('bitrate', 0)
                fmt     = stream.get('format', '').lower()
                if bitrate > best_bitrate or (bitrate == best_bitrate and 'm4a' in fmt):
                    best_bitrate = bitrate
                    best_audio   = stream
            if best_audio and best_audio.get('url'):
                quality_label = f"{best_bitrate // 1000}kbps" if best_bitrate > 0 else 'unknown'
                log.info(f"[YT Stream] ✓ Piped — videoId={video_id} {quality_label} via {instance}")
                return jsonify({
                    'success': True,
                    'url':     f"/api/stream?url={quote(best_audio['url'], safe='')}",
                    'quality': quality_label,
                    'source':  'piped',
                    'videoId': video_id,
                    'title':   title,
                    'artist':  artist,
                    'image':   f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg',
                })
        except Exception as e:
            log.warning(f"[YT Stream Piped {instance}] {e}")
            continue

    # ── Step 3: Invidious ─────────────────────────────────────
    log.info(f"[YT Stream] Piped miss → Invidious for videoId={video_id}")
    for instance in INVIDIOUS_INSTANCES:
        try:
            r = requests.get(
                f'{instance}/api/v1/videos/{video_id}',
                params={'fields': 'adaptiveFormats,title,author'},
                timeout=10,
                headers={'User-Agent': 'Mozilla/5.0'},
            )
            if r.status_code != 200:
                continue
            formats       = r.json().get('adaptiveFormats', [])
            audio_formats = [f for f in formats if f.get('type', '').startswith('audio')]
            if not audio_formats:
                continue
            best_fmt = max(audio_formats, key=lambda f: f.get('bitrate', 0))
            if not best_fmt.get('url'):
                continue
            bitrate       = best_fmt.get('bitrate', 0)
            quality_label = f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown'
            log.info(f"[YT Stream] ✓ Invidious — videoId={video_id} via {instance}")
            return jsonify({
                'success': True,
                'url':     f"/api/stream?url={quote(best_fmt['url'], safe='')}",
                'quality': quality_label,
                'source':  'invidious',
                'videoId': video_id,
                'title':   title,
                'artist':  artist,
                'image':   f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg',
            })
        except Exception as e:
            log.warning(f"[YT Stream Invidious {instance}] {e}")
            continue

    log.info(f"[YT Stream] ✗ All sources failed for videoId={video_id}")
    return jsonify({'success': False, 'error': 'Could not extract audio stream', 'videoId': video_id})

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
            if result.get('score', 0) < 0.8: result = None
        if result:
            if low_quality:
                low_url, low_q = _pick_low_quality(result.get('_raw_urls', []))
                if low_url:
                    result['url'] = low_url; result['quality'] = low_q
            log.info(f"[Saavn] ✓ q='{q}' → '{result['title']}' quality={result['quality']} score={result['score']}")
            return jsonify({'success': True, 'token': token, **result})
    log.info(f"[Saavn] ✗ No match — q='{q}'")
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# RESOLVE — Saavn → Cobalt → Piped → Invidious chain (UPDATED)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/resolve')
@limiter.limit("80 per minute")
def resolve_song():
    q        = request.args.get('q', '').strip()
    artist   = request.args.get('artist', '').strip()
    fallback = request.args.get('fallback', '').strip()
    token    = request.args.get('token', '').strip()
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})

    # ── Step 1: JioSaavn ─────────────────────────────────────
    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query)
        if result and not has_word_match(q, result['title']):
            if result.get('score', 0) < 0.8: result = None
        if result:
            log.info(f"[Resolve] ✓ JioSaavn — '{result['title']}' quality={result['quality']}")
            return jsonify({
                'success': True, 'token': token,
                'url':     f"/api/stream?url={quote(result['url'], safe='')}",
                'quality': result['quality'], 'title': result['title'],
                'artist':  result['artist'],  'image': result.get('image', ''),
                'source':  'saavn',
            })

    # ── Step 2: Piped search → Cobalt audio ──────────────────
    log.info(f"[Resolve] JioSaavn miss → Piped+Cobalt for '{q}'")
    piped_search = fetch_from_piped(q, title=q, artist=artist)
    if piped_search:
        video_id = piped_search.get('videoId', '')
        if video_id:
            cobalt = fetch_from_cobalt(video_id)
            if cobalt and cobalt.get('url'):
                log.info(f"[Resolve] ✓ Piped search + Cobalt audio — '{piped_search['title']}'")
                return jsonify({
                    'success': True, 'token': token,
                    'url':     f"/api/stream?url={quote(cobalt['url'], safe='')}",
                    'quality': cobalt['quality'],
                    'title':   piped_search['title'], 'artist': piped_search['artist'],
                    'image':   piped_search.get('image', f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"),
                    'source':  'cobalt',
                })
        # Piped direct stream (no Cobalt)
        if piped_search.get('url'):
            return jsonify({
                'success': True, 'token': token,
                'url':     f"/api/stream?url={quote(piped_search['url'], safe='')}",
                'quality': piped_search['quality'], 'title': piped_search['title'],
                'artist':  piped_search['artist'],  'image': piped_search.get('image', ''),
                'source':  'piped',
            })

    # ── Step 3: Invidious ─────────────────────────────────────
    log.info(f"[Resolve] Piped miss → Invidious for '{q}'")
    inv_result = fetch_from_invidious(q, title=q, artist=artist)
    if inv_result:
        video_id = inv_result.get('videoId', '')
        if video_id:
            cobalt = fetch_from_cobalt(video_id)
            if cobalt and cobalt.get('url'):
                log.info(f"[Resolve] ✓ Invidious search + Cobalt audio — '{inv_result['title']}'")
                return jsonify({
                    'success': True, 'token': token,
                    'url':     f"/api/stream?url={quote(cobalt['url'], safe='')}",
                    'quality': cobalt['quality'],
                    'title':   inv_result['title'], 'artist': inv_result['artist'],
                    'image':   inv_result.get('image', ''),
                    'source':  'cobalt',
                })
        if inv_result.get('url'):
            return jsonify({
                'success': True, 'token': token,
                'url':     f"/api/stream?url={quote(inv_result['url'], safe='')}",
                'quality': inv_result['quality'], 'title': inv_result['title'],
                'artist':  inv_result['artist'],  'image': inv_result.get('image', ''),
                'source':  'invidious',
            })

    log.info(f"[Resolve] ✗ All sources failed for '{q}'")
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# STREAM PROXY
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
    if not url: return jsonify({'error': 'Missing URL'}), 400
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'): return jsonify({'error': 'Invalid URL scheme'}), 400
        domain = parsed.netloc.lower().split(':')[0]
        if not _is_allowed_domain(domain):
            log.warning(f"[Stream] Blocked domain: {domain}")
            return jsonify({'error': 'Domain not allowed'}), 403
    except Exception: return jsonify({'error': 'Invalid URL'}), 400
    try:
        req_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': 'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header: req_headers['Range'] = range_header
        upstream = requests.get(url, headers=req_headers, stream=True, timeout=60, allow_redirects=True)
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally: upstream.close()
        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        log.error(f"[Stream] Error → {url[:80]}: {e}")
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# DOWNLOAD ENDPOINT
# ═══════════════════════════════════════════════════════════════
@app.route('/api/download')
@limiter.limit("20 per minute")
def download_song():
    q       = request.args.get('q', '').strip()
    artist  = request.args.get('artist', '').strip()
    quality = request.args.get('quality', 'full').strip()
    # YouTube download via videoId
    video_id = request.args.get('videoId', '').strip()

    if not q and not video_id:
        return jsonify({'error': 'Missing query or videoId'}), 400

    stream_url = None; content_type = 'audio/mpeg'
    filename_base = f"{q} - {artist}".strip(' -') if artist else (q or video_id)

    # If videoId provided — use Cobalt directly
    if video_id:
        cobalt = fetch_from_cobalt(video_id)
        if cobalt and cobalt.get('url'):
            stream_url    = cobalt['url']
            content_type  = 'audio/mpeg'
            filename_base = q or video_id
            log.info(f"[Download] Cobalt → videoId={video_id}")

    # Otherwise try Saavn chain
    if not stream_url and q:
        for query in build_query_variants(q, artist, ''):
            result = fetch_saavn_parallel(query)
            if result and result.get('url'):
                raw_urls = result.get('_raw_urls', [])
                if quality == 'gift' and raw_urls:
                    for item in raw_urls:
                        if '320' in str(item.get('quality', '')):
                            stream_url = item.get('url') or item.get('link'); break
                if not stream_url: stream_url = result['url']
                log.info(f"[Download] JioSaavn → '{result['title']}' quality={result.get('quality')}")
                filename_base = f"{result['title']} - {result['artist']}".strip(' -')
                break

        if not stream_url:
            piped = fetch_from_piped(q, title=q, artist=artist)
            if piped:
                vid = piped.get('videoId', '')
                if vid:
                    cobalt = fetch_from_cobalt(vid)
                    if cobalt and cobalt.get('url'):
                        stream_url    = cobalt['url']
                        filename_base = f"{piped['title']} - {piped['artist']}".strip(' -')
                        log.info(f"[Download] Piped+Cobalt → '{piped['title']}'")
                if not stream_url and piped.get('url'):
                    stream_url    = piped['url']
                    filename_base = f"{piped['title']} - {piped['artist']}".strip(' -')
                    content_type  = 'audio/webm'

        if not stream_url:
            inv = fetch_from_invidious(q, title=q, artist=artist)
            if inv and inv.get('url'):
                stream_url    = inv['url']
                filename_base = f"{inv['title']} - {inv['artist']}".strip(' -')
                content_type  = 'audio/webm'

    if not stream_url:
        return jsonify({'error': 'Song not found on any source'}), 404

    try:
        clean_name = re.sub(r'[/\\?%*:|"<>]', '-', filename_base)
        headers    = {'User-Agent': 'Mozilla/5.0', 'Accept': 'audio/*,*/*;q=0.8', 'Accept-Encoding': 'identity'}
        upstream   = requests.get(stream_url, headers=headers, stream=True, timeout=60, allow_redirects=True)
        if not upstream.ok: return jsonify({'error': f'Upstream error {upstream.status_code}'}), 502
        actual_ct  = upstream.headers.get('Content-Type', content_type)
        ext        = 'webm' if 'webm' in actual_ct else ('m4a' if ('mp4' in actual_ct or 'm4a' in actual_ct) else 'mp3')
        filename   = f"{clean_name}.{ext}"
        resp_headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': actual_ct,
            'Accept-Ranges': 'bytes',
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
        log.error(f"[Download] Stream error: {e}")
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# GOOGLE AUTH + PREMIUM SYNC ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.route('/api/auth/google', methods=['POST'])
@limiter.limit("20 per minute")
def handle_google_auth():
    data       = request.get_json() or {}
    credential = data.get('credential', '').strip()
    if not credential:
        return jsonify({'error': 'Missing credential'}), 400
    profile = _verify_google_jwt(credential)
    if not profile:
        return jsonify({'error': 'Invalid or expired credential'}), 401
    sub     = profile.get('sub', '').strip()
    name    = profile.get('name', '')
    email   = profile.get('email', '')
    picture = profile.get('picture', '')
    if not sub:
        return jsonify({'error': 'Missing user sub'}), 400
    with get_db() as conn:
        conn.execute('''
            INSERT INTO users (google_sub, name, email, picture)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(google_sub) DO UPDATE SET
                name    = excluded.name,
                email   = excluded.email,
                picture = excluded.picture
        ''', (sub, name, email, picture))
        conn.commit()
    log.info(f"[Auth] User upserted: {email}")
    return jsonify({'success': True, 'sub': sub, 'name': name})

@app.route('/api/sync/state', methods=['POST'])
@limiter.limit("60 per minute")
def save_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'error': 'Unauthorized'}), 401
    data       = request.get_json() or {}
    song_id    = (data.get('songId') or '').strip()
    song_title = data.get('songTitle', '')
    artist     = data.get('artist', '')
    art_url    = data.get('artUrl', '')
    device     = data.get('device', 'mobile')
    try:
        progress = max(0.0, min(float(data.get('progress', 0)), 3600.0))
    except (ValueError, TypeError):
        progress = 0.0
    if device not in ('mobile', 'tv'):
        device = 'mobile'
    if not song_id:
        return jsonify({'status': 'ignored — no song'}), 200
    with get_db() as conn:
        conn.execute('''
            INSERT INTO playback_state
                (google_sub, song_id, song_title, artist, art_url, progress, device, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(google_sub) DO UPDATE SET
                song_id    = excluded.song_id,
                song_title = excluded.song_title,
                artist     = excluded.artist,
                art_url    = excluded.art_url,
                progress   = excluded.progress,
                device     = excluded.device,
                updated_at = CURRENT_TIMESTAMP
        ''', (sub, song_id, song_title, artist, art_url, progress, device))
        conn.commit()
    log.info(f"[Sync] State saved — sub={sub[:8]}… song='{song_title}' @{progress:.0f}s device={device}")
    return jsonify({'success': True})

@app.route('/api/sync/state', methods=['GET'])
@limiter.limit("60 per minute")
def get_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        row = conn.execute('SELECT * FROM playback_state WHERE google_sub = ?', (sub,)).fetchone()
    if row:
        return jsonify({
            'success'   : True,
            'songId'    : row['song_id'],
            'songTitle' : row['song_title'],
            'artist'    : row['artist'],
            'artUrl'    : row['art_url'],
            'progress'  : row['progress'],
            'device'    : row['device'],
            'updatedAt' : row['updated_at'],
        })
    return jsonify({'success': False})

@app.route('/api/auth/tv-generate-code', methods=['POST'])
@limiter.limit("10 per minute")
def generate_tv_code():
    data       = request.get_json() or {}
    session_id = data.get('sessionId') or secrets.token_hex(8)
    code       = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    expiry     = (datetime.utcnow() + timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
    with get_db() as conn:
        conn.execute('DELETE FROM tv_pairing WHERE tv_session_id = ?', (session_id,))
        conn.execute('INSERT INTO tv_pairing (pairing_code, tv_session_id, expires_at) VALUES (?, ?, ?)', (code, session_id, expiry))
        conn.commit()
    return jsonify({'code': code, 'sessionId': session_id, 'expiresIn': 300})

@app.route('/api/auth/tv-poll')
@limiter.limit("60 per minute")
def poll_tv_pairing():
    code    = request.args.get('code', '').strip().upper()
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if not code: return jsonify({'status': 'pending'}), 400
    with get_db() as conn:
        row = conn.execute('SELECT * FROM tv_pairing WHERE pairing_code = ? AND expires_at > ?', (code, now_str)).fetchone()
        if not row: return jsonify({'status': 'expired'})
        if row['google_sub']:
            user = conn.execute('SELECT * FROM users WHERE google_sub = ?', (row['google_sub'],)).fetchone()
            conn.execute('DELETE FROM tv_pairing WHERE pairing_code = ?', (code,))
            conn.commit()
            if user:
                return jsonify({'status': 'authorized', 'user': {'sub': user['google_sub'], 'name': user['name'], 'email': user['email'], 'picture': user['picture']}})
    return jsonify({'status': 'pending'})

@app.route('/api/auth/tv-verify-mobile', methods=['POST'])
@limiter.limit("20 per minute")
def mobile_verify_tv():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data    = request.get_json() or {}
    code    = data.get('code', '').strip().upper()
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if not code:
        return jsonify({'success': False, 'error': 'Missing code'}), 400
    with get_db() as conn:
        row = conn.execute('SELECT * FROM tv_pairing WHERE pairing_code = ? AND expires_at > ?', (code, now_str)).fetchone()
        if not row: return jsonify({'success': False, 'error': 'Invalid or expired code'}), 404
        conn.execute('UPDATE tv_pairing SET google_sub = ? WHERE pairing_code = ?', (sub, code))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/auth/verify-ghost-pin', methods=['POST'])
@limiter.limit("10 per minute")
def verify_ghost_pin():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data = request.get_json() or {}
    pin  = data.get('pin', '').strip()
    if not pin:
        return jsonify({'success': False}), 400
    h_input = hashlib.sha256(pin.encode('utf-8')).hexdigest()
    with get_db() as conn:
        user = conn.execute('SELECT ghost_pin_hash FROM users WHERE google_sub = ?', (sub,)).fetchone()
        if not user:
            return jsonify({'success': False}), 404
        if not user['ghost_pin_hash']:
            conn.execute('UPDATE users SET ghost_pin_hash = ? WHERE google_sub = ?', (h_input, sub))
            conn.commit()
            return jsonify({'success': True})
        if hmac.compare_digest(user['ghost_pin_hash'], h_input):
            return jsonify({'success': True})
    return jsonify({'success': False})

# ═══════════════════════════════════════════════════════════════
# ADMIN
# ═══════════════════════════════════════════════════════════════
@app.route('/api/admin/users')
@limiter.limit("10 per minute")
def admin_users():
    secret = request.args.get('key', '')
    if not hmac.compare_digest(secret, ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        rows = conn.execute('SELECT name, email, picture, created_at FROM users ORDER BY created_at DESC').fetchall()
    return jsonify({'users': [dict(r) for r in rows], 'total': len(rows)})

# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════
@app.route('/health')
def health():
    return jsonify({
        'status':  'ok',
        'sources': ['saavn', 'cobalt', 'piped', 'invidious'],
        'auth':    'google-oauth',
        'yt_endpoints': ['/api/yt/search', '/api/yt/trending', '/api/yt/suggestions', '/api/yt/stream'],
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
