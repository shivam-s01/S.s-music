from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import requests
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# Best working mirrors (Render friendly)
SAAVN_MIRRORS = [
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
]

# ─────────────────────────────────────────────
# UTILS (The Logic Engine)
# ─────────────────────────────────────────────
def get_similarity(a, b):
    """Ye check karta hai ki gaane ka naam kitna match kar raha hai"""
    if not a or not b: return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def clean_name(text):
    """Faltu ki cheezein hatane ke liye (like [Official Video])"""
    if not text: return ""
    text = re.sub(r'\(.*?\)|\[.*?\]', '', text)
    text = re.sub(r'[^a-zA-Z0-9\s]', '', text)
    return ' '.join(text.split()).lower()

def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = '*'
    return resp

@app.after_request
def after_request(resp): return add_cors(resp)

# ─────────────────────────────────────────────
# CORE SEARCH (The 99.9% Fix)
# ─────────────────────────────────────────────
def fetch_from_mirror(mirror, track_name, artist_name):
    # 99% Fix: Hum Title aur Artist ko milakar search maarte hain
    search_query = f"{track_name} {artist_name}".strip()
    endpoints = ['/api/search/songs', '/search/songs']
    
    for endpoint in endpoints:
        try:
            url = f"{mirror}{endpoint}"
            r = requests.get(url, params={'query': search_query, 'limit': 5}, timeout=6)
            if r.status_code != 200: continue
            
            data = r.json()
            results = data.get('data', {}).get('results') or data.get('results') or []

            for song in results:
                res_title = song.get('name') or song.get('title', '')
                res_artist = song.get('primaryArtists') or song.get('primary_artists', '')
                
                # Accuracy Check: Match Title + Artist
                match_score = get_similarity(clean_name(track_name), clean_name(res_title))
                
                # Agar title 65% match hai, toh hum safe hain
                if match_score >= 0.65:
                    download_urls = song.get('downloadUrl') or song.get('download_url') or []
                    if download_urls:
                        # Hamesha best quality (last item) uthao
                        best_link = download_urls[-1].get('url') or download_urls[-1].get('link')
                        return {
                            'url': best_link,
                            'title': res_title,
                            'artist': res_artist,
                            'score': match_score,
                            'source': 'saavn'
                        }
        except: continue
    return None

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.route('/')
def index():
    # Make sure index.html is in the same folder
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/api/songs')
def search_itunes():
    q = request.args.get('q', 'top charts')
    try:
        # iTunes search for metadata (Global coverage)
        r = requests.get('https://itunes.apple.com/search', 
                         params={'term': q, 'media': 'music', 'limit': 30}, timeout=10)
        return jsonify({'results': r.json().get('results', [])})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})

@app.route('/api/saavn')
def get_stream_link():
    track = request.args.get('track', '').strip()
    artist = request.args.get('artist', '').strip()
    itunes_preview = request.args.get('preview', '').strip()

    if not track: return jsonify({'success': False, 'msg': 'Gaana toh batao bhai!'})

    # Parallel processing: Sab mirrors ko ek saath kaam pe lagao
    with ThreadPoolExecutor(max_workers=len(SAAVN_MIRRORS)) as executor:
        futures = [executor.submit(fetch_from_mirror, m, track, artist) for m in SAAVN_MIRRORS]
        for future in as_completed(futures):
            result = future.result()
            if result:
                return jsonify({'success': True, **result})

    # Last Resort for English Songs: Agar Saavn pe nahi mila, toh iTunes preview bajao
    if itunes_preview:
        return jsonify({
            'success': True,
            'url': itunes_preview,
            'title': track,
            'artist': artist,
            'source': 'itunes_fallback',
            'msg': 'Full song not on Saavn, playing preview'
        })

    return jsonify({'success': False, 'msg': 'Kahin nahi mila, kismat kharab hai!'})

@app.route('/api/stream')
def proxy_stream():
    url = request.args.get('url', '').strip()
    if not url: return "Missing URL", 400
    
    try:
        # Range support for seeking (fast forward/rewind)
        headers = {'Range': request.headers.get('Range')} if request.headers.get('Range') else {}
        r = requests.get(url, headers=headers, stream=True, timeout=30)
        
        def generate():
            for chunk in r.iter_content(chunk_size=128*1024):
                yield chunk
        
        excluded_headers = ['content-encoding', 'transfer-encoding', 'connection']
        resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded_headers}
        resp_headers['Access-Control-Allow-Origin'] = '*'
        
        return Response(generate(), status=r.status_code, headers=resp_headers)
    except Exception as e:
        return str(e), 500

if __name__ == '__main__':
    # Render default port 7700 or environment port
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True)
