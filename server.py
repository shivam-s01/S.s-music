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
from urllib.parse import urlparse, quote, urlencode
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))

GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
ADMIN_KEY        = os.environ.get('ADMIN_KEY', '')
TURSO_URL        = os.environ.get('TURSO_URL', '')
TURSO_TOKEN      = os.environ.get('TURSO_TOKEN', '')

if not GOOGLE_CLIENT_ID:
    raise RuntimeError('GOOGLE_CLIENT_ID env var is required')
if not ADMIN_KEY:
    raise RuntimeError('ADMIN_KEY env var is required')

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

limiter   = Limiter(get_real_ip, app=app, default_limits=[], storage_uri="memory://")
_executor = ThreadPoolExecutor(max_workers=32)
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
# DATABASE — TURSO (email/user save only)
# ═══════════════════════════════════════════════════════════════
import asyncio

def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

def _turso_available():
    return bool(TURSO_URL and TURSO_TOKEN)

async def _turso_execute(sql, args=None):
    import libsql_client
    async with libsql_client.create_client(url=TURSO_URL, auth_token=TURSO_TOKEN) as client:
        if args:
            return await client.execute(sql, args)
        return await client.execute(sql)

def db_execute(sql, args=None):
    if not _turso_available():
        return None
    try:
        return _run_async(_turso_execute(sql, args))
    except Exception as e:
        log.warning(f'[DB] execute error: {e}')
        return None

def init_db():
    if not _turso_available():
        log.info('[DB] Turso not configured — skipping DB init')
        return
    try:
        db_execute(
            "CREATE TABLE IF NOT EXISTS users (google_sub TEXT PRIMARY KEY, name TEXT, email TEXT, picture TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        log.info('[DB] Turso users table ready')
    except Exception as e:
        log.warning(f'[DB] init error: {e}')

init_db()

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
# SELF-HEALING: SOURCE HEALTH TRACKING
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
        h['fails']     += 1
        h['last_fail']  = time.time()

def _health_score(url: str) -> float:
    with _health_lock:
        h = _source_health.get(url, {})
    fails    = h.get('fails', 0)
    last_ok  = h.get('last_ok', 0)
    avg_ms   = h.get('avg_ms', 999)
    age_ok   = time.time() - last_ok if last_ok else 9999
    score    = 100.0
    score   -= fails * 10
    score   -= min(age_ok / 60, 50)
    score   -= min(avg_ms / 100, 30)
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
# AUTO MIRROR DISCOVERY — Saavn
# ═══════════════════════════════════════════════════════════════
_DISCOVERY_PATTERNS = [
    'jiosaavn-api', 'saavn-api', 'jiosaavn', 'saavn',
    'jio-saavn', 'saavnapi', 'jiosaavnapi',
]
_DISCOVERY_SUFFIXES = [
    '', '-v2', '-v3', '-v4', '-new', '-prod', '-main',
    '-app', '-api', '-server', '-backend', '-public',
    '-open', '-free', '-node', '-express',
    '-privatecvc', '-privatecvc2', '-privatecvc3',
    '-one', '-two', '-three', '-four', '-five',
    '-six', '-seven', '-eight', '-nine', '-ten',
]
_DISCOVERY_PREFIXES = ['', 'the-', 'my-', 'open-', 'free-', 'public-']
_DISCOVERY_HOSTS    = ['.vercel.app', '.up.railway.app', '.onrender.com']

def _test_mirror_working(url: str) -> bool:
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0 = time.time()
            r  = requests.get(
                f'{url}{endpoint}',
                params={'query': 'arijit singh', 'q': 'arijit singh', 'limit': 2},
                timeout=5,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200:
                continue
            data = r.json()
            results = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or []
            )
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

    log.info(f'[Discovery] Testing {len(candidates)} Saavn candidates...')
    new_found = []
    futures = {_executor.submit(_test_mirror_working, url): url for url in candidates}
    try:
        for future in as_completed(futures, timeout=60):
            url = futures[future]
            try:
                if future.result():
                    with _mirror_lock:
                        if url not in _discovered_set:
                            _discovered_set.add(url)
                            new_found.append(url)
                            log.info(f'[Discovery] ✓ New Saavn mirror: {url}')
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
        fail_count = _mirror_fail_count.get(url, 0)
        if fail_count >= 15:
            if not _test_mirror_working(url):
                to_remove.append(url)
                log.info(f'[Discovery] Removing dead Saavn mirror: {url}')
            else:
                _mirror_fail_count[url] = 0
    if to_remove:
        with _mirror_lock:
            for url in to_remove:
                if url in SAAVN_MIRRORS:
                    SAAVN_MIRRORS.remove(url)
                _discovered_set.discard(url)
        log.info(f'[SelfHeal] {len(to_remove)} Saavn mirrors died — triggering immediate rediscovery')
        _executor.submit(_discover_mirrors)

# ═══════════════════════════════════════════════════════════════
# SELF-HEALING: PIPED INSTANCE DISCOVERY
# ═══════════════════════════════════════════════════════════════
_BASE_PIPED = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.tokhmi.xyz',
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
    'https://pipedapi.reallyaweso.me',
    'https://pipedapi.in.projectsegfau.lt',
]
PIPED_INSTANCES     = list(_BASE_PIPED)
_piped_lock         = threading.Lock()
_piped_known        = set(_BASE_PIPED)

def _test_piped_instance(url: str) -> bool:
    try:
        t0 = time.time()
        r  = requests.get(f'{url}/search', params={'q': 'arijit singh', 'filter': 'music_songs'}, timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
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
        r = requests.get('https://piped-instances.kavin.rocks/', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            instances = r.json()
            new_candidates = []
            for inst in instances:
                api_url = inst.get('api_url', '').rstrip('/')
                if api_url and api_url not in _piped_known:
                    new_candidates.append(api_url)
            log.info(f'[SelfHeal:Piped] Testing {len(new_candidates)} new instances from registry...')
            futures = {_executor.submit(_test_piped_instance, u): u for u in new_candidates}
            try:
                for future in as_completed(futures, timeout=30):
                    url = futures[future]
                    try:
                        if future.result():
                            with _piped_lock:
                                if url not in _piped_known:
                                    _piped_known.add(url)
                                    PIPED_INSTANCES.append(url)
                                    log.info(f'[SelfHeal:Piped] ✓ New instance: {url}')
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[SelfHeal:Piped] Registry fetch failed: {e}')

    dead = []
    with _piped_lock:
        current = list(PIPED_INSTANCES)
    for url in current:
        with _health_lock:
            fails = _source_health.get(url, {}).get('fails', 0)
        if fails >= 10:
            if not _test_piped_instance(url):
                dead.append(url)
                log.info(f'[SelfHeal:Piped] Removing dead: {url}')
    if dead:
        with _piped_lock:
            for url in dead:
                if url in PIPED_INSTANCES:
                    PIPED_INSTANCES.remove(url)

# ═══════════════════════════════════════════════════════════════
# SELF-HEALING: INVIDIOUS INSTANCE DISCOVERY
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
        r  = requests.get(f'{url}/api/v1/search', params={'q': 'arijit singh', 'type': 'video', 'page': 1}, timeout=7, headers={'User-Agent': 'Mozilla/5.0'})
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
        r = requests.get('https://api.invidious.io/instances.json', params={'sort_by': 'health'}, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            instances = r.json()
            new_candidates = []
            for inst in instances:
                if not isinstance(inst, list) or len(inst) < 2: continue
                data = inst[1]
                uri  = data.get('uri', '').rstrip('/')
                if not uri.startswith('https'): continue
                if not data.get('api', False): continue
                if uri not in _invidious_known:
                    new_candidates.append(uri)
            log.info(f'[SelfHeal:Invidious] Testing {len(new_candidates)} new instances...')
            futures = {_executor.submit(_test_invidious_instance, u): u for u in new_candidates[:20]}
            try:
                for future in as_completed(futures, timeout=40):
                    url = futures[future]
                    try:
                        if future.result():
                            with _invidious_lock:
                                if url not in _invidious_known:
                                    _invidious_known.add(url)
                                    INVIDIOUS_INSTANCES.append(url)
                                    log.info(f'[SelfHeal:Invidious] ✓ New instance: {url}')
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[SelfHeal:Invidious] Registry fetch failed: {e}')

    dead = []
    with _invidious_lock:
        current = list(INVIDIOUS_INSTANCES)
    for url in current:
        with _health_lock:
            fails = _source_health.get(url, {}).get('fails', 0)
        if fails >= 10:
            if not _test_invidious_instance(url):
                dead.append(url)
                log.info(f'[SelfHeal:Invidious] Removing dead: {url}')
    if dead:
        with _invidious_lock:
            for url in dead:
                if url in INVIDIOUS_INSTANCES:
                    INVIDIOUS_INSTANCES.remove(url)

# ═══════════════════════════════════════════════════════════════
# SELF-HEALING: SOUNDCLOUD CLIENT ID AUTO-REFRESH
# ═══════════════════════════════════════════════════════════════
SOUNDCLOUD_CLIENT_ID     = os.environ.get('SOUNDCLOUD_CLIENT_ID', 'a3e059563d7fd3372b49b37f00a00bcf')
_sc_client_id_lock       = threading.Lock()
_sc_client_id_last_check = 0
_SC_ID_REFRESH_INTERVAL  = 3600

def _refresh_soundcloud_client_id():
    global SOUNDCLOUD_CLIENT_ID
    try:
        r = requests.get('https://soundcloud.com', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200:
            return
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
        log.warning(f'[SelfHeal:SC] Client ID refresh failed: {e}')

def _maybe_refresh_sc_id():
    global _sc_client_id_last_check
    now = time.time()
    if now - _sc_client_id_last_check > _SC_ID_REFRESH_INTERVAL:
        _sc_client_id_last_check = now
        _executor.submit(_refresh_soundcloud_client_id)

# ═══════════════════════════════════════════════════════════════
# SELF-HEALING: MASTER HEAL LOOP
# ═══════════════════════════════════════════════════════════════
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
                except Exception as e: log.warning(f'[SelfHeal] Healer error: {e}')

            with _mirror_lock:   sm = len(SAAVN_MIRRORS)
            with _piped_lock:    pi = len(PIPED_INSTANCES)
            with _invidious_lock: iv = len(INVIDIOUS_INSTANCES)
            log.info(f'[SelfHeal] ✓ Cycle done — Saavn:{sm} Piped:{pi} Invidious:{iv}')
        except Exception as e:
            log.error(f'[SelfHeal] Master loop error: {e}')
        time.sleep(1800)

# ═══════════════════════════════════════════════════════════════
# SELF-HEALING: REACTIVE HEAL
# ═══════════════════════════════════════════════════════════════
_reactive_heal_cooldown = {}
_REACTIVE_COOLDOWN_S    = 120

def _maybe_reactive_heal(source_type: str):
    now = time.time()
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

threading.Thread(target=_master_heal_loop, daemon=True).start()
log.info('[SelfHeal] Master heal loop started')

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
# CACHE
# ═══════════════════════════════════════════════════════════════
_meta_cache     = {}
META_CACHE_TTL  = 600
_ytdlp_cache    = {}
YTDLP_CACHE_TTL = 240

def _cache_get(key, store=None):
    store = store if store is not None else _meta_cache
    entry = store.get(key)
    if not entry:
        return None
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

# ═══════════════════════════════════════════════════════════════
# FIX 1: build_query_variants — artist-first variant added
# ═══════════════════════════════════════════════════════════════
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

    # ── PATCH: put "artist title" FIRST so Saavn finds exact modern song ──
    # This is the most specific query — artist + title together = best Saavn hit
    if artist_c:
        add(f"{artist_c} {title_c}")

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
    if artist_first and title_first:
        add(f"{title_first} {artist_first}")
    if artist_c and title_first:
        add(f"{artist_c} {title_first}")
    if artist_first and len(words) > 1:
        add(f"{words[0]} {words[1]} {artist_first}")

    # Hindi transliteration variants — extra queries, zero existing logic touched
    try:
        t_translit = _hindi_translit_normalize(title_c)
        if t_translit and t_translit != title_c:
            add(t_translit)
            if artist_first: add(f"{t_translit} {artist_first}")
    except Exception:
        pass

    return variants

# ═══════════════════════════════════════════════════════════════
# HINDI TRANSLITERATION — normalize variant spellings
# ═══════════════════════════════════════════════════════════════
_HINDI_TRANSLIT = [
    # vowels
    ('aa', 'a'), ('ee', 'i'), ('oo', 'u'), ('ae', 'ai'),
    # common substitutions
    ('ph', 'f'), ('bh', 'b'), ('gh', 'g'), ('kh', 'k'),
    ('th', 't'), ('dh', 'd'), ('sh', 's'), ('ch', 'c'),
    # vowel alternates
    ('ie', 'i'), ('ey', 'ai'), ('ay', 'ai'), ('oi', 'oy'),
    ('ou', 'u'), ('ue', 'u'),
    # common spelling variants
    ('hi', 'he'), ('he', 'hi'), ('ho', 'hu'), ('hu', 'ho'),
    ('ki', 'ke'), ('ke', 'ki'), ('ko', 'ku'),
    ('na', 'nah'), ('nah', 'na'),
    ('hai', 'he'), ('hain', 'he'), ('he', 'hai'),
    ('tum', 'tum'), ('hum', 'hum'),
    ('pyar', 'pyaar'), ('pyaar', 'pyar'),
    ('dil', 'dill'), ('dill', 'dil'),
    ('ishq', 'ishk'), ('ishk', 'ishq'),
]

def _hindi_translit_normalize(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    for src, dst in _HINDI_TRANSLIT:
        t = re.sub(r'' + src + r'', dst, t)
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
# MIRROR HEALTH
# ═══════════════════════════════════════════════════════════════
_mirror_fail_count = {}
_mirror_fail_time  = {}
MIRROR_FAIL_COOLDOWN = 30

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
    dead_count = sum(1 for m in SAAVN_MIRRORS if _mirror_fail_count.get(m, 0) >= 5)
    if dead_count >= max(1, len(SAAVN_MIRRORS) // 2):
        _maybe_reactive_heal('saavn')

# ═══════════════════════════════════════════════════════════════
# SAAVN SEARCH — parallel all mirrors
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
            raw = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or []
            )
            if raw: return raw
        except Exception:
            _mirror_failed(mirror)
    return []

def _fetch_saavn_search_parallel(search_term):
    with _mirror_lock:
        mirrors = [m for m in SAAVN_MIRRORS if _mirror_ok(m)]
    if not mirrors: mirrors = list(SAAVN_MIRRORS)
    futures = {_executor.submit(_fetch_saavn_search_mirror, m, search_term): m for m in mirrors}
    try:
        for future in as_completed(futures, timeout=10):
            try:
                result = future.result()
                if result:
                    for f in futures: f.cancel()
                    return result
            except Exception: pass
    except Exception: pass
    return []

# ═══════════════════════════════════════════════════════════════
# FIX 2: _normalize_saavn_songs — filter out absurdly long tracks
# ═══════════════════════════════════════════════════════════════
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

        # ── PATCH: skip tracks over 18 min (qawwalis, classical, etc.)
        # unless nothing else matched — this is a soft pre-filter
        # 1080s = 18 minutes
        if dur_s > 1080:
            continue

        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        _, quality = pick_best_quality(raw_urls)
        if not quality: continue
        # ── PATCH: pass title + artist in previewUrl so /api/play has
        # full context for scoring — not just a bare ID lookup
        play_url = (
            f"/api/play?id={quote(song_id, safe='')}"
            f"&title={quote(title, safe='')}"
            f"&artist={quote(artist, safe='')}"
        )
        normalized.append({
            'trackId':         song_id,
            'trackName':       title,
            'artistName':      artist,
            'artworkUrl100':   image.replace('500x500', '100x100') if image else '',
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
    cached = _cache_get(cache_key, _ytdlp_cache)
    if cached:
        return cached

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
                    if not info or not info.get('entries'):
                        continue

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
                            best_score  = score
                            best_result = entry

                    if best_score >= 1.5:
                        break
                except Exception:
                    continue

            if not best_result:
                return None

            formats = best_result.get('formats', [])

            audio_formats = [
                f for f in formats
                if f.get('acodec') not in ('none', None, '')
                and f.get('url')
                and (f.get('vcodec') in ('none', None, '') or not f.get('vcodec'))
            ]
            if not audio_formats:
                audio_formats = [
                    f for f in formats
                    if f.get('acodec') not in ('none', None, '')
                    and f.get('url')
                ]
            if not audio_formats:
                audio_formats = [f for f in formats if f.get('url')]

            if not audio_formats:
                return None

            best_fmt = max(audio_formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            abr      = best_fmt.get('abr') or best_fmt.get('tbr') or 0
            quality  = f"{int(abr)}kbps" if abr else 'unknown'

            thumb = best_result.get('thumbnail', '')
            if not thumb:
                vid_id = best_result.get('id', '')
                if vid_id:
                    thumb = f"https://img.youtube.com/vi/{vid_id}/mqdefault.jpg"

            result = {
                'url':     best_fmt['url'],
                'quality': quality,
                'title':   best_result.get('title', title),
                'artist':  best_result.get('uploader', artist) or best_result.get('artist', artist),
                'image':   thumb,
                'source':  'youtube',
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
            if not info or not info.get('entries'):
                return None

            best = None; best_score = -1
            for entry in info['entries']:
                if not entry: continue
                if entry.get('duration', 0) < 60: continue
                sc_title  = entry.get('title', '')
                sc_artist = entry.get('uploader', '')
                score = title_score(title, sc_title, sc_artist)
                if score > best_score:
                    best_score = score; best = entry

            if not best or best_score < 0.20:
                return None

            formats = best.get('formats', [])
            if not formats: return None
            best_fmt = max(formats, key=lambda f: f.get('abr') or f.get('tbr') or 0)
            if not best_fmt.get('url'): return None

            abr     = best_fmt.get('abr') or best_fmt.get('tbr') or 0
            quality = f"{int(abr)}kbps" if abr else 'unknown'

            result = {
                'url':     best_fmt['url'],
                'quality': quality,
                'title':   best.get('title', title),
                'artist':  best.get('uploader', artist),
                'image':   best.get('thumbnail', ''),
                'source':  'soundcloud',
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
        mirrors = [m for m in SAAVN_MIRRORS if _mirror_ok(m)]
    if not mirrors: mirrors = list(SAAVN_MIRRORS)

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
                r = requests.get(
                    f'{mirror}{endpoint}',
                    timeout=7, headers={'User-Agent': 'Mozilla/5.0'}
                )
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
                    return {
                        'url':     best_url,
                        'quality': quality,
                        'title':   song.get('name') or song.get('title', ''),
                        'artist':  song.get('primaryArtists') or song.get('primary_artists') or '',
                        'image':   pick_image(song),
                    }
            except Exception:
                _mirror_failed(mirror)
        return None

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
        return jsonify({'error': 'Missing id or title'}), 400

    audio_url = None
    quality   = 'unknown'
    source    = 'unknown'

    if not audio_url and song_id:
        result = _fetch_saavn_by_id(song_id)
        if result and result.get('url'):
            # ── PATCH: verify the fetched song actually matches artist
            # If artist is known and result artist is completely different, skip
            # and fall through to scored title search instead
            fetched_artist = normalize(result.get('artist', ''))
            expected_artist = normalize(artist)
            artist_ok = True
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
                audio_url = result['url']
                quality   = result.get('quality', 'unknown')
                source    = 'saavn'
                if not title:  title  = result.get('title', '')
                if not artist: artist = result.get('artist', '')
                log.info(f"[Play] ✓ Saavn ID quality={quality} artist_verified={artist_ok}")
            else:
                log.info(f"[Play] ID artist mismatch: expected='{artist}' got='{result.get('artist')}' — falling to title search")

    if not audio_url and song_id and not title:
        title = song_id.replace('_', ' ').replace('-', ' ').strip()
        log.info(f"[Play] ID→title rescue: '{title}'")

    if not audio_url and title:
        for query in build_query_variants(title, artist, ''):
            result = fetch_saavn_parallel(query)
            if result and result.get('url'):
                audio_url = result['url']
                quality   = result.get('quality', 'unknown')
                source    = 'saavn'
                log.info(f"[Play] ✓ Saavn title='{result['title']}' quality={quality}")
                break

    if not audio_url and title:
        log.info(f"[Play] Saavn miss → parallel YT + SC + Piped + Invidious: '{title}'")
        yt_future  = _executor.submit(fetch_from_ytdlp, title, artist)
        sc_future  = _executor.submit(fetch_from_soundcloud, title, artist)
        pip_future = _executor.submit(fetch_from_piped, title, title=title, artist=artist)
        inv_future = _executor.submit(fetch_from_invidious, title, title=title, artist=artist)

        for future in as_completed([yt_future, sc_future, pip_future, inv_future], timeout=30):
            try:
                res = future.result()
                if res and res.get('url'):
                    audio_url = res['url']
                    quality   = res.get('quality', 'unknown')
                    source    = res.get('source', 'unknown')
                    log.info(f"[Play] ✓ {source} '{res.get('title')}' quality={quality}")
                    yt_future.cancel(); sc_future.cancel()
                    pip_future.cancel(); inv_future.cancel()
                    break
            except Exception:
                pass

    if not audio_url and title:
        # Last resort — YT broad search with no artist filter, guaranteed attempt
        log.info(f"[Play] All parallel failed → last resort yt-dlp broad: '{title}'")
        for broad_query in [title, title.split()[0] if title.split() else title]:
            broad = fetch_from_ytdlp(broad_query, '')
            if broad and broad.get('url'):
                audio_url = broad['url']
                quality   = broad.get('quality', 'unknown')
                source    = 'youtube-broad'
                log.info(f"[Play] ✓ yt-dlp broad '{broad.get('title')}' quality={quality}")
                break

    if not audio_url:
        log.warning(f"[Play] ✗ ALL sources failed id={song_id} title='{title}'")
        return jsonify({'error': 'No audio source found'}), 404

    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }
        range_header = request.headers.get('Range')
        if range_header: req_headers['Range'] = range_header

        upstream = requests.get(
            audio_url, headers=req_headers, stream=True,
            timeout=60, allow_redirects=True
        )
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
# FIX 3: fetch_from_mirror — duration penalty + artist bonus
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

            # ── PATCH: extract artist hint from query if present ──
            # The query may be "Sachet Tandon Simroon Tera Naam"
            # We use all query words for scoring but also check artist match
            q_normalized = normalize(query)

            for song in results:
                song_title  = song.get('name') or song.get('title', '')
                song_artist = song.get('primaryArtists') or song.get('primary_artists') or ''
                if not has_word_match(query, song_title): continue

                score = title_score(query, song_title, song_artist)
                dur   = int(song.get('duration', 999) or 999)

                # ── PATCH A: penalize long tracks (qawwali / classical >= 10 min) ──
                if dur > 600:
                    score -= 0.6
                # ── PATCH B: further penalize absurdly long tracks >= 15 min ──
                if dur > 900:
                    score -= 1.0

                # ── PATCH C: boost songs released 2010+ (modern Bollywood) ──
                # year field on Saavn song object
                song_year = int(song.get('year') or 0)
                if song_year >= 2010:
                    score += 0.15
                elif song_year > 0 and song_year < 2000:
                    # Slight penalty for pre-2000 when we're looking for modern songs
                    # This helps "Simroon Tera Naam 2023" beat "Naam" by NFAK 1985
                    score -= 0.25

                # ── PATCH D: artist name present in query → reward artist match ──
                # e.g. query = "Sachet Tandon Simroon Tera Naam"
                # song_artist = "Sachet Tandon" → big boost
                if song_artist:
                    artist_norm = normalize(song_artist)
                    artist_words = [w for w in artist_norm.split() if len(w) >= 3]
                    query_words  = [w for w in q_normalized.split() if len(w) >= 3]
                    matching_artist_words = sum(
                        1 for aw in artist_words
                        if any(fuzzy_word_match(aw, qw) >= 0.80 for qw in query_words)
                    )
                    if artist_words and matching_artist_words >= 1:
                        # At least one artist word matched in query — strong signal
                        score += 0.5 * (matching_artist_words / max(len(artist_words), 1))

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
            _mirror_failed(mirror)
            continue
    return None

# ═══════════════════════════════════════════════════════════════
# FIX 4: fetch_saavn_parallel — year-aware reranking
# ═══════════════════════════════════════════════════════════════
def fetch_saavn_parallel(query):
    threshold = dynamic_min_score(query)
    with _mirror_lock:
        mirrors = [m for m in SAAVN_MIRRORS if _mirror_ok(m)]
    if not mirrors: mirrors = list(SAAVN_MIRRORS)
    futures     = {_executor.submit(fetch_from_mirror, m, query, threshold): m for m in mirrors}
    all_results = []
    try:
        for future in as_completed(futures, timeout=12):
            try:
                result = future.result()
                if result: all_results.append(result)
            except Exception: pass
    except Exception: pass
    if not all_results: return None

    # ── PATCH: sort by score (already includes duration + year penalties from fetch_from_mirror)
    # Additional 320kbps quality tiebreaker stays intact
    all_results.sort(
        key=lambda r: r.get('score', 0) + (0.05 if '320' in str(r.get('quality', '')) else 0),
        reverse=True
    )
    best = all_results[0]
    log.info(f"[Parallel] ✓ '{best['title']}' score={best['score']} quality={best['quality']}")
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
            r  = requests.get(
                f'{instance}/search',
                params={'q': search_q, 'filter': 'music_songs'},
                timeout=8, headers={'User-Agent': 'Mozilla/5.0'}
            )
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200:
                _health_record_fail(instance); fail_count += 1; continue
            results = r.json().get('items', [])
            if not results:
                _health_record_fail(instance); fail_count += 1; continue
            best = None; best_score = -1
            for item in results[:5]:
                if item.get('type') != 'stream': continue
                if not has_word_match(query, item.get('title', '')): continue
                score = title_score(query, item.get('title', ''), item.get('uploaderName', ''))
                if score > best_score: best_score = score; best = item
            if not best or best_score < 0.3:
                continue
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
                'url': best_audio['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title': best.get('title', title),
                'artist': best.get('uploaderName', artist),
                'image': best.get('thumbnail', ''),
                'source': 'piped'
            }
        except Exception as e:
            _health_record_fail(instance)
            fail_count += 1
            log.warning(f"[Piped {instance}] {e}"); continue
    if fail_count >= len(instances):
        _maybe_reactive_heal('piped')
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
            r  = requests.get(
                f'{instance}/api/v1/search',
                params={'q': search_q, 'type': 'video', 'page': 1},
                timeout=8, headers={'User-Agent': 'Mozilla/5.0'}
            )
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200:
                _health_record_fail(instance); fail_count += 1; continue
            results = r.json()
            if not results:
                _health_record_fail(instance); fail_count += 1; continue
            best = None; best_score = -1
            for item in results[:5]:
                if not has_word_match(query, item.get('title', '')): continue
                score = title_score(query, item.get('title', ''), item.get('author', ''))
                if score > best_score: best_score = score; best = item
            if not best or best_score < 0.3: continue
            video_id = best.get('videoId', '')
            if not video_id: continue
            vr = requests.get(
                f'{instance}/api/v1/videos/{video_id}',
                params={'fields': 'adaptiveFormats,title,author'},
                timeout=10, headers={'User-Agent': 'Mozilla/5.0'}
            )
            if vr.status_code != 200: continue
            formats = vr.json().get('adaptiveFormats', [])
            audio_formats = [f for f in formats if f.get('type', '').startswith('audio')]
            if not audio_formats: continue
            best_fmt = max(audio_formats, key=lambda f: f.get('bitrate', 0))
            if not best_fmt.get('url'): continue
            bitrate = best_fmt.get('bitrate', 0)
            _health_record_ok(instance, elapsed)
            return {
                'url': best_fmt['url'],
                'quality': f"{bitrate // 1000}kbps" if bitrate > 0 else 'unknown',
                'title': best.get('title', title),
                'artist': best.get('author', artist),
                'image': f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg",
                'source': 'invidious'
            }
        except Exception as e:
            _health_record_fail(instance)
            fail_count += 1
            log.warning(f"[Invidious {instance}] {e}"); continue
    if fail_count >= len(instances):
        _maybe_reactive_heal('invidious')
    return None

# ═══════════════════════════════════════════════════════════════
# /api/songs
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q      = request.args.get('q', 'top bollywood songs').strip()
    era    = request.args.get('era', '').strip()
    is_90s = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    cache_key = f"songs:{search_term.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({'results': cached, '_cached': True})

    raw = _fetch_saavn_search_parallel(search_term)
    if raw:
        normalized = _normalize_saavn_songs(raw)
        if is_90s:
            filtered = [s for s in normalized if 1990 <= _safe_year(s.get('releaseDate')) <= 1999]
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

    for query in build_query_variants(q, artist, fallback):
        result = fetch_saavn_parallel(query)
        if result:
            if low_quality:
                low_url, low_q = _pick_low_quality(result.get('_raw_urls', []))
                if low_url: result['url'] = low_url; result['quality'] = low_q
            return jsonify({'success': True, 'token': token, **result})

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        return jsonify({'success': True, 'token': token, **yt})

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
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
                'url': f"/api/stream?url={quote(result['url'], safe='')}",
                'quality': result['quality'], 'title': result['title'],
                'artist': result['artist'], 'image': result.get('image', ''), 'source': 'saavn'
            })

    yt = fetch_from_ytdlp(q, artist)
    if yt and yt.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url': f"/api/stream?url={quote(yt['url'], safe='')}",
            'quality': yt['quality'], 'title': yt['title'],
            'artist': yt['artist'], 'image': yt.get('image', ''), 'source': 'youtube'
        })

    sc = fetch_from_soundcloud(q, artist)
    if sc and sc.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url': f"/api/stream?url={quote(sc['url'], safe='')}",
            'quality': sc['quality'], 'title': sc['title'],
            'artist': sc['artist'], 'image': sc.get('image', ''), 'source': 'soundcloud'
        })

    piped = fetch_from_piped(q, title=q, artist=artist)
    if piped and piped.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url': f"/api/stream?url={quote(piped['url'], safe='')}",
            'quality': piped['quality'], 'title': piped['title'],
            'artist': piped['artist'], 'image': piped.get('image', ''), 'source': 'piped'
        })

    inv = fetch_from_invidious(q, title=q, artist=artist)
    if inv and inv.get('url'):
        return jsonify({
            'success': True, 'token': token,
            'url': f"/api/stream?url={quote(inv['url'], safe='')}",
            'quality': inv['quality'], 'title': inv['title'],
            'artist': inv['artist'], 'image': inv.get('image', ''), 'source': 'invidious'
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
        if not _is_allowed_domain(domain):
            return jsonify({'error': 'Domain not allowed'}), 403
    except Exception: return jsonify({'error': 'Invalid URL'}), 400
    try:
        req_headers = {
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
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

    if not stream_url: return jsonify({'error': 'Song not found'}), 404

    try:
        clean_name = re.sub(r'[/\\?%*:|"<>]', '-', filename_base)
        upstream   = requests.get(stream_url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}, stream=True, timeout=60, allow_redirects=True)
        if not upstream.ok: return jsonify({'error': f'Upstream {upstream.status_code}'}), 502
        actual_ct  = upstream.headers.get('Content-Type', content_type)
        ext        = 'webm' if 'webm' in actual_ct else ('m4a' if ('mp4' in actual_ct or 'm4a' in actual_ct) else 'mp3')
        resp_headers = {
            'Content-Disposition': f'attachment; filename="{clean_name}.{ext}"',
            'Content-Type': actual_ct, 'Accept-Ranges': 'bytes',
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
# /api/health
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
                'url':      url,
                'status':   status,
                'fails':    fails,
                'last_ok':  round(time.time() - last_ok) if last_ok else None,
                'avg_ms':   round(avg_ms),
            })
        result.sort(key=lambda x: x['fails'])
        return result

    with _sc_client_id_lock:
        sc_id = SOUNDCLOUD_CLIENT_ID

    return jsonify({
        'saavn':      {'count': len(saavn_list),  'instances': summarize(saavn_list)},
        'piped':      {'count': len(piped_list),  'instances': summarize(piped_list)},
        'invidious':  {'count': len(inv_list),    'instances': summarize(inv_list)},
        'soundcloud': {'client_id_prefix': sc_id[:8] + '...' if sc_id else 'missing'},
        'timestamp':  round(time.time()),
    })

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
        "INSERT INTO users (google_sub, name, email, picture) VALUES (?, ?, ?, ?) ON CONFLICT(google_sub) DO UPDATE SET name=excluded.name, email=excluded.email, picture=excluded.picture",
        [sub, profile.get('name',''), profile.get('email',''), profile.get('picture','')]
    )
    log.info(f"[Auth] User upserted: {profile.get('email', '')}")
    return jsonify({'success': True, 'sub': sub, 'name': profile.get('name','')})

# ═══════════════════════════════════════════════════════════════
# ADMIN — View registered users
# ═══════════════════════════════════════════════════════════════
@app.route('/api/admin/users')
@limiter.limit("10 per minute")
def admin_users():
    secret = request.args.get('key', '')
    if not hmac.compare_digest(secret, ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    if not _turso_available():
        return jsonify({'users': [], 'total': 0, 'note': 'Turso not configured'})
    result = db_execute("SELECT name, email, picture, created_at FROM users ORDER BY created_at DESC")
    if not result:
        return jsonify({'users': [], 'total': 0})
    cols   = [c.name for c in result.columns]
    users  = [dict(zip(cols, row)) for row in result.rows]
    return jsonify({'users': users, 'total': len(users)})

# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'sources': ['saavn', 'piped', 'invidious'], 'auth': 'google-oauth'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
