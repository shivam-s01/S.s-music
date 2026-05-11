from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ✅ STATIC FOLDER ENABLED
app = Flask(__name__, static_folder='static')

SAAVN_MIRRORS = [
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
]


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
def after_request(resp):
    return add_cors(resp)

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return add_cors(Response(status=200))


# ─────────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/manifest.json')
def manifest():
    return send_file(os.path.join(BASE_DIR, 'manifest.json'))

@app.route('/sw.js')
def service_worker():
    return send_file(os.path.join(BASE_DIR, 'sw.js'))


# ─────────────────────────────────────────────
# ITUNES SEARCH
# ─────────────────────────────────────────────
@app.route('/api/songs')
def get_songs():
    q = request.args.get('q', 'top songs')

    try:
        r = requests.get(
            'https://itunes.apple.com/search',
            params={
                'term': q,
                'media': 'music',
                'entity': 'song',
                'limit': 30,
                'country': 'US'
            },
            timeout=15
        )

        results = [
            s for s in r.json().get('results', [])
            if s.get('previewUrl')
        ]

        return jsonify({'results': results})

    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


# ─────────────────────────────────────────────
# QUERY CLEANER
# ─────────────────────────────────────────────
def clean_query(text):
    text = re.sub(
        r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)',
        '',
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r'\((OST|official|audio|video|lyrics|full\s*song|feat\.?.*?)\)',
        '',
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    return text


# ─────────────────────────────────────────────
# TITLE SIMILARITY
# ─────────────────────────────────────────────
def normalize(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def title_score(query, song_title, song_artist=''):
    q = normalize(query)
    t = normalize(song_title)
    a = normalize(song_artist)

    q_words = set(q.split())
    t_words = set(t.split())
    a_words = set(a.split())

    if not q_words:
        return 0

    title_matches = len(q_words & t_words) / len(q_words)
    artist_bonus = len(q_words & a_words) / len(q_words) * 0.3

    q_first = q.split()[0] if q.split() else ''
    start_bonus = 0.4 if t.startswith(q_first) else 0

    return title_matches + artist_bonus + start_bonus


# ─────────────────────────────────────────────
# SINGLE MIRROR FETCH
# ─────────────────────────────────────────────
def fetch_from_mirror(mirror, query, min_score=0.4):
    endpoints = [
        '/api/search/songs',
        '/api/search',
        '/search/songs'
    ]

    for endpoint in endpoints:
        try:
            r = requests.get(
                f'{mirror}{endpoint}',
                params={
                    'query': query,
                    'q': query,
                    'limit': 10
                },
                timeout=8,
                headers={
                    'User-Agent': 'Mozilla/5.0'
                }
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

            best_song = None
            best_score = -1

            for song in results:
                song_title = song.get('name') or song.get('title', '')
                song_artist = song.get('primaryArtists') or song.get('primary_artists', '')

                score = title_score(query, song_title, song_artist)

                if score > best_score:
                    best_score = score
                    best_song = song

            if best_song and best_score >= min_score:
                urls = (
                    best_song.get('downloadUrl') or
                    best_song.get('download_url') or
                    []
                )

                for item in reversed(urls):
                    url = item.get('url') or item.get('link')

                    if url:
                        return {
                            'url': url,
                            'quality': item.get('quality', '320kbps'),
                            'title': best_song.get('name') or best_song.get('title', ''),
                            'artist': best_song.get('primaryArtists') or best_song.get('primary_artists', ''),
                            'score': best_score,
                        }

        except:
            continue

    return None


# ─────────────────────────────────────────────
# PARALLEL MIRROR FETCH
# ─────────────────────────────────────────────
def fetch_saavn_parallel(query, min_score=0.4):
    with ThreadPoolExecutor(max_workers=len(SAAVN_MIRRORS)) as executor:
        futures = {
            executor.submit(
                fetch_from_mirror,
                mirror,
                query,
                min_score
            ): mirror
            for mirror in SAAVN_MIRRORS
        }

        for future in as_completed(futures):
            result = future.result()

            if result:
                return result

    return None


# ─────────────────────────────────────────────
# JIOSAAVN ENDPOINT
# ─────────────────────────────────────────────
@app.route('/api/saavn')
def get_saavn_song():
    q = request.args.get('q', '').strip()
    fallback = request.args.get('fallback', '').strip()

    if not q:
        return jsonify({
            'success': False,
            'url': None
        })

    q_clean = clean_query(q)
    fb_clean = clean_query(fallback) if fallback else ''

    queries = [q_clean]

    if fb_clean:
        queries.append(fb_clean)

    for query in queries:
        result = fetch_saavn_parallel(query)

        if result:
            return jsonify({
                'success': True,
                **result
            })

    return jsonify({
        'success': False,
        'url': None
    })


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
            'User-Agent': 'Mozilla/5.0',
            'Accept': '*/*',
            'Connection': 'keep-alive',
        }

        range_header = request.headers.get('Range')

        if range_header:
            headers['Range'] = range_header

        upstream = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=60,
            allow_redirects=True
        )

        excluded = {
            'content-encoding',
            'transfer-encoding',
            'connection'
        }

        resp_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in excluded
        }

        resp_headers['Access-Control-Allow-Origin'] = '*'
        resp_headers['Accept-Ranges'] = 'bytes'

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

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({
        'status': 'ok'
    })


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))

    app.run(
        host='0.0.0.0',
        port=port,
        threaded=True,
        debug=False
    )
