from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests
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

        results = [
            s for s in data.get('results', [])
            if s.get('previewUrl')
        ]

        return jsonify({
            'results': results
        })

    except Exception as e:
        return jsonify({
            'results': [],
            'error': str(e)
        })


# ─────────────────────────────────────────────
# JIOSAAVN FULL SONG
# ─────────────────────────────────────────────
@app.route('/api/saavn')
def get_saavn_song():

    q = request.args.get('q', '').strip()

    if not q:
        return jsonify({
            'success': False,
            'url': None
        })

    try:

        r = requests.get(
            'https://saavn.dev/api/search/songs',
            params={
                'query': q,
                'limit': 10
            },
            timeout=20,
            headers={
                'User-Agent': 'Mozilla/5.0'
            }
        )

        data = r.json()

        results = data.get('data', {}).get('results', [])

        if not results:
            return jsonify({
                'success': False,
                'url': None
            })

        for song in results:

            download_urls = song.get('downloadUrl', [])

            if not download_urls:
                continue

            best = download_urls[-1]

            url = best.get('url')

            if url:
                return jsonify({
                    'success': True,
                    'url': url,
                    'quality': best.get('quality', '320kbps'),
                    'title': song.get('name'),
                    'artist': song.get('primaryArtists')
                })

        return jsonify({
            'success': False,
            'url': None
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'url': None,
            'error': str(e)
        })


# ─────────────────────────────────────────────
# AUDIO STREAM PROXY
# ─────────────────────────────────────────────
@app.route('/api/stream')
def stream_audio():

    url = request.args.get('url', '').strip()

    if not url:
        return jsonify({
            'error': 'Missing URL'
        }), 400

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

        excluded_headers = [
            'content-encoding',
            'transfer-encoding',
            'connection'
        ]

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
        return jsonify({
            'error': 'Stream timeout'
        }), 504

    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500


# ─────────────────────────────────────────────
# YOUTUBE SEARCH
# ─────────────────────────────────────────────
@app.route('/api/search')
def search_youtube():

    q = request.args.get('q', '').strip()

    if not q:
        return jsonify({
            'videoId': None
        })

    api_key = os.environ.get('YT_API_KEY', '')

    if not api_key:
        return jsonify({
            'videoId': None,
            'error': 'Missing API key'
        })

    try:

        r = requests.get(
            'https://www.googleapis.com/youtube/v3/search',
            params={
                'part': 'snippet',
                'q': q,
                'type': 'video',
                'maxResults': 1,
                'key': api_key
            },
            timeout=15
        )

        data = r.json()

        items = data.get('items', [])

        if not items:
            return jsonify({
                'videoId': None
            })

        video_id = items[0]['id']['videoId']

        return jsonify({
            'videoId': video_id
        })

    except Exception as e:
        return jsonify({
            'videoId': None,
            'error': str(e)
        })


# ─────────────────────────────────────────────
# HEALTH CHECK
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
