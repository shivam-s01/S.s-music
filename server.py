from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# ── CORS helper ──
def _cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Range, Content-Type'
    return resp

@app.after_request
def after_request(resp):
    return _cors(resp)


# ── SERVE FRONTEND ──
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))


# ── ITUNES SEARCH (metadata + 30s preview URLs) ──
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
                'limit': 25,
                'country': 'US'
            },
            timeout=8
        )
        return jsonify(r.json())
    except Exception:
        return jsonify({'results': []})


# ── JIOSAAVN FULL SONG URL ──
@app.route('/api/saavn')
def get_saavn():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'url': None})
    try:
        r = requests.get(
            'https://saavn.dev/api/search/songs',
            params={'query': q, 'limit': 5},
            timeout=8
        )
        data = r.json()
        results = data.get('data', {}).get('results', [])
        for result in results:
            # Try highest quality first (reversed = high quality last in list)
            for item in reversed(result.get('downloadUrl', [])):
                if item.get('url'):
                    return jsonify({
                        'url': item['url'],
                        'quality': item.get('quality', ''),
                        'name': result.get('name', '')
                    })
        return jsonify({'url': None})
    except Exception as e:
        return jsonify({'url': None, 'error': str(e)})


# ── AUDIO STREAM PROXY ──
# Proxies Saavn (or any) audio URL through Flask to bypass browser CORS.
# Supports Range requests so the <audio> element can seek properly.
@app.route('/api/stream')
def stream_audio():
    url = request.args.get('url', '')
    if not url:
        return jsonify({'error': 'no url'}), 400

    try:
        req_headers = {'User-Agent': 'Mozilla/5.0 (compatible; AurumMusic/1.0)'}
        # Forward Range header so partial-content / seeking works
        range_header = request.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header

        upstream = requests.get(url, stream=True, timeout=20, headers=req_headers)

        resp_headers = {
            'Content-Type': upstream.headers.get('Content-Type', 'audio/mpeg'),
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*',
        }
        # Pass through useful headers from upstream
        for h in ('Content-Length', 'Content-Range', 'Content-Disposition'):
            if h in upstream.headers:
                resp_headers[h] = upstream.headers[h]

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=16384)),
            status=upstream.status_code,
            headers=resp_headers
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── YOUTUBE VIDEO ID SEARCH ──
@app.route('/api/search')
def search_youtube():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'error': 'no query'}), 400
    api_key = os.environ.get('YT_API_KEY', '')
    if not api_key:
        return jsonify({'videoId': None, 'error': 'no api key'})
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
            timeout=8
        )
        data = r.json()
        video_id = data.get('items', [{}])[0].get('id', {}).get('videoId', None)
        return jsonify({'videoId': video_id})
    except Exception:
        return jsonify({'videoId': None})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, debug=False)
