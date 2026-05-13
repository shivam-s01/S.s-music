from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests
import os
import re
import logging
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from cachetools import TTLCache
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════
# APP INIT
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__, static_folder='static')


# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# RATE LIMITER  (in-memory — no Redis needed)
# ═══════════════════════════════════════════════════════════════
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://"
)


# ═══════════════════════════════════════════════════════════════
# CACHE  (thread-safe TTL cache)
# ═══════════════════════════════════════════════════════════════
_saavn_cache      = TTLCache(maxsize=600, ttl=3600)   # 1 hour
_itunes_cache     = TTLCache(maxsize=400, ttl=1800)   # 30 min
_cache_lock       = threading.Lock()


def cache_get(cache, key):
    with _cache_lock:
        return cache.get(key)

def cache_set(cache, key, value):
    with _cache_lock:
        try:
            cache[key] = value
        except Exception:
            pass  # cache full — ignore silently


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

# SSRF protection — sirf inhi CDN domains se stream allowed
ALLOWED_STREAM_DOMAINS = [
    'akamaized.net',
    'jiocdn.com',
    'saavncdn.com',
    'cf.saavncdn.com',
    'aac.saavncdn.com',
    'static.saavncdn.com',
    'c.saavncdn.com',
    'h.saavncdn.com',
]

# Quality ranking — higher number = better quality
QUALITY_RANK = {
    '320kbps': 7,
    '320':     7,
    '160kbps': 5,
    '160':     5,
    '96kbps':  3,
    '96':      3,
    '48kbps':  2,
    '48':      2,
    '12kbps':  1,
    '12':      1,
}

NINETIES_SEEDS = [
    "Kumar Sanu hits",
    "Udit Narayan 90s",
    "Alka Yagnik 90s",
    "Lata Mangeshkar 90s",
    "Sonu Nigam 90s hits",
    "Kavita Krishnamurthy songs",
    "Asha Bhosle 90s",
    "Abhijeet Bhattacharya hits",
    "Shankar Mahadevan 90s",
    "AR Rahman 90s",
    "Anu Malik 90s hits",
    "Nadeem Shravan songs",
    "Jatin Lalit songs",
    "Kumar Sanu Alka Yagnik duets",
    "90s Bollywood superhits",
]

NINETIES_TRIGGERS = [
    '90', 'purane', 'purana', 'purani',
    'old', 'retro', 'classic', 'nineties',
    'throwback', 'evergreen', 'gaane',
]

# iTunes country routing by detected language/genre
# Japanese songs → JP store has more complete catalog
# English songs  → US store is most complete
# Hindi/Bollywood → IN store
ITUNES_COUNTRY_MAP = {
    'japanese': 'JP',
    'english':  'US',
    'hindi':    'IN',
    'default':  'IN',
}

# Japanese artist/keyword triggers
JAPANESE_TRIGGERS = [
    'japanese', 'jpop', 'j-pop', 'jrock', 'j-rock', 'anime',
    'vocaloid', 'touhou', 'yoasobi', 'ado', 'fujii kaze',
    'kenshi yonezu', 'yorushika', 'lisa', 'aimer', 'eve',
    'bump of chicken', 'radwimps', 'one ok rock', 'back number',
    'official hige dandism', 'king gnu', 'reol', 'zutomayo',
]

# English (non-Hindi) triggers — use US store
ENGLISH_TRIGGERS = [
    'english', 'pop', 'rock', 'hip hop', 'hiphop', 'rap',
    'rnb', 'r&b', 'jazz', 'blues', 'country', 'edm', 'electronic',
    'metal', 'punk', 'indie', 'alternative', 'kpop', 'k-pop',
]


# ═══════════════════════════════════════════════════════════════
# GLOBAL THREAD POOL  (reduced from 6 → 3 to save RAM)
# ═══════════════════════════════════════════════════════════════
_executor = ThreadPoolExecutor(max_workers=3)


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

@app.route('/style.css')
def styles():
    resp = send_file(os.path.join(BASE_DIR, 'style.css'), mimetype='text/css')
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@app.route('/app.js')
def scripts():
    resp = send_file(os.path.join(BASE_DIR, 'app.js'), mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp

@app.route('/manifest.json')
def manifest():
    resp = send_file(os.path.join(BASE_DIR, 'manifest.json'), mimetype='application/json')
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


# ═══════════════════════════════════════════════════════════════
# LANGUAGE DETECTOR
# ═══════════════════════════════════════════════════════════════
def detect_query_language(q: str) -> str:
    """Returns 'japanese', 'english', 'hindi', or 'default'."""
    q_lower = q.lower()

    # Check for Japanese Unicode characters
    for ch in q:
        cp = ord(ch)
        if (0x3040 <= cp <= 0x30FF) or (0x4E00 <= cp <= 0x9FFF) or (0xFF00 <= cp <= 0xFFEF):
            return 'japanese'

    if any(t in q_lower for t in JAPANESE_TRIGGERS):
        return 'japanese'

    if any(t in q_lower for t in ENGLISH_TRIGGERS):
        return 'english'

    return 'default'


# ═══════════════════════════════════════════════════════════════
# ITUNES SEARCH  (multi-country, 90s era support)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q   = request.args.get('q', 'top songs').strip()
    era = request.args.get('era', '').strip()

    is_90s      = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    lang    = detect_query_language(q)
    country = ITUNES_COUNTRY_MAP.get(lang, 'IN')

    cache_key = f"itunes:{search_term}:{country}:{era}"
    cached    = cache_get(_itunes_cache, cache_key)
    if cached is not None:
        log.info(f"[iTunes] Cache hit → '{search_term}' country={country}")
        return jsonify(cached)

    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={
                'term':    search_term,
                'media':   'music',
                'entity':  'song',
                'limit':   50,
                'country': country,
            },
            timeout=15
        )
        r.raise_for_status()
        results = r.json().get('results', [])

        if is_90s:
            filtered = [
                s for s in results
                if s.get('previewUrl') and
                1990 <= _safe_year(s.get('releaseDate')) <= 1999
            ]
            if len(filtered) < 5:
                filtered = [s for s in results if s.get('previewUrl')]
            random.shuffle(filtered)
            payload = {'results': filtered[:30]}
        else:
            payload = {
                'results': [s for s in results if s.get('previewUrl')]
            }

        cache_set(_itunes_cache, cache_key, payload)
        return jsonify(payload)

    except Exception as e:
        log.error(f"[iTunes] Search failed '{search_term}': {e}")
        return jsonify({'results': [], 'error': str(e)})


# ═══════════════════════════════════════════════════════════════
# 90s DEDICATED ENDPOINT
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed      = random.choice(NINETIES_SEEDS)
    cache_key = f"itunes:90s:{seed}"
    cached    = cache_get(_itunes_cache, cache_key)
    if cached is not None:
        log.info(f"[iTunes/90s] Cache hit → '{seed}'")
        return jsonify(cached)

    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={
                'term':    seed,
                'media':   'music',
                'entity':  'song',
                'limit':   50,
                'country': 'IN',
            },
            timeout=15
        )
        r.raise_for_status()
        results = r.json().get('results', [])

        filtered = [
            s for s in results
            if s.get('previewUrl') and
            1990 <= _safe_year(s.get('releaseDate')) <= 1999
        ]
        if len(filtered) < 5:
            filtered = [s for s in results if s.get('previewUrl')]

        random.shuffle(filtered)
        payload = {'results': filtered[:30], 'seed': seed}
        cache_set(_itunes_cache, cache_key, payload)
        return jsonify(payload)

    except Exception as e:
        log.error(f"[iTunes/90s] Seed '{seed}' failed: {e}")
        return jsonify({'results': [], 'error': str(e)})


def _safe_year(date_str):
    try:
        return int((date_str or '')[:4])
    except (ValueError, TypeError):
        return 0


# ═══════════════════════════════════════════════════════════════
# QUERY CLEANER
# ═══════════════════════════════════════════════════════════════
def clean_query(text):
    text = re.sub(
        r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)',
        '', text, flags=re.IGNORECASE
    )
    text = re.sub(
        r'\((OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?)\)',
        '', text, flags=re.IGNORECASE
    )
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ═══════════════════════════════════════════════════════════════
# QUERY VARIANTS
# ═══════════════════════════════════════════════════════════════
def build_query_variants(title, artist='', fallback=''):
    title_c  = clean_query(title)
    artist_c = clean_query(artist) if artist else ''
    fb_c     = clean_query(fallback) if fallback else ''

    seen     = set()
    variants = []

    def add(v):
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    add(title_c)
    if artist_c:
        add(f"{title_c} {artist_c}")
    if fb_c:
        add(fb_c)

    return variants


# ═══════════════════════════════════════════════════════════════
# NORMALIZER
# ═══════════════════════════════════════════════════════════════
def normalize(text):
    text = text.lower()
    # Keep Japanese characters intact for matching
    text = re.sub(r'[^a-z0-9\s\u3040-\u30ff\u4e00-\u9fff\uff00-\uffef]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ═══════════════════════════════════════════════════════════════
# LEVENSHTEIN
# ═══════════════════════════════════════════════════════════════
def levenshtein(s1, s2):
    if len(s1) < len(s2):
        return levenshtein(s2, s1)
    if not s2:
        return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


# ═══════════════════════════════════════════════════════════════
# FUZZY WORD MATCH
# ═══════════════════════════════════════════════════════════════
def fuzzy_word_match(qw, tw):
    if qw == tw:
        return 1.0
    if tw.startswith(qw) and (len(tw) == len(qw) or tw[len(qw)] == ' '):
        return 0.95
    max_len = max(len(qw), len(tw))
    if max_len == 0:
        return 0.0
    ratio = 1.0 - (levenshtein(qw, tw) / max_len)
    return ratio if ratio >= 0.70 else 0.0


# ═══════════════════════════════════════════════════════════════
# TITLE SCORE  (improved — artist name weighted more for JP/EN)
# ═══════════════════════════════════════════════════════════════
def title_score(query, song_title, song_artist='', lang='default'):
    q = normalize(query)
    t = normalize(song_title)
    a = normalize(song_artist)

    if not q:
        return 0.0
    if q == t:
        return 3.0

    q_words = q.split()
    t_words = t.split()
    a_words = a.split() if a else []
    score   = 0.0

    if t.startswith(q):
        score += 2.0

    title_match = sum(
        max((fuzzy_word_match(qw, tw) for tw in t_words), default=0.0)
        for qw in q_words
    )
    if q_words:
        score += (title_match / len(q_words)) * 1.5

    if a_words:
        # For Japanese/English, artist match matters more — boost weight
        artist_weight = 0.40 if lang in ('japanese', 'english') else 0.15
        artist_match  = sum(
            max((fuzzy_word_match(qw, aw) for aw in a_words), default=0.0)
            for qw in q_words
        )
        score += (artist_match / len(q_words)) * artist_weight

    return score


# ═══════════════════════════════════════════════════════════════
# DYNAMIC MIN SCORE
# ═══════════════════════════════════════════════════════════════
def dynamic_min_score(query):
    length = len(normalize(query).replace(' ', ''))
    if length <= 2:
        return 0.50
    elif length <= 5:
        return 0.60
    else:
        return 0.70


# ═══════════════════════════════════════════════════════════════
# WORD GUARD
# ═══════════════════════════════════════════════════════════════
def has_word_match(query, song_title):
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()

    if not q_words or not t_words:
        return False

    for qw in q_words:
        min_ratio = 1.0 if len(qw) <= 2 else 0.70
        for tw in t_words:
            score = fuzzy_word_match(qw, tw)
            if score >= min_ratio:
                return True
    return False


# ═══════════════════════════════════════════════════════════════
# QUALITY PICKER
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

    sorted_urls = sorted(urls, key=rank, reverse=True)

    for item in sorted_urls:
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'):
            return url, item.get('quality', 'unknown')

    return None, None


# ═══════════════════════════════════════════════════════════════
# IMAGE PICKER
# ═══════════════════════════════════════════════════════════════
def pick_image(song):
    images = song.get('image') or []

    if isinstance(images, list) and images:
        for item in reversed(images):
            url = item.get('url') or item.get('link') or ''
            if url.startswith('http'):
                url = re.sub(r'\b(50|150)x(50|150)\b', '500x500', url)
                return url

    if isinstance(images, str) and images.startswith('http'):
        return re.sub(r'\b(50|150)x(50|150)\b', '500x500', images)

    return ''


# ═══════════════════════════════════════════════════════════════
# SINGLE MIRROR FETCH  (lang-aware scoring)
# ═══════════════════════════════════════════════════════════════
def fetch_from_mirror(mirror, query, min_score=0.4, lang='default'):
    endpoints = [
        '/api/search/songs',
        '/api/search',
        '/search/songs',
    ]

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

            data    = r.json()
            results = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or
                []
            )

            best_song  = None
            best_score = -1

            for song in results:
                song_title  = song.get('name') or song.get('title', '')
                song_artist = (
                    song.get('primaryArtists') or
                    song.get('primary_artists') or ''
                )

                if not has_word_match(query, song_title):
                    continue

                score = title_score(query, song_title, song_artist, lang)

                if score > best_score:
                    best_score = score
                    best_song  = song

            if not best_song or best_score < min_score:
                continue

            raw_urls = (
                best_song.get('downloadUrl') or
                best_song.get('download_url') or
                []
            )
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]

            best_url, quality = pick_best_quality(raw_urls)

            if not best_url:
                continue

            return {
                'url':     best_url,
                'quality': quality,
                'title':   best_song.get('name') or best_song.get('title', ''),
                'artist':  (
                    best_song.get('primaryArtists') or
                    best_song.get('primary_artists') or ''
                ),
                'image':   pick_image(best_song),
                'score':   round(best_score, 3),
            }

        except Exception as e:
            log.warning(f"[Mirror {mirror}] {endpoint} → {e}")
            continue

    return None


# ═══════════════════════════════════════════════════════════════
# PARALLEL MIRROR FETCH  (3 workers, lang-aware)
# ═══════════════════════════════════════════════════════════════
def fetch_saavn_parallel(query, lang='default'):
    threshold = dynamic_min_score(query)

    # Pick top 3 mirrors to keep thread usage minimal
    mirrors_to_try = SAAVN_MIRRORS[:3]

    futures = {
        _executor.submit(fetch_from_mirror, mirror, query, threshold, lang): mirror
        for mirror in mirrors_to_try
    }

    all_results = []

    try:
        for future in as_completed(futures, timeout=12):
            try:
                result = future.result()
                if result:
                    all_results.append(result)
                    # Early exit — if we have a high-confidence match, stop waiting
                    if result.get('score', 0) >= 2.5:
                        break
            except Exception as e:
                log.warning(f"[Parallel] Future error: {e}")

    except Exception as e:
        log.error(f"[Parallel] Timeout: {e}")

    # Cancel remaining futures to free threads ASAP
    for f in futures:
        f.cancel()

    if not all_results:
        # Fallback: try remaining mirrors sequentially
        for mirror in SAAVN_MIRRORS[3:]:
            result = fetch_from_mirror(mirror, query, threshold, lang)
            if result:
                return result
        return None

    def result_rank(r):
        score   = r.get('score', 0)
        quality = r.get('quality', '')
        q_bonus = 0.03 if '320' in str(quality) else 0
        return score + q_bonus

    all_results.sort(key=result_rank, reverse=True)

    best = all_results[0]
    log.info(
        f"[Parallel] Best from {len(all_results)} results → "
        f"'{best['title']}' score={best['score']} quality={best['quality']}"
    )
    return best


# ═══════════════════════════════════════════════════════════════
# JIOSAAVN ENDPOINT  (with cache + lang detection)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/saavn')
@limiter.limit("80 per minute")
def get_saavn_song():
    q        = request.args.get('q', '').strip()
    artist   = request.args.get('artist', '').strip()
    fallback = request.args.get('fallback', '').strip()
    token    = request.args.get('token', '').strip()

    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})

    # Cache key — based on normalized query + artist
    cache_key = f"saavn:{normalize(q)}:{normalize(artist)}"
    cached    = cache_get(_saavn_cache, cache_key)
    if cached is not None:
        log.info(f"[Saavn] Cache hit → '{q}' token={token or '-'}")
        return jsonify({**cached, 'token': token})

    lang     = detect_query_language(f"{q} {artist}")
    variants = build_query_variants(q, artist, fallback)

    for query in variants:
        result = fetch_saavn_parallel(query, lang)

        if result:
            returned_title = result['title']
            original_match = has_word_match(q, returned_title)
            variant_match  = has_word_match(query, returned_title)

            if not original_match and not variant_match:
                log.warning(
                    f"[Saavn] Final reject — query='{q}' variant='{query}' "
                    f"returned '{returned_title}' (no word match)"
                )
                result = None

        if result:
            log.info(
                f"[Saavn] ✓ q='{q}' lang={lang} → '{result['title']}' "
                f"quality={result['quality']} score={result['score']} "
                f"token={token or '-'}"
            )
            payload = {'success': True, **result}
            cache_set(_saavn_cache, cache_key, payload)
            return jsonify({**payload, 'token': token})

    log.info(f"[Saavn] ✗ No match — q='{q}' lang={lang} token={token or '-'}")
    return jsonify({'success': False, 'url': None, 'token': token})


# ═══════════════════════════════════════════════════════════════
# STREAM PROXY  (larger chunks = fewer iterations = less CPU)
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

        domain  = parsed.netloc.lower().split(':')[0]
        allowed = any(
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
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',
            'Connection':      'keep-alive',
        }

        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header

        upstream = requests.get(
            url,
            headers=req_headers,
            stream=True,
            timeout=(10, 300),
            allow_redirects=True
        )

        excluded_headers = {
            'content-encoding', 'transfer-encoding', 'connection',
            'keep-alive', 'proxy-authenticate', 'proxy-authorization',
            'te', 'trailers', 'upgrade',
        }

        resp_headers = {}
        for k, v in upstream.headers.items():
            if k.lower() not in excluded_headers:
                resp_headers[k] = v

        resp_headers['Access-Control-Allow-Origin'] = '*'

        upstream_accept_ranges = upstream.headers.get('Accept-Ranges', '').strip()
        if upstream_accept_ranges and upstream_accept_ranges != 'none':
            resp_headers['Accept-Ranges'] = upstream_accept_ranges
        else:
            resp_headers['Accept-Ranges'] = 'bytes'

        resp_headers['Cache-Control'] = 'public, max-age=3600'

        if 'content-type' not in {k.lower() for k in resp_headers}:
            resp_headers['Content-Type'] = 'audio/mpeg'

        def generate():
            try:
                # 64KB chunks — half the system calls vs 32KB
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            except Exception as gen_err:
                log.warning(f"[Stream] Generator error: {gen_err}")
            finally:
                upstream.close()

        status_code = upstream.status_code

        log.info(
            f"[Stream] → {domain} | status={status_code} | "
            f"range={'yes' if range_header else 'no'} | "
            f"content-length={upstream.headers.get('Content-Length', '?')}"
        )

        return Response(
            stream_with_context(generate()),
            status=status_code,
            headers=resp_headers,
            direct_passthrough=True
        )

    except requests.exceptions.ConnectTimeout:
        log.error(f"[Stream] Connect timeout → {url[:80]}")
        return jsonify({'error': 'Upstream connect timeout'}), 504

    except requests.exceptions.ReadTimeout:
        log.error(f"[Stream] Read timeout → {url[:80]}")
        return jsonify({'error': 'Upstream read timeout'}), 504

    except Exception as e:
        log.error(f"[Stream] Error → {url[:80]}: {e}")
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════
@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


# ═══════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
