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

limiter     = Limiter(get_real_ip, app=app, default_limits=[], storage_uri="memory://")
_executor   = ThreadPoolExecutor(max_workers=40)   # +8 for background tasks
_google_req = google_requests.Request()

# Cleanly shut down thread pool on process exit (avoids hanging threads on Render restart)
atexit.register(lambda: _executor.shutdown(wait=False))


# ═══════════════════════════════════════════════════════════════════════════════
# SUPABASE HTTP HELPERS  (unchanged, battle-tested)
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
        if r.status_code == 200:
            return r.json()
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
        if r.status_code in (200, 201):
            return r.json()
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
def _verify_google_jwt(credential: str) -> Optional[dict]:
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

def _extract_bearer_sub(auth_header: str) -> Optional[str]:
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    token = auth_header[7:].strip()
    if not token: return None
    payload = _verify_google_jwt(token)
    if not payload: return None
    return payload.get('sub', '') or None


# ═══════════════════════════════════════════════════════════════════════════════
# ██████████████████████  L1 CACHE — IN-MEMORY TTL  ███████████████████████████
# ═══════════════════════════════════════════════════════════════════════════════
# Three distinct L1 pools:
#   _l1_meta     → search results / metadata   (600s TTL, max 500 entries)
#   _l1_audio    → resolved audio URLs         (300s TTL, max 400 entries)
#   _l1_popular  → hot / popular song cache    (1800s TTL, max 200 entries)
#
# LRU eviction via OrderedDict — O(1) get/set.
# ═══════════════════════════════════════════════════════════════════════════════

class _LRUCache:
    """Thread-safe LRU cache with TTL support."""

    def __init__(self, max_size: int, ttl: int):
        self._store: OrderedDict = OrderedDict()
        self._max   = max_size
        self._ttl   = ttl
        self._lock  = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._store:
                return None
            ts, val = self._store[key]
            if time.time() - ts > self._ttl:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return val

    def set(self, key: str, val: Any, reset_ttl: bool = True) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                # FIX BUG 8: preserve original ts unless reset_ttl=True
                # prevents stale entries (wrong song, lofi) from getting TTL extended on re-set
                old_ts = self._store[key][0] if not reset_ttl else time.time()
                self._store[key] = (old_ts, val)
            else:
                self._store[key] = (time.time(), val)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def evict_expired(self):
        now = time.time()
        with self._lock:
            expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
            for k in expired:
                del self._store[k]
        return len(expired)


_l1_meta     = _LRUCache(max_size=500, ttl=600)     # search / metadata results
_l1_audio    = _LRUCache(max_size=400, ttl=300)     # resolved audio URLs (yt-dlp, SC)
_l1_popular  = _LRUCache(max_size=200, ttl=1800)    # hot songs / popular searches
_l1_saavn    = _LRUCache(max_size=600, ttl=3600)    # Saavn stream URLs (stable CDN)
_l1_artwork  = _LRUCache(max_size=800, ttl=86400)   # artwork URLs — independent of audio source
_l1_verified = _LRUCache(max_size=300, ttl=7200)    # verified high-confidence song results


def _artwork_key(title: str, artist: str = '') -> str:
    # FIX BUG T4: normalize artist to first token only (strip feat/collab/comma parts)
    # so "Arijit Singh, Shreya Ghoshal" and "Arijit Singh" hit the same cache key.
    # Guard against whitespace-only artist → normalize returns '' → split() = [] → [0] IndexError
    _artist_tokens = normalize(artist).split() if artist else []
    artist_norm = _artist_tokens[0] if _artist_tokens else ''
    return f"art:{normalize(title)}:{artist_norm}"


def _store_artwork(title: str, artist: str, image_url: str, source_priority: int = 5):
    """
    Store artwork only if better than what's cached.
    Priority: 1=Saavn, 2=iTunes, 3=last-verified, 4=ytmusic, 5=other
    Lower number = higher priority.
    """
    if not image_url or not image_url.startswith('http'):
        return
    key = _artwork_key(title, artist)
    existing = _l1_artwork.get(key)
    if existing:
        existing_priority = existing.get('priority', 99)
        if source_priority >= existing_priority:
            return   # don't overwrite with lower-priority artwork
    _l1_artwork.set(key, {'url': image_url, 'priority': source_priority})


def _get_artwork(title: str, artist: str = '') -> str:
    """
    Retrieve best available artwork. Never returns empty if any artwork was stored.
    Priority order: Saavn → iTunes → last-verified → ytmusic → other
    """
    key = _artwork_key(title, artist)
    hit = _l1_artwork.get(key)
    if hit:
        return hit.get('url', '')
    return ''

def _verified_key(song_id: str = '', title: str = '', artist: str = '') -> str:
    if song_id:
        return f"verified:id:{song_id}"
    return f"verified:{normalize(title)}:{normalize(artist)}"

def _store_verified(song_id: str, title: str, artist: str, data: dict, confidence: float):
    """AGGRESSIVE: Store in verified cache only if conf ≥ 0.90 (was 0.85)."""
    if confidence < 0.90:
        return
    if song_id:
        _l1_verified.set(_verified_key(song_id=song_id), data)
    if title:
        _l1_verified.set(_verified_key(title=title, artist=artist), data)
    # Store artwork at priority 3 (last-verified) — persists beyond audio cache expiry
    image = data.get('image', '')
    if image and image.startswith('http') and title:
        _store_artwork(title, artist, image, 3)

def _get_verified(song_id: str = '', title: str = '', artist: str = '') -> Optional[dict]:
    if song_id:
        hit = _l1_verified.get(_verified_key(song_id=song_id))
        if hit:
            return hit
    if title:
        return _l1_verified.get(_verified_key(title=title, artist=artist))
    return None


# FIX 1: Legacy plain-dict caches replaced with thread-safe LRUCache
# Old _meta_cache/{} was not thread-safe — race condition on concurrent writes
_meta_cache_lru  = _LRUCache(max_size=300, ttl=600)
_ytdlp_cache_lru = _LRUCache(max_size=200, ttl=240)
# Keep references so any external code using the old names still works
_meta_cache  = {}   # unused now but kept for import safety
_ytdlp_cache = {}

META_CACHE_TTL  = 600
YTDLP_CACHE_TTL = 240

def _cache_get(key, store=None):
    # FIX BUG 3: compare by id() not identity — always use correct LRU
    if store is _ytdlp_cache or store is _ytdlp_cache_lru:
        return _ytdlp_cache_lru.get(key)
    return _meta_cache_lru.get(key)

def _cache_set(key, data, store=None):
    if store is _ytdlp_cache or store is _ytdlp_cache_lru:
        _ytdlp_cache_lru.set(key, data)
    else:
        _meta_cache_lru.set(key, data)


# ═══════════════════════════════════════════════════════════════════════════════
# ██████████████████████  L2 CACHE — SUPABASE  ████████████████████████████████
# ═══════════════════════════════════════════════════════════════════════════════
# Two-tier Supabase cache:
#   - VERIFIED cache  → confidence ≥ 0.80, TTL 86400s
#   - TEMPORARY cache → confidence < 0.80, TTL 21600s (volatile sources)
#
# Cache poisoning prevention:
#   Songs with confidence < CACHE_MIN_CONFIDENCE are NEVER written to L2.
#   Volatile sources (YouTube/Piped/Invidious) get shorter TTL + HEAD check.
# ═══════════════════════════════════════════════════════════════════════════════

_SONG_CACHE_TTL       = 86400
_VOLATILE_CACHE_TTL   = 21600
_TEMP_CACHE_TTL       = 14400
_VOLATILE_SOURCES     = {'youtube', 'youtube-broad', 'piped', 'invidious', 'soundcloud'}
_CACHE_MIN_CONFIDENCE = 0.80   # AGGRESSIVE: was 0.75 — only high-confidence results enter L2

def _supabase_cache_get(cache_key: str) -> Optional[dict]:
    # Check L1 first (zero network cost)
    l1_hit = _l1_saavn.get(f"sb:{cache_key}")
    if l1_hit:
        return l1_hit
    try:
        rows = sb_select('song_cache', {'cache_key': cache_key})
        if not rows: return None
        row    = rows[0]
        age    = int(time.time()) - int(row.get('cached_at', 0))
        source = row.get('source', '')
        conf   = float(row.get('confidence', 1.0))
        ttl    = _VOLATILE_CACHE_TTL if source in _VOLATILE_SOURCES else _SONG_CACHE_TTL
        if conf < _CACHE_MIN_CONFIDENCE:
            ttl = _TEMP_CACHE_TTL
        if age > ttl:
            _executor.submit(sb_delete, 'song_cache', {'cache_key': cache_key})
            return None
        # HEAD-check volatile URLs to detect expired CDN links
        # Rate-limited: once per URL per 60s to avoid per-request blocking
        if source in _VOLATILE_SOURCES:
            _head_check_key = f"hc:{cache_key}"
            _last_hc = _l1_audio.get(_head_check_key)
            if not _last_hc:
                try:
                    head = requests.head(row['url'], timeout=3, allow_redirects=True,
                                         headers={'User-Agent': 'Mozilla/5.0'})
                    if head.status_code >= 400:
                        _executor.submit(sb_delete, 'song_cache', {'cache_key': cache_key})
                        return None
                    # mark checked — TTL 60s
                    _l1_audio.set(_head_check_key, True)
                except Exception:
                    pass
        # Promote to L1 only for stable sources.
        # Volatile source URLs (YouTube/Piped/Invidious/SC) expire in minutes —
        # promoting them to _l1_saavn (TTL=3600s) would serve stale 403s.
        if source not in _VOLATILE_SOURCES:
            _l1_saavn.set(f"sb:{cache_key}", row)
        return row
    except Exception as e:
        log.warning(f'[Cache:L2] get error: {e}')
        return None

def _supabase_cache_set(cache_key: str, data: dict, confidence: float = 1.0):
    """Write to L2 only if confidence meets threshold (anti-poisoning)."""
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
            'confidence': round(confidence, 4),   # FIX 17: was missing from payload → L1 re-read returned 1.0
            'cached_at':  int(time.time()),
        }
        sb_upsert('song_cache', payload, on_conflict='cache_key')
        # Write to L1 only for stable sources — volatile URLs expire in minutes
        source_written = data.get('source', '')
        if source_written not in _VOLATILE_SOURCES:
            _l1_saavn.set(f"sb:{cache_key}", payload)
        # Persist artwork independently — survives audio cache expiry
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
# ██████████████████  MULTI-LAYER MATCH ENGINE  ███████████████████████████████
# ═══════════════════════════════════════════════════════════════════════════════
# Confidence scoring system:
#
#   title_sim    — SequenceMatcher + word overlap (40%)
#   artist_sim   — SequenceMatcher + token match  (25%)
#   duration_sim — exponential decay |Δs| / 30     (15%)
#   source_conf  — source reliability bonus        (10%)
#   remix_penalty — remix/slowed/cover detection  (-20% if triggered)
#   duplicate    — exact duplicate detection
#
# Final confidence ∈ [0, 1.0]
# Only results with confidence ≥ 0.75 enter L2 cache.
# ═══════════════════════════════════════════════════════════════════════════════

_REMIX_INDICATORS = [
    # DJ variants — all possible spellings/formats
    'dj remix', 'dj mix', 'dj version', 'dj edit', 'dj drop',
    'dj mashup', 'dj cut', 'dj blend', 'dj flip', 'dj bootleg',
    # standalone dj — checked via regex word boundary (not substring)
    'dj',
    # Remix / mashup / cover family
    'remix', 'remixed', 'mashup', 'mash up', 'cover', 'cover version',
    'tribute', 'flip', 'bootleg', 'rework', 'edit', 'reedited',
    # Slowed / speed family
    'slowed', 'slowed down', 'slowed reverb', 'reverb', 'pitched',
    'sped up', 'speed up', 'sped-up', 'nightcore', 'chopped', 'screwed',
    # Lofi family
    'lofi', 'lo-fi', 'lo fi', 'chill mix', 'chill version',
    # Bass / audio effects
    '8d audio', '8d', 'bass boosted', 'bass boost',
    # Karaoke / instrumental
    'karaoke', 'instrumental', 'minus one',
    # Club / extended
    'extended mix', 'extended version', 'club mix', 'dance mix',
    'radio edit', 'club version',
    # Live / acoustic
    'live', 'live at', 'live from', 'live version', 'live session',
    'acoustic', 'unplugged', 'stripped', 'concert', 'performance',
    'tour',
]
# NOTE: 'version' intentionally NOT in list — causes false positives on
# legitimate Saavn titles like "Deluxe Version", "2024 Version", etc.

# Pre-compiled DJ regex — catches all caps/spacing variants: DJ, dj, Dj
_DJ_RE = re.compile(r'\bdj\b', re.IGNORECASE)

_LIVE_INDICATORS = [
    'live', 'acoustic', 'unplugged', 'concert', 'live at', 'live from',
    'live version', 'live session', 'stripped',
]

_SLOWED_INDICATORS = [
    'slowed', 'reverb', 'lofi', 'lo-fi', 'nightcore', 'sped up',
    'speed up', 'slowed down', '8d audio', 'bass boosted', 'pitched',
]

_SOURCE_CONFIDENCE = {
    'saavn':     1.00,
    'jiosavan':  0.98,
    'ytmusic':   0.85,
    'youtube':   0.70,
    'soundcloud': 0.65,
    'piped':     0.72,
    'invidious': 0.70,
    'youtube-broad': 0.40,
}

_USER_VERSION_KEYWORDS = {
    'lofi', 'lo-fi', 'dj', 'remix', 'slowed', 'nightcore', 'cover', 'live',
    'reverb', 'bass boosted', 'instrumental', 'acoustic', 'unplugged', 'sped up',
    'speed up', '8d', 'mashup', 'karaoke',
}

def _query_requests_version(query: str) -> bool:
    """True if user's query explicitly contains a version keyword."""
    q = query.lower()
    return any(kw in q for kw in _USER_VERSION_KEYWORDS)

def _normalize_artist(text: str) -> str:
    """
    Strip feat/ft/featuring/&/x/, collaborators so artist cores can be matched.
    'Arijit Singh feat. Shreya Ghoshal' → 'arijit singh'
    'Arijit'                            → 'arijit'
    """
    if not text:
        return ''
    t = text.lower()
    # Remove feat/ft/featuring and everything after
    t = re.sub(r'\s*(feat\.?|ft\.?|featuring)\s+.*', '', t, flags=re.IGNORECASE)
    # Split on & / x / , and take first token group
    t = re.split(r'\s*[&,]\s*|\s+x\s+', t)[0]
    t = re.sub(r'[^a-z0-9\s]', '', t)
    return re.sub(r'\s+', ' ', t).strip()

def _normalize_text(text: str) -> str:
    t = text.lower()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    return re.sub(r'\s+', ' ', t).strip()

def _seq_ratio(a: str, b: str) -> float:
    if not a or not b: return 0.0
    return SequenceMatcher(None, a, b).ratio()

def _word_overlap(a: str, b: str) -> float:
    wa = set(a.split())
    wb = set(b.split())
    if not wa or not wb: return 0.0
    intersection = len(wa & wb)
    return intersection / max(len(wa), len(wb))

def _is_remix_or_cover(title: str) -> bool:
    t = title.lower()
    # Fast path: DJ regex (handles DJ, dj, Dj — all caps variants)
    if _DJ_RE.search(title):
        return True
    for ind in _REMIX_INDICATORS:
        if ind == 'dj':
            continue   # already handled above
        # Multi-word: plain substring match (e.g. 'extended mix', 'bass boosted')
        if ' ' in ind:
            if ind in t:
                return True
        else:
            # Single-word: word boundary to avoid 'cover'→'recover', 'edit'→'credit'
            if re.search(r'\b' + re.escape(ind.strip()) + r'\b', t):
                return True
    return False

def _is_live_version(title: str) -> bool:
    t = title.lower()
    for ind in _LIVE_INDICATORS:
        if ' ' in ind:
            if ind in t:
                return True
        else:
            if re.search(r'\b' + re.escape(ind) + r'\b', t):
                return True
    return False

def _is_slowed_reverb(title: str) -> bool:
    t = title.lower()
    for ind in _SLOWED_INDICATORS:
        if ' ' in ind:
            if ind in t:
                return True
        else:
            if re.search(r'\b' + re.escape(ind) + r'\b', t):
                return True
    return False

def compute_confidence(
    query_title:  str,
    query_artist: str,
    result_title: str,
    result_artist: str,
    query_duration_s:  int = 0,
    result_duration_s: int = 0,
    source: str = '',
) -> float:
    """
    Multi-layer confidence engine.
    Returns float ∈ [0.0, 1.0].
    """
    # Language-aware normalization: Bhojpuri gets extra translit pass
    _lang = _detect_language(query_title + ' ' + query_artist)
    if _lang == 'bhojpuri':
        qt = _bhojpuri_normalize(query_title)
        rt = _bhojpuri_normalize(result_title)
    else:
        qt = _normalize_text(query_title)
        rt = _normalize_text(result_title)
    qa = _normalize_text(query_artist)
    ra = _normalize_text(result_artist)

    # ── Title similarity (50% weight) ─────────────────────────────────────────
    t_seq  = _seq_ratio(qt, rt)
    t_word = _word_overlap(qt, rt)
    t_sim  = (t_seq * 0.6 + t_word * 0.4)

    # prefix boost: result starts with query — only if suffix is not a version indicator
    if rt.startswith(qt) or qt.startswith(rt):
        suffix = rt[len(qt):].strip() if rt.startswith(qt) else qt[len(rt):].strip()
        if not any(ind in suffix for ind in _REMIX_INDICATORS):
            t_sim = min(1.0, t_sim + 0.15)

    # ── Artist similarity (35% weight) ────────────────────────────────────────
    # Normalize artists before comparing: strip feat/ft/featuring/&/x/,
    qa_norm = _normalize_artist(qa)
    ra_norm = _normalize_artist(ra)
    a_sim = 0.0
    if qa_norm and ra_norm:
        a_seq  = _seq_ratio(qa_norm, ra_norm)
        a_word = _word_overlap(qa_norm, ra_norm)
        a_sim  = a_seq * 0.5 + a_word * 0.5
        # partial artist: first token match
        qa_first = qa_norm.split()[0] if qa_norm.split() else ''
        ra_first = ra_norm.split()[0] if ra_norm.split() else ''
        if qa_first and ra_first and _seq_ratio(qa_first, ra_first) >= 0.80:
            a_sim = min(1.0, a_sim + 0.10)
    elif not qa_norm:
        a_sim = 0.5  # neutral when query has no artist

    # ── Duration similarity (10% weight) ──────────────────────────────────────
    d_sim = 0.5  # neutral default
    if query_duration_s > 0 and result_duration_s > 0:
        delta = abs(query_duration_s - result_duration_s)
        # Exponential decay: ±5s → ~0.85, ±15s → ~0.60, ±30s → ~0.37, ±60s → 0.13
        d_sim = max(0.0, 1.0 - (delta / 45.0) ** 0.8)

    # ── Source confidence (5% weight) ─────────────────────────────────────────
    s_conf = _SOURCE_CONFIDENCE.get(source, 0.60)

    # ── Weighted sum (weights sum to 1.0) ─────────────────────────────────────
    conf = (t_sim * 0.50) + (a_sim * 0.35) + (d_sim * 0.10) + (s_conf * 0.05)

    # ── Bonus: exact title — applied BEFORE artist reject so a perfect title
    #    match with weak/missing artist metadata is never silently killed ───────
    if qt and rt and qt == rt:
        conf = min(1.0, conf + 0.15)

    # ── Hard artist mismatch rejection ────────────────────────────────────────
    # AGGRESSIVE: lowered threshold 0.60→0.50 — stricter artist match required
    if qa_norm and ra_norm and a_sim < 0.50 and t_sim < 0.95:
        if qt != rt:
            return 0.0

    # ── Duration hard-reject — AGGRESSIVE: 35%→25% delta, 20%→12% penalty trigger ──
    if query_duration_s > 0 and result_duration_s > 0:
        dur_ratio = abs(query_duration_s - result_duration_s) / max(query_duration_s, 1)
        if dur_ratio > 0.25:
            return 0.0       # AGGRESSIVE: was 0.35 — anything >25% duration diff = wrong song
        elif dur_ratio > 0.12:
            conf -= 0.25     # AGGRESSIVE: was 0.20 penalty at 0.20 ratio

    # ── Remix / slowed / cover / live — ZERO TOLERANCE on all variants ────────
    user_wants_version = _query_requests_version(query_title)
    query_is_remix   = _is_remix_or_cover(query_title)
    result_is_remix  = _is_remix_or_cover(rt)
    query_is_live    = _is_live_version(query_title)
    result_is_live   = _is_live_version(rt)
    query_is_slowed  = _is_slowed_reverb(query_title)
    result_is_slowed = _is_slowed_reverb(rt)

    if not user_wants_version:
        _query_starts_with_dj = bool(re.match(r'^dj\b', qt, re.IGNORECASE))
        if result_is_remix and not query_is_remix and not _query_starts_with_dj:
            return 0.0   # DJ remix, cover, mashup → instant reject
        if result_is_slowed and not query_is_slowed:
            return 0.0   # slowed/reverb/lofi/nightcore → instant reject
        # AGGRESSIVE: live version also instant reject now (was -0.30 penalty)
        if result_is_live and not query_is_live:
            return 0.0   # live/acoustic/concert → instant reject
    elif not result_is_remix and query_is_remix:
        conf -= 0.10

    return max(0.0, min(1.0, conf))

def is_likely_duplicate(a: dict, b: dict, threshold: float = 0.92) -> bool:
    """Detect duplicates using title + artist similarity."""
    ta = _normalize_text(a.get('trackName') or a.get('title', ''))
    tb = _normalize_text(b.get('trackName') or b.get('title', ''))
    aa = _normalize_text(a.get('artistName') or a.get('artist', ''))
    ab = _normalize_text(b.get('artistName') or b.get('artist', ''))
    t_sim = _seq_ratio(ta, tb)
    a_sim = _seq_ratio(aa, ab) if aa and ab else 0.5
    return (t_sim * 0.7 + a_sim * 0.3) >= threshold
# AGGRESSIVE MASTER MATCH GATE
# Single function called before ANY result is accepted from ANY source.
# Returns (is_confirmed: bool, confidence: float, reason: str)
# ═══════════════════════════════════════════════════════════════════════════════
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
    """
    AGGRESSIVE gate: every candidate from every source must pass this.
    Returns (True, conf, 'ok') or (False, conf, reason_string).
    """
    if not res_title:
        return False, 0.0, 'empty_title'

    # 1. Raw version keyword check BEFORE confidence — fastest rejection
    _user_wants_ver = _query_requests_version(req_title) or _query_requests_version(req_artist)
    if not _user_wants_ver:
        if _is_remix_or_cover(res_title):
            return False, 0.0, 'remix_cover_rejected'
        if _is_slowed_reverb(res_title):
            return False, 0.0, 'slowed_reverb_rejected'
        if _is_live_version(res_title):
            return False, 0.0, 'live_version_rejected'

    # 2. Compute full confidence
    conf = compute_confidence(
        req_title, req_artist, res_title, res_artist,
        query_duration_s=duration_s,
        result_duration_s=res_dur_s,
        source=source,
    )

    # 3. Minimum confidence gate
    if conf < min_conf:
        return False, conf, f'low_conf_{conf:.3f}'

    # 4. Title must share at least one significant word (anti-completely-wrong-song)
    _req_words = set(w for w in normalize(req_title).split() if len(w) >= 3)
    _res_words = set(w for w in normalize(res_title).split() if len(w) >= 3)
    if _req_words and _res_words and not _req_words.intersection(_res_words):
        # Allow if seq_ratio is very high (handles transliteration differences)
        _seq = _seq_ratio(normalize(req_title), normalize(res_title))
        if _seq < 0.55:
            return False, conf, f'no_word_overlap_seq={_seq:.3f}'

    # 5. Artist must not be completely different (when both provided)
    if req_artist and res_artist:
        _ra = _normalize_artist(normalize(req_artist))
        _rb = _normalize_artist(normalize(res_artist))
        if _ra and _rb:
            _a_sim = _seq_ratio(_ra, _rb)
            if _a_sim < 0.30:
                return False, conf, f'artist_mismatch_{_a_sim:.3f}'

    return True, conf, 'ok'
    """Detect duplicates using title + artist similarity."""
    ta = _normalize_text(a.get('trackName') or a.get('title', ''))
    tb = _normalize_text(b.get('trackName') or b.get('title', ''))
    aa = _normalize_text(a.get('artistName') or a.get('artist', ''))
    ab = _normalize_text(b.get('artistName') or b.get('artist', ''))
    t_sim = _seq_ratio(ta, tb)
    a_sim = _seq_ratio(aa, ab) if aa and ab else 0.5
    return (t_sim * 0.7 + a_sim * 0.3) >= threshold


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT HELPERS  (all original + enhancements)
# ═══════════════════════════════════════════════════════════════════════════════
def clean_query(text):
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(\s*(OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?|Hindi|English|Version|Remix|Cover|HD|HQ|Original|Soundtrack|Remastered|Extended|Radio\s*Edit)\s*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*[-–]\s*(official|audio|video|lyrics|full\s*song|hd|hq|remastered).*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    # FIX 4: strip bare version keywords not inside brackets
    # e.g. "Tum Hi Ho Lofi Version" → "Tum Hi Ho"
    _BARE_VERSION_PATTERN = (
        r'\s+(?:lofi|lo[- ]fi|slowed|reverb|slowed\s*reverb|reverb\s*slowed|'
        r'nightcore|sped\s*up|speed\s*up|bass\s*boosted|8d\s*audio|'
        r'dj\s+remix|dj\s+mix|remix|mashup|cover|karaoke|instrumental|'
        r'acoustic|unplugged|live\s*version|live\s*at|live\s*from|'
        r'pitched|chopped|screwed|extended\s*mix|club\s*mix|radio\s*edit|'
        r'tribute|stripped|concert\s*version)\b.*$'
    )
    text = re.sub(_BARE_VERSION_PATTERN, '', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip()

def build_query_variants(title, artist='', fallback=''):
    title_c      = clean_query(title)
    artist_c     = clean_query(artist) if artist else ''
    fb_c         = clean_query(fallback) if fallback else ''
    artist_first = artist_c.split()[0] if artist_c else ''
    title_first  = title_c.split()[0] if title_c else ''
    seen, variants = set(), []

    def add(v):
        v = re.sub(r'\s+', ' ', v).strip()
        if v and v not in seen:
            seen.add(v); variants.append(v)

    if artist_c: add(f"{artist_c} {title_c}")
    add(title_c)
    if artist_first: add(f"{title_c} {artist_first}")
    if artist_c:     add(f"{title_c} {artist_c}")
    if fb_c and fb_c != title_c: add(fb_c)
    if artist_c and fb_c: add(f"{artist_c} {title_c}")

    bracket_free = re.sub(r'\s*[\(\[].*?[\)\]]\s*', ' ', title_c).strip()
    add(bracket_free)
    dash_free = re.sub(r'\s*[-–]\s*', ' ', title_c).strip()
    add(dash_free)
    words = title_c.split()
    if len(words) > 2: add(' '.join(words[:3]))
    if len(words) > 3: add(' '.join(words[:2]))
    if artist_first and title_first: add(f"{title_first} {artist_first}")
    if artist_c and title_first:     add(f"{artist_c} {title_first}")
    if artist_first and len(words) > 1: add(f"{words[0]} {words[1]} {artist_first}")

    try:
        t_translit = _hindi_translit_normalize(title_c)
        if t_translit and t_translit != title_c:
            add(t_translit)
            if artist_first: add(f"{t_translit} {artist_first}")
    except Exception:
        pass

    return variants

_HINDI_TRANSLIT = [
    ('aa', 'a'), ('ee', 'i'), ('oo', 'u'), ('ae', 'ai'),
    ('ph', 'f'), ('bh', 'b'), ('gh', 'g'), ('kh', 'k'),
    ('th', 't'), ('dh', 'd'), ('sh', 's'), ('ch', 'c'),
    ('ie', 'i'), ('ey', 'ai'), ('ay', 'ai'), ('oi', 'oy'),
    ('ou', 'u'), ('ue', 'u'),
    # Normalise common Hindi romanisation variants to a single canonical form.
    # One-directional only — never add A→B AND B→A (causes infinite loops).
    ('hain', 'he'), ('hai', 'he'),   # all forms → 'he'
    ('ho', 'hu'),                    # hu/ho → hu
    ('ki', 'ke'),                    # ke/ki → ke
    ('ko', 'ku'),                    # ku/ko → ku
    ('nah', 'na'),                   # nah → na
    ('pyaar', 'pyar'),               # double-a → single
    ('dill', 'dil'),                 # double-l → single
    ('ishk', 'ishq'),                # ishk → ishq
]
# FIX BUG 6: removed all reciprocal (A↔B) pairs like ('hai','he')+('he','hai')
# which caused outputs to flip back and forth unpredictably.

def _hindi_translit_normalize(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # FIX 18: use word-boundary aware replacement to prevent "the" → "thai" etc
    for src, dst in _HINDI_TRANSLIT:
        t = re.sub(r'\b' + re.escape(src) + r'\b', dst, t)
    return t

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
    elif length <= 5:  return 0.40
    elif length <= 10: return 0.55
    else:              return 0.65

def has_word_match(query, song_title):
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words: return True
    q_main  = [w for w in q_words if len(w) >= 3]
    t_main  = [w for w in t_words if len(w) >= 3]
    if not q_main: return True
    if t_main and q_main[0] == t_main[0]: return True
    for qw in q_main:
        for tw in t_main:
            if fuzzy_word_match(qw, tw) >= 0.55: return True
    return False

def pick_best_quality(urls):
    if not urls: return None, None
    QUALITY_RANK = {
        '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
        '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
    }
    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK: return QUALITY_RANK[q]
        m = re.search(r'(\d+)', q)
        # FIX BUG 7: unknown quality → rank -1, never beats any known quality
        return int(m.group(1)) if m else -1
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
                # FIX BUG T3: replace all small Saavn CDN sizes → 500x500
                url = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
                return url
    if isinstance(images, str) and images.startswith('http'):
        return re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', images)
    return ''

def _pick_low_quality(urls):
    if not urls: return None, None
    for preferred in ['96kbps', '96', '128kbps', '128', '48kbps', '48']:
        for item in urls:
            q = (item.get('quality') or '').lower().strip()
            if q == preferred or preferred in q:
                url = item.get('url') or item.get('link') or ''
                if url.startswith('http'): return url, item.get('quality', preferred)
    QUALITY_RANK = {
        '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
        '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
    }
    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK: return QUALITY_RANK[q]
        m = re.search(r'(\d+)', q)
        return int(m.group(1)) if m else 999
    for item in sorted(urls, key=rank):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'): return url, item.get('quality', 'low')
    return None, None

def _safe_year(date_str):
    try: return int((date_str or '')[:4])
    except (ValueError, TypeError): return 0

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

# ─── Language Detection ───────────────────────────────────────────────────────
# Maps query keywords → language tag for Saavn search.
# Bhojpuri treated as its own language so Saavn returns correct regional results.
_LANGUAGE_KEYWORD_MAP = {
    'bhojpuri': 'bhojpuri', 'bhojpuri song': 'bhojpuri',
    'bhojpuri gana': 'bhojpuri', 'bhojpuri gaana': 'bhojpuri',
    'pawan singh': 'bhojpuri', 'khesari lal': 'bhojpuri',
    'dinesh lal': 'bhojpuri', 'nirahua': 'bhojpuri',
    'ritesh pandey': 'bhojpuri', 'ankush raja': 'bhojpuri',
    'pramod premi': 'bhojpuri', 'kallu': 'bhojpuri',
    'shilpi raj': 'bhojpuri', 'gunjan singh': 'bhojpuri',
    'hindi': 'hindi', 'bollywood': 'hindi',
    'hindi song': 'hindi', 'hindi gana': 'hindi',
    'english': 'english', 'english song': 'english', 'pop': 'english',
    'punjabi': 'punjabi', 'punjabi song': 'punjabi',
    'haryanvi': 'haryanvi', 'rajasthani': 'rajasthani',
    'tamil': 'tamil', 'telugu': 'telugu', 'kannada': 'kannada',
    'malayalam': 'malayalam', 'bengali': 'bengali',
    'marathi': 'marathi', 'gujarati': 'gujarati', 'odia': 'odia',
}

# Bhojpuri romanization normalization — same word, many spellings
_BHOJPURI_TRANSLIT = [
    ('tohaar', 'tohar'), ('hamaar', 'hamar'), ('kahe', 'kaahe'),
    ('bhaiya', 'bhaiyya'), ('saiya', 'saiyya'), ('piya', 'piyaa'),
    ('bhauji', 'bhouji'), ('lahariya', 'laharia'),
    ('goriya', 'goria'), ('balmuaa', 'balmua'),
    ('ae', 'aye'), ('ogo', 'ago'), ('hau', 'hu'),
    ('bade', 'bado'), ('kaisan', 'kaisa'),
]

def _detect_language(query: str) -> str:
    """Detect language from query. Returns tag like 'bhojpuri', 'hindi', 'english' or ''."""
    q = query.lower().strip()
    for kw in sorted(_LANGUAGE_KEYWORD_MAP, key=len, reverse=True):
        if kw in q:
            return _LANGUAGE_KEYWORD_MAP[kw]
    return ''

def _bhojpuri_normalize(text: str) -> str:
    """Normalize Bhojpuri romanization variants for matching engine."""
    t = text.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    for src, dst in _BHOJPURI_TRANSLIT:
        t = re.sub(r'\b' + re.escape(src) + r'\b', dst, t)
    return t

ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'cf.saavncdn.com', 'aac.saavncdn.com', 'static.saavncdn.com',
    'c.saavncdn.com', 'h.saavncdn.com',
    'googlevideo.com', 'youtube.com', 'ytimg.com',
    'manifest.googlevideo.com', 'sndcdn.com', 'soundcloud.com',
    'cf-media.sndcdn.com', 'a-v2.sndcdn.com',
    'rr1.sn-', 'rr2.sn-', 'rr3.sn-', 'rr4.sn-',
    'r1.sn-', 'r2.sn-', 'r3.sn-', 'r4.sn-',
    'r5.sn-', 'r6.sn-', 'r7.sn-',
]


# ═══════════════════════════════════════════════════════════════════════════════
# ████████████████████  ADAPTIVE SOURCE HEALTH SYSTEM  ████████████████████████
# ═══════════════════════════════════════════════════════════════════════════════
# Tracks each source URL with:
#   - fails / successes
#   - EMA response time
#   - quarantine flag + recovery cooldown
#   - reputation score ∈ [0, 100]
#
# Reputation scoring formula:
#   rep = 100
#        - fails * 8           (each fail costs 8 points)
#        - age_since_ok / 60   (penalty for staleness, max 40)
#        - avg_ms / 100        (latency penalty, max 30)
#        + success_rate * 10   (bonus for reliability)
#
# Sources below rep 20 are quarantined for QUARANTINE_SECS.
# Sources with 3+ consecutive successes are restored from quarantine.
# ═══════════════════════════════════════════════════════════════════════════════

_QUARANTINE_SECS       = 60    # STRONG HEAL: 1 min quarantine (was 3 min)
_REP_RECOVERY_SECS     = 30    # STRONG HEAL: probe after 30s (was 90s)
_REP_FAIL_COST         = 6     # STRONG HEAL: slightly lower cost so recovery is faster
_REP_MIN_FOR_TRAFFIC   = 15    # STRONG HEAL: lower bar — more sources stay alive
_ADAPTIVE_TIMEOUT_BASE = 4.0   # STRONG HEAL: tighter base timeout (was 5s)
_ADAPTIVE_TIMEOUT_MAX  = 8.0   # STRONG HEAL: max 8s (was 10s)

class _SourceHealth:
    """Per-source health tracker with reputation, adaptive timeout, quarantine."""

    def __init__(self):
        self._data: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def _get(self, url: str) -> dict:
        if url not in self._data:
            self._data[url] = {
                'fails':       0,
                'successes':   0,
                'consecutive_ok': 0,
                'last_fail':   0.0,
                'last_ok':     0.0,
                'avg_ms':      500.0,
                'quarantined': False,
                'quarantine_ts': 0.0,
                'total_hits':  0,
            }
        return self._data[url]

    def record_ok(self, url: str, elapsed_ms: float = 0.0):
        with self._lock:
            h = self._get(url)
            h['successes']      += 1
            h['consecutive_ok'] += 1
            h['last_ok']         = time.time()
            h['total_hits']      += 1
            h['fails']           = max(0, h['fails'] - 1)
            if elapsed_ms > 0:
                h['avg_ms'] = h['avg_ms'] * 0.75 + elapsed_ms * 0.25
            # Auto-recover from quarantine after 3 consecutive successes
            if h['quarantined'] and h['consecutive_ok'] >= 3:
                h['quarantined'] = False
                log.info(f'[Health] ✓ Recovered from quarantine: {url[:50]}')

    def record_fail(self, url: str):
        with self._lock:
            h = self._get(url)
            h['fails']          += 1
            h['consecutive_ok']  = 0
            h['last_fail']       = time.time()
            h['total_hits']      += 1
            # Quarantine if reputation drops below threshold
            if self.reputation(url, locked=True) < _REP_MIN_FOR_TRAFFIC:
                h['quarantined']    = True
                h['quarantine_ts']  = time.time()
                log.warning(f'[Health] ⚠ Quarantined: {url[:50]}')

    def reputation(self, url: str, locked: bool = False) -> float:
        """Compute reputation score ∈ [0, 100]. Higher = better."""
        h = self._data.get(url, {})
        if not h: return 50.0
        fails    = h.get('fails', 0)
        last_ok  = h.get('last_ok', 0.0)
        avg_ms   = h.get('avg_ms', 500.0)
        hits     = h.get('total_hits', 0)
        succ     = h.get('successes', 0)
        age_ok   = time.time() - last_ok if last_ok else 9999.0
        sr       = (succ / max(hits, 1)) if hits else 0.5
        rep      = 100.0
        rep     -= min(fails * _REP_FAIL_COST, 60)
        rep     -= min(age_ok / 60.0, 40.0)
        rep     -= min(avg_ms / 100.0, 30.0)
        rep     += sr * 10.0
        return max(0.0, rep)

    def is_alive(self, url: str) -> bool:
        with self._lock:
            h = self._get(url)
            if h.get('quarantined', False):
                # Allow probe after recovery delay
                if time.time() - h.get('quarantine_ts', 0) > _REP_RECOVERY_SECS:
                    return True   # let one request through as probe
                return False
            return True

    def adaptive_timeout(self, url: str) -> float:
        """Return recommended timeout in seconds based on source latency."""
        with self._lock:
            avg_ms = self._data.get(url, {}).get('avg_ms', 500.0)
        t = max(_ADAPTIVE_TIMEOUT_BASE, avg_ms / 1000.0 * 2.5)
        return min(t, _ADAPTIVE_TIMEOUT_MAX)

    def sort_by_reputation(self, urls: list) -> list:
        """Return urls sorted best-first by reputation (no lock needed for sort)."""
        return sorted(urls, key=lambda u: self.reputation(u), reverse=True)

    def summary(self, url: str) -> dict:
        with self._lock:
            h = self._get(url)
        return {
            'url':        url,
            'reputation': round(self.reputation(url), 1),
            'avg_ms':     round(h.get('avg_ms', 0)),
            'fails':      h.get('fails', 0),
            'quarantined': h.get('quarantined', False),
            'status':     ('quarantined' if h.get('quarantined') else
                           'ok' if self.reputation(url) >= 60 else
                           'degraded'),
        }


_health = _SourceHealth()

# backward-compat shims used by existing code
def _health_record_ok(url: str, elapsed_ms: float = 0):
    _health.record_ok(url, elapsed_ms)

def _health_record_fail(url: str):
    _health.record_fail(url)

def _health_score(url: str) -> float:
    return _health.reputation(url)

def _is_source_alive(url: str) -> bool:
    return _health.is_alive(url)


# ═══════════════════════════════════════════════════════════════════════════════
# SAAVN MIRRORS  (all original mirrors preserved)
# ═══════════════════════════════════════════════════════════════════════════════
_BASE_MIRRORS = [
    'https://jio-saavn-api.onrender.com',
    'https://my-jiosaavn-api.onrender.com',
    'https://saavn-backend.onrender.com',
    'https://jiosaavn-api-node.onrender.com',
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
    'https://jiosaavn-api.vercel.app',
    'https://saavn-api-three.vercel.app',
    'https://jiosaavn-api-production.up.railway.app',
    'https://saavn-api-ruby.vercel.app',
    'https://jiosaavn.vercel.app',
    'https://saavn-api-blond.vercel.app',
    'https://jiosaavn-api-five.vercel.app',
    'https://saavn-api-nu.vercel.app',
    'https://jiosaavn-api-six.vercel.app',
    'https://jiosaavn-api-nine.vercel.app',
    'https://jiosaavn-api-smoky.vercel.app',
    'https://saavn-api-seven.vercel.app',
    'https://jiosaavn-api-seven.vercel.app',
    'https://saavn-api-two.vercel.app',
]

SAAVN_MIRRORS   = list(_BASE_MIRRORS)
_mirror_lock    = threading.Lock()
_discovered_set = set(_BASE_MIRRORS)

_mirror_fail_count   = {}
_mirror_fail_time    = {}
MIRROR_FAIL_COOLDOWN = 30

def _mirror_ok(mirror):
    if not _health.is_alive(mirror): return False
    fails     = _mirror_fail_count.get(mirror, 0)
    if fails < 3: return True
    last_fail = _mirror_fail_time.get(mirror, 0)
    if time.time() - last_fail > MIRROR_FAIL_COOLDOWN:
        _mirror_fail_count[mirror] = 0
        return True
    return False

def _mirror_failed(mirror):
    _mirror_fail_count[mirror] = _mirror_fail_count.get(mirror, 0) + 1
    _mirror_fail_time[mirror]  = time.time()
    _health.record_fail(mirror)
    dead_count = sum(1 for m in SAAVN_MIRRORS if _mirror_fail_count.get(m, 0) >= 5)
    if dead_count >= max(1, len(SAAVN_MIRRORS) // 2):
        _maybe_reactive_heal('saavn')

def _best_mirrors(n: int = 8) -> list:
    """Return top-n mirrors sorted by reputation."""
    with _mirror_lock:
        alive = [m for m in SAAVN_MIRRORS if _mirror_ok(m)]
    if not alive:
        with _mirror_lock:
            alive = list(SAAVN_MIRRORS)
    return _health.sort_by_reputation(alive)[:n]


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO MIRROR DISCOVERY  (unchanged logic, improved concurrency)
# ═══════════════════════════════════════════════════════════════════════════════
_DISCOVERY_PATTERNS = ['jiosaavn-api', 'saavn-api', 'jiosaavn', 'saavn', 'jio-saavn', 'saavnapi', 'jiosaavnapi']
_DISCOVERY_SUFFIXES = ['', '-v2', '-v3', '-v4', '-new', '-prod', '-main', '-app', '-api', '-server',
                       '-backend', '-public', '-open', '-free', '-node', '-express', '-privatecvc',
                       '-privatecvc2', '-privatecvc3', '-one', '-two', '-three', '-four', '-five',
                       '-six', '-seven', '-eight', '-nine', '-ten']
_DISCOVERY_PREFIXES = ['', 'the-', 'my-', 'open-', 'free-', 'public-']
_DISCOVERY_HOSTS    = ['.vercel.app', '.up.railway.app', '.onrender.com']

def _test_mirror_working(url: str) -> bool:
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0 = time.time()
            r  = requests.get(f'{url}{endpoint}',
                              params={'query': 'arijit singh', 'q': 'arijit singh', 'limit': 2},
                              timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data    = r.json()
            results = (data.get('data', {}).get('results') or data.get('results') or
                       data.get('songs', {}).get('results') or [])
            if results and len(results) > 0:
                _health.record_ok(url, elapsed)
                return True
        except Exception:
            continue
    _health.record_fail(url)
    return False

def _discover_mirrors():
    global SAAVN_MIRRORS
    log.info('[Discovery] Starting Saavn mirror scan...')
    candidates = []
    for pattern in _DISCOVERY_PATTERNS:
        for prefix in _DISCOVERY_PREFIXES:
            for suffix in _DISCOVERY_SUFFIXES:
                for host in _DISCOVERY_HOSTS:
                    url = f'https://{prefix}{pattern}{suffix}{host}'
                    with _mirror_lock:
                        if url not in _discovered_set:
                            candidates.append(url)
    log.info(f'[Discovery] Testing {len(candidates)} candidates...')
    new_found = []
    futures   = {_executor.submit(_test_mirror_working, url): url for url in candidates}
    try:
        for future in as_completed(futures, timeout=30):
            url = futures[future]
            try:
                if future.result():
                    with _mirror_lock:
                        if url not in _discovered_set:
                            _discovered_set.add(url)
                            new_found.append(url)
                            log.info(f'[Discovery] ✓ New mirror: {url}')
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[Discovery] Timeout: {e}')
    if new_found:
        with _mirror_lock:
            SAAVN_MIRRORS = list(_discovered_set)
        log.info(f'[Discovery] Added {len(new_found)} mirrors. Total: {len(SAAVN_MIRRORS)}')

def _verify_existing_mirrors():
    global SAAVN_MIRRORS
    to_remove = []
    with _mirror_lock:
        current = list(SAAVN_MIRRORS)
    for url in current:
        if _mirror_fail_count.get(url, 0) >= 15:
            if not _test_mirror_working(url):
                to_remove.append(url)
            else:
                _mirror_fail_count[url] = 0
    if to_remove:
        with _mirror_lock:
            for url in to_remove:
                if url in SAAVN_MIRRORS:
                    SAAVN_MIRRORS.remove(url)
                _discovered_set.discard(url)
        _executor.submit(_discover_mirrors)


# ═══════════════════════════════════════════════════════════════════════════════
# PIPED INSTANCES
# ═══════════════════════════════════════════════════════════════════════════════
_BASE_PIPED = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.tokhmi.xyz',
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
    'https://pipedapi.reallyaweso.me',
    'https://pipedapi.in.projectsegfau.lt',
]
PIPED_INSTANCES = list(_BASE_PIPED)
_piped_lock     = threading.Lock()
_piped_known    = set(_BASE_PIPED)

def _test_piped_instance(url: str) -> bool:
    try:
        t0 = time.time()
        r  = requests.get(f'{url}/search',
                          params={'q': 'arijit singh', 'filter': 'music_songs'},
                          timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200 and r.json().get('items'):
            _health.record_ok(url, elapsed)
            return True
    except Exception:
        pass
    _health.record_fail(url)
    return False

def _heal_piped():
    global PIPED_INSTANCES
    try:
        r = requests.get('https://piped-instances.kavin.rocks/', timeout=10,
                         headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            instances      = r.json()
            new_candidates = [inst.get('api_url', '').rstrip('/') for inst in instances
                              if inst.get('api_url', '').rstrip('/') not in _piped_known]
            futures = {_executor.submit(_test_piped_instance, u): u for u in new_candidates if u}
            try:
                for future in as_completed(futures, timeout=30):
                    url = futures[future]
                    try:
                        if future.result():
                            with _piped_lock:
                                if url not in _piped_known:
                                    _piped_known.add(url)
                                    PIPED_INSTANCES.append(url)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[SelfHeal:Piped] {e}')
    dead = [url for url in PIPED_INSTANCES
            if _health.reputation(url) < _REP_MIN_FOR_TRAFFIC
            and not _test_piped_instance(url)]
    if dead:
        with _piped_lock:
            for url in dead:
                if url in PIPED_INSTANCES:
                    PIPED_INSTANCES.remove(url)


# ═══════════════════════════════════════════════════════════════════════════════
# INVIDIOUS INSTANCES
# ═══════════════════════════════════════════════════════════════════════════════
_BASE_INVIDIOUS = [
    'https://invidious.snopyta.org',
    'https://vid.puffyan.us',
    'https://invidious.kavin.rocks',
    'https://y.com.sb',
    'https://invidious.nerdvpn.de',
]
INVIDIOUS_INSTANCES = list(_BASE_INVIDIOUS)
_invidious_lock     = threading.Lock()
_invidious_known    = set(_BASE_INVIDIOUS)

def _test_invidious_instance(url: str) -> bool:
    try:
        t0 = time.time()
        r  = requests.get(f'{url}/api/v1/search',
                          params={'q': 'arijit singh', 'type': 'video', 'page': 1},
                          timeout=7, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) > 0:
            _health.record_ok(url, elapsed)
            return True
    except Exception:
        pass
    _health.record_fail(url)
    return False

def _heal_invidious():
    global INVIDIOUS_INSTANCES
    try:
        r = requests.get('https://api.invidious.io/instances.json',
                         params={'sort_by': 'health'}, timeout=10,
                         headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            new_candidates = [inst[1].get('uri', '').rstrip('/') for inst in r.json()
                              if isinstance(inst, list) and len(inst) >= 2
                              and inst[1].get('uri', '').startswith('https')
                              and inst[1].get('api', False)
                              and inst[1].get('uri', '').rstrip('/') not in _invidious_known]
            futures = {_executor.submit(_test_invidious_instance, u): u for u in new_candidates[:20] if u}
            try:
                for future in as_completed(futures, timeout=40):
                    url = futures[future]
                    try:
                        if future.result():
                            with _invidious_lock:
                                if url not in _invidious_known:
                                    _invidious_known.add(url)
                                    INVIDIOUS_INSTANCES.append(url)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[SelfHeal:Invidious] {e}')


# ═══════════════════════════════════════════════════════════════════════════════
# SOUNDCLOUD CLIENT ID AUTO-REFRESH
# ═══════════════════════════════════════════════════════════════════════════════
SOUNDCLOUD_CLIENT_ID     = os.environ.get('SOUNDCLOUD_CLIENT_ID', 'a3e059563d7fd3372b49b37f00a00bcf')
_sc_client_id_lock       = threading.Lock()
_sc_client_id_last_check = 0
_SC_ID_REFRESH_INTERVAL  = 3600

def _refresh_soundcloud_client_id():
    global SOUNDCLOUD_CLIENT_ID
    try:
        r = requests.get('https://soundcloud.com', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200: return
        script_urls = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', r.text)
        for script_url in script_urls[-5:]:
            try:
                sr = requests.get(script_url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
                if sr.status_code != 200: continue
                match = re.search(r'client_id\s*[:=]\s*["\']([a-zA-Z0-9]{32})["\']', sr.text)
                if match:
                    new_id = match.group(1)
                    with _sc_client_id_lock:
                        if new_id != SOUNDCLOUD_CLIENT_ID:
                            log.info(f'[SelfHeal:SC] Client ID refreshed: {new_id[:8]}...')
                            SOUNDCLOUD_CLIENT_ID = new_id
                    return
            except Exception:
                continue
    except Exception as e:
        log.warning(f'[SelfHeal:SC] {e}')

def _maybe_refresh_sc_id():
    global _sc_client_id_last_check
    now = time.time()
    if now - _sc_client_id_last_check > _SC_ID_REFRESH_INTERVAL:
        _sc_client_id_last_check = now
        _executor.submit(_refresh_soundcloud_client_id)


# ═══════════════════════════════════════════════════════════════════════════════
# MASTER HEAL LOOP  (improved: graduated heal intervals, load-aware)
# ═══════════════════════════════════════════════════════════════════════════════
_reactive_heal_cooldown      = {}
_reactive_heal_cooldown_lock = threading.Lock()   # FIX 2: was unprotected dict
_REACTIVE_COOLDOWN_S         = 120

def _maybe_reactive_heal(source_type: str):
    now = time.time()
    with _reactive_heal_cooldown_lock:
        last = _reactive_heal_cooldown.get(source_type, 0)
        if now - last < _REACTIVE_COOLDOWN_S:
            return
        _reactive_heal_cooldown[source_type] = now
    log.info(f'[SelfHeal:Reactive] Triggering heal for {source_type}')
    if source_type == 'saavn':
        _executor.submit(_discover_mirrors)
        _executor.submit(_verify_existing_mirrors)
    elif source_type == 'piped':
        _executor.submit(_heal_piped)
    elif source_type == 'invidious':
        _executor.submit(_heal_invidious)
    elif source_type == 'soundcloud':
        _executor.submit(_refresh_soundcloud_client_id)

# ─── STRONG SELF-HEAL THRESHOLDS ────────────────────────────────────────────
# If alive mirrors drop below these, emergency heal triggers immediately.
_HEAL_MIN_SAAVN_MIRRORS  = 5   # need at least 5 working Saavn mirrors
_HEAL_MIN_PIPED          = 2   # need at least 2 Piped instances
_HEAL_MIN_INVIDIOUS      = 2   # need at least 2 Invidious instances
_HEAL_EMERGENCY_INTERVAL = 120 # emergency heal: every 2 min if degraded
_HEAL_FULL_INTERVAL      = 3600 # full heal: every 1 hour (was 2 hours)

# Track last time emergency heal ran per source type
_last_emergency_heal: Dict[str, float] = {}
_emergency_heal_lock = threading.Lock()

def _should_emergency_heal(source: str, interval: float = _HEAL_EMERGENCY_INTERVAL) -> bool:
    now = time.time()
    with _emergency_heal_lock:
        if now - _last_emergency_heal.get(source, 0) > interval:
            _last_emergency_heal[source] = now
            return True
    return False

def _count_alive_mirrors() -> int:
    with _mirror_lock:
        return sum(1 for m in SAAVN_MIRRORS if _mirror_ok(m))

def _count_alive_piped() -> int:
    with _piped_lock:
        return sum(1 for p in PIPED_INSTANCES if _health.is_alive(p))

def _count_alive_invidious() -> int:
    with _invidious_lock:
        return sum(1 for i in INVIDIOUS_INSTANCES if _health.is_alive(i))

def _strong_heal_saavn():
    """
    STRONG HEAL for Saavn:
    1. Verify all existing mirrors — remove permanently dead ones.
    2. Restore any that were wrongly quarantined (probe them).
    3. Discover new mirrors if count is below threshold.
    """
    log.info('[StrongHeal:Saavn] Starting...')
    # Step 1: parallel verify all existing mirrors
    with _mirror_lock:
        current = list(SAAVN_MIRRORS)
    probe_futures = {_executor.submit(_test_mirror_working, m): m for m in current}
    dead, recovered = [], []
    try:
        for future in as_completed(probe_futures, timeout=20):
            m = probe_futures[future]
            try:
                alive = future.result()
                if alive:
                    recovered.append(m)
                    _mirror_fail_count[m] = 0   # reset fail count on success
                elif _mirror_fail_count.get(m, 0) >= 20:
                    dead.append(m)              # only remove if repeatedly failing
            except Exception:
                pass
    except Exception:
        pass

    if dead:
        with _mirror_lock:
            for m in dead:
                if m not in _BASE_MIRRORS and m in SAAVN_MIRRORS:
                    SAAVN_MIRRORS.remove(m)
                    _discovered_set.discard(m)
        log.info(f'[StrongHeal:Saavn] Removed {len(dead)} dead mirrors')

    # Step 2: reset quarantine on recovered mirrors
    for m in recovered:
        h = _health._data.get(m, {})
        if h.get('quarantined'):
            h['quarantined'] = False
            h['consecutive_ok'] = 3
            log.info(f'[StrongHeal:Saavn] ✓ Un-quarantined: {m[:50]}')

    # Step 3: discover new mirrors only if below threshold
    alive_count = _count_alive_mirrors()
    if alive_count < _HEAL_MIN_SAAVN_MIRRORS:
        log.info(f'[StrongHeal:Saavn] Only {alive_count} alive, discovering new...')
        _discover_mirrors()
    else:
        log.info(f'[StrongHeal:Saavn] ✓ {alive_count} mirrors alive — OK')

def _strong_heal_piped():
    """Verify + restore Piped instances, always keep ≥ _HEAL_MIN_PIPED alive."""
    log.info('[StrongHeal:Piped] Starting...')
    with _piped_lock:
        current = list(PIPED_INSTANCES)
    for inst in current:
        if not _health.is_alive(inst):
            # Try to probe and restore
            if _test_piped_instance(inst):
                h = _health._data.get(inst, {})
                h['quarantined'] = False
                h['consecutive_ok'] = 3
                log.info(f'[StrongHeal:Piped] ✓ Restored: {inst[:50]}')
    if _count_alive_piped() < _HEAL_MIN_PIPED:
        log.info('[StrongHeal:Piped] Below threshold — fetching new instances')
        _heal_piped()

def _strong_heal_invidious():
    """Verify + restore Invidious instances, always keep ≥ _HEAL_MIN_INVIDIOUS alive."""
    log.info('[StrongHeal:Invidious] Starting...')
    with _invidious_lock:
        current = list(INVIDIOUS_INSTANCES)
    for inst in current:
        if not _health.is_alive(inst):
            if _test_invidious_instance(inst):
                h = _health._data.get(inst, {})
                h['quarantined'] = False
                h['consecutive_ok'] = 3
                log.info(f'[StrongHeal:Invidious] ✓ Restored: {inst[:50]}')
    if _count_alive_invidious() < _HEAL_MIN_INVIDIOUS:
        log.info('[StrongHeal:Invidious] Below threshold — fetching new instances')
        _heal_invidious()

def _master_heal_loop():
    """
    STRONG MASTER HEAL LOOP
    - Runs every 60s
    - Emergency heals if sources drop below thresholds
    - Full heal cycle every 1 hour
    """
    time.sleep(20)   # shorter initial delay (was 30s)
    last_full_heal = 0.0

    while True:
        try:
            now = time.time()

            # ── Emergency heal: triggered when alive counts drop below threshold ──
            saavn_alive = _count_alive_mirrors()
            piped_alive = _count_alive_piped()
            inv_alive   = _count_alive_invidious()

            if saavn_alive < _HEAL_MIN_SAAVN_MIRRORS:
                if _should_emergency_heal('saavn'):
                    log.warning(f'[StrongHeal] ⚠ Emergency: only {saavn_alive} Saavn mirrors alive!')
                    _executor.submit(_strong_heal_saavn)

            if piped_alive < _HEAL_MIN_PIPED:
                if _should_emergency_heal('piped'):
                    log.warning(f'[StrongHeal] ⚠ Emergency: only {piped_alive} Piped instances alive!')
                    _executor.submit(_strong_heal_piped)

            if inv_alive < _HEAL_MIN_INVIDIOUS:
                if _should_emergency_heal('invidious'):
                    log.warning(f'[StrongHeal] ⚠ Emergency: only {inv_alive} Invidious instances alive!')
                    _executor.submit(_strong_heal_invidious)

            # ── Full heal cycle: every _HEAL_FULL_INTERVAL seconds ──────────────
            if now - last_full_heal > _HEAL_FULL_INTERVAL:
                last_full_heal = now
                log.info('[StrongHeal] Starting scheduled full heal cycle...')
                futures = [
                    _executor.submit(_strong_heal_saavn),
                    _executor.submit(_strong_heal_piped),
                    _executor.submit(_strong_heal_invidious),
                    _executor.submit(_refresh_soundcloud_client_id),
                ]
                for f in as_completed(futures, timeout=120):
                    try: f.result()
                    except Exception as e: log.warning(f'[StrongHeal] Error: {e}')
                log.info(
                    f'[StrongHeal] ✓ Full cycle done — '
                    f'Saavn:{_count_alive_mirrors()} '
                    f'Piped:{_count_alive_piped()} '
                    f'Invidious:{_count_alive_invidious()}'
                )

        except Exception as e:
            log.error(f'[StrongHeal] Master loop error: {e}')

        time.sleep(60)   # check every 60s (was every 2 hours!)

threading.Thread(target=_master_heal_loop, daemon=True).start()
log.info('[StrongHeal] Strong master heal loop started (60s interval)')


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND CACHE CLEANUP  (L1 expired entry eviction every 10 min)
# ═══════════════════════════════════════════════════════════════════════════════
def _cache_cleanup_loop():
    while True:
        time.sleep(600)
        try:
            for cache in [_l1_meta, _l1_audio, _l1_popular, _l1_saavn, _l1_artwork, _l1_verified]:
                evicted = cache.evict_expired()
                if evicted:
                    log.debug(f'[Cache:Cleanup] Evicted {evicted} expired entries')
        except Exception as e:
            log.warning(f'[Cache:Cleanup] Error: {e}')

threading.Thread(target=_cache_cleanup_loop, daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
# KEEPALIVE PING
# ═══════════════════════════════════════════════════════════════════════════════
_KEEPALIVE_URLS = [
    'https://jiosavan.onrender.com/song/?query=test',
    'https://jio-saavn-api.onrender.com/api/search/songs?query=test',
    'https://my-jiosaavn-api.onrender.com/api/search/songs?query=test',
    'https://saavn-backend.onrender.com/api/search/songs?query=test',
]

def _keepalive_ping():
    while True:
        try:
            time.sleep(270)   # FIX 3: was 600s — Render sleeps at 15min, ping every 4.5min
            for url in _KEEPALIVE_URLS:
                try:
                    requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
                    log.info(f'[Keepalive] Pinged: {url[:40]}')
                except Exception:
                    pass
        except Exception as e:
            log.warning(f'[Keepalive] Error: {e}')

threading.Thread(target=_keepalive_ping, daemon=True).start()
log.info('[Keepalive] Ping loop started')


# ═══════════════════════════════════════════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND ROUTES  (PWA routes — NEVER modified)
# ═══════════════════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════════════════
# SAAVN SEARCH HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _fetch_saavn_search_mirror(mirror, search_term, language: str = ''):
    if not _mirror_ok(mirror): return []
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0     = time.time()
            params = {'query': search_term, 'q': search_term, 'limit': 20}
            if language:
                params['language'] = language   # Saavn supports ?language= filter
            r  = requests.get(f'{mirror}{endpoint}',
                              params=params,
                              timeout=_health.adaptive_timeout(mirror),
                              headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data = r.json()
            raw  = (data.get('data', {}).get('results') or data.get('results') or
                    data.get('songs', {}).get('results') or [])
            if raw:
                _health.record_ok(mirror, elapsed)
                return raw
        except Exception:
            _mirror_failed(mirror)
    return []

def _fetch_saavn_search_parallel(search_term, language: str = ''):
    """
    Race top-N mirrors. Return as soon as the first winner responds.
    Cancel remaining futures immediately (bandwidth + CPU saving).
    """
    if not language:
        language = _detect_language(search_term)
    mirrors = _best_mirrors(n=6)
    futures = {_executor.submit(_fetch_saavn_search_mirror, m, search_term, language): m for m in mirrors}
    try:
        for future in as_completed(futures, timeout=8):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    return result
            except Exception:
                pass
    except Exception:
        pass
    return []

def _normalize_saavn_songs(raw_songs):
    normalized = []
    for song in raw_songs:
        song_id = song.get('id', '').strip()
        if not song_id: continue
        title  = song.get('name') or song.get('title', '')
        artist = song.get('primaryArtists') or song.get('primary_artists') or ''
        image  = pick_image(song)
        year   = str(song.get('year') or '0')[:4]
        dur_s  = int(song.get('duration', 0) or 0)
        dur_ms = dur_s * 1000
        if dur_s == 0 or dur_s > 1080: continue   # skip missing or too-long durations
        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        _, quality = pick_best_quality(raw_urls)
        if not quality: continue
        normalized.append({
            'trackId':         song_id,
            'trackName':       title,
            'artistName':      artist,
            'artworkUrl100':   image if image else '',
            'previewUrl':      f"/api/play?id={quote(song_id, safe='')}",
            'trackTimeMillis': dur_ms,
            'releaseDate':     f"{year}-01-01T00:00:00Z",
            '_saavnId':        song_id,
            '_quality':        quality,
            '_source':         'saavn',
        })
        # Store Saavn artwork independently (highest priority = 1)
        if image and title:
            _store_artwork(title, artist, image, 1)
    return normalized


# ═══════════════════════════════════════════════════════════════════════════════
# RESOLVE ITUNES → SAAVN ID  (enhanced with confidence engine)
# ═══════════════════════════════════════════════════════════════════════════════
def _resolve_itunes_to_saavn(itunes_song: dict) -> Optional[dict]:
    title  = itunes_song.get('trackName', '').strip()
    artist = itunes_song.get('artistName', '').strip()
    if not title: return None

    # upgrade artwork — replace all common small sizes with 600x600
    if itunes_song.get('artworkUrl100'):
        itunes_song['artworkUrl100'] = re.sub(
            r'\b\d+x\d+\b', '600x600', itunes_song['artworkUrl100']
        )
        # Store iTunes artwork early (priority 2) — persists even if resolution fails
        _store_artwork(title, artist, itunes_song['artworkUrl100'], 2)

    mirrors = _best_mirrors(n=6)

    for query in build_query_variants(title, artist, ''):
        for mirror in mirrors[:6]:
            for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
                try:
                    resp = requests.get(
                        f'{mirror}{endpoint}',
                        params={'query': query, 'q': query, 'limit': 5},
                        timeout=_health.adaptive_timeout(mirror),
                        headers={'User-Agent': 'Mozilla/5.0'},
                    )
                    if resp.status_code != 200: continue
                    data = resp.json()
                    raw  = (data.get('data', {}).get('results') or
                            data.get('results') or
                            data.get('songs', {}).get('results') or [])
                    if not raw: continue

                    best = None; best_conf = -1.0
                    itunes_dur = int((itunes_song.get('trackTimeMillis') or 0) // 1000)

                    for song in raw:
                        song_title  = song.get('name') or song.get('title', '')
                        song_artist = (song.get('primaryArtists') or
                                       song.get('primary_artists') or '')
                        song_dur = int(song.get('duration', 0) or 0)
                        # AGGRESSIVE: master gate
                        _ok, _conf, _reason = _is_confirmed_match(
                            title, artist, song_title, song_artist,
                            source='saavn', duration_s=itunes_dur, res_dur_s=song_dur,
                            min_conf=0.70,
                        )
                        if not _ok:
                            log.debug(f"[iTunesResolve] Rejected '{song_title}': {_reason}")
                            continue
                        if _conf > best_conf:
                            best_conf = _conf; best = song

                    if not best or best_conf < 0.70: continue  # GODMODE FIX 10: was 0.65

                    saavn_id = (best.get('id') or '').strip()
                    raw_urls = best.get('downloadUrl') or best.get('download_url') or []
                    if isinstance(raw_urls, str):
                        raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                    _, quality = pick_best_quality(raw_urls)
                    if not saavn_id or not quality: continue

                    itunes_song['previewUrl']      = f"/api/play?id={quote(saavn_id, safe='')}"
                    itunes_song['_saavnId']        = saavn_id
                    itunes_song['_resolvedTitle']  = best.get('name') or best.get('title', title)
                    itunes_song['_resolvedArtist'] = best.get('primaryArtists') or best.get('primary_artists') or artist
                    itunes_song['_confidence']     = round(best_conf, 3)
                    # Store Saavn artwork for this song (overrides iTunes priority)
                    saavn_img = pick_image(best)
                    if saavn_img:
                        itunes_song['artworkUrl100'] = saavn_img
                        _store_artwork(title, artist, saavn_img, 1)
                    log.info(f"[Resolve] ✓ '{title}' → {saavn_id} conf={best_conf:.2f}")
                    return itunes_song

                except Exception:
                    continue

    # Fallback: title-based play (low confidence, won't cache in L2)
    itunes_song['previewUrl'] = (
        f"/api/play?title={quote(title, safe='')}"
        f"&artist={quote(artist, safe='')}"
    )
    itunes_song['_confidence'] = 0.30
    log.info(f"[Resolve] ✗ No strong Saavn match for '{title}' — title fallback")
    return itunes_song


# ═══════════════════════════════════════════════════════════════════════════════
# YOUTUBE MUSIC (InnerTube API)
# ═══════════════════════════════════════════════════════════════════════════════
_YTM_SEARCH_URL  = 'https://music.youtube.com/youtubei/v1/search'
_YTM_API_KEY     = 'AIzaSyC9XL3ZjWddXya6X74dJoCTL-NKNELL6imp'
_YTM_CONTEXT     = {
    'client': {
        'clientName':    'WEB_REMIX',
        'clientVersion': '1.20250101.01.00',
        'hl':            'en',
        'gl':            'IN',
        'userAgent':     'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
}

def _ytm_search(query: str, limit: int = 8) -> list:
    try:
        body = {
            'context': _YTM_CONTEXT,
            'query':   query,
            'params':  'EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D',
        }
        r = requests.post(
            _YTM_SEARCH_URL,
            params={'key': _YTM_API_KEY, 'prettyPrint': 'false'},
            json=body,
            headers={
                'Content-Type':  'application/json',
                'X-YouTube-Client-Name':    '67',
                'X-YouTube-Client-Version': '1.20250101.01.00',
                'Origin':   'https://music.youtube.com',
                'Referer':  'https://music.youtube.com/',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            },
            timeout=8,
        )
        if r.status_code != 200: return []
        data    = r.json()
        results = []
        tabs    = (data.get('contents', {})
                       .get('tabbedSearchResultsRenderer', {})
                       .get('tabs', []))
        for tab in tabs:
            section_list = (tab.get('tabRenderer', {})
                               .get('content', {})
                               .get('sectionListRenderer', {})
                               .get('contents', []))
            for section in section_list:
                items = (section.get('musicShelfRenderer', {}).get('contents', []))
                for item in items:
                    renderer = item.get('musicResponsiveListItemRenderer', {})
                    if not renderer: continue
                    overlay = renderer.get('overlay', {})
                    vid_id  = (overlay.get('musicItemThumbnailOverlayRenderer', {})
                                      .get('content', {})
                                      .get('musicPlayButtonRenderer', {})
                                      .get('playNavigationEndpoint', {})
                                      .get('watchEndpoint', {})
                                      .get('videoId', ''))
                    if not vid_id:
                        for col in renderer.get('flexColumns', []):
                            runs = (col.get('musicResponsiveListItemFlexColumnRenderer', {})
                                       .get('text', {}).get('runs', []))
                            for run in runs:
                                ep = run.get('navigationEndpoint', {}).get('watchEndpoint', {})
                                if ep.get('videoId'):
                                    vid_id = ep['videoId']; break
                            if vid_id: break
                    if not vid_id: continue
                    cols     = renderer.get('flexColumns', [])
                    title_t  = ''; artist_t = ''
                    for i, col in enumerate(cols):
                        runs = (col.get('musicResponsiveListItemFlexColumnRenderer', {})
                                   .get('text', {}).get('runs', []))
                        text = ' '.join(r.get('text', '') for r in runs).strip()
                        if i == 0: title_t = text
                        elif i == 1: artist_t = text.split('\u2022')[0].strip()
                    thumbs = (renderer.get('thumbnail', {})
                                      .get('musicThumbnailRenderer', {})
                                      .get('thumbnail', {}).get('thumbnails', []))
                    thumb = thumbs[-1]['url'] if thumbs else ''
                    if thumb: thumb = re.sub(r'=w\d+-h\d+', '=w500-h500', thumb)
                    results.append({'videoId': vid_id, 'title': title_t,
                                    'artist': artist_t, 'thumbnail': thumb})
                    if len(results) >= limit: return results
        return results
    except Exception as e:
        log.warning(f'[YTMusic] search error: {e}')
        return []

def _ytm_get_stream_url(video_id: str) -> Tuple[Optional[str], Optional[str]]:
    l1_key = f"ytm_stream:{video_id}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached.get('url'), cached.get('quality')

    ydl_opts = {
        'format':         'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'quiet':          True,
        'no_warnings':    True,
        'socket_timeout': 12,
        'extract_flat':   False,
        'noplaylist':     True,
        'http_headers':   {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f'https://music.youtube.com/watch?v={video_id}', download=False)
            if not info: return None, None
            formats = info.get('formats', [])
            audio_formats = [
                f for f in formats
                if f.get('acodec') not in ('none', None, '')
                and f.get('url')
                and f.get('vcodec') in ('none', None, '')
            ]
            if not audio_formats:
                audio_formats = [f for f in formats
                                 if f.get('acodec') not in ('none', None, '') and f.get('url')]
            if not audio_formats: return None, None
            best    = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            abr     = best.get('abr') or best.get('tbr') or 0
            quality = f"{int(abr)}kbps" if abr else 'unknown'
            url     = best['url']
            _l1_audio.set(l1_key, {'url': url, 'quality': quality})
            return url, quality
    except Exception as e:
        log.warning(f'[YTMusic] stream extract error {video_id}: {e}')
        return None, None

def fetch_from_ytmusic(title: str, artist: str = '') -> Optional[dict]:
    l1_key = f"ytmusic:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached

    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    query = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title
    results = _ytm_search(query, limit=8)
    if not results: results = _ytm_search(clean_title, limit=5)
    if not results: return None

    best = None; best_conf = -1.0
    for item in results:
        _ok, _conf, _reason = _is_confirmed_match(
            title, artist, item.get('title', ''), item.get('artist', ''),
            source='ytmusic', min_conf=0.60,
        )
        if not _ok:
            log.debug(f"[YTMusic] Rejected '{item.get('title')}': {_reason}")
            continue
        if _conf > best_conf:
            best_conf = _conf; best = item

    if not best or best_conf < 0.60: return None  # GODMODE FIX 4: was 0.45
    video_id = best['videoId']
    url, quality = _ytm_get_stream_url(video_id)
    if not url: return None

    result = {
        'url': url, 'quality': quality,
        'title': best.get('title', title), 'artist': best.get('artist', artist),
        'image': best.get('thumbnail', ''), 'source': 'ytmusic',
        '_confidence': round(best_conf, 3),
    }
    _l1_audio.set(l1_key, result)
    # Store artwork with lower priority (4) so Saavn/iTunes can override
    if best.get('thumbnail'):
        # Use cached Saavn/iTunes artwork if available (priority ≤ 3 wins)
        cached_art = _get_artwork(title, artist)
        if cached_art:
            result['image'] = cached_art
        else:
            _store_artwork(title, artist, best.get('thumbnail', ''), 4)
    log.info(f"[YTMusic] ✓ '{best['title']}' conf={best_conf:.2f} q={quality}")
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# YT-DLP
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_ytdlp(title, artist='') -> Optional[dict]:
    l1_key = f"ytdlp:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached

    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()

    search_queries = []
    if clean_artist:
        search_queries += [
            f"ytmsearch5:{clean_artist} {clean_title}",
            f"ytsearch5:{clean_artist} {clean_title} full song",
            f"ytmsearch3:{clean_title}",
            f"ytsearch3:{clean_title} {clean_artist} audio",
            f"ytsearch2:{clean_title} song",
        ]
    else:
        search_queries += [
            f"ytmsearch5:{clean_title}",
            f"ytsearch5:{clean_title} full song audio",
            f"ytsearch3:{clean_title} song",
        ]

    ydl_opts = {
        'format':         'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'quiet':          True,
        'no_warnings':    True,
        'socket_timeout': 15,
        'extract_flat':   False,
        'noplaylist':     True,
        'http_headers':   {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            best_result = None; best_conf = -1.0
            for search_q in search_queries:
                try:
                    info = ydl.extract_info(search_q, download=False)
                    if not info or not info.get('entries'): continue
                    entries = [e for e in info['entries'] if e and e.get('duration', 0) > 90]
                    if not entries: entries = [e for e in info['entries'] if e]
                    _wants_ver = (
                        _query_requests_version(title) or _query_requests_version(artist)
                    )
                    for entry in entries:
                        if not entry: continue
                        yt_title  = entry.get('title', '')
                        yt_artist = entry.get('uploader', '') or entry.get('artist', '')
                        # AGGRESSIVE: master gate replaces all individual checks
                        _ok, _conf, _reason = _is_confirmed_match(
                            title, artist, yt_title, yt_artist,
                            source='youtube', min_conf=0.60,
                        )
                        if not _ok:
                            log.debug(f"[yt-dlp] Rejected '{yt_title}': {_reason}")
                            continue
                        if 'music.youtube' in (entry.get('webpage_url') or ''):
                            _conf = min(1.0, _conf + 0.05)
                        if _conf > best_conf:
                            best_conf = _conf; best_result = entry
                    if best_conf >= 0.75: break
                except Exception:
                    continue

            if not best_result: return None
            # GODMODE FIX 12: raised floor from 0.50 → 0.60 to prevent wrong-song cache poisoning
            if best_conf < 0.60: return None

            formats       = best_result.get('formats', [])
            audio_formats = [f for f in formats
                             if f.get('acodec') not in ('none', None, '')
                             and f.get('url')
                             and (f.get('vcodec') in ('none', None, '') or not f.get('vcodec'))]
            if not audio_formats:
                audio_formats = [f for f in formats
                                 if f.get('acodec') not in ('none', None, '') and f.get('url')]
            if not audio_formats:
                audio_formats = [f for f in formats if f.get('url')]
            if not audio_formats: return None

            best_fmt = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            abr      = best_fmt.get('abr') or best_fmt.get('tbr') or 0
            quality  = f"{int(abr)}kbps" if abr else 'unknown'
            thumb    = best_result.get('thumbnail', '')
            if not thumb:
                vid_id = best_result.get('id', '')
                if vid_id: thumb = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"

            result = {
                'url':     best_fmt['url'],
                'quality': quality,
                'title':   best_result.get('title', title),
                'artist':  best_result.get('uploader', artist) or best_result.get('artist', artist),
                'image':   thumb,
                'source':  'youtube',
                '_confidence': round(best_conf, 3),
            }
            # Prefer cached Saavn/iTunes artwork over YouTube thumbnail
            cached_art = _get_artwork(title, artist)
            if cached_art:
                result['image'] = cached_art
            elif thumb:
                _store_artwork(title, artist, thumb, 5)
            # Only cache if confidence passes threshold
            if best_conf >= 0.50:
                _l1_audio.set(l1_key, result)
            log.info(f"[yt-dlp] ✓ '{best_result.get('title')}' conf={best_conf:.2f} q={quality}")
            return result
    except Exception as e:
        log.warning(f"[yt-dlp] '{title}' → {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SOUNDCLOUD
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_soundcloud(title, artist='') -> Optional[dict]:
    l1_key = f"sc:{normalize(title)}:{normalize(artist)}"
    cached = _l1_audio.get(l1_key)
    if cached: return cached

    _maybe_refresh_sc_id()
    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    query        = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title

    ydl_opts = {
        'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True,
        'socket_timeout': 12, 'noplaylist': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch5:{query}", download=False)
            if not info or not info.get('entries'): return None
            best = None; best_conf = -1.0
            _wants_ver_sc = (
                _query_requests_version(title) or _query_requests_version(artist)
            )
            for entry in info['entries']:
                if not entry or entry.get('duration', 0) < 60: continue
                sc_title = entry.get('title', '')
                # AGGRESSIVE: master gate
                _ok, _conf, _reason = _is_confirmed_match(
                    title, artist, sc_title, entry.get('uploader', ''),
                    source='soundcloud', min_conf=0.60,
                )
                if not _ok:
                    log.debug(f"[SoundCloud] Rejected '{sc_title}': {_reason}")
                    continue
                if _conf > best_conf: best_conf = _conf; best = entry
            if not best or best_conf < 0.60: return None  # GODMODE FIX 7: was 0.50
            formats  = best.get('formats', [])
            if not formats: return None
            best_fmt = max(formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            if not best_fmt.get('url'): return None
            abr     = best_fmt.get('abr') or best_fmt.get('tbr') or 0
            quality = f"{int(abr)}kbps" if abr else 'unknown'
            result  = {
                'url': best_fmt['url'], 'quality': quality,
                'title': best.get('title', title), 'artist': best.get('uploader', artist),
                'image': best.get('thumbnail', ''), 'source': 'soundcloud',
                '_confidence': round(best_conf, 3),
            }
            # Prefer higher-priority cached artwork (Saavn/iTunes) over SoundCloud art
            cached_art = _get_artwork(title, artist)
            if cached_art:
                result['image'] = cached_art
            _l1_audio.set(l1_key, result)
            return result
    except Exception as e:
        log.warning(f"[SoundCloud] '{title}' → {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# SAAVN BY ID
# ═══════════════════════════════════════════════════════════════════════════════
def _fetch_saavn_by_id(song_id: str, expected_title: str = '', expected_artist: str = '') -> Optional[dict]:
    # Check L1 first
    l1_key = f"saavn_id:{song_id}"
    cached = _l1_saavn.get(l1_key)
    if cached: return cached

    mirrors   = _best_mirrors(n=6)
    endpoints = [
        f'/api/songs/{song_id}', f'/songs/{song_id}',
        f'/api/songs?id={song_id}', f'/song?id={song_id}', f'/api/song?id={song_id}',
    ]

    def try_mirror(mirror):
        for endpoint in endpoints:
            try:
                t0 = time.time()
                r  = requests.get(f'{mirror}{endpoint}',
                                  timeout=_health.adaptive_timeout(mirror),
                                  headers={'User-Agent': 'Mozilla/5.0'})
                elapsed = (time.time() - t0) * 1000
                if r.status_code != 200: continue
                data = r.json()
                song = None
                if isinstance(data.get('data'), list) and data['data']:
                    song = data['data'][0]
                elif isinstance(data.get('data'), dict):
                    song = data['data']
                elif data.get('id'):
                    song = data
                elif data.get('songs'):
                    songs = data['songs']
                    song  = songs[0] if isinstance(songs, list) and songs else songs
                if not song: continue
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                if isinstance(raw_urls, str):
                    raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                best_url, quality = pick_best_quality(raw_urls)
                if best_url:
                    _health.record_ok(mirror, elapsed)
                    id_result = {
                        'url': best_url, 'quality': quality,
                        'title': song.get('name') or song.get('title', ''),
                        'artist': song.get('primaryArtists') or song.get('primary_artists') or '',
                        'image': pick_image(song),
                        '_raw_urls': raw_urls,
                    }
                    # GODMODE FIX 1: Hard-reject ID result if title mismatches expected
                    if expected_title and id_result.get('title'):
                        _id_verify_conf = compute_confidence(
                            expected_title, expected_artist,
                            id_result['title'], id_result.get('artist', ''),
                            source='saavn',
                        )
                        if _id_verify_conf < 0.65:  # AGGRESSIVE: was 0.60
                            log.warning(
                                f"[SaavnID] MISMATCH REJECTED: expected='{expected_title}' "
                                f"got='{id_result['title']}' conf={_id_verify_conf:.3f}"
                            )
                            return None
                    if id_result['image']:
                        _store_artwork(id_result['title'], id_result['artist'], id_result['image'], 1)
                    return id_result
            except Exception:
                _mirror_failed(mirror)
        return None

    futures = {_executor.submit(try_mirror, m): m for m in mirrors}
    try:
        for future in as_completed(futures, timeout=6):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    _l1_saavn.set(l1_key, result)
                    return result
            except Exception: pass
    except Exception: pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# FETCH FROM MIRROR  (enhanced: confidence-filtered, remix-aware)
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_mirror(mirror, query, min_score=0.4, title: str = '', artist: str = '', language: str = ''):
    if not _mirror_ok(mirror): return None
    # Use original title for version-request detection — query may be a cleaned variant
    _version_check_src  = title or query
    # FIX 6a: also check artist — "DJ Shadow", "Lofi Girl" etc should not be skipped
    _user_wants_version = (
        _query_requests_version(_version_check_src) or
        _query_requests_version(artist)
    )
    _conf_title  = title or query
    _conf_artist = artist or ''

    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0 = time.time()
            _fparams = {'query': query, 'q': query, 'limit': 15}
            if language:
                _fparams['language'] = language
            r  = requests.get(f'{mirror}{endpoint}',
                              params=_fparams,
                              timeout=_health.adaptive_timeout(mirror),
                              headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data    = r.json()
            results = (data.get('data', {}).get('results') or data.get('results') or
                       data.get('songs', {}).get('results') or [])

            # Collect ALL candidates — never first-wins
            candidates = []
            for song in results:
                song_title  = song.get('name') or song.get('title', '')
                song_artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                if not has_word_match(query, song_title): continue
                dur = int(song.get('duration', 999) or 999)
                if dur > 1080: continue
                # AGGRESSIVE: master gate replaces individual version checks
                _ok, _conf, _reason = _is_confirmed_match(
                    _conf_title, _conf_artist, song_title, song_artist,
                    source='saavn', res_dur_s=dur, min_conf=0.65,
                )
                if not _ok:
                    log.debug(f"[Mirror] Rejected '{song_title}': {_reason}")
                    continue
                legacy_score = title_score(query, song_title, song_artist)
                candidates.append((_conf, legacy_score, song))

            if not candidates: continue

            # Sort by confidence desc, legacy score as tie-break
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best_conf, best_legacy, best_song = candidates[0]

            if best_conf < 0.65: continue  # GODMODE FIX 9: was 0.50 — prevent wrong song from Saavn mirror search

            raw_urls = best_song.get('downloadUrl') or best_song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            best_url, quality = pick_best_quality(raw_urls)
            if not best_url: continue

            _health.record_ok(mirror, elapsed)
            result_data = {
                'url': best_url, 'quality': quality,
                'title': best_song.get('name') or best_song.get('title', ''),
                'artist': best_song.get('primaryArtists') or best_song.get('primary_artists') or '',
                'image': pick_image(best_song), 'score': round(best_legacy, 3),
                '_confidence': round(best_conf, 3),
                'source': 'saavn', '_raw_urls': raw_urls,
            }
            if result_data['image']:
                _store_artwork(result_data['title'], result_data['artist'], result_data['image'], 1)
            return result_data
        except Exception:
            _mirror_failed(mirror)
            continue
    return None


def fetch_saavn_parallel(query, title: str = '', artist: str = ''):
    # Check L1 first
    l1_key = f"saavn_q:{normalize(query)}"
    cached = _l1_saavn.get(l1_key)
    if cached: return cached

    # Detect language from title/artist for better Saavn results
    _lang = _detect_language((title or query) + ' ' + artist)
    threshold = dynamic_min_score(query)
    mirrors   = _best_mirrors(n=8)
    futures   = {_executor.submit(fetch_from_mirror, m, query, threshold, title, artist, _lang): m
                 for m in mirrors}
    all_results = []
    try:
        for future in as_completed(futures, timeout=6):
            try:
                result = future.result()
                if result: all_results.append(result)
            except Exception: pass
    except Exception: pass

    if not all_results: return None

    # Pick globally best _confidence — never take first-responder blindly
    all_results.sort(
        key=lambda r: (
            float(r.get('_confidence', r.get('score', 0))) +
            (0.02 if '320' in str(r.get('quality', '')) else 0)
        ),
        reverse=True,
    )
    best      = all_results[0]
    best_conf = float(best.get('_confidence', best.get('score', 0)))

    # AGGRESSIVE: Hard gate — if even the best result is low confidence, return None
    # This forces fallback to other sources instead of playing wrong song
    if best_conf < 0.65:
        log.warning(f"[Parallel] ALL results below confidence gate (best={best_conf:.2f}) for '{best.get('title')}' — rejecting")
        return None

    # L1 write only if confidence passes threshold (anti-poisoning)
    if best_conf >= _CACHE_MIN_CONFIDENCE:
        _l1_saavn.set(l1_key, best)
    log.info(f"[Parallel] ✓ '{best['title']}' conf={best_conf:.2f} q={best['quality']} mirrors_responded={len(all_results)}")
    return best


# ═══════════════════════════════════════════════════════════════════════════════
# PIPED
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_piped(query, title='', artist='') -> Optional[dict]:
    search_q = f"{title} {artist}".strip() if title else query
    with _piped_lock:
        instances = _health.sort_by_reputation(list(PIPED_INSTANCES))
    fail_count = 0
    for instance in instances:
        if not _health.is_alive(instance): continue
        try:
            t0 = time.time()
            r  = requests.get(f'{instance}/search',
                              params={'q': search_q, 'filter': 'music_songs'},
                              timeout=_health.adaptive_timeout(instance),
                              headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200:
                _health.record_fail(instance); fail_count += 1; continue
            results = r.json().get('items', [])
            if not results:
                _health.record_fail(instance); fail_count += 1; continue

            best = None; best_conf = -1.0
            _wants_ver_piped = (
                _query_requests_version(title or query) or _query_requests_version(artist)
            )
            for item in results[:5]:
                if item.get('type') != 'stream': continue
                if not has_word_match(query, item.get('title', '')): continue
                piped_title = item.get('title', '')
                # AGGRESSIVE: master gate
                _ok, _conf, _reason = _is_confirmed_match(
                    title or query, artist, piped_title, item.get('uploaderName', ''),
                    source='piped', min_conf=0.60,
                )
                if not _ok:
                    log.debug(f"[Piped] Rejected '{piped_title}': {_reason}")
                    continue
                if _conf > best_conf: best_conf = _conf; best = item

            if not best or best_conf < 0.60: continue  # GODMODE FIX 5: was 0.45
            video_id = best.get('url', '').replace('/watch?v=', '').strip()
            if not video_id: continue

            sr = requests.get(f'{instance}/streams/{video_id}', timeout=10,
                              headers={'User-Agent': 'Mozilla/5.0'})
            if sr.status_code != 200: continue
            audio_streams = sr.json().get('audioStreams', [])
            if not audio_streams: continue
            best_audio = max(audio_streams, key=lambda s: s.get('bitrate', 0))
            if not best_audio.get('url'): continue

            bitrate = best_audio.get('bitrate', 0)
            _health.record_ok(instance, elapsed)
            piped_result = {
                'url': best_audio['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title': best.get('title', title), 'artist': best.get('uploaderName', artist),
                'image': best.get('thumbnail', ''), 'source': 'piped',
                '_confidence': round(best_conf, 3),
            }
            cached_art = _get_artwork(title, artist)
            if cached_art:
                piped_result['image'] = cached_art
            return piped_result
        except Exception as e:
            _health.record_fail(instance); fail_count += 1
            log.warning(f"[Piped {instance}] {e}"); continue
    if fail_count >= len(instances): _maybe_reactive_heal('piped')
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# INVIDIOUS
# ═══════════════════════════════════════════════════════════════════════════════
def fetch_from_invidious(query, title='', artist='') -> Optional[dict]:
    search_q = f"{title} {artist}".strip() if title else query
    with _invidious_lock:
        instances = _health.sort_by_reputation(list(INVIDIOUS_INSTANCES))
    fail_count = 0
    for instance in instances:
        if not _health.is_alive(instance): continue
        try:
            t0 = time.time()
            r  = requests.get(f'{instance}/api/v1/search',
                              params={'q': search_q, 'type': 'video', 'page': 1},
                              timeout=_health.adaptive_timeout(instance),
                              headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200:
                _health.record_fail(instance); fail_count += 1; continue
            results = r.json()
            if not results:
                _health.record_fail(instance); fail_count += 1; continue

            best = None; best_conf = -1.0
            _wants_ver_inv = (
                _query_requests_version(title or query) or _query_requests_version(artist)
            )
            for item in results[:5]:
                if not has_word_match(query, item.get('title', '')): continue
                inv_title = item.get('title', '')
                # AGGRESSIVE: master gate
                _ok, _conf, _reason = _is_confirmed_match(
                    title or query, artist, inv_title, item.get('author', ''),
                    source='invidious', min_conf=0.60,
                )
                if not _ok:
                    log.debug(f"[Invidious] Rejected '{inv_title}': {_reason}")
                    continue
                if _conf > best_conf: best_conf = _conf; best = item

            if not best or best_conf < 0.60: continue  # GODMODE FIX 6: was 0.45
            video_id = best.get('videoId', '')
            if not video_id: continue

            vr = requests.get(f'{instance}/api/v1/videos/{video_id}',
                              params={'fields': 'adaptiveFormats,title,author'},
                              timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if vr.status_code != 200: continue
            formats = vr.json().get('adaptiveFormats', [])
            audio_formats = [f for f in formats if f.get('type', '').startswith('audio')]
            if not audio_formats: continue
            best_fmt = max(audio_formats, key=lambda f: f.get('bitrate', 0))
            if not best_fmt.get('url'): continue

            bitrate = best_fmt.get('bitrate', 0)
            _health.record_ok(instance, elapsed)
            inv_result = {
                'url': best_fmt['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title': best.get('title', title), 'artist': best.get('author', artist),
                'image': f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
                'source': 'invidious',
                '_confidence': round(best_conf, 3),
            }
            cached_art = _get_artwork(title, artist)
            if cached_art:
                inv_result['image'] = cached_art
            return inv_result
        except Exception as e:
            _health.record_fail(instance); fail_count += 1
            log.warning(f"[Invidious {instance}] {e}"); continue
    if fail_count >= len(instances): _maybe_reactive_heal('invidious')
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# JIOSAVAN — Primary Source
# ═══════════════════════════════════════════════════════════════════════════════
_JIOSAVAN_BASE = 'https://jiosavan.onrender.com'

def fetch_from_jiosavan(title: str, artist: str = '') -> Optional[dict]:
    l1_key = f"jiosavan:{normalize(title)}:{normalize(artist)}"
    cached = _l1_saavn.get(l1_key)
    if cached: return cached

    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    query        = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title

    try:
        t0 = time.time()
        r  = requests.get(f'{_JIOSAVAN_BASE}/song/',
                          params={'query': query, 'songdata': 'true'},
                          timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code != 200: return None
        data  = r.json()
        songs = data if isinstance(data, list) else data.get('songs', []) or data.get('results', [])
        if not songs: return None

        best = None; best_conf = -1.0
        _user_wants_ver = (
            _query_requests_version(title) or _query_requests_version(artist)
        )
        for song in songs[:5]:
            song_title  = song.get('song') or song.get('title') or song.get('name', '')
            song_artist = song.get('primary_artists') or song.get('singers') or song.get('artist', '')
            song_dur    = int(song.get('duration', 0) or 0)
            # AGGRESSIVE: use master gate for every candidate
            _ok, _conf, _reason = _is_confirmed_match(
                title, artist, song_title, song_artist,
                source='jiosavan', duration_s=0, res_dur_s=song_dur, min_conf=0.65,
            )
            if not _ok:
                log.debug(f"[JioSavan] Rejected '{song_title}': {_reason}")
                continue
            if _conf > best_conf: best_conf = _conf; best = song

        if not best or best_conf < 0.65: return None  # GODMODE FIX 8: was 0.50 — primary source must be strict

        media_url = (best.get('media_url') or best.get('encrypted_media_url') or
                     best.get('download_url') or '')
        if not media_url: return None

        # FIX BUG 2: pick quality from downloadUrl array if available; don't hardcode '320kbps'
        raw_dl = best.get('downloadUrl') or best.get('download_url') or []
        if isinstance(raw_dl, str):
            raw_dl = [{'url': raw_dl, 'quality': 'unknown'}]
        if raw_dl:
            _best_dl_url, _best_quality = pick_best_quality(raw_dl)
            if _best_dl_url:
                media_url = _best_dl_url
        else:
            _best_quality = '320kbps'
        jiosavan_quality = _best_quality or '320kbps'

        image = best.get('image', '')
        if image: image = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', image)

        result = {
            'url':    media_url, 'quality': jiosavan_quality,
            'title':  best.get('song') or best.get('title', title),
            'artist': best.get('primary_artists') or best.get('singers', artist),
            'image':  image, 'source': 'jiosavan',
            'score':  round(best_conf, 3), '_confidence': round(best_conf, 3),
        }
        if image:
            _store_artwork(title, artist, image, 1)
        else:
            cached_art = _get_artwork(title, artist)
            if cached_art:
                result['image'] = cached_art
        # Only cache if confidence passes threshold — anti-poisoning
        if best_conf >= _CACHE_MIN_CONFIDENCE:
            _l1_saavn.set(l1_key, result)
        _health.record_ok(_JIOSAVAN_BASE, elapsed)
        log.info(f"[JioSavan] ✓ '{result['title']}' conf={best_conf:.2f}")
        return result
    except Exception as e:
        log.warning(f"[JioSavan] '{title}' → {e}")
        _health.record_fail(_JIOSAVAN_BASE)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ███████████████████  /api/play  — SMART PLAYBACK PIPELINE  ██████████████████
#
#  Pipeline:  L1 Check → L2 Check → Saavn ID → Saavn Search →
#             Parallel Fallbacks → Broad YT → Stream
#
#  Each result is tagged with confidence.
#  Only conf ≥ 0.75 results get written to L2 (Supabase).
#  Background resolution for next-likely songs via prefetch.
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    song_id = request.args.get('id', '').strip()[:100]
    title   = request.args.get('title', '').strip()[:200]
    artist  = request.args.get('artist', '').strip()[:100]

    if not song_id and not title:
        return jsonify({'error': 'Missing id or title'}), 400

    audio_url  = None
    quality    = 'unknown'
    source     = 'unknown'
    confidence = 0.0

    # FIX A: Dual cache keys — one by ID, one by title+artist
    # This ensures the SAME song is always found in cache regardless of how
    # the frontend calls /api/play (by id, or by title, or both).
    _play_ck       = f"play:{song_id or normalize(title)}:{normalize(artist)}"
    _play_ck_id    = f"play:{song_id}:{normalize(artist)}"    if song_id else None
    _play_ck_title = f"play:{normalize(title)}:{normalize(artist)}" if title  else None

    def _check_cache_entry(entry):
        """Return entry if valid, not an unwanted version, and title still matches. Else None."""
        if not entry or not entry.get('url'):
            return None
        _ct = entry.get('title', '')
        # Reject cached remix/slowed/cover if user didn't ask for it
        if not _user_wants_ver and (
            _is_remix_or_cover(_ct) or _is_slowed_reverb(_ct) or _is_live_version(_ct)
        ):
            return None
        # GODMODE FIX 2: Re-verify cached entry still matches current request title/artist.
        # Prevents stale cache from serving wrong song when same key is reused.
        _cached_title  = entry.get('title', '')
        _cached_artist = entry.get('artist', '')
        if title and _cached_title:
            _recheck_conf = compute_confidence(
                title, artist, _cached_title, _cached_artist, source='saavn'
            )
            if _recheck_conf < 0.65:  # AGGRESSIVE: was 0.60
                log.info(
                    f"[Cache] STALE ENTRY REJECTED: requested='{title}' "
                    f"cached='{_cached_title}' conf={_recheck_conf:.3f}"
                )
                return None
        return entry

    # ── 0. Verified cache — highest confidence results, fastest path ──────────
    _user_wants_ver = _query_requests_version(title or '') or _query_requests_version(artist or '')

    def _try_verified(key_id='', key_title='', key_artist=''):
        hit = _get_verified(song_id=key_id, title=key_title, artist=key_artist)
        return _check_cache_entry(hit)

    _verified_hit = _try_verified(song_id, title, artist)
    if not _verified_hit and _play_ck_id and song_id:
        _verified_hit = _try_verified(song_id=song_id)
    if _verified_hit:
        audio_url  = _verified_hit['url']
        quality    = _verified_hit.get('quality', 'unknown')
        source     = _verified_hit.get('source', 'unknown')
        confidence = float(_verified_hit.get('confidence', 0.90))
        if not title:  title  = _verified_hit.get('title', '')
        if not artist: artist = _verified_hit.get('artist', '')
        log.info(f"[Cache:Verified] HIT play key={_play_ck}")

    # ── 1. L1 cache (< 1ms) — check BOTH keys ────────────────────────────────
    if not audio_url:
        for _ck in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
            l1_hit = _check_cache_entry(_l1_saavn.get(_ck))
            if l1_hit:
                audio_url  = l1_hit['url']
                quality    = l1_hit.get('quality', 'unknown')
                source     = l1_hit.get('source', 'unknown')
                confidence = float(l1_hit.get('confidence', 1.0))
                if not title:  title  = l1_hit.get('title', '')
                if not artist: artist = l1_hit.get('artist', '')
                log.info(f"[Cache:L1] HIT key={_ck}")
                break
            elif _l1_saavn.get(_ck):
                # Entry exists but is an unwanted version — purge it
                _l1_saavn.delete(_ck)
                log.info(f"[Cache:L1] INVALIDATED unwanted version key={_ck}")

    # ── 2. L2 Supabase cache — check BOTH keys ───────────────────────────────
    if not audio_url:
        for _ck in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
            l2_hit = _check_cache_entry(_supabase_cache_get(_ck))
            if l2_hit:
                audio_url  = l2_hit['url']
                quality    = l2_hit.get('quality', 'unknown')
                source     = l2_hit.get('source', 'unknown')
                confidence = float(l2_hit.get('confidence', 1.0))
                if not title:  title  = l2_hit.get('title', '')
                if not artist: artist = l2_hit.get('artist', '')
                log.info(f"[Cache:L2] HIT key={_ck}")
                # Refresh L1 under all relevant keys
                for _wk in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
                    _l1_saavn.set(_wk, l2_hit)
                break
            elif _supabase_cache_get(_ck) is not None:
                _executor.submit(sb_delete, 'song_cache', {'cache_key': _ck})
                log.info(f"[Cache:L2] INVALIDATED unwanted version key={_ck}")

    # ── 3. Saavn ID path: ONLY ID-based fetch ─────────────────────────────────
    if not audio_url and song_id:
        result = _fetch_saavn_by_id(song_id, expected_title=title, expected_artist=artist)
        if result and result.get('url'):
            audio_url  = result['url']
            quality    = result.get('quality', 'unknown')
            source     = 'saavn'
            confidence = 0.95   # ID-based = very high confidence
            if not title:  title  = result.get('title', '')
            if not artist: artist = result.get('artist', '')
            log.info(f"[Play] ✓ Saavn ID={song_id} q={quality}")
            # FIX B: immediately cache under title key too — next call by title hits L1
            if title and _play_ck_title:
                _early_cache = {**result, 'source': 'saavn', 'confidence': 0.95,
                                'title': title, 'artist': artist}
                _l1_saavn.set(_play_ck_title, _early_cache)
                _l1_saavn.set(_play_ck_id,    _early_cache)
        else:
            # ID fetch failed — only use title if provided
            if title:
                for query_var in build_query_variants(title, artist, ''):
                    result = fetch_saavn_parallel(query_var, title=title, artist=artist)
                    if result and result.get('url'):
                        audio_url  = result['url']
                        quality    = result.get('quality', 'unknown')
                        source     = 'saavn'
                        confidence = float(result.get('_confidence', result.get('score', 0.5)))
                        log.info(f"[Play] ✓ Saavn title fallback '{result['title']}' q={quality}")
                        break

    # ── 4. Title-only path ────────────────────────────────────────────────────
    # FIX 14: was elif — if song_id was present but fetch failed, this was skipped entirely
    if not audio_url and title:
        for query_var in build_query_variants(title, artist, ''):
            result = fetch_saavn_parallel(query_var, title=title, artist=artist)
            if result and result.get('url'):
                audio_url  = result['url']
                quality    = result.get('quality', 'unknown')
                source     = 'saavn'
                confidence = float(result.get('_confidence', result.get('score', 0.5)))
                log.info(f"[Play] ✓ Saavn title='{result['title']}' q={quality}")
                break

    # ── 5. SCORED PARALLEL FALLBACKS ─────────────────────────────────────────
    # FIX C: Saavn-first strategy.
    # Phase 1 (0–1.5s): Saavn + JioSavan only (most accurate sources).
    # Phase 2 (only if Phase 1 fails): All other sources in parallel.
    # Winner must have confidence ≥ 0.65 to be accepted.
    # This prevents YouTube/SoundCloud low-confidence results from
    # beating a slower-but-correct Saavn result.
    if not audio_url and title:
        log.info(f"[Play] Saavn miss → Saavn-priority fallbacks: '{title}'")

        _MIN_FALLBACK_CONF = 0.72   # AGGRESSIVE: was 0.70 — every fallback source must clear this bar

        # Phase 1: Fast Saavn-family sources only
        _phase1_futures = {
            _executor.submit(fetch_from_jiosavan, title, artist): 'jiosavan',
        }
        _phase1_candidates = []
        try:
            for future in as_completed(_phase1_futures, timeout=3.0):
                try:
                    res = future.result()
                    if res and res.get('url'):
                        conf = float(res.get('_confidence', 0.50))
                        if conf >= _MIN_FALLBACK_CONF:
                            _phase1_candidates.append((conf, res, _phase1_futures[future]))
                except Exception:
                    pass
        except Exception:
            pass

        if _phase1_candidates:
            # Sort by confidence — take best
            _phase1_candidates.sort(key=lambda x: -x[0])
            _p1_conf, _p1_res, _p1_src = _phase1_candidates[0]
            audio_url  = _p1_res['url']
            quality    = _p1_res.get('quality', 'unknown')
            source     = _p1_res.get('source', _p1_src)
            confidence = _p1_conf
            if not title:  title  = _p1_res.get('title', title)
            if not artist: artist = _p1_res.get('artist', artist)
            log.info(f"[Play] ✓ Phase1 winner: {source} conf={_p1_conf:.3f}")

        # Phase 2: Only if Phase 1 produced nothing — launch all remaining sources
        if not audio_url:
            log.info(f"[Play] Phase1 miss → Phase2 all-sources: '{title}'")
            _COLLECT_WINDOW  = 1.5    # raised from 0.6s → 1.5s so more results arrive
            _CONF_DIFF_FLOOR = 0.05

            _all_fb_futures = {
                _executor.submit(fetch_from_ytmusic,    title, artist):              'ytmusic',
                _executor.submit(fetch_from_ytdlp,      title, artist):              'youtube',
                _executor.submit(fetch_from_soundcloud, title, artist):              'soundcloud',
                _executor.submit(fetch_from_piped,      title, title=title,
                                 artist=artist):                                      'piped',
                _executor.submit(fetch_from_invidious,  title, title=title,
                                 artist=artist):                                      'invidious',
            }

            _fb_candidates  = []
            _arrival_idx    = 0
            _deadline       = time.time() + _COLLECT_WINDOW

            try:
                remaining_timeout = max(0.05, _deadline - time.time())
                for future in as_completed(_all_fb_futures, timeout=remaining_timeout):
                    try:
                        res = future.result()
                        if res and res.get('url'):
                            src_name = _all_fb_futures[future]
                            conf     = float(res.get('_confidence', 0.50))
                            if conf >= _MIN_FALLBACK_CONF:   # FIX C2: reject low-conf results immediately
                                _fb_candidates.append((_arrival_idx, conf, res, src_name))
                                _arrival_idx += 1
                    except Exception:
                        pass
                    if time.time() >= _deadline:
                        break
            except Exception:
                pass

            # Extend window if we got nothing yet
            if not _fb_candidates:
                try:
                    for future in as_completed(_all_fb_futures, timeout=8):
                        try:
                            res = future.result()
                            if res and res.get('url'):
                                src_name = _all_fb_futures[future]
                                conf     = float(res.get('_confidence', 0.50))
                                if conf >= _MIN_FALLBACK_CONF:
                                    _fb_candidates.append((_arrival_idx, conf, res, src_name))
                                    _arrival_idx += 1
                                    if len(_fb_candidates) >= 2 or conf >= 0.85:
                                        break
                        except Exception:
                            pass
                except Exception:
                    pass

            if _fb_candidates:
                _fb_candidates.sort(key=lambda x: (-x[1], x[0]))
                _best_conf  = _fb_candidates[0][1]

                _winner_idx, _winner_conf, _winner_res, _winner_src = _fb_candidates[0]
                for _arr, _c, _r, _s in _fb_candidates:
                    if _arr < _winner_idx and (_best_conf - _c) < _CONF_DIFF_FLOOR:
                        _winner_idx  = _arr
                        _winner_conf = _c
                        _winner_res  = _r
                        _winner_src  = _s
                        break

                for f in _all_fb_futures:
                    f.cancel()

                audio_url  = _winner_res['url']
                quality    = _winner_res.get('quality', 'unknown')
                source     = _winner_res.get('source', _winner_src)
                confidence = _winner_conf
                if not title:  title  = _winner_res.get('title', title)
                if not artist: artist = _winner_res.get('artist', artist)
                if _winner_res.get('image') and title:
                    _art_priority = (
                        1 if source in ('saavn', 'jiosavan') else
                        2 if source == 'itunes' else
                        4 if source == 'ytmusic' else 5
                    )
                    _store_artwork(title, artist, _winner_res['image'], _art_priority)
                log.info(
                    f"[Play] ✓ Phase2 winner: {source} conf={_winner_conf:.3f} "
                    f"arrival={_winner_idx} candidates={len(_fb_candidates)} "
                    f"title='{_winner_res.get('title')}' q={quality}"
                )

    # ── 6. Broad YouTube last-resort ─────────────────────────────────────────
    if not audio_url and title:
        for broad_query in [title, title.split()[0] if title.split() else title]:
            broad = fetch_from_ytdlp(broad_query, artist)   # pass artist for scoring
            if broad and broad.get('url'):
                broad_conf = float(broad.get('_confidence', 0.0))
                if broad_conf < 0.60: continue   # GODMODE FIX 3: was 0.35 — too permissive, covers/remixes slipped in
                audio_url  = broad['url']
                quality    = broad.get('quality', 'unknown')
                source     = 'youtube-broad'
                confidence = broad_conf if broad_conf > 0 else 0.30
                break

    if not audio_url:
        log.warning(f"[Play] ✗ ALL sources failed id={song_id} title='{title}'")
        return jsonify({'error': 'No audio source found'}), 404

    # AGGRESSIVE FINAL GATE — Never stream low confidence result
    if title and audio_url:
        if confidence < 0.65 and source not in ('saavn',) and not song_id:
            log.warning(
                f"[Play] FINAL GATE REJECTED: low confidence={confidence:.3f} "
                f"source={source} title='{title}' — refusing to stream wrong song"
            )
            return jsonify({'error': 'No confident audio match found', 'confidence': confidence}), 404

    # ── 7. Async cache writes (only if confidence passes threshold) ───────────
    # FIX BUG T2: also try artwork from winner_res image if _get_artwork is empty
    _best_art = ''
    if title:
        _best_art = _get_artwork(title, artist)
        if not _best_art and audio_url:
            for _src_key in [f"saavn_q:{normalize(title)}", _play_ck]:
                _art_hit = _l1_saavn.get(_src_key)
                if _art_hit and _art_hit.get('image'):
                    _best_art = _art_hit['image']
                    break
    elif song_id:
        _best_art = _get_artwork(title or song_id, artist)

    _cache_payload = {
        'url': audio_url, 'quality': quality, 'source': source,
        'title': title, 'artist': artist, 'confidence': confidence,
        'image': _best_art,
    }
    # FIX D: Write under ALL relevant keys — id key + title key + legacy key
    # This means the next call (by id OR by title) will hit L1 immediately.
    if confidence >= _CACHE_MIN_CONFIDENCE:
        for _wk in filter(None, [_play_ck_id, _play_ck_title, _play_ck]):
            _l1_saavn.set(_wk, _cache_payload)
        if title:
            _l1_saavn.set(f"saavn_q:{normalize(title)}", _cache_payload)
    _executor.submit(_supabase_cache_set, _play_ck, _cache_payload, confidence)
    # Also write L2 under the title key for cross-session persistence
    if _play_ck_title and _play_ck_title != _play_ck:
        _executor.submit(_supabase_cache_set, _play_ck_title, _cache_payload, confidence)
    # Write to verified cache if confidence is high (≥ 0.85)
    _store_verified(song_id, title, artist, _cache_payload, confidence)

    # ── 8. Stream to client ──────────────────────────────────────────────────
    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header: req_headers['Range'] = range_header

        upstream     = requests.get(audio_url, headers=req_headers, stream=True,
                                    timeout=(10, None), allow_redirects=True)  # FIX 13: was timeout=60 — caused mid-song cutoff
        excluded     = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges':  'bytes',
            'Cache-Control':  'no-store',
            'X-Audio-Quality': quality,
            'X-Audio-Source':  source,
            'X-Confidence':    str(round(confidence, 3)),
            # FIX BUG T1: expose artwork URL in response header so frontend can show thumbnail
            'X-Artwork-URL':   _best_art or '',
            'X-Song-Title':    (title or '')[:200],
            'X-Song-Artist':   (artist or '')[:100],
            # Expose these headers to browser JS (CORS)
            'Access-Control-Expose-Headers': (
                'Content-Length, Content-Range, X-Audio-Quality, X-Audio-Source, '
                'X-Confidence, X-Artwork-URL, X-Song-Title, X-Song-Artist'
            ),
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally:
                upstream.close()

        return Response(stream_with_context(generate()), status=upstream.status_code,
                        headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        log.error(f"[Play] Stream error: {e}")
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# /api/songs  — Parallel fetch with confidence-filtered deduplication
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q       = request.args.get('q', 'top bollywood songs').strip()
    era     = request.args.get('era', '').strip()
    is_90s  = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    cache_key = f"songs:{search_term.lower()}"
    cached    = _l1_meta.get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, '_cached': True})

    # fallback to legacy cache
    legacy_cached = _cache_get(cache_key)
    if legacy_cached is not None:
        return jsonify({'results': legacy_cached, '_cached': True})

    itunes_results = []
    saavn_results  = []

    def fetch_itunes():
        nonlocal itunes_results
        try:
            r = requests.get('https://itunes.apple.com/search',
                             params={'term': search_term, 'media': 'music', 'entity': 'song',
                                     'limit': 50, 'country': 'IN'}, timeout=12)
            r.raise_for_status()
            results = r.json().get('results', [])
            if is_90s:
                filtered = [s for s in results if s.get('trackName') and
                            1990 <= _safe_year(s.get('releaseDate')) <= 1999]
                if len(filtered) < 5:
                    filtered = [s for s in results if s.get('trackName')]
                random.shuffle(filtered)
                candidates = filtered[:30]
            else:
                candidates = [s for s in results if s.get('trackName')][:30]

            resolve_futures = {
                _executor.submit(_resolve_itunes_to_saavn, s): s
                for s in candidates
            }
            resolved = []
            try:
                for future in as_completed(resolve_futures, timeout=10):
                    try:
                        res = future.result()
                        if res: resolved.append(res)
                    except Exception:
                        pass
            except Exception:
                pass
            itunes_results = resolved[:30]
        except Exception:
            pass

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
        except Exception:
            pass

    t1 = threading.Thread(target=fetch_itunes)
    t2 = threading.Thread(target=fetch_saavn)
    t1.start(); t2.start()
    t1.join(timeout=15)
    t2.join(timeout=8)    # FIX 15: was 4s — Saavn mirrors often need 5-7s on cold start

    merged = list(itunes_results)
    for s in saavn_results:
        if not any(is_likely_duplicate(s, e) for e in merged):
            merged.append(s)

    if merged:
        _l1_meta.set(cache_key, merged)
        _cache_set(cache_key, merged)
        return jsonify({'results': merged})

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
        return jsonify({'results': cached, 'seed': seed, '_cached': True})

    raw = _fetch_saavn_search_parallel(seed)
    if raw:
        normalized = _normalize_saavn_songs(raw)
        filtered   = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        result     = (filtered if len(filtered) >= 5 else normalized)[:30]
        random.shuffle(result)
        _l1_meta.set(cache_key, result)
        _cache_set(cache_key, result)
        return jsonify({'results': result, 'seed': seed})

    try:
        r = requests.get('https://itunes.apple.com/search',
                         params={'term': seed, 'media': 'music', 'entity': 'song',
                                 'limit': 50, 'country': 'IN'}, timeout=15)
        r.raise_for_status()
        results  = r.json().get('results', [])
        filtered = [s for s in results if s.get('trackName') and
                    1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        if len(filtered) < 5: filtered = [s for s in results if s.get('trackName')]
        random.shuffle(filtered)
        candidates = filtered[:30]

        resolve_futures = {
            _executor.submit(_resolve_itunes_to_saavn, s): s for s in candidates
        }
        resolved = []
        try:
            for future in as_completed(resolve_futures, timeout=10):
                try:
                    res = future.result()
                    if res: resolved.append(res)
                except Exception: pass
        except Exception: pass

        result = resolved[:30]
        _l1_meta.set(cache_key, result)
        _cache_set(cache_key, result)
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

    _ck = f"saavn:{normalize(q)}:{normalize(artist)}"

    # L1 check
    if not low_quality:
        l1_hit = _l1_saavn.get(_ck)
        if l1_hit:
            log.info(f"[Cache:L1] HIT saavn: '{q}'")
            # FIX BUG T5: always inject best available artwork; if still empty try artwork cache
            _best_art = _get_artwork(q, artist)
            if not _best_art and l1_hit.get('image'):
                _best_art = l1_hit['image']
            if _best_art:
                l1_hit = dict(l1_hit); l1_hit['image'] = _best_art
            return jsonify({'success': True, 'token': token, **l1_hit})

    # L2 check
    _cached = _supabase_cache_get(_ck)
    if _cached and not low_quality:
        log.info(f"[Cache:L2] HIT saavn: '{q}'")
        # FIX BUG T5: best art from cache → fallback to existing image in payload
        _best_art = _get_artwork(q, artist)
        if not _best_art and _cached.get('image'):
            _best_art = _cached['image']
        if _best_art:
            _cached = dict(_cached); _cached['image'] = _best_art
        _l1_saavn.set(_ck, _cached)
        return jsonify({'success': True, 'token': token, **_cached})

    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query, title=q, artist=artist)
        if result:
            if low_quality:
                low_url, low_q = _pick_low_quality(result.get('_raw_urls', []))
                if low_url: result['url'] = low_url; result['quality'] = low_q
            # BUG-9 fix: read _confidence, not legacy 'score'
            conf = float(result.get('_confidence', result.get('score', 0.5)))
            # BUG-10 fix: inject best cached artwork before returning
            _best_art = _get_artwork(q, artist)
            if _best_art: result['image'] = _best_art
            if conf >= _CACHE_MIN_CONFIDENCE:
                _l1_saavn.set(_ck, result)
            _executor.submit(_supabase_cache_set, _ck, result, conf)
            return jsonify({'success': True, 'token': token, **result})

    ytm = fetch_from_ytmusic(q, artist)
    if ytm and ytm.get('url'):
        conf = float(ytm.get('_confidence', 0.0))
        _best_art = _get_artwork(q, artist)
        if _best_art: ytm['image'] = _best_art
        if conf >= _CACHE_MIN_CONFIDENCE:
            _l1_saavn.set(_ck, ytm)
        _executor.submit(_supabase_cache_set, _ck, ytm, conf)
        return jsonify({'success': True, 'token': token, **ytm})

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        conf = float(yt.get('_confidence', 0.0))
        _best_art = _get_artwork(q, artist)
        if _best_art: yt['image'] = _best_art
        if conf >= _CACHE_MIN_CONFIDENCE:
            _l1_saavn.set(_ck, yt)
        _executor.submit(_supabase_cache_set, _ck, yt, conf)
        return jsonify({'success': True, 'token': token, **yt})

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
        conf = float(sc.get('_confidence', 0.0))
        _best_art = _get_artwork(q, artist)
        if _best_art: sc['image'] = _best_art
        if conf >= _CACHE_MIN_CONFIDENCE:
            _l1_saavn.set(_ck, sc)
        _executor.submit(_supabase_cache_set, _ck, sc, conf)
        return jsonify({'success': True, 'token': token, **sc})

    return jsonify({'success': False, 'url': None, 'token': token})


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

    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query, title=q, artist=artist)
        if result:
            _rart = _get_artwork(q, artist) or result.get('image', '')
            return jsonify({
                'success': True, 'token': token,
                'url':     f"/api/stream?url={quote(result['url'], safe='')}",
                'quality': result['quality'], 'title': result['title'],
                'artist':  result['artist'], 'image': _rart,
                'source':  'saavn',
            })

    ytm = fetch_from_ytmusic(q, artist)
    if ytm and ytm.get('url'):
        _rart = _get_artwork(q, artist) or ytm.get('image', '')
        return jsonify({'success': True, 'token': token,
                        'url':    f"/api/stream?url={quote(ytm['url'], safe='')}",
                        'quality': ytm['quality'], 'title': ytm['title'],
                        'artist':  ytm['artist'], 'image': _rart,
                        'source':  'ytmusic'})

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        _rart = _get_artwork(q, artist) or yt.get('image', '')
        return jsonify({'success': True, 'token': token,
                        'url':    f"/api/stream?url={quote(yt['url'], safe='')}",
                        'quality': yt['quality'], 'title': yt['title'],
                        'artist':  yt['artist'], 'image': _rart,
                        'source':  'youtube'})

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
        _rart = _get_artwork(q, artist) or sc.get('image', '')
        return jsonify({'success': True, 'token': token,
                        'url':    f"/api/stream?url={quote(sc['url'], safe='')}",
                        'quality': sc['quality'], 'title': sc['title'],
                        'artist':  sc['artist'], 'image': _rart,
                        'source':  'soundcloud'})

    piped = fetch_from_piped(q, title=q, artist=artist)
    if piped and piped.get('url'):
        # Piped CDN domains not in ALLOWED_STREAM_DOMAINS — return raw URL directly
        return jsonify({'success': True, 'token': token,
                        'url':    piped['url'],
                        'quality': piped['quality'], 'title': piped['title'],
                        'artist':  piped['artist'], 'image': _get_artwork(q, artist) or piped.get('image', ''),
                        'source':  'piped'})

    inv = fetch_from_invidious(q, title=q, artist=artist)
    if inv and inv.get('url'):
        # Invidious CDN domains not in ALLOWED_STREAM_DOMAINS — return raw URL directly
        return jsonify({'success': True, 'token': token,
                        'url':    inv['url'],
                        'quality': inv['quality'], 'title': inv['title'],
                        'artist':  inv['artist'], 'image': _get_artwork(q, artist) or inv.get('image', ''),
                        'source':  'invidious'})

    return jsonify({'success': False, 'url': None, 'token': token})


# ═══════════════════════════════════════════════════════════════════════════════
# STREAM PROXY
# ═══════════════════════════════════════════════════════════════════════════════
def _is_allowed_domain(domain):
    for allowed in ALLOWED_STREAM_DOMAINS:
        # Only exact match or proper subdomain — never substring match.
        # `allowed in domain` would let evil-googlevideo.com.attacker.com pass.
        if domain == allowed or domain.endswith('.' + allowed):
            return True
    return False

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
        upstream     = requests.get(url, headers=req_headers, stream=True, timeout=(10, None), allow_redirects=True)  # FIX 13b
        excluded     = {'content-encoding', 'transfer-encoding', 'connection'}
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

    for query in build_query_variants(q, artist, ''):
        result = fetch_saavn_parallel(query, title=q, artist=artist)
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

    # Validate stream_url domain — same allowlist as /api/stream
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
                                  stream=True, timeout=(10, None), allow_redirects=True)  # FIX 13c
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
# /api/health  — Enhanced with reputation scores
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
            key=lambda x: x['reputation'],
            reverse=True,
        )

    with _sc_client_id_lock:
        sc_id = SOUNDCLOUD_CLIENT_ID

    return jsonify({
        'saavn':     {'count': len(saavn_list),  'instances': summarize(saavn_list)},
        'piped':     {'count': len(piped_list),  'instances': summarize(piped_list)},
        'invidious': {'count': len(inv_list),    'instances': summarize(inv_list)},
        'soundcloud': {'client_id_prefix': sc_id[:8] + '...' if sc_id else 'missing'},
        'cache': {
            'l1_meta_size':    _l1_meta.size(),
            'l1_audio_size':   _l1_audio.size(),
            'l1_popular_size': _l1_popular.size(),
            'l1_saavn_size':   _l1_saavn.size(),
            'l1_artwork_size': _l1_artwork.size(),
            'l1_verified_size': _l1_verified.size(),
        },
        'timestamp': round(time.time()),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH — Google Login
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

    log.info(f"[Auth] User saved: {profile.get('email', '')} | pic: {bool(profile.get('picture'))}")
    return jsonify({
        'success': True,
        'sub':     sub,
        'name':    profile.get('name', ''),
        'email':   profile.get('email', ''),
        'picture': profile.get('picture', ''),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC — Playback State
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

    # Validate artUrl — must be https:// to prevent stored XSS / data: injection
    raw_art_url = str(data.get('artUrl', '') or '').strip()
    art_url = raw_art_url if raw_art_url.startswith('https://') else ''

    # Truncate free-text fields to prevent oversized DB writes
    song_title = str(data.get('songTitle', '') or '')[:200]
    artist_val = str(data.get('artist', '') or '')[:100]

    sb_upsert('playback_state', {
        'google_sub': sub,
        'song_id':    song_id[:100],
        'song_title': song_title,
        'artist':     artist_val,
        'art_url':    art_url,
        'progress':   progress,
        'device':     device,
        'updated_at': datetime.utcnow().isoformat(),
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
            'success':   True,
            'songId':    r.get('song_id'),
            'songTitle': r.get('song_title'),
            'artist':    r.get('artist'),
            'artUrl':    r.get('art_url'),
            'progress':  r.get('progress'),
            'device':    r.get('device'),
            'updatedAt': r.get('updated_at'),
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
        'pairing_code':  code,
        'tv_session_id': session_id,
        'expires_at':    expiry,
    }, on_conflict='pairing_code')
    return jsonify({'code': code, 'sessionId': session_id, 'expiresIn': 300})

@app.route('/api/auth/tv-poll')
@limiter.limit("60 per minute")
def poll_tv_pairing():
    code    = request.args.get('code', '').strip().upper()
    now_str = datetime.utcnow().isoformat()
    if not code: return jsonify({'status': 'pending'}), 400

    url = f"{SUPABASE_URL}/rest/v1/tv_pairing?pairing_code=eq.{quote(code, safe='')}&expires_at=gt.{quote(now_str, safe='')}"
    try:
        r    = requests.get(url, headers=_sb_headers(), timeout=10)
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        rows = []

    if not rows: return jsonify({'status': 'expired'})
    row = rows[0]
    if row.get('google_sub'):
        user_rows = sb_select('users', {'google_sub': row['google_sub']})
        sb_delete('tv_pairing', {'pairing_code': code})
        if user_rows:
            user = user_rows[0]
            return jsonify({'status': 'authorized', 'user': {
                'sub':     user['google_sub'],
                'name':    user['name'],
                'email':   user['email'],
                'picture': user['picture'],
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

    url = f"{SUPABASE_URL}/rest/v1/tv_pairing?pairing_code=eq.{quote(code, safe='')}&expires_at=gt.{quote(now_str, safe='')}"
    try:
        r    = requests.get(url, headers=_sb_headers(), timeout=10)
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        rows = []

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

    # Salted PBKDF2 — plain SHA256 of a 4-6 digit PIN is trivially brute-forced
    # Salt = user's google_sub (unique per user, no DB column needed)
    h_input = hashlib.pbkdf2_hmac(
        'sha256', pin.encode('utf-8'), sub.encode('utf-8'), iterations=300_000
    ).hex()
    rows     = sb_select('users', {'google_sub': sub}, columns='ghost_pin_hash')
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
    # FIX 16: removed ?key= query param fallback — it leaks in CDN/proxy access logs
    secret = request.headers.get('X-Admin-Key', '')
    if not secret or not hmac.compare_digest(secret, ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    rows = sb_select('users', columns='name,email,picture,created_at')
    return jsonify({'users': rows, 'total': len(rows)})


# ═══════════════════════════════════════════════════════════════════════════════
# ARTWORK PROXY  — /api/artwork?url=...
# FIX BUG T6: Saavn/iTunes CDN may block direct browser requests with CORS errors.
# Frontend loads artwork through this proxy to guarantee display.
# ═══════════════════════════════════════════════════════════════════════════════
_ARTWORK_ALLOWED_DOMAINS = [
    'saavncdn.com', 'cf.saavncdn.com', 'c.saavncdn.com', 'aac.saavncdn.com',
    'static.saavncdn.com', 'h.saavncdn.com',
    'is1-ssl.mzstatic.com', 'is2-ssl.mzstatic.com', 'is3-ssl.mzstatic.com',
    'is4-ssl.mzstatic.com', 'is5-ssl.mzstatic.com',
    'i.scdn.co', 'img.youtube.com', 'i.ytimg.com',
    'cf-media.sndcdn.com', 'i1.sndcdn.com', 'i2.sndcdn.com',
]

@app.route('/api/artwork')
@limiter.limit("300 per minute")
def artwork_proxy():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Missing url'}), 400
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return jsonify({'error': 'Invalid scheme'}), 400
        domain = parsed.netloc.lower().split(':')[0]
        allowed = any(domain == d or domain.endswith('.' + d) for d in _ARTWORK_ALLOWED_DOMAINS)
        if not allowed:
            return jsonify({'error': 'Domain not allowed'}), 403
    except Exception:
        return jsonify({'error': 'Invalid URL'}), 400
    try:
        r = requests.get(url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'}, stream=True)
        if not r.ok:
            return jsonify({'error': f'Upstream {r.status_code}'}), 502
        content_type = r.headers.get('Content-Type', 'image/jpeg')
        def generate():
            try:
                for chunk in r.iter_content(chunk_size=32768):
                    if chunk: yield chunk
            finally:
                r.close()
        return Response(stream_with_context(generate()), status=200,
                        content_type=content_type,
                        headers={
                            'Access-Control-Allow-Origin': '*',
                            'Cache-Control': 'public, max-age=86400',
                        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/health')
def health():
    return jsonify({
        'status':  'ok',
        'sources': ['saavn', 'jiosavan', 'ytmusic', 'piped', 'invidious', 'soundcloud', 'youtube'],
        'auth':    'google-oauth',
        'db':      'supabase',
        'version': '2.1',
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
