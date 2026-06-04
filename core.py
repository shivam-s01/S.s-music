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
import yt_dlp
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote
from difflib import SequenceMatcher
from collections import OrderedDict
from typing import Optional, Dict, Any, List, Tuple
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

import atexit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
ADMIN_KEY        = os.environ.get('ADMIN_KEY', '')
SUPABASE_URL     = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY     = os.environ.get('SUPABASE_KEY', '')
ACOUSTID_API_KEY = os.environ.get('ACOUSTID_API_KEY', '')  # FREE — acoustid.net pe register karo

if not GOOGLE_CLIENT_ID: raise RuntimeError('GOOGLE_CLIENT_ID env var is required')
if not ADMIN_KEY:         raise RuntimeError('ADMIN_KEY env var is required')
if not SUPABASE_URL:      raise RuntimeError('SUPABASE_URL env var is required')
if not SUPABASE_KEY:      raise RuntimeError('SUPABASE_KEY env var is required')

# ═══════════════════════════════════════════════════════════════════════════════
# APP INIT
# ═══════════════════════════════════════════════════════════════════════════════
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

_executor       = ThreadPoolExecutor(max_workers=20)
_executor_bg    = ThreadPoolExecutor(max_workers=10)
_executor_cache = ThreadPoolExecutor(max_workers=5)

_google_req = google_requests.Request()

# ═══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ═══════════════════════════════════════════════════════════════════════════════
class _CircuitBreaker:
    CLOSED = 'closed'; OPEN = 'open'; HALF = 'half'

    def __init__(self):
        self._state:          Dict[str, str]   = {}
        self._fail_count:     Dict[str, int]   = {}
        self._last_fail:      Dict[str, float] = {}
        self._success_streak: Dict[str, int]   = {}
        self._lock            = threading.Lock()
        self._OPEN_THRESHOLD  = 5
        self._RESET_SECS      = 60
        self._RECOVER_STREAK  = 2

    def _key(self, source): return source.lower().split(':')[0]

    def is_allowed(self, source):
        k = self._key(source)
        with self._lock:
            st = self._state.get(k, self.CLOSED)
            if st == self.CLOSED: return True
            if st == self.OPEN:
                if time.time() - self._last_fail.get(k, 0) >= self._RESET_SECS:
                    self._state[k] = self.HALF
                    return True
                return False
            return True

    def record_success(self, source):
        k = self._key(source)
        with self._lock:
            self._fail_count[k] = 0
            streak = self._success_streak.get(k, 0) + 1
            self._success_streak[k] = streak
            if self._state.get(k) in (self.HALF, self.OPEN) and streak >= self._RECOVER_STREAK:
                self._state[k] = self.CLOSED
                self._success_streak[k] = 0

    def record_failure(self, source):
        k = self._key(source)
        with self._lock:
            self._success_streak[k] = 0
            count = self._fail_count.get(k, 0) + 1
            self._fail_count[k] = count
            self._last_fail[k]  = time.time()
            if count >= self._OPEN_THRESHOLD and self._state.get(k) != self.OPEN:
                self._state[k] = self.OPEN
            elif self._state.get(k) == self.HALF:
                self._state[k] = self.OPEN

    def status(self):
        with self._lock:
            return {k: {'state': v, 'fails': self._fail_count.get(k, 0)}
                    for k, v in self._state.items()}

_cb = _CircuitBreaker()

atexit.register(lambda: _executor.shutdown(wait=False))
atexit.register(lambda: _executor_bg.shutdown(wait=False))
atexit.register(lambda: _executor_cache.shutdown(wait=False))

_YTDLP_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
    'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/120.0',
]

# ═══════════════════════════════════════════════════════════════════════════════
# SUPABASE HTTP HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _sb_headers():
    return {
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type':  'application/json',
        'Prefer':        'return=representation',
    }

def sb_select(table, filters=None, columns='*'):
    url = f"{SUPABASE_URL}/rest/v1/{table}?select={columns}"
    if filters:
        for k, v in filters.items():
            url += f"&{k}=eq.{quote(str(v), safe='')}"
    try:
        r = requests.get(url, headers=_sb_headers(), timeout=10)
        if r.status_code == 200: return r.json()
        log.warning(f"[Supabase] SELECT {table} error {r.status_code}: {r.text}")
    except Exception as e:
        log.warning(f"[Supabase] SELECT {table} exception: {e}")
    return []

def sb_upsert(table, data, on_conflict=None):
    url     = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = _sb_headers()
    if on_conflict:
        headers['Prefer'] = 'resolution=merge-duplicates,return=representation'
        url += f"?on_conflict={on_conflict}"
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        if r.status_code in (200, 201): return r.json()
        log.warning(f"[Supabase] UPSERT {table} error {r.status_code}: {r.text}")
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
        if r.status_code in (200, 204): return True
        log.warning(f"[Supabase] UPDATE {table} error {r.status_code}: {r.text}")
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
        if r.status_code in (200, 204): return True
        log.warning(f"[Supabase] DELETE {table} error {r.status_code}: {r.text}")
    except Exception as e:
        log.warning(f"[Supabase] DELETE {table} exception: {e}")
    return False

def init_db():
    log.info('[DB] Supabase ready — tables managed via Supabase SQL editor')

init_db()

# ═══════════════════════════════════════════════════════════════════════════════
# JWT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _verify_google_jwt(credential):
    try:
        payload = id_token.verify_oauth2_token(
            credential, _google_req, GOOGLE_CLIENT_ID, clock_skew_in_seconds=10)
        if payload.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
            return None
        return payload
    except Exception as e:
        log.warning(f'[Auth] JWT verify failed: {e}')
        return None

def _extract_bearer_sub(auth_header):
    if not auth_header or not auth_header.startswith('Bearer '): return None
    token = auth_header[7:].strip()
    if not token: return None
    payload = _verify_google_jwt(token)
    if not payload: return None
    return payload.get('sub', '') or None

# ═══════════════════════════════════════════════════════════════════════════════
# L1 CACHE — IN-MEMORY TTL LRU
# ═══════════════════════════════════════════════════════════════════════════════
class _LRUCache:
    def __init__(self, max_size, ttl):
        self._store: OrderedDict = OrderedDict()
        self._max   = max_size
        self._ttl   = ttl
        self._lock  = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self._store: return None
            ts, val = self._store[key]
            if time.time() - ts > self._ttl:
                del self._store[key]; return None
            self._store.move_to_end(key)
            return val

    def set(self, key, val, reset_ttl=True):
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                old_ts = self._store[key][0] if not reset_ttl else time.time()
                self._store[key] = (old_ts, val)
            else:
                self._store[key] = (time.time(), val)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def delete(self, key):
        with self._lock: self._store.pop(key, None)

    def size(self):
        with self._lock: return len(self._store)

    def evict_expired(self):
        now = time.time()
        with self._lock:
            expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
            for k in expired: del self._store[k]
        return len(expired)


_l1_meta     = _LRUCache(max_size=500, ttl=600)
_l1_audio    = _LRUCache(max_size=400, ttl=300)
_l1_popular  = _LRUCache(max_size=200, ttl=1800)
_l1_saavn    = _LRUCache(max_size=600, ttl=3600)
_l1_artwork  = _LRUCache(max_size=800, ttl=86400)
_l1_verified = _LRUCache(max_size=300, ttl=7200)

# Fingerprint cache — verified songs ka audio DNA store karo
_l1_fingerprint = _LRUCache(max_size=500, ttl=86400)  # 24hr


# ═══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE CONFIDENCE TUNER
# ═══════════════════════════════════════════════════════════════════════════════
def normalize(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


class _ConfidenceTuner:
    _DEFAULT = 0.65; _MIN = 0.45; _MAX = 0.80
    _NUDGE   = 0.03; _MAX_MISS = 3

    def __init__(self):
        self._floors: Dict[str, float] = {}
        self._misses: Dict[str, int]   = {}
        self._lock = threading.Lock()

    def _key(self, title, artist):
        return f"{normalize(title)[:40]}:{normalize(artist)[:20]}"

    def get_floor(self, title, artist):
        k = self._key(title, artist)
        with self._lock: return self._floors.get(k, self._DEFAULT)

    def record_miss(self, title, artist):
        k = self._key(title, artist)
        with self._lock:
            misses = self._misses.get(k, 0) + 1
            self._misses[k] = misses
            if misses >= self._MAX_MISS:
                current   = self._floors.get(k, self._DEFAULT)
                new_floor = max(self._MIN, current - self._NUDGE)
                if new_floor != current:
                    self._floors[k] = new_floor
                    self._misses[k] = 0

    def record_accept(self, title, artist, conf):
        k = self._key(title, artist)
        with self._lock:
            self._floors[k] = max(self._MIN, min(self._MAX, conf - 0.05))
            self._misses[k] = 0

    def status(self):
        with self._lock: return dict(self._floors)

_conf_tuner = _ConfidenceTuner()


def _artwork_key(title, artist=''):
    _artist_tokens = normalize(artist).split() if artist else []
    artist_norm = _artist_tokens[0] if _artist_tokens else ''
    return f"art:{normalize(title)}:{artist_norm}"

def _store_artwork(title, artist, image_url, source_priority=5):
    if not image_url or not image_url.startswith('http'): return
    key = _artwork_key(title, artist)
    existing = _l1_artwork.get(key)
    if existing and existing.get('priority', 99) <= source_priority: return
    _l1_artwork.set(key, {'url': image_url, 'priority': source_priority})

def _get_artwork(title, artist=''):
    key = _artwork_key(title, artist)
    hit = _l1_artwork.get(key)
    return hit.get('url', '') if hit else ''

def _verified_key(song_id='', title='', artist=''):
    if song_id: return f"verified:id:{song_id}"
    return f"verified:{normalize(title)}:{normalize(artist)}"

def _store_verified(song_id, title, artist, data, confidence):
    if confidence < 0.90: return
    if song_id: _l1_verified.set(_verified_key(song_id=song_id), data)
    if title:   _l1_verified.set(_verified_key(title=title, artist=artist), data)
    image = data.get('image', '')
    if image and image.startswith('http') and title:
        _store_artwork(title, artist, image, 3)

def _get_verified(song_id='', title='', artist=''):
    if song_id:
        hit = _l1_verified.get(_verified_key(song_id=song_id))
        if hit: return hit
    if title: return _l1_verified.get(_verified_key(title=title, artist=artist))
    return None


_meta_cache_lru  = _LRUCache(max_size=300, ttl=600)
_ytdlp_cache_lru = _LRUCache(max_size=200, ttl=240)
_meta_cache  = {}
_ytdlp_cache = {}

def _cache_get(key, store=None):
    if store is _ytdlp_cache or store is _ytdlp_cache_lru:
        return _ytdlp_cache_lru.get(key)
    return _meta_cache_lru.get(key)

def _cache_set(key, data, store=None):
    if store is _ytdlp_cache or store is _ytdlp_cache_lru:
        _ytdlp_cache_lru.set(key, data)
    else:
        _meta_cache_lru.set(key, data)

# L2 cache aliases used by fetchers
_cache_get_l2 = _cache_get
_cache_put_l2 = _cache_set

# ═══════════════════════════════════════════════════════════════════════════════
# L2 CACHE — SUPABASE
# ═══════════════════════════════════════════════════════════════════════════════
_SONG_CACHE_TTL       = 86400
_SAAVN_CDN_TTL        = 3600
_VOLATILE_CACHE_TTL   = 21600
_TEMP_CACHE_TTL       = 14400
_VOLATILE_SOURCES     = {'youtube', 'youtube-broad', 'piped', 'invidious', 'soundcloud'}
_CACHE_MIN_CONFIDENCE = 0.80

def _supabase_cache_get(cache_key):
    l1_hit = _l1_saavn.get(f"sb:{cache_key}")
    if l1_hit: return l1_hit
    try:
        rows = sb_select('song_cache', {'cache_key': cache_key})
        if not rows: return None
        row    = rows[0]
        age    = int(time.time()) - int(row.get('cached_at', 0))
        source = row.get('source', '')
        conf   = float(row.get('confidence', 1.0))
        if source in _VOLATILE_SOURCES:             ttl = _VOLATILE_CACHE_TTL
        elif source in ('saavn', 'jiosavan'):        ttl = _SAAVN_CDN_TTL
        else:                                        ttl = _SONG_CACHE_TTL
        if conf < _CACHE_MIN_CONFIDENCE:             ttl = _TEMP_CACHE_TTL
        if age > ttl:
            _executor_cache.submit(sb_delete, 'song_cache', {'cache_key': cache_key})
            return None
        if source not in _VOLATILE_SOURCES:
            _l1_saavn.set(f"sb:{cache_key}", row)
        return row
    except Exception as e:
        log.warning(f'[Cache:L2] get error: {e}')
        return None

def _supabase_cache_set(cache_key, data, confidence=1.0):
    _write_title = data.get('title', '')
    if (_is_remix_or_cover(_write_title) or
        _is_live_version(_write_title) or
        _is_slowed_reverb(_write_title)):
        log.debug(f'[Cache:L2] Blocked version write: "{_write_title}"')
        return
    if confidence < _CACHE_MIN_CONFIDENCE:
        log.debug(f'[Cache:L2] Skipping low-confidence write key={cache_key} conf={confidence:.2f}')
        return
    try:
        payload = {
            'cache_key':  cache_key,
            'url':        data.get('url', ''),
            'quality':    data.get('quality', ''),
            'title':      data.get('title', ''),
            'artist':     data.get('artist', ''),
            'image':      data.get('image', ''),
            'source':     data.get('source', ''),
            'confidence': round(confidence, 4),
            'cached_at':  int(time.time()),
        }
        sb_upsert('song_cache', payload, on_conflict='cache_key')
        source_written = data.get('source', '')
        if source_written not in _VOLATILE_SOURCES:
            _l1_saavn.set(f"sb:{cache_key}", payload)
        image  = data.get('image', '')
        title  = data.get('title', '')
        artist = data.get('artist', '')
        if image and title:
            source = data.get('source', '')
            art_priority = (
                1 if source in ('saavn', 'jiosavan') else
                2 if source == 'itunes' else
                4 if source == 'ytmusic' else 5
            )
            _store_artwork(title, artist, image, art_priority)
    except Exception as e:
        log.warning(f'[Cache:L2] set error: {e}')

# ═══════════════════════════════════════════════════════════════════════════════
# PROACTIVE URL HEALTH MONITOR
# ═══════════════════════════════════════════════════════════════════════════════
_url_refresh_queue = set()
_url_refresh_lock  = threading.Lock()
_URL_REFRESH_BEFORE_SECS = 300

def _schedule_url_refresh(cache_key, title, artist, source):
    if source in _VOLATILE_SOURCES: return
    with _url_refresh_lock:
        if cache_key not in _url_refresh_queue:
            _url_refresh_queue.add(cache_key)
            _executor_bg.submit(_do_url_refresh, cache_key, title, artist, source)

def _do_url_refresh(cache_key, title, artist, source):
    try:
        result = None
        if source in ('saavn', 'jiosavan'):
            song_id = _song_index_get(title, artist)
            if song_id: result = _fetch_saavn_by_id(song_id, title, artist)
            if not result:
                from match_engine import build_query_variants
                for qv in build_query_variants(title, artist, '')[:2]:
                    result = fetch_saavn_parallel(qv, title=title, artist=artist)
                    if result and result.get('url'): break
        if result and result.get('url'):
            conf = float(result.get('_confidence', result.get('score', 0.85)))
            _supabase_cache_set(cache_key, {**result, 'title': title, 'artist': artist}, conf)
    except Exception as e:
        log.debug(f'[URLRefresh] Failed: "{title}" — {e}')
    finally:
        with _url_refresh_lock:
            _url_refresh_queue.discard(cache_key)

def _supabase_cache_get_with_refresh(cache_key):
    l1_hit = _l1_saavn.get(f"sb:{cache_key}")
    if l1_hit: return l1_hit
    try:
        rows = sb_select('song_cache', {'cache_key': cache_key})
        if not rows: return None
        row    = rows[0]
        age    = int(time.time()) - int(row.get('cached_at', 0))
        source = row.get('source', '')
        conf   = float(row.get('confidence', 1.0))
        if source in _VOLATILE_SOURCES:             ttl = _VOLATILE_CACHE_TTL
        elif source in ('saavn', 'jiosavan'):        ttl = _SAAVN_CDN_TTL
        else:                                        ttl = _SONG_CACHE_TTL
        if conf < _CACHE_MIN_CONFIDENCE:             ttl = _TEMP_CACHE_TTL
        if age > ttl:
            _executor_cache.submit(sb_delete, 'song_cache', {'cache_key': cache_key})
            return None
        if (source not in _VOLATILE_SOURCES and
                age > ttl - _URL_REFRESH_BEFORE_SECS and row.get('title')):
            _schedule_url_refresh(cache_key, row['title'], row.get('artist', ''), source)
        if source not in _VOLATILE_SOURCES:
            _l1_saavn.set(f"sb:{cache_key}", row)
        return row
    except Exception as e:
        log.warning(f'[Cache:L2] get error: {e}')
        return None

# ═══════════════════════════════════════════════════════════════════════════════
# SONG INDEX
# ═══════════════════════════════════════════════════════════════════════════════
def _song_index_get(title, artist=''):
    key = f"idx:{normalize(title)}:{normalize(artist)}"
    hit = _l1_verified.get(key)
    if hit: return hit.get('saavn_id')
    try:
        rows = sb_select('song_index', {
            'search_title':  normalize(title)[:100],
            'search_artist': normalize(artist)[:50],
        }, columns='saavn_id,confirmed_title,artwork_url')
        if rows:
            _l1_verified.set(key, rows[0])
            return rows[0].get('saavn_id')
    except Exception: pass
    return None

def _song_index_put(title, artist, saavn_id, confirmed_title, artwork_url=''):
    if not title or not saavn_id: return
    try:
        key     = f"idx:{normalize(title)}:{normalize(artist)}"
        payload = {
            'search_title':    normalize(title)[:100],
            'search_artist':   normalize(artist)[:50],
            'saavn_id':        saavn_id,
            'confirmed_title': confirmed_title[:200],
            'artwork_url':     artwork_url[:500],
            'last_verified':   int(time.time()),
        }
        _l1_verified.set(key, payload)
        sb_upsert('song_index', payload, on_conflict='search_title,search_artist')
    except Exception as e:
        log.debug(f'[SongIndex] write error: {e}')


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIO FINGERPRINTING — PERMANENT MISMATCH PREVENTION
# AcoustID se audio DNA verify karo — language/genre/era se koi fark nahi
# Hindi, English, Bhojpuri, 90s — sab pe kaam karta hai
# ═══════════════════════════════════════════════════════════════════════════════

def _get_audio_fingerprint(url: str) -> tuple:
    """
    URL se pehle ~30 seconds ka audio fetch karke fingerprint banao.
    Return: (duration_seconds, fingerprint_string) or (None, None) on failure
    """
    try:
        import acoustid
        import chromaprint

        # Sirf pehle 512KB fetch karo — 30sec ke liye enough hai
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Range': 'bytes=0-524288',  # 512KB
        }
        r = requests.get(url, headers=headers, stream=True, timeout=8)
        audio_data = b''.join(r.iter_content(8192))
        if not audio_data:
            return None, None

        duration, fp = chromaprint.decode_audio_and_fingerprint(audio_data)
        return duration, fp
    except ImportError:
        # pyacoustid ya chromaprint install nahi — silently skip
        return None, None
    except Exception as e:
        log.debug(f'[Fingerprint] fetch error: {e}')
        return None, None


def verify_via_fingerprint(url: str, expected_title: str, expected_artist: str) -> bool:
    """
    AcoustID se verify karo ki URL wala song sahi hai ya nahi.

    WORKS FOR:
    - Hindi mainstream (100%)
    - English (100%)
    - 90s Bollywood (95% — mostly in AcoustID DB)
    - Bhojpuri (90% — rare songs AcoustID mein nahi, tab version-only check)

    Returns True if verified OR if fingerprint unavailable (safe fallback).
    Returns False only when we're SURE it's wrong.
    """
    if not ACOUSTID_API_KEY:
        # API key nahi — version DNA check pe fallback (still good protection)
        from match_engine import has_version_words
        result_has_version = has_version_words(expected_title)
        # Agar expected_title mein version word nahi toh assume correct
        return not result_has_version

    # L1 fingerprint cache check — ek baar verify ho gaya toh dobara nahi
    _fp_cache_key = f"fp_verified:{normalize(expected_title)}:{normalize(expected_artist)}"
    _cached_fp = _l1_fingerprint.get(_fp_cache_key)
    if _cached_fp is not None:
        return _cached_fp

    try:
        import acoustid

        duration, fp = _get_audio_fingerprint(url)

        if not fp or not duration:
            # Fingerprint nahi mila — DNA check pe fallback
            from match_engine import has_version_words
            _result = not has_version_words(expected_title)
            log.debug(f'[Fingerprint] No FP for "{expected_title}" — DNA fallback: {_result}')
            return _result

        # AcoustID lookup
        results = list(acoustid.lookup(
            ACOUSTID_API_KEY, fp, duration,
            meta='recordings releases'
        ))

        if not results:
            # AcoustID mein nahi mila — rare/regional song ho sakta hai
            # Bhojpuri, regional songs often not in AcoustID DB
            # Sirf version check karo
            from match_engine import has_version_words
            _result = not has_version_words(expected_title)
            log.debug(f'[Fingerprint] Not in AcoustID "{expected_title}" — DNA fallback: {_result}')
            _l1_fingerprint.set(_fp_cache_key, _result)
            return _result

        # Results milen — title/artist se match karo
        _exp_t = normalize(expected_title)
        _exp_a = normalize(expected_artist)

        best_score = 0.0
        for score, recording_id, title, artist in results:
            if score < 0.70:  # Low confidence AcoustID result — skip
                continue
            best_score = max(best_score, score)

            _res_t = normalize(title or '')
            _res_a = normalize(artist or '')

            # Title match check
            t_sim = SequenceMatcher(None, _exp_t, _res_t).ratio()
            # Artist match check (partial — pehla artist word bhi enough)
            a_sim = 0.0
            if _exp_a and _res_a:
                a_sim = SequenceMatcher(None, _exp_a, _res_a).ratio()
                # First word match bonus
                _exp_a_first = _exp_a.split()[0] if _exp_a.split() else ''
                _res_a_first = _res_a.split()[0] if _res_a.split() else ''
                if _exp_a_first and _res_a_first and _exp_a_first == _res_a_first:
                    a_sim = min(1.0, a_sim + 0.20)
            elif not _exp_a:
                a_sim = 0.5  # Artist unknown — neutral

            # Title match 0.75+ ya artist match 0.70+ — verified
            if t_sim >= 0.75 or a_sim >= 0.70:
                log.info(f'[Fingerprint] ✓ VERIFIED: "{expected_title}" t_sim={t_sim:.2f} a_sim={a_sim:.2f}')
                _l1_fingerprint.set(_fp_cache_key, True)
                return True

        # AcoustID mein mila lekin title/artist match nahi — WRONG SONG
        if best_score >= 0.85:
            log.warning(
                f'[Fingerprint] ✗ MISMATCH: requested="{expected_title}" '
                f'AcoustID returned different song (score={best_score:.2f})'
            )
            _l1_fingerprint.set(_fp_cache_key, False)
            return False

        # Inconclusive — DNA check pe fallback
        from match_engine import has_version_words
        _result = not has_version_words(expected_title)
        _l1_fingerprint.set(_fp_cache_key, _result)
        return _result

    except Exception as e:
        log.debug(f'[Fingerprint] Error for "{expected_title}": {e}')
        # Error pe safe fallback — assume correct
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# MATCH ENGINE — VERSION DETECTION
# ═══════════════════════════════════════════════════════════════════════════════
_REMIX_INDICATORS = [
    'dj remix', 'dj mix', 'dj version', 'dj edit', 'dj drop',
    'dj mashup', 'dj cut', 'dj blend', 'dj flip', 'dj bootleg', 'dj',
    'remix', 'remixed', 'mashup', 'mash up', 'cover', 'cover version',
    'tribute', 'flip', 'bootleg', 'rework', 'edit', 'reedited',
    'slowed', 'slowed down', 'slowed reverb', 'reverb', 'pitched',
    'sped up', 'speed up', 'sped-up', 'nightcore', 'chopped', 'screwed',
    'lofi', 'lo-fi', 'lo fi', 'chill mix', 'chill version',
    '8d audio', '8d', 'bass boosted', 'bass boost',
    'karaoke', 'instrumental', 'minus one',
    'extended mix', 'extended version', 'club mix', 'dance mix',
    'radio edit', 'club version',
    'live', 'live at', 'live from', 'live version', 'live session',
    'acoustic', 'unplugged', 'stripped', 'concert', 'performance', 'tour',
    'jhankar', 'jhankar beats', 'jhankar version',
    'superhit jhankar', 'electronic jhankar',
    'tapori mix', 'dhol mix', 'wedding mix',
    'bhangra mix', 'dandiya mix', 'garba mix',
    'club edit', 'festival mix', 'party mix',
    'lyric video', 'lyrics video', 'full video', 'beats version',
]

_DJ_RE = re.compile(r'\bdj\b', re.IGNORECASE)

_LIVE_INDICATORS = [
    'live', 'acoustic', 'unplugged', 'concert', 'live at', 'live from',
    'live version', 'live session', 'stripped',
    'coke studio', 'mtv unplugged', 'nescafe basement',
    'velo sound station', 'pepsi battle of bands',
    'studio session', 'home session', 'bedroom session',
    'radio session', 'spotify session', 'apple music session',
    'tiny desk', 'the stage', 'bbc session', 'season', 'remastered',
    'anniversary edition',
]

_SLOWED_INDICATORS = [
    'slowed', 'reverb', 'lofi', 'lo-fi', 'nightcore', 'sped up',
    'speed up', 'slowed down', '8d audio', 'bass boosted', 'pitched',
]

_SOURCE_CONFIDENCE = {
    'saavn':        1.00,
    'jiosavan':     0.98,
    'ytmusic':      0.85,
    'youtube':      0.70,
    'soundcloud':   0.65,
    'piped':        0.72,
    'invidious':    0.70,
    'youtube-broad': 0.40,
}

# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE PERFORMANCE RANKER
# ═══════════════════════════════════════════════════════════════════════════════
class _SourcePerf:
    _WINDOW = 50

    def __init__(self):
        self._latency: Dict[str, list] = {}
        self._success: Dict[str, list] = {}
        self._lock = threading.Lock()

    def record(self, source, latency_ms, success):
        with self._lock:
            lat = self._latency.setdefault(source, [])
            lat.append(latency_ms)
            if len(lat) > self._WINDOW: lat.pop(0)
            suc = self._success.setdefault(source, [])
            suc.append(1 if success else 0)
            if len(suc) > self._WINDOW: suc.pop(0)

    def score(self, source):
        with self._lock:
            lat = self._latency.get(source, [])
            suc = self._success.get(source, [])
        if not lat: return _SOURCE_CONFIDENCE.get(source, 0.60)
        p50   = sorted(lat)[len(lat)//2]
        sr    = sum(suc) / len(suc) if suc else 0.5
        lat_s = max(0.0, 1.0 - (p50 - 200) / 1800)
        return sr * 0.70 + lat_s * 0.30

    def ranked(self, sources):
        return sorted(sources, key=lambda s: self.score(s), reverse=True)

    def status(self):
        with self._lock:
            sources = set(self._latency) | set(self._success)
        return {s: {'score': round(self.score(s), 3), 'samples': len(self._latency.get(s, []))}
                for s in sources}

_src_perf = _SourcePerf()

_USER_VERSION_PHRASES = {
    'dj remix', 'dj mix', 'dj version', 'dj edit',
    'bass boosted', 'bass boost', 'slowed reverb', 'sped up', 'speed up',
    '8d audio', 'lo-fi', 'lo fi', 'lofi version', 'remix version',
    'acoustic version', 'unplugged version', 'live version', 'live at',
    'live from', 'live session', 'live concert', 'live performance',
    'live show', 'live recording', 'live in ',
    'instrumental version', 'karaoke version', 'cover version', 'acoustic cover',
}
_USER_VERSION_WORDS = {'lofi', 'remix', 'slowed', 'nightcore', 'reverb', 'mashup', 'karaoke', 'instrumental'}
_CONTEXT_ONLY_VERSION_WORDS = {'acoustic', 'unplugged', 'cover'}


def _query_requests_version(query: str) -> bool:
    q = query.lower().strip()
    for phrase in _USER_VERSION_PHRASES:
        if phrase in q: return True
    for word in _USER_VERSION_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', q): return True
    if re.search(r'\bdj\b', q): return True
    for word in _CONTEXT_ONLY_VERSION_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', q):
            if re.search(r'\b(version|ver|mix|edit|session|recording|perform|show)\b', q):
                return True
            if re.search(r'[-\u2013|]\s*' + re.escape(word) + r'\s*$', q):
                return True
    return False


def _normalize_artist(text: str) -> str:
    if not text: return ''
    t = text.lower()
    t = re.sub(r'\s*(feat\.?|ft\.?|featuring)\s+.*', '', t, flags=re.IGNORECASE)
    parts = re.split(r'\s*[&,]\s*|\s+x\s+', t)
    parts = [re.sub(r'[^a-z0-9\s]', '', p).strip() for p in parts if p.strip()]
    return re.sub(r'\s+', ' ', ' '.join(parts)).strip()


def _normalize_text(text: str) -> str:
    t = text.lower()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def _seq_ratio(a: str, b: str) -> float:
    if not a or not b: return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _word_overlap(a: str, b: str) -> float:
    wa = set(a.split()); wb = set(b.split())
    if not wa or not wb: return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


_AMBIGUOUS_VERSION_WORDS = {'live', 'acoustic', 'cover', 'edit', 'performance', 'concert', 'stripped', 'tribute', 'rework'}
_DEFINITE_VERSION_INDICATORS = {
    'remix', 'remixed', 'mashup', 'mash up', 'cover version',
    'slowed', 'slowed down', 'slowed reverb', 'reverb', 'pitched',
    'sped up', 'speed up', 'sped-up', 'nightcore', 'chopped', 'screwed',
    'lofi', 'lo-fi', 'lo fi', 'chill mix', 'chill version',
    '8d audio', '8d', 'bass boosted', 'bass boost',
    'karaoke', 'instrumental', 'minus one',
    'extended mix', 'extended version', 'club mix', 'dance mix',
    'radio edit', 'club version', 'club edit',
    'live version', 'live at', 'live from', 'live session',
    'acoustic version', 'unplugged',
    'jhankar', 'jhankar beats', 'tapori mix', 'dhol mix',
    'wedding mix', 'bhangra mix', 'dandiya mix', 'garba mix',
    'festival mix', 'party mix', 'beats version',
    'lyric video', 'lyrics video',
    'dj remix', 'dj mix', 'dj version', 'dj edit', 'dj drop',
    'dj mashup', 'dj cut', 'dj blend', 'dj flip', 'dj bootleg', 'bootleg', 'flip',
}


def _is_remix_or_cover(title: str) -> bool:
    t = title.lower()
    if _DJ_RE.search(title): return True
    for ind in _DEFINITE_VERSION_INDICATORS:
        if ' ' in ind:
            if ind in t: return True
        else:
            if re.search(r'\b' + re.escape(ind) + r'\b', t): return True
    _VERSION_CONTEXT = r'\b(version|ver|mix|edit|cover|remix|session|perform|show|concert|tour|record|cut)\b'
    for word in _AMBIGUOUS_VERSION_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', t):
            if re.search(_VERSION_CONTEXT, t): return True
            if word == 'live' and re.search(r'[\(\[\|]\s*live\s*[\)\]\|]', t): return True
            if re.search(r'[-–|]\s*' + re.escape(word) + r'\s*$', t): return True
    return False


def _is_live_version(title: str) -> bool:
    t = title.lower()
    for ind in _LIVE_INDICATORS:
        if ' ' in ind:
            if ind in t: return True
        else:
            if re.search(r'\b' + re.escape(ind) + r'\b', t): return True
    return False


def _is_slowed_reverb(title: str) -> bool:
    t = title.lower()
    for ind in _SLOWED_INDICATORS:
        if ' ' in ind:
            if ind in t: return True
        else:
            if re.search(r'\b' + re.escape(ind) + r'\b', t): return True
    return False


_DEVOTIONAL_KEYWORDS = [
    'chalisa', 'aarti', 'bhajan', 'stuti', 'mantra', 'stotra',
    'vandana', 'kirtan', 'prarthana', 'hanuman', 'ganesh', 'durga',
    'gayatri', 'om jai', 'jai shri', 'shiv', 'krishna', 'radhe',
    'sai baba', 'qawwali', 'naat', 'hamd', 'ramayan', 'mahabharat',
    'jai ganesh', 'saraswati', 'lakshmi', 'mata', 'devi',
]


def _is_devotional_query(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _DEVOTIONAL_KEYWORDS)


_HINDI_COVER_MARKERS = ['ki', 'ka', 'ke', 'hai', 'mein', 'se', 'tera', 'mera',
                        'pyar', 'dil', 'ishq', 'mohabbat', 'tum', 'hum']


def _is_english_song_query(title: str, artist: str) -> bool:
    from match_engine import _detect_language
    lang = _detect_language(title + ' ' + artist)
    if lang == 'english': return True
    combined = (title + ' ' + artist).strip()
    if not combined: return False
    ascii_ratio = sum(1 for c in combined if ord(c) < 128) / len(combined)
    return ascii_ratio > 0.88 and lang == ''


_BHOJPURI_ARTISTS = {
    'pawan singh', 'khesari lal', 'dinesh lal', 'nirahua',
    'ritesh pandey', 'ankush raja', 'pramod premi', 'kallu',
    'shilpi raj', 'gunjan singh', 'neelkamal singh', 'samar singh',
    'arvind akela', 'vijay chauhan', 'manoj tiwari', 'devi',
    'indu sonali', 'priyanka singh', 'rani chatterjee',
}


def compute_confidence(
    query_title:  str,
    query_artist: str,
    result_title: str,
    result_artist: str,
    query_duration_s:  int = 0,
    result_duration_s: int = 0,
    source: str = '',
) -> float:

    # ── STEP 0: DNA gate — ye confidence se PEHLE hota hai ──────────────────
    # Import here to avoid circular import
    try:
        from match_engine import dna_compatible
        if not dna_compatible(query_title, result_title):
            return 0.0
    except ImportError:
        pass

    from match_engine import _detect_language, _bhojpuri_normalize
    _lang = _detect_language(query_title + ' ' + query_artist)
    if _lang == 'bhojpuri':
        qt = _bhojpuri_normalize(query_title)
        rt = _bhojpuri_normalize(result_title)
    else:
        qt = _normalize_text(query_title)
        rt = _normalize_text(result_title)
    qa = _normalize_text(query_artist)
    ra = _normalize_text(result_artist)

    qa_norm = _normalize_artist(qa)
    ra_norm = _normalize_artist(ra)

    # ── STEP 1: EARLY ARTIST REJECT — confidence calculate karne se pehle ───
    if qa_norm and ra_norm:
        _early_a_seq  = _seq_ratio(qa_norm, ra_norm)
        _early_a_word = _word_overlap(qa_norm, ra_norm)
        _early_a_sim  = _early_a_seq * 0.5 + _early_a_word * 0.5
        # Artist bilkul alag — reject immediately
        if _early_a_sim < 0.20 and _seq_ratio(qt, rt) < 0.98:
            return 0.0

    # Title similarity (45% — artist weight badhaya)
    t_seq  = _seq_ratio(qt, rt)
    t_word = _word_overlap(qt, rt)
    t_sim  = (t_seq * 0.6 + t_word * 0.4)

    if rt.startswith(qt) or qt.startswith(rt):
        suffix = rt[len(qt):].strip() if rt.startswith(qt) else qt[len(rt):].strip()
        if not any(ind in suffix for ind in _REMIX_INDICATORS):
            t_sim = min(1.0, t_sim + 0.15)

    # Artist similarity (42% — increased from 35%)
    a_sim = 0.0
    if qa_norm and ra_norm:
        a_seq  = _seq_ratio(qa_norm, ra_norm)
        a_word = _word_overlap(qa_norm, ra_norm)
        a_sim  = a_seq * 0.5 + a_word * 0.5
        qa_first = qa_norm.split()[0] if qa_norm.split() else ''
        ra_first = ra_norm.split()[0] if ra_norm.split() else ''
        if qa_first and ra_first and _seq_ratio(qa_first, ra_first) >= 0.80:
            a_sim = min(1.0, a_sim + 0.10)
    elif not qa_norm:
        a_sim = 0.5

    # Duration (8%)
    # FIX: duration=0 pe d_sim=0.42 artificial boost hataya
    d_sim = 0.50  # neutral — was 0.42 jo artificially low tha

    if query_duration_s > 0 and result_duration_s > 0:
        delta = abs(query_duration_s - result_duration_s)
        d_sim = max(0.0, 1.0 - (delta / 45.0) ** 0.8)
    elif result_duration_s > 0 and query_duration_s == 0:
        if result_duration_s > 600:   d_sim = 0.0
        elif result_duration_s > 480: d_sim = 0.20
        else:                         d_sim = 0.45

    # Source confidence (5%)
    s_conf = _SOURCE_CONFIDENCE.get(source, 0.60)

    # FIX: Artist weight 35→42%, Title weight 50→45%
    conf = (t_sim * 0.45) + (a_sim * 0.42) + (d_sim * 0.08) + (s_conf * 0.05)

    if qt and rt and qt == rt:
        conf = min(1.0, conf + 0.15)

    # Decade hint bonus
    _query_combined = (query_title + ' ' + query_artist).lower()
    _DECADE_HINTS = {
        '90': (1990, 1999), '1990': (1990, 1999),
        'purane': (1970, 2004), 'purana': (1970, 2004), 'purani': (1970, 2004),
        'old': (1970, 2004), '80': (1980, 1989), '1980': (1980, 1989),
        '70': (1970, 1979), '2000': (2000, 2009),
    }
    for hint in _DECADE_HINTS:
        if hint in _query_combined:
            conf = min(1.0, conf + 0.05)
            break

    # Hard artist mismatch rejection
    if qa_norm and ra_norm and a_sim < 0.50 and t_sim < 0.95:
        if qt != rt:
            return 0.0

    # Duration penalty
    if query_duration_s > 0 and result_duration_s > 0:
        dur_ratio = abs(query_duration_s - result_duration_s) / max(query_duration_s, 1)
        if dur_ratio > 0.30:  return 0.0
        elif dur_ratio > 0.18: conf -= 0.15

    # Long form reject
    _LONG_FORM_KW = ['jukebox', 'full album', 'nonstop', 'medley', 'mashup songs', 'all songs']
    _query_is_longform  = any(kw in (query_title + ' ' + query_artist).lower() for kw in _LONG_FORM_KW)
    _is_devotional_ctx  = _is_devotional_query(query_title + ' ' + query_artist)
    if result_duration_s > 600 and not _query_is_longform and not _is_devotional_ctx:
        return 0.0
    if (result_duration_s > 480 and query_duration_s > 0
            and query_duration_s < 380 and not _is_devotional_ctx):
        return 0.0

    # English query → Hindi cover reject
    if _is_english_song_query(query_title, query_artist):
        if qa_norm and ra_norm and a_sim < 0.35: return 0.0
        _result_words = normalize(result_title).split()
        _hindi_hits   = sum(1 for w in _result_words if w in _HINDI_COVER_MARKERS)
        if _hindi_hits >= 2: return 0.0

    # Bhojpuri artist gate
    _qa_bhoj = qa.lower()
    if any(a in _qa_bhoj for a in _BHOJPURI_ARTISTS):
        if qa_norm and ra_norm and a_sim < 0.55: return 0.0

    # Version mismatch — ye ab DNA gate se pehle handle ho chuka hai
    # Yahan sirf penalty logic
    user_wants_version = _query_requests_version(query_title)
    query_is_remix     = _is_remix_or_cover(query_title)
    result_is_remix    = _is_remix_or_cover(rt)
    query_is_live      = _is_live_version(query_title)
    result_is_live     = _is_live_version(rt)
    query_is_slowed    = _is_slowed_reverb(query_title)
    result_is_slowed   = _is_slowed_reverb(rt)

    if not user_wants_version:
        _query_starts_with_dj = bool(re.match(r'^dj\b', qt, re.IGNORECASE))
        if result_is_remix  and not query_is_remix  and not _query_starts_with_dj: return 0.0
        if result_is_slowed and not query_is_slowed:                                return 0.0
        if result_is_live   and not query_is_live:                                  return 0.0
        _res_lower = rt.lower()
        _HARD_BLOCK_PATTERNS = [
            r'\blofi\b', r'\blo fi\b', r'\blo-fi\b',
            r'\bslowed\b', r'\breverb\b', r'\bnightcore\b',
            r'\bsped up\b', r'\bspeed up\b', r'\bbass boost\b',
            r'\b8d audio\b', r'\bkaraoke\b',
        ]
        for pat in _HARD_BLOCK_PATTERNS:
            if re.search(pat, _res_lower): return 0.0
    elif not result_is_remix and query_is_remix:
        conf -= 0.10

    return max(0.0, min(1.0, conf))


def is_likely_duplicate(a: dict, b: dict, threshold: float = 0.92) -> bool:
    ta = _normalize_text(a.get('trackName') or a.get('title', ''))
    tb = _normalize_text(b.get('trackName') or b.get('title', ''))
    aa = _normalize_text(a.get('artistName') or a.get('artist', ''))
    ab = _normalize_text(b.get('artistName') or b.get('artist', ''))
    t_sim = _seq_ratio(ta, tb)
    a_sim = _seq_ratio(aa, ab) if aa and ab else 0.5
    return (t_sim * 0.7 + a_sim * 0.3) >= threshold


def _is_confirmed_match(
    req_title:   str,
    req_artist:  str,
    res_title:   str,
    res_artist:  str,
    source:      str = '',
    duration_s:  int = 0,
    res_dur_s:   int = 0,
    min_conf:    float = 0.65,
) -> tuple:
    if not res_title:
        return False, 0.0, 'empty_title'

    # ── GATE 0: DNA check — SABSE PEHLE, kabhi bypass nahi ─────────────────
    try:
        from match_engine import dna_compatible, get_song_dna
        if not dna_compatible(req_title, res_title):
            _res_dna = get_song_dna(res_title)
            return False, 0.0, f'dna_mismatch:{_res_dna}'
    except ImportError:
        pass

    # ── GATE 1: Version rejection ────────────────────────────────────────────
    _user_wants_ver = _query_requests_version(req_title)
    if not _user_wants_ver:
        if _is_remix_or_cover(res_title):  return False, 0.0, 'remix_cover_rejected'
        if _is_slowed_reverb(res_title):   return False, 0.0, 'slowed_reverb_rejected'
        if _is_live_version(res_title):    return False, 0.0, 'live_version_rejected'

    # ── GATE 2: Devotional remix block ──────────────────────────────────────
    if _is_devotional_query(req_title + ' ' + req_artist):
        if _is_remix_or_cover(res_title) or _is_slowed_reverb(res_title):
            return False, 0.0, 'devotional_remix_rejected'
        _res_lower = res_title.lower()
        if any(w in _res_lower for w in ['dj', 'club', 'party', 'dance', 'rave']):
            return False, 0.0, 'devotional_club_rejected'

    # ── GATE 3: Confidence score ─────────────────────────────────────────────
    # Use tuner floor — adaptive threshold
    tuner_floor = _conf_tuner.get_floor(req_title, req_artist)
    effective_min_conf = max(min_conf, tuner_floor)

    conf = compute_confidence(
        req_title, req_artist, res_title, res_artist,
        query_duration_s=duration_s,
        result_duration_s=res_dur_s,
        source=source,
    )

    if conf < effective_min_conf:
        return False, conf, f'low_conf_{conf:.3f}'

    # ── GATE 4: Word overlap check ───────────────────────────────────────────
    _req_words = set(w for w in normalize(req_title).split() if len(w) >= 3)
    _res_words = set(w for w in normalize(res_title).split() if len(w) >= 3)
    if _req_words and _res_words and not _req_words.intersection(_res_words):
        _seq = _seq_ratio(normalize(req_title), normalize(res_title))
        if _seq < 0.55:
            return False, conf, f'no_word_overlap_seq={_seq:.3f}'

    # ── GATE 5: Artist mismatch final check ─────────────────────────────────
    if req_artist and res_artist:
        _ra = _normalize_artist(normalize(req_artist))
        _rb = _normalize_artist(normalize(res_artist))
        if _ra and _rb:
            _a_sim = _seq_ratio(_ra, _rb)
            if _a_sim < 0.30:
                return False, conf, f'artist_mismatch_{_a_sim:.3f}'

    return True, conf, 'ok'


# Placeholder — fetchers.py se import hoga
def fetch_saavn_parallel(query, title='', artist='', language=''):
    pass

def _fetch_saavn_by_id(song_id, expected_title='', expected_artist=''):
    pass
