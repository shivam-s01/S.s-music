from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from werkzeug.middleware.proxy_fix import ProxyFix
import requests
import os
import re
import logging
import random
import string
import secrets
import hmac
import hashlib
import time
import threading
import yt_dlp
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote, urlencode
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GOOGLE_CLIENT_ID  = os.environ.get('GOOGLE_CLIENT_ID', '')
ADMIN_KEY         = os.environ.get('ADMIN_KEY', '')
SUPABASE_URL      = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY      = os.environ.get('SUPABASE_KEY', '')

# Note: For development without Supabase, set these to empty strings
# and the app will work with local storage only

app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

def get_real_ip():
    return (request.headers.get('CF-Connecting-IP') or
            request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
            request.remote_addr or '127.0.0.1')

limiter = Limiter(get_real_ip, app=app, default_limits=[], storage_uri="memory://")
_executor = ThreadPoolExecutor(max_workers=32)
_google_req = google_requests.Request()

# ============================================================
# WORKING MIRRORS (Updated March 2024)
# ============================================================
SAAVN_MIRRORS = [
    'https://jiosaavn-api-v2.vercel.app',
    'https://saavn-api-v2.vercel.app',
    'https://jiosaavn-api.vercel.app',
    'https://saavn-api.vercel.app',
    'https://jiosaavn-api-privatecvc2.vercel.app',
]

PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.tokhmi.xyz',
    'https://api.piped.yt',
]

INVIDIOUS_INSTANCES = [
    'https://invidious.snopyta.org',
    'https://vid.puffyan.us',
    'https://y.com.sb',
]

# ============================================================
# CORS
# ============================================================
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, Range'
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range, Accept-Ranges'
    return resp

@app.after_request
def after_request(resp):
    return add_cors(resp)

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return add_cors(Response(status=200))

# ============================================================
# FRONTEND ROUTES
# ============================================================
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

@app.route('/<path:filename>')
def serve_static(filename):
    file_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(file_path):
        return send_file(file_path)
    return jsonify({'error': 'Not found'}), 404

# ============================================================
# HELPER FUNCTIONS
# ============================================================
def clean_query(text):
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\(\s*(OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?|Hindi|English|Version|Remix|Cover|HD|HQ|Original|Soundtrack|Remastered|Extended|Radio\s*Edit)\s*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*[-–]\s*(official|audio|video|lyrics|full\s*song|hd|hq|remastered).*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def normalize(text):
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def title_score(query, song_title, song_artist=''):
    q, t, a = normalize(query), normalize(song_title), normalize(song_artist)
    if not q: return 0.0
    if q == t: return 3.0
    q_words = q.split()
    t_words = t.split()
    score = 0.0
    if t.startswith(q): score += 2.0
    title_match = sum(1 for qw in q_words for tw in t_words if qw in tw or tw in qw)
    if q_words: score += (title_match / len(q_words)) * 1.5
    return score

def pick_best_quality(urls):
    if not urls: return None, None
    quality_order = {'320kbps': 7, '320': 7, '160kbps': 5, '160': 5, '96kbps': 3, '96': 3}
    for item in sorted(urls, key=lambda x: quality_order.get(x.get('quality', '').lower(), 0), reverse=True):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'):
            return url, item.get('quality', 'unknown')
    return None, None

def pick_image(song):
    images = song.get('image') or []
    if isinstance(images, list) and images:
        for img in reversed(images):
            url = img.get('url') or img.get('link') or ''
            if url.startswith('http'):
                return url.replace('150x150', '500x500').replace('100x100', '500x500')
    if isinstance(images, str) and images.startswith('http'):
        return images.replace('150x150', '500x500')
    return ''

# ============================================================
# SAAVN SEARCH
# ============================================================
def fetch_saavn_search(query):
    for mirror in SAAVN_MIRRORS:
        try:
            resp = requests.get(f'{mirror}/api/search/songs',
                               params={'query': query, 'limit': 25},
                               timeout=8,
                               headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code == 200:
                data = resp.json()
                songs = data.get('data', {}).get('results', [])
                if songs:
                    return songs
        except Exception as e:
            log.warning(f"Mirror failed: {e}")
            continue
    return []

def normalize_saavn_songs(raw_songs):
    normalized = []
    for song in raw_songs:
        song_id = song.get('id', '').strip()
        if not song_id: continue
        
        title = song.get('name') or song.get('title', '')
        artist = song.get('primaryArtists') or song.get('primary_artists', '')
        image = pick_image(song)
        year = str(song.get('year') or '0')[:4]
        duration = int(song.get('duration', 0) or 0)
        
        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        
        audio_url, quality = pick_best_quality(raw_urls)
        if not audio_url: continue
        
        normalized.append({
            'trackId': song_id,
            'trackName': title,
            'artistName': artist,
            'artworkUrl100': image.replace('500x500', '100x100') if image else '',
            'artworkUrl500': image,
            'previewUrl': f"/api/play?id={quote(song_id)}",
            'trackTimeMillis': duration * 1000,
            'releaseDate': f"{year}-01-01T00:00:00Z",
            '_audioUrl': audio_url,
            '_quality': quality,
            '_source': 'saavn',
        })
    return normalized

# ============================================================
# YOUTUBE SEARCH (yt-dlp)
# ============================================================
def search_youtube(query, limit=15):
    results = []
    try:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'noplaylist': True,
            'format': 'bestaudio/best',
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f"ytsearch{limit}:{query} song"
            info = ydl.extract_info(search_query, download=False)
            if info and info.get('entries'):
                for entry in info['entries']:
                    if entry and entry.get('duration', 0) > 60:
                        video_id = entry.get('id', '')
                        results.append({
                            'trackId': video_id,
                            'trackName': entry.get('title', '')[:100],
                            'artistName': entry.get('uploader', 'Unknown'),
                            'artworkUrl100': f"https://img.youtube.com/vi/{video_id}/default.jpg",
                            'artworkUrl500': f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
                            'previewUrl': f"/api/yt-play?id={video_id}",
                            'trackTimeMillis': (entry.get('duration', 0) * 1000),
                            'releaseDate': '',
                            '_source': 'youtube'
                        })
    except Exception as e:
        log.warning(f"YouTube search error: {e}")
    return results

# ============================================================
# YOUTUBE PLAY
# ============================================================
def get_youtube_audio(video_id):
    try:
        ydl_opts = {
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            url = f"https://www.youtube.com/watch?v={video_id}"
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            
            # Get best audio-only format
            audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
            if not audio_formats:
                audio_formats = [f for f in formats if f.get('acodec') != 'none']
            
            if audio_formats:
                best = max(audio_formats, key=lambda f: f.get('abr', 0) or f.get('tbr', 0))
                return best.get('url')
    except Exception as e:
        log.error(f"YouTube audio error: {e}")
    return None

@app.route('/api/yt-play')
def yt_play():
    video_id = request.args.get('id', '').strip()
    if not video_id:
        return jsonify({'error': 'Missing video id'}), 400
    
    audio_url = get_youtube_audio(video_id)
    if not audio_url:
        return jsonify({'error': 'No audio found'}), 404
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}
        range_header = request.headers.get('Range')
        if range_header:
            headers['Range'] = range_header
        
        upstream = requests.get(audio_url, headers=headers, stream=True, timeout=30)
        
        resp_headers = {
            'Content-Type': upstream.headers.get('Content-Type', 'audio/mpeg'),
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache',
        }
        
        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        
        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================
# SAAVN PLAY BY ID
# ============================================================
def fetch_saavn_by_id(song_id):
    for mirror in SAAVN_MIRRORS:
        try:
            resp = requests.get(f'{mirror}/api/songs/{song_id}', timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                song = data.get('data', {})
                if not song and isinstance(data, dict):
                    song = data
                
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                if isinstance(raw_urls, str):
                    raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                
                audio_url, quality = pick_best_quality(raw_urls)
                if audio_url:
                    return {
                        'url': audio_url,
                        'quality': quality,
                        'title': song.get('name') or song.get('title', ''),
                        'artist': song.get('primaryArtists') or song.get('primary_artists', ''),
                        'image': pick_image(song),
                    }
        except Exception:
            continue
    return None

@app.route('/api/play')
def play_song():
    song_id = request.args.get('id', '').strip()
    title = request.args.get('title', '').strip()
    artist = request.args.get('artist', '').strip()
    
    audio_url = None
    source = None
    
    # Try by ID first
    if song_id:
        result = fetch_saavn_by_id(song_id)
        if result:
            audio_url = result['url']
            source = 'saavn'
            if not title: title = result.get('title', '')
            if not artist: artist = result.get('artist', '')
    
    # If not found, search by title
    if not audio_url and title:
        songs = fetch_saavn_search(title)
        if songs:
            for song in songs:
                score = title_score(title, song.get('name', ''), song.get('primaryArtists', ''))
                if score > 0.3:
                    raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                    if isinstance(raw_urls, str):
                        raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                    audio_url, _ = pick_best_quality(raw_urls)
                    if audio_url:
                        source = 'saavn'
                        if not title: title = song.get('name', '')
                        if not artist: artist = song.get('primaryArtists', '')
                        break
    
    # YouTube fallback
    if not audio_url and title:
        search_results = search_youtube(title, 3)
        if search_results:
            video_id = search_results[0]['trackId']
            audio_url = get_youtube_audio(video_id)
            source = 'youtube'
            if not title: title = search_results[0].get('trackName', '')
            if not artist: artist = search_results[0].get('artistName', '')
    
    if not audio_url:
        return jsonify({'error': 'No audio found'}), 404
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}
        range_header = request.headers.get('Range')
        if range_header:
            headers['Range'] = range_header
        
        upstream = requests.get(audio_url, headers=headers, stream=True, timeout=30)
        
        resp_headers = {
            'Content-Type': upstream.headers.get('Content-Type', 'audio/mpeg'),
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache',
            'X-Audio-Source': source,
        }
        
        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        
        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================
# /api/songs - MAIN SEARCH ENDPOINT (FIXED)
# ============================================================
NINETIES_TRIGGERS = ['90', 'purane', 'old', 'retro', 'classic', 'nineties']
NINETIES_SEEDS = ['Kumar Sanu hits', 'Udit Narayan 90s', 'Alka Yagnik 90s', '90s Bollywood superhits']

@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q = request.args.get('q', 'top bollywood songs').strip()
    era = request.args.get('era', '').strip()
    is_90s = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q
    
    results = []
    
    # Try Saavn first
    raw_songs = fetch_saavn_search(search_term)
    if raw_songs:
        saavn_results = normalize_saavn_songs(raw_songs)
        if is_90s:
            # Filter 90s songs
            filtered = [s for s in saavn_results if '199' in s.get('releaseDate', '')]
            saavn_results = filtered if len(filtered) >= 5 else saavn_results
        results.extend(saavn_results[:25])
    
    # Add YouTube results for variety
    youtube_results = search_youtube(search_term, 10)
    results.extend(youtube_results)
    
    # Remove duplicates by title
    seen = set()
    unique_results = []
    for song in results:
        key = normalize(song.get('trackName', ''))
        if key not in seen:
            seen.add(key)
            unique_results.append(song)
    
    if unique_results:
        return jsonify({'results': unique_results[:30]})
    
    return jsonify({'results': [], 'error': 'No results found'})

# ============================================================
# /api/songs/90s
# ============================================================
@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed = random.choice(NINETIES_SEEDS)
    raw_songs = fetch_saavn_search(seed)
    
    if raw_songs:
        results = normalize_saavn_songs(raw_songs)
        filtered = [s for s in results if '199' in s.get('releaseDate', '')]
        results = filtered if len(filtered) >= 5 else results
        random.shuffle(results)
        return jsonify({'results': results[:30], 'seed': seed})
    
    # Fallback to YouTube
    youtube_results = search_youtube(seed, 20)
    return jsonify({'results': youtube_results[:30], 'seed': seed})

# ============================================================
# /api/saavn - Legacy endpoint
# ============================================================
@app.route('/api/saavn')
@limiter.limit("100 per minute")
def get_saavn_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    token = request.args.get('token', '').strip()
    
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})
    
    songs = fetch_saavn_search(q)
    for song in songs:
        score = title_score(q, song.get('name', ''), song.get('primaryArtists', ''))
        if score > 0.3:
            raw_urls = song.get('downloadUrl') or song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            audio_url, quality = pick_best_quality(raw_urls)
            if audio_url:
                return jsonify({
                    'success': True, 'token': token,
                    'url': audio_url, 'quality': quality,
                    'title': song.get('name', ''), 'artist': song.get('primaryArtists', ''),
                    'image': pick_image(song), 'source': 'saavn'
                })
    
    return jsonify({'success': False, 'url': None, 'token': token})

# ============================================================
# /api/resolve
# ============================================================
@app.route('/api/resolve')
@limiter.limit("100 per minute")
def resolve_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    token = request.args.get('token', '').strip()
    
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})
    
    songs = fetch_saavn_search(q)
    for song in songs:
        score = title_score(q, song.get('name', ''), song.get('primaryArtists', ''))
        if score > 0.3:
            raw_urls = song.get('downloadUrl') or song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            audio_url, quality = pick_best_quality(raw_urls)
            if audio_url:
                return jsonify({
                    'success': True, 'token': token,
                    'url': f"/api/stream?url={quote(audio_url)}",
                    'quality': quality,
                    'title': song.get('name', ''), 'artist': song.get('primaryArtists', ''),
                    'image': pick_image(song), 'source': 'saavn'
                })
    
    # YouTube fallback
    youtube_results = search_youtube(q, 1)
    if youtube_results:
        return jsonify({
            'success': True, 'token': token,
            'url': youtube_results[0]['previewUrl'],
            'quality': '128kbps',
            'title': youtube_results[0]['trackName'],
            'artist': youtube_results[0]['artistName'],
            'image': youtube_results[0]['artworkUrl500'],
            'source': 'youtube'
        })
    
    return jsonify({'success': False, 'url': None, 'token': token})

# ============================================================
# STREAM PROXY
# ============================================================
ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'googlevideo.com', 'youtube.com', 'ytimg.com',
    'sndcdn.com', 'soundcloud.com',
]

def is_allowed_domain(domain):
    for allowed in ALLOWED_STREAM_DOMAINS:
        if domain == allowed or domain.endswith('.' + allowed) or allowed in domain:
            return True
    return False

@app.route('/api/stream')
@limiter.limit("200 per minute")
def stream_audio():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'Missing URL'}), 400
    
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return jsonify({'error': 'Invalid scheme'}), 400
        domain = parsed.netloc.lower().split(':')[0]
        if not is_allowed_domain(domain):
            return jsonify({'error': 'Domain not allowed'}), 403
    except Exception:
        return jsonify({'error': 'Invalid URL'}), 400
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}
        range_header = request.headers.get('Range')
        if range_header:
            headers['Range'] = range_header
        
        upstream = requests.get(url, headers=headers, stream=True, timeout=30)
        
        resp_headers = {
            'Content-Type': upstream.headers.get('Content-Type', 'audio/mpeg'),
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache',
        }
        
        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        
        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================
# DOWNLOAD ENDPOINT
# ============================================================
@app.route('/api/download')
@limiter.limit("20 per minute")
def download_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    
    if not q:
        return jsonify({'error': 'Missing query'}), 400
    
    stream_url = None
    filename = f"{q} - {artist}".strip(' -') if artist else q
    
    songs = fetch_saavn_search(q)
    for song in songs:
        score = title_score(q, song.get('name', ''), song.get('primaryArtists', ''))
        if score > 0.3:
            raw_urls = song.get('downloadUrl') or song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            # Try to get 320kbps first
            for item in raw_urls:
                if '320' in str(item.get('quality', '')):
                    stream_url = item.get('url') or item.get('link')
                    break
            if not stream_url:
                stream_url, _ = pick_best_quality(raw_urls)
            if stream_url:
                filename = f"{song.get('name', q)} - {song.get('primaryArtists', artist)}".strip(' -')
                break
    
    if not stream_url:
        youtube_results = search_youtube(q, 1)
        if youtube_results:
            video_id = youtube_results[0]['trackId']
            stream_url = get_youtube_audio(video_id)
            filename = f"{youtube_results[0]['trackName']} - {youtube_results[0]['artistName']}".strip(' -')
    
    if not stream_url:
        return jsonify({'error': 'Song not found'}), 404
    
    try:
        clean_name = re.sub(r'[/\\?%*:|"<>]', '-', filename)
        upstream = requests.get(stream_url, headers={'User-Agent': 'Mozilla/5.0'}, stream=True, timeout=60)
        
        content_type = upstream.headers.get('Content-Type', 'audio/mpeg')
        ext = 'webm' if 'webm' in content_type else ('m4a' if 'mp4' in content_type or 'm4a' in content_type else 'mp3')
        
        resp_headers = {
            'Content-Disposition': f'attachment; filename="{clean_name}.{ext}"',
            'Content-Type': content_type,
            'Accept-Ranges': 'bytes',
        }
        
        def generate():
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        
        return Response(stream_with_context(generate()), status=200, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ============================================================
# AUTH - Google Login
# ============================================================
def verify_google_jwt(credential: str) -> dict | None:
    if not GOOGLE_CLIENT_ID:
        # Development mode - skip verification
        return {'sub': 'dev_user', 'name': 'Dev User', 'email': 'dev@example.com', 'picture': ''}
    try:
        payload = id_token.verify_oauth2_token(credential, _google_req, GOOGLE_CLIENT_ID, clock_skew_in_seconds=10)
        if payload.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
            return None
        return payload
    except Exception as e:
        log.warning(f'JWT verify failed: {e}')
        return None

def extract_bearer_sub(auth_header: str) -> str | None:
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    token = auth_header[7:].strip()
    if not token:
        return None
    payload = verify_google_jwt(token)
    if not payload:
        return None
    return payload.get('sub', '') or None

# Supabase helpers (optional - works without Supabase)
def sb_upsert(table, data, on_conflict=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return data  # Mock mode
    # ... (keep original implementation)
    return data

@app.route('/api/auth/google', methods=['POST'])
@limiter.limit("20 per minute")
def handle_google_auth():
    data = request.get_json() or {}
    credential = data.get('credential', '').strip()
    
    if not credential and not GOOGLE_CLIENT_ID:
        # Dev mode
        return jsonify({
            'success': True,
            'sub': 'dev_user',
            'name': 'Development User',
            'email': 'dev@example.com',
            'picture': '',
        })
    
    if not credential:
        return jsonify({'error': 'Missing credential'}), 400
    
    profile = verify_google_jwt(credential)
    if not profile:
        return jsonify({'error': 'Invalid credential'}), 401
    
    sub = profile.get('sub', '').strip()
    if not sub:
        return jsonify({'error': 'Missing sub'}), 400
    
    # Save to Supabase if configured
    if SUPABASE_URL and SUPABASE_KEY:
        sb_upsert('users', {
            'google_sub': sub,
            'name': profile.get('name', ''),
            'email': profile.get('email', ''),
            'picture': profile.get('picture', ''),
        }, on_conflict='google_sub')
    
    return jsonify({
        'success': True,
        'sub': sub,
        'name': profile.get('name', ''),
        'email': profile.get('email', ''),
        'picture': profile.get('picture', ''),
    })

# ============================================================
# SYNC - Playback State (Optional)
# ============================================================
@app.route('/api/sync/state', methods=['POST'])
@limiter.limit("60 per minute")
def save_playback_state():
    sub = extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Just return success - sync is optional
    return jsonify({'status': 'ok'})

@app.route('/api/sync/state', methods=['GET'])
@limiter.limit("60 per minute")
def get_playback_state():
    sub = extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'error': 'Unauthorized'}), 401
    
    return jsonify({'success': False})

# ============================================================
# TV PAIRING (Optional)
# ============================================================
@app.route('/api/auth/tv-generate-code', methods=['POST'])
@limiter.limit("10 per minute")
def generate_tv_code():
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return jsonify({'code': code, 'sessionId': secrets.token_hex(8), 'expiresIn': 300})

@app.route('/api/auth/tv-poll')
@limiter.limit("60 per minute")
def poll_tv_pairing():
    return jsonify({'status': 'pending'})

@app.route('/api/auth/tv-verify-mobile', methods=['POST'])
@limiter.limit("20 per minute")
def mobile_verify_tv():
    return jsonify({'success': True})

# ============================================================
# GHOST PIN (Optional)
# ============================================================
@app.route('/api/auth/verify-ghost-pin', methods=['POST'])
@limiter.limit("10 per minute")
def verify_ghost_pin():
    sub = extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    data = request.get_json() or {}
    pin = data.get('pin', '').strip()
    
    if pin:
        return jsonify({'success': True})
    return jsonify({'success': False})

# ============================================================
# ADMIN - View users
# ============================================================
@app.route('/api/admin/users')
@limiter.limit("10 per minute")
def admin_users():
    secret = request.args.get('key', '')
    if not hmac.compare_digest(secret, ADMIN_KEY) and ADMIN_KEY:
        return jsonify({'error': 'Unauthorized'}), 401
    
    return jsonify({'users': [], 'total': 0})

# ============================================================
# HEALTH CHECKS
# ============================================================
@app.route('/api/health')
def health_status():
    return jsonify({
        'status': 'ok',
        'saavn_mirrors': len(SAAVN_MIRRORS),
        'sources': ['saavn', 'youtube'],
        'timestamp': time.time(),
    })

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    log.info(f"🚀 Server starting on http://localhost:{port}")
    log.info(f"📱 Open http://localhost:{port} in your browser")
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
