from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests
import re
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)


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
        data = r.json()
        results = [s for s in data.get('results', []) if s.get('previewUrl')]
        return jsonify({'results': results})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


# ─────────────────────────────────────────────
# SAAVN CORE FETCH
# ─────────────────────────────────────────────
def fetch_saavn(query):
    """Hit saavn.dev with a single query. Returns result dict or None."""
    try:
        r = requests.get(
            'https://saavn.dev/api/search/songs',
            params={'query': query, 'limit': 10},
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        data = r.json()
        results = data.get('data', {}).get('results', [])

        for song in results:
            urls = song.get('downloadUrl', [])
            if not urls:
                continue
            # Highest quality = last in list
            for item in reversed(urls):
                url = item.get('url')
                if url:
                    return {
                        'url':     url,
                        'quality': item.get('quality', '320kbps'),
                        'title':   song.get('name', ''),
                        'artist':  song.get('primaryArtists', ''),
                    }
    except Exception as e:
        print(f'[Saavn] error for "{query}": {e}')
    return None


# ─────────────────────────────────────────────
# JIOSAAVN ENDPOINT  — smart multi-query chain
# ─────────────────────────────────────────────
@app.route('/api/saavn')
def get_saavn_song():
    q        = request.args.get('q', '').strip()
    fallback = request.args.get('fallback', '').strip()

    if not q:
        return jsonify({'success': False, 'url': None})

    parts = q.split()

    # Build query ladder:
    # 1. Primary query   (e.g. "Shararat Dhurandhar")
    # 2. Fallback query  (e.g. "Shararat Shashwat Sachdev")
    # 3. First 5 words
    # 4. First 3 words
    # 5. First 2 words   (bare song title)
    queries = [q]
    if fallback and fallback != q:
        queries.append(fallback)
    if len(parts) > 5:
        queries.append(' '.join(parts[:5]))
    if len(parts) > 3:
        queries.append(' '.join(parts[:3]))
    if len(parts) > 1:
        queries.append(' '.join(parts[:2]))

    # Also add fallback parts if fallback was given
    if fallback:
        fb_parts = fallback.split()
        if len(fb_parts) > 3:
            queries.append(' '.join(fb_parts[:3]))
        if len(fb_parts) > 1:
            queries.append(' '.join(fb_parts[:2]))

    # Deduplicate while preserving order
    seen = set()
    unique_queries = []
    for query in queries:
        if query not in seen:
            seen.add(query)
            unique_queries.append(query)

    for query in unique_queries:
        result = fetch_saavn(query)
        if result:
            print(f'[Saavn ✓] found with query: "{query}"')
            return jsonify({'success': True, **result})

    print(f'[Saavn ✗] not found for: "{q}"')
    return jsonify({'success': False, 'url': None})


# ─────────────────────────────────────────────
# AUDIO STREAM PROXY  — with Range / seek support
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

        upstream = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=60,
            allow_redirects=True
        )

        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in excluded}
        response_headers['Access-Control-Allow-Origin'] = '*'
        response_headers['Accept-Ranges']               = 'bytes'
        response_headers['Cache-Control']               = 'no-cache'

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
            headers=response_headers,
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
