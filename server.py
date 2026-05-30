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

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GOOGLE_CLIENT_ID  = os.environ.get('GOOGLE_CLIENT_ID', '')
ADMIN_KEY         = os.environ.get('ADMIN_KEY', '')
SUPABASE_URL      = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY      = os.environ.get('SUPABASE_KEY', '')  # secret key (service_role)

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
_executor  = ThreadPoolExecutor(max_workers=32)
_google_req = google_requests.Request()

# ═══════════════════════════════════════════════════════════════
# SUPABASE HTTP HELPERS
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
        log.warning(f"[Supabase] SELECT {table} error {r.status_code}: {r.text}")
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
        if r.status_code in (200, 204):
            return True
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
        if r.status_code in (200, 204):
            return True
        log.warning(f"[Supabase] DELETE {table} error {r.status_code}: {r.text}")
    except Exception as e:
        log.warning(f"[Supabase] DELETE {table} exception: {e}")
    return False

def init_db():
    log.info('[DB] Supabase ready — tables managed via Supabase SQL editor')

init_db()

# ═══════════════════════════════════════════════════════════════
# JWT HELPERS
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
# SUPABASE SONG CACHE
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
        if source in _VOLATILE_SOURCES:
            try:
                head = requests.head(row['url'], timeout=3, allow_redirects=True,
                                     headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                if head.status_code >= 400 and head.status_code != 403:
                    _executor.submit(sb_delete, 'song_cache', {'cache_key': cache_key})
                    return None
            except Exception:
                pass
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
# SAAVN MIRRORS
# ═══════════════════════════════════════════════════════════════
_BASE_MIRRORS = [
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

# ═══════════════════════════════════════════════════════════════
# SOURCE HEALTH TRACKING
# ═══════════════════════════════════════════════════════════════
_source_health = {}
_health_lock   = threading.Lock()

def _health_record_ok(url: str, elapsed_ms: float = 0):
    with _health_lock:
        h = _source_health.setdefault(url, {'fails': 0, 'last_fail': 0, 'last_ok': 0, 'avg_ms': 0, 'total_hits': 0})
        h['fails']      = max(0, h['fails'] - 1)
        h['last_ok']    = time.time()
        h['total_hits'] = h.get('total_hits', 0) + 1
        if elapsed_ms > 0:
            h['avg_ms'] = (h['avg_ms'] * 0.8 + elapsed_ms * 0.2) if h['avg_ms'] else elapsed_ms

def _health_record_fail(url: str):
    with _health_lock:
        h = _source_health.setdefault(url, {'fails': 0, 'last_fail': 0, 'last_ok': 0, 'avg_ms': 0, 'total_hits': 0})
        h['fails']    += 1
        h['last_fail'] = time.time()

def _health_score(url: str) -> float:
    with _health_lock:
        h = _source_health.get(url, {})
    fails   = h.get('fails', 0)
    last_ok = h.get('last_ok', 0)
    avg_ms  = h.get('avg_ms', 999)
    age_ok  = time.time() - last_ok if last_ok else 9999
    score   = 100.0 - fails * 10 - min(age_ok / 60, 50) - min(avg_ms / 100, 30)
    return score

def _is_source_alive(url: str) -> bool:
    with _health_lock:
        h = _source_health.get(url, {})
    fails     = h.get('fails', 0)
    last_fail = h.get('last_fail', 0)
    if fails < 5: return True
    if time.time() - last_fail > 60:
        with _health_lock:
            _source_health[url]['fails'] = 0
        return True
    return False

# ═══════════════════════════════════════════════════════════════
# AUTO MIRROR DISCOVERY
# ═══════════════════════════════════════════════════════════════
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
            r  = requests.get(f'{url}{endpoint}', params={'query': 'arijit singh', 'q': 'arijit singh', 'limit': 2},
                              timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data    = r.json()
            results = (data.get('data', {}).get('results') or data.get('results') or
                       data.get('songs', {}).get('results') or [])
            if results and len(results) > 0:
                _health_record_ok(url, elapsed)
                return True
        except Exception:
            continue
    _health_record_fail(url)
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
        for future in as_completed(futures, timeout=60):
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

_mirror_fail_count = {}
_mirror_fail_time  = {}
MIRROR_FAIL_COOLDOWN = 30

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

# ═══════════════════════════════════════════════════════════════
# PIPED INSTANCES
# ═══════════════════════════════════════════════════════════════
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
        r  = requests.get(f'{url}/search', params={'q': 'arijit singh', 'filter': 'music_songs'},
                          timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200 and r.json().get('items'):
            _health_record_ok(url, elapsed)
            return True
    except Exception:
        pass
    _health_record_fail(url)
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
    dead = [url for url in PIPED_INSTANCES if _source_health.get(url, {}).get('fails', 0) >= 10
            and not _test_piped_instance(url)]
    if dead:
        with _piped_lock:
            for url in dead:
                if url in PIPED_INSTANCES:
                    PIPED_INSTANCES.remove(url)

# ═══════════════════════════════════════════════════════════════
# INVIDIOUS INSTANCES
# ═══════════════════════════════════════════════════════════════
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
            _health_record_ok(url, elapsed)
            return True
    except Exception:
        pass
    _health_record_fail(url)
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

# ═══════════════════════════════════════════════════════════════
# SOUNDCLOUD CLIENT ID AUTO-REFRESH
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
# MASTER HEAL LOOP
# ═══════════════════════════════════════════════════════════════
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
        time.sleep(1800)

threading.Thread(target=_master_heal_loop, daemon=True).start()
log.info('[SelfHeal] Master heal loop started')

# ═══════════════════════════════════════════════════════════════
# ALLOWED STREAM DOMAINS
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
# IN-MEMORY CACHE
# ═══════════════════════════════════════════════════════════════
_meta_cache     = {}
META_CACHE_TTL  = 600
_ytdlp_cache    = {}
YTDLP_CACHE_TTL = 240

def _cache_get(key, store=None):
    store = store if store is not None else _meta_cache
    entry = store.get(key)
    if not entry: return None
    ts, data = entry
    ttl = YTDLP_CACHE_TTL if store is _ytdlp_cache else META_CACHE_TTL
    if time.time() - ts > ttl:
        try:
            del store[key]
        except KeyError:
            pass
        return None
    return data

def _cache_set(key, data, store=None):
    store = store if store is not None else _meta_cache
    store[key] = (time.time(), data)
    if len(store) > 300:
        oldest = min(store, key=lambda k: store[k][0])
        try:
            del store[oldest]
        except KeyError:
            pass

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
# TEXT HELPERS
# ═══════════════════════════════════════════════════════════════
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
    if q == t: return 3.0
    q_words = q.split(); t_words = t.split(); a_words = a.split() if a else []
    score = 0.0
    if t.startswith(q): score += 2.0
    title_match = sum(max((fuzzy_word_match(qw, tw) for tw in t_words), default=0.0) for qw in q_words)
    if q_words: score += (title_match / len(q_words)) * 1.5
    artist_match = sum(max((fuzzy_word_match(qw, aw) for aw in a_words), default=0.0) for qw in q_words)
    if q_words and a_words: score += (artist_match / len(q_words)) * 1.0
    return score

def has_word_match(query, target):
    qw = set(normalize(query).split())
    tw = set(normalize(target).split())
    if not qw: return True
    return len(qw.intersection(tw)) > 0

# ═══════════════════════════════════════════════════════════════
# QUALITY & METADATA SELECTION
# ═══════════════════════════════════════════════════════════════
def pick_best_quality(urls):
    if not urls: return None, None
    for preferred in ['320kbps', '320', '160kbps', '160', '96kbps', '96']:
        for item in urls:
            q = (item.get('quality') or '').lower().strip()
            if q == preferred or preferred in q:
                url = item.get('url') or item.get('link') or ''
                if url.startswith('http'): return url, item.get('quality', preferred)
    for item in urls:
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'): return url, item.get('quality', 'unknown')
    return None, None

def pick_image(song):
    images = song.get('image') or song.get('imageUrls') or song.get('image_url') or ''
    if isinstance(images, list) and images:
        images = sorted(images, key=lambda x: int(re.search(r'(\d+)', x.get('quality', '0')).group(1)) if re.search(r'(\d+)', x.get('quality', '0')) else 0, reverse=True)
        url = images[0].get('url') or images[0].get('link') or ''
        if url.startswith('http'): return re.sub(r'\b(50|150)x(50|150)\b', '500x500', url)
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

# ═══════════════════════════════════════════════════════════════
# MIRROR HEALTH
# ═══════════════════════════════════════════════════════════════
def _mirror_ok(mirror):
    if not _is_source_alive(mirror): return False
    fails = _mirror_fail_count.get(mirror, 0)
    if fails < 3: return True
    last_fail = _mirror_fail_time.get(mirror, 0)
    if time.time() - last_fail > MIRROR_FAIL_COOLDOWN:
        _mirror_fail_count[mirror] = 0
        return True
    return False

def _mirror_failed(mirror):
    _mirror_fail_count[mirror] = _mirror_fail_count.get(mirror, 0) + 1
    _mirror_fail_time[mirror]  = time.time()
    _health_record_fail(mirror)

# ═══════════════════════════════════════════════════════════════
# NORMALIZER FOR SAAVN RESPONSES
# ═══════════════════════════════════════════════════════════════
def _normalize_saavn_songs(songs_raw):
    normalized = []
    for song in songs_raw:
        if not song: continue
        song_id = song.get('id') or song.get('perma_url', '').split('/')[-1] or ''
        if not song_id: continue
        title  = song.get('name') or song.get('title') or song.get('song') or 'Unknown Track'
        artist = song.get('primaryArtists') or song.get('primary_artists') or song.get('singers') or 'Unknown Artist'
        image  = pick_image(song)
        
        proxied_image = f"/api/proxy-image?url={quote(image)}" if image else ""

        dur_sec = int(song.get('duration') or 0)
        dur_ms  = dur_sec * 1000 if dur_sec else 180000
        year    = _safe_year(song.get('year') or song.get('releaseDate')) or datetime.now().year
        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        _, quality = pick_best_quality(raw_urls)
        if not quality: continue
        normalized.append({
            'trackId': song_id,
            'trackName': title,
            'artistName': artist,
            'artworkUrl100': proxied_image,
            'previewUrl': f"/api/play?id={quote(song_id, safe='')}",
            'trackTimeMillis': dur_ms,
            'releaseDate': f"{year}-01-01T00:00:00Z",
            '_saavnId': song_id,
            '_quality': quality, '_source': 'saavn',
        })
    return normalized

# ═══════════════════════════════════════════════════════════════
# IMAGE PROXY ROUTE (CORS & Referer fix)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/proxy-image')
@limiter.limit("300 per minute")
def proxy_image():
    img_url = request.args.get('url', '').strip()
    if not img_url:
        return jsonify({'error': 'Missing image url'}), 400
    try:
        req_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.jiosaavn.com/'
        }
        r = requests.get(img_url, headers=req_headers, timeout=10, stream=True)
        if r.status_code == 200:
            excluded = {'content-encoding', 'transfer-encoding', 'connection'}
            resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded}
            resp_headers['Access-Control-Allow-Origin'] = '*'
            if 'content-type' not in {k.lower() for k in resp_headers}:
                resp_headers['Content-Type'] = 'image/jpeg'
                
            def generate():
                for chunk in r.iter_content(chunk_size=32768):
                    yield chunk
            return Response(stream_with_context(generate()), status=200, headers=resp_headers)
    except Exception as e:
        log.error(f"Image proxy failed: {e}")
    return jsonify({'error': 'Failed to fetch image'}), 500

# ═══════════════════════════════════════════════════════════════
# YT-DLP
# ═══════════════════════════════════════════════════════════════
def fetch_from_ytdlp(title, artist=''):
    cache_key = f"ytdlp:{normalize(title)}:{normalize(artist)}"
    cached = _cache_get(cache_key, _ytdlp_cache)
    if cached: return cached
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
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
        'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True,
        'socket_timeout': 10, 'noplaylist': True, 'extract_flat': False,
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
    }
    for sq in search_queries:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(sq, download=False)
                if not info or 'entries' not in info or not info['entries']: continue
                best_result, best_score = None, -1
                for entry in info['entries']:
                    if not entry: continue
                    entry_title = entry.get('title', '')
                    if not has_word_match(title, entry_title): continue
                    score = title_score(title, entry_title, entry.get('uploader', ''))
                    if score > best_score: best_score = score; best_result = entry
                if not best_result or best_score < 0.3: continue
                
                res_info = ydl.extract_info(best_result['url'], download=False)
                formats = res_info.get('formats', [])
                audio_fmts = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
                if not audio_fmts: audio_fmts = [f for f in formats if f.get('url')]
                if not audio_fmts: continue
                
                best_fmt = max(audio_fmts, key=lambda f: f.get('abr', 0) or f.get('tbr', 0) or 0)
                if not best_fmt.get('url'): continue
                
                quality = f"{int(best_fmt.get('abr', 128))}kbps"
                thumb = best_result.get('thumbnail', '')
                if thumb and thumb.startswith('http'):
                    thumb = f"/api/proxy-image?url={quote(thumb)}"
                    
                result = {
                    'url': best_fmt['url'], 'quality': quality,
                    'title': best_result.get('title', title),
                    'artist': best_result.get('uploader', artist) or best_result.get('artist', artist),
                    'image': thumb, 'source': 'youtube',
                }
                _cache_set(cache_key, result, _ytdlp_cache)
                log.info(f"[yt-dlp] ✓ '{best_result.get('title')}' score={best_score:.2f} quality={quality}")
                return result
        except Exception as e:
            log.warning(f"[yt-dlp] '{title}' → {e}")
    return None

# ═══════════════════════════════════════════════════════════════
# SOUNDCLOUD
# ═══════════════════════════════════════════════════════════════
def fetch_from_soundcloud(title, artist=''):
    cache_key = f"sc:{normalize(title)}:{normalize(artist)}"
    cached = _cache_get(cache_key, _ytdlp_cache)
    if cached: return cached
    _maybe_refresh_sc_id()
    clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    query = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title
    ydl_opts = {
        'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True,
        'socket_timeout': 12, 'noplaylist': True,
        'http_headers': {'User-Agent': 'Mozilla/5.0'},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch5:{query}", download=False)
            if not info or not info.get('entries'): return None
            best = None; best_score = -1
            for entry in info['entries']:
                if not entry: continue
                if entry.get('duration', 0) < 60: continue
                score = title_score(query, entry.get('title', ''), entry.get('uploader', ''))
                if score > best_score: best_score = score; best = entry
            if not best or best_score < 0.4: return None
            
            res_info = ydl.extract_info(best['url'], download=False)
            formats = res_info.get('formats', [])
            if not formats: return None
            best_fmt = max(formats, key=lambda f: f.get('tbr', 0) or 0)
            if not best_fmt.get('url'): return None
            
            thumb = best.get('thumbnail', '')
            if thumb and thumb.startswith('http'):
                thumb = f"/api/proxy-image?url={quote(thumb)}"
                
            res = {
                'url': best_fmt['url'], 'quality': f"{int(best_fmt.get('tbr', 128))}kbps",
                'title': best.get('title', title), 'artist': best.get('uploader', artist),
                'image': thumb, 'source': 'soundcloud',
            }
            _cache_set(cache_key, res, _ytdlp_cache)
            return res
    except Exception as e:
        log.warning(f"[SoundCloud] '{query}' → {e}")
    return None

# ═══════════════════════════════════════════════════════════════
# SAAVN PARALLEL CORE ENGINE
# ═══════════════════════════════════════════════════════════════
def fetch_saavn_parallel(query, threshold=0.4):
    with _mirror_lock: mirrors = sorted(SAAVN_MIRRORS, key=_health_score, reverse=True)
    def try_mirror(mirror):
        try: return fetch_from_mirror(mirror, query, threshold)
        except Exception: return None
    futures = {_executor.submit(try_mirror, m): m for m in mirrors}
    try:
        for future in as_completed(futures, timeout=12):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    return result
            except Exception: pass
    except Exception: pass
    return None

def fetch_from_mirror(mirror, query, min_score=0.4):
    if not _mirror_ok(mirror): return None
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            r = requests.get(f'{mirror}{endpoint}', params={'query': query, 'q': query, 'limit': 10}, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200: continue
            data = r.json()
            results = (data.get('data', {}).get('results') or data.get('results') or data.get('songs', {}).get('results') or [])
            best_song, best_score, best_dur = None, -1, float('inf')
            for song in results:
                song_title = song.get('name') or song.get('title') or ''
                song_artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                if not has_word_match(query, song_title): continue
                score = title_score(query, song_title, song_artist)
                dur = int(song.get('duration', 999) or 999)
                if score > best_score or (abs(score - best_score) < 0.05 and dur < best_dur):
                    best_score = score; best_song = song; best_dur = dur
            if not best_song or best_score < min_score: continue
            
            raw_urls = best_song.get('downloadUrl') or best_song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls}]
            best_url, quality = pick_best_quality(raw_urls)
            if not best_url: continue
            
            _health_record_ok(mirror)
            thumb = pick_image(best_song)
            if thumb and thumb.startswith('http'):
                thumb = f"/api/proxy-image?url={quote(thumb)}"
                
            return {
                'url': best_url, 'quality': quality, 'title': best_song.get('name') or best_song.get('title', ''),
                'artist': best_song.get('primaryArtists') or best_song.get('primary_artists') or '',
                'image': thumb, 'source': 'saavn', 'score': best_score, '_raw_urls': raw_urls,
            }
        except Exception:
            _mirror_failed(mirror)
    return None

def _fetch_saavn_search_parallel(query):
    with _mirror_lock: mirrors = sorted(SAAVN_MIRRORS, key=_health_score, reverse=True)[:5]
    def try_search(mirror):
        if not _mirror_ok(mirror): return None
        for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
            try:
                r = requests.get(f'{mirror}{endpoint}', params={'query': query, 'q': query, 'limit': 35}, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
                if r.status_code == 200:
                    d = r.json()
                    res = (d.get('data', {}).get('results') or d.get('results') or d.get('songs', {}).get('results') or [])
                    if res: _health_record_ok(mirror); return res
            except Exception: pass
        _mirror_failed(mirror)
        return None
    futures = {_executor.submit(try_search, m): m for m in mirrors}
    try:
        for future in as_completed(futures, timeout=10):
            try:
                res = future.result()
                if res:
                    for f in futures: f.cancel()
                    return res
            except Exception: pass
    except Exception: pass
    return []

# ═══════════════════════════════════════════════════════════════
# PIPED
# ═══════════════════════════════════════════════════════════════
def fetch_from_piped(query, title='', artist=''):
    search_q = f"{title} {artist}".strip() if title else query
    with _piped_lock: instances = sorted(PIPED_INSTANCES, key=_health_score, reverse=True)
    fail_count = 0
    for instance in instances:
        if not _is_source_alive(instance): continue
        try:
            t0 = time.time()
            r = requests.get(f'{instance}/search', params={'q': search_q, 'filter': 'music_songs'}, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: _health_record_fail(instance); fail_count += 1; continue
            results = r.json().get('items', [])
            if not results: _health_record_fail(instance); fail_count += 1; continue
            best = None; best_score = -1
            for item in results[:5]:
                if item.get('type') != 'stream': continue
                if not has_word_match(query, item.get('title', '')): continue
                score = title_score(query, item.get('title', ''), item.get('uploaderName', ''))
                if score > best_score: best_score = score; best = item
            if not best or best_score < 0.3: continue
            
            video_id = best.get('url', '').split('=')[-1] if '=' in best.get('url', '') else best.get('url', '').split('/')[-1]
            if not video_id: continue
            sr = requests.get(f'{instance}/streams/{video_id}', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if sr.status_code != 200: continue
            audio_streams = sr.json().get('audioStreams', [])
            if not audio_streams: continue
            best_stream = max(audio_streams, key=lambda s: s.get('bitrate', 0))
            if not best_stream.get('url'): continue
            
            _health_record_ok(instance, elapsed)
            thumb = best.get('thumbnail', '')
            if thumb and thumb.startswith('http'):
                thumb = f"/api/proxy-image?url={quote(thumb)}"
                
            return {
                'url': best_stream['url'], 'quality': f"{best_stream.get('bitrate', 128000) // 1000}kbps",
                'title': best.get('title', title), 'artist': best.get('uploaderName', artist),
                'image': thumb, 'source': 'piped',
            }
        except Exception as e:
            _health_record_fail(instance); fail_count += 1
            log.warning(f"[Piped {instance}] {e}"); continue
    if fail_count >= len(instances): _maybe_reactive_heal('piped')
    return None

# ═══════════════════════════════════════════════════════════════
# INVIDIOUS
# ═══════════════════════════════════════════════════════════════
def fetch_from_invidious(query, title='', artist=''):
    search_q = f"{title} {artist}".strip() if title else query
    with _invidious_lock: instances = sorted(INVIDIOUS_INSTANCES, key=_health_score, reverse=True)
    fail_count = 0
    for instance in instances:
        if not _is_source_alive(instance): continue
        try:
            t0 = time.time()
            r = requests.get(f'{instance}/api/v1/search', params={'q': search_q, 'type': 'video', 'page': 1}, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: _health_record_fail(instance); fail_count += 1; continue
            results = r.json()
            if not results or not isinstance(results, list): _health_record_fail(instance); fail_count += 1; continue
            best = None; best_score = -1
            for item in results[:5]:
                if not has_word_match(query, item.get('title', '')): continue
                score = title_score(query, item.get('title', ''), item.get('author', ''))
                if score > best_score: best_score = score; best = item
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
            
            _health_record_ok(instance, elapsed)
            return {
                'url': best_fmt['url'], 'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title': best.get('title', title), 'artist': best.get('author', artist),
                'image': f"/api/proxy-image?url={quote(f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg')}",
                'source': 'invidious',
            }
        except Exception as e:
            _health_record_fail(instance); fail_count += 1
            log.warning(f"[Invidious {instance}] {e}"); continue
    if fail_count >= len(instances): _maybe_reactive_heal('invidious')
    return None

# ═══════════════════════════════════════════════════════════════
# /api/play
# ═══════════════════════════════════════════════════════════════
@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    song_id = request.args.get('id', '').strip()
    title   = request.args.get('title', '').strip()
    artist  = request.args.get('artist', '').strip()
    if not song_id and not title:
        return jsonify({'error': 'Missing identification'}), 400
        
    _play_ck = f"stream:{song_id or 'none'}:{normalize(title)}:{normalize(artist)}"
    cached  = _supabase_cache_get(_play_ck)
    if cached and cached.get('url'):
        audio_url = cached['url']
        quality   = cached.get('quality', 'unknown')
        source    = cached.get('source', 'unknown')
        log.info(f"[Play] ✓ Cache hit id={song_id} source={cached.get('source')}")
    else:
        audio_url, quality, source = None, 'unknown', 'unknown'
        if song_id:
            with _mirror_lock: mirrors = sorted(SAAVN_MIRRORS, key=_health_score, reverse=True)
            def try_id(m):
                if not _mirror_ok(m): return None
                for ep in [f'/api/songs/{song_id}', f'/songs/{song_id}', f'/api/songs?id={song_id}']:
                    try:
                        r = requests.get(f"{m}{ep}", timeout=7, headers={'User-Agent': 'Mozilla/5.0'})
                        if r.status_code == 200:
                            data = r.json().get('data', []) or r.json()
                            if isinstance(data, list) and data: return data[0]
                            if isinstance(data, dict): return data
                    except Exception: pass
                return None
            futures = {_executor.submit(try_id, m): m for m in mirrors[:6]}
            for future in as_completed(futures, timeout=8):
                song_meta = future.result()
                if song_meta:
                    raw_urls = song_meta.get('downloadUrl') or song_meta.get('download_url') or []
                    if isinstance(raw_urls, str): raw_urls = [{'url': raw_urls}]
                    audio_url, quality = pick_best_quality(raw_urls)
                    source = 'saavn'
                    if not title: title = song_meta.get('name') or song_meta.get('title') or ''
                    if not artist: artist = song_meta.get('primaryArtists') or song_meta.get('primary_artists') or ''
                    break
                    
        if not audio_url and title:
            query = f"{artist} {title}".strip() if artist else title
            yt_future  = _executor.submit(fetch_from_ytdlp, title, artist)
            sc_future  = _executor.submit(fetch_from_soundcloud, title, artist)
            pip_future = _executor.submit(fetch_from_piped, query, title, artist)
            inv_future = _executor.submit(fetch_from_invidious, query, title, artist)
            
            for f in as_completed([yt_future, sc_future, pip_future, inv_future], timeout=12):
                try:
                    res = f.result()
                    if res and res.get('url'):
                        audio_url = res['url']
                        quality = res.get('quality', 'unknown')
                        source = res.get('source', 'unknown')
                        log.info(f"[Play] ✓ {source} '{res.get('title')}' quality={quality}")
                        yt_future.cancel(); sc_future.cancel(); pip_future.cancel(); inv_future.cancel()
                        break
                except Exception: pass
                
        if not audio_url and title:
            for broad_query in [title, title.split()[0] if title.split() else title]:
                broad = fetch_from_ytdlp(broad_query, '')
                if broad and broad.get('url'):
                    audio_url = broad['url']; quality = broad.get('quality', 'unknown')
                    source = 'youtube-broad'
                    break
                    
        if not audio_url:
            log.warning(f"[Play] ✗ ALL sources failed id={song_id} title='{title}'")
            return jsonify({'error': 'No audio source found'}), 404
            
        _executor.submit(_supabase_cache_set, _play_ck, {
            'url': audio_url, 'quality': quality, 'source': source, 'title': title, 'artist': artist, 'image': ''
        })

    try:
        range_header = request.headers.get('Range')
        req_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': '*/*',
            'Connection': 'keep-alive',
        }
        if range_header:
            req_headers['Range'] = range_header

        upstream = requests.get(audio_url, headers=req_headers, stream=True, timeout=30, allow_redirects=True)
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'X-Audio-Quality': quality,
            'X-Audio-Source': source,
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'
            
        status_code = upstream.status_code
        if range_header and status_code == 200:
            status_code = 206

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=131072): 
                    if chunk: yield chunk
            except Exception as stream_err:
                log.error(f"Streaming error inside generator: {stream_err}")
            finally:
                upstream.close()
                
        return Response(stream_with_context(generate()), status=status_code, headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# /api/search
# ═══════════════════════════════════════════════════════════════
@app.route('/api/search')
@limiter.limit("120 per minute")
def search_songs():
    query = request.args.get('q', '').strip()
    if not query: return jsonify({'results': []})
    
    is_90s = any(t in query.lower() for t in NINETIES_TRIGGERS)
    cache_key = f"search:{query.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, '_cached': True})
        
    itunes_results, saavn_results = [], []
    def fetch_itunes():
        nonlocal itunes_results
        try:
            r = requests.get('https://itunes.apple.com/search', params={'term': query, 'media': 'music', 'limit': 20, 'country': 'IN'}, timeout=6)
            if r.status_code == 200:
                for item in r.json().get('results', []):
                    track_id = str(item.get('trackId', ''))
                    if not track_id: continue
                    img = item.get('artworkUrl100', '')
                    if img and img.startswith('http'):
                        img = f"/api/proxy-image?url={quote(img.replace('100x100bb', '500x500bb'))}"
                    itunes_results.append({
                        'trackId': track_id,
                        'trackName': item.get('trackName', 'Unknown'),
                        'artistName': item.get('artistName', 'Unknown'),
                        'artworkUrl100': img,
                        'previewUrl': f"/api/play?title={quote(item.get('trackName',''), safe='')}&artist={quote(item.get('artistName',''), safe='')}",
                        'trackTimeMillis': item.get('trackTimeMillis', 180000),
                        'releaseDate': item.get('releaseDate', ''),
                        '_source': 'itunes',
                    })
        except Exception: pass

    def fetch_saavn():
        nonlocal saavn_results
        try:
            search_term = re.sub(r'\b(song|songs|video|mp3|download|lyric|lyrics)\b', '', query, flags=re.IGNORECASE).strip()
            raw = _fetch_saavn_search_parallel(search_term)
            if raw:
                normalized = _normalize_saavn_songs(raw)
                if is_90s:
                    filtered = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
                    normalized = filtered if len(filtered) >= 5 else normalized
                random.shuffle(normalized)
                saavn_results = normalized[:30]
        except Exception: pass

    t1 = threading.Thread(target=fetch_itunes)
    t2 = threading.Thread(target=fetch_saavn)
    t1.start(); t2.start()
    t1.join(timeout=13); t2.join(timeout=13)
    
    def is_duplicate(song, existing):
        name = normalize(song.get('trackName') or song.get('artistName') or '')
        for e in existing:
            e_name = normalize(e.get('trackName') or e.get('artistName') or '')
            if name and e_name and (name in e_name or e_name in name): return True
        return False
        
    merged = list(itunes_results)
    for s in saavn_results:
        if not is_duplicate(s, merged): merged.append(s)
    if merged:
        _cache_set(cache_key, merged)
        return jsonify({'results': merged})
    return jsonify({'results': [], 'error': 'No results found'})

# ═══════════════════════════════════════════════════════════════
# /api/songs/90s
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed = random.choice(NINETIES_SEEDS)
    cache_key = f"songs:{seed.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, 'seed': seed, '_cached': True})
        
    raw = _fetch_saavn_search_parallel(seed)
    if raw:
        normalized = _normalize_saavn_songs(raw)
        filtered = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        result = (filtered if len(filtered) >= 5 else normalized)
        random.shuffle(result)
        final_res = result[:30]
        _cache_set(cache_key, final_res)
        return jsonify({'results': final_res, 'seed': seed})
    return jsonify({'results': [], 'seed': seed})

# ═══════════════════════════════════════════════════════════════
# /api/stream
# ═══════════════════════════════════════════════════════════════
@app.route('/api/stream')
@limiter.limit("300 per minute")
def stream_audio():
    audio_url = request.args.get('url', '').strip()
    if not audio_url: return jsonify({'error': 'Missing url'}), 400
    
    parsed = urlparse(audio_url)
    domain_ok = any(d in parsed.netloc for d in ALLOWED_STREAM_DOMAINS) or any(re.match(d.replace('.', r'\.'), parsed.netloc) for d in ALLOWED_STREAM_DOMAINS if '-' in d)
    if not domain_ok:
        return jsonify({'error': 'Domain not whitelisted'}), 403
        
    try:
        range_header = request.headers.get('Range')
        req_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': '*/*',
            'Connection': 'keep-alive',
        }
        if range_header:
            req_headers['Range'] = range_header
            
        upstream = requests.get(audio_url, headers=req_headers, stream=True, timeout=30, allow_redirects=True)
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache, no-store, must-revalidate'
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'
            
        status_code = upstream.status_code
        if range_header and status_code == 200:
            status_code = 206
            
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=131072):
                    if chunk: yield chunk
            except Exception as stream_err:
                log.error(f"Streaming exception: {stream_err}")
            finally:
                upstream.close()
                
        return Response(stream_with_context(generate()), status=status_code, headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════════════════════════
@app.route('/api/download')
@limiter.limit("20 per minute")
def download_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    quality = request.args.get('quality', 'full').strip()
    if not q: return jsonify({'error': 'Missing query'}), 400
    
    stream_url = None; content_type = 'audio/mpeg'
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
            stream_url = yt['url']
            filename_base = f"{yt['title']} - {yt['artist']}".strip(' -')
            content_type = 'audio/webm'
            
    if not stream_url:
        sc = fetch_from_soundcloud(q, artist)
        if sc and sc.get('url'):
            stream_url = sc['url']
            filename_base = f"{sc['title']} - {sc['artist']}".strip(' -')
            
    if not stream_url:
        pip = fetch_from_piped(f"{artist} {q}".strip(), q, artist)
        if pip and pip.get('url'):
            stream_url = pip['url']
            filename_base = f"{pip['title']} - {pip['artist']}".strip(' -')
            
    if not stream_url:
        return jsonify({'error': 'No downloadable source found'}), 404
        
    try:
        r = requests.get(stream_url, headers={'User-Agent': 'Mozilla/5.0'}, stream=True, timeout=30)
        ext = 'mp3' if 'mpeg' in content_type else 'webm'
        safe_filename = "".join(c for c in filename_base if c.isalnum() or c in (' ', '-', '_')).strip() or "track"
        
        excluded = {'content-encoding', 'transfer-encoding', 'connection', 'content-disposition'}
        headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded}
        headers.update({
            'Content-Disposition': f'attachment; filename="{safe_filename}.{ext}"',
            'Content-Type': content_type,
            'Access-Control-Allow-Origin': '*'
        })
        return Response(stream_with_context(r.iter_content(chunk_size=65536)), status=r.status_code, headers=headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# STATUS & DEV OPS
# ═══════════════════════════════════════════════════════════════
@app.route('/api/status')
@limiter.limit("30 per minute")
def system_status():
    with _mirror_lock: saavn_list = list(SAAVN_MIRRORS)
    with _piped_lock: piped_list = list(PIPED_INSTANCES)
    with _invidious_lock: inv_list = list(INVIDIOUS_INSTANCES)
    
    def summarize(sources):
        result = []
        for url in sources[:8]:
            with _health_lock: h = _source_health.get(url, {})
            fails  = h.get('fails', 0)
            last_ok = h.get('last_ok', 0)
            avg_ms  = h.get('avg_ms', 0)
            status  = 'alive' if _is_source_alive(url) else 'dead'
            result.append({
                'url': url, 'status': status, 'fails': fails,
                'last_ok': round(time.time() - last_ok) if last_ok else None,
                'avg_ms': round(avg_ms),
            })
        result.sort(key=lambda x: x['fails'])
        return result
        
    with _sc_client_id_lock: sc_id = SOUNDCLOUD_CLIENT_ID
    return jsonify({
        'saavn': {'count': len(saavn_list), 'instances': summarize(saavn_list)},
        'piped': {'count': len(piped_list), 'instances': summarize(piped_list)},
        'invidious': {'count': len(inv_list), 'instances': summarize(inv_list)},
        'soundcloud': {'client_id_prefix': sc_id[:8] + '...' if sc_id else 'missing'},
        'timestamp': round(time.time()),
    })

# ═══════════════════════════════════════════════════════════════
# AUTH — Google Login + Save User to Supabase
# ═══════════════════════════════════════════════════════════════
@app.route('/api/auth/google', methods=['POST'])
@limiter.limit("20 per minute")
def handle_google_auth():
    data = request.get_json() or {}
    credential = data.get('credential', '').strip()
    if not credential: return jsonify({'error': 'Missing credential'}), 400
    
    profile = _verify_google_jwt(credential)
    if not profile: return jsonify({'error': 'Invalid credential'}), 401
    
    sub = profile.get('sub', '').strip()
    if not sub: return jsonify({'error': 'Missing sub'}), 400
    
    sb_upsert('users', {
        'google_sub': sub,
        'name': profile.get('name', ''),
        'email': profile.get('email', ''),
        'picture': profile.get('picture', ''),
    }, on_conflict='google_sub')
    log.info(f"[Auth] User saved: {profile.get('email', '')} | pic: {bool(profile.get('picture'))}")
    return jsonify({
        'success': True, 'sub': sub, 'name': profile.get('name', ''),
        'email': profile.get('email', ''), 'picture': profile.get('picture', ''),
    })

# ═══════════════════════════════════════════════════════════════
# SYNC — Playback State
# ═══════════════════════════════════════════════════════════════
@app.route('/api/sync/state', methods=['POST'])
@limiter.limit("60 per minute")
def sync_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    sb_upsert('playback_state', {
        'google_sub': sub,
        'song_id':    data.get('song_id', ''),
        'song_title': data.get('song_title', ''),
        'artist':     data.get('artist', ''),
        'art_url':    data.get('art_url', ''),
        'progress':   float(data.get('progress', 0) or 0),
        'device':     data.get('device', 'mobile'),
        'updated_at': datetime.utcnow().isoformat(),
    }, on_conflict='google_sub')
    return jsonify({'success': True})

@app.route('/api/sync/state', methods=['GET'])
@limiter.limit("100 per minute")
def get_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'error': 'Unauthorized'}), 401
    
    rows = sb_select('playback_state', {'google_sub': sub})
    if not rows: return jsonify({'state': None})
    return jsonify({'state': rows[0]})

# ═══════════════════════════════════════════════════════════════
# TV PAIRING
# ═══════════════════════════════════════════════════════════════
@app.route('/api/auth/tv-code')
@limiter.limit("10 per minute")
def get_tv_pairing_code():
    session_id = request.args.get('sessionId', '').strip()
    if not session_id: return jsonify({'error': 'Missing sessionId'}), 400
    
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    exp  = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
    sb_upsert('tv_pairing', {
        'pairing_code':  code,
        'tv_session_id': session_id,
        'google_sub':    None,
        'expires_at':    exp
    }, on_conflict='pairing_code')
    return jsonify({'code': code, 'sessionId': session_id, 'expiresIn': 300})

@app.route('/api/auth/tv-poll')
@limiter.limit("60 per minute")
def poll_tv_pairing():
    code = request.args.get('code', '').strip().upper()
    now_str = datetime.utcnow().isoformat()
    if not code: return jsonify({'status': 'pending'}), 400
    
    url = f"{SUPABASE_URL}/rest/v1/tv_pairing?pairing_code=eq.{quote(code, safe='')}&expires_at=gt.{quote(now_str, safe='')}"
    try:
        r = requests.get(url, headers=_sb_headers(), timeout=10)
        rows = r.json() if r.status_code == 200_code else []
    except Exception:
        rows = []
    if not rows: return jsonify({'status': 'expired'})
    
    row = rows[0]
    if row.get('google_sub'):
        user_rows = sb_select('users', {'google_sub': row['google_sub']})
        sb_delete('tv_pairing', {'pairing_code': code})
        if user_rows:
            user = user_rows[0]
            return jsonify({
                'status': 'paired', 'sub': user['google_sub'], 'name': user.get('name', ''),
                'email': user.get('email', ''), 'picture': user.get('picture', '')
            })
    return jsonify({'status': 'pending'})

@app.route('/api/auth/tv-pair', methods=['POST'])
@limiter.limit("20 per minute")
def confirm_tv_pairing():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    if not code: return jsonify({'error': 'Missing code'}), 400
    
    now_str = datetime.utcnow().isoformat()
    url = f"{SUPABASE_URL}/rest/v1/tv_pairing?pairing_code=eq.{quote(code, safe='')}&expires_at=gt.{quote(now_str, safe='')}"
    try:
        r = requests.get(url, headers=_sb_headers(), timeout=10)
        rows = r.json() if r.status_code == 200 else []
    except Exception: rows = []
    if not rows: return jsonify({'error': 'Invalid or expired code'}), 400
    
    sb_update('tv_pairing', {'google_sub': sub}, {'pairing_code': code})
    return jsonify({'success': True, 'sessionId': rows[0].get('tv_session_id')})

# ═══════════════════════════════════════════════════════════════
# GHOST MODE
# ═══════════════════════════════════════════════════════════════
@app.route('/api/ghost/pin', methods=['POST'])
@limiter.limit("15 per minute")
def manage_ghost_pin():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    pin  = str(data.get('pin', '')).strip()
    if not pin or not pin.isdigit() or len(pin) < 4:
        return jsonify({'success': False}), 400
        
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

# ═══════════════════════════════════════════════════════════════
# ADMIN — View registered users
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
    return jsonify({'status': 'healthy', 'timestamp': int(time.time())})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
