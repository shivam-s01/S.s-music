from flask import Flask, request, jsonify, send_from_directory
import requests, os

app = Flask(__name__)

# Serve your HTML file from same folder as this script
@app.route('/')
@app.route('/<path:filename>')
def serve_file(filename='aurum-music-fixed.html'):
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)

# Proxy route — browser calls this, server fetches YouTube search
@app.route('/api/search')
def search_youtube():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'error': 'no query'}), 400

    PIPED_NODES = [
        'https://pipedapi.kavin.rocks',
        'https://pipedapi.adminforge.de',
        'https://piped-api.garudalinux.org',
    ]
    INVIDIOUS_NODES = [
        'https://inv.riverside.rocks',
        'https://yt.cdaut.de',
    ]

    # Try Piped nodes first
    for node in PIPED_NODES:
        try:
            r = requests.get(f'{node}/search?q={q}&filter=videos', timeout=6)
            if r.ok:
                data = r.json()
                items = data.get('items', [])
                if items:
                    url = items[0].get('url', '')
                    vid = url.split('v=')[-1].split('&')[0] if 'v=' in url else None
                    if vid:
                        return jsonify({'videoId': vid})
        except Exception:
            continue

    # Fallback: Invidious nodes
    for node in INVIDIOUS_NODES:
        try:
            r = requests.get(f'{node}/api/v1/search?q={q}&type=video&fields=videoId', timeout=6)
            if r.ok:
                data = r.json()
                if isinstance(data, list) and data and data[0].get('videoId'):
                    return jsonify({'videoId': data[0]['videoId']})
        except Exception:
            continue

    return jsonify({'videoId': None})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7700, debug=False)
