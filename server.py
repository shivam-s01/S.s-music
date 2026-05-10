from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# ─────────────────────────────────────────────
# Multiple JioSaavn API mirrors — tried in order
# If one is down, next is used automatically
# ─────────────────────────────────────────────
SAAVN_MIRRORS = [
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
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
# QUERY CLEANER
# Removes (From "Movie"), quotes, parentheses
# ─────────────────────────────────────────────
def clean_query(text):
    # Remove (From "...") or (From '...')
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    # Remove (OST), (Official), (Audio), (Video), (Lyrics), (Full Song) etc
    text = re.sub(r'\((OST|official|audio|video|lyrics|full\s*song|feat\.?.*?)\)', '', text, flags=re.IGNORECASE)
    # Remove all quotes and parentheses
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    # Remove extra spaces
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# ─────────────────────────────────────────────
# SAAVN FETCH — tries all mirrors for one query
# ─────────────────────────────────────────────
def fetch_saavn_query(query):
    for mirror in SAAVN_MIRRORS:
        try:
            r = requests.get(
                f'{mirror}/api/search/songs',
                params={'query': query, 'limit': 10},
                timeout=12,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if r.status_code != 200:
                continue
            data = r.json()
            results = data.get('data', {}).get('results', [])
            for song in results:
                for item in reversed(song.get('downloadUrl', [])):
                    url = item.get('url')
                    if url:
                        print(f'[Saavn ✓] mirror={mirror} query="{query}"')
                        return {
                            'url':     url,
                            'quality': item.get('quality', '320kbps'),
                            'title':   song.get('name', ''),
                            'artist':  song.get('primaryArtists', ''),
                        }
        except Exception as e:
            print(f'[Saavn] mirror {mirror} failed: {e}')
            continue
    return None


# ─────────────────────────────────────────────
# JIOSAAVN ENDPOINT
# ─────────────────────────────────────────────
@app.route('/api/saavn')
def get_saavn_song():
    q        = request.args.get('q', '').strip()
    fallback = request.args.get('fallback', '').strip()

    if not q:
        return jsonify({'success': False, 'url': None})

    q_clean   = clean_query(q)
    fb_clean  = clean_query(fallback) if fallback else ''
    parts     = q_clean.split()

    # ── Build query ladder (most specific → broadest) ──
    queries = []

    # 1. Cleaned full query (parentheses/quotes removed)
    queries.append(q_clean)

    # 2. Original query as-is
    if q != q_clean:
        queries.append(q)

    # 3. Cleaned fallback
    if fb_clean and fb_clean not in queries:
        queries.append(fb_clean)

    # 4. Original fallback
    if fallback and fallback != q and fallback not in queries:
        queries.append(fallback)

    # 5. Progressive truncation of cleaned query
    if len(parts) > 5:
        queries.append(' '.join(parts[:5]))
    if len(parts) > 3:
        queries.append(' '.join(parts[:3]))
    if len(parts) > 1:
        queries.append(' '.join(parts[:2]))

    # 6. Progressive truncation of cleaned fallback
    if fb_clean:
        fb_parts = fb_clean.split()
        if len(fb_parts) > 2:
            queries.append(' '.join(fb_parts[:3]))
        if len(fb_parts) > 1:
            queries.append(' '.join(fb_parts[:2]))

    # ── Deduplicate while preserving order ──
    seen, unique = set(), []
    for query in queries:
        q_strip = query.strip()
        if q_strip and q_strip not in seen:
            seen.add(q_strip)
            unique.append(q_strip)

    print(f'[Saavn] Query ladder: {unique}')

    for query in unique:
        result = fetch_saavn_query(query)
        if result:
            return jsonify({'success': True, **result})

    print(f'[Saavn ✗] all mirrors + queries failed for: "{q}"')
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
        range_header = request.headers.get('Range')
        if range_header:
            headers['Range'] = range_header

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
    return jsonify({'status': 'ok'})


# ─────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
