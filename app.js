const PerfMode = { ULTRA: 'ultra', BALANCED: 'balanced', LITE: 'lite' };
let currentPerfMode = PerfMode.BALANCED;
let perfSettings = {};

function detectPerformanceMode() {
  const cores = navigator.hardwareConcurrency || 4;
  const memory = navigator.deviceMemory || 4;
  const isBatterySaver = navigator.connection?.saveData === true;
  const isTV = window.__IS_TV__ || /SmartTV|BRAVIA|WebOS|Tizen|HbbTV/i.test(navigator.userAgent);
  const isLowEndDevice = isTV || cores <= 4 || memory <= 2;
  if (isLowEndDevice || isBatterySaver || isTV) return PerfMode.LITE;
  if (cores >= 8 && memory >= 6 && !isBatterySaver) return PerfMode.ULTRA;
  return PerfMode.BALANCED;
}

function applyPerformanceSettings() {
  const mode = currentPerfMode;
  perfSettings = {
    vizBarCount: mode === PerfMode.ULTRA ? 44 : mode === PerfMode.BALANCED ? 28 : 0,
    vizEnabled: mode !== PerfMode.LITE,
    ambientColorExtraction: mode !== PerfMode.LITE,
    animationDuration: mode === PerfMode.ULTRA ? 0.4 : 0.28,
    artworkSize: mode === PerfMode.ULTRA ? '600x600' : mode === PerfMode.BALANCED ? '400x400' : '200x200',
    lazyLoadMargin: mode === PerfMode.LITE ? '40px' : '120px',
    backdropFilterEnabled: mode !== PerfMode.LITE,
  };
  document.documentElement.style.setProperty('--anim-duration', perfSettings.animationDuration + 's');
  if (!perfSettings.backdropFilterEnabled) document.body.classList.add('no-backdrop-filter');
}

currentPerfMode = detectPerformanceMode();
applyPerformanceSettings();

// ─── 2. DEVICE DETECTION ────────────────────────────────────────────────────
const isTV = window.__IS_TV__ || (
  /SmartTV|SMART-TV|WebOS|Tizen|BRAVIA|HbbTV|TVBrowser|Viera|Vidaa|NetCast|PhilipsTV/i.test(navigator.userAgent) ||
  (window.innerWidth >= 1280 && !window.matchMedia('(pointer:fine)').matches)
);
const isMobile = /Android|iPhone|iPad/i.test(navigator.userAgent) && !isTV;
const isLowEnd = isTV ||
  (navigator.hardwareConcurrency || 8) <= 4 ||
  (typeof navigator.deviceMemory !== 'undefined' && navigator.deviceMemory <= 2);

// ─── 3. DYNAMIC VIEWPORT ────────────────────────────────────────────────────
function setVh() {
  document.documentElement.style.setProperty('--vh', (window.innerHeight * 0.01) + 'px');
}
window.addEventListener('resize', setVh, { passive: true });
window.addEventListener('orientationchange', () => setTimeout(setVh, 300), { passive: true });
setVh();

// ─── 4. FPS CAP ─────────────────────────────────────────────────────────────
const TARGET_FPS = currentPerfMode === PerfMode.ULTRA ? 60 : 30;
const FRAME_BUDGET = 1000 / TARGET_FPS;
let _lastRafTime = 0;

// ─── 5. IMAGE SYSTEM ─────────────────────────────────────────────────────────
const IMG_PLACEHOLDER = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='100'%3E%3Crect width='100' height='100' fill='%230d0d12'/%3E%3Ccircle cx='50' cy='42' r='14' fill='none' stroke='%232e2b26' stroke-width='2'/%3E%3Cpath d='M44 42v-8l16 4v8' fill='none' stroke='%232e2b26' stroke-width='2' stroke-linecap='round'/%3E%3Ccircle cx='44' cy='44' r='3' fill='%232e2b26'/%3E%3Ccircle cx='60' cy='46' r='3' fill='%232e2b26'/%3E%3C/svg%3E";

const _sharedCanvas = document.createElement('canvas');
_sharedCanvas.width = 16; _sharedCanvas.height = 16;
const _sharedCtx = _sharedCanvas.getContext('2d', { willReadFrequently: true }) || null;

const _imageCache = new Map();
const MAX_IMAGE_CACHE = 40;

const imgObserver = (!isTV && typeof IntersectionObserver !== 'undefined')
  ? new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          const img = entry.target;
          if (img.dataset.lazySrc) { img.src = img.dataset.lazySrc; delete img.dataset.lazySrc; }
          imgObserver.unobserve(img);
        }
      }
    }, { rootMargin: perfSettings.lazyLoadMargin, threshold: 0 })
  : null;

function setupImg(img) {
  img.classList.remove('loaded', 'img-error');
  img.onerror = function() {
    if (this.src !== IMG_PLACEHOLDER) this.src = IMG_PLACEHOLDER;
    this.classList.add('img-error', 'loaded');
    this.onerror = null;
  };
  img.onload = function() { this.classList.add('loaded'); };
  if (img.complete && img.naturalWidth > 0) img.classList.add('loaded');
}

function setImgSrc(img, src) {
  if (!src) { img.src = IMG_PLACEHOLDER; setupImg(img); return; }
  if (_imageCache.has(src)) { img.src = _imageCache.get(src); setupImg(img); return; }
  img.classList.remove('loaded', 'img-error');
  img.onerror = function() {
    if (this.src !== IMG_PLACEHOLDER) this.src = IMG_PLACEHOLDER;
    this.classList.add('img-error', 'loaded');
    this.onerror = null;
  };
  img.onload = function() {
    this.classList.add('loaded');
    if (_imageCache.size < MAX_IMAGE_CACHE) _imageCache.set(src, src);
  };
  if (img && img.id === 'mini-art' && src) {
    const mp = document.getElementById('mini-player');
    if (mp) {
      mp.style.setProperty('--mini-art-bg', `url('${src}')`);
      mp.style.setProperty('--mini-art-opacity', '1');
    }
  }
  if (imgObserver && !img.closest('#fullscreen-player') && !img.closest('#mini-player')) {
    img.dataset.lazySrc = src;
    img.src = IMG_PLACEHOLDER;
    imgObserver.observe(img);
  } else {
    img.src = src;
    if (img.complete && img.naturalWidth > 0) img.classList.add('loaded');
  }
}

function getArtUrl(song, size) {
  const target = isLowEnd ? '300x300' : (size || perfSettings.artworkSize || '400x400');
  if (!song) return '';
  const _c = [song.artworkUrl100, song.artworkUrl60, song.image, song.artwork, song.thumbnail, song.cover].filter(u => u && typeof u === 'string' && u.startsWith('http'));
  if (!_c.length) return '';
  const _r = _c[0].replace(/\d{2,4}x\d{2,4}/g, target);
  if (!_r.startsWith('/api/artwork') && (
    _r.includes('saavncdn.com') ||
    _r.includes('mzstatic.com') ||
    _r.includes('is1-ssl') || _r.includes('is2-ssl') || _r.includes('is3-ssl') ||
    _r.includes('is4-ssl') || _r.includes('is5-ssl')
  )) return '/api/artwork?url=' + encodeURIComponent(_r);
  return _r;
}

document.querySelectorAll('img').forEach(img => setupImg(img));
new MutationObserver(muts => {
  for (const m of muts) {
    for (const n of m.addedNodes) {
      const imgs = n.nodeName === 'IMG' ? [n] : (n.querySelectorAll ? [...n.querySelectorAll('img')] : []);
      for (const img of imgs) setupImg(img);
    }
  }
}).observe(document.body, { childList: true, subtree: true });

// ─── 6. LISTEN HISTORY ───────────────────────────────────────────────────────
let _listenHistory = {};
try { _listenHistory = JSON.parse(localStorage.getItem('aurum_listen_history') || '{}'); } catch(e) { _listenHistory = {}; }

function _trackListen(song) {
  if (!song?.artistName) return;
  const artists = song.artistName.split(/[&,]|feat\.|ft\./i).map(a => a.trim()).filter(Boolean);
  for (const artist of artists) {
    if (!_listenHistory[artist]) _listenHistory[artist] = { count: 0, lastSeen: 0, songs: [] };
    _listenHistory[artist].count++;
    _listenHistory[artist].lastSeen = Date.now();
    const existing = _listenHistory[artist].songs;
    if (!existing.find(s => String(s.trackId) === String(song.trackId))) {
      existing.unshift(song);
      if (existing.length > 20) existing.pop();
    }
  }
  const keys = Object.keys(_listenHistory);
  if (keys.length > 50) {
    const oldest = keys.sort((a, b) => _listenHistory[a].lastSeen - _listenHistory[b].lastSeen)[0];
    delete _listenHistory[oldest];
  }
  try { localStorage.setItem('aurum_listen_history', JSON.stringify(_listenHistory)); } catch(e) {}
}

function _getTopArtists(limit = 5) {
  return Object.entries(_listenHistory).sort((a, b) => b[1].count - a[1].count).slice(0, limit).map(([artist, data]) => ({ artist, ...data }));
}

// ─── 7. STATE ────────────────────────────────────────────────────────────────
let currentQueue         = [];
let currentIndex         = 0;
let currentTrack         = null;
let isPlaying            = false;
let shuffleOn            = false;
let repeatOn             = false;
let savedSongs           = (() => { try { return JSON.parse(localStorage.getItem('aurum_saved') || '[]'); } catch(e) { return []; } })();
let playlists            = (() => { try { return JSON.parse(localStorage.getItem('aurum_playlists') || '[]'); } catch(e) { return []; } })();
let recentlyPlayed       = (() => { try { return JSON.parse(localStorage.getItem('aurum_recent_played') || '[]'); } catch(e) { return []; } })();
let recentSearches       = (() => { try { return JSON.parse(localStorage.getItem('aurum_recent') || '[]'); } catch(e) { return []; } })();
let currentLibTab        = 'playlists';
let currentQuality       = 'loading';
let currentGenre         = 'all';
let currentPlaylistIndex = null;
let optsPlaylistIndex    = null;
let modalTrack           = null;
let _downloadSong        = null;
let _fullSongAbort       = null;
let _searchTimeout       = null;
let _recFetchAbort       = null;
let _recFetchTimeout     = null;
let sectionCache         = {};
let queuePanelOpen       = false;
let _lastObjectUrl       = null;
let _lastTuTime          = 0;
let _uiHidden            = false;
let _dismissedTrackId    = null;
let lyricsViewActive     = false;
let originalArtworkHTML  = null;

// [PATCH-1/6] Session-level recommendation state — feeds queue scoring engine
const _sessionPlayedIds  = new Set();   // all trackIds played this session
const _sessionArtistFreq = {};          // artist → play count this session
const QUEUE_TARGET       = 65;          // target queue depth (was 15, now 65)

// ─── 8. AUDIO ENGINE ─────────────────────────────────────────────────────────
const audio = new Audio();
audio.preload = 'none';
audio.crossOrigin = 'anonymous';
audio.setAttribute('playsinline', '');
audio.setAttribute('webkit-playsinline', '');
window._aurumAudio = audio;

let _currentSaavnUrl     = null;
let _currentSaavnQuality = null;

const _VERSION_KW = ['remix','lofi','lo-fi','lo fi','slowed','reverb','nightcore','cover','acoustic','live version','live at','mashup','instrumental','dj remix','dj mix','bass boost','8d audio','sped up','speed up','karaoke','unplugged','stripped','deep house','chillout','extended mix','club mix','dhol mix','tapori','jhankar','wedding mix','bhangra mix','dandiya','garba mix','party mix','dance mix'];
function _isVersionSong(t) { return !!t && _VERSION_KW.some(kw => t.toLowerCase().includes(kw)); }
function _userWantsVersion(t, a) { return _VERSION_KW.some(kw => ((t||'')+' '+(a||'')).toLowerCase().includes(kw)); }

function _titleMatches(saavnTitle, trackName) {
  if (!saavnTitle || !trackName) return false;
  const norm = s => s.toLowerCase().replace(/\(.*?\)/g,'').replace(/\[.*?\]/g,'').replace(/[^a-z0-9\s]/g,'').replace(/\s+/g,' ').trim();
  const st = norm(saavnTitle), it = norm(trackName);
  if (!st || !it) return false;
  if (_isVersionSong(saavnTitle) && !_isVersionSong(trackName)) return false;
  if (st === it) return true;
  if (st.includes(it) || it.includes(st)) return true;
  const sw = st.split(' ').filter(w => w.length > 0);
  const iw = it.split(' ').filter(w => w.length > 0);
  if (!sw.length || !iw.length) return false;
  if (sw.length >= 2 && iw.length >= 2) {
    const bigrams = arr => arr.slice(0,-1).map((w,i) => w+' '+arr[i+1]);
    const sb2 = bigrams(sw), ib2 = bigrams(iw);
    const bigMatches = sb2.filter(b => ib2.includes(b)).length;
    if (bigMatches > 0 && bigMatches / Math.max(sb2.length, ib2.length) >= 0.4) return true;
  }
  let matched = 0;
  for (const w of sw) {
    if (iw.some(iword => iword === w || (w.length > 3 && iword.length > 3 && (iword.startsWith(w) || w.startsWith(iword))))) matched++;
  }
  const total = Math.max(sw.length, iw.length);
  const threshold = total <= 2 ? 0.85 : total <= 3 ? 0.60 : 0.50;
  return matched / total >= threshold;
}

// ─── ITUNES ART FETCHER — [FIX] Cache + song object direct update ─────────────
// _itunesArtCache: same title+artist pe repeat fetch band karo
const _itunesArtCache = new Map();
const MAX_ITUNES_CACHE = 200;

async function _fetchItunesArt(title, artist, songObj) {
  const key = `${(title||'').toLowerCase()}|${(artist||'').toLowerCase()}`;
  if (_itunesArtCache.has(key)) {
    const cached = _itunesArtCache.get(key);
    // Song object bhi update karo agar cached URL hai
    if (cached && songObj) {
      songObj.artworkUrl100 = cached;
      songObj.image = cached;
    }
    return cached;
  }
  try {
    const q = encodeURIComponent(`${title} ${artist}`);
    const r = await fetch(`https://itunes.apple.com/search?term=${q}&entity=song&limit=1`);
    const d = await r.json();
    if (d.results?.[0]?.artworkUrl100) {
      const url = d.results[0].artworkUrl100.replace('100x100', '600x600');
      // Cache mein store karo
      if (_itunesArtCache.size >= MAX_ITUNES_CACHE) {
        const firstKey = _itunesArtCache.keys().next().value;
        _itunesArtCache.delete(firstKey);
      }
      _itunesArtCache.set(key, url);
      // Song object directly update karo — card/row references bhi theek honge
      if (songObj) {
        songObj.artworkUrl100 = url;
        songObj.image = url;
      }
      return url;
    }
  } catch(e) {}
  _itunesArtCache.set(key, null); // null cache karo — repeat fetch band
  return null;
}

// ─── 9. LOAD TRACK — SAAVN-FIRST ─────────────────────────────────────────────
// [MASTER-FIX] AbortController timing fix: ctrl synchronously banao PEHLE,
// phir async function ko pass karo — fast skip pe race condition zero.
let _loadGeneration = 0; // monotonic counter — har loadTrack pe increment

function loadTrack(song, autoplay = true) {
  if (!song) return;
  _dismissedTrackId = null;
  _prefetchFiredForTrack = null;

  // [FIX-ABORT-TIMING] Pehle abort, phir naya ctrl — synchronous, race-free
  if (_fullSongAbort) { _fullSongAbort.abort(); _fullSongAbort = null; }
  const ctrl = new AbortController();
  _fullSongAbort = ctrl;
  _loadGeneration++; // har song pe unique generation id
  const myGen = _loadGeneration;

  _currentSaavnUrl = null; _currentSaavnQuality = null;

  const pill = document.querySelector('.quality-pill');
  if (pill) pill.style.boxShadow = '';

  const sb = document.getElementById('fp-seekbar');
  if (sb) { sb.classList.remove('full-active'); sb.max = 30; sb.value = 0; sb.style.setProperty('--prog', '0%'); }

  currentTrack = song;

  // [PATCH-6] Session artist frequency tracking — feeds recommendation scorer
  const _ltArtist = (song.artistName || '').split(/[&,]|feat\.|ft\./i)[0].trim().toLowerCase();
  if (_ltArtist) _sessionArtistFreq[_ltArtist] = (_sessionArtistFreq[_ltArtist] || 0) + 1;
  _sessionPlayedIds.add(String(song.trackId));

  // iTunes art — sirf tab fetch karo jab Saavn image nahi hai
  const _alreadyHasArt = !!(song.artworkUrl100 || song.image);
  const _isSaavnSong = song._source === 'saavn' || !!song._saavnId;
  if (!(_isSaavnSong && _alreadyHasArt)) {
    _fetchItunesArt(song.trackName || '', song.artistName || '', song).then(url => {
      if (url && _loadGeneration === myGen && currentTrack?.trackId === song.trackId) {
        currentTrack.artworkUrl100 = url; currentTrack.image = url; updatePlayerUI();
      }
    });
  }

  currentQuality = 'loading';
  const durEl = document.getElementById('fp-duration');
  if (durEl) durEl.textContent = '0:30';

  audio.pause(); audio.src = ''; audio.load();

  updatePlayerUI(); showMiniPlayer(); updateActiveRows(); updateQualityLabel();
  addToRecentlyPlayed(song);

  clearTimeout(_recFetchTimeout);
  if (_recFetchAbort) { _recFetchAbort.abort(); _recFetchAbort = null; }
  _recFetchTimeout = setTimeout(() => fetchRecommendations(song), 800);
  fetchLyrics(song);

  if (song._source === 'saavn') {
    _autoFetchFullSong(song, autoplay, ctrl, myGen); return;
  }

  // iTunes: SEEDHA Saavn se full song fetch karo — Apple preview kabhi play mat karo
  // previewUrl = Apple 30s clip — ignore karo
  // /api/play se full song aayega (Saavn 320kbps)
  _autoFetchFullSong(song, autoplay, ctrl, myGen);
}

function playSongs(queue, index) {
  currentQueue = [...queue]; currentIndex = index;
  loadTrack(currentQueue[currentIndex]);
}

// ─── 10. AUTO FETCH FULL SONG ─────────────────────────────────────────────────
// [MASTER-FIX] ctrl + myGen dono check karo har await ke baad
// — double protection against song mismatch
async function _autoFetchFullSong(song, autoplay = true, ctrl, myGen) {
  // Agar ctrl nahi diya (legacy call), banao
  if (!ctrl) { ctrl = new AbortController(); _fullSongAbort = ctrl; }
  if (!myGen) myGen = _loadGeneration;
  const requested = song;

  // Guard: kya ye request abhi bhi valid hai?
  function _stillValid() {
    return !ctrl.signal.aborted &&
           _loadGeneration === myGen &&
           currentTrack?.trackId === requested.trackId;
  }

  try {
    // ── Direct Saavn ID path — fastest, zero search ──
    if (song._saavnId) {
      const proxyUrl = `/api/play?id=${encodeURIComponent(song._saavnId)}`
        + `&title=${encodeURIComponent(song.trackName || '')}`
        + `&artist=${encodeURIComponent(song.artistName || '')}`;
      if (!_stillValid()) return;
      await _upgradeAudio(proxyUrl, null, song, autoplay, ctrl, requested, myGen);
      return;
    }

    // ── iTunes song: search Saavn ──
    const cleanTitle  = (song.trackName  || '').replace(/\(.*?\)|\[.*?\]/g, '').trim();
    const cleanArtist = (song.artistName || '').split(/[&,]|feat\.|ft\./i)[0].trim();
    const movieMatch  = (song.trackName  || '').match(/\(From\s+[\u201c\u201d""]?(.+?)[\u201c\u201d""]?\)/i);
    const movieName   = movieMatch ? movieMatch[1].trim() : '';
    const primaryQ    = encodeURIComponent(movieName ? `${cleanTitle} ${movieName}` : `${cleanTitle} ${cleanArtist}`);
    const fallbackQ   = encodeURIComponent(`${cleanTitle} ${cleanArtist}`);
    const artistQ     = encodeURIComponent(cleanArtist);

    let d = null, proxyUrl = null;

    // [FIX] /api/play is a STREAMING endpoint — it returns audio bytes, NOT JSON.
    // Previous code called r.json() which always failed silently.
    // Correct approach: use /api/play URL directly as audio src, read metadata from headers.
    try {
      // Step 1: HEAD request to verify the song exists + grab metadata from headers
      const headUrl = `/api/play?title=${primaryQ}&artist=${artistQ}`;
    const headR = await fetch(headUrl, { method: 'HEAD', signal: ctrl.signal });
      if (!_stillValid()) return;

      if (headR.ok) {
        const resTitle  = headR.headers.get('X-Song-Title')  || '';
        const resArtist = headR.headers.get('X-Song-Artist') || '';
        const resSource = headR.headers.get('X-Audio-Source') || '';
        const resQuality = headR.headers.get('X-Audio-Quality') || 'unknown';
        const resArtUrl  = headR.headers.get('X-Artwork-URL') || '';

        const _wV = _userWantsVersion(requested.trackName, requested.artistName || '');

        // Version guard
        if (_isVersionSong(resTitle) && !_wV) {
          console.info('[AutoFetch] VERSION REJECTED: ' + resTitle);
        } else if (resSource === 'saavn' || resSource === 'jiosavan' || _titleMatches(resTitle, requested.trackName)) {
          // Artist guard
          const _reqArtistNorm = (requested.artistName || '').toLowerCase().split(/[&,]/)[0].trim();
          const _resArtistNorm = (resArtist || '').toLowerCase().split(/[&,]/)[0].trim();
          const _artistOk = !_reqArtistNorm || !_resArtistNorm ||
            _resArtistNorm.includes(_reqArtistNorm) || _reqArtistNorm.includes(_resArtistNorm) ||
            _reqArtistNorm.split(' ').some(w => w.length > 3 && _resArtistNorm.includes(w));

          if (!_artistOk) {
            console.info(`[AutoFetch] ARTIST MISMATCH: req="${_reqArtistNorm}" got="${_resArtistNorm}"`);
          } else {
            // iTunes artwork update — best quality mzstatic URL backend ne bheja
            if (resArtUrl && resArtUrl.startsWith('http')) {
              requested.artworkUrl100 = resArtUrl;
              requested.image = resArtUrl;
              if (song) { song.artworkUrl100 = resArtUrl; song.image = resArtUrl; }
            }
            // /api/play URL directly as stream — no /api/stream wrapping needed
            proxyUrl = headUrl;
            d = {
              quality: resQuality,
              title:   resTitle  || requested.trackName,
              artist:  resArtist || requested.artistName,
              source:  resSource,
            };
          }
        }
      } else if (headR.status === 404) {
        // Saavn pe nahi mila — fallback query try karo
        const fallbackUrl = `/api/play?title=${fallbackQ}&artist=${artistQ}`;
       const fallR = await fetch(fallbackUrl, { method: 'HEAD', signal: ctrl.signal });
        if (!_stillValid()) return;
        if (fallR.ok) {
          const resQuality = fallR.headers.get('X-Audio-Quality') || 'unknown';
          const resTitle   = fallR.headers.get('X-Song-Title')  || '';
          const resArtist  = fallR.headers.get('X-Song-Artist') || '';
          const resArtUrl  = fallR.headers.get('X-Artwork-URL') || '';
          if (resArtUrl && resArtUrl.startsWith('http')) {
            requested.artworkUrl100 = resArtUrl;
            requested.image = resArtUrl;
            if (song) { song.artworkUrl100 = resArtUrl; song.image = resArtUrl; }
          }
          proxyUrl = fallbackUrl;
          d = { quality: resQuality, title: resTitle, artist: resArtist, source: 'saavn' };
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.info('[AutoFetch] HEAD failed, trying direct stream:', e.message);
      // HEAD fail hua (CORS / server issue) — direct stream try karo as last resort
      proxyUrl = `/api/play?title=${primaryQ}&artist=${artistQ}`;
      d = { quality: 'unknown', title: requested.trackName, artist: requested.artistName, source: 'saavn' };
    }

    if (!_stillValid()) return;
    if (!d || !proxyUrl) {
      showToast('Song unavailable — try another');
      return;
    }

    await _upgradeAudio(proxyUrl, d, song, autoplay, ctrl, requested, myGen);

  } catch (e) {
    if (e.name !== 'AbortError') console.info('[AutoFetch] Error:', e.message);
  }
}

// ── Helper: preload + swap audio ─────────────────────────────────────────────
// [MASTER-FIX] preload = 'auto', adaptive timeout, generation guard
async function _upgradeAudio(proxyUrl, d, song, autoplay, ctrl, requested, myGen) {
  if (!myGen) myGen = _loadGeneration;

  function _stillValid() {
    return !ctrl?.signal?.aborted &&
           _loadGeneration === myGen &&
           currentTrack?.trackId === requested.trackId;
  }

  _currentSaavnUrl     = proxyUrl;
  _currentSaavnQuality = d?.quality || 'unknown';
  if (d?.quality) _updateDlSheetQuality(d.quality);

  const preAudio = new Audio();
  preAudio.preload     = 'auto'; // 'metadata' pe canplay reliable nahi — 'auto' rakho
  preAudio.crossOrigin = 'anonymous';
  const _cleanupPre = () => { try { preAudio.src = ''; preAudio.load(); } catch(e) {} };

  // [FIX-TIMEOUT] 2G pe 15s, baaki 10s — pehle se zyada patient
  const _connType = navigator.connection?.effectiveType || '4g';
  const _preloadTimeout = _connType === '2g' ? 15000 : _connType === '3g' ? 10000 : 8000;

  try {
    await new Promise((res, rej) => {
      const to = setTimeout(() => { _cleanupPre(); rej(new Error('preload-timeout')); }, _preloadTimeout);
      preAudio.addEventListener('canplay',  () => { clearTimeout(to); res(); }, { once: true });
      preAudio.addEventListener('canplaythrough', () => { clearTimeout(to); res(); }, { once: true });
      preAudio.addEventListener('error',    () => { clearTimeout(to); rej(new Error('preload-error')); }, { once: true });
      preAudio.src = proxyUrl;
      preAudio.load();
    });
  } catch (preErr) {
    _cleanupPre();
    if (!_stillValid()) return; // Song badal gaya — silently exit
    // Timeout pe bhi try karo — stream directly set karo bina preload ke
    if (preErr.message === 'preload-timeout') {
      console.info('[Audio] Preload timeout — setting src directly');
      // Fall through to direct play below
    } else {
      if (song._source === 'saavn') showToast('Stream error — retrying…');
      return;
    }
  }

  if (!_stillValid()) { _cleanupPre(); return; }

  const wasPlaying = song._source === 'saavn' ? autoplay : isPlaying;
  const prevPos = audio.currentTime;

  // [FIX-ART] Backend headers async read — playback block nahi karta
  (async () => {
    try {
      if (!_stillValid()) return;
      const headResp = await fetch(proxyUrl, { method: 'HEAD', signal: ctrl?.signal });
      if (!_stillValid() || !headResp.ok) return;
      const backendArtUrl  = headResp.headers.get('X-Artwork-URL');
      const backendQuality = headResp.headers.get('X-Audio-Quality');
      if (backendQuality && _stillValid()) {
        _currentSaavnQuality = backendQuality;
        _updateDlSheetQuality(backendQuality); updateQualityLabel();
      }
      if (backendArtUrl && backendArtUrl.startsWith('http') && _stillValid()) {
        song.artworkUrl100 = backendArtUrl; song.image = backendArtUrl;
        requested.artworkUrl100 = backendArtUrl; requested.image = backendArtUrl;
        if (currentTrack && String(currentTrack.trackId) === String(requested.trackId)) {
          currentTrack.artworkUrl100 = backendArtUrl; currentTrack.image = backendArtUrl;
        }
        for (const qs of currentQueue) {
          if (String(qs.trackId) === String(requested.trackId)) {
            qs.artworkUrl100 = backendArtUrl; qs.image = backendArtUrl;
          }
        }
        _itunesArtCache.set(`${(requested.trackName||'').toLowerCase()}|${(requested.artistName||'').toLowerCase()}`, backendArtUrl);
        if (_stillValid()) updatePlayerUI();
      }
    } catch(e) { /* silent */ }
  })();

  // [CORE] Audio src swap
  audio.addEventListener('loadedmetadata', () => {
    if (isFinite(prevPos) && prevPos > 1 && isFinite(audio.duration) && prevPos < audio.duration) {
      audio.currentTime = prevPos;
    }
  }, { once: true });

  audio.src = proxyUrl;
  const sbEl = document.getElementById('fp-seekbar');
  if (sbEl) sbEl.classList.add('full-active');
  _cleanupPre();

  if (d?.quality) _currentSaavnQuality = d.quality;
  if (_stillValid()) updatePlayerUI();

  if (wasPlaying) {
    const pp = audio.play();
    if (pp?.then) {
      pp.then(() => {
        if (!_stillValid()) { audio.pause(); return; }
        isPlaying = true; currentQuality = 'full'; _fullSongAbort = null;
        updateQualityLabel(); updatePlayerUI();
        if (_bgAudioCtx?.state === 'suspended') _bgAudioCtx.resume().catch(()=>{});
        _acquireWakeLock();
        updateMediaSession();
      }).catch(err => {
        if (err.name === 'AbortError') return;
        if (!_stillValid()) return;
        // Autoplay block hua — user interaction pe retry karo
        const _retryPlay = () => {
          if (!_stillValid()) return;
          audio.play().then(() => {
            isPlaying = true; currentQuality = 'full';
            updateQualityLabel(); updatePlayerUI(); updateMediaSession();
          }).catch(()=>{});
          document.removeEventListener('touchstart', _retryPlay);
          document.removeEventListener('click', _retryPlay);
        };
        document.addEventListener('touchstart', _retryPlay, { once: true });
        document.addEventListener('click', _retryPlay, { once: true });
        if (song._source !== 'saavn') _fallbackToPreview(requested);
      });
    }
  } else {
    currentQuality = 'full'; _fullSongAbort = null;
    updateQualityLabel(); updatePlayerUI();
  }
}

function _fallbackToPreview(song) {
  if (!song?.previewUrl) return;
  if (currentTrack?.trackId !== song.trackId) return;
  if (song._source === 'saavn') return; // No preview for Saavn songs
  const sb = document.getElementById('fp-seekbar');
  if (sb) { sb.classList.remove('full-active'); sb.max = 30; }
  audio.src = song.previewUrl;
  const p = audio.play();
  if (p?.then) p.then(() => { isPlaying = true; updatePlayerUI(); }).catch(() => {});
  currentQuality = 'preview'; updateQualityLabel();
}

function _updateDlSheetQuality(quality) {
  const desc  = document.getElementById('dl-full-desc');
  const badge = document.getElementById('dl-full-badge');
  if (!desc || !badge) return;
  const q = (quality || '').toLowerCase();
  if (q.includes('320'))      { desc.textContent = 'JioSaavn stream · 320 kbps'; badge.textContent = '320 kbps'; badge.className = 'dl-kbps-badge b320'; }
  else if (q.includes('160')) { desc.textContent = 'JioSaavn stream · 160 kbps'; badge.textContent = '160 kbps'; badge.className = 'dl-kbps-badge b160'; }
  else if (q.includes('96'))  { desc.textContent = 'JioSaavn stream · 96 kbps';  badge.textContent = '96 kbps';  badge.className = 'dl-kbps-badge b128'; }
  else                        { desc.textContent = 'JioSaavn stream · best available'; badge.textContent = 'HQ'; badge.className = 'dl-kbps-badge b320'; }
}

// ─── 11. PLAYBACK CONTROLS ───────────────────────────────────────────────────
function togglePlay() {
  if (!currentTrack) return;
  if (isPlaying) {
    audio.pause(); isPlaying = false;
  } else {
    const p = audio.play();
    if (p && p.then) {
      p.then(() => { isPlaying = true; updatePlayerUI(); }).catch(() => { isPlaying = false; updatePlayerUI(); });
      return;
    }
    isPlaying = true;
  }
  updatePlayerUI();
}

function _getShuffleIndex(currentIdx, length) {
  if (length <= 1) return 0;
  const indices = [];
  for (let i = 0; i < length; i++) { if (i !== currentIdx) indices.push(i); }
  return indices[Math.floor(Math.random() * indices.length)];
}

let _prevTrackLock = false;
function nextTrack() {
  if (!currentQueue.length) return;
  if (shuffleOn) {
    currentIndex = _getShuffleIndex(currentIndex, currentQueue.length);
  } else {
    currentIndex = (currentIndex + 1) % currentQueue.length;
  }
  loadTrack(currentQueue[currentIndex]);
  updateQueuePanel();

  // [PATCH-5] Proactive queue refill — if fewer than 12 songs remain ahead,
  // trigger recommendation fetch immediately so queue never runs dry in background
  const remaining = currentQueue.length - currentIndex - 1;
  if (remaining < 12 && currentTrack) {
    clearTimeout(_recFetchTimeout);
    if (_recFetchAbort) { _recFetchAbort.abort(); _recFetchAbort = null; }
    _recFetchTimeout = setTimeout(() => fetchRecommendations(currentQueue[currentIndex] || currentTrack), 500);
  }
}

function prevTrack() {
  if (audio.currentTime > 3) { audio.currentTime = 0; return; }
  if (!currentQueue.length) return;
  if (_prevTrackLock) return;
  _prevTrackLock = true;
  setTimeout(() => { _prevTrackLock = false; }, 400);
  currentIndex = (currentIndex - 1 + currentQueue.length) % currentQueue.length;
  loadTrack(currentQueue[currentIndex]);
  updateQueuePanel();
}

function seekTo(v) {
  if (isFinite(audio.duration) && audio.duration > 0) audio.currentTime = parseFloat(v);
}

function setVolume(v) {
  audio.volume = parseFloat(v);
  const vv = (parseFloat(v) * 100).toFixed(0) + '%';
  const slider = document.getElementById('fp-vol-slider');
  if (slider) slider.style.setProperty('--vol', vv);
  const vPath = document.getElementById('vol-path');
  if (vPath) {
    if (parseFloat(v) === 0)        vPath.setAttribute('d', 'M23 9l-4.5 4.5M18.5 9L23 13.5');
    else if (parseFloat(v) < 0.5)  vPath.setAttribute('d', 'M15.54 8.46a5 5 0 0 1 0 7.07');
    else                            vPath.setAttribute('d', 'M15.54 8.46a5 5 0 0 1 0 7.07M19.07 4.93a10 10 0 0 1 0 14.14');
  }
}

function toggleShuffle() {
  shuffleOn = !shuffleOn;
  const btn = document.getElementById('shuffle-btn');
  if (btn) btn.querySelector('svg').style.stroke = shuffleOn ? 'var(--gold-l)' : '';
  showToast(shuffleOn ? 'Shuffle on' : 'Shuffle off');
}

function toggleRepeat() {
  repeatOn = !repeatOn; audio.loop = repeatOn;
  const btn = document.getElementById('repeat-btn');
  if (btn) btn.querySelector('svg').style.stroke = repeatOn ? 'var(--gold-l)' : '';
  showToast(repeatOn ? 'Repeat on' : 'Repeat off');
}

// ─── 12. AUDIO EVENTS ────────────────────────────────────────────────────────
audio.addEventListener('ended', () => {
  if (repeatOn) {
    audio.currentTime = 0; audio.play().catch(()=>{});
    return;
  }
  if (currentQueue.length <= 1) {
    audio.currentTime = 0; audio.play().catch(()=>{});
    return;
  }
  _maybeTriggerFeedbackPrompt('ended');

  // [PATCH-2] Resume AudioContext BEFORE advancing track.
  // On Android/iOS, AudioContext suspends in background; calling audio.play()
  // on a suspended context is silently swallowed — queue appears to stall.
  const _advance = () => {
    nextTrack();
    // If queue is now draining fast, proactively refetch for the new current song
    if (currentQueue.length - currentIndex < 10 && currentTrack) {
      clearTimeout(_recFetchTimeout);
      _recFetchTimeout = setTimeout(() => fetchRecommendations(currentTrack), 300);
    }
  };

  if (_bgAudioCtx && _bgAudioCtx.state === 'suspended') {
    _bgAudioCtx.resume().then(_advance).catch(_advance);
  } else {
    _advance();
  }
});
audio.addEventListener('error', () => {
  if (currentQuality === 'full' && currentTrack?.previewUrl && currentTrack?._source !== 'saavn') {
    _fallbackToPreview(currentTrack);
  }
  const pc = document.getElementById('fp-play-circle');
  if (pc) pc.classList.remove('buffering');
});
audio.addEventListener('waiting',  () => { const pc = document.getElementById('fp-play-circle'); if (pc) pc.classList.add('buffering'); });
audio.addEventListener('stalled',  () => { const pc = document.getElementById('fp-play-circle'); if (pc) pc.classList.add('buffering'); });
audio.addEventListener('canplay',  () => { const pc = document.getElementById('fp-play-circle'); if (pc) pc.classList.remove('buffering'); });
audio.addEventListener('playing', () => {
  const pc = document.getElementById('fp-play-circle');
  if (pc) pc.classList.remove('buffering');
  isPlaying = true;
  _syncPlayIcons();
  _syncPlayingClass();
  updateMediaSession();
  if (!document.hidden && !isLowEnd && perfSettings.vizEnabled) _startViz();
});
audio.addEventListener('pause', () => {
  isPlaying = false;
  _syncPlayIcons();
  _syncPlayingClass();
  updateMediaSession();
  if (!document.hidden) _stopViz();
});

let _throttledTimeUpdate = 0;
let _prefetchFiredForTrack = null; // [FIX-PREFETCH] 70% pe ek baar prefetch karo

audio.addEventListener('timeupdate', () => {
  const now = Date.now();
  if (now - _throttledTimeUpdate < 250) return;
  _throttledTimeUpdate = now;
  if (_uiHidden) return;
  const seekbar = document.getElementById('fp-seekbar');
  if (seekbar) {
    const offset = audio.currentTime * 120;
    seekbar.style.setProperty('--wave-offset', offset + 'px');
    seekbar.style.setProperty('--squig-offset', offset + 'px');
  }
  const dur = isFinite(audio.duration) && audio.duration > 0 ? audio.duration : (currentQuality === 'full' ? 0 : 30);
  const p = dur ? audio.currentTime / dur * 100 : 0;
  const mpb = document.getElementById('mini-progress-bar');
  if (mpb) mpb.style.width = p + '%';
  const s = document.getElementById('fp-seekbar');
  if (s && !s.matches(':active')) {
    s.value = audio.currentTime;
    s.style.setProperty('--prog', p + '%');
  }
  const fc = document.getElementById('fp-current');
  if (fc) fc.textContent = formatSec(audio.currentTime);

  // [FIX-SMART-PREFETCH] 70% pe sirf next song prefetch karo — not on load
  if (p >= 70 && currentTrack && currentQueue.length > 1) {
    const nextIdx = shuffleOn ? _getShuffleIndex(currentIndex, currentQueue.length) : (currentIndex + 1) % currentQueue.length;
    const nextSong = currentQueue[nextIdx];
    if (nextSong && String(nextSong.trackId) !== String(_prefetchFiredForTrack)) {
      _prefetchFiredForTrack = nextSong.trackId;
      // Background mein quietly prefetch karo
      fetch(`/api/prefetch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ songs: [{ id: nextSong._saavnId || nextSong.trackId, title: nextSong.trackName, artist: nextSong.artistName }] }),
      }).catch(() => {});
    }
  }

  // [SW-PATCH] Har 30s pe MediaSession refresh + AudioContext health check + SW ping
  // PWABuilder mein OS audio focus silently release ho jaata hai —
  // yeh ensure karta hai ki lock screen notification aur audio pipeline dono live rahen
  if (now - _lastTuTime > 30000 && isPlaying) {
    _lastTuTime = now;
    updateMediaSession();
    if (_bgAudioCtx && _bgAudioCtx.state === 'suspended') {
      _bgAudioCtx.resume().catch(() => {});
    }
    // Service Worker ko signal: audio active hai, SW ko idle mat hone do
    if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: 'AUDIO_PLAYING' });
    }
  }
});

audio.addEventListener('durationchange', () => {
  if (isFinite(audio.duration) && audio.duration > 0) {
    const sb = document.getElementById('fp-seekbar');
    if (sb) sb.max = audio.duration;
    const fd = document.getElementById('fp-duration');
    if (fd) fd.textContent = formatSec(audio.duration);
  }
});

// ─── 13. OFFLINE DETECTION ───────────────────────────────────────────────────
function _handleConnectivity() {
  const banner = document.getElementById('offline-banner');
  if (!banner) return;
  navigator.onLine ? banner.classList.remove('show') : banner.classList.add('show');
}
window.addEventListener('online', _handleConnectivity, { passive: true });
window.addEventListener('offline', _handleConnectivity, { passive: true });
_handleConnectivity();

// ─── 14. VISUALIZER ──────────────────────────────────────────────────────────
let _vizBars = [];
let _vizTargets = [];
let _vizPhase = 0;
let _vizRafActive = false;
let _vizRafId = null;
let _lastVizFrame = 0;
function _getVizCount() { return perfSettings.vizBarCount || 0; }
let _vizRandOffsets = [];

function initViz() {
  if (isLowEnd || _getVizCount() === 0) return;
  const count = _getVizCount();
  const c = document.getElementById('fp-visualizer'); if (!c) return;
  c.innerHTML = ''; _vizBars = []; _vizTargets = [];
  _vizRandOffsets = Array.from({ length: count }, () => Math.random() * Math.PI * 2);
  for (let i = 0; i < count; i++) {
    const b = document.createElement('div'); b.className = 'fp-viz-bar';
    c.appendChild(b); _vizBars.push(b); _vizTargets.push(0.05);
  }
}

function _startViz() {
  if (!perfSettings.vizEnabled || _vizRafActive || document.hidden || _vizBars.length === 0) return;
  _vizRafActive = true;
  function loop(now) {
    if (!_vizRafActive || document.hidden) { _vizRafActive = false; _vizRafId = null; return; }
    const frameInterval = currentPerfMode === PerfMode.ULTRA ? 33 : 41;
    if (now - _lastVizFrame >= frameInterval) { _lastVizFrame = now; _updateVizBars(); }
    _vizRafId = requestAnimationFrame(loop);
  }
  _vizRafId = requestAnimationFrame(loop);
}

function _stopViz() {
  _vizRafActive = false;
  if (_vizRafId) { cancelAnimationFrame(_vizRafId); _vizRafId = null; }
}

function _updateVizBars() {
  if (!isPlaying || _vizBars.length === 0) {
    _vizTargets = _vizTargets.map(t => t * 0.88 + 0.05 * 0.12);
    for (let i = 0; i < _vizBars.length; i++) _vizBars[i].style.transform = `scaleY(${_vizTargets[i].toFixed(3)})`;
    return;
  }
  _vizPhase += 0.025;
  const count = _vizBars.length;
  for (let i = 0; i < count; i++) {
    const norm = i / count;
    let freqCurve;
    if (norm < 0.12) freqCurve = norm / 0.12;
    else if (norm < 0.44) freqCurve = 1 - (norm - 0.12) * 0.55;
    else freqCurve = Math.max(0.08, 0.72 - (norm - 0.44) * 1.2);
    const rOff = _vizRandOffsets[i] || 0;
    const o1 = Math.sin(_vizPhase * 2.2 + i * 0.42 + rOff) * 0.4 + 0.4;
    const o2 = Math.sin(_vizPhase * 1.4 + i * 0.72 + rOff * 0.6 + 0.8) * 0.24 + 0.24;
    const o3 = Math.sin(_vizPhase * 3.4 + i * 0.28 + rOff * 0.35 + 1.8) * 0.12 + 0.12;
    let target = (o1 + o2 * 0.6 + o3 * 0.4) * freqCurve * 0.92;
    target = Math.max(0.04, Math.min(1, target));
    _vizTargets[i] = _vizTargets[i] * 0.72 + target * 0.28;
    _vizBars[i].style.transform = `scaleY(${_vizTargets[i].toFixed(3)})`;
  }
}

function tickViz() { _startViz(); }

// ─── 15. SCREEN OFF / ON ─────────────────────────────────────────────────────
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    // ── Screen locked / tab hidden ─────────────────────────────────────────
    _uiHidden = true; _stopViz();
    document.body.classList.add('screen-off');
    const ac = document.getElementById('ambient-canvas');
    if (ac) ac.classList.remove('orbs-active');
    const fp = document.getElementById('fullscreen-player');
    if (fp) fp.style.setProperty('visibility', 'hidden', 'important');
    if (ac) ac.style.setProperty('display', 'none', 'important');
    const keep = new Set(['recent', 'featured']);
    for (const k of Object.keys(sectionCache)) { if (!keep.has(k)) delete sectionCache[k]; }

    // [PATCH-4] Keep AudioContext alive while hidden — do NOT pause audio.
    // If context is suspended while isPlaying, pre-resume it so that when
    // audio.ended fires in background the next play() call will succeed.
    if (_bgAudioCtx && _bgAudioCtx.state === 'suspended' && isPlaying) {
      _bgAudioCtx.resume().catch(() => {});
    }

  } else {
    // ── App came back to foreground ────────────────────────────────────────
    _uiHidden = false;
    document.body.classList.remove('screen-off');
    const fp = document.getElementById('fullscreen-player');
    const ac = document.getElementById('ambient-canvas');
    if (fp) fp.style.removeProperty('visibility');
    if (ac) ac.style.removeProperty('display');
    if (fp?.classList.contains('open') && !isLowEnd) _startViz();
    if (!isLowEnd && ac) ac.classList.add('orbs-active');
    if (currentTrack) {
      _syncPlayIcons(); _syncPlayingClass(); updateQualityLabel();

      // [PATCH-4] Correct resume ordering: AudioContext first → then audio.play().
      // play() on a suspended context is a no-op; context must be live first.
      if (_bgAudioCtx && _bgAudioCtx.state === 'suspended') {
        _bgAudioCtx.resume().then(() => {
          if (isPlaying && audio.paused && audio.src) {
            audio.play().catch(() => {});
          }
        }).catch(() => {
          if (isPlaying && audio.paused && audio.src) {
            audio.play().catch(() => {});
          }
        });
      } else if (isPlaying && audio.paused) {
        audio.play().catch(() => {});
      }
    }
  }
}, { passive: true });

// ─── 16. UI UPDATES ──────────────────────────────────────────────────────────
function _syncPlayIcons() {
  const playIcon  = '<polygon points="5 3 19 12 5 21 5 3"/>';
  const pauseIcon = '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>';
  const icon = isPlaying ? pauseIcon : playIcon;
  const mi = document.getElementById('mini-play-icon');
  const fi = document.getElementById('fp-play-icon');
  if (mi) mi.innerHTML = icon;
  if (fi) fi.innerHTML = icon;
}

function _syncPlayingClass() {
  const fp = document.getElementById('fullscreen-player');
  const mp = document.getElementById('mini-player');
  if (fp) isPlaying ? fp.classList.add('playing') : fp.classList.remove('playing');
  if (mp) isPlaying ? mp.classList.add('playing-glow') : mp.classList.remove('playing-glow');
}

function updatePlayerUI() {
  if (!currentTrack) return;
  const artUrl = getArtUrl(currentTrack, perfSettings.artworkSize);
  const miniArt = document.getElementById('mini-art');
  if (miniArt) setImgSrc(miniArt, artUrl);
  const fpArt = document.getElementById('fp-art');
  if (fpArt) setImgSrc(fpArt, artUrl);
  const mt = document.getElementById('mini-title'); if (mt) mt.textContent = currentTrack.trackName || 'Unknown';
  const ma = document.getElementById('mini-artist'); if (ma) ma.textContent = currentTrack.artistName || 'Unknown';
  const ft = document.getElementById('fp-title'); if (ft) ft.textContent = currentTrack.trackName || 'Unknown';
  const fa = document.getElementById('fp-artist'); if (fa) fa.textContent = currentTrack.artistName || 'Unknown';
  _syncPlayIcons(); _syncPlayingClass(); updateSaveBtn(); updateActiveRows();
  updateAmbientPlayer(artUrl); updateNextStrip(); updateMediaSession(); showMiniPlayer();
}

function toggleLyricsView() {
  const wrap = document.getElementById('fp-lyrics-wrap');
  const lyricsBtn = document.getElementById('fp-lyrics-toggle');
  if (!wrap) return;
  const isOpen = wrap.dataset.lyricsOpen === '1';
  if (isOpen) {
    wrap.style.display = 'none'; wrap.dataset.lyricsOpen = '0';
    lyricsViewActive = false;
    if (lyricsBtn) lyricsBtn.classList.remove('active');
  } else {
    const el = document.getElementById('fp-lyrics');
    if (!el || !el.textContent.trim()) { showToast('No lyrics available'); return; }
    wrap.style.display = 'block'; wrap.dataset.lyricsOpen = '1';
    el.scrollTop = 0; lyricsViewActive = true;
    if (lyricsBtn) lyricsBtn.classList.add('active');
  }
}

let _shuffleNextIndex = -1;
function _getNextIndexForStrip() {
  if (!currentQueue.length || currentQueue.length < 2) return -1;
  if (shuffleOn) {
    if (_shuffleNextIndex === -1 || _shuffleNextIndex === currentIndex) {
      _shuffleNextIndex = _getShuffleIndex(currentIndex, currentQueue.length);
    }
    return _shuffleNextIndex;
  }
  return (currentIndex + 1) % currentQueue.length;
}

function updateNextStrip() {
  const strip = document.getElementById('fp-next-strip');
  if (!strip) return;
  const remainingCount = currentQueue.length - currentIndex - 1;
  if (!currentQueue.length || currentQueue.length < 2) { strip.style.display = 'none'; return; }
  const nextIdx = _getNextIndexForStrip();
  if (nextIdx === -1) { strip.style.display = 'none'; return; }
  const nextSong = currentQueue[nextIdx];
  if (!nextSong) { strip.style.display = 'none'; return; }
  strip.style.display = 'flex';
  const tag = strip.querySelector('.fp-next-tag');
  if (tag && remainingCount > 0) {
    tag.innerHTML = `UP<br>NEXT<br><span style="font-size:7px; margin-top:2px;">${remainingCount}</span>`;
  } else if (tag) { tag.innerHTML = 'UP<br>NEXT'; }
  const wasHidden = !strip.style.opacity || strip.style.opacity === '0' || getComputedStyle(strip).opacity === '0';
  if (wasHidden) {
    strip.style.opacity = '0'; strip.style.transform = 'translateY(8px)';
    strip.style.transition = 'opacity 0.35s cubic-bezier(0.22,1,0.36,1), transform 0.35s cubic-bezier(0.22,1,0.36,1)';
    requestAnimationFrame(() => requestAnimationFrame(() => {
      strip.style.opacity = ''; strip.style.transform = '';
      setTimeout(() => { strip.style.transition = ''; }, 380);
    }));
  }
  const nextArtEl    = document.getElementById('fp-next-art');
  const nextTitleEl  = document.getElementById('fp-next-title');
  const nextArtistEl = document.getElementById('fp-next-artist');
  if (nextArtEl) {
    const newSrc = getArtUrl(nextSong, '300x300');
    if (nextArtEl.dataset.currentSrc !== newSrc) {
      nextArtEl.dataset.currentSrc = newSrc;
      nextArtEl.style.transition = 'opacity 0.25s ease'; nextArtEl.style.opacity = '0';
      setTimeout(() => { setImgSrc(nextArtEl, newSrc); nextArtEl.style.opacity = ''; }, 180);
    }
  }
  if (nextTitleEl)  nextTitleEl.textContent  = nextSong.trackName  || 'Unknown';
  if (nextArtistEl) nextArtistEl.textContent = nextSong.artistName || 'Unknown';
}

function _resetShuffleNext() { _shuffleNextIndex = -1; }

function updateMediaSession() {
  if (!('mediaSession' in navigator) || !currentTrack) return;
  try {
    const artUrl = getArtUrl(currentTrack, '512x512');
    navigator.mediaSession.metadata = new MediaMetadata({
      title: currentTrack.trackName || 'Unknown',
      artist: currentTrack.artistName || 'Unknown',
      artwork: [{ src: artUrl, sizes: '512x512', type: 'image/jpeg' }]
    });
    // [SW-PATCH] Play handler: AudioContext resume FIRST, phir audio.play()
    // PWABuilder lock screen pe play dabane se AudioContext suspended milta hai —
    // seedha audio.play() karne se silently fail hota hai
    navigator.mediaSession.setActionHandler('play', () => {
      if (_bgAudioCtx && _bgAudioCtx.state === 'suspended') {
        _bgAudioCtx.resume().then(() => {
          audio.play().catch(() => {});
        }).catch(() => { audio.play().catch(() => {}); });
      } else {
        audio.play().catch(() => {});
      }
      isPlaying = true; updatePlayerUI();
    });
    navigator.mediaSession.setActionHandler('pause', () => { audio.pause(); isPlaying = false; updatePlayerUI(); });
    navigator.mediaSession.setActionHandler('nexttrack', nextTrack);
    navigator.mediaSession.setActionHandler('previoustrack', prevTrack);
    // [SW-PATCH] Stop action: OS kabhi kabhi 'stop' bhejta hai (call/notification) —
    // handle karo warna OS audio focus le leta hai aur stream kill ho jaata hai
    try {
      navigator.mediaSession.setActionHandler('stop', () => {
        audio.pause(); isPlaying = false; updatePlayerUI();
      });
    } catch(e) {}
    // [FIX-MEDIASESSION-SEEK] Lock screen seek support
    try {
      navigator.mediaSession.setActionHandler('seekto', d => { if (isFinite(d.seekTime)) seekTo(d.seekTime); });
    } catch(e) {}
    try {
      navigator.mediaSession.setActionHandler('seekbackward', d => {
        const skip = d.seekOffset || 10;
        seekTo(Math.max(0, audio.currentTime - skip));
      });
      navigator.mediaSession.setActionHandler('seekforward', d => {
        const skip = d.seekOffset || 10;
        seekTo(Math.min(audio.duration || 999, audio.currentTime + skip));
      });
    } catch(e) {}
    navigator.mediaSession.playbackState = isPlaying ? 'playing' : 'paused';
    // [FIX-MEDIASESSION-POS] Position state update karo — lock screen seekbar
    try {
      if (isFinite(audio.duration) && audio.duration > 0) {
        navigator.mediaSession.setPositionState({
          duration: audio.duration,
          playbackRate: audio.playbackRate || 1,
          position: Math.min(audio.currentTime, audio.duration),
        });
      }
    } catch(e) {}
  } catch(e) {}
}

function updateSaveBtn() {
  if (!currentTrack) return;
  const saved = isSaved(currentTrack);
  const btn = document.getElementById('fp-save-btn');
  const lbl = document.getElementById('fp-save-label');
  if (btn) btn.classList.toggle('saved', saved);
  if (lbl) lbl.textContent = saved ? 'Saved' : 'Save';
}

function showMiniPlayer() {
  if (isTV) return;
  if (_dismissedTrackId && currentTrack && String(_dismissedTrackId) === String(currentTrack.trackId)) return;
  const mp = document.getElementById('mini-player');
  if (!mp) return;
  // [FIX-MINI-1] Force reset inline styles that may block visibility
  mp.style.transform = '';
  mp.style.transition = '';
  mp.style.opacity = '';
  mp.style.pointerEvents = '';
  mp.classList.add('show');
  // [FIX-MINI-2] Agar fullscreen open hai toh mini player hide rakho
  const fp = document.getElementById('fullscreen-player');
  if (fp && fp.classList.contains('open')) {
    mp.style.opacity = '0';
    mp.style.pointerEvents = 'none';
  }
}

function updateActiveRows() {
  document.querySelectorAll('.song-row,.queue-item').forEach(r => {
    const isCurrent = currentTrack && (String(r.dataset.trackId) === String(currentTrack.trackId));
    r.classList.toggle('playing', isCurrent); r.classList.toggle('current', isCurrent);
    const rightDiv = r.querySelector('.song-row-right');
    if (!rightDiv) return;
    const existing = rightDiv.querySelector('.now-playing-bar');
    const durSpan  = rightDiv.querySelector('.song-row-duration');
    if (isCurrent && isPlaying) {
      if (!existing) {
        const bar = document.createElement('div'); bar.className = 'now-playing-bar';
        bar.innerHTML = '<span></span><span></span><span></span>';
        if (durSpan) durSpan.style.display = 'none';
        rightDiv.appendChild(bar);
      }
    } else {
      if (existing) existing.remove();
      if (durSpan) durSpan.style.display = '';
    }
  });
}

function updateQualityLabel() {
  const lbl = document.getElementById('quality-label');
  const pill = document.querySelector('.quality-pill');
  if (!lbl) return;
  if (currentQuality === 'full') {
    const q = (_currentSaavnQuality || '').toLowerCase();
    const kbps = q.includes('320') ? '320 kbps' : q.includes('160') ? '160 kbps' : q.includes('96') ? '96 kbps' : 'HQ';
    lbl.textContent = kbps; lbl.style.color = 'var(--gold-l)';
    if (pill) pill.style.boxShadow = '0 0 0 0.5px rgba(184,150,64,0.24)';
  } else if (currentQuality === 'loading') {
    lbl.textContent = '·'; lbl.style.color = 'var(--text3)';
    if (pill) pill.style.boxShadow = '';
  } else {
    lbl.textContent = '128 kbps'; lbl.style.color = 'var(--text3)';
    if (pill) pill.style.boxShadow = '';
  }
  const fb = document.getElementById('q-badge-full');
  const pb = document.getElementById('q-badge-preview');
  if (fb) {
    const q = (_currentSaavnQuality || '').toLowerCase();
    const kLabel = q.includes('320') ? '320 kbps' : q.includes('160') ? '160 kbps' : 'HQ Stream';
    fb.textContent = currentQuality === 'full' ? `● ${kLabel}` : '▶ Stream';
    fb.className = 'quality-badge' + (currentQuality === 'full' ? ' active' : ' ext');
  }
  if (pb) {
    pb.textContent = currentQuality === 'preview' ? '● 128 kbps' : '○ Preview';
    pb.className = 'quality-badge' + (currentQuality === 'preview' ? ' active' : ' ext');
  }
}

// ─── 17. AMBIENT PLAYER BG ───────────────────────────────────────────────────
let _lastAmbientSrc = '';
let _cachedDominantColor = null;

function updateAmbientPlayer(artUrl) {
  if (isLowEnd) return;
  if (!artUrl || artUrl === _lastAmbientSrc) return;
  _lastAmbientSrc = artUrl;
  const bgArt = document.getElementById('fp-bg-art');
  if (!bgArt) return;
  bgArt.onerror = () => { bgArt.style.opacity = '0'; };
  bgArt.onload  = () => {
    bgArt.style.opacity = '1';
    try {
      extractDominantColor(bgArt, (r, g, b) => {
        _cachedDominantColor = [r, g, b];
        const glow = document.getElementById('fp-ambient-glow');
        if (glow) glow.style.background = `radial-gradient(ellipse at 50% 80%,rgba(${r},${g},${b},0.2),transparent 72%)`;
        document.querySelectorAll('.fp-viz-bar').forEach(bar => {
          bar.style.background = `linear-gradient(to top,rgba(${r},${g},${b},0.75) 0%,rgba(${r},${g},${b},0.1) 100%)`;
        });
        const fp = document.getElementById('fullscreen-player');
        if (fp) fp.style.setProperty('--fp-art-glow', `rgba(${r},${g},${b},0.24)`);
      });
    } catch(e) {}
  };
  bgArt.style.opacity = '0';
  bgArt.src = artUrl;
  if (bgArt.complete && bgArt.naturalWidth > 0) bgArt.onload && bgArt.onload();
}

function extractDominantColor(imgEl, callback) {
  if (!_sharedCtx) { callback(184, 150, 64); return; }
  try {
    _sharedCtx.drawImage(imgEl, 0, 0, 16, 16);
    let data;
    try { data = _sharedCtx.getImageData(0, 0, 16, 16).data; }
    catch(corsErr) { callback(184, 150, 64); return; }
    let r = 0, g = 0, b = 0, count = 0;
    for (let i = 0; i < data.length; i += 16) { r += data[i]; g += data[i+1]; b += data[i+2]; count++; }
    if (!count) { callback(184, 150, 64); return; }
    r = Math.round(r / count); g = Math.round(g / count); b = Math.round(b / count);
    const max = Math.max(r, g, b, 1);
    r = Math.round(r / max * 210); g = Math.round(g / max * 210); b = Math.round(b / max * 210);
    callback(r, g, b);
  } catch(e) { callback(184, 150, 64); }
}

// ─── 18. SMART BLUR HELPERS ──────────────────────────────────────────────────
function _pauseBlur() {
  if (!perfSettings.backdropFilterEnabled) return;
  ['fullscreen-player', 'mini-player', 'queue-panel'].forEach(id => {
    const el = document.getElementById(id); if (!el) return;
    el.style.backdropFilter = 'none'; el.style.webkitBackdropFilter = 'none';
  });
}

function _resumeBlur() {
  if (!perfSettings.backdropFilterEnabled) return;
  ['fullscreen-player', 'mini-player', 'queue-panel'].forEach(id => {
    const el = document.getElementById(id); if (!el) return;
    el.style.backdropFilter = ''; el.style.webkitBackdropFilter = '';
  });
}

// ─── 19. GESTURE SYSTEM ──────────────────────────────────────────────────────
function setupMiniGesture() {
  const mp = document.getElementById('mini-player');
  if (!mp) return;
  let startY = 0, startX = 0, isDragging = false, startTime = 0;
  let moved = false, rafId = null, axisLocked = null;

  function snapBack() {
    mp.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
    mp.style.transform = ''; mp.style.willChange = '';
    setTimeout(() => { mp.style.transition = ''; }, 340); _resumeBlur();
  }

  mp.addEventListener('touchstart', e => {
    startY = e.touches[0].clientY; startX = e.touches[0].clientX;
    isDragging = true; startTime = Date.now(); moved = false; axisLocked = null;
    mp.style.transition = 'none'; mp.style.willChange = 'transform';
    mp.style.transform = mp.style.transform || 'translateZ(0)'; _pauseBlur();
  }, { passive: true });

  mp.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dy = e.touches[0].clientY - startY, dx = e.touches[0].clientX - startX;
    if (!axisLocked && (Math.abs(dy) > 8 || Math.abs(dx) > 8)) {
      axisLocked = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
    }
    if (axisLocked === 'horizontal') { isDragging = false; mp.style.willChange = ''; snapBack(); return; }
    if (axisLocked === 'vertical' && Math.abs(dy) > 4) {
      moved = true; e.preventDefault();
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => { rafId = null; mp.style.transform = `translateY(${dy}px)`; });
    }
  }, { passive: false });

  mp.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return;
    isDragging = false; axisLocked = null;
    const dy = e.changedTouches[0].clientY - startY;
    const vel = dy / Math.max(1, Date.now() - startTime);
    mp.style.willChange = ''; _resumeBlur();
    if (!moved) { snapBack(); return; }
    if (dy < -30 || vel < -0.45) { mp.style.transform = ''; mp.style.transition = ''; openFullscreen(); return; }
    if (dy > 100 || (vel > 0.55 && dy > 30)) {
      if (currentTrack) _dismissedTrackId = currentTrack.trackId;
      mp.style.transition = 'transform 0.25s ease, opacity 0.2s ease';
      mp.style.transform = 'translateY(120px)'; mp.style.opacity = '0';
      setTimeout(() => { mp.classList.remove('show'); mp.style.transform = ''; mp.style.opacity = ''; mp.style.transition = ''; }, 250);
      if (isPlaying) { audio.pause(); isPlaying = false; _syncPlayIcons(); _syncPlayingClass(); }
      return;
    }
    snapBack();
  }, { passive: true });

  mp.addEventListener('touchcancel', () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging = false; axisLocked = null; mp.style.willChange = ''; snapBack();
  }, { passive: true });
}

function setupFullPlayerGesture() {
  const fp = document.getElementById('fullscreen-player');
  const qp = document.getElementById('queue-panel');
  if (!fp || !qp) return;
  let startY = 0, startX = 0, isDragging = false, startTime = 0;
  let gestureTarget = null, moved = false, rafId = null, axisLocked = null, queueOpenTriggered = false;

  function snapBackFp() {
    fp.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
    fp.style.transform = ''; fp.style.willChange = '';
    setTimeout(() => { fp.style.transition = ''; }, 340); _resumeBlur();
  }

  function snapBackQp() {
    qp.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
    qp.style.transform = qp.classList.contains('open') ? 'translateY(0)' : 'translateY(100%)';
    qp.style.willChange = '';
    setTimeout(() => { qp.style.transform = ''; qp.style.transition = ''; }, 340); _resumeBlur();
  }

  function cleanupGesture() {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging = false; axisLocked = null; moved = false; queueOpenTriggered = false;
    fp.classList.remove('dragging'); qp.classList.remove('dragging');
    fp.style.willChange = ''; qp.style.willChange = '';
  }

  function isGestureZone(el) {
    return el.closest('#fp-drag-hint') || el.closest('.fp-header') || el.closest('.fp-info') ||
           el.closest('.fp-art-wrap') || el.closest('.fp-progress-wrap') || el.closest('.fp-controls') ||
           el.closest('.fp-bottom') || el.closest('.fp-next-strip') || el.closest('.queue-panel-handle') ||
           el.closest('#queue-drag-handle');
  }

  fp.addEventListener('touchstart', e => {
    const qpOpen = qp.classList.contains('open');
    const onQueueHandle = e.target.closest('#queue-drag-handle');
    const onQueueBody   = qp.contains(e.target) && !onQueueHandle;
    if (qpOpen && onQueueBody) { isDragging = false; return; }
    if (!qpOpen && !isGestureZone(e.target)) { isDragging = false; return; }
    startY = e.touches[0].clientY; startX = e.touches[0].clientX;
    isDragging = true; startTime = Date.now(); moved = false; axisLocked = null; queueOpenTriggered = false;
    gestureTarget = qpOpen ? 'queue' : 'player';
    const target = gestureTarget === 'player' ? fp : qp;
    target.style.willChange = 'transform'; target.style.transform = target.style.transform || 'translateZ(0)';
    fp.classList.add('dragging'); qp.classList.add('dragging'); _pauseBlur();
  }, { passive: true });

  fp.addEventListener('touchmove', e => {
    if (!isDragging || queueOpenTriggered) return;
    const dy = e.touches[0].clientY - startY, dx = e.touches[0].clientX - startX;
    if (!axisLocked && (Math.abs(dy) > 6 || Math.abs(dx) > 6)) {
      axisLocked = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
    }
    if (axisLocked === 'horizontal') { isDragging = false; return; }
    if (axisLocked === 'vertical' && Math.abs(dy) > 4) {
      moved = true;
      if (gestureTarget === 'player' && dy > 0) {
        e.preventDefault();
        if (rafId) cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(() => { fp.style.transform = `translateY(${dy}px)`; rafId = null; });
      } else if (gestureTarget === 'queue' && dy > 0) {
        e.preventDefault();
        if (rafId) cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(() => { qp.style.transform = `translateY(${dy}px)`; rafId = null; });
      } else if (gestureTarget === 'player' && dy < -60) {
        e.preventDefault(); queueOpenTriggered = true; cleanupGesture();
        fp.style.transform = ''; qp.style.transform = ''; _resumeBlur(); openQueuePanel();
      }
    }
  }, { passive: false });

  fp.addEventListener('touchend', e => {
    if (!isDragging) return;
    const dy = e.changedTouches[0].clientY - startY;
    const vel = dy / Math.max(1, Date.now() - startTime);
    const wasTarget = gestureTarget, wasMoved = moved;
    cleanupGesture(); _resumeBlur();
    if (!wasMoved) { wasTarget === 'player' ? snapBackFp() : snapBackQp(); return; }
    if (wasTarget === 'player') {
      if (dy > 100 || (vel > 0.5 && dy > 40)) { fp.style.transform = ''; closeFullscreen(); }
      else snapBackFp();
    } else if (wasTarget === 'queue') {
      if (dy > 90 || (vel > 0.5 && dy > 25)) { qp.style.transform = ''; closeQueuePanel(); }
      else snapBackQp();
    }
  }, { passive: true });

  fp.addEventListener('touchcancel', () => {
    const wasTarget = gestureTarget; cleanupGesture(); _resumeBlur();
    if (wasTarget === 'player') snapBackFp();
    else if (wasTarget === 'queue') snapBackQp();
  }, { passive: true });
}

function _attachArtSwipe() {
  const artWrap = document.getElementById('fp-art-wrap');
  if (!artWrap || artWrap._swipeAttached) return;
  artWrap._swipeAttached = true;
  let startX = 0, startY = 0, isDragging = false, moved = false, startTime = 0, rafId = null, axisLocked = null;

  function resetArt() {
    artWrap.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1), opacity 0.22s ease';
    artWrap.style.transform = ''; artWrap.style.opacity = ''; artWrap.style.willChange = '';
    setTimeout(() => { artWrap.style.transition = ''; }, 350);
  }

  artWrap.addEventListener('touchstart', e => {
    startX = e.touches[0].clientX; startY = e.touches[0].clientY;
    isDragging = true; moved = false; axisLocked = null; startTime = Date.now();
    artWrap.style.transition = 'none'; artWrap.style.willChange = 'transform,opacity';
    artWrap.style.transform = artWrap.style.transform || 'translateZ(0)';
  }, { passive: true });

  artWrap.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dx = e.touches[0].clientX - startX, dy = e.touches[0].clientY - startY;
    if (!axisLocked && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
      axisLocked = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
    }
    if (axisLocked === 'vertical') { isDragging = false; artWrap.style.willChange = ''; resetArt(); return; }
    if (axisLocked === 'horizontal' && Math.abs(dx) > 8) {
      moved = true; e.preventDefault();
      const clamped = dx * 0.72, tilt = clamped * 0.018;
      const fade = Math.max(0.28, 1 - Math.abs(dx) / 280);
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        artWrap.style.transform = `translateX(${clamped}px) rotate(${tilt}deg)`;
        artWrap.style.opacity = String(fade); rafId = null;
      });
    }
  }, { passive: false });

  artWrap.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return;
    isDragging = false; artWrap.style.willChange = '';
    const dx = e.changedTouches[0].clientX - startX;
    const vel = dx / Math.max(1, Date.now() - startTime);
    if (!moved) { resetArt(); return; }
    if (dx < -55 || vel < -0.38)    { _animateArtSwipe('left',  nextTrack); }
    else if (dx > 55 || vel > 0.38) { _animateArtSwipe('right', prevTrack); }
    else                             { resetArt(); }
  }, { passive: true });

  artWrap.addEventListener('touchcancel', () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging = false; axisLocked = null; artWrap.style.willChange = ''; resetArt();
  }, { passive: true });
}

function setupArtSwipeGesture() {
  _attachArtSwipe();
  const fp = document.getElementById('fullscreen-player');
  if (fp) fp.addEventListener('transitionend', () => _attachArtSwipe(), { passive: true });
}

function _animateArtSwipe(direction, callback) {
  const artWrap = document.getElementById('fp-art-wrap');
  if (!artWrap) { callback(); return; }
  const xOut = direction === 'left' ? '-115%' : '115%';
  const xIn  = direction === 'left' ? '115%'  : '-115%';
  artWrap.style.transition = 'transform .2s cubic-bezier(0.4,0,1,1), opacity .18s ease';
  artWrap.style.transform = `translateX(${xOut}) rotate(${direction === 'left' ? -4 : 4}deg)`;
  artWrap.style.opacity = '0';
  setTimeout(() => {
    artWrap.style.transition = 'none';
    artWrap.style.transform = `translateX(${xIn}) rotate(${direction === 'left' ? 4 : -4}deg)`;
    artWrap.style.opacity = '0';
    _resetShuffleNext(); callback();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      artWrap.style.transition = 'transform .42s cubic-bezier(0.22,1,0.36,1), opacity .3s ease';
      artWrap.style.transform = ''; artWrap.style.opacity = '';
    }));
  }, 185);
}

function setupShakeGesture() {
  if (!window.DeviceMotionEvent) return;
  const THRESHOLD = 18, COOLDOWN = 1500;
  let lastShake = 0, lastX = 0, lastY = 0, lastZ = 0, initialized = false;

  function onMotion(e) {
    if (typeof appSettings !== 'undefined' && appSettings?.shakeToShuffle === false) return;
    const acc = e.accelerationIncludingGravity;
    if (!acc) return;
    if (!initialized) { lastX = acc.x||0; lastY = acc.y||0; lastZ = acc.z||0; initialized = true; return; }
    const dx = Math.abs((acc.x||0) - lastX), dy = Math.abs((acc.y||0) - lastY), dz = Math.abs((acc.z||0) - lastZ);
    lastX = acc.x||0; lastY = acc.y||0; lastZ = acc.z||0;
    if (dx + dy + dz > THRESHOLD) {
      const now = Date.now();
      if (now - lastShake < COOLDOWN) return;
      lastShake = now;
      if (currentQueue.length > 1) {
        haptic([20, 50, 20]); shuffleOn = true;
        const shuffleBtn = document.getElementById('shuffle-btn');
        if (shuffleBtn) shuffleBtn.querySelector('svg').style.stroke = 'var(--gold-l)';
        showToast('🔀 Shuffled!'); nextTrack();
      } else if (currentTrack) { haptic([10, 30]); audio.currentTime = 0; showToast('🔀 Replaying'); }
    }
  }

  if (typeof DeviceMotionEvent.requestPermission === 'function') {
    document.addEventListener('touchend', function askPerm() {
      DeviceMotionEvent.requestPermission()
        .then(r => { if (r === 'granted') window.addEventListener('devicemotion', onMotion, { passive: true }); })
        .catch(() => {});
      document.removeEventListener('touchend', askPerm);
    }, { once: true });
  } else { window.addEventListener('devicemotion', onMotion, { passive: true }); }
}

// ─── 20. PLAYER OPEN / CLOSE ─────────────────────────────────────────────────
function openFullscreen() {
  const fp = document.getElementById('fullscreen-player');
  const mp = document.getElementById('mini-player');
  if (!fp) return;
  fp.style.transform = ''; fp.classList.add('open');
  document.body.classList.add('fp-open');
  if (mp) { mp.style.transition = 'opacity 0.2s ease, transform 0.2s ease'; mp.style.opacity = '0'; mp.style.pointerEvents = 'none'; }
  updateNextStrip();
  setTimeout(() => _attachArtSwipe(), 100);
  if (!document.hidden && !isLowEnd) _startViz();
  if (!isLowEnd) { const ac = document.getElementById('ambient-canvas'); if (ac) ac.classList.add('orbs-active'); }
}

function closeFullscreen() {
  const fp = document.getElementById('fullscreen-player');
  const mp = document.getElementById('mini-player');
  if (!fp || !fp.classList.contains('open')) return;
  fp.style.transform = ''; fp.classList.remove('open');
  document.body.classList.remove('fp-open');
  closeQueuePanel(); _stopViz();
  const ac = document.getElementById('ambient-canvas');
  if (ac) ac.classList.remove('orbs-active');
  if (mp) {
    fp._closeId = (fp._closeId || 0) + 1;
    const closeId = fp._closeId;
    setTimeout(() => {
      if (fp._closeId !== closeId) return;
      // [FIX-MINI-CLOSE] Force all inline styles reset before showing
      mp.style.transition = ''; mp.style.transform = '';
      mp.style.opacity = ''; mp.style.pointerEvents = '';
      if (currentTrack) showMiniPlayer();
    }, 220);
  }
}

// ─── 21. QUEUE PANEL ─────────────────────────────────────────────────────────
function toggleQueuePanel() { queuePanelOpen ? closeQueuePanel() : openQueuePanel(); }

function openQueuePanel() {
  const panel = document.getElementById('queue-panel');
  const btn   = document.getElementById('fp-queue-btn');
  if (!panel) return;
  panel.style.transform = ''; panel.style.transition = '';
  queuePanelOpen = true; panel.classList.add('open');
  if (btn) btn.classList.add('queue-open');
  requestAnimationFrame(() => { updateQueuePanel(); _attachQueueSwipe(); });
}

function closeQueuePanel() {
  const panel = document.getElementById('queue-panel');
  const btn   = document.getElementById('fp-queue-btn');
  if (!panel) return;
  queuePanelOpen = false; panel.style.transform = '';
  panel.classList.remove('open');
  if (btn) btn.classList.remove('queue-open');
}

function updateQueuePanel() {
  const body    = document.getElementById('queue-panel-body');
  const countEl = document.getElementById('queue-count');
  if (!body) return;
  body._swipeAttached = false;
  body.innerHTML = '';
  if (!currentQueue.length) {
    body.innerHTML = '<div style="padding:32px;text-align:center;color:var(--text3);font-size:12px;">Queue is empty</div>';
    return;
  }
  const remaining = currentQueue.length - currentIndex - 1;
  if (countEl) countEl.textContent = remaining + ' songs remaining';
  if (currentTrack) {
    const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = 'NOW PLAYING';
    body.appendChild(sec); body.appendChild(makeQueueItem(currentTrack, currentIndex, true));
  }
  const nextSongs = currentQueue.slice(currentIndex + 1);
  if (nextSongs.length) {
    const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = `UP NEXT (${nextSongs.length})`;
    body.appendChild(sec);
    for (let i = 0; i < nextSongs.length; i++) body.appendChild(makeQueueItem(nextSongs[i], currentIndex + 1 + i, false));
  }
  if (currentIndex > 0) {
    const prevSongs = currentQueue.slice(Math.max(0, currentIndex - 8), currentIndex);
    if (prevSongs.length) {
      const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = 'PREVIOUSLY PLAYED';
      body.appendChild(sec);
      for (let i = 0; i < prevSongs.length; i++) body.appendChild(makeQueueItem(prevSongs[i], Math.max(0, currentIndex - prevSongs.length) + i, false));
    }
  }
  updateNextStrip();
}

function makeQueueItem(song, qIdx, isCurrent) {
  const item = document.createElement('div');
  item.className = 'queue-item' + (isCurrent ? ' current' : '');
  item.dataset.trackId = String(song.trackId);
  const artUrl = getArtUrl(song, '300x300');
  const dur = song.trackTimeMillis ? formatMs(song.trackTimeMillis) : '';
  item.dataset.dur = dur;
  const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
  setImgSrc(img, artUrl); item.appendChild(img);
  const info = document.createElement('div'); info.className = 'queue-item-info';
  const safeName   = (song && song.trackName)  ? esc(song.trackName)  : 'Unknown';
  const safeArtist = (song && song.artistName) ? esc(song.artistName) : 'Unknown';
  info.innerHTML = `<div class="queue-item-title">${safeName}</div><div class="queue-item-artist">${safeArtist}</div>`;
  item.appendChild(info);
  if (isCurrent && isPlaying) {
    const bar = document.createElement('div'); bar.className = 'queue-now-playing';
    bar.innerHTML = '<span></span><span></span><span></span>'; item.appendChild(bar);
  } else {
    const d = document.createElement('span'); d.className = 'queue-item-dur'; d.textContent = dur; item.appendChild(d);
  }
  if (!isCurrent) item.onclick = () => { currentIndex = qIdx; loadTrack(currentQueue[currentIndex]); updateQueuePanel(); closeQueuePanel(); };
  return item;
}

// ─── 22. SWIPE TO REMOVE FROM QUEUE ──────────────────────────────────────────
function _attachQueueSwipe() {
  const queueBody = document.getElementById('queue-panel-body');
  if (!queueBody || queueBody._swipeAttached) return;
  queueBody._swipeAttached = true;
  let startX = 0, startY = 0, startTime = 0, currentItem = null, isSwiping = false;
  const swipeThreshold = 80;

  queueBody.addEventListener('touchstart', (e) => {
    const item = e.target.closest('.queue-item');
    if (!item || item.classList.contains('current')) return;
    startX = e.touches[0].clientX; startY = e.touches[0].clientY;
    startTime = Date.now(); currentItem = item; isSwiping = true;
    item.style.transition = 'none';
  }, { passive: true });

  queueBody.addEventListener('touchmove', (e) => {
    if (!isSwiping || !currentItem) return;
    const dx = e.touches[0].clientX - startX, dy = e.touches[0].clientY - startY;
    if (Math.abs(dx) < 15 && Math.abs(dy) < 15) return;
    if (Math.abs(dx) > Math.abs(dy) && dx < 0) {
      e.preventDefault();
      const translateX = Math.max(-swipeThreshold, dx);
      currentItem.style.transform = `translateX(${translateX}px)`;
      currentItem.style.opacity   = String(1 - (Math.abs(translateX) / swipeThreshold) * 0.8);
      currentItem.style.transition = 'none';
    }
  }, { passive: false });

  queueBody.addEventListener('touchend', (e) => {
    if (!isSwiping || !currentItem) { resetSwipe(); return; }
    const dx = e.changedTouches[0].clientX - startX;
    const velocity = Math.abs(dx) / (Date.now() - startTime);
    if (dx < -swipeThreshold || (dx < -40 && velocity > 0.3)) {
      const trackId = currentItem.dataset.trackId;
      removeFromQueue(trackId);
      currentItem.style.transition = 'transform 0.2s ease, opacity 0.15s ease';
      currentItem.style.transform = 'translateX(-100%)'; currentItem.style.opacity = '0';
      setTimeout(() => {
        if (currentItem && currentItem.parentNode) currentItem.remove();
        updateQueuePanel(); updateNextStrip(); _attachQueueSwipe();
      }, 180);
    } else {
      currentItem.style.transition = 'transform 0.25s cubic-bezier(0.2,0.9,0.4,1.1), opacity 0.2s ease';
      currentItem.style.transform = ''; currentItem.style.opacity = '';
    }
    resetSwipe();
  });

  function resetSwipe() {
    if (currentItem) { currentItem.style.transition = ''; currentItem.style.transform = ''; currentItem.style.opacity = ''; }
    isSwiping = false; currentItem = null; startX = 0; startY = 0;
  }
  queueBody.addEventListener('touchcancel', resetSwipe);
}

function setupSwipeToRemove() { _attachQueueSwipe(); }

function removeFromQueue(trackId) {
  const index = currentQueue.findIndex(s => String(s.trackId) === String(trackId));
  if (index === -1) return;
  if (index === currentIndex) { showToast("Can't remove currently playing song"); return; }
  const wasBeforeCurrent = index < currentIndex;
  currentQueue.splice(index, 1);
  if (wasBeforeCurrent) currentIndex = Math.max(0, currentIndex - 1);
  if (currentIndex >= currentQueue.length) currentIndex = Math.max(0, currentQueue.length - 1);
  updateQueuePanel(); updateNextStrip(); haptic(15); showToast('Removed from queue');
}

// ─── 23. QUEUE PANEL GESTURE ─────────────────────────────────────────────────
function setupQueuePanelGesture() {
  const qp = document.getElementById('queue-panel');
  if (!qp) return;
  let startY = 0, startX = 0, isDragging = false, startTime = 0;
  let moved = false, rafId = null, axisLocked = null;

  function snapBack() {
    qp.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
    qp.style.transform = ''; qp.style.willChange = '';
    setTimeout(() => { qp.style.transition = ''; }, 340); _resumeBlur();
  }

  qp.addEventListener('touchstart', e => {
    const onHandle  = e.target.closest('#queue-drag-handle') || e.target.closest('.queue-panel-handle');
    const body      = document.getElementById('queue-panel-body');
    const bodyAtTop = !body || body.scrollTop <= 0;
    if (!onHandle && !bodyAtTop) return;
    startY = e.touches[0].clientY; startX = e.touches[0].clientX;
    isDragging = true; startTime = Date.now(); moved = false; axisLocked = null;
    qp.style.transition = 'none'; qp.style.willChange = 'transform';
    qp.classList.add('dragging'); _pauseBlur();
  }, { passive: true });

  qp.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dy = e.touches[0].clientY - startY, dx = e.touches[0].clientX - startX;
    if (!axisLocked && (Math.abs(dy) > 6 || Math.abs(dx) > 6)) {
      axisLocked = Math.abs(dx) > Math.abs(dy) ? 'horizontal' : 'vertical';
    }
    if (axisLocked === 'horizontal') { isDragging = false; return; }
    if (axisLocked === 'vertical' && dy > 0 && Math.abs(dy) > 4) {
      moved = true; e.preventDefault();
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => { qp.style.transform = `translateY(${dy}px)`; rafId = null; });
    }
  }, { passive: false });

  qp.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return;
    isDragging = false; qp.classList.remove('dragging'); qp.style.willChange = '';
    const dy = e.changedTouches[0].clientY - startY;
    const vel = dy / Math.max(1, Date.now() - startTime);
    _resumeBlur();
    if (!moved) { snapBack(); return; }
    if (dy > 90 || (vel > 0.45 && dy > 25)) { qp.style.transform = ''; closeQueuePanel(); }
    else snapBack();
  }, { passive: true });

  qp.addEventListener('touchcancel', () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging = false; qp.classList.remove('dragging'); qp.style.willChange = ''; snapBack();
  }, { passive: true });
}

// ─── 24. ARTIST PAGE ─────────────────────────────────────────────────────────
function openArtistPage(artistName, songs, artUrl) {
  let page = document.getElementById('artist-page');
  if (!page) {
    page = document.createElement('div');
    page.id = 'artist-page'; page.className = 'artist-page';
    const app = document.getElementById('app');
    if (app) app.appendChild(page);
  }
  const thumbUrl = artUrl || (songs[0] ? getArtUrl(songs[0], '600x600') : '');
  page.innerHTML = `
    <div class="artist-page-hero">
      <img class="artist-page-bg" src="${thumbUrl}" alt="" crossorigin="anonymous">
      <div class="artist-page-overlay"></div>
      <div class="artist-page-topbar">
        <button class="ap-back-btn" id="ap-back-btn" aria-label="Back">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
        <div class="ap-logo">
          <svg viewBox="0 0 28 28" fill="none" width="20" height="20"><path d="M4 23L10 7L14 16L18 7L24 23" stroke="rgba(184,150,64,0.28)" stroke-width="1" stroke-linecap="round"/><path d="M6.5 23L12 8.5L14 13" stroke="var(--gold-l)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M14 13L16 8.5L21.5 23" stroke="var(--gold)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span>Aurum</span>
        </div>
        <button class="ap-share-btn" id="ap-share-btn" aria-label="Share">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
        </button>
      </div>
      <div class="artist-page-info">
        <div class="ap-artist-name"></div>
        <div class="ap-track-count">${songs.length} songs</div>
      </div>
      <div class="artist-page-actions">
        <button class="ap-play-btn" id="ap-play-btn">
          <svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3" fill="currentColor"/></svg>
          Play All
        </button>
        <button class="ap-shuffle-btn" id="ap-shuffle-btn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/></svg>
        </button>
      </div>
    </div>
    <div class="artist-page-songs" id="ap-songs-list"></div>
  `;
  const apName = page.querySelector('.ap-artist-name');
  if (apName) apName.textContent = artistName;
  const apBack = page.querySelector('#ap-back-btn'); if (apBack) apBack.onclick = () => closeArtistPage();
  const apShare = page.querySelector('#ap-share-btn'); if (apShare) apShare.onclick = () => _shareArtist(artistName);
  const apPlay = page.querySelector('#ap-play-btn'); if (apPlay) apPlay.onclick = () => _playArtistAll();
  const apShuffle = page.querySelector('#ap-shuffle-btn'); if (apShuffle) apShuffle.onclick = () => _playArtistShuffle();
  page._songs = songs; page._artistName = artistName;
  const list = document.getElementById('ap-songs-list');
  if (list) { for (let i = 0; i < songs.length; i++) list.appendChild(makeSongRow(songs[i], i, songs)); }
  requestAnimationFrame(() => page.classList.add('open'));
}

function closeArtistPage() {
  const page = document.getElementById('artist-page'); if (!page) return;
  page.classList.remove('open'); setTimeout(() => page.remove(), 340);
}

function _playArtistAll() {
  const page = document.getElementById('artist-page');
  if (!page?._songs?.length) return;
  playSongs(page._songs, 0); openFullscreen();
}

function _playArtistShuffle() {
  const page = document.getElementById('artist-page');
  if (!page?._songs?.length) return;
  playSongs(page._songs, Math.floor(Math.random() * page._songs.length));
  shuffleOn = true;
  const sb = document.getElementById('shuffle-btn');
  if (sb) sb.querySelector('svg').style.stroke = 'var(--gold-l)';
  showToast('Shuffle on'); openFullscreen();
}

function _shareArtist(artistName) {
  if (navigator.share) {
    navigator.share({ title: artistName + ' on Aurum', text: 'Check out ' + artistName + ' on Aurum!', url: window.location.href }).catch(() => {});
  } else { navigator.clipboard?.writeText(window.location.href).then(() => showToast('Link copied')); }
}

async function openArtistPageFromName(artistName) {
  showToast('Loading ' + artistName + '…');
  try {
    const q = encodeURIComponent(artistName + ' songs');
    const r = await fetch(`/api/songs?q=${q}`);
    const d = await r.json();
    const songs = (d.results || []).filter(s => s.previewUrl || s._source === 'saavn');
    if (!songs.length) { showToast('No songs found'); return; }
    openArtistPage(artistName, songs, getArtUrl(songs[0], '600x600'));
  } catch(e) { showToast('Could not load artist'); }
}

async function fetchRecommendations(song) {
  if (!song) return;
  // [PATCH-1] Target 65 songs — previously capped at 15 which caused queue drift
  if (currentQueue.length >= QUEUE_TARGET) return;

  const ctrl = new AbortController();
  _recFetchAbort = ctrl;

  // Mark this song in session history
  _sessionPlayedIds.add(String(song.trackId));
  const _primaryArtist = (song.artistName || '').split(/[&,]|feat\.|ft\./i)[0].trim().toLowerCase();
  if (_primaryArtist) {
    _sessionArtistFreq[_primaryArtist] = (_sessionArtistFreq[_primaryArtist] || 0) + 1;
  }

  // Build ranked query list: same artist first, then session artists, then related
  const cleanArtist = (song.artistName || '').split(/[&,]|feat\.|ft\./i)[0].trim();
  const cleanTitle  = (song.trackName  || '').replace(/\(.*?\)|\[.*?\]/g, '').trim();
  const topSessionArtists = Object.entries(_sessionArtistFreq)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([a]) => a);

  const queries = [
    `${cleanArtist} best songs`,
    `${cleanArtist} top hits`,
    ...topSessionArtists
      .filter(a => a !== _primaryArtist)
      .map(a => `${a} songs`),
    `${cleanTitle} similar songs`,
    `${cleanArtist} ${song.primaryGenreName || ''} songs`.trim(),
  ].filter(Boolean).slice(0, 4);

  const existingIds = new Set(currentQueue.map(s => String(s.trackId)));

  // ── Scoring function — pure frontend, no backend change ─────────────────
  function _scoreSong(candidate) {
    let score = 0;
    const cArtist = (candidate.artistName || '').toLowerCase();
    const cGenre  = (candidate.primaryGenreName || '').toLowerCase();
    const sGenre  = (song.primaryGenreName || '').toLowerCase();

    // Artist match — highest priority
    if (cArtist.includes(_primaryArtist) || _primaryArtist.includes(cArtist.split(/[&,]/)[0].trim())) {
      score += 50;
    }
    // Session artist boost
    for (const [sessArtist, freq] of Object.entries(_sessionArtistFreq)) {
      if (cArtist.includes(sessArtist)) {
        score += Math.min(freq * 8, 24);
        break;
      }
    }
    // Genre match
    if (sGenre && cGenre && (cGenre.includes(sGenre) || sGenre.includes(cGenre))) {
      score += 20;
    }
    // Duration proximity (±45s feels cohesive)
    if (song.trackTimeMillis && candidate.trackTimeMillis) {
      const diff = Math.abs(song.trackTimeMillis - candidate.trackTimeMillis) / 1000;
      if (diff < 45) score += 10;
    }
    // Penalize version/remix songs unless current is also one
    if (_isVersionSong(candidate.trackName || '') && !_isVersionSong(song.trackName || '')) {
      score -= 30;
    }
    // Lightly penalize already-heard-this-session songs
    if (_sessionPlayedIds.has(String(candidate.trackId))) {
      score -= 15;
    }
    return score;
  }

  // ── Quality gate: only pass songs with meaningful artist or score relation ─
  function _passesQualityGate(candidate) {
    const cArtist = (candidate.artistName || '').toLowerCase();
    if (!candidate.previewUrl && candidate._source !== 'saavn') return false;
    if (existingIds.has(String(candidate.trackId))) return false;
    const artistMatch = cArtist.includes(_primaryArtist) ||
      _primaryArtist.includes(cArtist.split(/[&,]/)[0].trim()) ||
      Object.keys(_sessionArtistFreq).some(a => cArtist.includes(a));
    const score = _scoreSong(candidate);
    return artistMatch || score >= 20;
  }

  // ── Fetch from multiple queries, collect candidates ───────────────────────
  const allCandidates = [];

  for (const q of queries) {
    if (ctrl.signal.aborted) return;
    if (currentQueue.length + allCandidates.length >= QUEUE_TARGET) break;
    try {
      const r = await fetch(`/api/songs?q=${encodeURIComponent(q)}`, { signal: ctrl.signal });
      if (ctrl.signal.aborted) return;
      const d = await r.json();
      const batch = (d.results || []).filter(s => _passesQualityGate(s));
      for (const s of batch) {
        if (!allCandidates.find(c => String(c.trackId) === String(s.trackId))) {
          allCandidates.push(s);
          existingIds.add(String(s.trackId));
        }
      }
    } catch (e) {
      if (e.name === 'AbortError') return;
    }
  }

  if (!allCandidates.length) return;

  // ── Sort by score descending ──────────────────────────────────────────────
  allCandidates.sort((a, b) => _scoreSong(b) - _scoreSong(a));

  // ── Interleave by artist so same-artist songs don't all clump together ────
  const byArtist = {};
  for (const s of allCandidates) {
    const key = (s.artistName || '').split(/[&,]/)[0].trim().toLowerCase();
    if (!byArtist[key]) byArtist[key] = [];
    byArtist[key].push(s);
  }
  const buckets   = Object.values(byArtist);
  const maxLen    = Math.max(...buckets.map(b => b.length));
  const interleaved = [];
  for (let i = 0; i < maxLen; i++) {
    for (const bucket of buckets) {
      if (bucket[i]) interleaved.push(bucket[i]);
    }
  }

  // ── Append to queue ───────────────────────────────────────────────────────
  const slotsLeft = QUEUE_TARGET - currentQueue.length;
  const toAdd     = interleaved.slice(0, slotsLeft);
  if (!toAdd.length) return;

  currentQueue = [...currentQueue, ...toAdd];
  if (queuePanelOpen) updateQueuePanel();
  updateNextStrip();
}

// ─── 25. RECENTLY PLAYED ──────────────────────────────────────────────────────
function addToRecentlyPlayed(song) {
  recentlyPlayed = recentlyPlayed.filter(s => String(s.trackId) !== String(song.trackId));
  recentlyPlayed.unshift(song);
  if (recentlyPlayed.length > 20) recentlyPlayed = recentlyPlayed.slice(0, 20);
  try { localStorage.setItem('aurum_recent_played', JSON.stringify(recentlyPlayed)); } catch(e) {}
  _trackListen(song); _resetShuffleNext(); renderQuickResume();
}

// ─── 26. HOME SECTIONS ────────────────────────────────────────────────────────
const SECTION_POOL = [
  { id:'recent',   title:'Continue Listening', type:'wide',     fn: getRecentlyPlayedSongs },
  { id:'featured', title:'Made For You',       type:'featured', queries:['top bollywood songs hits','best hindi songs','latest bollywood hits','top hindi songs trending','best bollywood songs playlist'] },
  { id:'trending', title:'Trending Now',       type:'cards',    queries:['trending hindi songs chart','bollywood chart toppers','top hindi songs this week','most popular bollywood songs'] },
  { id:'mood',     title:'Mood: Late Night',   type:'bw',       queries:['sad emotional bollywood songs','heartbreak hindi songs arijit','late night slow songs hindi','emotional romantic songs hindi'] },
  { id:'romantic', title:'Bollywood Romantic', type:'bw',       queries:['bollywood romantic songs hits','best romantic hindi songs','love songs bollywood','romantic songs arijit atif'] },
  { id:'classic',  title:'Golden Era',         type:'bw',       queries:['90s bollywood romantic classic songs','80s hindi classic songs','old is gold bollywood songs kishore kumar','retro bollywood hits lata mangeshkar'] },
  { id:'hiphop',   title:'Desi Hip-Hop',       type:'cards',    queries:['divine emiway bantai rap hindi','desi hip hop india rap songs','yo yo honey singh badshah rap','india rap gully boy songs'] },
  { id:'lofi',     title:'Lo-Fi Chill',        type:'cards',    queries:['lofi chill beats hindi songs','lofi bollywood remix chill','lo-fi hindi songs study chill','lofi beats india relaxing'] },
  { id:'arijit',   title:'Arijit Singh',       type:'rows',     queries:['arijit singh best romantic songs','arijit singh top hits','arijit singh soulful songs','arijit singh emotional hits'] },
  { id:'atif',     title:'Atif Aslam',         type:'rows',     queries:['atif aslam best songs','atif aslam top hits hindi','atif aslam romantic songs','atif aslam soulful'] },
  { id:'shreya',   title:'Shreya Ghoshal',     type:'rows',     queries:['shreya ghoshal best songs','shreya ghoshal romantic hits','shreya ghoshal top songs'] },
  { id:'neha',     title:'Neha Kakkar',        type:'rows',     queries:['neha kakkar best songs','neha kakkar hits','neha kakkar popular songs'] },
  { id:'kumar',    title:'Kumar Sanu',         type:'rows',     queries:['kumar sanu 90s hits','kumar sanu best songs','kumar sanu alka yagnik duets'] },
  { id:'kishore',  title:'Kishore Kumar',      type:'rows',     queries:['kishore kumar best songs','kishore kumar classics','kishore kumar evergreen hits'] },
  { id:'workout',  title:'Energy Boost',       type:'cards',    queries:['workout hindi songs gym','upbeat dance bollywood songs','party hindi songs badshah','high energy bollywood beats'] },
  { id:'sad',      title:'Heartbreak',         type:'bw',       queries:['sad hindi songs breakup','dil tod ke chali gayi','heartbreak bollywood songs','tere bina hindi sad songs'] },
  { id:'party',    title:'Party Hits',         type:'cards',    queries:['bollywood party songs dance','badshah party hits','punjabi party songs','hindi dance floor hits'] },
  { id:'new',      title:'New Releases',       type:'cards',    queries:['new hindi songs latest','new bollywood songs released','latest hindi songs hits','new bollywood songs trending'] },
];

const _algoSections = {};
const genreMap = {
  all:'top bollywood songs hits', bollywood:'bollywood romantic songs hits',
  hiphop:'desi hip hop rap india', pop:'pop hits bollywood', rock:'rock songs hindi',
  indie:'indie bollywood songs', rnb:'rnb soul songs india', lofi:'lofi chill beats hindi songs'
};
const genreSections = {
  bollywood:['featured','trending','classic','arijit'],
  hiphop:['hiphop','trending','featured','new'],
  pop:['featured','new','trending','workout'],
  rock:['featured','trending','classic','new'],
  indie:['featured','lofi','trending','new'],
  rnb:['featured','mood','trending','new'],
  lofi:['lofi','mood','featured','classic']
};
const BOLLYWOOD_META = [
  {color:'#c48c28',genre:'Romance'},{color:'#b83838',genre:'Love'},
  {color:'#9838b8',genre:'Romantic'},{color:'#3878c8',genre:'Sad Vibes'},
  {color:'#5434a8',genre:'Heartbreak'},{color:'#b82858',genre:'Dance'},
  {color:'#286c3c',genre:'Chill'},{color:'#6c4c18',genre:'Classic'}
];

function getRecentlyPlayedSongs() { return Promise.resolve(recentlyPlayed); }

function _pickQuery(sec) {
  if (!sec.queries || !sec.queries.length) return null;
  return sec.queries[Math.floor(Math.random() * sec.queries.length)];
}

// [FIX-MB-2] loadHomeSection — sectionCache properly use karo
// Pehle cache check nahi hota tha — har render pe fresh API call hoti thi
async function loadHomeSection(sec) {
  // Cache hit — no API call
  if (sectionCache[sec.id] && sectionCache[sec.id].length > 0) {
    return sectionCache[sec.id];
  }

  try {
    if (typeof sec.fn === 'function') {
      const songs = await sec.fn();
      sectionCache[sec.id] = songs;
      return songs;
    }
    const q = _pickQuery(sec);
    if (!q) return [];
    const ctrl = new AbortController();
    const to   = setTimeout(() => ctrl.abort(), 12000);
    try {
      const r = await fetch(`/api/songs?q=${encodeURIComponent(q)}`, { signal: ctrl.signal });
      clearTimeout(to);
      const d = await r.json();
      let songs = (d.results || []).filter(s => s.previewUrl || s._source === 'saavn');
      if (!['lofi','hiphop','party','workout'].includes(sec.id)) {
        songs = songs.filter(s => !_isVersionSong(s.trackName || ''));
      }
      for (let i = songs.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [songs[i], songs[j]] = [songs[j], songs[i]];
      }
      // Cache mein store karo
      sectionCache[sec.id] = songs;
      return songs;
    } catch(fe) {
      clearTimeout(to);
      if (sec.queries && sec.queries.length > 1) {
        const fq = sec.queries[Math.floor(Math.random() * sec.queries.length)];
        const r2 = await fetch(`/api/songs?q=${encodeURIComponent(fq)}`);
        const d2 = await r2.json();
        const songs = (d2.results || []).filter(s => s.previewUrl || s._source === 'saavn');
        sectionCache[sec.id] = songs;
        return songs;
      }
      return [];
    }
  } catch(e) { return []; }
}

function refreshHomeSections() {
  // [FIX] Refresh pe cache clear karo — fresh data fetch hogi
  sectionCache = {};
  haptic(15); buildHomeSections(currentGenre || 'all'); showToast('Refreshed');
}

function renderSkeletonSection(type, count = 4) {
  let html = '<div class="h-scroll-row" style="padding-right:20px;">';
  for (let i = 0; i < count; i++) {
    if (type === 'wide')    html += `<div class="wide-sk"><div class="wide-sk-cover"></div><div class="wide-sk-line" style="width:80%"></div><div class="wide-sk-line" style="width:50%;margin-top:4px;"></div></div>`;
    else if (type === 'bw') html += `<div class="bw-sk"><div class="bw-sk-cover"></div><div class="bw-sk-line w70"></div><div class="bw-sk-line w45"></div></div>`;
    else                    html += `<div class="quick-sk"><div class="quick-sk-cover"></div><div class="bw-sk-line w70" style="margin-top:8px;"></div><div class="bw-sk-line w45" style="margin-top:5px;"></div></div>`;
  }
  html += '</div>';
  return html;
}

function renderRowSkeleton(count = 5) {
  let html = '';
  for (let i = 0; i < count; i++) html += `<div class="sk-row"><div class="sk-art"></div><div class="sk-info"><div class="sk-line l1"></div><div class="sk-line l2"></div></div></div>`;
  return html;
}

async function buildHomeSections(genre = 'all') {
  const container = document.getElementById('home-sections');
  if (!container) return;
  container.innerHTML = ''; currentGenre = genre;
  let sections = [];
  if (genre === 'all') {
    sections.push(SECTION_POOL.find(s => s.id === 'recent'));
    sections.push(SECTION_POOL.find(s => s.id === 'featured'));
    const topArtists = _getTopArtists(3);
    for (const { artist, count } of topArtists) {
      const id = 'artist_' + artist.replace(/\s+/g, '_').toLowerCase();
      const existing = SECTION_POOL.find(s => s.title === artist);
      if (existing) { sections.push(existing); }
      else {
        const algoSec = { id, title: artist, type: 'rows', queries: [`${artist} best songs`, `${artist} top hits`, `${artist} popular songs`], _isAlgo: true, _listenCount: count };
        _algoSections[id] = algoSec; sections.push(algoSec);
      }
    }
    sections.push(SECTION_POOL.find(s => s.id === 'trending'));
    const usedIds = new Set(sections.map(s => s?.id));
    const rest = SECTION_POOL.filter(s => s && !usedIds.has(s.id)).sort(() => Math.random() - 0.5).slice(0, 4);
    sections.push(...rest);
  } else {
    const ids = genreSections[genre] || ['featured','trending','new','classic'];
    sections = ids.map(id => SECTION_POOL.find(s => s.id === id)).filter(Boolean);
  }
  sections = sections.filter(Boolean);
  for (const sec of sections) {
    const wrap = document.createElement('div'); wrap.className = 'section'; wrap.id = 'sec-wrap-' + sec.id;
    if (sec.id === 'recent' && !recentlyPlayed.length) wrap.style.display = 'none';
    const type      = sec.type === 'featured' ? 'cards' : sec.type;
    const typeCount = type === 'bw' ? 5 : type === 'wide' ? 5 : type === 'rows' ? 0 : 5;
    const badge     = sec._isAlgo ? ` <span class="algo-badge" style="font-size:9px;background:rgba(184,150,64,0.15);color:var(--gold);padding:2px 7px;border-radius:20px;font-weight:700;vertical-align:middle;">FOR YOU</span>` : '';
    wrap.innerHTML  = `<div class="section-head"><h2>${sec.title}${badge}</h2><span onclick="refreshSection('${sec.id}')">Refresh</span></div><div id="sec-${sec.id}">${type === 'rows' ? renderRowSkeleton() : renderSkeletonSection(type, typeCount)}</div>`;
    container.appendChild(wrap);
    _renderSection(sec, wrap);
  }
}

async function _renderSection(sec, wrap) {
  const songs = await loadHomeSection(sec);
  const el = document.getElementById('sec-' + sec.id); if (!el) return;
  if (!songs || !songs.length) {
    el.innerHTML = `<div style="padding:18px 4px;text-align:center;"><button onclick="refreshSection('${sec.id}')" style="background:var(--surface2);border:none;color:var(--text2);font-size:12px;padding:8px 18px;border-radius:20px;cursor:pointer;font-family:inherit;">Retry</button></div>`;
    return;
  }
  el.innerHTML = '';
  const type = sec.type === 'featured' ? 'cards' : sec.type;
  if (type === 'rows') {
    for (let i = 0; i < Math.min(songs.length, 12); i++) el.appendChild(makeSongRow(songs[i], i, songs));
  } else if (type === 'wide') {
    const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
    for (let i = 0; i < Math.min(songs.length, 8); i++) row.appendChild(makeWideCard(songs[i], i, songs));
    el.appendChild(row);
  } else if (type === 'bw') {
    const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
    for (let i = 0; i < Math.min(songs.length, 8); i++) row.appendChild(makeBwCard(songs[i], i, songs, BOLLYWOOD_META[i % BOLLYWOOD_META.length]));
    el.appendChild(row);
  } else {
    const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
    for (let i = 0; i < Math.min(songs.length, 8); i++) row.appendChild(makeQuickCard(songs[i], i, songs));
    el.appendChild(row);
  }
}

async function refreshSection(secId) {
  let sec = SECTION_POOL.find(s => s.id === secId);
  if (!sec) sec = _algoSections[secId];
  if (!sec) return;
  const wrap = document.getElementById('sec-wrap-' + secId); if (!wrap) return;
  const el   = document.getElementById('sec-' + secId); if (!el) return;
  const type = sec.type === 'featured' ? 'cards' : sec.type;
  el.innerHTML = type === 'rows' ? renderRowSkeleton() : renderSkeletonSection(type, 5);
  // [FIX] Specific section ka cache clear karo — fresh data aayega
  delete sectionCache[secId];
  haptic(10); _renderSection(sec, wrap);
}

function renderQuickResume() {
  const wrap = document.getElementById('sec-wrap-recent');
  const el   = document.getElementById('sec-recent');
  if (!el) return;
  if (!recentlyPlayed.length) { if (wrap) wrap.style.display = 'none'; return; }
  if (wrap) wrap.style.display = '';
  el.innerHTML = '';
  const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
  for (let i = 0; i < Math.min(recentlyPlayed.length, 8); i++) row.appendChild(makeWideCard(recentlyPlayed[i], i, recentlyPlayed));
  el.appendChild(row);
}

// ─── 27. CARD MAKERS ─────────────────────────────────────────────────────────
function makeQuickCard(s, i, queue) {
  const div = document.createElement('div'); div.className = 'quick-card anim-in';
  div.style.animationDelay = (i * 0.05) + 's';
  // [FIX-MB-3] Low-end pe 200x200 karo — less MB
  const imgSize = isLowEnd ? '200x200' : '400x400';
  const img = document.createElement('img'); img.alt = esc(s.trackName); img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, imgSize)); div.appendChild(img);
  const info = document.createElement('div'); info.className = 'quick-card-info';
  info.innerHTML = `<div class="quick-card-title">${esc(s.trackName)}</div><div class="quick-card-artist">${esc(s.artistName)}</div>`;
  div.appendChild(info);
  if (isTV) div.tabIndex = 0;
  div.onclick = () => playSongs(queue, i);
  return div;
}

function makeWideCard(s, i, queue) {
  const div = document.createElement('div'); div.className = 'wide-card anim-in';
  div.style.animationDelay = (i * 0.05) + 's';
  // [FIX-MB-3] Low-end pe 200x200 karo — less MB
  const imgSize = isLowEnd ? '200x200' : '400x400';
  const cover = document.createElement('div'); cover.className = 'wide-card-cover';
  const img = document.createElement('img'); img.alt = esc(s.trackName); img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, imgSize));
  const play = document.createElement('div'); play.className = 'wide-card-play';
  play.innerHTML = '<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3" fill="white"/></svg>';
  cover.appendChild(img); cover.appendChild(play);
  const info = document.createElement('div'); info.className = 'wide-card-info';
  info.innerHTML = `<div class="wide-card-title">${esc(s.trackName)}</div><div class="wide-card-sub">${esc(s.artistName)}</div>`;
  div.appendChild(cover); div.appendChild(info);
  if (isTV) div.tabIndex = 0;
  div.onclick = () => playSongs(queue, i);
  return div;
}

function makeBwCard(s, i, queue, meta) {
  meta = meta || { color:'#b89640', genre:'Music' };
  const div = document.createElement('div'); div.className = 'bw-card anim-in';
  div.style.animationDelay = (i * 0.05) + 's';
  // [FIX-MB-3] Low-end pe 200x200 karo — less MB
  const imgSize = isLowEnd ? '200x200' : '400x400';
  const cover = document.createElement('div'); cover.className = 'bw-card-cover';
  const img = document.createElement('img'); img.alt = esc(s.trackName); img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, imgSize)); cover.appendChild(img);
  const overlay = document.createElement('div'); overlay.className = 'bw-card-overlay';
  overlay.innerHTML = `<div class="bw-card-genre" style="color:${meta.color}">${meta.genre}</div><div class="bw-card-title">${esc(s.trackName)}</div><div class="bw-card-sub">${esc(s.artistName)}</div>`;
  cover.appendChild(overlay);
  const info = document.createElement('div'); info.className = 'bw-card-info';
  info.innerHTML = `<div class="bw-card-name">${esc(s.trackName)}</div><div class="bw-card-artist">${esc(s.artistName)}</div>`;
  div.appendChild(cover); div.appendChild(info);
  if (isTV) div.tabIndex = 0;
  div.onclick = () => playSongs(queue, i);
  return div;
}

function makeSongRow(s, i, queue) {
  const row = document.createElement('div'); row.className = 'song-row anim-in';
  row.dataset.trackId = String(s.trackId);
  row.style.animationDelay = (i * 0.034) + 's';
  const dur = s.trackTimeMillis ? formatMs(s.trackTimeMillis) : '';
  row.dataset.dur = dur;
  const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, '300x300')); row.appendChild(img);
  const info = document.createElement('div'); info.className = 'song-row-info';
  const titleDiv = document.createElement('div'); titleDiv.className = 'song-row-title'; titleDiv.textContent = s.trackName || '';
  const artistDiv = document.createElement('div'); artistDiv.className = 'song-row-artist';
  const artistSpan = document.createElement('span'); artistSpan.className = 'artist-link'; artistSpan.textContent = s.artistName || '';
  artistDiv.appendChild(artistSpan); info.appendChild(titleDiv); info.appendChild(artistDiv); row.appendChild(info);
  const right = document.createElement('div'); right.className = 'song-row-right';
  const heartBtn = document.createElement('button');
  heartBtn.className = 'song-row-heart' + (isSaved(s) ? ' saved' : '');
  heartBtn.setAttribute('aria-label', 'Like');
  heartBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>';
  heartBtn.onclick = e => { e.stopPropagation(); toggleSave(s); heartBtn.classList.toggle('saved', isSaved(s)); haptic(10); };
  const durSpan = document.createElement('span'); durSpan.className = 'song-row-duration'; durSpan.textContent = dur;
  const moreBtn = document.createElement('button'); moreBtn.className = 'song-row-more';
  moreBtn.setAttribute('aria-label', 'More options');
  moreBtn.innerHTML = '<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.2" fill="currentColor"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><circle cx="19" cy="12" r="1.2" fill="currentColor"/></svg>';
  moreBtn.onclick = e => { e.stopPropagation(); openSongModal(s); };
  right.appendChild(heartBtn); right.appendChild(durSpan); right.appendChild(moreBtn);
  row.appendChild(right);
  if (isTV) row.tabIndex = 0;
  row._song = s;
  let _pt = null, _longFired = false, _moved = false, _downX = 0, _downY = 0;
  row.addEventListener('pointerdown', e => {
    _longFired = false; _moved = false; _downX = e.clientX; _downY = e.clientY;
    if (isTV) return;
    _pt = setTimeout(() => {
      if (_moved) return;
      _longFired = true; row.classList.add('long-press-active');
      haptic([20, 40, 20]); openSongModal(s);
      setTimeout(() => row.classList.remove('long-press-active'), 300);
    }, 480);
  }, { passive: true });
  row.addEventListener('pointermove', e => {
    if (Math.abs(e.clientX - _downX) > 8 || Math.abs(e.clientY - _downY) > 8) {
      _moved = true; if (_pt) { clearTimeout(_pt); _pt = null; }
    }
  }, { passive: true });
  row.addEventListener('pointerup',     () => { if (_pt) { clearTimeout(_pt); _pt = null; } }, { passive: true });
  row.addEventListener('pointercancel', () => { if (_pt) { clearTimeout(_pt); _pt = null; } row.classList.remove('long-press-active'); _moved = false; }, { passive: true });
  row.addEventListener('click', e => {
    if (_longFired || _moved) return;
    if (e.target.closest('.song-row-heart') || e.target.closest('.song-row-more')) return;
    if (e.target === artistSpan || artistSpan.contains(e.target)) {
      e.stopPropagation();
      const name = (s.artistName || '').split(/[&,]/)[0].trim();
      if (name) openArtistPageFromName(name);
      return;
    }
    playSongs(queue, i); haptic(8);
  });
  return row;
}

// ─── 28. GENRE FILTER ────────────────────────────────────────────────────────
function filterHome(genre, chip) {
  document.querySelectorAll('#home-chips .chip').forEach(c => c.classList.remove('active'));
  chip.classList.add('active', 'popping');
  chip.addEventListener('animationend', () => chip.classList.remove('popping'), { once: true });
  haptic(8); buildHomeSections(genre);
}

// ─── 29. SEARCH ──────────────────────────────────────────────────────────────
const browseCategories = [
  { label:'Pop',        sub:'Charts & hits',         cls:'bc-pop',        genre:'pop' },
  { label:'Hip-Hop',    sub:'Trap & rap',             cls:'bc-hiphop',     genre:'hiphop' },
  { label:'Rock',       sub:'Classic & alternative',  cls:'bc-rock',       genre:'rock' },
  { label:'Indie',      sub:'Chill & discover',       cls:'bc-indie',      genre:'indie' },
  { label:'R&B',        sub:'Soul & vibes',           cls:'bc-rnb',        genre:'rnb' },
  { label:'Electronic', sub:'Dance & beats',          cls:'bc-electronic', genre:'electronic' },
  { label:'Trending',   sub:'Right now',              cls:'bc-trending',   genre:'trending' },
  { label:'Chill',      sub:'Relax & unwind',         cls:'bc-chill',      genre:'chill' },
];
const extraGenreMap = { electronic:'electronic music dance', trending:'top trending songs', chill:'chill lofi music' };

let _suggestTimeout = null;
let _lastSuggestQ = '';

function _createSuggestDropdown() {
  let d = document.getElementById('search-suggest-drop');
  if (d) return d;
  d = document.createElement('div');
  d.id = 'search-suggest-drop';
  d.style.cssText = `
    position:absolute;left:0;right:0;top:100%;z-index:999;
    background:var(--surface1,#111);border-radius:0 0 16px 16px;
    overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,0.5);
    border:1px solid rgba(255,255,255,0.06);border-top:none;
  `;
  const searchBar = document.getElementById('search-input')?.parentElement;
  if (searchBar) {
    searchBar.style.position = 'relative';
    searchBar.appendChild(d);
  }
  return d;
}

function _hideSuggestDropdown() {
  const d = document.getElementById('search-suggest-drop');
  if (d) d.remove();
}

async function _fetchSuggestions(q) {
  if (q === _lastSuggestQ) return;
  _lastSuggestQ = q;
  try {
    const r = await fetch(`/api/suggest?q=${encodeURIComponent(q)}`);
    const d = await r.json();
    _renderSuggestions(d.suggestions || [], q);
  } catch(e) { _hideSuggestDropdown(); }
}

function _renderSuggestions(suggestions, q) {
  if (!suggestions.length) { _hideSuggestDropdown(); return; }
  const drop = _createSuggestDropdown();
  drop.innerHTML = '';
  for (const s of suggestions) {
    const item = document.createElement('div');
    item.style.cssText = `
      display:flex;align-items:center;gap:10px;padding:10px 14px;
      cursor:pointer;transition:background 0.15s;
    `;
    item.onmouseenter = () => item.style.background = 'rgba(255,255,255,0.06)';
    item.onmouseleave = () => item.style.background = '';
    // [FIX-SUGGEST-BLUR] Prevent blur firing before click on desktop
    item.addEventListener('mousedown', (e) => e.preventDefault(), { passive: false });
    const img = document.createElement('img');
    img.src = s.artworkUrl || IMG_PLACEHOLDER;
    img.style.cssText = 'width:36px;height:36px;border-radius:6px;object-fit:cover;flex-shrink:0;';
    img.onerror = () => { img.src = IMG_PLACEHOLDER; };
    const info = document.createElement('div');
    info.style.cssText = 'flex:1;min-width:0;';
    info.innerHTML = `
      <div style="font-size:13px;font-weight:500;color:var(--text1,#fff);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(s.trackName)}</div>
      <div style="font-size:11px;color:var(--text3,#888);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(s.artistName)}</div>
    `;
    const icon = document.createElement('div');
    icon.innerHTML = `<svg viewBox="0 0 24 24" style="width:14px;height:14px;stroke:var(--text3,#888);fill:none;stroke-width:2;stroke-linecap:round;"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>`;
    item.appendChild(img); item.appendChild(info); item.appendChild(icon);
    item.addEventListener('touchend', (e) => {
      e.preventDefault(); // [FIX-SUGGEST-TOUCH] Click delay bypass on mobile
      clearTimeout(_searchTimeout); // [FIX-SUGGEST-DOUBLE] doSearch cancel karo
      clearTimeout(_suggestTimeout);
      const input = document.getElementById('search-input');
      if (input) input.value = s.trackName + ' ' + s.artistName;
      _hideSuggestDropdown();
      saveRecentSearch(s.trackName);
      doSearch(s.trackName + ' ' + s.artistName);
    }, { passive: false });
    item.addEventListener('click', () => {
      clearTimeout(_searchTimeout); // [FIX-SUGGEST-DOUBLE] doSearch cancel karo
      clearTimeout(_suggestTimeout);
      const input = document.getElementById('search-input');
      if (input) input.value = s.trackName + ' ' + s.artistName;
      _hideSuggestDropdown();
      saveRecentSearch(s.trackName);
      doSearch(s.trackName + ' ' + s.artistName);
    });
    drop.appendChild(item);
  }
}

const searchInput = document.getElementById('search-input');
if (searchInput) {
  searchInput.addEventListener('focus', function() {
    if (!this.value.trim()) renderSearchIdle();
  });
  searchInput.addEventListener('input', function() {
    const v = this.value.trim();
    const clearBtn = document.getElementById('search-clear');
    if (clearBtn) clearBtn.style.display = v ? 'flex' : 'none';
    clearTimeout(_searchTimeout);
    clearTimeout(_suggestTimeout);
    if (!v) { renderSearchIdle(); _hideSuggestDropdown(); return; }
    _suggestTimeout = setTimeout(() => _fetchSuggestions(v), 200);
    showSearchSkeleton();
    _searchTimeout = setTimeout(() => {
      _hideSuggestDropdown();
      doSearch(v);
    }, 500);
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#search-suggest-drop') && !e.target.closest('#search-input')) {
      _hideSuggestDropdown();
    }
  }, { passive: true });
}

function clearSearch() {
  const input = document.getElementById('search-input');
  const clearBtn = document.getElementById('search-clear');
  if (input) input.value = '';
  if (clearBtn) clearBtn.style.display = 'none';
  clearTimeout(_searchTimeout); renderSearchIdle();
}

function _saveSearchToStorage(searches) {
  try { localStorage.setItem('aurum_recent', JSON.stringify(searches)); } catch(e) {}
}

function renderSearchIdle() {
  const body = document.getElementById('search-body'); if (!body) return;
  let html = '';
  if (recentSearches.length) {
    html += `<div class="recent-section"><div class="recent-head"><h4>Recent</h4><button onclick="clearAllRecent()">Clear</button></div><div class="recent-chips-wrap">`;
    for (let i = 0; i < recentSearches.length; i++) {
      html += `<div class="recent-chip" onclick="tapRecentSearch('${esc(recentSearches[i])}')">${esc(recentSearches[i])}<button class="rc-rm" onclick="removeRecent(event,${i})"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>`;
    }
    html += `</div></div>`;
  }
  html += `<div class="browse-section"><div class="browse-label">Browse</div><div class="browse-grid">`;
  for (const c of browseCategories) {
    html += `<div class="browse-card ${c.cls}" onclick="browseGenre('${c.genre}')"${isTV ? ' tabindex="0"' : ''}><div class="browse-card-label">${c.label}</div><div class="browse-card-sub">${c.sub}</div></div>`;
  }
  html += `</div></div>`;
  body.innerHTML = html;
}

function browseGenre(genre) {
  const q = genreMap[genre] || extraGenreMap[genre] || genre;
  const input = document.getElementById('search-input');
  const clearBtn = document.getElementById('search-clear');
  if (input) input.value = q;
  if (clearBtn) clearBtn.style.display = 'flex';
  saveRecentSearch(q); doSearch(q);
}

function tapRecentSearch(q) {
  const input = document.getElementById('search-input');
  const clearBtn = document.getElementById('search-clear');
  if (input) input.value = q;
  if (clearBtn) clearBtn.style.display = 'flex';
  doSearch(q);
}

function saveRecentSearch(q) {
  recentSearches = recentSearches.filter(r => r.toLowerCase() !== q.toLowerCase());
  recentSearches.unshift(q);
  if (recentSearches.length > 6) recentSearches = recentSearches.slice(0, 6);
  _saveSearchToStorage(recentSearches);
}

function removeRecent(e, i) { e.stopPropagation(); recentSearches.splice(i, 1); _saveSearchToStorage(recentSearches); renderSearchIdle(); }
function clearAllRecent() { recentSearches = []; _saveSearchToStorage(recentSearches); renderSearchIdle(); }

function showSearchSkeleton() {
  const body = document.getElementById('search-body'); if (!body) return;
  let html = '<div style="padding:4px 0">';
  for (let i = 0; i < 5; i++) html += `<div class="sk-row"><div class="sk-art"></div><div class="sk-info"><div class="sk-line l1"></div><div class="sk-line l2"></div></div></div>`;
  html += '</div>';
  body.innerHTML = html;
}

async function doSearch(q) {
  showSearchSkeleton();
  try {
    const r = await fetch(`/api/songs?q=${encodeURIComponent(q)}`);
    const d = await r.json();
    saveRecentSearch(q);
    let songs = (d.results || []).filter(s => s.previewUrl || s._source === 'saavn');
    if (!_userWantsVersion(q, '')) {
      songs = songs.filter(s => !_isVersionSong(s.trackName || ''));
    }
    renderSearchResults(songs, q);
  } catch(e) {
    const body = document.getElementById('search-body');
    if (body) body.innerHTML = `<div class="search-placeholder"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg><h3>Something went wrong</h3><p>Check your connection and try again</p></div>`;
  }
}

function renderSearchResults(songs, q) {
  const body = document.getElementById('search-body'); if (!body) return;
  if (!songs.length) {
    body.innerHTML = `<div class="search-placeholder"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg><h3>Nothing found</h3><p>Try a different name or artist</p></div>`;
    return;
  }
  body.innerHTML = `<div style="font-size:11px;color:var(--text3);padding:0 24px 10px;font-weight:500;">${songs.length} results for "${esc(q)}"</div><div id="search-results-list"></div>`;
  const list = document.getElementById('search-results-list');
  if (list) { for (let i = 0; i < songs.length; i++) list.appendChild(makeSongRow(songs[i], i, songs)); }
}

// ─── 30. NAVIGATION ──────────────────────────────────────────────────────────
function goPage(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  const page = document.getElementById('page-' + name);
  if (page) page.classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'library') renderLibrary();
  if (name === 'search')  renderSearchIdle();
}

// ─── 31. SAVE / LIBRARY ──────────────────────────────────────────────────────
function isSaved(song) { return savedSongs.some(s => String(s.trackId) === String(song.trackId)); }
function toggleSaveCurrentTrack() { if (!currentTrack) return; toggleSave(currentTrack); updateSaveBtn(); }
function toggleSave(song) {
  if (isSaved(song)) { savedSongs = savedSongs.filter(s => String(s.trackId) !== String(song.trackId)); showToast('Removed from library'); }
  else { savedSongs.push(song); showToast('Saved to library'); }
  try { localStorage.setItem('aurum_saved', JSON.stringify(savedSongs)); } catch(e) {}
  renderLibrary(); updateSaveBtn();
}

function playLikedSongs() {
  if (!savedSongs.length) { showToast('Save some songs first'); return; }
  playSongs(savedSongs, 0); openFullscreen();
}

function switchLibTab(tab, el) {
  document.querySelectorAll('.lib-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active'); currentLibTab = tab;
  const playlistsDiv = document.getElementById('lib-playlists');
  const savedDiv = document.getElementById('lib-saved');
  const downloadsDiv = document.getElementById('lib-downloads');
  if (playlistsDiv) playlistsDiv.style.display = tab === 'playlists' ? '' : 'none';
  if (savedDiv) savedDiv.style.display = tab === 'saved' ? '' : 'none';
  if (downloadsDiv) downloadsDiv.style.display = tab === 'downloads' ? '' : 'none';
  haptic(8); renderLibrary();
}

function renderLibrary() {
  renderPlaylists(); renderSavedSongs(); renderDownloadedSongs();
  const lc = document.getElementById('liked-count');
  if (lc) lc.textContent = savedSongs.length ? savedSongs.length + ' song' + (savedSongs.length !== 1 ? 's' : '') : 'Nothing saved yet';
  const st = document.getElementById('saved-tab');
  if (st) st.textContent = 'Liked' + (savedSongs.length ? ` (${savedSongs.length})` : '');
  const dlMeta = (() => { try { return JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]'); } catch(e) { return []; } })();
  const dt = document.getElementById('dl-tab');
  if (dt) dt.textContent = 'Downloads' + (dlMeta.length ? ` (${dlMeta.length})` : '');
}

function renderPlaylists() {
  const grid = document.getElementById('playlist-grid'); if (!grid) return;
  grid.innerHTML = '';
  if (!playlists.length) return;
  for (let i = 0; i < playlists.length; i++) {
    const pl = playlists[i];
    const card = document.createElement('div'); card.className = 'playlist-card';
    if (isTV) card.tabIndex = 0;
    card.onclick = () => openPlaylistDetail(i);
    const songs = pl.songs || [];
    const coverWrap = document.createElement('div'); coverWrap.className = 'playlist-card-cover';
    if (songs.length >= 4) {
      const grid4 = document.createElement('div'); grid4.className = 'playlist-card-grid';
      for (let j = 0; j < 4; j++) { const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy'; setImgSrc(img, getArtUrl(songs[j], '300x300')); grid4.appendChild(img); }
      coverWrap.appendChild(grid4);
    } else if (songs.length > 0) {
      const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
      setImgSrc(img, getArtUrl(songs[0], '300x300'));
      img.style.cssText = 'width:100%;height:100%;object-fit:cover;border-radius:12px;';
      coverWrap.appendChild(img);
    } else {
      coverWrap.innerHTML = '<svg viewBox="0 0 24 24" style="width:28px;height:28px;stroke:var(--text3);fill:none;stroke-width:1.4;stroke-linecap:round;"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
    }
    card.appendChild(coverWrap);
    const info = document.createElement('div'); info.className = 'playlist-card-info';
    info.innerHTML = `<div class="playlist-card-name">${esc(pl.name)}</div><div class="playlist-card-count">${songs.length} song${songs.length !== 1 ? 's' : ''}</div>`;
    card.appendChild(info);
    const opts = document.createElement('button'); opts.className = 'playlist-card-opts';
    opts.innerHTML = '<svg viewBox="0 0 24 24" style="width:14px;height:14px;fill:currentColor;"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg>';
    opts.onclick = e => openPlaylistOpts(e, i);
    card.appendChild(opts); grid.appendChild(card);
  }
}

function renderSavedSongs() {
  const list = document.getElementById('saved-songs-list'); if (!list) return;
  list.innerHTML = '';
  if (!savedSongs.length) {
    list.innerHTML = `<div class="empty-library"><svg viewBox="0 0 24 24"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg><h3>No liked songs yet</h3><p>Tap the heart on any song to save it here</p></div>`;
    return;
  }
  for (let i = 0; i < savedSongs.length; i++) list.appendChild(makeSongRow(savedSongs[i], i, savedSongs));
}

// ─── 32. DOWNLOADS ───────────────────────────────────────────────────────────
const DL_DB_NAME = 'aurum_downloads'; const DL_DB_VER = 1; let _dlDb = null;

function openDlDb() {
  if (_dlDb) return Promise.resolve(_dlDb);
  return new Promise((res, rej) => {
    const req = indexedDB.open(DL_DB_NAME, DL_DB_VER);
    req.onupgradeneeded = e => { const db = e.target.result; if (!db.objectStoreNames.contains('songs')) db.createObjectStore('songs', { keyPath:'trackId' }); };
    req.onsuccess = e => { _dlDb = e.target.result; res(_dlDb); };
    req.onerror   = () => rej(req.error);
  });
}

async function saveToDb(song, blob) {
  const db = await openDlDb();
  return new Promise((res, rej) => {
    const tx = db.transaction('songs', 'readwrite');
    tx.objectStore('songs').put({ ...song, _blob: blob, _savedAt: Date.now() });
    tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error);
  });
}

async function deleteFromDb(trackId) {
  const db = await openDlDb();
  return new Promise((res, rej) => {
    const tx = db.transaction('songs', 'readwrite');
    tx.objectStore('songs').delete(trackId);
    tx.oncomplete = () => res(); tx.onerror = () => rej(tx.error);
  });
}

async function requestPersistentStorage() {
  if (!navigator.storage?.persist) return;
  const already = await navigator.storage.persisted();
  if (!already) await navigator.storage.persist();
}

async function _warnIfStorageNotPersisted() {
  if (!navigator.storage?.persist) return;
  const already = await navigator.storage.persisted();
  if (already) return;
  const granted = await navigator.storage.persist();
  if (!granted) showToast('Tip: Add to Home Screen for permanent downloads');
}

window.addEventListener('beforeunload', () => {
  if (_lastObjectUrl) { try { URL.revokeObjectURL(_lastObjectUrl); } catch(e) {} }
});

async function downloadSongOffline(song, customUrl, customQuality) {
  const rawUrl = customUrl || (_currentSaavnUrl && currentQuality === 'full' ? _currentSaavnUrl : null) || song.previewUrl;
  const quality = customQuality || _currentSaavnQuality || 'preview';
  await _warnIfStorageNotPersisted();
  showToast('Saving to app…');
  try {
    let blob = null;
    const urls = [rawUrl, song.previewUrl].filter(Boolean);
    for (const url of urls) {
      try { const r = await fetch(url); if (r.ok) { blob = await r.blob(); break; } } catch(e) { continue; }
    }
    if (!blob) throw new Error('All URLs failed');
    await saveToDb({ ...song, _quality: quality }, blob);
    const metas = (() => { try { return JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]'); } catch(e) { return []; } })();
    const filtered = metas.filter(s => String(s.trackId) !== String(song.trackId));
    filtered.unshift({ trackId:song.trackId, trackName:song.trackName, artistName:song.artistName, artworkUrl100:song.artworkUrl100, _quality:quality, _savedAt:Date.now() });
    try { localStorage.setItem('aurum_dl_meta', JSON.stringify(filtered)); } catch(e) {}
    haptic([20, 50, 20]); showToast('Saved to app ✓'); renderLibrary();
  } catch(e) { showToast('Save failed — check connection'); console.error('[Offline Save]', e); }
}

async function playDownloadedSong(trackId) {
  try {
    const db  = await openDlDb();
    const key = isNaN(Number(trackId)) ? trackId : Number(trackId);
    const tx  = db.transaction('songs', 'readonly');
    const req = tx.objectStore('songs').get(key);
    req.onsuccess = () => {
      const rec = req.result;
      if (!rec || !rec._blob) { showToast('File missing — re-download'); return; }
      const newUrl = URL.createObjectURL(rec._blob);
      audio.pause(); audio.src = newUrl; audio.load();
      audio.play().then(() => {
        if (_lastObjectUrl && _lastObjectUrl !== newUrl) { try { URL.revokeObjectURL(_lastObjectUrl); } catch(e) {} }
        _lastObjectUrl = newUrl;
        isPlaying = true; currentTrack = rec; currentQuality = 'full';
        _dismissedTrackId = null; updatePlayerUI(); showMiniPlayer();
      }).catch(() => { try { URL.revokeObjectURL(newUrl); } catch(e) {} });
    };
  } catch(e) { showToast('Cannot play'); }
}

async function deleteDownload(trackId) {
  await deleteFromDb(trackId);
  const metas = (() => { try { return JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]'); } catch(e) { return []; } })();
  const filtered = metas.filter(s => String(s.trackId) !== String(trackId));
  try { localStorage.setItem('aurum_dl_meta', JSON.stringify(filtered)); } catch(e) {}
  haptic(15); showToast('Removed'); renderLibrary();
}

function renderDownloadedSongs() {
  const list = document.getElementById('downloaded-songs-list'); if (!list) return;
  list.innerHTML = '';
  const songs = (() => { try { return JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]'); } catch(e) { return []; } })();
  if (!songs.length) {
    list.innerHTML = `<div class="empty-library"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><h3>No downloads yet</h3><p>Save songs offline from the player or song menu</p></div>`;
    return;
  }
  const hdr = document.createElement('div');
  hdr.style.cssText = 'padding:4px 22px 10px;display:flex;align-items:center;justify-content:space-between;';
  hdr.innerHTML = `<span style="font-size:11px;color:var(--text3);font-weight:600;">${songs.length} song${songs.length!==1?'s':''} saved offline</span><button style="font-size:11px;color:var(--text3);background:none;border:none;cursor:pointer;font-family:Sora,sans-serif;" onclick="confirmClearDownloads()">Clear all</button>`;
  list.appendChild(hdr);
  for (const s of songs) {
    const row = document.createElement('div'); row.className = 'song-row anim-in'; row.dataset.trackId = String(s.trackId);
    if (isTV) row.tabIndex = 0;
    const img = document.createElement('img'); img.alt=''; img.loading='lazy'; setImgSrc(img, getArtUrl(s, '300x300')); row.appendChild(img);
    const info = document.createElement('div'); info.className = 'song-row-info';
    const qLabel = s._quality && s._quality.includes('320') ? '320K' : s._quality && s._quality.includes('160') ? '160K' : 'OFFLINE';
    info.innerHTML = `<div class="song-row-title">${esc(s.trackName)}</div><div class="song-row-artist"><span style="color:var(--gold);font-size:8px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;margin-right:5px;">${qLabel}</span>${esc(s.artistName)}</div>`;
    row.appendChild(info);
    const right = document.createElement('div'); right.className = 'song-row-right';
    const delBtn = document.createElement('button'); delBtn.className = 'song-row-more';
    delBtn.innerHTML = '<svg viewBox="0 0 24 24" style="stroke:var(--text3);width:15px;height:15px;fill:none;stroke-width:1.8;stroke-linecap:round;"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
    delBtn.onclick = e => { e.stopPropagation(); deleteDownload(s.trackId); };
    right.appendChild(delBtn); row.appendChild(right);
    row.onclick = () => playDownloadedSong(s.trackId);
    list.appendChild(row);
  }
}

function confirmClearDownloads() {
  if (!confirm('Remove all downloaded songs?')) return;
  openDlDb().then(db => {
    const tx = db.transaction('songs', 'readwrite');
    tx.objectStore('songs').clear();
    tx.oncomplete = () => {
      try { localStorage.removeItem('aurum_dl_meta'); } catch(e) {}
      renderLibrary(); showToast('Downloads cleared');
    };
  });
}

// ─── 33. PLAYLIST DETAIL ─────────────────────────────────────────────────────
function openPlaylistDetail(i) {
  currentPlaylistIndex = i;
  const pl = playlists[i], songs = pl.songs || [];
  const nameEl = document.getElementById('pl-detail-name');
  const titleEl = document.getElementById('pl-detail-title');
  const subEl = document.getElementById('pl-detail-sub');
  if (nameEl) nameEl.textContent = pl.name;
  if (titleEl) titleEl.textContent = pl.name;
  if (subEl) subEl.textContent = songs.length + ' songs';
  const coverEl = document.getElementById('pl-big-cover');
  if (!songs.length) {
    const emptyDiv = document.createElement('div'); emptyDiv.id = 'pl-big-cover';
    emptyDiv.style.cssText = 'width:100%;max-width:248px;aspect-ratio:1;border-radius:20px;background:var(--surface2);display:flex;align-items:center;justify-content:center;';
    if (coverEl) coverEl.replaceWith(emptyDiv);
  } else if (songs.length < 4) {
    const img = document.createElement('img'); img.id = 'pl-big-cover'; img.className = 'pl-big-cover'; img.alt = '';
    setImgSrc(img, getArtUrl(songs[0], '500x500'));
    if (coverEl) coverEl.replaceWith(img);
  } else {
    const g = document.createElement('div'); g.id = 'pl-big-cover'; g.className = 'pl-big-cover-grid';
    for (let j = 0; j < 4; j++) { const img = document.createElement('img'); img.alt=''; img.loading='lazy'; setImgSrc(img, getArtUrl(songs[j], '300x300')); g.appendChild(img); }
    if (coverEl) coverEl.replaceWith(g);
  }
  const sl = document.getElementById('pl-songs-list');
  if (sl) {
    sl.innerHTML = '';
    if (!songs.length) sl.innerHTML = `<div style="text-align:center;padding:38px 22px;color:var(--text3);font-size:12px;">No songs yet — find some in Search</div>`;
    else for (let j = 0; j < songs.length; j++) sl.appendChild(makeSongRow(songs[j], j, songs));
  }
  const detailDiv = document.getElementById('playlist-detail');
  if (detailDiv) detailDiv.classList.add('open');
}

function closePlaylistDetail() {
  const detailDiv = document.getElementById('playlist-detail');
  if (detailDiv) detailDiv.classList.remove('open');
  renderLibrary();
}

function playPlaylist() {
  if (currentPlaylistIndex === null) return;
  const songs = playlists[currentPlaylistIndex].songs || [];
  if (!songs.length) { showToast('No songs in playlist'); return; }
  playSongs(songs, 0); closePlaylistDetail(); openFullscreen();
}

function shufflePlaylist() {
  if (currentPlaylistIndex === null) return;
  const songs = playlists[currentPlaylistIndex].songs || [];
  if (!songs.length) { showToast('No songs in playlist'); return; }
  playSongs(songs, Math.floor(Math.random() * songs.length));
  shuffleOn = true;
  const sb = document.getElementById('shuffle-btn');
  if (sb) sb.querySelector('svg').style.stroke = 'var(--gold-l)';
  closePlaylistDetail(); openFullscreen();
}

function openPlaylistOpts(e, i) {
  e.stopPropagation(); optsPlaylistIndex = i;
  const titleEl = document.getElementById('pl-opts-title');
  if (titleEl) titleEl.textContent = playlists[i]?.name || 'Playlist';
  const modal = document.getElementById('playlist-opts-modal');
  if (modal) { modal.style.display = ''; modal.classList.add('open'); }
}

function closePlaylistOpts(e) {
  if (e && e.target && e.target.closest && e.target.closest('.modal-sheet')) return;
  const modal = document.getElementById('playlist-opts-modal');
  if (modal) { modal.classList.remove('open'); modal.style.display = 'none'; }
}

function openRenameModal() {
  const idx = optsPlaylistIndex; closePlaylistOpts();
  if (idx === null || idx === undefined) return;
  const inputEl = document.getElementById('rename-input');
  if (inputEl) inputEl.value = playlists[idx]?.name || '';
  optsPlaylistIndex = idx;
  const modal = document.getElementById('rename-modal');
  if (modal) { modal.style.display = ''; modal.classList.add('open'); }
  setTimeout(() => { const inp = document.getElementById('rename-input'); if (inp) inp.focus(); }, 360);
}

function closeRenameModal() {
  const modal = document.getElementById('rename-modal');
  if (modal) { modal.classList.remove('open'); modal.style.display = 'none'; }
}

function confirmRename() {
  const name = document.getElementById('rename-input')?.value.trim();
  if (!name || optsPlaylistIndex === null) { showToast('Enter a name'); return; }
  playlists[optsPlaylistIndex].name = name;
  try { localStorage.setItem('aurum_playlists', JSON.stringify(playlists)); } catch(e) {}
  closeRenameModal(); optsPlaylistIndex = null; renderPlaylists(); showToast('Renamed');
}

function confirmDeletePlaylist() {
  if (optsPlaylistIndex === null) return;
  const name = playlists[optsPlaylistIndex].name;
  playlists.splice(optsPlaylistIndex, 1);
  try { localStorage.setItem('aurum_playlists', JSON.stringify(playlists)); } catch(e) {}
  optsPlaylistIndex = null; closePlaylistOpts(); renderPlaylists(); showToast(`"${name}" deleted`);
}

function openCreatePlaylist() {
  const modal = document.getElementById('create-playlist-modal');
  if (modal) { modal.style.display = ''; modal.classList.add('open'); }
  setTimeout(() => { const inp = document.getElementById('playlist-name-input'); if (inp) inp.focus(); }, 360);
}

function closeCreatePlaylist() {
  const modal = document.getElementById('create-playlist-modal');
  if (modal) { modal.classList.remove('open'); modal.style.display = 'none'; }
  const inp = document.getElementById('playlist-name-input'); if (inp) inp.value = '';
}

function createPlaylist() {
  const name = document.getElementById('playlist-name-input')?.value.trim();
  if (!name) { showToast('Enter a playlist name'); return; }
  playlists.push({ name, songs: [] });
  try { localStorage.setItem('aurum_playlists', JSON.stringify(playlists)); } catch(e) {}
  closeCreatePlaylist(); renderPlaylists(); showToast('"' + name + '" created');
}

function openSongModal(song) {
  if (!song) return;
  modalTrack = song;
  const artEl = document.getElementById('modal-song-art');
  if (artEl) setImgSrc(artEl, getArtUrl(song, '300x300'));
  const titleEl = document.getElementById('modal-song-title');
  const artistEl = document.getElementById('modal-song-artist');
  const saveLabel = document.getElementById('modal-save-label');
  if (titleEl) titleEl.textContent = song.trackName || 'Unknown';
  if (artistEl) artistEl.textContent = song.artistName || 'Unknown';
  if (saveLabel) saveLabel.textContent = isSaved(song) ? 'Remove from Library' : 'Save to Library';
  const modal = document.getElementById('song-modal');
  if (modal) modal.classList.add('open');
}

function closeSongModal(e) {
  if (e && e.target && e.target.closest && e.target.closest('.modal-sheet')) return;
  const modal = document.getElementById('song-modal');
  if (modal) modal.classList.remove('open');
  modalTrack = null;
}

function modalSave() {
  if (!modalTrack) return;
  toggleSave(modalTrack);
  const saveLabel = document.getElementById('modal-save-label');
  if (saveLabel) saveLabel.textContent = isSaved(modalTrack) ? 'Remove from Library' : 'Save to Library';
  const modal = document.getElementById('song-modal');
  if (modal) modal.classList.remove('open');
  modalTrack = null;
}

function playNext() {
  if (!modalTrack) return;
  currentQueue.splice(currentIndex + 1, 0, modalTrack);
  showToast('Playing next');
  const modal = document.getElementById('song-modal');
  if (modal) modal.classList.remove('open');
  modalTrack = null; updateQueuePanel();
}

function modalDownload() {
  if (!modalTrack) return;
  const s = modalTrack;
  const modal = document.getElementById('song-modal');
  if (modal) modal.classList.remove('open');
  _downloadSong = s; modalTrack = null; openDownloadModal();
}

function openAddToPlaylistModal() {
  const songToAdd = modalTrack;
  const modal = document.getElementById('song-modal');
  if (modal) modal.classList.remove('open');
  const opts = document.getElementById('add-playlist-options'); if (!opts) return;
  opts.innerHTML = '';
  if (!playlists.length) {
    opts.innerHTML = `<div style="padding:12px 0;text-align:center;color:var(--text3);font-size:12px;">No playlists yet.</div>`;
  } else {
    for (let i = 0; i < playlists.length; i++) {
      const pl = playlists[i];
      const div = document.createElement('div'); div.className = 'modal-option';
      div.innerHTML = `<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg><span>${esc(pl.name)} <span style="color:var(--text3);font-size:10px;">(${pl.songs.length})</span></span>`;
      div.onclick = () => addToPlaylist(i, songToAdd); opts.appendChild(div);
    }
  }
  const addModal = document.getElementById('add-playlist-modal');
  if (addModal) addModal.classList.add('open');
}

function closeAddToPlaylistModal(e) {
  if (e && e.target && e.target.closest && e.target.closest('.modal-sheet')) return;
  const modal = document.getElementById('add-playlist-modal');
  if (modal) modal.classList.remove('open');
}

function addToPlaylist(i, song) {
  const s = song || modalTrack; if (!s) return;
  const pl = playlists[i];
  if (pl.songs.some(x => String(x.trackId) === String(s.trackId))) { showToast('Already in "' + pl.name + '"'); }
  else { pl.songs.push(s); try { localStorage.setItem('aurum_playlists', JSON.stringify(playlists)); } catch(e) {} showToast('Added to "' + pl.name + '"'); }
  const modal = document.getElementById('add-playlist-modal');
  if (modal) modal.classList.remove('open');
  modalTrack = null;
}

function openQualitySheet() {
  if (!currentTrack) { showToast('Play a song first'); return; }
  const sub = document.getElementById('qs-track-name');
  if (sub) sub.textContent = `${currentTrack.trackName || 'Unknown'} · ${currentTrack.artistName || 'Unknown'}`;
  updateQualityLabel();
  const modal = document.getElementById('quality-modal');
  if (modal) { modal.style.display = ''; modal.classList.add('open'); }
}

function closeQualitySheet(e) {
  if (e && e.target && e.target.closest && e.target.closest('.quality-sheet')) return;
  const modal = document.getElementById('quality-modal');
  if (modal) { modal.classList.remove('open'); modal.style.display = 'none'; }
}

function selectQuality(q) {
  if (q === 'preview') { _fallbackToPreview(currentTrack); closeQualitySheet(); }
  else { if (_fullSongAbort) { _fullSongAbort.abort(); _fullSongAbort = null; } _autoFetchFullSong(currentTrack); closeQualitySheet(); }
}

// ─── 34. DOWNLOAD MODAL ──────────────────────────────────────────────────────
function openDownloadModal() {
  const song = _downloadSong || currentTrack;
  if (!song) { showToast('Play a song first'); return; }
  _downloadSong = song;
  const sub = document.getElementById('dl-track-name');
  if (sub) sub.textContent = `${song.trackName || 'Unknown'} · ${song.artistName || 'Unknown'}`;
  if (_currentSaavnQuality) { _updateDlSheetQuality(_currentSaavnQuality); }
  else {
    const desc  = document.getElementById('dl-full-desc');
    const badge = document.getElementById('dl-full-badge');
    if (desc) desc.textContent = currentQuality === 'loading' ? 'Fetching stream…' : 'Play song first';
    if (badge) { badge.textContent = '—'; badge.className = 'dl-kbps-badge b128'; }
  }
  const modal = document.getElementById('download-modal');
  if (modal) { modal.style.display = ''; modal.classList.add('open'); }
}

function closeDownloadModal(e) {
  if (e && e.target && e.target.closest && e.target.closest('.dl-sheet')) return;
  const modal = document.getElementById('download-modal');
  if (modal) { modal.classList.remove('open'); modal.style.display = 'none'; }
  _downloadSong = null;
}

async function triggerDownload(quality) {
  try {
    if (quality === 'ringtone' && !window.validateFeature?.('ringtone')) return;
    if ((quality === 'full' || quality === 'gift') && !window.validateFeature?.('download')) return;
    const song = _downloadSong || currentTrack;
    _downloadSong = null;
    if (!song) { showToast('No track selected'); return; }
    const dlModal = document.getElementById('download-modal');
    if (dlModal) { dlModal.classList.remove('open'); dlModal.style.display = 'none'; }
    const cleanTitle  = (song.trackName  || 'audio').replace(/[/\?%*:|"<>]/g, '-');
    const cleanArtist = (song.artistName || '').replace(/[/\?%*:|"<>]/g, '-');
    if (quality === 'preview') {
      try {
        showToast('Downloading preview…');
        const res = await fetch(song.previewUrl);
        if (!res.ok) throw new Error('fetch failed');
        const blob = await res.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = objUrl; a.download = `${cleanTitle}_preview.m4a`;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(objUrl), 5000);
        haptic([10, 30, 10]); showToast('Preview saved ✓');
      } catch(e) { showToast('Download failed'); }
      return;
    }
    if (quality === 'ringtone') {
      try {
        showToast('Saving ringtone…');
        const res = await fetch(song.previewUrl);
        if (!res.ok) throw new Error('fetch failed');
        const blob = await res.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = objUrl; a.download = `${cleanTitle}_ringtone.m4a`;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(objUrl), 5000);
        haptic([10, 30, 10]); showToast('Ringtone saved ✓');
      } catch(e) { showToast('Download failed'); }
      return;
    }
    if (quality === 'full') {
      await downloadSongOffline(song, _currentSaavnUrl, _currentSaavnQuality);
      try {
        const q      = encodeURIComponent(song.trackName  || '');
        const artist = encodeURIComponent(song.artistName || '');
        const dlUrl  = `/api/download?q=${q}&artist=${artist}&quality=full`;
        const a = document.createElement('a'); a.href = dlUrl; a.download = `${cleanTitle} - ${cleanArtist}.mp3`;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        haptic([10, 30, 10]); showToast('Saving to app & downloading…');
      } catch(e) { showToast('Saved to app ✓'); }
      return;
    }
    if (quality === 'gift') {
      showToast('Fetching 320 kbps…');
      try {
        const rawTitle  = (song.trackName  || '').replace(/\(.*?\)/g, '').trim();
        const rawArtist = (song.artistName || '').split(/[&,]/)[0].trim();
        const q      = encodeURIComponent(rawTitle);
        const artist = encodeURIComponent(rawArtist);
        const dlUrl  = `/api/download?q=${q}&artist=${artist}&quality=gift`;
        const a = document.createElement('a'); a.href = dlUrl; a.download = `${cleanTitle} - ${cleanArtist}_320.mp3`;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        haptic([10, 30, 10]); showToast('320 kbps download started ✓');
      } catch(e) { showToast('Owner Gift failed'); }
      return;
    }
  } catch(outerErr) {
    console.error('[triggerDownload] Unhandled:', outerErr);
    showToast('Download error — please retry');
  }
}

// ─── 35. LYRICS ──────────────────────────────────────────────────────────────
async function fetchLyrics(song) {
  const wrap      = document.getElementById('fp-lyrics-wrap');
  const el        = document.getElementById('fp-lyrics');
  const lyricsBtn = document.getElementById('fp-lyrics-toggle');
  if (!wrap || !el) return;
  wrap.style.display = 'none'; wrap.dataset.lyricsOpen = '0';
  el.textContent = ''; lyricsViewActive = false;
  if (lyricsBtn) { lyricsBtn.style.display = 'none'; lyricsBtn.classList.remove('active'); }
  try {
    const artist = encodeURIComponent((song.artistName || '').split(/[&,]/)[0].trim());
    const title  = encodeURIComponent(song.trackName || '');
    const r = await fetch(`https://api.lyrics.ovh/v1/${artist}/${title}`);
    if (!r.ok) throw new Error('not found');
    const d = await r.json();
    if (!d.lyrics || !d.lyrics.trim()) throw new Error('empty');
    el.textContent = d.lyrics.trim();
    if (lyricsBtn) lyricsBtn.style.display = 'flex';
  } catch(e) { if (lyricsBtn) lyricsBtn.style.display = 'none'; }
}

// ─── 36. BACKGROUND AUDIO KEEP-ALIVE ─────────────────────────────────────────
let _wakeLock = null;
let _bgPingInterval = null;
let _bgAudioCtx = null;

async function _acquireWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try { _wakeLock = await navigator.wakeLock.request('screen'); _wakeLock.addEventListener('release', () => { _wakeLock = null; }); } catch(e) {}
}

function _releaseWakeLock() {
  if (_wakeLock) { _wakeLock.release().catch(()=>{}); _wakeLock = null; }
}

// ── [PATCH-3] Background audio: production-grade keep-alive ──────────────────
// Strategy:
//   1. Connect real audio element into AudioContext graph (primary mechanism)
//   2. Schedule silent buffer nodes every 20s (prevents browser GC of context)
//   3. Stall watchdog: polls every 8s for unexpected pause in background
//   4. Visibility restore forces correct resume order (context → audio)
let _bgAudioSource   = null;
let _bgKeepaliveTick = null;

function _setupBgAudioPing() {
  try {
    _bgAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    window._bgAudioCtx = _bgAudioCtx;
  } catch (e) { return; }

  // Step 1: Connect real audio element into context graph.
  // createMediaElementSource can only be called once per element — guard it.
  if (!_bgAudioSource) {
    try {
      _bgAudioSource = _bgAudioCtx.createMediaElementSource(audio);
      _bgAudioSource.connect(_bgAudioCtx.destination);
    } catch (e) {
      _bgAudioSource = null;
      // If createMediaElementSource fails (already connected elsewhere),
      // fall through — silent ping below will still keep context alive
    }
  }

  // Step 2: Periodic silent buffer scheduling every 20s.
  // Signals to browser's background process manager that audio pipeline is active.
  function _scheduleSilentPing() {
    if (!isPlaying) return;
    try {
      if (_bgAudioCtx.state === 'suspended') {
        _bgAudioCtx.resume().catch(() => {});
        return;
      }
      const buf = _bgAudioCtx.createBuffer(1, _bgAudioCtx.sampleRate * 0.1, _bgAudioCtx.sampleRate);
      // Buffer is zero-initialized by spec — truly silent
      const src = _bgAudioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(_bgAudioCtx.destination);
      src.start(0);
    } catch (e) { /* context may be closed — ignore */ }
  }

  if (_bgPingInterval) clearInterval(_bgPingInterval);
  _bgPingInterval = setInterval(_scheduleSilentPing, 20000);
  window._bgPingInterval = _bgPingInterval;

  // Step 3: Stall watchdog — polls every 8s.
  // PWABuilder sometimes suspends audio without firing 'pause'.
  // If isPlaying=true but audio.paused, attempt recovery.
  if (_bgKeepaliveTick) clearInterval(_bgKeepaliveTick);
  _bgKeepaliveTick = setInterval(() => {
    if (!isPlaying || !audio.src) return;
    if (audio.paused) {
      if (_bgAudioCtx && _bgAudioCtx.state === 'suspended') {
        _bgAudioCtx.resume().catch(() => {});
      }
      audio.play().then(() => {
        _syncPlayIcons(); _syncPlayingClass(); updateMediaSession();
      }).catch(() => {});
    }
  }, 8000);
  window._bgKeepaliveTick = _bgKeepaliveTick;

  // [SW-PATCH] Service Worker keep-alive ping — har 15s
  // SW idle ho jaaye toh PWABuilder WebView audio pipeline kill kar deta hai.
  // Yeh interval SW ko signal karta hai ki audio active hai.
  let _swPingInterval = null;
  if (window._swPingInterval) clearInterval(window._swPingInterval);
  _swPingInterval = setInterval(() => {
    if (!isPlaying) return;
    if ('serviceWorker' in navigator && navigator.serviceWorker.controller) {
      navigator.serviceWorker.controller.postMessage({ type: 'AUDIO_PLAYING' });
    }
  }, 15000);
  window._swPingInterval = _swPingInterval;
}

audio.addEventListener('playing', () => {
  _acquireWakeLock();
  // Resume AudioContext if suspended (iOS requires this after user gesture)
  if (_bgAudioCtx && _bgAudioCtx.state === 'suspended') {
    _bgAudioCtx.resume().catch(()=>{});
  }
});
audio.addEventListener('pause', () => { if (!isPlaying) _releaseWakeLock(); });

// [PATCH-3] visibilitychange: foreground restore with correct context→audio order
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    if (_bgAudioCtx && _bgAudioCtx.state === 'suspended') {
      _bgAudioCtx.resume().then(() => {
        if (isPlaying && audio.paused && audio.src) {
          audio.play().then(() => {
            isPlaying = true; _syncPlayIcons(); _syncPlayingClass(); updateMediaSession();
          }).catch(()=>{});
        }
      }).catch(()=>{
        if (isPlaying && audio.paused && audio.src) {
          audio.play().then(() => {
            isPlaying = true; _syncPlayIcons(); _syncPlayingClass(); updateMediaSession();
          }).catch(()=>{});
        }
      });
    } else if (isPlaying && audio.paused && audio.src) {
      audio.play().then(() => {
        isPlaying = true; _syncPlayIcons(); _syncPlayingClass(); updateMediaSession();
      }).catch(()=>{});
    }
    if (isPlaying) _acquireWakeLock();
  }
}, { passive: true });

// ─── 37. TOAST ───────────────────────────────────────────────────────────────
let _toastTimer = null;
function showToast(msg) {
  const t = document.getElementById('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

// ─── 38. UTILS ───────────────────────────────────────────────────────────────
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function formatMs(ms) { const s = Math.floor((ms||0)/1000); return `${Math.floor(s/60)}:${(s%60).toString().padStart(2,'0')}`; }
function formatSec(s)  { s = Math.floor(s||0); return `${Math.floor(s/60)}:${(s%60).toString().padStart(2,'0')}`; }
function haptic(pat)   { try { if (navigator.vibrate && (typeof appSettings === 'undefined' || appSettings?.hapticFeedback !== false)) navigator.vibrate(pat); } catch(e) {} }

// ─── 38b. FEEDBACK PROMPT SYSTEM — Spotify / YT Music style ──────────────────
//
// Logic:
//  • Har 7th song ke baad (~14% chance per song) quietly ek small card pop hota hai
//  • 2 types alternate: "Rate this song" (thumbs) aur "Tell us more" (text)
//  • Auto-dismiss 6 sec mein agar user ignore kare
//  • Session mein max 2 baar — spam nahi
//  • Fullscreen player open ho toh wahan show karo, warna mini player ke upar
// ─────────────────────────────────────────────────────────────────────────────
let _fbSongsPlayed   = 0;   // session mein kitne songs play hue
let _fbShownCount    = 0;   // session mein kitni baar prompt aaya
let _fbLastShownTime = 0;   // last prompt ka timestamp
let _fbPromptTimer   = null;
let _feedbackSubmitting = false;
const _FB_MAX_PER_SESSION = 3;
const _FB_MIN_INTERVAL_MS = 5 * 60 * 1000; // 5 min ke beech dobara nahi
const _FB_SONG_INTERVAL   = 6; // har 6th song ke baad eligible
const _FB_PROMPTS = [
  { type: 'rate',  label: 'How was that song?' },
  { type: 'rate',  label: 'Enjoying the music?' },
  { type: 'text',  label: 'Any feedback for us?' },
  { type: 'rate',  label: 'Was this the right song?' },
  { type: 'text',  label: 'Help us improve Aurum 🎵' },
];

function _maybeTriggerFeedbackPrompt(trigger) {
  _fbSongsPlayed++;
  if (_fbShownCount >= _FB_MAX_PER_SESSION) return;
  if (Date.now() - _fbLastShownTime < _FB_MIN_INTERVAL_MS) return;
  if (_fbSongsPlayed % _FB_SONG_INTERVAL !== 0) return;
  // Extra random 40% skip — feel random, not mechanical
  if (Math.random() < 0.4) return;
  // 1.5s delay — song end ke turant baad nahi, thoda wait karo
  setTimeout(_showFeedbackPrompt, 1500);
}

function _showFeedbackPrompt() {
  if (_fbShownCount >= _FB_MAX_PER_SESSION) return;
  if (document.getElementById('fb-prompt')) return; // already open

  _fbShownCount++;
  _fbLastShownTime = Date.now();

  const prompt = _FB_PROMPTS[(_fbShownCount - 1) % _FB_PROMPTS.length];
  const song   = currentTrack;

  const el = document.createElement('div');
  el.id = 'fb-prompt';
  el.style.cssText = `
    position:fixed;left:50%;transform:translateX(-50%) translateY(20px);
    bottom:${document.getElementById('mini-player')?.classList.contains('show') ? '82px' : '20px'};
    z-index:9000;
    background:rgba(24,24,28,0.97);
    border:1px solid rgba(255,255,255,0.09);
    border-radius:16px;
    padding:14px 16px 12px;
    width:min(340px, calc(100vw - 32px));
    box-shadow:0 8px 32px rgba(0,0,0,0.5);
    opacity:0;
    transition:opacity 0.25s ease, transform 0.25s cubic-bezier(0.34,1.56,0.64,1);
    pointer-events:auto;
  `;

  if (prompt.type === 'rate') {
    el.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
        <span style="font-size:13px;font-weight:600;color:#fff;">${prompt.label}</span>
        <button onclick="_dismissFbPrompt()" style="background:none;border:none;color:rgba(255,255,255,0.4);font-size:18px;cursor:pointer;line-height:1;padding:0 2px;">×</button>
      </div>
      ${song ? `<div style="font-size:11px;color:rgba(255,255,255,0.4);margin-bottom:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(song.trackName||'')} · ${esc(song.artistName||'')}</div>` : ''}
      <div style="display:flex;justify-content:center;gap:20px;">
        <button onclick="_submitFbRating('thumbs_up')" style="
          background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);
          border-radius:50px;padding:10px 28px;font-size:20px;cursor:pointer;
          transition:all 0.15s;color:#fff;
        " onmouseenter="this.style.background='rgba(255,255,255,0.12)'" onmouseleave="this.style.background='rgba(255,255,255,0.06)'">👍</button>
        <button onclick="_submitFbRating('thumbs_down')" style="
          background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);
          border-radius:50px;padding:10px 28px;font-size:20px;cursor:pointer;
          transition:all 0.15s;color:#fff;
        " onmouseenter="this.style.background='rgba(255,255,255,0.12)'" onmouseleave="this.style.background='rgba(255,255,255,0.06)'">👎</button>
      </div>
    `;
  } else {
    el.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
        <span style="font-size:13px;font-weight:600;color:#fff;">${prompt.label}</span>
        <button onclick="_dismissFbPrompt()" style="background:none;border:none;color:rgba(255,255,255,0.4);font-size:18px;cursor:pointer;line-height:1;padding:0 2px;">×</button>
      </div>
      ${song ? `<div style="font-size:11px;color:rgba(255,255,255,0.4);margin-bottom:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(song.trackName||'')} · ${esc(song.artistName||'')}</div>` : ''}
      <div style="display:flex;gap:8px;">
        <input id="fb-prompt-input" type="text" placeholder="Type your feedback…" style="
          flex:1;background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.1);
          border-radius:10px;padding:9px 12px;color:#fff;font-size:13px;
          font-family:inherit;outline:none;
        " onkeydown="if(event.key==='Enter') _submitFbText()">
        <button onclick="_submitFbText()" style="
          background:#b89640;border:none;border-radius:10px;
          padding:9px 14px;font-size:13px;font-weight:700;
          color:#000;cursor:pointer;font-family:inherit;white-space:nowrap;
        ">Send</button>
      </div>
    `;
  }

  document.body.appendChild(el);
  // Animate in
  requestAnimationFrame(() => {
    el.style.opacity = '1';
    el.style.transform = 'translateX(-50%) translateY(0)';
  });

  // Auto-dismiss after 7s
  _fbPromptTimer = setTimeout(_dismissFbPrompt, 7000);
}

function _dismissFbPrompt() {
  clearTimeout(_fbPromptTimer);
  const el = document.getElementById('fb-prompt');
  if (!el) return;
  el.style.opacity = '0';
  el.style.transform = 'translateX(-50%) translateY(16px)';
  setTimeout(() => el.remove(), 280);
}

async function _submitFbRating(rating) {
  const song = currentTrack;
  haptic([8, 20, 8]);
  // Thumbs up — quick confirm anim
  const el = document.getElementById('fb-prompt');
  if (el) {
    el.innerHTML = `<div style="text-align:center;padding:8px 0;font-size:22px;">${rating === 'thumbs_up' ? '👍' : '👎'}</div><div style="text-align:center;font-size:13px;color:rgba(255,255,255,0.6);margin-top:4px;">Thanks!</div>`;
    setTimeout(_dismissFbPrompt, 900);
  }
  // Thumbs down — full feedback sheet kholo
  if (rating === 'thumbs_down') {
    setTimeout(() => openFeedback('Wrong song'), 950);
  }
  try {
    await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: rating === 'thumbs_up' ? 'Liked' : 'Disliked',
        message: rating,
        song_name:   song?.trackName  || '',
        artist_name: song?.artistName || '',
        song_id:     song?._saavnId   || String(song?.trackId || ''),
        quality:     _currentSaavnQuality || '',
        user_agent:  navigator.userAgent.slice(0, 200),
      }),
    });
  } catch(e) {}
}

async function _submitFbText() {
  const input = document.getElementById('fb-prompt-input');
  const text  = (input?.value || '').trim();
  if (!text) { if (input) input.focus(); return; }
  const song  = currentTrack;
  haptic([8, 20, 8]);
  const el = document.getElementById('fb-prompt');
  if (el) {
    el.innerHTML = `<div style="text-align:center;padding:8px 0;font-size:22px;">🙏</div><div style="text-align:center;font-size:13px;color:rgba(255,255,255,0.6);margin-top:4px;">Feedback sent!</div>`;
    setTimeout(_dismissFbPrompt, 900);
  }
  try {
    await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        type: 'Quick feedback',
        message: text,
        song_name:   song?.trackName  || '',
        artist_name: song?.artistName || '',
        song_id:     song?._saavnId   || String(song?.trackId || ''),
        quality:     _currentSaavnQuality || '',
        user_agent:  navigator.userAgent.slice(0, 200),
      }),
    });
  } catch(e) {}
}

window._dismissFbPrompt  = _dismissFbPrompt;
window._submitFbRating   = _submitFbRating;
window._submitFbText     = _submitFbText;
window._maybeTriggerFeedbackPrompt = _maybeTriggerFeedbackPrompt;

function openFeedback(prefillType) {
  // Inline modal banao agar exist nahi karta
  let modal = document.getElementById('feedback-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'feedback-modal';
    modal.style.cssText = `
      position:fixed;inset:0;z-index:10000;display:flex;align-items:flex-end;
      background:rgba(0,0,0,0.6);backdrop-filter:blur(6px);
      opacity:0;transition:opacity 0.22s ease;pointer-events:none;
    `;
    modal.innerHTML = `
      <div id="feedback-sheet" style="
        width:100%;background:var(--surface1,#111);border-radius:22px 22px 0 0;
        padding:20px 20px 36px;box-shadow:0 -8px 40px rgba(0,0,0,0.5);
        transform:translateY(100%);transition:transform 0.3s cubic-bezier(0.22,1,0.36,1);
      ">
        <div style="width:36px;height:3px;background:rgba(255,255,255,0.15);border-radius:2px;margin:0 auto 18px;"></div>
        <div style="font-size:15px;font-weight:700;color:var(--text1,#fff);margin-bottom:4px;">Send Feedback</div>
        <div id="feedback-song-label" style="font-size:11px;color:var(--text3,#888);margin-bottom:16px;"></div>

        <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;">
          ${['Wrong song','Wrong artist','Bad quality','App bug','Suggestion','Other'].map(t =>
            `<button class="fb-chip" onclick="_fbSelectType(this,'${t}')" style="
              padding:6px 12px;border-radius:20px;border:1px solid rgba(255,255,255,0.12);
              background:var(--surface2,#1a1a1a);color:var(--text2,#aaa);
              font-size:12px;cursor:pointer;font-family:inherit;transition:all 0.15s;
            ">${t}</button>`
          ).join('')}
        </div>

        <textarea id="feedback-text" placeholder="Describe the issue or suggestion…" style="
          width:100%;min-height:80px;background:var(--surface2,#1a1a1a);
          border:1px solid rgba(255,255,255,0.1);border-radius:12px;
          color:var(--text1,#fff);font-size:13px;padding:10px 12px;
          font-family:inherit;resize:none;outline:none;box-sizing:border-box;
        "></textarea>

        <div style="display:flex;gap:10px;margin-top:12px;">
          <button onclick="closeFeedback()" style="
            flex:1;padding:12px;border-radius:12px;border:1px solid rgba(255,255,255,0.1);
            background:var(--surface2,#1a1a1a);color:var(--text2,#aaa);
            font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;
          ">Cancel</button>
          <button id="fb-submit-btn" onclick="submitFeedback()" style="
            flex:2;padding:12px;border-radius:12px;border:none;
            background:var(--gold,#b89640);color:#000;
            font-size:13px;font-weight:700;cursor:pointer;font-family:inherit;
          ">Send ✓</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', e => { if (e.target === modal) closeFeedback(); });
  }

  // Song label update karo
  const lbl = document.getElementById('feedback-song-label');
  if (lbl && currentTrack) {
    lbl.textContent = `${currentTrack.trackName || ''} · ${currentTrack.artistName || ''}`;
  } else if (lbl) { lbl.textContent = ''; }

  // Prefill type agar diya
  if (prefillType) {
    document.querySelectorAll('.fb-chip').forEach(c => {
      c.style.background = c.textContent === prefillType ? 'var(--gold,#b89640)' : '';
      c.style.color      = c.textContent === prefillType ? '#000' : '';
      c.style.borderColor= c.textContent === prefillType ? 'var(--gold,#b89640)' : '';
      c.dataset.selected = c.textContent === prefillType ? '1' : '';
    });
  }

  modal.style.pointerEvents = 'auto';
  requestAnimationFrame(() => {
    modal.style.opacity = '1';
    const sheet = document.getElementById('feedback-sheet');
    if (sheet) sheet.style.transform = 'translateY(0)';
  });
}

function _fbSelectType(btn, type) {
  document.querySelectorAll('.fb-chip').forEach(c => {
    c.style.background  = '';
    c.style.color       = '';
    c.style.borderColor = '';
    c.dataset.selected  = '';
  });
  btn.style.background  = 'var(--gold,#b89640)';
  btn.style.color       = '#000';
  btn.style.borderColor = 'var(--gold,#b89640)';
  btn.dataset.selected  = '1';
}

function closeFeedback() {
  const modal = document.getElementById('feedback-modal');
  if (!modal) return;
  const sheet = document.getElementById('feedback-sheet');
  modal.style.opacity = '0';
  if (sheet) sheet.style.transform = 'translateY(100%)';
  setTimeout(() => { modal.style.pointerEvents = 'none'; }, 300);
  const ta = document.getElementById('feedback-text');
  if (ta) ta.value = '';
  document.querySelectorAll('.fb-chip').forEach(c => {
    c.style.background = ''; c.style.color = ''; c.style.borderColor = ''; c.dataset.selected = '';
  });
}

async function submitFeedback() {
  if (_feedbackSubmitting) return;
  const typeBtn = document.querySelector('.fb-chip[data-selected="1"]');
  const type    = typeBtn?.textContent || 'General';
  const text    = (document.getElementById('feedback-text')?.value || '').trim();

  if (!text && type === 'General') { showToast('Describe the issue first'); return; }

  _feedbackSubmitting = true;
  const btn = document.getElementById('fb-submit-btn');
  if (btn) { btn.textContent = 'Sending…'; btn.style.opacity = '0.6'; }

  const payload = {
    type,
    message: text,
    song_name:   currentTrack?.trackName  || '',
    artist_name: currentTrack?.artistName || '',
    song_id:     currentTrack?._saavnId   || String(currentTrack?.trackId || ''),
    quality:     _currentSaavnQuality     || '',
    user_agent:  navigator.userAgent.slice(0, 200),
    timestamp:   new Date().toISOString(),
  };

  try {
    await fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    showToast('Feedback sent — thank you! 🙏');
    haptic([10, 30, 10]);
    closeFeedback();
  } catch(e) {
    showToast('Send failed — check connection');
  } finally {
    _feedbackSubmitting = false;
    if (btn) { btn.textContent = 'Send ✓'; btn.style.opacity = ''; }
  }
}

window.openFeedback  = openFeedback;
window.closeFeedback = closeFeedback;
window.submitFeedback = submitFeedback;
window._fbSelectType = _fbSelectType;

if (!isTV) {
  document.addEventListener('keydown', e => {
    if (e.code === 'Space' && e.target.tagName !== 'INPUT') { e.preventDefault(); togglePlay(); }
  });
}

// ─── 39. SMART BACK NAVIGATION ────────────────────────────────────────────────
let _exitConfirmShown = false;
let _exitConfirmTimer = null;

function _pushNavSentinel() {
  try { history.pushState({ aurumNav: true }, '', location.href); } catch(e) {}
}

function _handleSmartBack() {
  if (isTV) return;
  const modalIds = ['song-modal','add-playlist-modal','playlist-opts-modal','quality-modal','download-modal','create-playlist-modal','rename-modal'];
  for (const id of modalIds) {
    const el = document.getElementById(id);
    if (el && (el.classList.contains('open') || el.style.display === 'flex')) {
      if (id === 'song-modal') closeSongModal();
      else if (id === 'add-playlist-modal') closeAddToPlaylistModal();
      else if (id === 'playlist-opts-modal') closePlaylistOpts();
      else if (id === 'quality-modal') closeQualitySheet();
      else if (id === 'download-modal') closeDownloadModal();
      else if (id === 'create-playlist-modal') closeCreatePlaylist();
      else if (id === 'rename-modal') closeRenameModal();
      _pushNavSentinel(); return;
    }
  }
  const qp = document.getElementById('queue-panel');
  if (qp && qp.classList.contains('open')) { closeQueuePanel(); _pushNavSentinel(); return; }
  const ap = document.getElementById('artist-page');
  if (ap && ap.classList.contains('open')) { closeArtistPage(); _pushNavSentinel(); return; }
  const pd = document.getElementById('playlist-detail');
  if (pd && pd.classList.contains('open')) { closePlaylistDetail(); _pushNavSentinel(); return; }
  const fp = document.getElementById('fullscreen-player');
  if (fp && fp.classList.contains('open')) { closeFullscreen(); _pushNavSentinel(); return; }
  const searchPage  = document.getElementById('page-search');
  const libraryPage = document.getElementById('page-library');
  const homeBtn     = document.getElementById('nav-home');
  if ((searchPage && searchPage.classList.contains('active')) || (libraryPage && libraryPage.classList.contains('active'))) {
    if (typeof goPage === 'function' && homeBtn) goPage('home', homeBtn);
    _pushNavSentinel(); return;
  }
  if (_exitConfirmShown) {
    _exitConfirmShown = false; clearTimeout(_exitConfirmTimer);
    try { window.close(); } catch(e) {}
    history.go(-(history.length)); return;
  }
  _exitConfirmShown = true;
  showToast('Press back again to exit');
  clearTimeout(_exitConfirmTimer);
  _exitConfirmTimer = setTimeout(() => { _exitConfirmShown = false; }, 3000);
  _pushNavSentinel();
}

window.addEventListener('popstate', () => { _pushNavSentinel(); _handleSmartBack(); });
document.addEventListener('backbutton', e => { e.preventDefault(); _handleSmartBack(); }, false);

// ─── 40. INITIALIZATION ──────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  setVh(); initViz(); buildHomeSections('all'); renderLibrary(); renderSearchIdle();
  setupMiniGesture(); setupFullPlayerGesture(); setupQueuePanelGesture();
  setupArtSwipeGesture(); setupShakeGesture(); _setupBgAudioPing();
  requestPersistentStorage(); _pushNavSentinel();
});

if (navigator.getBattery) {
  navigator.getBattery().then(b => {
    document.body.dataset.smartSaver = b.level < 0.2 ? 'true' : 'false';
    b.onlevelchange = () => { document.body.dataset.smartSaver = b.level < 0.2 ? 'true' : 'false'; };
  }).catch(() => {});
}

// ─── 41. GLOBAL EXPORTS ──────────────────────────────────────────────────────
window.playSongs = playSongs;
window.togglePlay = togglePlay;
window.nextTrack = nextTrack;
window.prevTrack = prevTrack;
window.openFullscreen = openFullscreen;
window.closeFullscreen = closeFullscreen;
window.toggleQueuePanel = toggleQueuePanel;
window.goPage = goPage;
window.filterHome = filterHome;
window.refreshHomeSections = refreshHomeSections;
window.refreshSection = refreshSection;
window.openArtistPageFromName = openArtistPageFromName;
window.openSongModal = openSongModal;
window.toggleSaveCurrentTrack = toggleSaveCurrentTrack;
window.playLikedSongs = playLikedSongs;
window.switchLibTab = switchLibTab;
window.openCreatePlaylist = openCreatePlaylist;
window.openPlaylistDetail = openPlaylistDetail;
window.closePlaylistDetail = closePlaylistDetail;
window.playPlaylist = playPlaylist;
window.shufflePlaylist = shufflePlaylist;
window.openPlaylistOpts = openPlaylistOpts;
window.closePlaylistOpts = closePlaylistOpts;
window.openRenameModal = openRenameModal;
window.closeRenameModal = closeRenameModal;
window.confirmRename = confirmRename;
window.confirmDeletePlaylist = confirmDeletePlaylist;
window.createPlaylist = createPlaylist;
window.closeCreatePlaylist = closeCreatePlaylist;
window.closeSongModal = closeSongModal;
window.modalSave = modalSave;
window.playNext = playNext;
window.modalDownload = modalDownload;
window.openAddToPlaylistModal = openAddToPlaylistModal;
window.closeAddToPlaylistModal = closeAddToPlaylistModal;
window.addToPlaylist = addToPlaylist;
window.openQualitySheet = openQualitySheet;
window.closeQualitySheet = closeQualitySheet;
window.selectQuality = selectQuality;
window.openDownloadModal = openDownloadModal;
window.closeDownloadModal = closeDownloadModal;
window.triggerDownload = triggerDownload;
window.toggleLyricsView = toggleLyricsView;
window.closeArtistPage = closeArtistPage;
window._playArtistAll = _playArtistAll;
window._playArtistShuffle = _playArtistShuffle;
window._shareArtist = _shareArtist;
window.browseGenre = browseGenre;
window.tapRecentSearch = tapRecentSearch;
window.clearSearch = clearSearch;
window.removeRecent = removeRecent;
window.clearAllRecent = clearAllRecent;
window.confirmClearDownloads = confirmClearDownloads;
window.showToast = showToast;
window.formatSec = formatSec;
window.esc = esc;
window.haptic = haptic;
window.setVolume = setVolume;
window.seekTo = seekTo;
window.toggleShuffle = toggleShuffle;
window.toggleRepeat = toggleRepeat;

// ════════════════════════════════════════════════════════════════════════════
// END OF APP.JS
// ════════════════════════════════════════════════════════════════════════════
