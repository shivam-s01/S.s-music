from flask import Flask, request, jsonify, send_file
import requests, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/api/songs')
def get_songs():
    q = request.args.get('q', 'top songs')
    try:
        r = requests.get('https://itunes.apple.com/search',
            params={'term': q, 'media': 'music', 'entity': 'song', 'limit': 25, 'country': 'US'},
            timeout=8)
        return jsonify(r.json())
    except Exception:
        return jsonify({'results': []})

@app.route('/api/saavn')
def get_saavn():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'url': None, 'duration': None})
    try:
        r = requests.get('https://saavn.dev/api/search/songs',
            params={'query': q, 'limit': 5}, timeout=8)
        data = r.json()
        results = data.get('data', {}).get('results', [])
        if results:
            for result in results:
                urls = result.get('downloadUrl', [])
                # Get best quality URL (320kbps preferred)
                best = None
                for item in reversed(urls):
                    if item.get('url'):
                        best = item['url']
                        break
                if best:
                    duration = result.get('duration', None)
                    return jsonify({'url': best, 'duration': duration})
        return jsonify({'url': None, 'duration': None})
    except Exception:
        return jsonify({'url': None, 'duration': None})

@app.route('/api/search')
def search_youtube():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'error': 'no query'}), 400
    api_key = os.environ.get('YT_API_KEY', '')
    try:
        r = requests.get('https://www.googleapis.com/youtube/v3/search',
            params={'part': 'snippet', 'q': q, 'type': 'video', 'maxResults': 1, 'key': api_key},
            timeout=8)
        data = r.json()
        video_id = data.get('items', [{}])[0].get('id', {}).get('videoId', None)
        return jsonify({'videoId': video_id})
    except Exception:
        return jsonify({'videoId': None})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, debug=False)
