from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

SAAVN_MIRRORS = [
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
]

QUALITY_ORDER = ['320kbps', '160kbps', '96kbps', '48kbps', '12kbps']


# ─────────────────────────────────────────────
# CORS
# ─────────────────────────────────────────────
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = '*'
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
    return resp

@app.after_request
def after_request(resp): return add_cors(resp)

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path): return add_cors(Response(status=200))


# ─────────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))


# ─────────────────────────────────────────────
# ITUNES SEARCH
# ─────────────────────────────────────────────
@app.route('/api/songs')
def get_songs():
    q = request.args.get('q', 'top songs')
    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={'term': q, 'media': 'music', 'entity': 'song', 'limit': 30, 'country': 'US'},
            timeout=15
        )
        results = [s for s in r.json().get('results', []) if s.get('previewUrl')]
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


# ─────────────────────────────────────────────
# TEXT UTILS
# ─────────────────────────────────────────────
def normalize(text):
    """Standard normalization."""
    text = (text or '').lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def normalize_loose(text):
    """
    Loose normalization for Hindi/transliteration variations.
    Collapses common spelling differences so 'Aashiqui' == 'Ashiqui'.
    Safe to apply to English too — just collapses double vowels/consonants.
    """
    text = normalize(text)
    text = re.sub(r'aa', 'a', text)   # aashiqui → ashiqui
    text = re.sub(r'ee', 'i', text)   # beeti → biti
    text = re.sub(r'oo', 'u', text)   # toone → tune
    text = re.sub(r'([a-z])\1+', r'\1', text)  # double letters
    return text.strip()

def clean_query(text):
    """Strip bracketed qualifiers from track names."""
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\((OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|remaster.*?|remix.*?)\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'- (remaster|remix|live|acoustic|radio edit).*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def extract_movie(raw_title):
    """Pull movie name from '(From "Movie")' pattern."""
    m = re.search(r'\(From\s+["\u201c\u201d\u2018\u2019]?(.+?)["\u201c\u201d\u2018\u2019]?\)', raw_title, re.IGNORECASE)
    return m.group(1).strip() if m else ''

def primary_artist(artist_str):
    """First artist when multiple are listed."""
    return re.split(r'[&,]|feat\.|ft\.', artist_str, flags=re.IGNORECASE)[0].strip()


# ─────────────────────────────────────────────
# SCORING FUNCTIONS
# ─────────────────────────────────────────────
def text_sim(a, b):
    """
    Composite text similarity: Jaccard + coverage + sequence bonus.
    Applied with both strict and loose normalization; max is used.
    """
    def _sim(qa, tb):
        q_words = qa.split()
        t_words = tb.split()
        q_set, t_set = set(q_words), set(t_words)
        if not q_set or not t_set:
            return 0.0
        if qa == tb:
            return 1.0
        inter  = len(q_set & t_set)
        union  = len(q_set | t_set)
        jaccard   = inter / union if union else 0
        coverage  = inter / len(q_set)
        seq_bonus = 0.0
        for n in range(min(5, len(q_words)), 1, -1):
            if ' '.join(q_words[:n]) in tb:
                seq_bonus = 0.12 * n
                break
        extra_penalty = min(len(t_set - q_set) * 0.04, 0.25)
        return max(0.0, jaccard * 0.35 + coverage * 0.35 + seq_bonus - extra_penalty)

    strict = _sim(normalize(a), normalize(b))
    loose  = _sim(normalize_loose(a), normalize_loose(b))
    return round(max(strict, loose), 4)

def duration_score(itunes_sec, saavn_sec):
    """Returns score 0-1 or None if unknown."""
    if itunes_sec <= 0 or saavn_sec <= 0:
        return None
    diff = abs(itunes_sec - saavn_sec)
    if   diff <= 2:  return 1.0
    elif diff <= 5:  return 0.75
    elif diff <= 10: return 0.40
    elif diff <= 20: return 0.10
    else:            return 0.0

def composite_score(clean_title, clean_artist, duration_sec, album, year, saavn_song):
    """
    Final composite match score.

    Weights (duration available):
      Duration 45% | Title 28% | Artist 17% | Album 7% | Year 3%
    Weights (no duration):
      Title 55%    | Artist 30% | Album 10% | Year 5%
    """
    s_title  = saavn_song.get('name')  or saavn_song.get('title', '')
    s_artist = saavn_song.get('primaryArtists') or saavn_song.get('primary_artists', '')
    s_dur    = int(saavn_song.get('duration') or 0)
    s_album  = ''
    alb = saavn_song.get('album')
    if isinstance(alb, dict):
        s_album = alb.get('name', '')
    elif isinstance(alb, str):
        s_album = alb
    s_year = str(saavn_song.get('year') or '')

    t = text_sim(clean_title,  s_title)
    a = text_sim(clean_artist, s_artist)
    d = duration_score(duration_sec, s_dur)

    # Album bonus
    alb_score = text_sim(album, s_album) * 0.5 if album and s_album else 0.0

    # Year bonus
    yr_score = 0.10 if (year and s_year and str(year) in s_year) else 0.0

    if d is not None:
        base = t * 0.28 + a * 0.17 + d * 0.45
    else:
        base = t * 0.55 + a * 0.30

    return round(base + alb_score * 0.07 + yr_score * 0.03, 4)


# ─────────────────────────────────────────────
# QUERY BUILDER — comprehensive coverage
# ─────────────────────────────────────────────
def build_queries(raw_title, raw_artist, collection=''):
    """
    Build a ranked list of search queries covering all strategies.
    Handles Bollywood (movie extraction), English, 90s albums, featuring artists, etc.
    """
    movie        = extract_movie(raw_title)
    ct           = clean_query(raw_title)    # clean title
    ca           = clean_query(raw_artist)   # clean full artist
    pa           = primary_artist(ca)        # first artist only
    clean_album  = clean_query(collection) if collection else ''
    t_words      = ct.split()

    queries = []

    # ── Bollywood: title + movie name ──
    if movie:
        cm = clean_query(movie)
        queries += [
            f"{ct} {cm}",
            f"{ct} {cm} {pa}",
            f"{ct} {pa} {cm}",
        ]

    # ── Universal: title + artist combos ──
    queries += [
        f"{ct} {pa}",
        f"{ct} {ca}",
        f"{pa} {ct}",
        ct,
    ]

    # ── Shortened title variants (long titles) ──
    if len(t_words) > 4:
        short = ' '.join(t_words[:4])
        queries += [f"{short} {pa}", short]
    if len(t_words) > 2:
        queries.append(' '.join(t_words[:3]) + ' ' + pa)
        queries.append(' '.join(t_words[:2]) + ' ' + pa)

    # ── Album-based (90s songs where album is iconic) ──
    if clean_album:
        queries += [
            f"{ct} {clean_album}",
            f"{clean_album} {ct}",
        ]

    # ── Loose title (handles double-vowel variations) ──
    ct_loose = normalize_loose(ct)
    pa_loose = normalize_loose(pa)
    if ct_loose != normalize(ct):
        queries += [f"{ct_loose} {pa_loose}", ct_loose]

    # Deduplicate preserving order
    seen, unique = set(), []
    for q in queries:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            unique.append(q)

    return unique


# ─────────────────────────────────────────────
# BEST QUALITY URL
# ─────────────────────────────────────────────
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


# ─────────────────────────────────────────────
# FETCH FROM ONE MIRROR — returns all scored candidates
# ─────────────────────────────────────────────
def fetch_candidates_from_mirror(mirror, query, clean_title, clean_artist,
                                  duration_sec, album, year):
    """
    Returns list of scored candidate dicts from a single mirror+query combo.
    """
    endpoints = ['/api/search/songs', '/api/search', '/search/songs']
    candidates = []

    for endpoint in endpoints:
        try:
            r = requests.get(
                f'{mirror}{endpoint}',
                params={'query': query, 'q': query, 'limit': 10},
                timeout=7,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200:
                continue

            data = r.json()
            results = (
                data.get('data', {}).get('results') or
                data.get('results') or
                data.get('songs', {}).get('results') or
                []
            )
            if not results:
                continue

            for song in results:
                score = composite_score(clean_title, clean_artist,
                                        duration_sec, album, year, song)
                urls = song.get('downloadUrl') or song.get('download_url') or []
                best_url, best_quality = get_best_url(urls)
                if best_url:
                    candidates.append({
                        'url':     best_url,
                        'quality': best_quality,
                        'title':   song.get('name') or song.get('title', ''),
                        'artist':  song.get('primaryArtists') or song.get('primary_artists', ''),
                        'score':   score,
                        '_dur':    int(song.get('duration') or 0),
                        '_mirror': mirror,
                        '_query':  query,
                    })

            # Got results from this endpoint — no need to try others for same mirror
            break

        except Exception as e:
            print(f'[Saavn] {mirror}{endpoint}: {e}')
            continue

    return candidates


# ─────────────────────────────────────────────
# GLOBAL BEST MATCH — all queries × all mirrors in parallel
# ─────────────────────────────────────────────
def find_best_match(queries, clean_title, clean_artist,
                    duration_sec, album, year, min_score=0.30):
    """
    Fire EVERY query against EVERY mirror simultaneously.
    Collect all candidates, return the one with highest composite score.
    This is the key improvement over sequential search.
    """
    all_candidates = []

    # Limit to top N queries to avoid too many requests
    top_queries = queries[:8]

    jobs = [
        (mirror, query)
        for query in top_queries
        for mirror in SAAVN_MIRRORS
    ]

    with ThreadPoolExecutor(max_workers=min(len(jobs), 24)) as executor:
        futures = {
            executor.submit(
                fetch_candidates_from_mirror,
                mirror, query,
                clean_title, clean_artist,
                duration_sec, album, year
            ): (mirror, query)
            for mirror, query in jobs
        }
        for future in as_completed(futures, timeout=12):
            try:
                candidates = future.result()
                all_candidates.extend(candidates)
            except Exception:
                pass

    if not all_candidates:
        return None

    # Sort by score descending
    all_candidates.sort(key=lambda x: x['score'], reverse=True)
    best = all_candidates[0]

    print(
        f'[Saavn BEST] score={best["score"]} quality={best["quality"]} '
        f'dur_diff={abs(duration_sec - best["_dur"])}s '
        f'query="{best["_query"]}" -> "{best["title"]}" by "{best["artist"]}"'
    )

    if best['score'] >= min_score:
        return {k: v for k, v in best.items() if not k.startswith('_')}

    return None


# ─────────────────────────────────────────────
# JIOSAAVN ENDPOINT
# Accepts: q, fallback, artist, duration, album, year
# ─────────────────────────────────────────────
@app.route('/api/saavn')
def get_saavn_song():
    raw_q        = request.args.get('q', '').strip()
    raw_fallback = request.args.get('fallback', '').strip()
    raw_artist   = request.args.get('artist', '').strip()
    duration_sec = int(request.args.get('duration', 0) or 0)
    raw_album    = request.args.get('album', '').strip()
    raw_year     = request.args.get('year', '').strip()

    if not raw_q:
        return jsonify({'success': False, 'url': None})

    clean_title  = clean_query(raw_q)
    clean_artist = clean_query(raw_artist)
    clean_album  = clean_query(raw_album)

    queries = build_queries(raw_q, raw_artist, raw_album)

    # Add fallback variants if provided
    if raw_fallback:
        fb_clean = clean_query(raw_fallback)
        if fb_clean and fb_clean not in queries:
            queries.insert(2, fb_clean)

    print(
        f'[Saavn] title="{clean_title}" artist="{clean_artist}" '
        f'album="{clean_album}" dur={duration_sec}s year={raw_year} '
        f'queries({len(queries)})={queries[:5]}...'
    )

    # ── Phase 1: full scoring, all queries, all mirrors ──
    result = find_best_match(
        queries, clean_title, clean_artist,
        duration_sec, clean_album, raw_year,
        min_score=0.30
    )
    if result:
        return jsonify({'success': True, **result})

    # ── Phase 2: lower threshold (song might have very different metadata) ──
    print(f'[Saavn] Phase 2 low-threshold for "{clean_title}"')
    result = find_best_match(
        queries[:4], clean_title, clean_artist,
        duration_sec, clean_album, raw_year,
        min_score=0.10
    )
    if result:
        return jsonify({'success': True, **result})

    print(f'[Saavn MISS] "{raw_q}"')
    return jsonify({'success': False, 'url': None})


# ─────────────────────────────────────────────
# STREAM PROXY
# ─────────────────────────────────────────────
@app.route('/api/stream')
def stream_audio():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Missing URL'}), 400
    try:
        headers = {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Accept':          '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection':      'keep-alive',
        }
        rng = request.headers.get('Range')
        if rng:
            headers['Range'] = rng

        upstream = requests.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True)

        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        resp_headers['Access-Control-Allow-Origin'] = '*'
        resp_headers['Accept-Ranges']               = 'bytes'
        resp_headers['Cache-Control']               = 'no-cache'

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
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
    except requests.exceptions.Timeout:
        return jsonify({'error': 'Stream timeout'}), 504
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────
@app.route('/health')
def health():
    mirror_status = {}
    for mirror in SAAVN_MIRRORS:
        try:
            r = requests.get(f'{mirror}/api/search/songs',
                             params={'query': 'test', 'limit': 1}, timeout=5)
            mirror_status[mirror] = r.status_code
        except Exception as e:
            mirror_status[mirror] = f'down ({str(e)[:40]})'
    return jsonify({'status': 'ok', 'mirrors': mirror_status})


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
