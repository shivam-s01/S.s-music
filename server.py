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

# Environment variables (optional - for production)
GOOGLE_CLIENT_ID  = os.environ.get('GOOGLE_CLIENT_ID', '')
ADMIN_KEY         = os.environ.get('ADMIN_KEY', '')
SUPABASE_URL      = os.environ.get('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY      = os.environ.get('SUPABASE_KEY', '')

# ═══════════════════════════════════════════════════════════════
# APP INIT
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__, static_folder='static')
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

def get_real_ip():
    return (
        request.headers.get('CF-Connecting-IP') or
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.remote_addr or '127.0.0.1'
    )

limiter = Limiter(get_real_ip, app=app, default_limits=[], storage_uri="memory://")
_executor = ThreadPoolExecutor(max_workers=32)
_google_req = google_requests.Request()

# ═══════════════════════════════════════════════════════════════
# WORKING MIRRORS (UPDATED)
# ═══════════════════════════════════════════════════════════════
SAAVN_MIRRORS = [
    'https://jiosaavn-api-v2.vercel.app',
    'https://saavn-api-v2.vercel.app',
    'https://jiosaavn-api.vercel.app',
    'https://saavn-api.vercel.app',
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-eight.vercel.app',
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

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'googlevideo.com', 'youtube.com', 'ytimg.com',
    'sndcdn.com', 'soundcloud.com',
]

QUALITY_RANK = {
    '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
    '96kbps': 3, '96': 3, '48kbps': 2, '48': 2,
}

NINETIES_SEEDS = [
    "Kumar Sanu hits", "Udit Narayan 90s", "Alka Yagnik 90s",
    "Lata Mangeshkar 90s", "Sonu Nigam 90s hits",
    "90s Bollywood superhits",
]

NINETIES_TRIGGERS = ['90', 'purane', 'old', 'retro', 'classic', 'nineties']

# ═══════════════════════════════════════════════════════════════
# CACHE
# ═══════════════════════════════════════════════════════════════
_meta_cache = {}
META_CACHE_TTL = 600

def _cache_get(key):
    entry = _meta_cache.get(key)
    if not entry: return None
    ts, data = entry
    if time.time() - ts > META_CACHE_TTL:
        del _meta_cache[key]
        return None
    return data

def _cache_set(key, data):
    _meta_cache[key] = (time.time(), data)
    if len(_meta_cache) > 200:
        oldest = min(_meta_cache, key=lambda k: _meta_cache[k][0])
        del _meta_cache[oldest]

# ═══════════════════════════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════════════════════════
@app.after_request
def after_request(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, Range'
    resp.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range, Accept-Ranges'
    return resp

@app.route('/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return Response(status=200)

# ═══════════════════════════════════════════════════════════════
# FRONTEND ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/<path:filename>')
def serve_static(filename):
    file_path = os.path.join(BASE_DIR, filename)
    if os.path.isfile(file_path):
        return send_file(file_path)
    return jsonify({'error': 'Not found'}), 404

# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════
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
    for item in urls:
        q = item.get('quality', '').lower()
        if '320' in q:
            url = item.get('url') or item.get('link')
            if url and url.startswith('http'):
                return url, '320kbps'
    for item in urls:
        url = item.get('url') or item.get('link')
        if url and url.startswith('http'):
            return url, item.get('quality', 'unknown')
    return None, None

def pick_image(song):
    images = song.get('image') or []
    if isinstance(images, list) and images:
        for img in reversed(images):
            url = img.get('url') or img.get('link')
            if url and url.startswith('http'):
                return url.replace('150x150', '500x500').replace('100x100', '500x500')
    if isinstance(images, str) and images.startswith('http'):
        return images.replace('150x150', '500x500')
    return ''

# ═══════════════════════════════════════════════════════════════
# SAAVN SEARCH
# ═══════════════════════════════════════════════════════════════
def search_saavn(query):
    for mirror in SAAVN_MIRRORS:
        try:
            resp = requests.get(
                f'{mirror}/api/search/songs',
                params={'query': query, 'limit': 25},
                timeout=8,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            if resp.status_code == 200:
                data = resp.json()
                songs = data.get('data', {}).get('results', [])
                if songs:
                    return songs
        except Exception:
            continue
    return []

def get_saavn_song_by_id(song_id):
    for mirror in SAAVN_MIRRORS:
        try:
            resp = requests.get(f'{mirror}/api/songs/{song_id}', timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                song = data.get('data', {})
                if not song and isinstance(data, dict):
                    song = data
                if song:
                    return song
        except Exception:
            continue
    return None

# ═══════════════════════════════════════════════════════════════
# YOUTUBE SEARCH (yt-dlp)
# ═══════════════════════════════════════════════════════════════
def search_youtube(query, limit=12):
    results = []
    try:
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'noplaylist': True,
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
                            'trackTimeMillis': entry.get('duration', 0) * 1000,
                            '_source': 'youtube'
                        })
    except Exception as e:
        log.warning(f"YouTube search error: {e}")
    return results

def get_youtube_audio_url(video_id):
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

# ═══════════════════════════════════════════════════════════════
# /api/songs - MAIN ENDPOINT
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs')
@limiter.limit("60 per minute")
def get_songs():
    q = request.args.get('q', 'top bollywood songs').strip()
    era = request.args.get('era', '').strip()
    is_90s = (era == '90s') or any(t in q.lower() for t in NINETIES_TRIGGERS)
    
    search_term = random.choice(NINETIES_SEEDS) if is_90s else q
    
    cache_key = f"songs:{search_term.lower()}"
    cached = _cache_get(cache_key)
    if cached:
        return jsonify({'results': cached, '_cached': True})
    
    results = []
    
    # Try Saavn
    raw_songs = search_saavn(search_term)
    for song in raw_songs[:20]:
        song_id = song.get('id', '')
        title = song.get('name', '')
        artist = song.get('primaryArtists', '')
        image = pick_image(song)
        duration = int(song.get('duration', 0) or 0)
        
        # Get audio URL
        raw_urls = song.get('downloadUrl') or song.get('download_url') or []
        if isinstance(raw_urls, str):
            raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
        audio_url, quality = pick_best_quality(raw_urls)
        
        if audio_url:
            results.append({
                'trackId': song_id,
                'trackName': title,
                'artistName': artist,
                'artworkUrl100': image.replace('500x500', '100x100') if image else '',
                'artworkUrl500': image,
                'previewUrl': f"/api/play?id={quote(song_id)}",
                'trackTimeMillis': duration * 1000,
                'releaseDate': f"{song.get('year', '0')}-01-01",
                '_source': 'saavn'
            })
    
    # Add YouTube results for variety if needed
    if len(results) < 10:
        youtube_results = search_youtube(search_term, 10)
        for yt in youtube_results:
            # Avoid duplicates
            if not any(r.get('trackName') == yt.get('trackName') for r in results):
                results.append(yt)
    
    if results:
        _cache_set(cache_key, results[:30])
        return jsonify({'results': results[:30]})
    
    return jsonify({'results': [], 'error': 'No results found'})

# ═══════════════════════════════════════════════════════════════
# /api/songs/90s
# ═══════════════════════════════════════════════════════════════
@app.route('/api/songs/90s')
@limiter.limit("60 per minute")
def get_90s_songs():
    seed = random.choice(NINETIES_SEEDS)
    cache_key = f"songs:{seed.lower()}"
    cached = _cache_get(cache_key)
    if cached:
        return jsonify({'results': cached, 'seed': seed, '_cached': True})
    
    results = []
    raw_songs = search_saavn(seed)
    
    for song in raw_songs:
        year = int(song.get('year', 0) or 0)
        if 1990 <= year <= 1999:
            song_id = song.get('id', '')
            title = song.get('name', '')
            artist = song.get('primaryArtists', '')
            image = pick_image(song)
            duration = int(song.get('duration', 0) or 0)
            
            raw_urls = song.get('downloadUrl') or song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            audio_url, quality = pick_best_quality(raw_urls)
            
            if audio_url:
                results.append({
                    'trackId': song_id,
                    'trackName': title,
                    'artistName': artist,
                    'artworkUrl100': image.replace('500x500', '100x100') if image else '',
                    'artworkUrl500': image,
                    'previewUrl': f"/api/play?id={quote(song_id)}",
                    'trackTimeMillis': duration * 1000,
                    'releaseDate': f"{year}-01-01",
                    '_source': 'saavn'
                })
    
    if len(results) < 5:
        youtube_results = search_youtube(seed, 20)
        for yt in youtube_results:
            if not any(r.get('trackName') == yt.get('trackName') for r in results):
                results.append(yt)
    
    if results:
        random.shuffle(results)
        _cache_set(cache_key, results[:30])
        return jsonify({'results': results[:30], 'seed': seed})
    
    return jsonify({'results': [], 'error': 'No results found'})

# ═══════════════════════════════════════════════════════════════
# /api/play - PLAY AUDIO
# ═══════════════════════════════════════════════════════════════
@app.route('/api/play')
@limiter.limit("200 per minute")
def play_song():
    song_id = request.args.get('id', '').strip()
    title = request.args.get('title', '').strip()
    artist = request.args.get('artist', '').strip()
    
    audio_url = None
    
    # Try by ID first
    if song_id:
        song = get_saavn_song_by_id(song_id)
        if song:
            raw_urls = song.get('downloadUrl') or song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            audio_url, _ = pick_best_quality(raw_urls)
            if not title:
                title = song.get('name', '')
            if not artist:
                artist = song.get('primaryArtists', '')
    
    # If not found, search by title
    if not audio_url and title:
        songs = search_saavn(title)
        for song in songs:
            score = title_score(title, song.get('name', ''), song.get('primaryArtists', ''))
            if score > 0.3:
                raw_urls = song.get('downloadUrl') or song.get('download_url') or []
                if isinstance(raw_urls, str):
                    raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
                audio_url, _ = pick_best_quality(raw_urls)
                if audio_url:
                    if not title: title = song.get('name', '')
                    if not artist: artist = song.get('primaryArtists', '')
                    break
    
    # YouTube fallback
    if not audio_url and title:
        youtube_results = search_youtube(f"{title} {artist}".strip(), 3)
        if youtube_results:
            video_id = youtube_results[0]['trackId']
            audio_url = get_youtube_audio_url(video_id)
            if not title: title = youtube_results[0].get('trackName', '')
            if not artist: artist = youtube_results[0].get('artistName', '')
    
    if not audio_url:
        return jsonify({'error': 'No audio found'}), 404
    
    # Stream the audio
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
            upstream.close()
        
        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# /api/yt-play - YOUTUBE PLAY
# ═══════════════════════════════════════════════════════════════
@app.route('/api/yt-play')
@limiter.limit("200 per minute")
def yt_play():
    video_id = request.args.get('id', '').strip()
    if not video_id:
        return jsonify({'error': 'Missing video id'}), 400
    
    audio_url = get_youtube_audio_url(video_id)
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
            upstream.close()
        
        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# /api/saavn - LEGACY ENDPOINT
# ═══════════════════════════════════════════════════════════════
@app.route('/api/saavn')
@limiter.limit("100 per minute")
def get_saavn_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    token = request.args.get('token', '').strip()
    
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})
    
    songs = search_saavn(q)
    for song in songs:
        score = title_score(q, song.get('name', ''), song.get('primaryArtists', ''))
        if score > 0.3:
            raw_urls = song.get('downloadUrl') or song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            audio_url, quality = pick_best_quality(raw_urls)
            if audio_url:
                return jsonify({
                    'success': True,
                    'token': token,
                    'url': audio_url,
                    'quality': quality,
                    'title': song.get('name', ''),
                    'artist': song.get('primaryArtists', ''),
                    'image': pick_image(song),
                    'source': 'saavn'
                })
    
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/resolve
# ═══════════════════════════════════════════════════════════════
@app.route('/api/resolve')
@limiter.limit("100 per minute")
def resolve_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    token = request.args.get('token', '').strip()
    
    if not q:
        return jsonify({'success': False, 'url': None, 'token': token})
    
    songs = search_saavn(q)
    for song in songs:
        score = title_score(q, song.get('name', ''), song.get('primaryArtists', ''))
        if score > 0.3:
            raw_urls = song.get('downloadUrl') or song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            audio_url, quality = pick_best_quality(raw_urls)
            if audio_url:
                return jsonify({
                    'success': True,
                    'token': token,
                    'url': f"/api/stream?url={quote(audio_url)}",
                    'quality': quality,
                    'title': song.get('name', ''),
                    'artist': song.get('primaryArtists', ''),
                    'image': pick_image(song),
                    'source': 'saavn'
                })
    
    # YouTube fallback
    youtube_results = search_youtube(f"{q} {artist}".strip(), 1)
    if youtube_results:
        return jsonify({
            'success': True,
            'token': token,
            'url': youtube_results[0]['previewUrl'],
            'quality': '128kbps',
            'title': youtube_results[0]['trackName'],
            'artist': youtube_results[0]['artistName'],
            'image': youtube_results[0]['artworkUrl500'],
            'source': 'youtube'
        })
    
    return jsonify({'success': False, 'url': None, 'token': token})

# ═══════════════════════════════════════════════════════════════
# /api/stream - PROXY
# ═══════════════════════════════════════════════════════════════
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
            upstream.close()
        
        return Response(stream_with_context(generate()), status=upstream.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# /api/download
# ═══════════════════════════════════════════════════════════════
@app.route('/api/download')
@limiter.limit("20 per minute")
def download_song():
    q = request.args.get('q', '').strip()
    artist = request.args.get('artist', '').strip()
    
    if not q:
        return jsonify({'error': 'Missing query'}), 400
    
    stream_url = None
    filename = f"{q} - {artist}".strip(' -') if artist else q
    
    songs = search_saavn(q)
    for song in songs:
        score = title_score(q, song.get('name', ''), song.get('primaryArtists', ''))
        if score > 0.3:
            raw_urls = song.get('downloadUrl') or song.get('download_url') or []
            if isinstance(raw_urls, str):
                raw_urls = [{'url': raw_urls, 'quality': 'unknown'}]
            # Try for 320kbps first
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
        youtube_results = search_youtube(f"{q} {artist}".strip(), 1)
        if youtube_results:
            video_id = youtube_results[0]['trackId']
            stream_url = get_youtube_audio_url(video_id)
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
            upstream.close()
        
        return Response(stream_with_context(generate()), status=200, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════
# AUTH - GOOGLE LOGIN
# ═══════════════════════════════════════════════════════════════
def verify_google_jwt(credential):
    if not GOOGLE_CLIENT_ID:
        # Development mode
        return {'sub': 'dev_user', 'name': 'Dev User', 'email': 'dev@example.com', 'picture': ''}
    try:
        payload = id_token.verify_oauth2_token(credential, _google_req, GOOGLE_CLIENT_ID)
        return payload
    except Exception:
        return None

def extract_bearer_sub(auth_header):
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    token = auth_header[7:].strip()
    if not token:
        return None
    payload = verify_google_jwt(token)
    return payload.get('sub') if payload else None

@app.route('/api/auth/google', methods=['POST'])
@limiter.limit("20 per minute")
def handle_google_auth():
    data = request.get_json() or {}
    credential = data.get('credential', '').strip()
    
    if not credential and not GOOGLE_CLIENT_ID:
        return jsonify({
            'success': True,
            'sub': 'dev_user',
            'name': 'Development User',
            'email': 'dev@example.com',
            'picture': ''
        })
    
    if not credential:
        return jsonify({'error': 'Missing credential'}), 400
    
    profile = verify_google_jwt(credential)
    if not profile:
        return jsonify({'error': 'Invalid credential'}), 401
    
    return jsonify({
        'success': True,
        'sub': profile.get('sub'),
        'name': profile.get('name', ''),
        'email': profile.get('email', ''),
        'picture': profile.get('picture', ''),
    })

# ═══════════════════════════════════════════════════════════════
# SYNC ENDPOINTS (Optional)
# ═══════════════════════════════════════════════════════════════
@app.route('/api/sync/state', methods=['POST'])
@limiter.limit("60 per minute")
def save_playback_state():
    sub = extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'status': 'ok'})

@app.route('/api/sync/state', methods=['GET'])
@limiter.limit("60 per minute")
def get_playback_state():
    sub = extract_bearer_sub(request.headers.get('Authorization', ''))
    if not sub:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'success': False})

# ═══════════════════════════════════════════════════════════════
# TV PAIRING
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
# GHOST PIN
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
# ADMIN
# ═══════════════════════════════════════════════════════════════
@app.route('/api/admin/users')
@limiter.limit("10 per minute")
def admin_users():
    secret = request.args.get('key', '')
    if ADMIN_KEY and not hmac.compare_digest(secret, ADMIN_KEY):
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'users': [], 'total': 0})

# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════
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

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7700))
    print(f"\n{'='*50}")
    print(f"🚀 Server running on http://localhost:{port}")
    print(f"📱 Open this URL in your browser")
    print(f"{'='*50}\n")
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)
