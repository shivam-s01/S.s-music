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
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
ADMIN_KEY        = os.environ.get('ADMIN_KEY', '')
TURSO_URL        = os.environ.get('TURSO_URL', '')
TURSO_TOKEN      = os.environ.get('TURSO_TOKEN', '')

if not GOOGLE_CLIENT_ID: raise RuntimeError('GOOGLE_CLIENT_ID env var is required')
if not ADMIN_KEY:         raise RuntimeError('ADMIN_KEY env var is required')
if not TURSO_URL:         raise RuntimeError('TURSO_URL env var is required')
if not TURSO_TOKEN:       raise RuntimeError('TURSO_TOKEN env var is required')

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
_executor   = ThreadPoolExecutor(max_workers=32)
_google_req = google_requests.Request()

# ═══════════════════════════════════════════════════════════════
# L1 IN-MEMORY CACHE (fast, no network) — sits in front of Turso
# ═══════════════════════════════════════════════════════════════
_l1_cache     = {}
_l1_lock      = threading.Lock()
_L1_TTL       = 300   # 5 min for play/saavn hits
_L1_MAX       = 1000  # max keys before eviction

def _l1_get(key: str):
    with _l1_lock:
        entry = _l1_cache.get(key)
    if not entry:
        return None
    ts, val = entry
    if time.time() - ts > _L1_TTL:
        with _l1_lock:
            _l1_cache.pop(key, None)
        return None
    return val

def _l1_set(key: str, val):
    with _l1_lock:
        if len(_l1_cache) >= _L1_MAX:
            # evict oldest 20%
            sorted_keys = sorted(_l1_cache, key=lambda k: _l1_cache[k][0])
            for k in sorted_keys[:_L1_MAX // 5]:
                del _l1_cache[k]
        _l1_cache[key] = (time.time(), val)

# ═══════════════════════════════════════════════════════════════
# JWT HELPERS
# ═══════════════════════════════════════════════════════════════
# Cache verified JWTs so repeat requests don't re-verify
_jwt_verify_cache = {}
_JWT_CACHE_TTL    = 180  # 3 min

def _verify_google_jwt(credential: str) -> dict | None:
    cached = _jwt_verify_cache.get(credential)
    if cached:
        ts, payload = cached
        if time.time() - ts < _JWT_CACHE_TTL:
            return payload
    try:
        payload = id_token.verify_oauth2_token(
            credential, _google_req, GOOGLE_CLIENT_ID, clock_skew_in_seconds=10,
        )
        if payload.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
            return None
        _jwt_verify_cache[credential] = (time.time(), payload)
        # keep cache small
        if len(_jwt_verify_cache) > 500:
            oldest = min(_jwt_verify_cache, key=lambda k: _jwt_verify_cache[k][0])
            del _jwt_verify_cache[oldest]
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
# DATABASE — TURSO HTTP REST API
# ═══════════════════════════════════════════════════════════════
_turso_session = requests.Session()  # reuse TCP connection to Turso

def _turso_headers():
    return {
        'Authorization': f'Bearer {TURSO_TOKEN}',
        'Content-Type':  'application/json',
    }

def _turso_url():
    url = TURSO_URL
    if url.startswith('libsql://'):
        url = 'https://' + url[len('libsql://'):]
    url = url.rstrip('/')
    return f"{url}/v2/pipeline"

def _encode_arg(a):
    if a is None:           return {'type': 'null'}
    if isinstance(a, int):  return {'type': 'integer', 'value': str(a)}
    if isinstance(a, float):return {'type': 'float',   'value': str(a)}
    return {'type': 'text', 'value': str(a)}

class TursoResult:
    def __init__(self, raw):
        cols_raw     = raw.get('cols', [])
        self.columns = [type('Col', (), {'name': c['name']})() for c in cols_raw]
        raw_rows     = raw.get('rows', [])
        self.rows    = []
        for row in raw_rows:
            parsed = []
            for cell in row:
                t = cell.get('type', 'null')
                v = cell.get('value')
                if t == 'null':    parsed.append(None)
                elif t == 'integer': parsed.append(int(v))
                elif t == 'float':   parsed.append(float(v))
                else:              parsed.append(v)
            self.rows.append(parsed)

def db_execute(sql: str, args=None) -> TursoResult:
    stmt = {'sql': sql}
    if args:
        stmt['args'] = [_encode_arg(a) for a in args]
    body = {'requests': [{'type': 'execute', 'stmt': stmt}, {'type': 'close'}]}
    try:
        r = _turso_session.post(_turso_url(), json=body, headers=_turso_headers(), timeout=10)
        r.raise_for_status()
        result = r.json()['results'][0]
        if result.get('type') == 'error':
            raise RuntimeError(f"Turso error: {result.get('error', {}).get('message', 'unknown')}")
        return TursoResult(result.get('response', {}).get('result', {}))
    except Exception as e:
        log.error(f'[DB] execute error: {e}  sql={sql[:80]}')
        raise

def db_batch(statements) -> list:
    reqs = []
    for s in statements:
        if isinstance(s, str):
            reqs.append({'type': 'execute', 'stmt': {'sql': s}})
        else:
            sql, args = s[0], s[1]
            reqs.append({'type': 'execute', 'stmt': {'sql': sql, 'args': [_encode_arg(a) for a in args]}})
    reqs.append({'type': 'close'})
    body = {'requests': reqs}
    try:
        r = _turso_session.post(_turso_url(), json=body, headers=_turso_headers(), timeout=15)
        r.raise_for_status()
        return r.json().get('results', [])
    except Exception as e:
        log.error(f'[DB] batch error: {e}')
        raise

def init_db():
    db_batch([
        "CREATE TABLE IF NOT EXISTS users (google_sub TEXT PRIMARY KEY, name TEXT, email TEXT, picture TEXT, ghost_pin_hash TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS playback_state (google_sub TEXT PRIMARY KEY, song_id TEXT, song_title TEXT, artist TEXT, art_url TEXT, progress REAL DEFAULT 0, device TEXT DEFAULT 'mobile', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS tv_pairing (pairing_code TEXT PRIMARY KEY, tv_session_id TEXT, google_sub TEXT, expires_at TIMESTAMP)",
        "CREATE TABLE IF NOT EXISTS song_cache (cache_key TEXT PRIMARY KEY, url TEXT, quality TEXT, title TEXT, artist TEXT, image TEXT, source TEXT, cached_at INTEGER DEFAULT (strftime('%s','now')))",
    ])
    log.info('[DB] Turso tables initialized')

init_db()

# ═══════════════════════════════════════════════════════════════
# TURSO SONG CACHE — always async (never blocks request)
# ═══════════════════════════════════════════════════════════════
_SONG_CACHE_TTL      = 86400   # 24h stable
_VOLATILE_CACHE_TTL  = 21600   # 6h youtube/sc/piped
_VOLATILE_SOURCES    = {'youtube', 'youtube-broad', 'piped', 'invidious', 'soundcloud'}

def _turso_cache_get(cache_key: str) -> dict | None:
    # L1 first — zero network cost
    hit = _l1_get(f"tc:{cache_key}")
    if hit is not None:
        return hit

    try:
        result = db_execute(
            "SELECT url, quality, title, artist, image, source, cached_at FROM song_cache WHERE cache_key = ?",
            [cache_key]
        )
        if not result.rows:
            _l1_set(f"tc:{cache_key}", None)
            return None
        cols = [c.name for c in result.columns]
        row  = dict(zip(cols, result.rows[0]))
        age  = int(time.time()) - int(row.get('cached_at', 0))
        src  = row.get('source', '')
        ttl  = _VOLATILE_CACHE_TTL if src in _VOLATILE_SOURCES else _SONG_CACHE_TTL
        if age > ttl:
            _executor.submit(_bg_delete_cache, cache_key)
            return None
        # Volatile URL liveness check (background, non-blocking)
        if src in _VOLATILE_SOURCES:
            _executor.submit(_bg_check_volatile_url, cache_key, row['url'])
        _l1_set(f"tc:{cache_key}", row)
        return row
    except Exception as e:
        log.warning(f'[TursoCache] get error: {e}')
        return None

def _bg_check_volatile_url(cache_key: str, url: str):
    try:
        head = requests.head(url, timeout=3, allow_redirects=True, headers={'User-Agent': 'Mozilla/5.0'})
        if head.status_code >= 400:
            _bg_delete_cache(cache_key)
            with _l1_lock:
                _l1_cache.pop(f"tc:{cache_key}", None)
    except Exception:
        pass

def _bg_delete_cache(cache_key: str):
    try:
        db_execute("DELETE FROM song_cache WHERE cache_key = ?", [cache_key])
    except Exception:
        pass

def _turso_cache_set(cache_key: str, data: dict):
    # Also update L1
    _l1_set(f"tc:{cache_key}", data)
    try:
        db_execute(
            "INSERT INTO song_cache (cache_key, url, quality, title, artist, image, source) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET url=excluded.url, quality=excluded.quality, title=excluded.title, "
            "artist=excluded.artist, image=excluded.image, source=excluded.source, cached_at=strftime('%s','now')",
            [cache_key, data.get('url',''), data.get('quality',''), data.get('title',''),
             data.get('artist',''), data.get('image',''), data.get('source','')]
        )
    except Exception as e:
        log.warning(f'[TursoCache] set error: {e}')

# ═══════════════════════════════════════════════════════════════
# SAAVN MIRRORS — trimmed, top performers first
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
# MIRROR HEALTH (per-mirror fail tracking)
# ═══════════════════════════════════════════════════════════════
_mirror_fail_count   = {}
_mirror_fail_time    = {}
MIRROR_FAIL_COOLDOWN = 30

def _mirror_ok(mirror):
    if not _is_source_alive(mirror): return False
    fails = _mirror_fail_count.get(mirror, 0)
    if fails < 3: return True
    if time.time() - _mirror_fail_time.get(mirror, 0) > MIRROR_FAIL_COOLDOWN:
        _mirror_fail_count[mirror] = 0
        return True
    return False

def _mirror_failed(mirror):
    _mirror_fail_count[mirror] = _mirror_fail_count.get(mirror, 0) + 1
    _mirror_fail_time[mirror]  = time.time()
    _health_record_fail(mirror)

# ═══════════════════════════════════════════════════════════════
# SELF-HEALING — runs lazily, delayed start, longer intervals
# ═══════════════════════════════════════════════════════════════
_DISCOVERY_PATTERNS = [
    'jiosaavn-api', 'saavn-api', 'jiosaavn', 'saavn',
    'jio-saavn', 'saavnapi', 'jiosaavnapi',
]
_DISCOVERY_SUFFIXES = [
    '', '-v2', '-v3', '-v4', '-new', '-prod', '-main', '-app', '-api',
    '-privatecvc2', '-privatecvc3',
    '-one', '-two', '-three', '-four', '-five',
    '-six', '-seven', '-eight', '-nine', '-ten',
]
_DISCOVERY_PREFIXES = ['', 'the-', 'my-', 'open-', 'free-']
_DISCOVERY_HOSTS    = ['.vercel.app', '.up.railway.app', '.onrender.com']

def _test_mirror_working(url: str) -> bool:
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0 = time.time()
            r  = requests.get(
                f'{url}{endpoint}',
                params={'query': 'arijit singh', 'q': 'arijit singh', 'limit': 2},
                timeout=5, headers={'User-Agent': 'Mozilla/5.0'}
            )
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data    = r.json()
            results = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or []
            )
            if results:
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
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[Discovery] Timeout: {e}')
    if new_found:
        with _mirror_lock:
            SAAVN_MIRRORS = list(_discovered_set)
        log.info(f'[Discovery] +{len(new_found)} mirrors. Total: {len(SAAVN_MIRRORS)}')

def _verify_existing_mirrors():
    global SAAVN_MIRRORS
    to_remove = []
    with _mirror_lock:
        current = list(SAAVN_MIRRORS)
    for url in current:
        if _mirror_fail_count.get(url, 0) >= 15:
            if not _test_mirror_working(url):
                to_remove.append(url)
    if to_remove:
        with _mirror_lock:
            for url in to_remove:
                SAAVN_MIRRORS.remove(url) if url in SAAVN_MIRRORS else None
                _discovered_set.discard(url)
        log.info(f'[SelfHeal] Removed {len(to_remove)} dead mirrors')
        _executor.submit(_discover_mirrors)

# PIPED
_BASE_PIPED  = [
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
        r  = requests.get(f'{url}/search', params={'q': 'arijit singh', 'filter': 'music_songs'}, timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200 and r.json().get('items'):
            _health_record_ok(url, elapsed); return True
    except Exception:
        pass
    _health_record_fail(url); return False

def _heal_piped():
    global PIPED_INSTANCES
    try:
        r = requests.get('https://piped-instances.kavin.rocks/', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            instances     = r.json()
            new_candidates= [inst.get('api_url', '').rstrip('/') for inst in instances
                             if inst.get('api_url','').rstrip('/') not in _piped_known]
            futures = {_executor.submit(_test_piped_instance, u): u for u in new_candidates if u}
            try:
                for future in as_completed(futures, timeout=30):
                    url = futures[future]
                    try:
                        if future.result():
                            with _piped_lock:
                                if url not in _piped_known:
                                    _piped_known.add(url); PIPED_INSTANCES.append(url)
                    except Exception: pass
            except Exception: pass
    except Exception as e:
        log.warning(f'[SelfHeal:Piped] {e}')

# INVIDIOUS
_BASE_INVIDIOUS  = [
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
        r  = requests.get(f'{url}/api/v1/search', params={'q': 'arijit singh', 'type': 'video', 'page': 1}, timeout=7, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) > 0:
            _health_record_ok(url, elapsed); return True
    except Exception: pass
    _health_record_fail(url); return False

def _heal_invidious():
    global INVIDIOUS_INSTANCES
    try:
        r = requests.get('https://api.invidious.io/instances.json', params={'sort_by': 'health'}, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            new_candidates = [
                inst[1].get('uri','').rstrip('/') for inst in r.json()
                if isinstance(inst, list) and len(inst) >= 2
                and inst[1].get('uri','').startswith('https')
                and inst[1].get('api', False)
                and inst[1].get('uri','').rstrip('/') not in _invidious_known
            ]
            futures = {_executor.submit(_test_invidious_instance, u): u for u in new_candidates[:20] if u}
            try:
                for future in as_completed(futures, timeout=40):
                    url = futures[future]
                    try:
                        if future.result():
                            with _invidious_lock:
                                if url not in _invidious_known:
                                    _invidious_known.add(url); INVIDIOUS_INSTANCES.append(url)
                    except Exception: pass
            except Exception: pass
    except Exception as e:
        log.warning(f'[SelfHeal:Invidious] {e}')

# SOUNDCLOUD AUTO CLIENT-ID
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
            except Exception: continue
    except Exception as e:
        log.warning(f'[SelfHeal:SC] {e}')

def _maybe_refresh_sc_id():
    global _sc_client_id_last_check
    now = time.time()
    if now - _sc_client_id_last_check > _SC_ID_REFRESH_INTERVAL:
        _sc_client_id_last_check = now
        _executor.submit(_refresh_soundcloud_client_id)

# REACTIVE HEAL
_reactive_heal_cooldown = {}
_REACTIVE_COOLDOWN_S    = 120

def _maybe_reactive_heal(source_type: str):
    now  = time.time()
    last = _reactive_heal_cooldown.get(source_type, 0)
    if now - last < _REACTIVE_COOLDOWN_S: return
    _reactive_heal_cooldown[source_type] = now
    log.info(f'[SelfHeal:Reactive] {source_type}')
    if source_type == 'saavn':
        _executor.submit(_discover_mirrors)
        _executor.submit(_verify_existing_mirrors)
    elif source_type == 'piped':
        _executor.submit(_heal_piped)
    elif source_type == 'invidious':
        _executor.submit(_heal_invidious)
    elif source_type == 'soundcloud':
        _executor.submit(_refresh_soundcloud_client_id)

# MASTER HEAL LOOP — delayed start, longer interval
def _master_heal_loop():
    time.sleep(120)   # Wait 2 min after startup — don't hammer on boot
    while True:
        try:
            log.info('[SelfHeal] Starting heal cycle...')
            futures = [
                _executor.submit(_discover_mirrors),
                _executor.submit(_verify_existing_mirrors),
                _executor.submit(_heal_piped),
                _executor.submit(_heal_invidious),
                _executor.submit(_refresh_soundcloud_client_id),
            ]
            for f in as_completed(futures, timeout=120):
                try: f.result()
                except Exception as e: log.warning(f'[SelfHeal] {e}')
            with _mirror_lock:    sm = len(SAAVN_MIRRORS)
            with _piped_lock:     pi = len(PIPED_INSTANCES)
            with _invidious_lock: iv = len(INVIDIOUS_INSTANCES)
            log.info(f'[SelfHeal] ✓ Saavn:{sm} Piped:{pi} Invidious:{iv}')
        except Exception as e:
            log.error(f'[SelfHeal] Master loop error: {e}')
        time.sleep(3600)   # Every 1 hour — was 30 min

threading.Thread(target=_master_heal_loop, daemon=True).start()
log.info('[SelfHeal] Master heal loop started (delayed 120s)')

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
    '96kbps': 3,  '96': 3,  '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
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
# META CACHE (in-memory)
# ═══════════════════════════════════════════════════════════════
_meta_cache    = {}
META_CACHE_TTL = 600
_ytdlp_cache   = {}
YTDLP_CACHE_TTL= 240

def _cache_get(key, store=None):
    store = store if store is not None else _meta_cache
    entry = store.get(key)
    if not entry: return None
    ts, data = entry
    ttl = YTDLP_CACHE_TTL if store is _ytdlp_cache else META_CACHE_TTL
    if time.time() - ts > ttl:
        del store[key]; return None
    return data

def _cache_set(key, data, store=None):
    store = store if store is not None else _meta_cache
    store[key] = (time.time(), data)
    if len(store) > 300:
        oldest = min(store, key=lambda k: store[k][0])
        del store[oldest]

# ═══════════════════════════════════════════════════════════════
# CORS — also handle preflight properly
# ═══════════════════════════════════════════════════════════════
_CORS_HEADERS = {
    'Access-Control-Allow-Origin':   '*',
    'Access-Control-Allow-Methods':  'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers':  'Authorization, Content-Type, X-Requested-With',
    'Access-Control-Expose-Headers': 'Content-Length, Content-Range, X-Audio-Quality, X-Audio-Source',
    'Access-Control-Max-Age':        '86400',
}

def add_cors(resp):
    for k, v in _CORS_HEADERS.items():
        resp.headers[k] = v
    return resp

@app.after_request
def after_request(resp):
    return add_cors(resp)

@app.route('/<path:path>', methods=['OPTIONS'])
@app.route('/', methods=['OPTIONS'])
def options_handler(path=''):
    resp = Response(status=204)
    for k, v in _CORS_HEADERS.items():
        resp.headers[k] = v
    return resp

# ═══════════════════════════════════════════════════════════════
# FRONTEND ROUTES — proper cache headers for PWA
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def index():
    resp = send_file(os.path.join(BASE_DIR, 'index.html'))
    # HTML should revalidate, not cache forever
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/manifest.json')
def manifest():
    resp = send_file(os.path.join(BASE_DIR, 'manifest.json'), mimetype='application/manifest+json')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp

@app.route('/sw.js')
def service_worker():
    resp = send_file(os.path.join(BASE_DIR, 'sw.js'), mimetype='application/javascript')
    # Service worker MUST be no-cache so browser picks up updates
    resp.headers['Cache-Control']          = 'no-cache, no-store, must-revalidate'
    resp.headers['Pragma']                 = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp

@app.route('/.well-known/assetlinks.json')
def assetlinks():
    resp = app.send_static_file('assetlinks.json')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp

@app.route('/<path:filename>')
def serve_static(filename):
    file_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(file_path):
        resp = send_file(file_path)
        # Cache static assets aggressively (JS, CSS, images, fonts)
        ext = filename.rsplit('.', 1)[-1].lower()
        if ext in ('js', 'css', 'png', 'jpg', 'jpeg', 'svg', 'ico', 'woff', 'woff2', 'ttf'):
            resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        else:
            resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp
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

    bracket_free = re.sub(r'\s*[\(\[].*?[\)\]]\s*', ' ', title_c).strip()
    add(bracket_free)
    dash_free = re.sub(r'\s*[-–]\s*', ' ', title_c).strip()
    add(dash_free)
    words = title_c.split()
    if len(words) > 2: add(' '.join(words[:3]))
    if len(words) > 3: add(' '.join(words[:2]))
    if artist_first and title_first: add(f"{title_first} {artist_first}")
    if artist_c and title_first:     add(f"{artist_c} {title_first}")

    try:
        t_translit = _hindi_translit_normalize(title_c)
        if t_translit and t_translit != title_c:
            add(t_translit)
            if artist_first: add(f"{t_translit} {artist_first}")
    except Exception: pass

    return variants

_HINDI_TRANSLIT = [
    ('aa','a'),('ee','i'),('oo','u'),('ae','ai'),('ph','f'),('bh','b'),('gh','g'),
    ('kh','k'),('th','t'),('dh','d'),('sh','s'),('ch','c'),('ie','i'),('ey','ai'),
    ('ay','ai'),('oi','oy'),('ou','u'),('ue','u'),('hi','he'),('he','hi'),
    ('ho','hu'),('hu','ho'),('ki','ke'),('ke','ki'),('ko','ku'),('na','nah'),
    ('nah','na'),('hai','he'),('hain','he'),('he','hai'),('tum','tum'),('hum','hum'),
    ('pyar','pyaar'),('pyaar','pyar'),('dil','dill'),('dill','dil'),
    ('ishq','ishk'),('ishk','ishq'),
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
    if a_words:
        artist_match = sum(max((fuzzy_word_match(qw, aw) for aw in a_words), default=0.0) for qw in q_words)
        if q_words: score += (artist_match / len(q_words)) * 0.5
    return score

def dynamic_min_score(query):
    length = len(normalize(query).replace(' ', ''))
    if length <= 2:    return 0.10
    elif length <= 5:  return 0.20
    elif length <= 10: return 0.35
    else:              return 0.45

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
    """
    Returns the best thumbnail URL — prefers 150x150 for fast load,
    falls back to 500x500 only if smaller not available.
    """
    images = song.get('image') or []
    if isinstance(images, list) and images:
        for item in reversed(images):
            url = item.get('url') or item.get('link') or ''
            if url.startswith('http'):
                # Prefer 150x150 for fast thumbnail load
                return re.sub(r'\b(500|50)x(500|50)\b', '150x150', url)
    if isinstance(images, str) and images.startswith('http'):
        return re.sub(r'\b(500|50)x(500|50)\b', '150x150', images)
    return ''

def pick_image_large(song):
    """Full size 500x500 — only used when user explicitly views full art."""
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
    for preferred in ['96kbps','96','128kbps','128','48kbps','48']:
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
# SAAVN SEARCH
# ═══════════════════════════════════════════════════════════════
def _fetch_saavn_search_mirror(mirror, search_term):
    if not _mirror_ok(mirror): return []
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            r = requests.get(
                f'{mirror}{endpoint}',
                params={'query': search_term, 'q': search_term, 'limit': 20},
                timeout=6, headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200: continue
            data = r.json()
            raw  = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or []
            )
            if raw: return raw
        except Exception:
            _mirror_failed(mirror)
    return []

def _fetch_saavn_search_parallel(search_term):
    """
    Use only top-5 healthy mirrors (by health score) to avoid
    spawning 20 threads per request.
    """
    with _mirror_lock:
        mirrors = sorted(
            [m for m in SAAVN_MIRRORS if _mirror_ok(m)],
            key=_health_score, reverse=True
        )[:5]   # ← KEY FIX: was all 20 mirrors
    if not mirrors:
        mirrors = list(SAAVN_MIRRORS)[:5]
    futures = {_executor.submit(_fetch_saavn_search_mirror, m, search_term): m for m in mirrors}
    try:
        for future in as_completed(futures, timeout=8):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    return result
            except Exception: pass
    except Exception: pass
    return []

def _normalize_saavn_songs(raw_songs):
    normalized = []
    for song in raw_songs:
        song_id = song.get('id', '').strip()
        if not song_id: continue
        title   = song.get('name') or song.get('title', '')
        artist  = song.get('primaryArtists') or song.get('primary_artists') or ''
        image   = pick_image(song)    # 150x150 — fast load
        year    = str(song.get('year') or '0')[:4]
        dur_s   = int(song.get('duration', 0) or 0)
        dur_ms  = dur_s * 1000
        if dur_s > 1080: continue
        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        _, quality = pick_best_quality(raw_urls)
        if not quality: continue
        play_url = (
            f"/api/play?id={quote(song_id, safe='')}"
            f"&title={quote(title, safe='')}"
            f"&artist={quote(artist, safe='')}"
        )
        normalized.append({
            'trackId':         song_id,
            'trackName':       title,
            'artistName':      artist,
            'artworkUrl100':   image,   # 150x150 for fast grid load
            'previewUrl':      play_url,
            'trackTimeMillis': dur_ms,
            'releaseDate':     f"{year}-01-01T00:00:00Z",
            '_saavnId':        song_id,
            '_quality':        quality,
            '_source':         'saavn',
        })
    return normalized

# ═══════════════════════════════════════════════════════════════
# YT-DLP
# ═══════════════════════════════════════════════════════════════
def fetch_from_ytdlp(title, artist=''):
    cache_key = f"ytdlp:{normalize(title)}:{normalize(artist)}"
    cached    = _cache_get(cache_key, _ytdlp_cache)
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
            best_result = None
            best_score  = -1
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
                        score = title_score(title, yt_title, yt_artist)
                        if 'music.youtube' in (entry.get('webpage_url') or ''):
                            score += 0.3
                        if score > best_score:
                            best_score  = score; best_result = entry
                    if best_score >= 1.5: break
                except Exception: continue

            if not best_result: return None

            formats       = best_result.get('formats', [])
            audio_formats = [
                f for f in formats
                if f.get('acodec') not in ('none', None, '')
                and f.get('url')
                and (f.get('vcodec') in ('none', None, '') or not f.get('vcodec'))
            ]
            if not audio_formats:
                audio_formats = [f for f in formats if f.get('acodec') not in ('none', None, '') and f.get('url')]
            if not audio_formats:
                audio_formats = [f for f in formats if f.get('url')]
            if not audio_formats: return None

            best_fmt = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            abr      = best_fmt.get('abr') or best_fmt.get('tbr') or 0
            quality  = f"{int(abr)}kbps" if abr else 'unknown'

            thumb  = best_result.get('thumbnail', '')
            vid_id = best_result.get('id', '')
            if not thumb and vid_id:
                thumb = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"

            result = {
                'url':    best_fmt['url'],
                'quality': quality,
                'title':  best_result.get('title', title),
                'artist': best_result.get('uploader', artist) or best_result.get('artist', artist),
                'image':  thumb,
                'source': 'youtube',
            }
            _cache_set(cache_key, result, _ytdlp_cache)
            log.info(f"[yt-dlp] ✓ '{best_result.get('title')}' score={best_score:.2f} q={quality}")
            return result
    except Exception as e:
        log.warning(f"[yt-dlp] '{title}' → {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# SOUNDCLOUD
# ═══════════════════════════════════════════════════════════════
def fetch_from_soundcloud(title, artist=''):
    cache_key = f"sc:{normalize(title)}:{normalize(artist)}"
    cached    = _cache_get(cache_key, _ytdlp_cache)
    if cached: return cached

    _maybe_refresh_sc_id()

    clean_title  = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_artist = artist.split(',')[0].split('&')[0].strip() if artist else ''
    query = f"{clean_artist} {clean_title}".strip() if clean_artist else clean_title

    ydl_opts = {
        'format':         'bestaudio/best',
        'quiet':          True,
        'no_warnings':    True,
        'socket_timeout': 12,
        'noplaylist':     True,
        'http_headers':   {'User-Agent': 'Mozilla/5.0'},
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch5:{query}", download=False)
            if not info or not info.get('entries'): return None

            best = None; best_score = -1
            for entry in info['entries']:
                if not entry or entry.get('duration', 0) < 60: continue
                score = title_score(title, entry.get('title', ''), entry.get('uploader', ''))
                if score > best_score: best_score = score; best = entry

            if not best or best_score < 0.20: return None

            formats = best.get('formats', [])
            if not formats: return None
            best_fmt = max(formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            if not best_fmt.get('url'): return None

            abr     = best_fmt.get('abr') or best_fmt.get('tbr') or 0
            quality = f"{int(abr)}kbps" if abr else 'unknown'

            result = {
                'url':    best_fmt['url'],
                'quality': quality,
                'title':  best.get('title', title),
                'artist': best.get('uploader', artist),
                'image':  best.get('thumbnail', ''),
                'source': 'soundcloud',
            }
            _cache_set(cache_key, result, _ytdlp_cache)
            log.info(f"[SoundCloud] ✓ '{best.get('title')}' score={best_score:.2f}")
            return result
    except Exception as e:
        log.warning(f"[SoundCloud] '{title}' → {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# SAAVN BY ID
# ═══════════════════════════════════════════════════════════════
def _fetch_saavn_by_id(song_id: str) -> dict | None:
    with _mirror_lock:
        mirrors = sorted(
            [m for m in SAAVN_MIRRORS if _mirror_ok(m)],
            key=_health_score, reverse=True
        )[:5]
    if not mirrors: mirrors = list(SAAVN_MIRRORS)[:5]

    endpoints = [
        f'/api/songs/{song_id}',
        f'/songs/{song_id}',
        f'/api/songs?id={song_id}',
        f'/song?id={song_id}',
        f'/api/song?id={song_id}',
    ]

    def try_mirror(mirror):
        for endpoint in endpoints:
            try:
                r = requests.get(f'{mirror}{endpoint}', timeout=7, headers={'User-Agent': 'Mozilla/5.0'})
                if r.status_code != 200: continue
                data = r.json()
                song = None
                if isinstance(data.get('data'), list) and data['data']:   song = data['data'][0]
                elif isinstance(data.get('data'), dict):                  song = data['data']
                elif data.get('id'):                                       song = data
                elif data.get('songs'):
                    songs = data['songs']
                    song  = songs[0] if isinstance(songs, list) and songs else songs
                if not song: continue
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                if isinstance(raw_urls, str):
                    raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                best_url, quality = pick_best_quality(raw_urls)
                if best_url:
                    return {
                        'url':    best_url,
                        'quality': quality,
                        'title':  song.get('name') or song.get('title', ''),
                        'artist': song.get('primaryArtists') or song.get('primary_artists') or '',
                        'image':  pick_image(song),
                    }
            except Exception:
                _mirror_failed(mirror)
        return None

    futures = {_executor.submit(try_mirror, m): m for m in mirrors}
    try:
        for future in as_completed(futures, timeout=10):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    return result
            except Exception: pass
    except Exception: pass
    return None

# ═══════════════════════════════════════════════════════════════
# /api/play — streaming endpoint
# ═══════════════════════════════════════════════════════════════
@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    song_id = request.args.get('id', '').strip()
    title   = request.args.get('title', '').strip()
    artist  = request.args.get('artist', '').strip()

    if not song_id and not title:
        return jsonify({'error': 'Missing id or title'}), 400

    _play_ck     = f"play:{song_id or normalize(title)}:{normalize(artist)}"
    _play_cached = _turso_cache_get(_play_ck)   # L1 first, then Turso
    if _play_cached and _play_cached.get('url'):
        log.info(f"[Cache] HIT play: id={song_id} title='{title}'")
        audio_url = _play_cached['url']
        quality   = _play_cached.get('quality', 'unknown')
        source    = _play_cached.get('source', 'unknown')
        if not title:  title  = _play_cached.get('title', '')
        if not artist: artist = _play_cached.get('artist', '')
    else:
        audio_url = None; quality = 'unknown'; source = 'unknown'

    if not audio_url and song_id:
        result = _fetch_saavn_by_id(song_id)
        if result and result.get('url'):
            fetched_artist  = normalize(result.get('artist', ''))
            expected_artist = normalize(artist)
            artist_ok       = True
            if expected_artist and fetched_artist:
                ea_words = [w for w in expected_artist.split() if len(w) >= 3]
                fa_words = [w for w in fetched_artist.split() if len(w) >= 3]
                if ea_words and fa_words:
                    match_count = sum(
                        1 for ew in ea_words
                        if any(fuzzy_word_match(ew, fw) >= 0.75 for fw in fa_words)
                    )
                    artist_ok = match_count >= 1
            if artist_ok:
                audio_url = result['url']; quality = result.get('quality', 'unknown'); source = 'saavn'
                if not title:  title  = result.get('title', '')
                if not artist: artist = result.get('artist', '')

    if not audio_url and song_id and not title:
        title = song_id.replace('_', ' ').replace('-', ' ').strip()

    if not audio_url and title:
        for query in build_query_variants(title, artist, ''):
            result = fetch_saavn_parallel(query)
            if result and result.get('url'):
                audio_url = result['url']; quality = result.get('quality', 'unknown'); source = 'saavn'
                break

    if not audio_url and title:
        log.info(f"[Play] Saavn miss → parallel fallbacks: '{title}'")
        yt_future  = _executor.submit(fetch_from_ytdlp, title, artist)
        sc_future  = _executor.submit(fetch_from_soundcloud, title, artist)
        pip_future = _executor.submit(fetch_from_piped, title, title=title, artist=artist)
        inv_future = _executor.submit(fetch_from_invidious, title, title=title, artist=artist)

        for future in as_completed([yt_future, sc_future, pip_future, inv_future], timeout=30):
            try:
                res = future.result()
                if res and res.get('url'):
                    audio_url = res['url']; quality = res.get('quality', 'unknown'); source = res.get('source', 'unknown')
                    yt_future.cancel(); sc_future.cancel(); pip_future.cancel(); inv_future.cancel()
                    break
            except Exception: pass

    if not audio_url and title:
        for broad_query in [title, title.split()[0] if title.split() else title]:
            broad = fetch_from_ytdlp(broad_query, '')
            if broad and broad.get('url'):
                audio_url = broad['url']; quality = broad.get('quality', 'unknown'); source = 'youtube-broad'
                break

    if not audio_url:
        log.warning(f"[Play] ✗ ALL sources failed id={song_id} title='{title}'")
        return jsonify({'error': 'No audio source found'}), 404

    _executor.submit(_turso_cache_set, _play_ck, {
        'url': audio_url, 'quality': quality, 'source': source,
        'title': title, 'artist': artist, 'image': ''
    })

    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header: req_headers['Range'] = range_header

        upstream = requests.get(audio_url, headers=req_headers, stream=True, timeout=60, allow_redirects=True)
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({
            'Access-Control-Allow-Origin': '*',
            'Accept-Ranges':               'bytes',
            'Cache-Control':               'no-store',
            'X-Audio-Quality':             quality,
            'X-Audio-Source':              source,
        })
        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally: upstream.close()

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=resp_headers,
            direct_passthrough=True,
        )
    except Exception as e:
        log.error(f"[Play] Stream error: {e}")
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# fetch_from_mirror / fetch_saavn_parallel
# ═══════════════════════════════════════════════════════════════
def fetch_from_mirror(mirror, query, min_score=0.4):
    if not _mirror_ok(mirror): return None
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            r = requests.get(
                f'{mirror}{endpoint}',
                params={'query': query, 'q': query, 'limit': 10},
                timeout=8, headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200: continue
            data    = r.json()
            results = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or []
            )
            best_song, best_score, best_dur = None, -1, float('inf')
            q_normalized = normalize(query)

            for song in results:
                song_title  = song.get('name') or song.get('title', '')
                song_artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                if not has_word_match(query, song_title): continue

                score = title_score(query, song_title, song_artist)
                dur   = int(song.get('duration', 999) or 999)

                if dur > 600:  score -= 0.6
                if dur > 900:  score -= 1.0

                song_year = int(song.get('year') or 0)
                if song_year >= 2010:  score += 0.15
                elif 0 < song_year < 2000: score -= 0.25

                if song_artist:
                    artist_norm  = normalize(song_artist)
                    artist_words = [w for w in artist_norm.split() if len(w) >= 3]
                    query_words  = [w for w in q_normalized.split() if len(w) >= 3]
                    matching     = sum(
                        1 for aw in artist_words
                        if any(fuzzy_word_match(aw, qw) >= 0.80 for qw in query_words)
                    )
                    if artist_words and matching >= 1:
                        score += 0.5 * (matching / max(len(artist_words), 1))

                if score > best_score or (score == best_score and dur < best_dur):
                    best_score = score; best_song = song; best_dur = dur

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
        except Exception:
            _mirror_failed(mirror); continue
    return None

def fetch_saavn_parallel(query):
    threshold = dynamic_min_score(query)
    with _mirror_lock:
        mirrors = sorted(
            [m for m in SAAVN_MIRRORS if _mirror_ok(m)],
            key=_health_score, reverse=True
        )[:5]  # Top 5 only
    if not mirrors: mirrors = list(SAAVN_MIRRORS)[:5]
    futures     = {_executor.submit(fetch_from_mirror, m, query, threshold): m for m in mirrors}
    all_results = []
    try:
        for future in as_completed(futures, timeout=10):
            try:
                result = future.result()
                if result: all_results.append(result)
            except Exception: pass
    except Exception: pass
    if not all_results: return None

    all_results.sort(
        key=lambda r: r.get('score', 0) + (0.05 if '320' in str(r.get('quality', '')) else 0),
        reverse=True
    )
    best = all_results[0]
    log.info(f"[Parallel] ✓ '{best['title']}' score={best['score']} q={best['quality']}")
    return best

# ═══════════════════════════════════════════════════════════════
# PIPED
# ═══════════════════════════════════════════════════════════════
def fetch_from_piped(query, title='', artist=''):
    search_q = f"{title} {artist}".strip() if title else query
    with _piped_lock:
        instances = sorted(PIPED_INSTANCES, key=_health_score, reverse=True)
    fail_count = 0
    for instance in instances:
        if not _is_source_alive(instance): continue
        try:
            t0 = time.time()
            r  = requests.get(f'{instance}/search', params={'q': search_q, 'filter': 'music_songs'}, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
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
            video_id = best.get('url', '').replace('/watch?v=', '').strip()
            if not video_id: continue
            sr = requests.get(f'{instance}/streams/{video_id}', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if sr.status_code != 200: continue
            audio_streams = sr.json().get('audioStreams', [])
            if not audio_streams: continue
            best_audio = max(audio_streams, key=lambda s: s.get('bitrate', 0))
            if not best_audio.get('url'): continue
            bitrate = best_audio.get('bitrate', 0)
            _health_record_ok(instance, elapsed)
            return {
                'url':    best_audio['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title':  best.get('title', title),
                'artist': best.get('uploaderName', artist),
                'image':  best.get('thumbnail', ''),
                'source': 'piped'
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
    with _invidious_lock:
        instances = sorted(INVIDIOUS_INSTANCES, key=_health_score, reverse=True)
    fail_count = 0
    for instance in instances:
        if not _is_source_alive(instance): continue
        try:
            t0 = time.time()
            r  = requests.get(f'{instance}/api/v1/search', params={'q': search_q, 'type': 'video', 'page': 1}, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: _health_record_fail(instance); fail_count += 1; continue
            results = r.json()
            if not results: _health_record_fail(instance); fail_count += 1; continue
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
            formats       = vr.json().get('adaptiveFormats', [])
            audio_formats = [f for f in formats if f.get('type', '').startswith('audio')]
            if not audio_formats: continue
            best_fmt = max(audio_formats, key=lambda f: f.get('bitrate', 0))
            if not best_fmt.get('url'): continue
            bitrate = best_fmt.get('bitrate', 0)
            _health_record_ok(instance, elapsed)
            return {
                'url':    best_fmt['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title':  best.get('title', title),
                'artist': best.get('author', artist),
                'image':  f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
                'source': 'invidious'
            }
        except Exception as e:
            _health_record_fail(instance); fail_count += 1
            log.warning(f"[Invidious {instance}] {e}"); continue
    if fail_count >= len(instances): _maybe_reactive_heal('invidious')
    return None

# ═══════════════════════════════════════════════════════════════
# /api/songs
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q       = request.args.get('q', 'top bollywood songs').strip()
    era     = request.args.get('era', '').strip()
    is_90s  = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    cache_key = f"songs:{search_term.lower()}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, '_cached': True})

    raw = _fetch_saavn_search_parallel(search_term)
    if raw:
        normalized = _normalize_saavn_songs(raw)
        if is_90s:
            filtered   = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
            normalized = filtered if len(filtered) >= 5 else normalized
            random.shuffle(normalized)
        result = normalized[:30]
        _cache_set(cache_key, result)
        return jsonify({'results': result})

    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={'term': search_term, 'media': 'music', 'entity': 'song', 'limit': 50, 'country': 'IN'},
            timeout=15
        )
        r.raise_for_status()
        results = r.json().get('results', [])
        if is_90s:
            filtered = [s for s in results if s.get('previewUrl') and 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
            if len(filtered) < 5: filtered = [s for s in results if s.get('previewUrl')]
            random.shuffle(filtered)
            result = filtered[:30]
        else:
            result = [s for s in results if s.get('previewUrl')]
        _cache_set(cache_key, result)
        return jsonify({'results': result})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})

@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed      = random.choice(NINETIES_SEEDS)
    cache_key = f"songs:{seed.lower()}"
    cached    = _cache_get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, 'seed': seed, '_cached': True})

    raw = _fetch_saavn_search_parallel(seed)
    if raw:
        normalized = _normalize_saavn_songs(raw)
        filtered   = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        result     = (filtered if len(filtered) >= 5 else normalized)[:30]
        random.shuffle(result)
        _cache_set(cache_key, result)
        return jsonify({'results': result, 'seed': seed})

    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={'term': seed, 'media': 'music', 'entity': 'song', 'limit': 50, 'country': 'IN'},
            timeout=15
        )
        r.raise_for_status()
        results  = r.json().get('results', [])
        filtered = [s for s in results if s.get('previewUrl') and 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
        if len(filtered) < 5: filtered = [s for s in results if s.get('previewUrl')]
        random.shuffle(filtered)
        result = filtered[:30]
        _cache_set(cache_key, result)
        return jsonify({'results': result, 'seed': seed})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})

# ═══════════════════════════════════════════════════════════════
# /api/saavn
# ═══════════════════════════════════════════════════════════════
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

    _ck     = f"saavn:{normalize(q)}:{normalize(artist)}"
    _cached = _turso_cache_get(_ck)
    if _cached and not low_quality:
        log.info(f"[Cache] HIT saavn: '{q}'")
        return jsonify({'success': True, 'token': token, **_cached})

    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query)
        if result:
            if low_quality:
                low_url, low_q = _pick_low_quality(result.get('_raw_urls', []))
                if low_url: result['url'] = low_url; result['quality'] = low_q
            _executor.submit(_turso_cache_set, _ck, result)
            return jsonify({'success': True, 'token': token, **result})

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        _executor.submit(_turso_cache_set, _ck, yt)
        return jsonify({'success': True, 'token': token, **yt})

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
        _executor.submit(_turso_cache_set, _ck, sc)
        return jsonify({'success': True, 'token': token, **sc})

    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/resolve
# ═══════════════════════════════════════════════════════════════
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
                'url':    f"/api/stream?url={quote(result['url'], safe='')}",
                'quality': result['quality'], 'title': result['title'],
                'artist':  result['artist'],  'image': result.get('image', ''), 'source': 'saavn'
            })

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url':    f"/api/stream?url={quote(yt['url'], safe='')}",
            'quality': yt['quality'], 'title': yt['title'],
            'artist':  yt['artist'],  'image': yt.get('image', ''), 'source': 'youtube'
        })

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url':    f"/api/stream?url={quote(sc['url'], safe='')}",
            'quality': sc['quality'], 'title': sc['title'],
            'artist':  sc['artist'],  'image': sc.get('image', ''), 'source': 'soundcloud'
        })

    piped = fetch_from_piped(q, title=q, artist=artist)
    if piped and piped.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url':    f"/api/stream?url={quote(piped['url'], safe='')}",
            'quality': piped['quality'], 'title': piped['title'],
            'artist':  piped['artist'],  'image': piped.get('image', ''), 'source': 'piped'
        })

    inv = fetch_from_invidious(q, title=q, artist=artist)
    if inv and inv.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url':    f"/api/stream?url={quote(inv['url'], safe='')}",
            'quality': inv['quality'], 'title': inv['title'],
            'artist':  inv['artist'],  'image': inv.get('image', ''), 'source': 'invidious'
        })

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
@limiter.limit("200 per minute")
def stream_audio():
    url = request.args.get('url', '').strip()
    if not url: return jsonify({'error': 'Missing URL'}), 400
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'): return jsonify({'error': 'Invalid scheme'}), 400
        domain = parsed.netloc.lower().split(':')[0]
        if not _is_allowed_domain(domain): return jsonify({'error': 'Domain not allowed'}), 403
    except Exception: return jsonify({'error': 'Invalid URL'}), 400
    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0',
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity', 'Connection': 'keep-alive'
        }
        range_header = request.headers.get('Range')
        if range_header: req_headers['Range'] = range_header
        upstream = requests.get(url, headers=req_headers, stream=True, timeout=60, allow_redirects=True)
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers.update({'Access-Control-Allow-Origin': '*', 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store'})
        if 'content-type' not in {k.lower() for k in resp_headers}: resp_headers['Content-Type'] = 'audio/mpeg'
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally: upstream.close()
        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers, direct_passthrough=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# DOWNLOAD
# ═══════════════════════════════════════════════════════════════
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
        upstream   = requests.get(stream_url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}, stream=True, timeout=60, allow_redirects=True)
        if not upstream.ok: return jsonify({'error': f'Upstream {upstream.status_code}'}), 502
        actual_ct  = upstream.headers.get('Content-Type', content_type)
        ext        = 'webm' if 'webm' in actual_ct else ('m4a' if ('mp4' in actual_ct or 'm4a' in actual_ct) else 'mp3')
        resp_headers = {
            'Content-Disposition':       f'attachment; filename="{clean_name}.{ext}"',
            'Content-Type':              actual_ct,
            'Accept-Ranges':             'bytes',
            'Access-Control-Allow-Origin': '*'
        }
        if 'Content-Length' in upstream.headers: resp_headers['Content-Length'] = upstream.headers['Content-Length']
        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk: yield chunk
            finally: upstream.close()
        return Response(stream_with_context(generate()), status=200, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# AUTH + SYNC
# ═══════════════════════════════════════════════════════════════
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
    db_execute(
        "INSERT INTO users (google_sub, name, email, picture) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(google_sub) DO UPDATE SET name=excluded.name, email=excluded.email, picture=excluded.picture",
        [sub, profile.get('name',''), profile.get('email',''), profile.get('picture','')]
    )
    log.info(f"[Auth] User upserted: {profile.get('email', '')}")
    return jsonify({'success': True, 'sub': sub, 'name': profile.get('name','')})

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
    db_execute(
        "INSERT INTO playback_state (google_sub, song_id, song_title, artist, art_url, progress, device, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(google_sub) DO UPDATE SET song_id=excluded.song_id, song_title=excluded.song_title, "
        "artist=excluded.artist, art_url=excluded.art_url, progress=excluded.progress, device=excluded.device, updated_at=CURRENT_TIMESTAMP",
        [sub, song_id, data.get('songTitle',''), data.get('artist',''), data.get('artUrl',''), progress, device]
    )
    return jsonify({'status': 'ok'})

@app.route('/api/sync/state', methods=['GET'])
@limiter.limit("60 per minute")
def get_playback_state():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'error': 'Unauthorized'}), 401

    # Check L1 first
    l1_key    = f"sync:{sub}"
    l1_cached = _l1_get(l1_key)
    if l1_cached:
        return jsonify(l1_cached)

    result = db_execute("SELECT * FROM playback_state WHERE google_sub = ?", [sub])
    if result.rows:
        row  = result.rows[0]
        cols = [c.name for c in result.columns]
        r    = dict(zip(cols, row))
        data = {
            'success':   True,
            'songId':    r['song_id'],
            'songTitle': r['song_title'],
            'artist':    r['artist'],
            'artUrl':    r['art_url'],
            'progress':  r['progress'],
            'device':    r['device'],
            'updatedAt': r['updated_at'],
        }
        _l1_set(l1_key, data)
        return jsonify(data)
    return jsonify({'success': False})

# ═══════════════════════════════════════════════════════════════
# TV PAIRING
# ═══════════════════════════════════════════════════════════════
@app.route('/api/auth/tv-generate-code', methods=['POST'])
@limiter.limit("10 per minute")
def generate_tv_code():
    data       = request.get_json() or {}
    session_id = data.get('sessionId') or secrets.token_hex(8)
    code       = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    expiry     = (datetime.utcnow() + timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
    db_execute("DELETE FROM tv_pairing WHERE tv_session_id = ?", [session_id])
    db_execute("INSERT INTO tv_pairing (pairing_code, tv_session_id, expires_at) VALUES (?, ?, ?)", [code, session_id, expiry])
    return jsonify({'code': code, 'sessionId': session_id, 'expiresIn': 300})

@app.route('/api/auth/tv-poll')
@limiter.limit("60 per minute")
def poll_tv_pairing():
    code    = request.args.get('code', '').strip().upper()
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if not code: return jsonify({'status': 'pending'}), 400
    result = db_execute("SELECT * FROM tv_pairing WHERE pairing_code = ? AND expires_at > ?", [code, now_str])
    if not result.rows: return jsonify({'status': 'expired'})
    cols = [c.name for c in result.columns]
    row  = dict(zip(cols, result.rows[0]))
    if row.get('google_sub'):
        user_result = db_execute("SELECT * FROM users WHERE google_sub = ?", [row['google_sub']])
        db_execute("DELETE FROM tv_pairing WHERE pairing_code = ?", [code])
        if user_result.rows:
            ucols = [c.name for c in user_result.columns]
            user  = dict(zip(ucols, user_result.rows[0]))
            return jsonify({'status': 'authorized', 'user': {'sub': user['google_sub'], 'name': user['name'], 'email': user['email'], 'picture': user['picture']}})
    return jsonify({'status': 'pending'})

@app.route('/api/auth/tv-verify-mobile', methods=['POST'])
@limiter.limit("20 per minute")
def mobile_verify_tv():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data    = request.get_json() or {}
    code    = data.get('code', '').strip().upper()
    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    if not code: return jsonify({'success': False, 'error': 'Missing code'}), 400
    result = db_execute("SELECT * FROM tv_pairing WHERE pairing_code = ? AND expires_at > ?", [code, now_str])
    if not result.rows: return jsonify({'success': False, 'error': 'Invalid or expired code'}), 404
    db_execute("UPDATE tv_pairing SET google_sub = ? WHERE pairing_code = ?", [sub, code])
    return jsonify({'success': True})

# ═══════════════════════════════════════════════════════════════
# GHOST PIN
# ═══════════════════════════════════════════════════════════════
@app.route('/api/auth/verify-ghost-pin', methods=['POST'])
@limiter.limit("10 per minute")
def verify_ghost_pin():
    sub = _extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub: return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    data    = request.get_json() or {}
    pin     = data.get('pin', '').strip()
    if not pin: return jsonify({'success': False}), 400
    h_input = hashlib.sha256(pin.encode('utf-8')).hexdigest()
    result  = db_execute("SELECT ghost_pin_hash FROM users WHERE google_sub = ?", [sub])
    if not result.rows: return jsonify({'success': False}), 404
    cols = [c.name for c in result.columns]
    user = dict(zip(cols, result.rows[0]))
    if not user.get('ghost_pin_hash'):
        db_execute("UPDATE users SET ghost_pin_hash = ? WHERE google_sub = ?", [h_input, sub])
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
    result = db_execute("SELECT name, email, picture, created_at FROM users ORDER BY created_at DESC")
    cols   = [c.name for c in result.columns]
    users  = [dict(zip(cols, row)) for row in result.rows]
    return jsonify({'users': users, 'total': len(users)})

# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════
@app.route('/api/health')
def health_status():
    with _mirror_lock:    saavn_list = list(SAAVN_MIRRORS)
    with _piped_lock:     piped_list = list(PIPED_INSTANCES)
    with _invidious_lock: inv_list   = list(INVIDIOUS_INSTANCES)

    def summarize(urls):
        result = []
        for url in urls:
            with _health_lock:
                h = _source_health.get(url, {})
            fails   = h.get('fails', 0)
            last_ok = h.get('last_ok', 0)
            avg_ms  = h.get('avg_ms', 0)
            status  = 'ok' if fails < 5 else ('degraded' if fails < 10 else 'dead')
            result.append({
                'url':     url,
                'status':  status,
                'fails':   fails,
                'last_ok': round(time.time() - last_ok) if last_ok else None,
                'avg_ms':  round(avg_ms),
            })
        result.sort(key=lambda x: x['fails'])
        return result

    with _sc_client_id_lock:
        sc_id = SOUNDCLOUD_CLIENT_ID

    with _l1_lock:
        l1_size = len(_l1_cache)

    return jsonify({
        'saavn':      {'count': len(saavn_list),  'instances': summarize(saavn_list)},
        'piped':      {'count': len(piped_list),  'instances': summarize(piped_list)},
        'invidious':  {'count': len(inv_list),    'instances': summarize(inv_list)},
        'soundcloud': {'client_id_prefix': sc_id[:8] + '...' if sc_id else 'missing'},
        'l1_cache':   {'size': l1_size, 'max': _L1_MAX},
        'timestamp':  round(time.time()),
    })

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'sources': ['saavn', 'piped', 'invidious'], 'auth': 'google-oauth'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
