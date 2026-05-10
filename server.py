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
            params={
                'term': q,
                'media': 'music',
                'entity': 'song',
                'limit': 30,
                'country': 'US'
            },
            timeout=15
        )

        data = r.json()
        results = [s for s in data.get('results', []) if s.get('previewUrl')]
        return jsonify({'results': results})

    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def clean_title(title):
    """Remove (feat. ...), [Remastered], etc. from song title."""
    title = re.sub(r'\(.*?\)', '', title)
    title = re.sub(r'\[.*?\]', '', title)
    return title.strip()


def clean_artist(artist):
    """Take only the first artist (before & , feat. ft.)."""
    artist = re.split(r'[&,]|feat\.|ft\.', artist, flags=re.IGNORECASE)[0]
    return artist.strip()


def fetch_saavn(query):
    """Hit saavn.dev and return best download URL or None."""
    try:
        r = requests.get(
            'https://saavn.dev/api/search/songs',
            params={'query': query, 'limit': 10},
            timeout=20,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        data = r.json()
        results = data.get('data', {}).get('results', [])

        for song in results:
            download_urls = song.get('downloadUrl', [])
            if not download_urls:
                continue
            # Prefer highest quality (last in list)
            for item in reversed(download_urls):
                url = item.get('url')
                if url:
                    return {
                        'url': url,
                        'quality': item.get('quality', '320kbps'),
                        'title': song.get('name'),
                        'artist': song.get('primaryArtists')
                    }
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
# JIOSAAVN FULL SONG
# ─────────────────────────────────────────────
@app.route('/api/saavn')
def get_saavn_song():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'success': False, 'url': None})

    # Parse incoming query into title + artist parts
    # Frontend sends: "Song Title Artist Name"
    # We try progressively simpler queries to maximise hit rate
    parts = q.split(' ')

    # Strategy 1: cleaned query as-is (frontend already cleaned it)
    result = fetch_saavn(q)
    if result:
        return jsonify({'success': True, **result})

    # Strategy 2: first 5 words (handles long titles with extra metadata)
    if len(parts) > 5:
        result = fetch_saavn(' '.join(parts[:5]))
        if result:
            return jsonify({'success': True, **result})

    # Strategy 3: first 3 words (title-only approximation)
    if len(parts) > 3:
        result = fetch_saavn(' '.join(parts[:3]))
        if result:
            return jsonify({'success': True, **result})

    # Strategy 4: just the first 2 words (last resort, song name)
    if len(parts) > 1:
        result = fetch_saavn(' '.join(parts[:2]))
        if result:
            return jsonify({'success': True, **result})

    return jsonify({'success': False, 'url': None})


# ─────────────────────────────────────────────
# AUDIO STREAM PROXY
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
            'Connection': 'keep-alive'
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

        excluded_headers = ['content-encoding', 'transfer-encoding', 'connection']
        response_headers = {}

        for name, value in upstream.headers.items():
            if name.lower() not in excluded_headers:
                response_headers[name] = value

        response_headers['Access-Control-Allow-Origin'] = '*'
        response_headers['Accept-Ranges'] = 'bytes'
        response_headers['Cache-Control'] = 'no-cache'

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=1024 * 64):
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
# HEALTH CHECK
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
