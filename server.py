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
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from urllib.parse import urlparse, quote, urlencode
from difflib import SequenceMatcher
from collections import defaultdict, OrderedDict
from typing import Optional, Dict, Any, List, Tuple
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

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

    def set(self, key: str, val: Any) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
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
    return f"art:{normalize(title)}:{normalize(artist)}"


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

# backward-compat aliases used by existing code paths
_meta_cache     = {}   # kept for legacy _cache_get/_cache_set
_ytdlp_cache    = {}
META_CACHE_TTL  = 600
YTDLP_CACHE_TTL = 240

def _cache_get(key, store=None):
    store = store if store is not None else _meta_cache
    entry = store.get(key)
    if not entry: return None
    ts, data = entry
    ttl = YTDLP_CACHE_TTL if store is _ytdlp_cache else META_CACHE_TTL
    if time.time() - ts > ttl:
        del store[key]
        return None
    return data

def _cache_set(key, data, store=None):
    store = store if store is not None else _meta_cache
    store[key] = (time.time(), data)
    if len(store) > 300:
        oldest = min(store, key=lambda k: store[k][0])
        del store[oldest]


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
_CACHE_MIN_CONFIDENCE = 0.60   # below this → never cache in L2 (poison prevention)

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
        # Promote to L1
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
            'confidence': round(confidence, 4),
            'cached_at':  int(time.time()),
        }
        sb_upsert('song_cache', payload, on_conflict='cache_key')
        # Also write to L1
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
# Only results with confidence ≥ 0.60 enter L2 cache.
# ═══════════════════════════════════════════════════════════════════════════════

_REMIX_INDICATORS = [
    'remix', 'slowed', 'reverb', 'lofi', 'lo-fi', 'mashup', 'cover',
    'karaoke', 'instrumental', 'dj ', 'nightcore', 'pitched', 'sped up',
    'chopped', 'screwed', 'flip', 'bootleg', 'edit', 'extended mix',
    'club mix', 'dance mix', 'radio edit', 'version', 'tribute',
    'live', 'acoustic', 'unplugged', 'concert', 'tour', 'performance',
    'live at', 'live from', 'live version', 'live session', 'stripped',
    '8d audio', 'bass boosted', 'speed up', 'slowed down',
]

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
    return any(ind in t for ind in _REMIX_INDICATORS)

def _is_live_version(title: str) -> bool:
    t = title.lower()
    return any(ind in t for ind in _LIVE_INDICATORS)

def _is_slowed_reverb(title: str) -> bool:
    t = title.lower()
    return any(ind in t for ind in _SLOWED_INDICATORS)

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
    qt = _normalize_text(query_title)
    qa = _normalize_text(query_artist)
    rt = _normalize_text(result_title)
    ra = _normalize_text(result_artist)

    # ── Title similarity (40% weight) ─────────────────────────────────────────
    t_seq  = _seq_ratio(qt, rt)
    t_word = _word_overlap(qt, rt)
    t_sim  = (t_seq * 0.6 + t_word * 0.4)

    # prefix boost: result starts with query
    if rt.startswith(qt) or qt.startswith(rt):
        t_sim = min(1.0, t_sim + 0.15)

    # ── Artist similarity (25% weight) ────────────────────────────────────────
    a_sim = 0.0
    if qa and ra:
        a_seq  = _seq_ratio(qa, ra)
        a_word = _word_overlap(qa, ra)
        a_sim  = a_seq * 0.5 + a_word * 0.5
        # partial artist: first token match
        qa_first = qa.split()[0] if qa.split() else ''
        ra_first = ra.split()[0] if ra.split() else ''
        if qa_first and ra_first and _seq_ratio(qa_first, ra_first) >= 0.80:
            a_sim = min(1.0, a_sim + 0.10)
    elif not qa:
        a_sim = 0.5  # neutral when query has no artist

    # ── Duration similarity (15% weight) ──────────────────────────────────────
    d_sim = 0.5  # neutral default
    if query_duration_s > 0 and result_duration_s > 0:
        delta = abs(query_duration_s - result_duration_s)
        # Exponential decay: ±5s → ~0.85, ±15s → ~0.60, ±30s → ~0.37, ±60s → 0.13
        d_sim = max(0.0, 1.0 - (delta / 45.0) ** 0.8)

    # ── Source confidence (10% weight) ────────────────────────────────────────
    s_conf = _SOURCE_CONFIDENCE.get(source, 0.60)

    # ── Weighted sum (weights sum to 1.0) ─────────────────────────────────────
    conf = (t_sim * 0.45) + (a_sim * 0.30) + (d_sim * 0.15) + (s_conf * 0.10)

    # ── Bonus: exact title ────────────────────────────────────────────────────
    if qt and rt and qt == rt:
        conf = min(1.0, conf + 0.15)

    # ── Remix / slowed / cover / live penalties ───────────────────────────────
    query_is_remix  = _is_remix_or_cover(query_title)
    result_is_remix = _is_remix_or_cover(result_title)
    query_is_live   = _is_live_version(query_title)
    result_is_live  = _is_live_version(result_title)
    query_is_slowed = _is_slowed_reverb(query_title)
    result_is_slowed = _is_slowed_reverb(result_title)

    if result_is_slowed and not query_is_slowed:
        conf -= 0.30   # harshest penalty: slowed/reverb never wanted accidentally
    elif result_is_live and not query_is_live:
        conf -= 0.20   # live versions are unexpected unless requested
    elif result_is_remix and not query_is_remix:
        conf -= 0.22   # penalize unexpected remixes heavily
    elif not result_is_remix and query_is_remix:
        conf -= 0.10   # slightly penalize if user wanted remix but got original

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


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT HELPERS  (all original + enhancements)
# ═══════════════════════════════════════════════════════════════════════════════
def clean_query(text):
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(\s*(OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?|Hindi|English|Version|Remix|Cover|HD|HQ|Original|Soundtrack|Remastered|Extended|Radio\s*Edit)\s*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*[-–]\s*(official|audio|video|lyrics|full\s*song|hd|hq|remastered).*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
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
    ('ou', 'u'), ('ue', 'u'), ('hi', 'he'), ('he', 'hi'),
    ('ho', 'hu'), ('hu', 'ho'), ('ki', 'ke'), ('ke', 'ki'),
    ('ko', 'ku'), ('na', 'nah'), ('nah', 'na'),
    ('hai', 'he'), ('hain', 'he'), ('he', 'hai'),
    ('pyar', 'pyaar'), ('pyaar', 'pyar'),
    ('dil', 'dill'), ('dill', 'dil'),
    ('ishq', 'ishk'), ('ishk', 'ishq'),
]

def _hindi_translit_normalize(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    for src, dst in _HINDI_TRANSLIT:
        t = re.sub(src, dst, t)
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
    # Exact match bonus reduced 3.0→2.0 so duration_bonus and compute_confidence
    # can tiebreak between multiple songs with identical titles (e.g. "Ram Jane"
    # by 5 different artists). 3.0 was too dominant — compute_confidence had
    # zero influence when exact match dominated at 65% weight.
    if q == t: return 2.0
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

def _duration_bonus(dur_s: int) -> float:
    """
    Bonus/penalty based on track duration.
    Rationale: songs < 2:30 are almost always snippets, short covers, or
    low-quality uploads. Real songs are 3:00-7:00. This tiebreaks between
    e.g. "Luv Latter" (1:59 snippet) vs "Luv Letter" (4:31 real song)
    when both match the typo query equally.

    < 2:30  → -1.5  (almost certainly not the intended song)
    2:30-3:00 → -0.3  (slightly short)
    3:00-7:00 → +0.2  (normal song length)
    > 7:00  →  0.0  (long but neutral)
    unknown →  0.0  (no duration data — neutral)
    """
    if dur_s <= 0:   return 0.0
    if dur_s < 150:  return -1.5
    if dur_s < 180:  return -0.3
    if dur_s <= 420: return 0.2
    return 0.0

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

_QUARANTINE_SECS       = 180   # 3 minutes quarantine for dead sources
_REP_RECOVERY_SECS     = 90    # probe quarantined source after 90s
_REP_FAIL_COST         = 8
_REP_MIN_FOR_TRAFFIC   = 20    # below this → quarantine
_ADAPTIVE_TIMEOUT_BASE = 5.0   # base timeout seconds
_ADAPTIVE_TIMEOUT_MAX  = 10.0

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
_reactive_heal_cooldown = {}
_REACTIVE_COOLDOWN_S    = 120

def _maybe_reactive_heal(source_type: str):
    now  = time.time()
    last = _reactive_heal_cooldown.get(source_type, 0)
    if now - last < _REACTIVE_COOLDOWN_S: return
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

def _master_heal_loop():
    time.sleep(30)
    while True:
        try:
            log.info('[SelfHeal] Starting full heal cycle...')
            futures = [
                _executor.submit(_discover_mirrors),
                _executor.submit(_verify_existing_mirrors),
                _executor.submit(_heal_piped),
                _executor.submit(_heal_invidious),
                _executor.submit(_refresh_soundcloud_client_id),
            ]
            for f in as_completed(futures, timeout=120):
                try: f.result()
                except Exception as e: log.warning(f'[SelfHeal] Error: {e}')
            with _mirror_lock:    sm = len(SAAVN_MIRRORS)
            with _piped_lock:     pi = len(PIPED_INSTANCES)
            with _invidious_lock: iv = len(INVIDIOUS_INSTANCES)
            log.info(f'[SelfHeal] ✓ Done — Saavn:{sm} Piped:{pi} Invidious:{iv}')
        except Exception as e:
            log.error(f'[SelfHeal] Master loop error: {e}')
        time.sleep(7200)

threading.Thread(target=_master_heal_loop, daemon=True).start()
log.info('[SelfHeal] Master heal loop started')


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
            time.sleep(600)
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
def _fetch_saavn_search_mirror(mirror, search_term):
    if not _mirror_ok(mirror): return []
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0 = time.time()
            r  = requests.get(f'{mirror}{endpoint}',
                              params={'query': search_term, 'q': search_term, 'limit': 20},
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

def _fetch_saavn_search_parallel(search_term):
    """
    Race top-N mirrors. Return as soon as the first winner responds.
    Cancel remaining futures immediately (bandwidth + CPU saving).
    """
    mirrors = _best_mirrors(n=6)
    futures = {_executor.submit(_fetch_saavn_search_mirror, m, search_term): m for m in mirrors}
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
        if dur_s > 1080: continue
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

    # upgrade artwork
    if itunes_song.get('artworkUrl100'):
        itunes_song['artworkUrl100'] = itunes_song['artworkUrl100'].replace('100x100', '600x600')
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
                        # Hard skip: result is remix but user didn't ask for remix
                        if (_is_remix_or_cover(song_title) and
                                not _is_remix_or_cover(title) and
                                not _is_live_version(title) and
                                not _is_slowed_reverb(title)):
                            continue
                        song_dur = int(song.get('duration', 0) or 0)
                        conf = compute_confidence(
                            title, artist, song_title, song_artist,
                            query_duration_s=itunes_dur,
                            result_duration_s=song_dur,
                            source='saavn',
                        )
                        if conf > best_conf:
                            best_conf = conf; best = song

                    if not best or best_conf < 0.65: continue

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
        conf = compute_confidence(title, artist, item.get('title', ''),
                                  item.get('artist', ''), source='ytmusic')
        if conf > best_conf:
            best_conf = conf; best = item

    if not best or best_conf < 0.25: return None
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
                    for entry in entries:
                        if not entry: continue
                        yt_title  = entry.get('title', '')
                        yt_artist = entry.get('uploader', '') or entry.get('artist', '')
                        conf = compute_confidence(title, artist, yt_title, yt_artist, source='youtube')
                        if 'music.youtube' in (entry.get('webpage_url') or ''):
                            conf = min(1.0, conf + 0.05)
                        if conf > best_conf:
                            best_conf = conf; best_result = entry
                    if best_conf >= 0.75: break
                except Exception:
                    continue

            if not best_result: return None
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
            for entry in info['entries']:
                if not entry or entry.get('duration', 0) < 60: continue
                conf = compute_confidence(title, artist, entry.get('title', ''),
                                          entry.get('uploader', ''), source='soundcloud')
                if conf > best_conf: best_conf = conf; best = entry
            if not best or best_conf < 0.20: return None
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
def _fetch_saavn_by_id(song_id: str) -> Optional[dict]:
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
def fetch_from_mirror(mirror, query, min_score=0.4):
    if not _mirror_ok(mirror): return None
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0 = time.time()
            r  = requests.get(f'{mirror}{endpoint}',
                              params={'query': query, 'q': query, 'limit': 10},
                              timeout=_health.adaptive_timeout(mirror),
                              headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data    = r.json()
            results = (data.get('data', {}).get('results') or data.get('results') or
                       data.get('songs', {}).get('results') or [])

            best_song = None; best_score = -1; best_dur = float('inf')
            q_normalized = normalize(query)

            for song in results:
                song_title  = song.get('name') or song.get('title', '')
                song_artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                if not has_word_match(query, song_title): continue
                score = title_score(query, song_title, song_artist)
                dur   = int(song.get('duration', 999) or 999)
                if dur > 600: score -= 0.6
                if dur > 900: score -= 1.0
                # remix penalty
                if _is_remix_or_cover(song_title): score -= 0.8
                song_year = int(song.get('year') or 0)
                if song_year >= 2010:               score += 0.15
                elif song_year > 0 and song_year < 2000: score -= 0.25
                if song_artist:
                    artist_norm  = normalize(song_artist)
                    artist_words = [w for w in artist_norm.split() if len(w) >= 3]
                    query_words  = [w for w in q_normalized.split() if len(w) >= 3]
                    matching_aw  = sum(1 for aw in artist_words
                                       if any(fuzzy_word_match(aw, qw) >= 0.80 for qw in query_words))
                    if artist_words and matching_aw >= 1:
                        score += 0.5 * (matching_aw / max(len(artist_words), 1))
                if score > best_score or (score == best_score and dur < best_dur):
                    best_score = score; best_song = song; best_dur = dur

            if not best_song or best_score < min_score: continue
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
                'image': pick_image(best_song), 'score': round(best_score, 3),
                'source': 'saavn', '_raw_urls': raw_urls,
            }
            if result_data['image']:
                _store_artwork(result_data['title'], result_data['artist'], result_data['image'], 1)
            return result_data
        except Exception:
            _mirror_failed(mirror)
            continue
    return None


def fetch_saavn_parallel(query):
    # Check L1 first
    l1_key = f"saavn_q:{normalize(query)}"
    cached = _l1_saavn.get(l1_key)
    if cached: return cached

    threshold = dynamic_min_score(query)
    mirrors   = _best_mirrors(n=8)
    futures   = {_executor.submit(fetch_from_mirror, m, query, threshold): m for m in mirrors}
    all_results = []
    try:
        for future in as_completed(futures, timeout=6):
            try:
                result = future.result()
                if result: all_results.append(result)
            except Exception: pass
    except Exception: pass

    if not all_results: return None

    # Blend mirror score with confidence engine for better ranking
    all_results.sort(
        key=lambda r: (
            r.get('score', 0) * 0.5 +
            compute_confidence(
                query, '',
                r.get('title', ''), r.get('artist', ''),
                source=r.get('source', 'saavn'),
            ) * 0.5 +
            (0.05 if '320' in str(r.get('quality', '')) else 0)
        ),
        reverse=True,
    )
    best = all_results[0]
    _l1_saavn.set(l1_key, best)
    log.info(f"[Parallel] ✓ '{best['title']}' score={best['score']} q={best['quality']}")
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
            for item in results[:5]:
                if item.get('type') != 'stream': continue
                if not has_word_match(query, item.get('title', '')): continue
                conf = compute_confidence(query, artist, item.get('title', ''),
                                          item.get('uploaderName', ''), source='piped')
                if conf > best_conf: best_conf = conf; best = item

            if not best or best_conf < 0.30: continue
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
            for item in results[:5]:
                if not has_word_match(query, item.get('title', '')): continue
                conf = compute_confidence(query, artist, item.get('title', ''),
                                          item.get('author', ''), source='invidious')
                if conf > best_conf: best_conf = conf; best = item

            if not best or best_conf < 0.30: continue
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
        for song in songs[:5]:
            song_title  = song.get('song') or song.get('title') or song.get('name', '')
            song_artist = song.get('primary_artists') or song.get('singers') or song.get('artist', '')
            conf = compute_confidence(title, artist, song_title, song_artist, source='jiosavan')
            if conf > best_conf: best_conf = conf; best = song

        if not best or best_conf < 0.40: return None

        media_url = (best.get('media_url') or best.get('encrypted_media_url') or
                     best.get('download_url') or '')
        if not media_url: return None

        image = best.get('image', '')
        if image: image = image.replace('150x150', '500x500').replace('50x50', '500x500')

        result = {
            'url':    media_url, 'quality': '320kbps',
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
#  Only conf ≥ 0.60 results get written to L2 (Supabase).
#  Background resolution for next-likely songs via prefetch.
# ═══════════════════════════════════════════════════════════════════════════════
@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    song_id = request.args.get('id', '').strip()
    title   = request.args.get('title', '').strip()
    artist  = request.args.get('artist', '').strip()

    if not song_id and not title:
        return jsonify({'error': 'Missing id or title'}), 400

    audio_url  = None
    quality    = 'unknown'
    source     = 'unknown'
    confidence = 0.0

    _play_ck = f"play:{song_id or normalize(title)}:{normalize(artist)}"

    # ── 1. L1 cache (< 1ms) ──────────────────────────────────────────────────
    l1_hit = _l1_saavn.get(_play_ck)
    if l1_hit and l1_hit.get('url'):
        audio_url  = l1_hit['url']
        quality    = l1_hit.get('quality', 'unknown')
        source     = l1_hit.get('source', 'unknown')
        confidence = float(l1_hit.get('confidence', 1.0))
        if not title:  title  = l1_hit.get('title', '')
        if not artist: artist = l1_hit.get('artist', '')
        log.info(f"[Cache:L1] HIT play key={_play_ck}")

    # ── 2. L2 Supabase cache ──────────────────────────────────────────────────
    if not audio_url:
        _play_cached = _supabase_cache_get(_play_ck)
        if _play_cached and _play_cached.get('url'):
            audio_url  = _play_cached['url']
            quality    = _play_cached.get('quality', 'unknown')
            source     = _play_cached.get('source', 'unknown')
            confidence = float(_play_cached.get('confidence', 1.0))
            if not title:  title  = _play_cached.get('title', '')
            if not artist: artist = _play_cached.get('artist', '')
            log.info(f"[Cache:L2] HIT play key={_play_ck}")
            # Refresh L1
            _l1_saavn.set(_play_ck, _play_cached)

    # ── 3. Saavn ID path: ONLY ID-based fetch ─────────────────────────────────
    if not audio_url and song_id:
        result = _fetch_saavn_by_id(song_id)
        if result and result.get('url'):
            audio_url  = result['url']
            quality    = result.get('quality', 'unknown')
            source     = 'saavn'
            confidence = 0.95   # ID-based = very high confidence
            if not title:  title  = result.get('title', '')
            if not artist: artist = result.get('artist', '')
            log.info(f"[Play] ✓ Saavn ID={song_id} q={quality}")
        else:
            # ID fetch failed — only use title if provided
            if title:
                for query_var in build_query_variants(title, artist, ''):
                    result = fetch_saavn_parallel(query_var)
                    if result and result.get('url'):
                        audio_url  = result['url']
                        quality    = result.get('quality', 'unknown')
                        source     = 'saavn'
                        confidence = result.get('score', 0.5)
                        log.info(f"[Play] ✓ Saavn title fallback '{result['title']}' q={quality}")
                        break

    # ── 4. Title-only path ────────────────────────────────────────────────────
    elif not audio_url and title:
        for query_var in build_query_variants(title, artist, ''):
            result = fetch_saavn_parallel(query_var)
            if result and result.get('url'):
                audio_url  = result['url']
                quality    = result.get('quality', 'unknown')
                source     = 'saavn'
                confidence = result.get('score', 0.5)
                log.info(f"[Play] ✓ Saavn title='{result['title']}' q={quality}")
                break

    # ── 5. SMART PARALLEL FALLBACKS ──────────────────────────────────────────
    # Progressive strategy: launch fastest sources first, cancel on win
    if not audio_url and title:
        log.info(f"[Play] Saavn miss → smart parallel fallbacks: '{title}'")

        # Tier 1: YTMusic + JioSavan (fastest, highest quality)
        t1_futures = {
            _executor.submit(fetch_from_ytmusic, title, artist): 'ytmusic',
            _executor.submit(fetch_from_jiosavan, title, artist): 'jiosavan',
        }
        for future in as_completed(t1_futures, timeout=8):
            try:
                res = future.result()
                if res and res.get('url'):
                    audio_url  = res['url']
                    quality    = res.get('quality', 'unknown')
                    source     = res.get('source', t1_futures[future])
                    confidence = float(res.get('_confidence', 0.6))
                    if not title:  title  = res.get('title', title)
                    if not artist: artist = res.get('artist', artist)
                    # Prefer cached Saavn/iTunes artwork over source artwork
                    _best_fallback_art = _get_artwork(title, artist) or res.get('image', '')
                    log.info(f"[Play] ✓ Tier1:{source} '{res.get('title')}' q={quality}")
                    for f in t1_futures: f.cancel()
                    break
            except Exception:
                pass

        # Tier 2: yt-dlp + SoundCloud (medium speed)
        if not audio_url:
            t2_futures = {
                _executor.submit(fetch_from_ytdlp, title, artist): 'youtube',
                _executor.submit(fetch_from_soundcloud, title, artist): 'soundcloud',
            }
            for future in as_completed(t2_futures, timeout=15):
                try:
                    res = future.result()
                    if res and res.get('url'):
                        audio_url  = res['url']
                        quality    = res.get('quality', 'unknown')
                        source     = res.get('source', t2_futures[future])
                        confidence = float(res.get('_confidence', 0.5))
                        log.info(f"[Play] ✓ Tier2:{source} '{res.get('title')}' q={quality}")
                        for f in t2_futures: f.cancel()
                        break
                except Exception:
                    pass

        # Tier 3: Piped + Invidious (slowest, last resort before broad)
        if not audio_url:
            t3_futures = {
                _executor.submit(fetch_from_piped, title, title=title, artist=artist): 'piped',
                _executor.submit(fetch_from_invidious, title, title=title, artist=artist): 'invidious',
            }
            for future in as_completed(t3_futures, timeout=15):
                try:
                    res = future.result()
                    if res and res.get('url'):
                        audio_url  = res['url']
                        quality    = res.get('quality', 'unknown')
                        source     = res.get('source', t3_futures[future])
                        confidence = float(res.get('_confidence', 0.45))
                        log.info(f"[Play] ✓ Tier3:{source} '{res.get('title')}' q={quality}")
                        for f in t3_futures: f.cancel()
                        break
                except Exception:
                    pass

    # ── 6. Broad YouTube last-resort ─────────────────────────────────────────
    if not audio_url and title:
        for broad_query in [title, title.split()[0] if title.split() else title]:
            broad = fetch_from_ytdlp(broad_query, '')
            if broad and broad.get('url'):
                audio_url  = broad['url']
                quality    = broad.get('quality', 'unknown')
                source     = 'youtube-broad'
                confidence = 0.30
                break

    if not audio_url:
        log.warning(f"[Play] ✗ ALL sources failed id={song_id} title='{title}'")
        return jsonify({'error': 'No audio source found'}), 404

    # ── 7. Async cache writes (only if confidence passes threshold) ───────────
    # Resolve best available artwork — never write empty image when artwork exists
    _best_art = _get_artwork(title, artist) if title else ''
    _l1_saavn.set(_play_ck, {
        'url': audio_url, 'quality': quality, 'source': source,
        'title': title, 'artist': artist, 'confidence': confidence,
        'image': _best_art,
    })
    _executor.submit(_supabase_cache_set, _play_ck, {
        'url': audio_url, 'quality': quality, 'source': source,
        'title': title, 'artist': artist, 'image': _best_art,
    }, confidence)

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
                                    timeout=60, allow_redirects=True)
        excluded     = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges':  'bytes',
            'Cache-Control':  'no-store',
            'X-Audio-Quality': quality,
            'X-Audio-Source':  source,
            'X-Confidence':    str(round(confidence, 3)),
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
# ═══════════════════════════════════════════════════════════════════════════════
# /api/songs  — FIXED: Relevance-ranked, cache-poison-proof, mismatch-immune
# ═══════════════════════════════════════════════════════════════════════════════
#
# ROOT CAUSES OF MISMATCH (ALL FIXED HERE):
#
# BUG-1  No relevance sort after merge
#        merged = itunes + saavn was returned in raw API order.
#        iTunes popularity order ≠ query relevance order.
#        "Choliye" → "Coolie Disco" because Saavn's API returned it first.
#        FIX: final merged list sorted by hybrid score (title_score + confidence).
#
# BUG-2  Saavn direct results had zero relevance filtering
#        _normalize_saavn_songs() preserved raw Saavn API order.
#        FIX: every saavn result scored against query; below 0.30 discarded.
#
# BUG-3  iTunes resolved results kept iTunes popularity order
#        After _resolve_itunes_to_saavn(), results weren't re-ranked by query.
#        FIX: resolved itunes list sorted by title_score before merging.
#
# BUG-4  Wrong results got cached and poisoned next 600s of responses
#        If top result had low confidence, it still got cached.
#        FIX: cache only when top result confidence >= 0.42 for real searches.
#
# ═══════════════════════════════════════════════════════════════════════════════

def _search_score(query: str, song: dict) -> float:
    """
    Hybrid relevance score:
      title_score  (exact/prefix/word match, 0.65 weight)
      compute_confidence (remix/live penalties, artist sim, 0.35 weight)
      _duration_bonus (penalizes snippets < 2:30, tiebreaks equal titles)
    """
    title  = song.get('trackName') or song.get('title', '')
    artist = (song.get('artistName') or song.get('artist', '')
              or song.get('_resolvedArtist', ''))
    dur_ms = int(song.get('trackTimeMillis', 0) or 0)
    dur_s  = dur_ms // 1000

    ts = title_score(query, title, artist)
    cc = compute_confidence(
        query_title=query,
        query_artist='',
        result_title=title,
        result_artist=artist,
        query_duration_s=0,
        result_duration_s=dur_s,
        source=song.get('_source', 'saavn'),
    )
    db = _duration_bonus(dur_s)
    return ts * 0.65 + cc * 2.0 * 0.35 + db


def _filter_and_rank(query: str, songs: list, min_conf: float = 0.0) -> list:
    """
    Score every song against query, drop below min_conf, return sorted best-first.
    min_conf=0.0 means no filtering (used for home/90s/generic queries).
    """
    scored = [(s, _search_score(query, s)) for s in songs]
    if min_conf > 0:
        scored = [(s, sc) for s, sc in scored if sc >= min_conf]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in scored]


@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q   = request.args.get('q', '').strip()
    era = request.args.get('era', '').strip()

    # Distinguish real search from home/browse mode
    is_real_search = bool(q) and q.lower() not in ('top bollywood songs',)
    if not q:
        q = 'top bollywood songs'

    is_90s      = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    cache_key = f"songs:{search_term.lower()}"

    # ── L1 cache ──────────────────────────────────────────────────────────────
    cached = _l1_meta.get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, '_cached': True})

    legacy = _cache_get(cache_key)
    if legacy is not None:
        return jsonify({'results': legacy, '_cached': True})

    itunes_results: list = []
    saavn_results:  list = []

    # ── iTunes fetch + Saavn resolve ──────────────────────────────────────────
    def fetch_itunes():
        nonlocal itunes_results
        try:
            r = requests.get(
                'https://itunes.apple.com/search',
                params={'term': search_term, 'media': 'music',
                        'entity': 'song', 'limit': 50, 'country': 'IN'},
                timeout=12,
            )
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

            resolve_futures = {_executor.submit(_resolve_itunes_to_saavn, s): s
                               for s in candidates}
            resolved = []
            try:
                for fut in as_completed(resolve_futures, timeout=10):
                    try:
                        res = fut.result()
                        if res: resolved.append(res)
                    except Exception: pass
            except Exception: pass

            # BUG-3 FIX: re-rank resolved results by query relevance,
            # not by iTunes popularity. Only for real searches (not 90s/home).
            if is_real_search and not is_90s:
                resolved = _filter_and_rank(search_term, resolved, min_conf=0.0)

            itunes_results = resolved[:30]
        except Exception:
            pass

    # ── Saavn direct fetch ────────────────────────────────────────────────────
    def fetch_saavn():
        nonlocal saavn_results
        try:
            raw = _fetch_saavn_search_parallel(search_term)
            if not raw:
                return
            normalized = _normalize_saavn_songs(raw)

            if is_90s:
                filtered = [s for s in normalized if
                            1990 <= _safe_year(s.get('releaseDate')) <= 1999]
                normalized = filtered if len(filtered) >= 5 else normalized
                random.shuffle(normalized)
                saavn_results = normalized[:30]
            else:
                if is_real_search:
                    # BUG-2 FIX: score + filter Saavn direct results.
                    # min_conf=0.30 removes completely unrelated results
                    # while keeping partial/translit matches.
                    normalized = _filter_and_rank(search_term, normalized, min_conf=0.30)
                saavn_results = normalized[:30]
        except Exception:
            pass

    t1 = threading.Thread(target=fetch_itunes)
    t2 = threading.Thread(target=fetch_saavn)
    t1.start(); t2.start()
    t1.join(timeout=15)
    t2.join(timeout=4)

    # ── Merge + dedup ─────────────────────────────────────────────────────────
    merged = list(itunes_results)
    for s in saavn_results:
        if not any(is_likely_duplicate(s, e) for e in merged):
            merged.append(s)

    if not merged:
        return jsonify({'results': [], 'error': 'No results found'})

    # BUG-1 FIX: sort the full merged list by relevance for real searches.
    # 90s/home queries keep shuffle/API order (intentional variety).
    if is_real_search and not is_90s:
        merged = _filter_and_rank(search_term, merged, min_conf=0.0)

    # BUG-4 FIX: only cache if top result is actually relevant.
    # Prevents wrong results from poisoning cache for 600s.
    should_cache = True
    if is_real_search and not is_90s and merged:
        top_score = _search_score(search_term, merged[0])
        if top_score < 0.42:
            log.warning(
                f"[Search] Skipping cache — low relevance top result "
                f"query='{search_term}' top='{merged[0].get('trackName', '')}' "
                f"score={top_score:.3f}"
            )
            should_cache = False

    if should_cache:
        _l1_meta.set(cache_key, merged)
        _cache_set(cache_key, merged)

    return jsonify({'results': merged})



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
    q           = request.args.get('q', '').strip()
    artist      = request.args.get('artist', '').strip()
    fallback    = request.args.get('fallback', '').strip()
    token       = request.args.get('token', '').strip()
    low_quality = request.args.get('low_quality', 'false').lower() == 'true'
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})

    _ck = f"saavn:{normalize(q)}:{normalize(artist)}"

    # L1 check
    if not low_quality:
        l1_hit = _l1_saavn.get(_ck)
        if l1_hit:
            log.info(f"[Cache:L1] HIT saavn: '{q}'")
            return jsonify({'success': True, 'token': token, **l1_hit})

    # L2 check
    _cached = _supabase_cache_get(_ck)
    if _cached and not low_quality:
        log.info(f"[Cache:L2] HIT saavn: '{q}'")
        _l1_saavn.set(_ck, _cached)
        return jsonify({'success': True, 'token': token, **_cached})

    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query)
        if result:
            if low_quality:
                low_url, low_q = _pick_low_quality(result.get('_raw_urls', []))
                if low_url: result['url'] = low_url; result['quality'] = low_q
            conf = result.get('score', 0.5)
            _l1_saavn.set(_ck, result)
            _executor.submit(_supabase_cache_set, _ck, result, conf)
            return jsonify({'success': True, 'token': token, **result})

    ytm = fetch_from_ytmusic(q, artist)
    if ytm and ytm.get('url'):
        conf = float(ytm.get('_confidence', 0.6))
        _l1_saavn.set(_ck, ytm)
        _executor.submit(_supabase_cache_set, _ck, ytm, conf)
        return jsonify({'success': True, 'token': token, **ytm})

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        conf = float(yt.get('_confidence', 0.5))
        _l1_saavn.set(_ck, yt)
        _executor.submit(_supabase_cache_set, _ck, yt, conf)
        return jsonify({'success': True, 'token': token, **yt})

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
        conf = float(sc.get('_confidence', 0.45))
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
    q        = request.args.get('q', '').strip()
    artist   = request.args.get('artist', '').strip()
    fallback = request.args.get('fallback', '').strip()
    token    = request.args.get('token', '').strip()
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})

    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query)
        if result:
            return jsonify({
                'success': True, 'token': token,
                'url':     f"/api/stream?url={quote(result['url'], safe='')}",
                'quality': result['quality'], 'title': result['title'],
                'artist':  result['artist'], 'image': result.get('image', ''),
                'source':  'saavn',
            })

    ytm = fetch_from_ytmusic(q, artist)
    if ytm and ytm.get('url'):
        return jsonify({'success': True, 'token': token,
                        'url':    f"/api/stream?url={quote(ytm['url'], safe='')}",
                        'quality': ytm['quality'], 'title': ytm['title'],
                        'artist':  ytm['artist'], 'image': ytm.get('image', ''),
                        'source':  'ytmusic'})

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        return jsonify({'success': True, 'token': token,
                        'url':    f"/api/stream?url={quote(yt['url'], safe='')}",
                        'quality': yt['quality'], 'title': yt['title'],
                        'artist':  yt['artist'], 'image': yt.get('image', ''),
                        'source':  'youtube'})

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
        return jsonify({'success': True, 'token': token,
                        'url':    f"/api/stream?url={quote(sc['url'], safe='')}",
                        'quality': sc['quality'], 'title': sc['title'],
                        'artist':  sc['artist'], 'image': sc.get('image', ''),
                        'source':  'soundcloud'})

    piped = fetch_from_piped(q, title=q, artist=artist)
    if piped and piped.get('url'):
        return jsonify({'success': True, 'token': token,
                        'url':    f"/api/stream?url={quote(piped['url'], safe='')}",
                        'quality': piped['quality'], 'title': piped['title'],
                        'artist':  piped['artist'], 'image': piped.get('image', ''),
                        'source':  'piped'})

    inv = fetch_from_invidious(q, title=q, artist=artist)
    if inv and inv.get('url'):
        return jsonify({'success': True, 'token': token,
                        'url':    f"/api/stream?url={quote(inv['url'], safe='')}",
                        'quality': inv['quality'], 'title': inv['title'],
                        'artist':  inv['artist'], 'image': inv.get('image', ''),
                        'source':  'invidious'})

    return jsonify({'success': False, 'url': None, 'token': token})


# ═══════════════════════════════════════════════════════════════════════════════
# STREAM PROXY
# ═══════════════════════════════════════════════════════════════════════════════
def _is_allowed_domain(domain):
    for allowed in ALLOWED_STREAM_DOMAINS:
        if domain == allowed or domain.endswith('.' + allowed) or allowed in domain:
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
        upstream     = requests.get(url, headers=req_headers, stream=True, timeout=60, allow_redirects=True)
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
        result = fetch_saavn_parallel(query)
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

    try:
        clean_name = re.sub(r'[/\\?%*:|"<>]', '-', filename_base)
        upstream   = requests.get(stream_url,
                                  headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'},
                                  stream=True, timeout=60, allow_redirects=True)
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

    sb_upsert('playback_state', {
        'google_sub': sub,
        'song_id':    song_id,
        'song_title': data.get('songTitle', ''),
        'artist':     data.get('artist', ''),
        'art_url':    data.get('artUrl', ''),
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

    h_input  = hashlib.sha256(pin.encode('utf-8')).hexdigest()
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
    secret = request.args.get('key', '')
    if not hmac.compare_digest(secret, ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    rows = sb_select('users', columns='name,email,picture,created_at')
    return jsonify({'users': rows, 'total': len(rows)})


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
        'version': '2.0',
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
