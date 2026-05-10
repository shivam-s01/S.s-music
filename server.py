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
# YT HEADERS CACHE  (in-memory, per process)
# ─────────────────────────────────────────────
_yt_headers_cache = {}


# ─────────────────────────────────────────────
# SAAVN FETCH
# ─────────────────────────────────────────────
def fetch_saavn(query):
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
            download_urls = song.get('downloadUrl', [])
            if not download_urls:
                continue
            for item in reversed(download_urls):
                url = item.get('url')
                if url:
                    return {
                        'url':     url,
                        'quality': item.get('quality', '320kbps'),
                        'title':   song.get('name'),
                        'artist':  song.get('primaryArtists'),
                        'source':  'saavn'
                    }
    except Exception as e:
        print(f'[Saavn] error: {e}')
    return None


# ─────────────────────────────────────────────
# YT-DLP FETCH
# ─────────────────────────────────────────────
def fetch_ytdlp(title, artist):
    try:
        import yt_dlp

        query = f"{title} {artist} official audio"

        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
            'quiet':        True,
            'no_warnings':  True,
            'noplaylist':   True,
            'extract_flat': False,
            'socket_timeout': 20,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)

            if not info or 'entries' not in info or not info['entries']:
                return None

            entry = info['entries'][0]
            if not entry:
                return None

            # Best audio-only format
            formats = entry.get('formats', [])
            audio_formats = [
                f for f in formats
                if f.get('vcodec') in ('none', None)
                and f.get('acodec') not in ('none', None)
                and f.get('url')
            ]

            if audio_formats:
                best = sorted(
                    audio_formats,
                    key=lambda x: float(x.get('abr') or 0),
                    reverse=True
                )[0]
                url = best.get('url')
                http_headers = best.get('http_headers', entry.get('http_headers', {}))
            else:
                url = entry.get('url')
                http_headers = entry.get('http_headers', {})

            if not url:
                return None

            # Cache headers for proxy
            _yt_headers_cache[url[:80]] = dict(http_headers)

            return {
                'url':     url,
                'quality': 'YouTube Audio',
                'title':   entry.get('title', title),
                'artist':  artist,
                'source':  'youtube'
            }

    except ImportError:
        print('[yt-dlp] not installed')
    except Exception as e:
        print(f'[yt-dlp] error: {e}')
    return None


# ─────────────────────────────────────────────
# JIOSAAVN  →  yt-dlp  fallback chain
# ─────────────────────────────────────────────
@app.route('/api/saavn')
def get_saavn_song():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'success': False, 'url': None})

    parts = q.split()

    # ── Saavn: progressively simpler queries ──
    saavn_queries = [q]
    if len(parts) > 5:
        saavn_queries.append(' '.join(parts[:5]))
    if len(parts) > 3:
        saavn_queries.append(' '.join(parts[:3]))
    if len(parts) > 1:
        saavn_queries.append(' '.join(parts[:2]))

    for query in saavn_queries:
        result = fetch_saavn(query)
        if result:
            print(f'[Saavn ✓] {query}')
            return jsonify({'success': True, **result})

    print(f'[Saavn ✗] not found — trying yt-dlp | query: {q}')

    # ── yt-dlp fallback ──
    # Frontend sends "CleanTitle FirstArtist", split roughly at midpoint
    mid = max(1, len(parts) // 2)
    title_part  = ' '.join(parts[:mid])
    artist_part = ' '.join(parts[mid:]) if len(parts) > mid else ''

    result = fetch_ytdlp(title_part, artist_part)
    if result:
        print(f'[yt-dlp ✓] {title_part} – {artist_part}')
        return jsonify({'success': True, **result})

    print(f'[yt-dlp ✗] not found | {title_part} – {artist_part}')
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
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept':          '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection':      'keep-alive',
            'Sec-Fetch-Dest':  'audio',
            'Sec-Fetch-Mode':  'no-cors',
            'Sec-Fetch-Site':  'cross-site',
        }

        # Merge cached yt-dlp headers (needed for YouTube URLs)
        cache_key = url[:80]
        if cache_key in _yt_headers_cache:
            headers.update(_yt_headers_cache[cache_key])

        # Range support — critical for seeking
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
        response_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in excluded
        }
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
