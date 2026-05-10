from flask import Flask, request, jsonify, send_file
import requests, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/api/search')
def search_youtube():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'error': 'no query'}), 400

    api_key = os.environ.get('YT_API_KEY', '')
    try:
        r = requests.get(
            'https://www.googleapis.com/youtube/v3/search',
            params={'part': 'snippet', 'q': q, 'type': 'video', 'maxResults': 1, 'key': api_key},
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
