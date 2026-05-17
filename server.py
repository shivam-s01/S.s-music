from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
import requests, os, re, logging, random, time, threading, sys, atexit
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from urllib.parse import urlparse
from flask_limiter import Limiter

# ── RECURSION GUARD ──
sys.setrecursionlimit(2500)

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════
# APP INIT (Cloudflare & Proxy Fix)
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__, static_folder='static')
# Cloudflare ke real IP ke liye ye zaroori hai
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# Real IP detection for Cloudflare
def get_real_ip():
    return (
        request.headers.get('CF-Connecting-IP') or
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.remote_addr or
        '127.0.0.1'
    )

limiter = Limiter(get_real_ip, app=app, storage_uri="memory://")

# ── CONSTANTS & MIRRORS ──
SAAVN_MIRRORS = [
    'https://saavn.dev',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app'
]

# ── THREAD POOL ──
_saavn_executor = ThreadPoolExecutor(max_workers=len(SAAVN_MIRRORS))
atexit.register(_saavn_executor.shutdown, wait=False)

# ── NO RECURSION LEVENSHTEIN ──
def levenshtein(s1, s2):
    if len(s1) < len(s2): s1, s2 = s2, s1
    if not s2: return len(s1)
    prev = list(range(len(s2) + 1))
    for c1 in s1:
        curr = [prev[0] + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]

# ═══════════════════════════════════════════════════════════════
# PWA & APK ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/manifest.json')
def manifest():
    return send_file(os.path.join(BASE_DIR, 'manifest.json'), mimetype='application/manifest+json')

@app.route('/sw.js')
def service_worker():
    resp = send_file(os.path.join(BASE_DIR, 'sw.js'), mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp

@app.route('/.well-known/assetlinks.json')
def assetlinks():
    return send_file(os.path.join(BASE_DIR, 'static/assetlinks.json'))

# ═══════════════════════════════════════════════════════════════
# MUSIC LOGIC (Saavn + iTunes)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
def get_songs():
    q = request.args.get('q', 'top songs').strip()
    try:
        r = requests.get('https://itunes.apple.com/search', params={
            'term': q, 'media': 'music', 'entity': 'song', 'limit': 30, 'country': 'IN'
        }, timeout=8)
        return jsonify(r.json())
    except Exception as e:
        log.error(f"iTunes Error: {e}")
        return jsonify({'results': []})

@app.route('/api/stream')
def stream_audio():
    url = request.args.get('url', '').strip()
    if not url: return "Missing URL", 400
    try:
        # Proxying stream to avoid CORS and hide origin
        upstream = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, stream=True, timeout=30)
        return Response(stream_with_context(upstream.iter_content(chunk_size=65536)), 
                        content_type=upstream.headers.get('Content-Type'))
    except Exception as e:
        return str(e), 500

# Serve static files (CSS/JS/Images)
@app.route('/<path:filename>')
def serve_static(filename):
    file_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(file_path):
        return send_file(file_path)
    return jsonify({'error': 'Not found'}), 404

# ═══════════════════════════════════════════════════════════════
# RUN SERVER
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    # Railway environment variable "PORT" use karega
    port = int(os.environ.get('PORT', 7700))
    app.run(host='0.0.0.0', port=port, threaded=True)
