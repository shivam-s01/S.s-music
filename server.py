from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import os
import re
import logging
import random
import string
import secrets
import hmac
import hashlib
import time
import threading
import asyncio
import yt_dlp
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote, urlencode
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from collections import OrderedDict
from functools import lru_cache
import weakref

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GOOGLE_CLIENT_ID  = os.environ.get('GOOGLE_CLIENT_ID', '')
ADMIN_KEY         = os.environ.get('ADMIN_KEY', '')
SUPABASE_URL      = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY      = os.environ.get('SUPABASE_KEY', '')

if not GOOGLE_CLIENT_ID:
    raise RuntimeError('GOOGLE_CLIENT_ID env var is required')
if not ADMIN_KEY:
    raise RuntimeError('ADMIN_KEY env var is required')
if not SUPABASE_URL:
    raise RuntimeError('SUPABASE_URL env var is required')
if not SUPABASE_KEY:
    raise RuntimeError('SUPABASE_KEY env var is required')

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

limiter    = Limiter(get_real_ip, app=app, default_limits=[], storage_uri="memory://")
# Reduced thread pool for better resource management on low-end devices
_executor  = ThreadPoolExecutor(max_workers=8, thread_name_prefix="aurum_worker")
_google_req = google_requests.Request()

# ═══════════════════════════════════════════════════════════════
# REQUEST DEDUPLICATION (Performance Critical)
# ═══════════════════════════════════════════════════════════════
_pending_requests = {}
_pending_lock = threading.Lock()

def dedupe_request(key: str, timeout: float = 15.0):
    """Decorator/helper to deduplicate identical in-flight requests"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            with _pending_lock:
                if key in _pending_requests:
                    future = _pending_requests[key]
                    if not future.done():
                        log.debug(f"[Dedupe] Reusing pending request: {key[:50]}")
                        return future.result()
                    else:
                        del _pending_requests[key]
            
            # Execute and store future
            future = _executor.submit(func, *args, **kwargs)
            with _pending_lock:
                _pending_requests[key] = future
            
            try:
                result = future.result(timeout=timeout)
                return result
            finally:
                with _pending_lock:
                    _pending_requests.pop(key, None)
        return wrapper
    return decorator

# ═══════════════════════════════════════════════════════════════
# LRU CACHE WITH SIZE LIMIT (Memory leak fix)
# ═══════════════════════════════════════════════════════════════
class TimedLRUCache:
    """LRU cache with TTL and max size - prevents memory growth"""
    def __init__(self, maxsize=200, ttl=600):
        self.cache = OrderedDict()
        self.maxsize = maxsize
        self.ttl = ttl
        self.lock = threading.Lock()
    
    def get(self, key):
        with self.lock:
            if key not in self.cache:
                return None
            value, timestamp = self.cache[key]
            if time.time() - timestamp > self.ttl:
                del self.cache[key]
                return None
            # Move to end (LRU)
            self.cache.move_to_end(key)
            return value
    
    def set(self, key, value):
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = (value, time.time())
            
            # Enforce max size
            while len(self.cache) > self.maxsize:
                self.cache.popitem(last=False)
    
    def invalidate(self, key):
        with self.lock:
            self.cache.pop(key, None)

# Initialize bounded caches
_meta_cache = TimedLRUCache(maxsize=200, ttl=600)
_ytdlp_cache = TimedLRUCache(maxsize=100, ttl=240)

# ═══════════════════════════════════════════════════════════════
# Supabase HTTP Helpers
# ═══════════════════════════════════════════════════════════════
def _sb_headers():
    return {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=representation',
    }

def sb_select(table, filters=None, columns='*'):
    url = f"{SUPABASE_URL}/rest/v1/{table}?select={columns}"
    if filters:
        for k, v in filters.items():
            url += f"&{k}=eq.{quote(str(v), safe='')}"
    try:
        r = requests.get(url, headers=_sb_headers(), timeout=10)
        if r.status_code == 200:
            return r.json()
        log.warning(f"[Supabase] SELECT {table} error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"[Supabase] SELECT {table} exception: {e}")
    return []

def sb_upsert(table, data, on_conflict=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = _sb_headers()
    if on_conflict:
        headers['Prefer'] = f'resolution=merge-duplicates,return=representation'
        url += f"?on_conflict={on_conflict}"
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        if r.status_code in (200, 201):
            return r.json()
        log.warning(f"[Supabase] UPSERT {table} error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"[Supabase] UPSERT {table} exception: {e}")
    return None

def sb_update(table, data, filters):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if filters:
        params = '&'.join(f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items())
        url += f"?{params}"
    try:
        r = requests.patch(url, headers=_sb_headers(), json=data, timeout=10)
        if r.status_code in (200, 204):
            return True
        log.warning(f"[Supabase] UPDATE {table} error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"[Supabase] UPDATE {table} exception: {e}")
    return False

def sb_delete(table, filters):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if filters:
        params = '&'.join(f"{k}=eq.{quote(str(v), safe='')}" for k, v in filters.items())
        url += f"?{params}"
    try:
        r = requests.delete(url, headers=_sb_headers(), timeout=10)
        if r.status_code in (200, 204):
            return True
        log.warning(f"[Supabase] DELETE {table} error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"[Supabase] DELETE {table} exception: {e}")
    return False

# ═══════════════════════════════════════════════════════════════
# JWT Helpers
# ═══════════════════════════════════════════════════════════════
def _verify_google_jwt(credential: str) -> dict | None:
    try:
        payload = id_token.verify_oauth2_token(
            credential, _google_req, GOOGLE_CLIENT_ID, clock_skew_in_seconds=10,
        )
        if payload.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
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
# Supabase Song Cache
# ═══════════════════════════════════════════════════════════════
_SONG_CACHE_TTL    = 86400
_VOLATILE_SOURCES  = {'youtube', 'youtube-broad', 'piped', 'invidious', 'soundcloud'}
_VOLATILE_CACHE_TTL = 21600

def _supabase_cache_get(cache_key: str) -> dict | None:
    try:
        rows = sb_select('song_cache', {'cache_key': cache_key})
        if not rows:
            return None
        row = rows[0]
        age    = int(time.time()) - int(row.get('cached_at', 0))
        source = row.get('source', '')
        ttl    = _VOLATILE_CACHE_TTL if source in _VOLATILE_SOURCES else _SONG_CACHE_TTL
        if age > ttl:
            _executor.submit(sb_delete, 'song_cache', {'cache_key': cache_key})
            return None
        return row
    except Exception as e:
        log.warning(f'[Cache] get error: {e}')
        return None

def _supabase_cache_set(cache_key: str, data: dict):
    try:
        sb_upsert('song_cache', {
            'cache_key': cache_key,
            'url':       data.get('url', ''),
            'quality':   data.get('quality', ''),
            'title':     data.get('title', ''),
            'artist':    data.get('artist', ''),
            'image':     data.get('image', ''),
            'source':    data.get('source', ''),
            'cached_at': int(time.time()),
        }, on_conflict='cache_key')
    except Exception as e:
        log.warning(f'[Cache] set error: {e}')

# ═══════════════════════════════════════════════════════════════
# Saavn Mirrors (Reduced for performance)
# ═══════════════════════════════════════════════════════════════
_BASE_MIRRORS = [
    'https://jio-saavn-api.onrender.com',
    'https://my-jiosaavn-api.onrender.com',
    'https://saavn-backend.onrender.com',
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://jiosaavn-api.vercel.app',
]

SAAVN_MIRRORS   = list(_BASE_MIRRORS)
_mirror_lock    = threading.Lock()
_discovered_set = set(_BASE_MIRRORS)

# ═══════════════════════════════════════════════════════════════
# Source Health Tracking (Bounded to prevent memory leak)
# ═══════════════════════════════════════════════════════════════
_source_health = {}
_health_lock   = threading.Lock()
_MAX_HEALTH_ENTRIES = 100  # Prevent unbounded growth

def _cleanup_old_health_entries():
    """Prevent memory leak by removing old/dead entries"""
    with _health_lock:
        if len(_source_health) > _MAX_HEALTH_ENTRIES:
            # Remove entries with oldest last_ok or highest fails
            to_remove = sorted(_source_health.items(), 
                              key=lambda x: (x[1].get('last_ok', 0), -x[1].get('fails', 0)))[:20]
            for url in to_remove:
                _source_health.pop(url[0], None)

def _health_record_ok(url: str, elapsed_ms: float = 0):
    with _health_lock:
        h = _source_health.setdefault(url, {'fails': 0, 'last_fail': 0, 'last_ok': 0, 'avg_ms': 0, 'total_hits': 0})
        h['fails']      = max(0, h['fails'] - 1)
        h['last_ok']    = time.time()
        h['total_hits'] = h.get('total_hits', 0) + 1
        if elapsed_ms > 0:
            h['avg_ms'] = (h['avg_ms'] * 0.8 + elapsed_ms * 0.2) if h['avg_ms'] else elapsed_ms
    _cleanup_old_health_entries()

def _health_record_fail(url: str):
    with _health_lock:
        h = _source_health.setdefault(url, {'fails': 0, 'last_fail': 0, 'last_ok': 0, 'avg_ms': 0, 'total_hits': 0})
        h['fails']    += 1
        h['last_fail'] = time.time()
    _cleanup_old_health_entries()

# ═══════════════════════════════════════════════════════════════
# Piped Instances (Reduced)
# ═══════════════════════════════════════════════════════════════
_BASE_PIPED = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.tokhmi.xyz',
    'https://api.piped.yt',
]
PIPED_INSTANCES = list(_BASE_PIPED)
_piped_lock     = threading.Lock()
_piped_known    = set(_BASE_PIPED)

# ═══════════════════════════════════════════════════════════════
# Invidious Instances (Reduced)
# ═══════════════════════════════════════════════════════════════
_BASE_INVIDIOUS = [
    'https://invidious.snopyta.org',
    'https://vid.puffyan.us',
    'https://invidious.kavin.rocks',
]
INVIDIOUS_INSTANCES = list(_BASE_INVIDIOUS)
_invidious_lock     = threading.Lock()

# ═══════════════════════════════════════════════════════════════
# SoundCloud Client ID
# ═══════════════════════════════════════════════════════════════
SOUNDCLOUD_CLIENT_ID = os.environ.get('SOUNDCLOUD_CLIENT_ID', 'a3e059563d7fd3372b49b37f00a00bcf')
_sc_client_id_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════
# KEEPALIVE (Reduced frequency)
# ═══════════════════════════════════════════════════════════════
_KEEPALIVE_URLS = [
    'https://jiosavan.onrender.com/song/?query=test',
]

def _keepalive_ping():
    time.sleep(300)  # Wait 5 min before first ping
    while True:
        try:
            time.sleep(1800)  # Every 30 minutes instead of 10
            for url in _KEEPALIVE_URLS:
                try:
                    requests.get(url, timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
                except Exception:
                    pass
        except Exception:
            pass

threading.Thread(target=_keepalive_ping, daemon=True).start()

# ═══════════════════════════════════════════════════════════════
# ALLOWED STREAM DOMAINS
# ═══════════════════════════════════════════════════════════════
ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'cf.saavncdn.com', 'aac.saavncdn.com', 'static.saavncdn.com',
    'googlevideo.com', 'youtube.com', 'ytimg.com',
    'sndcdn.com', 'soundcloud.com',
]

QUALITY_RANK = {
    '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
    '96kbps': 3, '96': 3, '48kbps': 2, '48': 2,
}

NINETIES_SEEDS = [
    "Kumar Sanu hits", "Udit Narayan 90s", "Alka Yagnik 90s",
    "Lata Mangeshkar 90s", "Sonu Nigam 90s hits",
]

NINETIES_TRIGGERS = ['90', 'purane', 'purani', 'old', 'retro', 'classic', 'nineties']

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
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
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
# CRITICAL FIX: STRICT NORMALIZATION (No over-aggressive stripping)
# ═══════════════════════════════════════════════════════════════
def normalize_strict(text):
    """Strict normalization - preserves original character integrity"""
    if not text:
        return ""
    text = text.lower().strip()
    # Remove only problematic punctuation, NOT normal letters
    text = re.sub(r'[^\w\s\u0900-\u097F]', '', text)  # Keep Devanagari
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def normalize_lenient(text):
    """Lenient normalization for matching - removes extra metadata only"""
    if not text:
        return ""
    # Remove parenthetical content (OST, official, etc.) but NOT main title
    text = re.sub(r'\(\s*(?:from|ost|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?)\s*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[\s*(?:official|audio|video|lyrics|hd|hq)\s*\]', '', text, flags=re.IGNORECASE)
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

# ═══════════════════════════════════════════════════════════════
# CRITICAL FIX: EXACT MATCH SCORING
# ═══════════════════════════════════════════════════════════════
def is_exact_title_match(query_title: str, song_title: str) -> bool:
    """Check if titles match exactly after strict normalization"""
    q_norm = normalize_strict(query_title)
    s_norm = normalize_strict(song_title)
    return q_norm == s_norm or q_norm in s_norm or s_norm in q_norm

def is_exact_artist_match(query_artist: str, song_artist: str) -> bool:
    """Check if artists match exactly"""
    if not query_artist or not song_artist:
        return False
    q_norm = normalize_strict(query_artist)
    s_norm = normalize_strict(song_artist)
    return q_norm == s_norm or q_norm in s_norm.split()

def is_remix_or_cover(title: str) -> bool:
    """Detect if song is a remix, cover, live, or other variant"""
    title_lower = title.lower()
    bad_patterns = [
        'remix', 'cover', 'live', 'karaoke', 'instrumental', 
        'slowed', 'reverb', 'sped up', 'speed up', 'workout',
        'dj ', ' mashup', 'club mix', 'dance mix', 'extended',
        'acoustic', 'version', 'edit', 'radio edit', 'lofi', 'lo-fi'
    ]
    for pattern in bad_patterns:
        if pattern in title_lower:
            return True
    return False

def calculate_match_score(query_title: str, query_artist: str, 
                          song_title: str, song_artist: str, 
                          song_year: int = 0) -> float:
    """
    Multi-stage scoring with strict penalties for wrong matches.
    Score range: 0.0 (no match) to 10.0 (perfect exact match)
    """
    q_title_norm = normalize_strict(query_title)
    q_artist_norm = normalize_strict(query_artist) if query_artist else ""
    s_title_norm = normalize_strict(song_title)
    s_artist_norm = normalize_strict(song_artist) if song_artist else ""
    
    # PERFECT MATCH (Stage 1)
    if q_title_norm == s_title_norm:
        if q_artist_norm and s_artist_norm and q_artist_norm == s_artist_norm:
            return 10.0
        return 9.0
    
    # EXACT TITLE ONLY (Stage 2)
    if q_title_norm == s_title_norm:
        return 8.5
    
    # TITLE CONTAINS EXACT (e.g., "Choliye" vs "Choliye Song")
    if s_title_norm.startswith(q_title_norm) or q_title_norm.startswith(s_title_norm):
        base_score = 7.5
    # WORD MATCH (all query words present)
    else:
        q_words = set(q_title_norm.split())
        s_words = set(s_title_norm.split())
        if q_words and q_words.issubset(s_words):
            base_score = 6.5
        else:
            # Fuzzy fallback
            common = q_words & s_words
            if not common:
                return 0.0
            base_score = 5.0 + (len(common) / max(len(q_words), 1)) * 2.0
    
    # Artist validation bonus
    if q_artist_norm and s_artist_norm:
        if q_artist_norm == s_artist_norm:
            base_score += 2.0
        elif q_artist_norm in s_artist_norm or s_artist_norm in q_artist_norm:
            base_score += 1.0
        elif any(word in s_artist_norm for word in q_artist_norm.split()[:2]):
            base_score += 0.5
    
    # HEAVY PENALTIES for wrong versions
    if is_remix_or_cover(song_title):
        base_score -= 3.0
    
    if is_remix_or_cover(query_title):
        # User explicitly wants remix? Keep original intent
        pass
    elif any(term in s_title_norm for term in ['remix', 'cover', 'live']):
        base_score -= 2.0
    
    # Year penalty (very old or very new vs expected)
    if song_year > 0:
        current_year = datetime.now().year
        if song_year < 1990:
            base_score -= 0.5
        elif song_year > current_year:
            base_score -= 0.5
    
    return max(0.0, min(10.0, base_score))

def should_accept_match(score: float, has_artist: bool) -> bool:
    """Determine if a match should be accepted based on score"""
    if has_artist:
        return score >= 6.0
    return score >= 5.0

# ═══════════════════════════════════════════════════════════════
# BUILD QUERY VARIANTS (Limited, No aggressive transliteration)
# ═══════════════════════════════════════════════════════════════
def build_query_variants(title: str, artist: str = '', fallback: str = '') -> list:
    """Build search variants without creating completely different words"""
    variants = []
    seen = set()
    
    title_clean = normalize_lenient(title)
    artist_clean = normalize_lenient(artist) if artist else ''
    
    # Primary: Title + Artist
    if artist_clean:
        primary = f"{title_clean} {artist_clean}"
        if primary not in seen:
            seen.add(primary)
            variants.append(primary)
    
    # Title only
    if title_clean not in seen:
        seen.add(title_clean)
        variants.append(title_clean)
    
    # Title without common suffixes
    title_no_parentheses = re.sub(r'\([^)]*\)', '', title_clean).strip()
    if title_no_parentheses and title_no_parentheses != title_clean:
        if title_no_parentheses not in seen:
            seen.add(title_no_parentheses)
            variants.append(title_no_parentheses)
    
    # Artist first word + title
    if artist_clean:
        artist_first = artist_clean.split()[0] if artist_clean.split() else ''
        if artist_first and artist_first != artist_clean:
            combined = f"{artist_first} {title_clean}"
            if combined not in seen:
                seen.add(combined)
                variants.append(combined)
    
    # Fallback
    if fallback and fallback not in seen:
        seen.add(fallback)
        variants.append(fallback)
    
    # Limit variants to prevent over-broad searches
    return variants[:5]

# ═══════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════
def pick_best_quality(urls):
    if not urls:
        return None, None
    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK:
            return QUALITY_RANK[q]
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

def _safe_year(date_str):
    try:
        return int((date_str or '')[:4])
    except (ValueError, TypeError):
        return 0

def _mirror_ok(mirror):
    return True  # Simplified for performance

def _mirror_failed(mirror):
    _health_record_fail(mirror)

# ═══════════════════════════════════════════════════════════════
# Saavn Search
# ═══════════════════════════════════════════════════════════════
def _fetch_saavn_search_mirror(mirror, search_term):
    for endpoint in ['/api/search/songs', '/api/search']:
        try:
            r = requests.get(f'{mirror}{endpoint}',
                             params={'query': search_term, 'limit': 10},
                             timeout=5,
                             headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                continue
            data = r.json()
            raw = (data.get('data', {}).get('results') or 
                   data.get('results') or
                   data.get('songs', {}).get('results') or [])
            if raw:
                return raw
        except Exception:
            pass
    return []

def _fetch_saavn_search_parallel(search_term):
    with _mirror_lock:
        mirrors = SAAVN_MIRRORS[:3]  # Limit to 3 mirrors for speed
    for mirror in mirrors:
        result = _fetch_saavn_search_mirror(mirror, search_term)
        if result:
            return result
    return []

def _normalize_saavn_songs(raw_songs):
    normalized = []
    for song in raw_songs:
        song_id = song.get('id', '').strip()
        if not song_id:
            continue
        title = song.get('name') or song.get('title', '')
        artist = song.get('primaryArtists') or song.get('primary_artists', '')
        image = pick_image(song)
        year = str(song.get('year') or '0')[:4]
        dur_s = int(song.get('duration', 0) or 0)
        dur_ms = dur_s * 1000
        
        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        _, quality = pick_best_quality(raw_urls)
        
        if not quality:
            continue
        
        normalized.append({
            'trackId': song_id,
            'trackName': title,
            'artistName': artist,
            'artworkUrl100': image if image else '',
            'previewUrl': f"/api/play?id={quote(song_id, safe='')}",
            'trackTimeMillis': dur_ms,
            'releaseDate': f"{year}-01-01T00:00:00Z",
            '_saavnId': song_id,
            '_quality': quality,
            '_source': 'saavn',
        })
    return normalized

# ═══════════════════════════════════════════════════════════════
# CRITICAL FIX: EXACT ID RESOLUTION (No fallback to broad search)
# ═══════════════════════════════════════════════════════════════
def _fetch_saavn_by_id_exact(song_id: str) -> dict | None:
    """Fetch song by exact ID - no fallback to title search"""
    with _mirror_lock:
        mirrors = SAAVN_MIRRORS[:3]
    
    for mirror in mirrors:
        endpoints = [f'/api/songs/{song_id}', f'/songs/{song_id}']
        for endpoint in endpoints:
            try:
                r = requests.get(f'{mirror}{endpoint}', timeout=6,
                                 headers={'User-Agent': 'Mozilla/5.0'})
                if r.status_code != 200:
                    continue
                data = r.json()
                song = None
                if isinstance(data.get('data'), list) and data['data']:
                    song = data['data'][0]
                elif isinstance(data.get('data'), dict):
                    song = data['data']
                elif data.get('id'):
                    song = data
                
                if not song:
                    continue
                
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                if isinstance(raw_urls, str):
                    raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                best_url, quality = pick_best_quality(raw_urls)
                if best_url:
                    return {
                        'url': best_url,
                        'quality': quality,
                        'title': song.get('name') or song.get('title', ''),
                        'artist': song.get('primaryArtists') or song.get('primary_artists', ''),
                        'image': pick_image(song),
                        'source': 'saavn'
                    }
            except Exception:
                continue
    return None

def fetch_from_mirror_with_scoring(mirror, query, query_title, query_artist) -> dict | None:
    """Fetch and score matches - returns only high-confidence matches"""
    for endpoint in ['/api/search/songs', '/api/search']:
        try:
            r = requests.get(f'{mirror}{endpoint}',
                             params={'query': query, 'limit': 10},
                             timeout=4,
                             headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                continue
            data = r.json()
            results = (data.get('data', {}).get('results') or 
                       data.get('results') or 
                       data.get('songs', {}).get('results') or [])
            
            best_song = None
            best_score = -1
            
            for song in results:
                song_title = song.get('name') or song.get('title', '')
                song_artist = song.get('primaryArtists') or song.get('primary_artists', '')
                song_year = int(song.get('year') or 0)
                
                score = calculate_match_score(query_title, query_artist, 
                                              song_title, song_artist, song_year)
                
                if score > best_score:
                    best_score = score
                    best_song = song
            
            if best_song and should_accept_match(best_score, bool(query_artist)):
                raw_urls = best_song.get('downloadUrl') or best_song.get('download_url') or []
                if isinstance(raw_urls, str):
                    raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                best_url, quality = pick_best_quality(raw_urls)
                if best_url:
                    return {
                        'url': best_url,
                        'quality': quality,
                        'title': best_song.get('name') or best_song.get('title', ''),
                        'artist': best_song.get('primaryArtists') or best_song.get('primary_artists', ''),
                        'image': pick_image(best_song),
                        'score': best_score,
                        'source': 'saavn'
                    }
        except Exception:
            continue
    return None

def fetch_saavn_with_scoring(title: str, artist: str = '') -> dict | None:
    """Fetch Saavn result with strict scoring"""
    variants = build_query_variants(title, artist, '')
    best_result = None
    best_score = -1
    
    for query in variants:
        with _mirror_lock:
            mirrors = SAAVN_MIRRORS[:3]
        for mirror in mirrors:
            result = fetch_from_mirror_with_scoring(mirror, query, title, artist)
            if result and result.get('score', 0) > best_score:
                best_score = result['score']
                best_result = result
                if best_score >= 8.0:  # Good enough, stop searching
                    break
        if best_score >= 8.0:
            break
    
    return best_result if best_score >= 5.0 else None

# ═══════════════════════════════════════════════════════════════
# YouTube Music (Simplified)
# ═══════════════════════════════════════════════════════════════
_YTM_SEARCH_URL = 'https://music.youtube.com/youtubei/v1/search'
_YTM_API_KEY = 'AIzaSyC9XL3ZjWddXya6X74dJoCTL-NKNELL6imp'
_YTM_CONTEXT = {
    'client': {
        'clientName': 'WEB_REMIX',
        'clientVersion': '1.20250101.01.00',
        'hl': 'en',
        'gl': 'IN',
    }
}

def _ytm_search(query: str, limit: int = 5) -> list:
    try:
        body = {
            'context': _YTM_CONTEXT,
            'query': query,
            'params': 'EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D',
        }
        r = requests.post(_YTM_SEARCH_URL, params={'key': _YTM_API_KEY}, json=body,
                          headers={'Content-Type': 'application/json'},
                          timeout=6)
        if r.status_code != 200:
            return []
        data = r.json()
        results = []
        tabs = (data.get('contents', {})
                    .get('tabbedSearchResultsRenderer', {})
                    .get('tabs', []))
        for tab in tabs:
            section_list = (tab.get('tabRenderer', {})
                               .get('content', {})
                               .get('sectionListRenderer', {})
                               .get('contents', []))
            for section in section_list:
                items = (section.get('musicShelfRenderer', {})
                                .get('contents', []))
                for item in items[:limit]:
                    renderer = item.get('musicResponsiveListItemRenderer', {})
                    if not renderer:
                        continue
                    vid_id = None
                    # Extract video ID
                    overlay = renderer.get('overlay', {})
                    vid_id = (overlay.get('musicItemThumbnailOverlayRenderer', {})
                                    .get('content', {})
                                    .get('musicPlayButtonRenderer', {})
                                    .get('playNavigationEndpoint', {})
                                    .get('watchEndpoint', {})
                                    .get('videoId', ''))
                    if not vid_id:
                        for col in renderer.get('flexColumns', []):
                            runs = (col.get('musicResponsiveListItemFlexColumnRenderer', {})
                                       .get('text', {})
                                       .get('runs', []))
                            for run in runs:
                                ep = run.get('navigationEndpoint', {}).get('watchEndpoint', {})
                                if ep.get('videoId'):
                                    vid_id = ep['videoId']
                                    break
                            if vid_id:
                                break
                    if not vid_id:
                        continue
                    cols = renderer.get('flexColumns', [])
                    title_t = ''
                    artist_t = ''
                    for i, col in enumerate(cols):
                        runs = (col.get('musicResponsiveListItemFlexColumnRenderer', {})
                                   .get('text', {})
                                   .get('runs', []))
                        text = ' '.join(r.get('text', '') for r in runs).strip()
                        if i == 0:
                            title_t = text
                        elif i == 1:
                            artist_t = text.split('\u2022')[0].strip()
                    results.append({
                        'videoId': vid_id,
                        'title': title_t,
                        'artist': artist_t,
                    })
                    if len(results) >= limit:
                        return results
        return results
    except Exception as e:
        log.warning(f'[YTMusic] search error: {e}')
        return []

def fetch_from_ytmusic_strict(title: str, artist: str = '') -> dict | None:
    """Fetch from YouTube Music with strict matching"""
    cache_key = f"ytmusic_strict:{normalize_strict(title)}:{normalize_strict(artist)}"
    cached = _ytdlp_cache.get(cache_key)
    if cached:
        return cached
    
    results = _ytm_search(f"{title} {artist}".strip(), limit=3)
    if not results:
        return None
    
    best_match = None
    best_score = -1
    
    for item in results:
        score = calculate_match_score(title, artist, 
                                      item.get('title', ''), 
                                      item.get('artist', ''))
        if score > best_score:
            best_score = score
            best_match = item
    
    if not best_match or best_score < 5.0:
        return None
    
    # Get stream URL
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 10,
        'noplaylist': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'https://music.youtube.com/watch?v={best_match["videoId"]}',
                                    download=False)
            if not info:
                return None
            formats = info.get('formats', [])
            audio_formats = [f for f in formats if f.get('acodec') not in ('none', None, '') and f.get('url')]
            if not audio_formats:
                return None
            best_fmt = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            result = {
                'url': best_fmt['url'],
                'quality': f"{int(best_fmt.get('abr', 0))}kbps" if best_fmt.get('abr') else 'unknown',
                'title': best_match['title'],
                'artist': best_match['artist'],
                'source': 'ytmusic',
            }
            _ytdlp_cache.set(cache_key, result)
            return result
    except Exception as e:
        log.warning(f'[YTMusic] stream error: {e}')
        return None

# ═══════════════════════════════════════════════════════════════
# YT-DLP (Simplified)
# ═══════════════════════════════════════════════════════════════
def fetch_from_ytdlp_strict(title: str, artist: str = '') -> dict | None:
    cache_key = f"ytdlp_strict:{normalize_strict(title)}:{normalize_strict(artist)}"
    cached = _ytdlp_cache.get(cache_key)
    if cached:
        return cached
    
    search_query = f"{title} {artist}".strip()
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 12,
        'noplaylist': True,
        'extract_flat': 'in_playlist',
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch3:{search_query} song", download=False)
            if not info or not info.get('entries'):
                return None
            
            best_entry = None
            best_score = -1
            
            for entry in info['entries'][:3]:
                if not entry:
                    continue
                score = calculate_match_score(title, artist,
                                              entry.get('title', ''),
                                              entry.get('uploader', ''))
                if score > best_score:
                    best_score = score
                    best_entry = entry
            
            if not best_entry or best_score < 5.0:
                return None
            
            # Extract audio URL
            with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl2:
                full_info = ydl2.extract_info(best_entry['webpage_url'], download=False)
                if full_info:
                    formats = full_info.get('formats', [])
                    audio_formats = [f for f in formats if f.get('acodec') not in ('none', None, '') and f.get('url')]
                    if audio_formats:
                        best_fmt = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
                        result = {
                            'url': best_fmt['url'],
                            'quality': f"{int(best_fmt.get('abr', 0))}kbps" if best_fmt.get('abr') else 'unknown',
                            'title': best_entry.get('title', title),
                            'artist': best_entry.get('uploader', artist),
                            'source': 'youtube',
                        }
                        _ytdlp_cache.set(cache_key, result)
                        return result
    except Exception as e:
        log.warning(f'[yt-dlp] error: {e}')
    return None

# ═══════════════════════════════════════════════════════════════
# /api/play - CRITICAL FIXED ENDPOINT
# ═══════════════════════════════════════════════════════════════
@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    song_id = request.args.get('id', '').strip()
    title = request.args.get('title', '').strip()
    artist = request.args.get('artist', '').strip()
    
    if not song_id and not title:
        return jsonify({'error': 'Missing id or title'}), 400
    
    # CRITICAL FIX: If song_id is provided, ONLY use ID resolution
    if song_id:
        # Check cache first
        cache_key = f"play_id:{song_id}"
        cached = _ytdlp_cache.get(cache_key)
        if cached and cached.get('url'):
            log.info(f"[Play] Cache HIT: id={song_id}")
            audio_url = cached['url']
            quality = cached.get('quality', 'unknown')
            source = cached.get('source', 'unknown')
            if not title:
                title = cached.get('title', '')
            if not artist:
                artist = cached.get('artist', '')
        else:
            # EXACT ID resolution only - NO title fallback search
            result = _fetch_saavn_by_id_exact(song_id)
            if result and result.get('url'):
                audio_url = result['url']
                quality = result.get('quality', 'unknown')
                source = result.get('source', 'unknown')
                title = result.get('title', title)
                artist = result.get('artist', artist)
                _ytdlp_cache.set(cache_key, result)
                log.info(f"[Play] ID Resolution SUCCESS: {song_id} -> {title[:50]}")
            else:
                log.warning(f"[Play] ID Resolution FAILED: {song_id}")
                return jsonify({'error': 'Song not found for ID'}), 404
        
        # Stream the audio
        try:
            req_headers = {
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'audio/mpeg,audio/webm,audio/ogg,*/*;q=0.5',
                'Accept-Encoding': 'identity',
                'Connection': 'keep-alive',
            }
            range_header = request.headers.get('Range')
            if range_header:
                req_headers['Range'] = range_header
            
            upstream = requests.get(audio_url, headers=req_headers, stream=True,
                                    timeout=60, allow_redirects=True)
            
            excluded = {'content-encoding', 'transfer-encoding', 'connection'}
            resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
            resp_headers.update({
                'Access-Control-Allow-Origin': '*',
                'Accept-Ranges': 'bytes',
                'Cache-Control': 'no-store',
                'X-Audio-Quality': quality,
                'X-Audio-Source': source,
            })
            if 'content-type' not in {k.lower() for k in resp_headers}:
                resp_headers['Content-Type'] = 'audio/mpeg'
            
            def generate():
                try:
                    for chunk in upstream.iter_content(chunk_size=32768):  # Reduced chunk size for memory
                        if chunk:
                            yield chunk
                finally:
                    upstream.close()
            
            return Response(stream_with_context(generate()), status=upstream.status_code,
                          headers=resp_headers, direct_passthrough=True)
        except Exception as e:
            log.error(f"[Play] Stream error: {e}")
            return jsonify({'error': str(e)}), 500
    
    # NO ID provided - need to search with strict scoring
    if not title:
        return jsonify({'error': 'Missing title'}), 400
    
    cache_key = f"play_title:{normalize_strict(title)}:{normalize_strict(artist)}"
    cached = _ytdlp_cache.get(cache_key)
    if cached and cached.get('url'):
        log.info(f"[Play] Search Cache HIT: {title[:50]}")
        audio_url = cached['url']
        quality = cached.get('quality', 'unknown')
        source = cached.get('source', 'unknown')
        title = cached.get('title', title)
        artist = cached.get('artist', artist)
    else:
        # Multi-stage search with strict scoring
        result = None
        
        # Stage 1: Saavn search with strict scoring
        result = fetch_saavn_with_scoring(title, artist)
        if result:
            log.info(f"[Play] Saavn match: {result.get('title')} score={result.get('score', 0)}")
        
        # Stage 2: YouTube Music
        if not result:
            result = fetch_from_ytmusic_strict(title, artist)
            if result:
                log.info(f"[Play] YTMusic match")
        
        # Stage 3: yt-dlp fallback
        if not result:
            result = fetch_from_ytdlp_strict(title, artist)
            if result:
                log.info(f"[Play] yt-dlp match")
        
        if not result or not result.get('url'):
            log.warning(f"[Play] No match found: {title}")
            return jsonify({'error': 'No matching song found'}), 404
        
        audio_url = result['url']
        quality = result.get('quality', 'unknown')
        source = result.get('source', 'unknown')
        title = result.get('title', title)
        artist = result.get('artist', artist)
        _ytdlp_cache.set(cache_key, result)
    
    # Stream the audio
    try:
        req_headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'audio/mpeg,audio/webm,audio/ogg,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header
        
        upstream = requests.get(audio_url, headers=req_headers, stream=True,
                                timeout=60, allow_redirects=True)
        
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-store',
            'X-Audio-Quality': quality,
            'X-Audio-Source': source,
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'
        
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=32768):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
        
        return Response(stream_with_context(generate()), status=upstream.status_code,
                      headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        log.error(f"[Play] Stream error: {e}")
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# /api/songs (Preserved but with improved filtering)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q = request.args.get('q', 'top bollywood songs').strip()
    era = request.args.get('era', '').strip()
    is_90s = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q
    
    cache_key = f"songs:{normalize_strict(search_term)}"
    cached = _meta_cache.get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, '_cached': True})
    
    itunes_results = []
    saavn_results = []
    
    def fetch_itunes():
        nonlocal itunes_results
        try:
            r = requests.get('https://itunes.apple.com/search',
                             params={'term': search_term, 'media': 'music', 'entity': 'song',
                                     'limit': 50, 'country': 'IN'}, timeout=10)
            r.raise_for_status()
            results = r.json().get('results', [])
            if is_90s:
                filtered = [s for s in results if s.get('trackName') and
                            1990 <= _safe_year(s.get('releaseDate')) <= 1999]
                if len(filtered) < 5:
                    filtered = [s for s in results if s.get('trackName')]
                random.shuffle(filtered)
                itunes_results = filtered[:30]
            else:
                itunes_results = [s for s in results if s.get('trackName')][:30]
            for s in itunes_results:
                title_val = s.get('trackName', '')
                artist_val = s.get('artistName', '')
                if title_val:
                    s['previewUrl'] = (
                        f"/api/play?title={quote(title_val, safe='')}"
                        f"&artist={quote(artist_val, safe='')}"
                    )
                if s.get('artworkUrl100'):
                    s['artworkUrl100'] = s['artworkUrl100'].replace('100x100', '600x600')
        except Exception as e:
            log.warning(f"[iTunes] error: {e}")
    
    def fetch_saavn():
        nonlocal saavn_results
        try:
            raw = _fetch_saavn_search_parallel(search_term)
            if raw:
                normalized = _normalize_saavn_songs(raw)
                if is_90s:
                    filtered = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
                    normalized = filtered if len(filtered) >= 5 else normalized
                    random.shuffle(normalized)
                saavn_results = normalized[:30]
        except Exception as e:
            log.warning(f"[Saavn] error: {e}")
    
    t1 = threading.Thread(target=fetch_itunes)
    t2 = threading.Thread(target=fetch_saavn)
    t1.start()
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=4.0)
    
    merged = list(itunes_results)
    for s in saavn_results:
        merged.append(s)
    
    if merged:
        _meta_cache.set(cache_key, merged)
        return jsonify({'results': merged})
    
    return jsonify({'results': [], 'error': 'No results found'})

# ═══════════════════════════════════════════════════════════════
# /api/songs/90s (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed = random.choice(NINETIES_SEEDS)
    cache_key = f"songs:{normalize_strict(seed)}"
    cached = _meta_cache.get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, 'seed': seed, '_cached': True})
    
    raw = _fetch_saavn_search_parallel(seed)
    if raw:
        normalized = _normalize_saavn_songs(raw)
        filtered = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        result = (filtered if len(filtered) >= 5 else normalized)[:30]
        random.shuffle(result)
        _meta_cache.set(cache_key, result)
        return jsonify({'results': result, 'seed': seed})
    
    try:
        r = requests.get('https://itunes.apple.com/search',
                         params={'term': seed, 'media': 'music', 'entity': 'song',
                                 'limit': 50, 'country': 'IN'}, timeout=10)
        r.raise_for_status()
        results = r.json().get('results', [])
        filtered = [s for s in results if s.get('trackName') and
                    1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        if len(filtered) < 5:
            filtered = [s for s in results if s.get('trackName')]
        random.shuffle(filtered)
        result = filtered[:30]
        for s in result:
            title_val = s.get('trackName', '')
            artist_val = s.get('artistName', '')
            if title_val:
                s['previewUrl'] = (
                    f"/api/play?title={quote(title_val, safe='')}"
                    f"&artist={quote(artist_val, safe='')}"
                )
            if s.get('artworkUrl100'):
                s['artworkUrl100'] = s['artworkUrl100'].replace('100x100', '600x600')
        _meta_cache.set(cache_key, result)
        return jsonify({'results': result, 'seed': seed})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})

# ═══════════════════════════════════════════════════════════════
# /api/saavn (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/saavn')
@limiter.limit("100 per minute")
def get_saavn_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    fallback = request.args.get('fallback', '').strip()
    token = request.args.get('token', '').strip()
    low_quality = request.args.get('low_quality', 'false').lower() == 'true'
    
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})
    
    result = fetch_saavn_with_scoring(q, artist)
    if result:
        return jsonify({'success': True, 'token': token, **result})
    
    ytm = fetch_from_ytmusic_strict(q, artist)
    if ytm and ytm.get('url'):
        return jsonify({'success': True, 'token': token, **ytm})
    
    yt = fetch_from_ytdlp_strict(q, artist)
    if yt and yt.get('url'):
        return jsonify({'success': True, 'token': token, **yt})
    
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/resolve (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/resolve')
@limiter.limit("100 per minute")
def resolve_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    fallback = request.args.get('fallback', '').strip()
    token = request.args.get('token', '').strip()
    
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})
    
    result = fetch_saavn_with_scoring(q, artist)
    if result:
        return jsonify({
            'success': True, 'token': token,
            'url': f"/api/stream?url={quote(result['url'], safe='')}",
            'quality': result['quality'], 'title': result['title'],
            'artist': result['artist'], 'image': result.get('image', ''), 'source': 'saavn'
        })
    
    ytm = fetch_from_ytmusic_strict(q, artist)
    if ytm and ytm.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url': f"/api/stream?url={quote(ytm['url'], safe='')}",
            'quality': ytm['quality'], 'title': ytm['title'],
            'artist': ytm['artist'], 'image': ytm.get('image', ''), 'source': 'ytmusic'
        })
    
    yt = fetch_from_ytdlp_strict(q, artist)
    if yt and yt.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url': f"/api/stream?url={quote(yt['url'], safe='')}",
            'quality': yt['quality'], 'title': yt['title'],
            'artist': yt['artist'], 'image': yt.get('image', ''), 'source': 'youtube'
        })
    
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/stream (Preserved)
# ═══════════════════════════════════════════════════════════════
def _is_allowed_domain(domain):
    for allowed in ALLOWED_STREAM_DOMAINS:
        if domain == allowed or domain.endswith('.' + allowed) or allowed in domain:
            return True
    return False

@app.route('/api/stream')
@limiter.limit("200 per minute")
def stream_audio():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Missing URL'}), 400
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return jsonify({'error': 'Invalid scheme'}), 400
        domain = parsed.netloc.lower().split(':')[0]
        if not _is_allowed_domain(domain):
            return jsonify({'error': 'Domain not allowed'}), 403
    except Exception:
        return jsonify({'error': 'Invalid URL'}), 400
    
    try:
        req_headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'audio/mpeg,audio/webm,audio/ogg,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive'
        }
        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header
        
        upstream = requests.get(url, headers=req_headers, stream=True, timeout=60, allow_redirects=True)
        
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-store'
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'
        
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=32768):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
        
        return Response(stream_with_context(generate()), status=upstream.status_code,
                        headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# /api/download (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/download')
@limiter.limit("20 per minute")
def download_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    
    if not q:
        return jsonify({'error': 'Missing query'}), 400
    
    result = fetch_saavn_with_scoring(q, artist)
    if not result:
        ytm = fetch_from_ytmusic_strict(q, artist)
        if ytm:
            result = ytm
    if not result:
        yt = fetch_from_ytdlp_strict(q, artist)
        if yt:
            result = yt
    
    if not result or not result.get('url'):
        return jsonify({'error': 'Song not found'}), 404
    
    stream_url = result['url']
    filename_base = f"{result['title']} - {result['artist']}".strip(' -')
    
    try:
        clean_name = re.sub(r'[/\\?%*:|"<>]', '-', filename_base)
        upstream = requests.get(stream_url,
                                headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'},
                                stream=True, timeout=60, allow_redirects=True)
        if not upstream.ok:
            return jsonify({'error': f'Upstream {upstream.status_code}'}), 502
        
        actual_ct = upstream.headers.get('Content-Type', 'audio/mpeg')
        ext = 'webm' if 'webm' in actual_ct else ('m4a' if ('mp4' in actual_ct or 'm4a' in actual_ct) else 'mp3')
        
        resp_headers = {
            'Content-Disposition': f'attachment; filename="{clean_name}.{ext}"',
            'Content-Type': actual_ct,
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*'
        }
        if 'Content-Length' in upstream.headers:
            resp_headers['Content-Length'] = upstream.headers['Content-Length']
        
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=32768):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
        
        return Response(stream_with_context(generate()), status=200, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# /api/health
# ═══════════════════════════════════════════════════════════════
@app.route('/api/health')
def health_status():
    with _mirror_lock:
        saavn_list = list(SAAVN_MIRRORS)
    with _piped_lock:
        piped_list = list(PIPED_INSTANCES)
    with _invidious_lock:
        inv_list = list(INVIDIOUS_INSTANCES)
    
    return jsonify({
        'saavn': {'count': len(saavn_list)},
        'piped': {'count': len(piped_list)},
        'invidious': {'count': len(inv_list)},
        'timestamp': round(time.time()),
    })

# ═══════════════════════════════════════════════════════════════
# AUTH — Google Login (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/auth/google', methods=['POST'])
@limiter.limit("20 per minute")
def handle_google_auth():
    data = request.get_json() or {}
    credential = data.get('credential', '').strip()
    if not credential:
        return jsonify({'error': 'Missing credential'}), 400
    
    profile = _verify_google_jwt(credential)
    if not profile:
        return jsonify({'error': 'Invalid credential'}), 401
    
    sub = profile.get('sub', '').strip()
    if not sub:
        return jsonify({'error': 'Missing sub'}), 400
    
    sb_upsert('users', {
        'google_sub': sub,
        'name': profile.get('name', ''),
        'email': profile.get('email', ''),
        'picture': profile.get('picture', ''),
    }, on_conflict='google_sub')
    
    log.info(f"[Auth] User saved: {profile.get('email', '')}")
    return jsonify({
        'success': True,
        'sub': sub,
        'name': profile.get('name', ''),
        'email': profile.get('email', ''),
        'picture': profile.get('picture', ''),
    })

# ═══════════════════════════════════════════════════════════════
# SYNC — Playback State (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/sync/state', methods=['POST'])
@limiter.limit("60 per minute")
def save_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    song_id = (data.get('songId') or '').strip()
    try:
        progress = max(0.0, min(float(data.get('progress', 0)), 3600.0))
    except:
        progress = 0.0
    device = data.get('device', 'mobile')
    if device not in ('mobile', 'tv'):
        device = 'mobile'
    
    if not song_id:
        return jsonify({'status': 'ignored'}), 200
    
    sb_upsert('playback_state', {
        'google_sub': sub,
        'song_id': song_id,
        'song_title': data.get('songTitle', ''),
        'artist': data.get('artist', ''),
        'art_url': data.get('artUrl', ''),
        'progress': progress,
        'device': device,
        'updated_at': datetime.utcnow().isoformat(),
    }, on_conflict='google_sub')
    return jsonify({'status': 'ok'})

@app.route('/api/sync/state', methods=['GET'])
@limiter.limit("60 per minute")
def get_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'error': 'Unauthorized'}), 401
    
    rows = sb_select('playback_state', {'google_sub': sub})
    if rows:
        r = rows[0]
        return jsonify({
            'success': True,
            'songId': r.get('song_id'),
            'songTitle': r.get('song_title'),
            'artist': r.get('artist'),
            'artUrl': r.get('art_url'),
            'progress': r.get('progress'),
            'device': r.get('device'),
            'updatedAt': r.get('updated_at'),
        })
    return jsonify({'success': False})

# ═══════════════════════════════════════════════════════════════
# TV PAIRING (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/auth/tv-generate-code', methods=['POST'])
@limiter.limit("10 per minute")
def generate_tv_code():
    data = request.get_json() or {}
    session_id = data.get('sessionId') or secrets.token_hex(8)
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    expiry = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
    
    sb_delete('tv_pairing', {'tv_session_id': session_id})
    sb_upsert('tv_pairing', {
        'pairing_code': code,
        'tv_session_id': session_id,
        'expires_at': expiry,
    }, on_conflict='pairing_code')
    return jsonify({'code': code, 'sessionId': session_id, 'expiresIn': 300})

@app.route('/api/auth/tv-poll')
@limiter.limit("60 per minute")
def poll_tv_pairing():
    code = request.args.get('code', '').strip().upper()
    now_str = datetime.utcnow().isoformat()
    if not code:
        return jsonify({'status': 'pending'}), 400
    
    url = f"{SUPABASE_URL}/rest/v1/tv_pairing?pairing_code=eq.{quote(code, safe='')}&expires_at=gt.{quote(now_str, safe='')}"
    try:
        r = requests.get(url, headers=_sb_headers(), timeout=10)
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        rows = []
    
    if not rows:
        return jsonify({'status': 'expired'})
    row = rows[0]
    if row.get('google_sub'):
        user_rows = sb_select('users', {'google_sub': row['google_sub']})
        sb_delete('tv_pairing', {'pairing_code': code})
        if user_rows:
            user = user_rows[0]
            return jsonify({'status': 'authorized', 'user': {
                'sub': user['google_sub'],
                'name': user['name'],
                'email': user['email'],
                'picture': user['picture'],
            }})
    return jsonify({'status': 'pending'})

@app.route('/api/auth/tv-verify-mobile', methods=['POST'])
@limiter.limit("20 per minute")
def mobile_verify_tv():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    now_str = datetime.utcnow().isoformat()
    if not code:
        return jsonify({'success': False, 'error': 'Missing code'}), 400
    
    url = f"{SUPABASE_URL}/rest/v1/tv_pairing?pairing_code=eq.{quote(code, safe='')}&expires_at=gt.{quote(now_str, safe='')}"
    try:
        r = requests.get(url, headers=_sb_headers(), timeout=10)
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        rows = []
    
    if not rows:
        return jsonify({'success': False, 'error': 'Invalid or expired code'}), 404
    sb_update('tv_pairing', {'google_sub': sub}, {'pairing_code': code})
    return jsonify({'success': True})

# ═══════════════════════════════════════════════════════════════
# GHOST PIN (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/auth/verify-ghost-pin', methods=['POST'])
@limiter.limit("10 per minute")
def verify_ghost_pin():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    pin = data.get('pin', '').strip()
    if not pin:
        return jsonify({'success': False}), 400
    
    h_input = hashlib.sha256(pin.encode('utf-8')).hexdigest()
    rows = sb_select('users', {'google_sub': sub}, columns='ghost_pin_hash')
    if not rows:
        return jsonify({'success': False}), 404
    
    stored_hash = rows[0].get('ghost_pin_hash')
    if not stored_hash:
        sb_update('users', {'ghost_pin_hash': h_input}, {'google_sub': sub})
        return jsonify({'success': True})
    if hmac.compare_digest(stored_hash, h_input):
        return jsonify({'success': True})
    return jsonify({'success': False})

# ═══════════════════════════════════════════════════════════════
# ADMIN (Preserved)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/admin/users')
@limiter.limit("10 per minute")
def admin_users():
    secret = request.args.get('key', '')
    if not hmac.compare_digest(secret, ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    
    rows = sb_select('users', columns='name,email,picture,created_at')
    return jsonify({'users': rows, 'total': len(rows)})

# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'sources': ['saavn', 'youtube'], 'auth': 'google-oauth', 'db': 'supabase'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
