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


# ═══════════════════════════════════════════════════════════════
# GLOBAL THREAD POOL  (reuse across requests — no overhead)
# ═══════════════════════════════════════════════════════════════
_executor = ThreadPoolExecutor(max_workers=len(SAAVN_MIRRORS))


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
# FRONTEND ROUTES  (PWA — TOUCH NAHI KARNA)
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))


@app.route('/manifest.json')
def manifest():
    return send_file(os.path.join(BASE_DIR, 'manifest.json'))


@app.route('/sw.js')
def service_worker():
    return send_file(os.path.join(BASE_DIR, 'sw.js'))


@app.route('/.well-known/assetlinks.json')
def assetlinks():
    return app.send_static_file('assetlinks.json')


# ═══════════════════════════════════════════════════════════════
# ITUNES SEARCH  (90s era support)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q   = request.args.get('q', 'top songs').strip()
    era = request.args.get('era', '').strip()

    is_90s      = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q

    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={
                'term':    search_term,
                'media':   'music',
                'entity':  'song',
                'limit':   50,
                'country': 'IN',
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
            return jsonify({'results': filtered[:30]})

        return jsonify({
            'results': [s for s in results if s.get('previewUrl')]
        })

    except Exception as e:
        log.error(f"[iTunes] Search failed '{search_term}': {e}")
        return jsonify({'results': [], 'error': str(e)})


# ═══════════════════════════════════════════════════════════════
# 90s DEDICATED ENDPOINT
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed = random.choice(NINETIES_SEEDS)

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
        return jsonify({'results': filtered[:30], 'seed': seed})

    except Exception as e:
        log.error(f"[iTunes/90s] Seed '{seed}' failed: {e}")
        return jsonify({'results': [], 'error': str(e)})


def _safe_year(date_str):
    """releaseDate se safely year nikalo."""
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
# QUERY VARIANTS  (better match probability)
# ═══════════════════════════════════════════════════════════════
def build_query_variants(title, artist='', fallback=''):
    """
    Multiple search variants — order: most specific → least specific
    1. Title + Artist
    2. Title only
    3. Fallback string
    """
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

    if artist_c:
        add(f"{title_c} {artist_c}")
    add(title_c)
    if fb_c:
        add(fb_c)

    return variants


# ═══════════════════════════════════════════════════════════════
# NORMALIZER
# ═══════════════════════════════════════════════════════════════
def normalize(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ═══════════════════════════════════════════════════════════════
# LEVENSHTEIN DISTANCE
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
    """
    Prefix    : "d"      → "dubai"   → 1.0
    Substring : "bechin" → "bechain" → 0.85
    Levenshtein: "betab" → "betaab"  → ratio
    """
    if tw.startswith(qw):
        return 1.0
    if qw in tw:
        return 0.85
    max_len = max(len(qw), len(tw))
    if max_len == 0:
        return 0.0
    ratio = 1.0 - (levenshtein(qw, tw) / max_len)
    return ratio if ratio >= 0.65 else 0.0


# ═══════════════════════════════════════════════════════════════
# TITLE SCORE  (Spotify-style algorithm)
# ═══════════════════════════════════════════════════════════════
def title_score(query, song_title, song_artist=''):
    q = normalize(query)
    t = normalize(song_title)
    a = normalize(song_artist)

    if not q:
        return 0.0
    if q == t:
        return 3.0

    q_words = q.split()
    t_words = t.split()
    a_words = a.split()
    score   = 0.0

    # Full query prefix match
    if t.startswith(q):
        score += 2.0

    # Per query-word: best fuzzy match across title words
    title_match = sum(
        max((fuzzy_word_match(qw, tw) for tw in t_words), default=0.0)
        for qw in q_words
    )
    if q_words:
        score += (title_match / len(q_words)) * 1.5

    # Artist bonus
    artist_match = sum(
        max((fuzzy_word_match(qw, aw) for aw in a_words), default=0.0)
        for qw in q_words
    )
    if q_words:
        score += (artist_match / len(q_words)) * 0.5

    return score


# ═══════════════════════════════════════════════════════════════
# DYNAMIC MIN SCORE
# ═══════════════════════════════════════════════════════════════
def dynamic_min_score(query):
    length = len(normalize(query).replace(' ', ''))
    if length <= 2:
        return 0.25
    elif length <= 5:
        return 0.45
    else:
        return 0.60


# ═══════════════════════════════════════════════════════════════
# QUALITY PICKER  ← GODMODE: ALWAYS HIGHEST QUALITY
# ═══════════════════════════════════════════════════════════════
def pick_best_quality(urls):
    """
    downloadUrl array se highest quality URL chuno.
    320kbps > 160kbps > 96kbps > 48kbps > 12kbps
    Returns: (url, quality_string)
    """
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
    """Song ka highest resolution image URL nikalo."""
    images = song.get('image') or []

    if isinstance(images, list) and images:
        for item in reversed(images):
            url = item.get('url') or item.get('link') or ''
            if url.startswith('http'):
                # Upgrade to 500x500 if possible
                url = re.sub(r'\b(50|150)x(50|150)\b', '500x500', url)
                return url

    if isinstance(images, str) and images.startswith('http'):
        return re.sub(r'\b(50|150)x(50|150)\b', '500x500', images)

    return ''


# ═══════════════════════════════════════════════════════════════
# SINGLE MIRROR FETCH
# ═══════════════════════════════════════════════════════════════
def fetch_from_mirror(mirror, query, min_score=0.4):
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
                score = title_score(query, song_title, song_artist)

                if score > best_score:
                    best_score = score
                    best_song  = song

            if not best_song or best_score < min_score:
                continue

            # Highest quality URL
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
# PARALLEL MIRROR FETCH
# ═══════════════════════════════════════════════════════════════
def fetch_saavn_parallel(query):
    threshold = dynamic_min_score(query)

    futures = {
        _executor.submit(fetch_from_mirror, mirror, query, threshold): mirror
        for mirror in SAAVN_MIRRORS
    }

    best_result = None

    try:
        for future in as_completed(futures, timeout=12):
            try:
                result = future.result()
                if result:
                    # 320kbps mila? Immediately return — no need to wait
                    if '320' in str(result.get('quality', '')):
                        for f in futures:
                            f.cancel()
                        return result
                    # Warna track karo — shayad aage 320 aaye
                    if best_result is None:
                        best_result = result

            except Exception as e:
                log.warning(f"[Parallel] Future error: {e}")

    except Exception as e:
        log.error(f"[Parallel] Timeout: {e}")

    return best_result


# ═══════════════════════════════════════════════════════════════
# JIOSAAVN ENDPOINT
# token param → race condition fix: jo play karo wahi aaye
# ═══════════════════════════════════════════════════════════════
@app.route('/api/saavn')
@limiter.limit("80 per minute")
def get_saavn_song():
    q        = request.args.get('q', '').strip()
    artist   = request.args.get('artist', '').strip()
    fallback = request.args.get('fallback', '').strip()
    token    = request.args.get('token', '').strip()  # race condition fix

    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})

    variants = build_query_variants(q, artist, fallback)

    for query in variants:
        result = fetch_saavn_parallel(query)
        if result:
            log.info(
                f"[Saavn] ✓ '{query}' "
                f"quality={result['quality']} "
                f"score={result['score']} "
                f"token={token or '-'}"
            )
            return jsonify({
                'success': True,
                'token':   token,   # ← echo back for race condition check
                **result
            })

    log.info(f"[Saavn] ✗ No match — q='{q}' token={token or '-'}")
    return jsonify({'success': False, 'url': None, 'token': token})


# ═══════════════════════════════════════════════════════════════
# STREAM PROXY  (SSRF protected — HD audio streaming)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/stream')
@limiter.limit("120 per minute")
def stream_audio():
    url = request.args.get('url', '').strip()

    if not url:
        return jsonify({'error': 'Missing URL'}), 400

    # ── SSRF Protection ──────────────────────────────────────
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
    # ─────────────────────────────────────────────────────────

    try:
        req_headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept':          'audio/mpeg,audio/webm,audio/ogg,audio/*;q=0.9,*/*;q=0.5',
            'Accept-Encoding': 'identity',   # audio compress mat karo
            'Connection':      'keep-alive',
        }

        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header   # seeking support

        upstream = requests.get(
            url,
            headers=req_headers,
            stream=True,
            timeout=60,
            allow_redirects=True
        )

        excluded = {'content-encoding', 'transfer-encoding', 'connection'}

        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in excluded
        }
        resp_headers['Access-Control-Allow-Origin'] = '*'
        resp_headers['Accept-Ranges']               = 'bytes'
        resp_headers['Cache-Control']               = 'no-store'

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):  # 64 KB
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=resp_headers,
            direct_passthrough=True
        )

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
