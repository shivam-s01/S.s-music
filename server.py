"""
╔══════════════════════════════════════════════════════════════════════╗
║   GODMODE MUSIC STREAMING ENGINE  v4.0  —  PRODUCTION EDITION       ║
║   JioSaavn + YouTube/yt-dlp fallback • Rate Limiting • Gevent-safe  ║
║   Gunicorn/gevent ready • Single cleanup thread • Global executor   ║
╚══════════════════════════════════════════════════════════════════════╝

Production run:
    gunicorn -c gunicorn.conf.py app:app

Dev run:
    python app.py
"""

import os, re, time, json, hashlib, logging, threading, ipaddress, socket
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, send_file, Response, stream_with_context

# ── Optional: yt-dlp ─────────────────────────────────────────────────
try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

# ── Optional: Redis ──────────────────────────────────────────────────
REDIS_CLIENT = None
try:
    import redis as _redis_lib
    _rc = _redis_lib.from_url(
        os.environ.get('REDIS_URL', 'redis://localhost:6379/0'),
        decode_responses=True, socket_connect_timeout=2
    )
    _rc.ping()
    REDIS_CLIENT = _rc
    print('[startup] Redis connected')
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('godmode')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)


# ════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════
SAAVN_MIRRORS = [
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
]

QUALITY_ORDER        = ['320kbps', '160kbps', '96kbps', '48kbps', '12kbps']
STREAM_CHUNK         = 65_536   # 64 KB — better throughput than 32 KB
SEARCH_TTL           = 300      # 5 min  — search result cache
URL_ALIVE_TTL        = 60       # 1 min  — URL alive-check cache
YT_URL_TTL           = 18_000   # 5 hrs  — yt-dlp URL cache (YouTube link ~6h)
MIRROR_BAN_TTL       = 120      # 2 min  — backoff window after mirror failure
MAX_MIRRORS_PER_PASS = 3        # Only top-N mirrors queried per search round

# ── Global thread pool — reused; no per-request pool creation overhead ──
# I/O-bound work — 16 workers handles ~50 concurrent search bursts fine.
_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix='gm')

# ── Single persistent HTTP session with connection pool ──────────────
SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,   # parallel host pools
    pool_maxsize=30,       # sockets per host
    max_retries=0          # we handle retries manually
)
SESSION.mount('http://',  _adapter)
SESSION.mount('https://', _adapter)


# ════════════════════════════════════════════════════════════════════
# SSRF PROTECTION
# Blocks /api/stream from being used to probe internal services,
# cloud metadata endpoints, or any non-CDN/non-audio host.
# ════════════════════════════════════════════════════════════════════

# Trusted hostname suffixes for the stream proxy (strict allowlist).
# Saavn CDN, YouTube streaming, and the mirror API hosts only.
_TRUSTED_STREAM_SUFFIXES = (
    '.googlevideo.com',     # YouTube media delivery
    '.youtube.com',
    '.ytimg.com',
    '.ggpht.com',           # Google user-content
    '.jiosaavn.com',        # JioSaavn CDN
    '.saavncdn.com',
    '.cdnsaavnimg.com',
    '.akamaized.net',       # Akamai (used by JioSaavn)
    '.akamaihd.net',
    '.vercel.app',          # Saavn mirror APIs
    'saavn.dev',            # bare domain — matched as exact or suffix
)

# All private / reserved IPv4+IPv6 ranges. Any resolved IP in these → blocked.
_BLOCKED_NETS = [
    ipaddress.ip_network('127.0.0.0/8'),      # loopback
    ipaddress.ip_network('10.0.0.0/8'),       # RFC 1918
    ipaddress.ip_network('172.16.0.0/12'),    # RFC 1918
    ipaddress.ip_network('192.168.0.0/16'),   # RFC 1918
    ipaddress.ip_network('169.254.0.0/16'),   # link-local / cloud metadata (AWS/GCP/Azure)
    ipaddress.ip_network('0.0.0.0/8'),        # this-network
    ipaddress.ip_network('100.64.0.0/10'),    # carrier-grade NAT
    ipaddress.ip_network('192.0.0.0/24'),     # IETF protocol
    ipaddress.ip_network('198.18.0.0/15'),    # benchmarking
    ipaddress.ip_network('240.0.0.0/4'),      # reserved (class E)
    ipaddress.ip_network('::1/128'),          # IPv6 loopback
    ipaddress.ip_network('fc00::/7'),         # IPv6 unique-local
    ipaddress.ip_network('fe80::/10'),        # IPv6 link-local
]


def _ip_is_private(addr_str):
    """Return True if addr_str falls inside any blocked network range."""
    try:
        ip = ipaddress.ip_address(addr_str)
        return any(ip in net for net in _BLOCKED_NETS)
    except ValueError:
        return True   # unparseable → treat as unsafe


def ssrf_check(url, require_trusted_domain=False):
    """
    Validate a URL before making any outbound request.

    Returns (ok: bool, reason: str | None).

    Checks (in order):
      1. Scheme must be 'https'  — blocks file://, ftp://, http://, etc.
      2. Hostname must be present and non-empty.
      3. [optional] Hostname suffix must be in _TRUSTED_STREAM_SUFFIXES.
      4. ALL resolved IPs must be public (blocks loopback, RFC1918,
         cloud-metadata 169.254.169.254, etc.)
    """
    if not url:
        return False, 'empty url'

    try:
        parsed = urlparse(url)
    except Exception:
        return False, 'unparseable url'

    # 1. Scheme
    if parsed.scheme != 'https':
        return False, 'disallowed scheme: {!r}'.format(parsed.scheme)

    host = (parsed.hostname or '').lower().strip()
    if not host:
        return False, 'missing hostname'

    # 2. Trusted-domain allowlist (stream proxy only)
    if require_trusted_domain:
        trusted = any(
            host == s.lstrip('.') or host.endswith(s)
            for s in _TRUSTED_STREAM_SUFFIXES
        )
        if not trusted:
            log.warning('[SSRF] Blocked untrusted domain: %s', host)
            return False, 'domain not in allowlist: {}'.format(host)

    # 3. DNS resolution — check every returned address
    try:
        addr_infos = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, 'dns error: {}'.format(exc)

    if not addr_infos:
        return False, 'dns returned no addresses'

    for info in addr_infos:
        resolved_ip = info[4][0]
        if _ip_is_private(resolved_ip):
            log.warning('[SSRF] Blocked private IP %s for host %s', resolved_ip, host)
            return False, 'private/reserved IP blocked: {}'.format(resolved_ip)

    return True, None


# ════════════════════════════════════════════════════════════════════
# RATE LIMITER — Token Bucket, per-IP, no Redis required
# ════════════════════════════════════════════════════════════════════
class RateLimiter:
    """
    Sliding token-bucket limiter.
    Allows short bursts but throttles sustained abuse.
    Zero external dependencies; ~O(1) per check.
    """
    def __init__(self, rate=10, burst=20, window=60):
        self._store  = {}          # ip -> {'tokens': float, 'last': float}
        self._lock   = threading.Lock()
        self.rate    = rate        # sustained tokens per window
        self.burst   = burst       # max bucket size
        self.window  = window      # refill period (seconds)
        threading.Thread(target=self._cleanup, daemon=True).start()

    def is_allowed(self, ip):
        now = time.time()
        with self._lock:
            b = self._store.get(ip)
            if b is None:
                self._store[ip] = {'tokens': self.burst - 1, 'last': now}
                return True
            elapsed      = now - b['last']
            b['tokens']  = min(self.burst, b['tokens'] + elapsed * (self.rate / self.window))
            b['last']    = now
            if b['tokens'] >= 1:
                b['tokens'] -= 1
                return True
            return False

    def _cleanup(self):
        """Remove stale IPs every 5 min to bound memory."""
        while True:
            time.sleep(300)
            cutoff = time.time() - 600
            with self._lock:
                dead = [ip for ip, b in self._store.items() if b['last'] < cutoff]
                for ip in dead:
                    del self._store[ip]


# 10 searches/min sustained, burst 20 — enough for real users, blocks scrapers
_search_limiter = RateLimiter(rate=10, burst=20, window=60)
# 30 stream opens/min, burst 50 — one user can seek many times
_stream_limiter = RateLimiter(rate=30, burst=50, window=60)


def _client_ip():
    """Extract real client IP, respecting reverse-proxy headers."""
    xff = request.headers.get('X-Forwarded-For', '')
    return xff.split(',')[0].strip() if xff else (request.remote_addr or '0.0.0.0')


# ════════════════════════════════════════════════════════════════════
# UNIFIED TTL CACHE — in-memory + optional Redis back-end
# Single background cleanup thread for ALL cache instances (was 3 threads)
# ════════════════════════════════════════════════════════════════════
class TTLCache:
    """
    Thread-safe TTL key-value store.
    All instances share ONE cleanup thread — class-level coordination.
    """
    _instances    = []
    _global_lock  = threading.Lock()
    _cleanup_up   = False

    def __init__(self, default_ttl=300, namespace=''):
        self._store      = {}
        self._lock       = threading.Lock()
        self.default_ttl = default_ttl
        self.ns          = namespace
        with TTLCache._global_lock:
            TTLCache._instances.append(self)
            if not TTLCache._cleanup_up:
                TTLCache._cleanup_up = True
                threading.Thread(target=TTLCache._cleanup_all, daemon=True).start()

    @staticmethod
    def _cleanup_all():
        """Periodic eviction across all caches — runs every 90 s."""
        while True:
            time.sleep(90)
            now = time.time()
            with TTLCache._global_lock:
                instances = list(TTLCache._instances)
            for cache in instances:
                with cache._lock:
                    stale = [k for k, v in cache._store.items() if now > v['exp']]
                    for k in stale:
                        del cache._store[k]

    def _k(self, key):
        return '{}:{}'.format(self.ns, key) if self.ns else key

    def get(self, key):
        rk = self._k(key)
        if REDIS_CLIENT:
            try:
                v = REDIS_CLIENT.get(rk)
                if v:
                    return json.loads(v)
            except Exception:
                pass
        with self._lock:
            e = self._store.get(rk)
            if not e:
                return None
            if time.time() > e['exp']:
                del self._store[rk]
                return None
            return e['v']

    def set(self, key, value, ttl=None):
        rk  = self._k(key)
        ttl = ttl or self.default_ttl
        if REDIS_CLIENT:
            try:
                REDIS_CLIENT.setex(rk, ttl, json.dumps(value, default=str))
            except Exception:
                pass
        with self._lock:
            self._store[rk] = {'v': value, 'exp': time.time() + ttl}

    def delete(self, key):
        rk = self._k(key)
        if REDIS_CLIENT:
            try:
                REDIS_CLIENT.delete(rk)
            except Exception:
                pass
        with self._lock:
            self._store.pop(rk, None)

    def flush(self):
        if REDIS_CLIENT:
            try:
                # Only flush keys in our namespace to be safe
                pattern = '{}:*'.format(self.ns) if self.ns else '*'
                cursor, keys = REDIS_CLIENT.scan(0, match=pattern, count=200)
                if keys:
                    REDIS_CLIENT.delete(*keys)
            except Exception:
                pass
        with self._lock:
            self._store.clear()

    def size(self):
        with self._lock:
            return len(self._store)


search_cache = TTLCache(default_ttl=SEARCH_TTL,   namespace='search')
url_cache    = TTLCache(default_ttl=URL_ALIVE_TTL, namespace='url')
yt_cache     = TTLCache(default_ttl=YT_URL_TTL,    namespace='yt')


# ════════════════════════════════════════════════════════════════════
# MIRROR HEALTH TRACKER
# ════════════════════════════════════════════════════════════════════
class MirrorHealth:
    """Tracks per-mirror success rate + latency for smart routing."""

    def __init__(self):
        self._d    = defaultdict(lambda: {'ok': 0, 'fail': 0, 'lat': [], 'ban_until': 0})
        self._lock = threading.Lock()

    def record(self, mirror, success, latency_ms=0):
        with self._lock:
            d = self._d[mirror]
            if success:
                d['ok'] += 1
                d['lat'].append(latency_ms)
                if len(d['lat']) > 20:          # Keep last 20 samples (was 30)
                    d['lat'] = d['lat'][-20:]
            else:
                d['fail'] += 1
                d['ban_until'] = time.time() + MIRROR_BAN_TTL

    def is_banned(self, mirror):
        with self._lock:
            return time.time() < self._d[mirror]['ban_until']

    def score(self, mirror):
        with self._lock:
            d     = self._d[mirror]
            total = d['ok'] + d['fail']
            if total == 0:
                return 0.5
            sr      = d['ok'] / total
            avg_lat = sum(d['lat']) / len(d['lat']) if d['lat'] else 1000
            lat_s   = max(0.0, 1.0 - avg_lat / 8000)
            return round(sr * 0.7 + lat_s * 0.3, 4)

    def ranked(self, mirrors):
        avail  = [m for m in mirrors if not self.is_banned(m)]
        banned = [m for m in mirrors if     self.is_banned(m)]
        avail.sort(key=lambda m: self.score(m), reverse=True)
        return avail + banned

    def stats(self):
        with self._lock:
            return {
                m: {
                    'score':  self.score(m),
                    'banned': self.is_banned(m),
                    'ok':     self._d[m]['ok'],
                    'fail':   self._d[m]['fail'],
                }
                for m in SAAVN_MIRRORS
            }


mirror_health = MirrorHealth()


# ════════════════════════════════════════════════════════════════════
# TEXT UTILITIES
# ════════════════════════════════════════════════════════════════════
def normalize(text):
    text = (text or '').lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def normalize_loose(text):
    """Collapse common Hindi transliteration variants."""
    text = normalize(text)
    text = re.sub(r'aa', 'a', text)
    text = re.sub(r'ee', 'i', text)
    text = re.sub(r'oo', 'u', text)
    text = re.sub(r'([a-z])\1+', r'\1', text)
    return text.strip()

def clean_query(text):
    text = re.sub(
        r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)',
        '', text, flags=re.IGNORECASE)
    text = re.sub(
        r'\((OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|remaster.*?|remix.*?)\)',
        '', text, flags=re.IGNORECASE)
    text = re.sub(r'- (remaster|remix|live|acoustic|radio edit).*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def extract_movie(raw_title):
    m = re.search(
        r'\(From\s+["\u201c\u201d\u2018\u2019]?(.+?)["\u201c\u201d\u2018\u2019]?\)',
        raw_title, re.IGNORECASE)
    return m.group(1).strip() if m else ''

def primary_artist(artist_str):
    return re.split(r'[&,]|feat\.|ft\.', artist_str, flags=re.IGNORECASE)[0].strip()


# ════════════════════════════════════════════════════════════════════
# SCORING
# ════════════════════════════════════════════════════════════════════
def text_sim(a, b):
    def _sim(qa, tb):
        qw, tw = qa.split(), tb.split()
        qs, ts = set(qw), set(tw)
        if not qs or not ts:
            return 0.0
        if qa == tb:
            return 1.0
        inter    = len(qs & ts)
        union    = len(qs | ts)
        jaccard  = inter / union if union else 0
        coverage = inter / len(qs)
        seq      = 0.0
        for n in range(min(5, len(qw)), 1, -1):
            if ' '.join(qw[:n]) in tb:
                seq = 0.12 * n
                break
        penalty = min(len(ts - qs) * 0.04, 0.25)
        return max(0.0, jaccard * 0.35 + coverage * 0.35 + seq - penalty)
    return round(max(
        _sim(normalize(a),       normalize(b)),
        _sim(normalize_loose(a), normalize_loose(b))
    ), 4)

def duration_score(itunes_s, saavn_s):
    if itunes_s <= 0 or saavn_s <= 0:
        return None
    d = abs(itunes_s - saavn_s)
    if d <= 2:  return 1.00
    if d <= 5:  return 0.75
    if d <= 10: return 0.40
    if d <= 20: return 0.10
    return 0.0

def composite_score(clean_title, clean_artist, duration_sec, album, year, song):
    s_title  = song.get('name') or song.get('title', '')
    s_artist = song.get('primaryArtists') or song.get('primary_artists', '')
    s_dur    = int(song.get('duration') or 0)
    alb      = song.get('album', '')
    s_album  = alb.get('name', '') if isinstance(alb, dict) else (alb or '')
    s_year   = str(song.get('year') or '')

    t     = text_sim(clean_title, s_title)
    a     = text_sim(clean_artist, s_artist)
    d     = duration_score(duration_sec, s_dur)
    alb_s = text_sim(album, s_album) * 0.5 if album and s_album else 0.0
    yr_s  = 0.10 if (year and s_year and str(year) in s_year) else 0.0

    base = (t * 0.28 + a * 0.17 + d * 0.45) if d is not None else (t * 0.55 + a * 0.30)
    return round(base + alb_s * 0.07 + yr_s * 0.03, 4)


# ════════════════════════════════════════════════════════════════════
# QUERY BUILDER
# ════════════════════════════════════════════════════════════════════
def build_queries(raw_title, raw_artist, collection=''):
    movie       = extract_movie(raw_title)
    ct          = clean_query(raw_title)
    ca          = clean_query(raw_artist)
    pa          = primary_artist(ca)
    clean_album = clean_query(collection) if collection else ''
    t_words     = ct.split()
    queries     = []

    if movie:
        cm = clean_query(movie)
        queries += ['{} {}'.format(ct, cm),
                    '{} {} {}'.format(ct, cm, pa),
                    '{} {} {}'.format(ct, pa, cm)]

    queries += ['{} {}'.format(ct, pa),
                '{} {}'.format(ct, ca),
                '{} {}'.format(pa, ct),
                ct]

    if len(t_words) > 4:
        short = ' '.join(t_words[:4])
        queries += ['{} {}'.format(short, pa), short]
    if len(t_words) > 2:
        queries += [
            '{} {}'.format(' '.join(t_words[:3]), pa),
            '{} {}'.format(' '.join(t_words[:2]), pa),
        ]

    if clean_album:
        queries += ['{} {}'.format(ct, clean_album),
                    '{} {}'.format(clean_album, ct)]

    ct_loose = normalize_loose(ct)
    pa_loose = normalize_loose(pa)
    if ct_loose != normalize(ct):
        queries += ['{} {}'.format(ct_loose, pa_loose), ct_loose]

    seen, unique = set(), []
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


# ════════════════════════════════════════════════════════════════════
# URL VALIDATION
# ════════════════════════════════════════════════════════════════════
def is_url_alive(url, timeout=4):
    """HEAD-probe an audio URL; falls back to tiny GET for strict servers."""
    if not url or url.startswith('/api/'):
        return True  # Internal proxy — always trust

    # SSRF guard: never probe internal/private addresses
    ok, reason = ssrf_check(url, require_trusted_domain=False)
    if not ok:
        log.debug('[URL-alive] SSRF blocked: %s', reason)
        return False

    cache_key = 'alive:' + hashlib.md5(url.encode()).hexdigest()
    cached    = url_cache.get(cache_key)
    if cached is not None:
        return cached

    alive = False
    try:
        r  = SESSION.head(url, timeout=timeout, allow_redirects=True,
                          headers={'User-Agent': 'Mozilla/5.0'})
        ct = r.headers.get('Content-Type', '').lower()
        alive = r.status_code in (200, 206) and any(
            x in ct for x in ('audio', 'octet', 'mpeg', 'mp4', 'webm', 'ogg')
        )
        if not alive and r.status_code in (403, 405):
            # HEAD not allowed — tiny ranged GET
            r2 = SESSION.get(url, timeout=timeout, allow_redirects=True,
                             headers={'User-Agent': 'Mozilla/5.0',
                                      'Range': 'bytes=0-1023'},
                             stream=True)
            r2.close()
            alive = r2.status_code in (200, 206)
    except Exception:
        alive = False

    url_cache.set(cache_key, alive, ttl=URL_ALIVE_TTL if alive else 20)
    return alive


# ════════════════════════════════════════════════════════════════════
# JIOSAAVN ENGINE
# ════════════════════════════════════════════════════════════════════
def get_best_url(urls):
    if not urls:
        return None, None
    url_map = {}
    for item in urls:
        q = (item.get('quality') or '').strip().lower()
        u = item.get('url') or item.get('link') or ''
        if u:
            url_map[q] = u
    for preferred in QUALITY_ORDER:
        if preferred in url_map:
            return url_map[preferred], preferred
    for item in reversed(urls):
        u = item.get('url') or item.get('link') or ''
        if u:
            return u, item.get('quality', 'unknown')
    return None, None


def _fetch_from_mirror(mirror, query, clean_title, clean_artist, duration_sec, album, year):
    """Hit one mirror with one query. Returns list of scored candidates."""
    endpoints  = ['/api/search/songs', '/api/search', '/search/songs']
    candidates = []
    t0         = time.time()

    for ep in endpoints:
        try:
            r = SESSION.get(
                '{}{}'.format(mirror, ep),
                params={'query': query, 'q': query, 'limit': 10},
                timeout=6,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200:
                continue

            data    = r.json()
            results = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or []
            )
            if not results:
                continue

            mirror_health.record(mirror, True, (time.time() - t0) * 1000)

            for song in results:
                score    = composite_score(
                    clean_title, clean_artist, duration_sec, album, year, song)
                urls     = song.get('downloadUrl') or song.get('download_url') or []
                best_url, best_quality = get_best_url(urls)
                if best_url:
                    candidates.append({
                        'url':     best_url,
                        'quality': best_quality,
                        'title':   song.get('name') or song.get('title', ''),
                        'artist':  song.get('primaryArtists') or song.get('primary_artists', ''),
                        'score':   score,
                        'source':  'jiosaavn',
                        '_dur':    int(song.get('duration') or 0),
                        '_mirror': mirror,
                        '_query':  query,
                    })
            break  # Got results — skip remaining endpoints

        except Exception as e:
            mirror_health.record(mirror, False)
            log.debug('[Saavn] {}{}: {}'.format(mirror, ep, e))

    return candidates


def saavn_search(queries, clean_title, clean_artist,
                 duration_sec, album, year, min_score=0.30):
    """
    Optimised parallel Saavn search:
      • Only top-N mirrors per round (default 3) — caps max concurrency
      • Global executor — no per-request ThreadPoolExecutor overhead
      • Early exit when a high-confidence match appears mid-flight
    """
    ckey   = hashlib.md5('{}|{}|{}'.format(
        clean_title, clean_artist, duration_sec).encode()).hexdigest()
    cached = search_cache.get(ckey)
    if cached and is_url_alive(cached.get('url', '')):
        log.info('[Saavn] Cache hit: "{}"'.format(clean_title))
        return cached

    ranked_mirrors = mirror_health.ranked(SAAVN_MIRRORS)
    top_mirrors    = ranked_mirrors[:MAX_MIRRORS_PER_PASS]
    top_queries    = queries[:6]                            # cap blast radius

    jobs    = [(m, q) for q in top_queries for m in top_mirrors]
    futures = {
        _EXECUTOR.submit(_fetch_from_mirror, m, q, clean_title,
                         clean_artist, duration_sec, album, year): (m, q)
        for m, q in jobs
    }

    all_candidates = []
    done_count     = 0
    deadline       = time.time() + 10

    for fut in as_completed(futures, timeout=10):
        try:
            candidates = fut.result()
            all_candidates.extend(candidates)
            done_count += 1

            # Early exit: stop waiting once 3+ futures done AND confident match exists
            if done_count >= 3 and any(c['score'] >= 0.70 for c in all_candidates):
                for f in futures:
                    if not f.done():
                        f.cancel()
                break
        except Exception:
            pass

        if time.time() > deadline:
            break

    if not all_candidates:
        return None

    all_candidates.sort(key=lambda x: x['score'], reverse=True)

    for c in all_candidates[:7]:
        if c['score'] < min_score:
            break
        if is_url_alive(c['url']):
            result = {k: v for k, v in c.items() if not k.startswith('_')}
            search_cache.set(ckey, result, ttl=SEARCH_TTL)
            log.info(
                '[Saavn OK] score={} quality={} dur_diff={}s mirror={} "{}" - "{}"'.format(
                    c['score'], c['quality'],
                    abs(duration_sec - c['_dur']),
                    c['_mirror'].split('/')[2],
                    c['title'], c['artist']
                )
            )
            return result
        log.debug('[Saavn] Dead URL skipped score={} "{}"'.format(c['score'], c['title']))

    return None


# ════════════════════════════════════════════════════════════════════
# YOUTUBE / YT-DLP ENGINE
# ════════════════════════════════════════════════════════════════════

# Dedup: if two requests for the same query arrive simultaneously, the
# second waits for the first's result instead of spawning a second yt-dlp.
# Each entry: ckey -> {'event': threading.Event, 'started': float}
_yt_inflight   = {}
_yt_inflight_l = threading.Lock()
_YT_INFLIGHT_MAX_AGE = 60   # seconds — abandon stale extractions after this


def _yt_inflight_cleanup():
    """
    Periodically evict entries that were never set (e.g. worker crashed
    before the finally-block ran). Prevents new requests from waiting
    forever on a ghost event.
    """
    while True:
        time.sleep(120)
        cutoff = time.time() - _YT_INFLIGHT_MAX_AGE
        with _yt_inflight_l:
            stale = [k for k, v in _yt_inflight.items() if v['started'] < cutoff]
            for k in stale:
                entry = _yt_inflight.pop(k)
                entry['event'].set()   # unblock any waiter
                log.warning('[YT] Cleaned up stale in-flight entry %s', k[:12])


threading.Thread(target=_yt_inflight_cleanup, daemon=True).start()


def _ytdlp_opts(flat=False):
    base = {
        'quiet':          True,
        'no_warnings':    True,
        'noplaylist':     True,
        'retries':        1,
        'socket_timeout': 15,
        'http_headers':   {'User-Agent': 'Mozilla/5.0'},
    }
    if flat:
        base['extract_flat'] = True
    else:
        base['format'] = 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best'
    return base


def _pick_best_yt_entry(entries, duration_hint):
    valid = [e for e in entries if e and e.get('id')]
    if not valid:
        return None
    if not duration_hint:
        return valid[0]
    return min(valid[:5],
               key=lambda e: abs((e.get('duration') or 0) - duration_hint),
               default=valid[0])


def _extract_yt_audio(video_url):
    """Extract best audio URL from a YouTube video URL. Returns dict or None."""
    try:
        with yt_dlp.YoutubeDL(_ytdlp_opts()) as ydl:
            info = ydl.extract_info(video_url, download=False)
        if not info:
            return None

        direct = info.get('url')
        if not direct:
            fmts = [f for f in (info.get('formats') or [])
                    if f.get('acodec') != 'none'
                    and f.get('vcodec') in (None, 'none', '')]
            if fmts:
                fmts.sort(key=lambda f: (f.get('abr') or 0), reverse=True)
                direct = fmts[0].get('url')

        if not direct:
            return None

        # Derive TTL from the URL's ?expire= param — avoids serving stale links
        ttl = YT_URL_TTL
        m   = re.search(r'expire=(\d+)', direct)
        if m:
            expire_ts = int(m.group(1))
            ttl = max(60, expire_ts - int(time.time()) - 300)

        return {
            'url':      direct,
            'title':    info.get('title', ''),
            'duration': info.get('duration', 0),
            'source':   'youtube',
            'quality':  'bestaudio',
            'ext':      info.get('ext', 'webm'),
            '_ttl':     ttl,
        }
    except Exception as e:
        log.error('[YT] Extraction error {}: {}'.format(video_url, e))
        return None


def youtube_search_extract(query, duration_hint=0):
    """
    Search YouTube and extract best audio URL.

    Concurrent-safe:
      - Duplicate in-flight queries share ONE yt-dlp call (dedup).
      - Waiters time out after 35 s and return None if extraction fails/hangs.
      - Stale events (from crashed workers) are cleaned up by a background thread.
      - finally block always fires: event.set() + dict cleanup, even on exceptions.
    """
    if not YT_DLP_AVAILABLE:
        return None

    ckey   = 'ytq:' + hashlib.md5('{}|{}'.format(query, duration_hint).encode()).hexdigest()
    cached = yt_cache.get(ckey)
    if cached:
        if is_url_alive(cached['url'], timeout=4):
            log.info('[YT] Cache hit: "%s"', query)
            return cached
        yt_cache.delete(ckey)

    # ── Dedup in-flight check ─────────────────────────────────────
    with _yt_inflight_l:
        entry = _yt_inflight.get(ckey)
        if entry:
            # Another thread is already extracting — check it isn't stale
            age = time.time() - entry['started']
            if age < _YT_INFLIGHT_MAX_AGE:
                already_inflight = True
                event = entry['event']
            else:
                # Stale — take ownership and re-extract
                log.warning('[YT] Overriding stale inflight (age=%.0fs): "%s"', age, query)
                already_inflight = False
                event = threading.Event()
                _yt_inflight[ckey] = {'event': event, 'started': time.time()}
        else:
            already_inflight = False
            event = threading.Event()
            _yt_inflight[ckey] = {'event': event, 'started': time.time()}

    if already_inflight:
        log.info('[YT] Joining in-flight extraction: "%s"', query)
        event.wait(timeout=35)
        # Return whatever the owner stored (may be None if extraction failed)
        return yt_cache.get(ckey)

    # ── We own this extraction ────────────────────────────────────
    log.info('[YT] Searching: "%s"', query)
    result = None
    try:
        with yt_dlp.YoutubeDL(_ytdlp_opts(flat=True)) as ydl:
            sr = ydl.extract_info('ytsearch5:{}'.format(query), download=False)

        if sr and sr.get('entries'):
            best = _pick_best_yt_entry(sr['entries'], duration_hint)
            if best:
                vid_url = 'https://www.youtube.com/watch?v={}'.format(best['id'])
                result  = _extract_yt_audio(vid_url)
                if result:
                    ttl = result.pop('_ttl', YT_URL_TTL)
                    yt_cache.set(ckey, result, ttl=ttl)
                    log.info('[YT OK] "%s"', result['title'])
    except Exception as exc:
        log.error('[YT] Search failed "%s": %s', query, exc)
    finally:
        # Always unblock waiters and remove from dict, even on crash
        with _yt_inflight_l:
            _yt_inflight.pop(ckey, None)
        event.set()

    return result


def yt_store_proxy(result, proxy_key, query, duration_hint):
    """Store YT result under a stable proxy key. Frontend uses /api/stream?ytkey=..."""
    yt_cache.set('proxy:{}'.format(proxy_key), result,                           ttl=YT_URL_TTL)
    yt_cache.set('qmeta:{}'.format(proxy_key), {'q': query, 'd': duration_hint}, ttl=YT_URL_TTL + 600)


def yt_resolve_proxy(proxy_key):
    """
    Resolve ytkey → real URL.
    Auto re-extracts transparently if the cached URL has expired.
    """
    result = yt_cache.get('proxy:{}'.format(proxy_key))
    if result and is_url_alive(result['url'], timeout=4):
        return result

    meta = yt_cache.get('qmeta:{}'.format(proxy_key))
    if not meta:
        return None

    log.info('[YT] Re-extracting for key {}...'.format(proxy_key[:10]))
    fresh = youtube_search_extract(meta['q'], meta.get('d', 0))
    if fresh:
        yt_cache.set('proxy:{}'.format(proxy_key), fresh, ttl=YT_URL_TTL)
    return fresh


# ════════════════════════════════════════════════════════════════════
# MULTI-SOURCE ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════
def find_best_source(raw_q, raw_artist='', duration_sec=0,
                     raw_album='', raw_year='', raw_fallback=''):
    """
    Phase 1 — JioSaavn high confidence (score ≥ 0.30)
    Phase 2 — JioSaavn relaxed threshold (score ≥ 0.10, obscure / bad metadata)
    Phase 3 — YouTube via yt-dlp, auto-refreshing proxy key
    """
    clean_title  = clean_query(raw_q)
    clean_artist = clean_query(raw_artist)
    clean_album  = clean_query(raw_album)

    queries = build_queries(raw_q, raw_artist, raw_album)
    if raw_fallback:
        fb = clean_query(raw_fallback)
        if fb and fb not in queries:
            queries.insert(2, fb)

    log.info('[Search] "{}" | "{}" | dur={}s | {} queries'.format(
        clean_title, clean_artist, duration_sec, len(queries)))

    result = saavn_search(queries, clean_title, clean_artist,
                          duration_sec, clean_album, raw_year, min_score=0.30)
    if result:
        return result

    log.info('[Search] Phase 2 low-threshold for "{}"'.format(clean_title))
    result = saavn_search(queries[:4], clean_title, clean_artist,
                          duration_sec, clean_album, raw_year, min_score=0.10)
    if result:
        return result

    if YT_DLP_AVAILABLE:
        log.info('[Search] YouTube fallback for "{}"'.format(clean_title))
        pa       = primary_artist(clean_artist)
        yt_query = '{} {} official audio'.format(clean_title, pa)
        yt_result = youtube_search_extract(yt_query, duration_sec)

        if yt_result:
            pkey = hashlib.md5('{}|{}|{}'.format(
                raw_q, raw_artist, duration_sec).encode()).hexdigest()
            yt_store_proxy(yt_result.copy(), pkey, yt_query, duration_sec)
            return {
                'url':     '/api/stream?ytkey={}'.format(pkey),
                'title':   yt_result['title'],
                'artist':  clean_artist,
                'quality': yt_result['quality'],
                'source':  'youtube',
            }

    log.warning('[Search] ALL SOURCES FAILED: "{}"'.format(clean_title))
    return None


# ════════════════════════════════════════════════════════════════════
# CORS HELPERS
# ════════════════════════════════════════════════════════════════════
def _cors(resp):
    resp.headers['Access-Control-Allow-Origin']   = '*'
    resp.headers['Access-Control-Allow-Methods']  = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers']  = '*'
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
    return resp

@app.after_request
def after_request(resp): return _cors(resp)

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path): return _cors(Response(status=200))


# ════════════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))


@app.route('/api/songs')
def get_songs():
    q = request.args.get('q', 'top songs')
    try:
        r = SESSION.get(
            'https://itunes.apple.com/search',
            params={'term': q, 'media': 'music', 'entity': 'song',
                    'limit': 30, 'country': 'US'},
            timeout=15
        )
        results = [s for s in r.json().get('results', []) if s.get('previewUrl')]
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


@app.route('/api/saavn')
def get_saavn_song():
    if not _search_limiter.is_allowed(_client_ip()):
        return jsonify({'success': False, 'error': 'Rate limit exceeded', 'retry_after': 10}), 429

    raw_q        = request.args.get('q',        '').strip()
    raw_fallback = request.args.get('fallback', '').strip()
    raw_artist   = request.args.get('artist',   '').strip()
    raw_album    = request.args.get('album',    '').strip()
    raw_year     = request.args.get('year',     '').strip()
    duration_sec = int(request.args.get('duration', 0) or 0)

    if not raw_q:
        return jsonify({'success': False, 'url': None})

    result = find_best_source(
        raw_q, raw_artist, duration_sec, raw_album, raw_year, raw_fallback)

    if result:
        return jsonify({'success': True, **result})
    return jsonify({'success': False, 'url': None})


# ════════════════════════════════════════════════════════════════════
# PRODUCTION STREAM PROXY
# ════════════════════════════════════════════════════════════════════

# Hop-by-hop headers that must never be forwarded to the client
_HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate',
    'proxy-authorization', 'te', 'trailers',
    'transfer-encoding', 'upgrade',
})

_RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$', re.IGNORECASE)


def _sanitize_range_header(raw):
    """
    Validate and return a Range header string, or None if malformed.

    Accepts:   bytes=0-,   bytes=0-1023,   bytes=512-
    Rejects:   anything non-bytes, multi-range (bytes=0-1,3-5), garbage
    """
    if not raw:
        return None
    raw = raw.strip()
    m = _RANGE_RE.match(raw)
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    # Must have at least one bound
    if not start_s and not end_s:
        return None
    # Sanity: start must not exceed end when both present
    if start_s and end_s and int(start_s) > int(end_s):
        return None
    return raw


def _build_resp_headers(upstream_headers):
    """
    Strip hop-by-hop headers, handle content-encoding decompression mismatch,
    ensure audio content-type, add required streaming headers.
    """
    drop = set(_HOP_BY_HOP)

    # requests auto-decompresses — the content-length no longer matches
    # the actual byte stream, so both headers must be dropped.
    if upstream_headers.get('content-encoding', '').strip():
        drop |= {'content-encoding', 'content-length'}

    headers = {k: v for k, v in upstream_headers.items() if k.lower() not in drop}

    ct = headers.get('Content-Type', headers.get('content-type', ''))
    if not ct or any(x in ct.lower() for x in ('text', 'html', 'json')):
        headers['Content-Type'] = 'audio/mpeg'

    headers['Access-Control-Allow-Origin'] = '*'
    headers['Accept-Ranges']               = 'bytes'
    headers['Cache-Control']               = 'no-cache'
    headers['X-Content-Type-Options']      = 'nosniff'
    return headers


@app.route('/api/stream')
def stream_audio():
    if not _stream_limiter.is_allowed(_client_ip()):
        return jsonify({'error': 'Rate limit exceeded'}), 429

    url   = request.args.get('url',   '').strip()
    ytkey = request.args.get('ytkey', '').strip()

    # ── Resolve ytkey → real URL ──────────────────────────────────
    if ytkey:
        yt_data = yt_resolve_proxy(ytkey)
        if not yt_data:
            return jsonify({'error': 'YouTube source expired or unavailable'}), 404
        url = yt_data['url']

    if not url:
        return jsonify({'error': 'Missing url or ytkey parameter'}), 400

    # ── SSRF check — strict allowlist for stream proxy ────────────
    ok, reason = ssrf_check(url, require_trusted_domain=True)
    if not ok:
        log.warning('[Stream] SSRF blocked url=%s reason=%s', url[:80], reason)
        return jsonify({'error': 'URL not allowed'}), 403

    # ── Validate and forward Range header ─────────────────────────
    raw_range     = request.headers.get('Range', '')
    clean_range   = _sanitize_range_header(raw_range)
    is_range_req  = bool(clean_range)

    req_headers = {
        'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept':          '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection':      'keep-alive',
    }
    if clean_range:
        req_headers['Range'] = clean_range
    elif raw_range:
        # Malformed range header from client — log and ignore it
        log.debug('[Stream] Malformed Range header ignored: %r', raw_range)

    # ── Upstream fetch with one retry ─────────────────────────────
    upstream = None
    for attempt in range(2):
        try:
            upstream = SESSION.get(
                url,
                headers=req_headers,
                stream=True,
                timeout=(8, 20),       # connect timeout, read-first-byte timeout
                allow_redirects=True
            )
            break
        except requests.exceptions.ConnectionError:
            if attempt == 0:
                time.sleep(0.4)
                continue
            return jsonify({'error': 'Cannot connect to audio source'}), 502
        except requests.exceptions.Timeout:
            return jsonify({'error': 'Audio source timed out'}), 504
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500

    if upstream is None:
        return jsonify({'error': 'Upstream connection failed'}), 502

    status = upstream.status_code

    # ── Seek / partial-content consistency handling ───────────────
    #
    # Case A: client sent Range, upstream honoured it → 206 ✓ pass through
    # Case B: client sent Range, upstream ignored it → 200
    #         Serve as full-content 200; browser will re-seek within the buffer.
    # Case C: client sent no Range, upstream returned 206 (rare, broken CDN)
    #         Treat as 200 — drop Content-Range header.
    # Case D: any non-200/206 → error

    if status not in (200, 206):
        upstream.close()
        return jsonify({'error': 'Upstream returned {}'.format(status)}), 502

    if is_range_req and status == 200:
        # Upstream does not support Range — serve full content, drop Content-Range
        log.debug('[Stream] Range requested but upstream returned 200 (no seek support)')

    if not is_range_req and status == 206:
        # Broken CDN sent 206 without a Range request — normalise to 200
        status = 200

    resp_headers = _build_resp_headers(upstream.headers)

    # If we got 200 instead of the expected 206, remove Content-Range header
    # to avoid confusing the client about the byte range.
    if status == 200:
        resp_headers.pop('Content-Range', None)
        resp_headers.pop('content-range', None)

    # ── Streaming generator ───────────────────────────────────────
    def generate():
        stall_limit = 15   # seconds without any chunk before giving up
        last_chunk  = time.time()
        try:
            for chunk in upstream.iter_content(chunk_size=STREAM_CHUNK):
                if chunk:
                    last_chunk = time.time()
                    yield chunk
                elif time.time() - last_chunk > stall_limit:
                    log.warning('[Stream] Stall timeout — closing upstream')
                    break
        except GeneratorExit:
            pass   # client disconnected cleanly
        except Exception as exc:
            log.error('[Stream] Generator error: %s', exc)
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=status,
        headers=resp_headers,
    )



# ════════════════════════════════════════════════════════════════════
# HEALTH & MONITORING
# ════════════════════════════════════════════════════════════════════

# Cache health results — prevents /health from hammering all mirrors on
# every request (common with load balancer probes hitting /health rapidly)
_health_snapshot = {'data': None, 'ts': 0}
_HEALTH_TTL      = 30   # seconds between live checks


@app.route('/health')
def health():
    now = time.time()
    if _health_snapshot['data'] and (now - _health_snapshot['ts']) < _HEALTH_TTL:
        return jsonify(_health_snapshot['data'])

    def ping(mirror):
        try:
            t0  = time.time()
            r   = SESSION.get('{}/api/search/songs'.format(mirror),
                              params={'query': 'test', 'limit': 1}, timeout=4)
            lat = round((time.time() - t0) * 1000)
            return mirror, {
                'http':    r.status_code,
                'latency': '{}ms'.format(lat),
                'score':   mirror_health.score(mirror),
                'banned':  mirror_health.is_banned(mirror),
            }
        except Exception as e:
            return mirror, {
                'http':   'down',
                'error':  str(e)[:60],
                'score':  mirror_health.score(mirror),
                'banned': mirror_health.is_banned(mirror),
            }

    mirror_status = {}
    futs = [_EXECUTOR.submit(ping, m) for m in SAAVN_MIRRORS]
    for fut in as_completed(futs, timeout=6):
        try:
            m, s = fut.result()
            mirror_status[m] = s
        except Exception:
            pass

    result = {
        'status':  'ok',
        'yt_dlp':  YT_DLP_AVAILABLE,
        'redis':   REDIS_CLIENT is not None,
        'mirrors': mirror_status,
        'cache':   {
            'search': search_cache.size(),
            'url':    url_cache.size(),
            'yt':     yt_cache.size(),
        },
    }
    _health_snapshot['data'] = result
    _health_snapshot['ts']   = now
    return jsonify(result)


@app.route('/api/mirror-stats')
def mirror_stats():
    return jsonify(mirror_health.stats())


@app.route('/api/cache-flush')
def cache_flush():
    """Emergency flush — clears search + URL caches when all cached URLs are stale."""
    key = request.args.get('key', '')
    if key != os.environ.get('FLUSH_SECRET', 'godmode'):
        return jsonify({'error': 'Unauthorized'}), 403
    search_cache.flush()
    url_cache.flush()
    return jsonify({'flushed': True, 'message': 'search + url caches cleared'})


# ════════════════════════════════════════════════════════════════════
# DEV ENTRY POINT — Use gunicorn in production (see gunicorn.conf.py)
# ════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    log.warning('Running Flask dev server. Use gunicorn for production.')
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
