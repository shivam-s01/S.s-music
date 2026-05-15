// ─── DEVICE DETECTION (first — everything depends on this) ───────────────────
const isTV = navigator.userAgent.includes('TV') ||
             navigator.userAgent.includes('SmartTV') ||
             navigator.userAgent.includes('SMART-TV') ||
             (window.innerWidth >= 1280 &&
              window.matchMedia('(hover:hover)').matches &&
              !window.matchMedia('(pointer:fine)').matches);

const isMobile = /Android|iPhone|iPad/i.test(navigator.userAgent) && !isTV;

const isLowEnd = isTV ||
  (navigator.hardwareConcurrency || 8) <= 4 ||
  (typeof navigator.deviceMemory !== 'undefined' && navigator.deviceMemory <= 2);

// ─── DYNAMIC VIEWPORT (Blank Fix) ────────────────────────────────────────────
// Sets --vh so calc(var(--vh, 1vh) * 100) always fills the real visible viewport,
// eliminating bottom gaps during swipe / browser-chrome hide/show.
function setVh() {
  document.documentElement.style.setProperty('--vh', (window.innerHeight * 0.01) + 'px');
}
window.addEventListener('resize',            setVh, { passive: true });
window.addEventListener('orientationchange', () => setTimeout(setVh, 300), { passive: true });
setVh(); // run immediately — before any layout

// ─── FPS CAP (30fps) — for GESTURE moves ─────────────────────────────────────
const TARGET_FPS   = 30;
const FRAME_BUDGET = 1000 / TARGET_FPS; // 33.3 ms
let   _lastRafTime = 0;

// Used ONLY for gesture touchmove batching (not viz — viz has its own loop below)
function cappedRaf(cb) {
  const id = requestAnimationFrame(ts => {
    if (ts - _lastRafTime < FRAME_BUDGET) { cappedRaf(cb); return; }
    _lastRafTime = ts;
    cb(ts);
  });
  return id;
}

// ─── IMAGE SYSTEM ─────────────────────────────────────────────────────────────
const IMG_PLACEHOLDER = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='100'%3E%3Crect width='100' height='100' fill='%230d0d12'/%3E%3Ccircle cx='50' cy='42' r='14' fill='none' stroke='%232e2b26' stroke-width='2'/%3E%3Cpath d='M44 42v-8l16 4v8' fill='none' stroke='%232e2b26' stroke-width='2' stroke-linecap='round'/%3E%3Ccircle cx='44' cy='44' r='3' fill='%232e2b26'/%3E%3Ccircle cx='60' cy='46' r='3' fill='%232e2b26'/%3E%3C/svg%3E";

const _sharedCanvas = document.createElement('canvas');
_sharedCanvas.width = 16; _sharedCanvas.height = 16;
const _sharedCtx = _sharedCanvas.getContext('2d', { willReadFrequently: true });

const imgObserver = (!isTV && typeof IntersectionObserver !== 'undefined')
  ? new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const img = entry.target;
          if (img.dataset.lazySrc) { img.src = img.dataset.lazySrc; delete img.dataset.lazySrc; }
          imgObserver.unobserve(img);
        }
      });
    }, { rootMargin: '80px', threshold: 0 })
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
  img.classList.remove('loaded', 'img-error');
  img.onerror = function() {
    if (this.src !== IMG_PLACEHOLDER) this.src = IMG_PLACEHOLDER;
    this.classList.add('img-error', 'loaded');
    this.onerror = null;
  };
  img.onload = function() { this.classList.add('loaded'); };
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
  const target = isLowEnd ? '300x300' : (size || '600x600');
  return (song?.artworkUrl100 || '').replace('100x100', target);
}

document.querySelectorAll('img').forEach(img => setupImg(img));
new MutationObserver(muts => {
  muts.forEach(m => m.addedNodes.forEach(n => {
    const imgs = n.nodeName === 'IMG' ? [n] : (n.querySelectorAll ? [...n.querySelectorAll('img')] : []);
    imgs.forEach(img => setupImg(img));
  }));
}).observe(document.body, { childList: true, subtree: true });

// ─── STATE ────────────────────────────────────────────────────────────────────
let currentQueue         = [];
let currentIndex         = 0;
let currentTrack         = null;
let isPlaying            = false;
let shuffleOn            = false;
let repeatOn             = false;
let savedSongs           = JSON.parse(localStorage.getItem('aurum_saved')         || '[]');
let playlists            = JSON.parse(localStorage.getItem('aurum_playlists')     || '[]');
let recentlyPlayed       = JSON.parse(localStorage.getItem('aurum_recent_played') || '[]');
let recentSearches       = JSON.parse(localStorage.getItem('aurum_recent')        || '[]');
let currentLibTab        = 'playlists';
let currentQuality       = 'loading';
let currentGenre         = 'all';
let currentPlaylistIndex = null;
let optsPlaylistIndex    = null;
let modalTrack           = null;
let _downloadSong        = null;
let _fullSongAbort       = null;
let _searchTimeout       = null;
let _recFetchTimeout     = null;
let homeCache            = {};
let sectionCache         = {};
let queuePanelOpen       = false;
let _miniPlayerDismissed = false;
let _lastObjectUrl       = null;
let _lastTuTime          = 0;
let _uiHidden            = false;

// ─── AUDIO ENGINE ─────────────────────────────────────────────────────────────
const audio = new Audio();
audio.preload = 'none';
audio.crossOrigin = 'anonymous';
window._aurumAudio = audio;

let _currentSaavnUrl     = null;
let _currentSaavnQuality = null;

// ── TITLE MISMATCH GUARD ──────────────────────────────────────────────────────
function _titleMatches(saavnTitle, itunesTitle) {
  if (!saavnTitle || !itunesTitle) return false;
  const norm = s => s.toLowerCase()
    .replace(/\(.*?\)/g,'').replace(/\[.*?\]/g,'')
    .replace(/[^a-z0-9\s]/g,'').replace(/\s+/g,' ').trim();
  const st = norm(saavnTitle), it = norm(itunesTitle);
  if (!st || !it) return false;
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
    if (iw.some(iword =>
      iword === w ||
      (w.length > 3 && iword.length > 3 && (iword.startsWith(w) || w.startsWith(iword)))
    )) matched++;
  }
  const total = Math.max(sw.length, iw.length);
  const threshold = total <= 2 ? 1.0 : total <= 3 ? 0.67 : 0.55;
  return matched / total >= threshold;
}

function loadTrack(song, autoplay = true) {
  if (!song?.previewUrl) return;

  _miniPlayerDismissed = false;

  if (_fullSongAbort) { _fullSongAbort.abort(); _fullSongAbort = null; }
  _currentSaavnUrl = null; _currentSaavnQuality = null;

  const pill = document.querySelector('.quality-pill');
  if (pill) pill.style.boxShadow = '';

  const sb = document.getElementById('fp-seekbar');
  if (sb) { sb.classList.remove('full-active'); sb.max = 30; sb.value = 0; sb.style.setProperty('--prog', '0%'); }

  currentTrack = song; currentQuality = 'loading';
  document.getElementById('fp-duration').textContent = '0:30';

  audio.pause();
  audio.src = '';
  audio.load();
  audio.src = song.previewUrl;

  if (autoplay) {
    const p = audio.play();
    if (p && p.then) {
      p.then(() => { isPlaying = true; updatePlayerUI(); })
       .catch(err => {
         if (err.name !== 'AbortError') { isPlaying = false; updatePlayerUI(); }
       });
    }
  }

  updatePlayerUI();
  showMiniPlayer();
  updateActiveRows();
  updateQualityLabel();
  addToRecentlyPlayed(song);
  _autoFetchFullSong(song);
  clearTimeout(_recFetchTimeout);
  _recFetchTimeout = setTimeout(() => fetchRecommendations(song), 800);
}

function playSongs(queue, index) {
  currentQueue = [...queue]; currentIndex = index;
  loadTrack(currentQueue[currentIndex]);
}

async function _autoFetchFullSong(song) {
  const ctrl = new AbortController();
  _fullSongAbort = ctrl;
  const requested = song;
  try {
    const rawTitle   = song.trackName  || '';
    const rawArtist  = song.artistName || '';
    const movieMatch = rawTitle.match(/\(From\s+[\u201c\u201d""]?(.+?)[\u201c\u201d""]?\)/i);
    const movieName  = movieMatch ? movieMatch[1].trim() : '';
    const cleanTitle  = rawTitle.replace(/\(.*?\)|\[.*?\]/g, '').trim();
    const cleanArtist = rawArtist.split(/[&,]|feat\.|ft\./i)[0].trim();

    const primaryQ  = encodeURIComponent(movieName ? `${cleanTitle} ${movieName}` : `${cleanTitle} ${cleanArtist}`);
    const fallbackQ = encodeURIComponent(`${cleanTitle} ${cleanArtist}`);
    const artistQ   = encodeURIComponent(cleanArtist);

    const r = await fetch(`/api/saavn?q=${primaryQ}&artist=${artistQ}&fallback=${fallbackQ}`, { signal: ctrl.signal });
    if (!r.ok) throw new Error('api-err');
    const d = await r.json();

    if (ctrl.signal.aborted) return;
    if (currentTrack?.trackId !== requested.trackId) return;
    if (!d.success || !d.url) return;

    if (!_titleMatches(d.title, requested.trackName)) {
      console.warn(`[Mismatch] Asked="${requested.trackName}" Got="${d.title}" — staying on preview`);
      return;
    }

    const proxyUrl = `/api/stream?url=${encodeURIComponent(d.url)}`;
    _currentSaavnUrl     = proxyUrl;
    _currentSaavnQuality = d.quality || 'unknown';
    _updateDlSheetQuality(d.quality);

    const preAudio = new Audio();
    preAudio.preload = 'auto';
    preAudio.crossOrigin = 'anonymous';

    await new Promise((res, rej) => {
      const to = setTimeout(() => rej(new Error('preload-timeout')), 14000);
      preAudio.addEventListener('canplay', () => { clearTimeout(to); res(); }, { once: true });
      preAudio.addEventListener('error',   () => { clearTimeout(to); rej(new Error('preload-error')); }, { once: true });
      preAudio.src = proxyUrl;
      preAudio.load();
    });

    if (ctrl.signal.aborted || currentTrack?.trackId !== requested.trackId) {
      preAudio.src = ''; return;
    }

    const wasPlaying = isPlaying;
    const pos = audio.currentTime;

    const onMeta = () => {
      if (isFinite(pos) && pos > 0 && pos < audio.duration) audio.currentTime = pos;
    };
    audio.addEventListener('loadedmetadata', onMeta, { once: true });

    audio.src = proxyUrl;

    const sbEl = document.getElementById('fp-seekbar');
    if (sbEl) sbEl.classList.add('full-active');

    if (wasPlaying) {
      const pp = audio.play();
      if (pp?.then) pp.then(() => {
        if (ctrl.signal.aborted || currentTrack?.trackId !== requested.trackId) { audio.pause(); return; }
        isPlaying = true; currentQuality = 'full'; _fullSongAbort = null;
        updateQualityLabel(); updatePlayerUI();
      }).catch(() => { if (!ctrl.signal.aborted) _fallbackToPreview(requested); });
    } else {
      currentQuality = 'full'; _fullSongAbort = null;
      updateQualityLabel(); updatePlayerUI();
    }
    preAudio.src = '';

  } catch(e) {
    if (e.name !== 'AbortError') console.info('[AutoFetch] Staying on preview:', e.message);
  }
}

function _fallbackToPreview(song) {
  if (!song?.previewUrl) return;
  if (currentTrack?.trackId !== song.trackId) return;
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

// ─── PLAYBACK CONTROLS ────────────────────────────────────────────────────────
function togglePlay() {
  if (!currentTrack) return;
  if (isPlaying) {
    audio.pause(); isPlaying = false;
  } else {
    const p = audio.play();
    if (p && p.then) {
      p.then(() => { isPlaying = true; updatePlayerUI(); })
       .catch(() => { isPlaying = false; updatePlayerUI(); });
      return;
    }
    isPlaying = true;
  }
  updatePlayerUI();
}

function nextTrack() {
  if (!currentQueue.length) return;
  if (shuffleOn) {
    let next;
    do { next = Math.floor(Math.random() * currentQueue.length); }
    while (next === currentIndex && currentQueue.length > 1);
    currentIndex = next;
  } else {
    currentIndex = (currentIndex + 1) % currentQueue.length;
  }
  loadTrack(currentQueue[currentIndex]);
  updateQueuePanel();
}

function prevTrack() {
  if (audio.currentTime > 3) { audio.currentTime = 0; return; }
  if (!currentQueue.length) return;
  currentIndex = (currentIndex - 1 + currentQueue.length) % currentQueue.length;
  loadTrack(currentQueue[currentIndex]);
  updateQueuePanel();
}

function seekTo(v) { if (isFinite(audio.duration)) audio.currentTime = parseFloat(v); }

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
  document.getElementById('shuffle-btn').querySelector('svg').style.stroke = shuffleOn ? 'var(--gold-l)' : '';
  showToast(shuffleOn ? 'Shuffle on' : 'Shuffle off');
}

function toggleRepeat() {
  repeatOn = !repeatOn; audio.loop = repeatOn;
  document.getElementById('repeat-btn').querySelector('svg').style.stroke = repeatOn ? 'var(--gold-l)' : '';
  showToast(repeatOn ? 'Repeat on' : 'Repeat off');
}

// ─── AUDIO EVENTS ─────────────────────────────────────────────────────────────
audio.addEventListener('ended', () => { if (!repeatOn) nextTrack(); });

audio.addEventListener('error', () => {
  if (currentQuality === 'full' && currentTrack?.previewUrl) _fallbackToPreview(currentTrack);
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
});

audio.addEventListener('pause', () => {
  isPlaying = false;
  _syncPlayIcons();
  _syncPlayingClass();
  updateMediaSession();
});

// ─── TIMEUPDATE — 250ms throttle, no RAF ─────────────────────────────────────
audio.addEventListener('timeupdate', () => {
  const now = Date.now();
  if (now - _lastTuTime < 250) return;
  _lastTuTime = now;

  if (_uiHidden) return;

  const dur = isFinite(audio.duration) && audio.duration > 0
    ? audio.duration : (currentQuality === 'full' ? 0 : 30);
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
});

audio.addEventListener('durationchange', () => {
  if (isFinite(audio.duration) && audio.duration > 0) {
    const sb = document.getElementById('fp-seekbar');
    if (sb) sb.max = audio.duration;
    const fd = document.getElementById('fp-duration');
    if (fd) fd.textContent = formatSec(audio.duration);
  }
});

// ─── OFFLINE DETECTION ────────────────────────────────────────────────────────
function _handleConnectivity() {
  const banner = document.getElementById('offline-banner');
  if (!banner) return;
  navigator.onLine ? banner.classList.remove('show') : banner.classList.add('show');
}
window.addEventListener('online',  _handleConnectivity, { passive: true });
window.addEventListener('offline', _handleConnectivity, { passive: true });
_handleConnectivity();

// ─── SCREEN OFF / ON — complete UI freeze ────────────────────────────────────
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    _uiHidden = true;

    // Stop viz immediately — Heat Fix: no GPU work while screen is off
    _stopViz();

    document.getElementById('ambient-canvas')?.classList.remove('orbs-active');

    const fp = document.getElementById('fullscreen-player');
    const ac = document.getElementById('ambient-canvas');
    if (fp) fp.style.setProperty('visibility', 'hidden', 'important');
    if (ac) ac.style.setProperty('display',    'none',   'important');

    const keep = new Set(['recent', 'featured']);
    Object.keys(sectionCache).forEach(k => { if (!keep.has(k)) delete sectionCache[k]; });

  } else {
    _uiHidden = false;

    const fp = document.getElementById('fullscreen-player');
    const ac = document.getElementById('ambient-canvas');
    if (fp) fp.style.removeProperty('visibility');
    if (ac) ac.style.removeProperty('display');

    // Only restart viz if fullscreen player is actually open
    if (fp?.classList.contains('open') && !isLowEnd) _startViz();

    if (!isLowEnd) document.getElementById('ambient-canvas')?.classList.add('orbs-active');

    if (currentTrack) {
      _syncPlayIcons();
      _syncPlayingClass();
      updateQualityLabel();
    }
  }
}, { passive: true });

// ─── UI UPDATES ───────────────────────────────────────────────────────────────
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
  if (fp) isPlaying ? fp.classList.add('playing')      : fp.classList.remove('playing');
  if (mp) isPlaying ? mp.classList.add('playing-glow') : mp.classList.remove('playing-glow');
}

function updatePlayerUI() {
  if (!currentTrack) return;
  const artUrl = getArtUrl(currentTrack, '600x600');

  const miniArt = document.getElementById('mini-art');
  if (miniArt) setImgSrc(miniArt, artUrl);
  const fpArt = document.getElementById('fp-art');
  if (fpArt) setImgSrc(fpArt, artUrl);

  const mt = document.getElementById('mini-title');   if (mt) mt.textContent  = currentTrack.trackName  || 'Unknown';
  const ma = document.getElementById('mini-artist');  if (ma) ma.textContent  = currentTrack.artistName || 'Unknown';
  const ft = document.getElementById('fp-title');     if (ft) ft.textContent  = currentTrack.trackName  || 'Unknown';
  const fa = document.getElementById('fp-artist');    if (fa) fa.textContent  = currentTrack.artistName || 'Unknown';

  _syncPlayIcons();
  _syncPlayingClass();
  updateSaveBtn();
  updateActiveRows();
  updateAmbientPlayer(artUrl);
  updateNextStrip();
  updateMediaSession();
  showMiniPlayer();
}

function updateNextStrip() {
  const strip = document.getElementById('fp-next-strip');
  if (!strip) return;
  if (!currentQueue.length || currentQueue.length < 2) { strip.style.display = 'none'; return; }
  const nextIdx = shuffleOn
    ? (currentIndex + 1 + Math.floor(Math.random() * (currentQueue.length - 1))) % currentQueue.length
    : (currentIndex + 1) % currentQueue.length;
  const nextSong = currentQueue[nextIdx];
  if (!nextSong) { strip.style.display = 'none'; return; }
  strip.style.display = '';
  setImgSrc(document.getElementById('fp-next-art'), getArtUrl(nextSong, '300x300'));
  const nt = document.getElementById('fp-next-title');   if (nt) nt.textContent  = nextSong.trackName  || 'Unknown';
  const na = document.getElementById('fp-next-artist');  if (na) na.textContent  = nextSong.artistName || 'Unknown';
}

function updateMediaSession() {
  if (!('mediaSession' in navigator) || !currentTrack) return;
  try {
    const artUrl = getArtUrl(currentTrack, '512x512');
    navigator.mediaSession.metadata = new MediaMetadata({
      title:   currentTrack.trackName  || 'Unknown',
      artist:  currentTrack.artistName || 'Unknown',
      artwork: [{ src: artUrl, sizes: '512x512', type: 'image/jpeg' }]
    });
    navigator.mediaSession.setActionHandler('play',          () => { audio.play().catch(()=>{}); isPlaying = true;  updatePlayerUI(); });
    navigator.mediaSession.setActionHandler('pause',         () => { audio.pause();               isPlaying = false; updatePlayerUI(); });
    navigator.mediaSession.setActionHandler('nexttrack',     nextTrack);
    navigator.mediaSession.setActionHandler('previoustrack', prevTrack);
    try { navigator.mediaSession.setActionHandler('seekto', d => { if (isFinite(d.seekTime)) seekTo(d.seekTime); }); } catch(e) {}
    navigator.mediaSession.playbackState = isPlaying ? 'playing' : 'paused';
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
  if (_miniPlayerDismissed) return;
  if (isTV) return;
  document.getElementById('mini-player').classList.add('show');
}

function updateActiveRows() {
  document.querySelectorAll('.song-row,.queue-item').forEach(r => {
    const isCurrent = currentTrack && (String(r.dataset.trackId) === String(currentTrack.trackId));
    r.classList.toggle('playing', isCurrent);
    r.classList.toggle('current', isCurrent);
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
      if (durSpan)  durSpan.style.display = '';
    }
  });
}

function updateQualityLabel() {
  const lbl  = document.getElementById('quality-label');
  const pill = document.querySelector('.quality-pill');
  if (!lbl) return;
  if (currentQuality === 'full') {
    const q    = (_currentSaavnQuality || '').toLowerCase();
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
    const q      = (_currentSaavnQuality || '').toLowerCase();
    const kLabel = q.includes('320') ? '320 kbps' : q.includes('160') ? '160 kbps' : 'HQ Stream';
    fb.textContent = currentQuality === 'full' ? `● ${kLabel}` : '▶ Stream';
    fb.className   = 'quality-badge' + (currentQuality === 'full' ? ' active' : ' ext');
  }
  if (pb) {
    pb.textContent = currentQuality === 'preview' ? '● 128 kbps' : '○ Preview';
    pb.className   = 'quality-badge' + (currentQuality === 'preview' ? ' active' : ' ext');
  }
}

// ─── AMBIENT PLAYER BG ───────────────────────────────────────────────────────
let _lastAmbientSrc = '';
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
  try {
    _sharedCtx.drawImage(imgEl, 0, 0, 16, 16);
    const data = _sharedCtx.getImageData(0, 0, 16, 16).data;
    let r = 0, g = 0, b = 0, count = 0;
    for (let i = 0; i < data.length; i += 16) { r += data[i]; g += data[i+1]; b += data[i+2]; count++; }
    if (!count) { callback(184, 150, 64); return; }
    r = Math.round(r / count); g = Math.round(g / count); b = Math.round(b / count);
    const max = Math.max(r, g, b, 1);
    r = Math.round(r / max * 210); g = Math.round(g / max * 210); b = Math.round(b / max * 210);
    callback(r, g, b);
  } catch(e) { callback(184, 150, 64); }
}

// ─── VISUALIZER — Zero-Heat Loop ─────────────────────────────────────────────
// HEAT FIX: Uses its own RAF loop that checks for the 'open' class on every tick.
// If the player is not open, or the screen is hidden, it stops IMMEDIATELY.
// Uses a separate _lastVizTime timer (not shared with cappedRaf) to avoid starvation.
const VIZ_COUNT = isLowEnd ? 0 : 44;
let vizBars       = [];
let vizRaf        = null;
let vizPhase      = 0;
let vizTarget     = [];
let _lastVizTime  = 0; // separate from _lastRafTime — no conflict with gesture RAF
const vizRandOffsets = Array.from({ length: VIZ_COUNT }, () => Math.random() * 6.28);

function initViz() {
  if (isLowEnd) return;
  const c = document.getElementById('fp-visualizer'); if (!c) return;
  c.innerHTML = ''; vizBars = []; vizTarget = [];
  for (let i = 0; i < VIZ_COUNT; i++) {
    const b = document.createElement('div'); b.className = 'fp-viz-bar';
    c.appendChild(b); vizBars.push(b); vizTarget.push(0.05);
  }
  // Do NOT auto-start here — wait until player opens
}

function _startViz() {
  if (isLowEnd || vizRaf !== null) return;
  vizRaf = requestAnimationFrame(_vizLoop);
}

function _stopViz() {
  if (vizRaf) { cancelAnimationFrame(vizRaf); vizRaf = null; }
}

function _vizLoop(ts) {
  // ── HARD STOP CONDITIONS — checked at the top of EVERY frame ──
  const fp = document.getElementById('fullscreen-player');
  if (document.hidden || !fp?.classList.contains('open') || isLowEnd) {
    vizRaf = null; // Stop the loop completely — zero battery drain
    return;
  }

  // 30fps cap — own timer so gestures' cappedRaf doesn't interfere
  if (ts - _lastVizTime < FRAME_BUDGET) {
    vizRaf = requestAnimationFrame(_vizLoop);
    return;
  }
  _lastVizTime = ts;

  // ── ANIMATION FRAME ──
  vizPhase += 0.034 + Math.sin(vizPhase * 0.1) * 0.004;
  vizBars.forEach((b, i) => {
    if (!isPlaying) {
      vizTarget[i] = vizTarget[i] * 0.88 + 0.05 * 0.12;
      b.style.transform = `scaleY(${vizTarget[i].toFixed(3)})`; return;
    }
    const norm      = i / VIZ_COUNT;
    const freqCurve = norm < 0.12 ? (norm / 0.12) : norm < 0.44 ? 1 - (norm - 0.12) * 0.55 : Math.max(0.1, 0.8 - (norm - 0.44) * 1.35);
    const rOff = vizRandOffsets[i];
    const o1 = Math.sin(vizPhase * 2.0 + i * 0.36 + rOff) * 0.38 + 0.38;
    const o2 = Math.sin(vizPhase * 1.3 + i * 0.68 + rOff * 0.7 + 0.9) * 0.22 + 0.22;
    const o3 = Math.sin(vizPhase * 3.1 + i * 0.22 + rOff * 0.4 + 2.1) * 0.11 + 0.11;
    const target = Math.max(0.05, Math.min(1, (o1 + o2 * 0.58 + o3 * 0.38) * freqCurve * 0.88));
    vizTarget[i] = vizTarget[i] * 0.74 + target * 0.26;
    b.style.transform = `scaleY(${vizTarget[i].toFixed(3)})`;
  });

  // Schedule next frame — the loop is self-cancelling via the top guards
  vizRaf = requestAnimationFrame(_vizLoop);
}

// Legacy alias so existing call-sites still work
function tickViz() { _startViz(); }

// ─── SMART BLUR HELPERS (Performance Fix) ────────────────────────────────────
// Disabling backdrop-filter during drag is the #1 thermal win on mobile GPUs.
// Re-enabled on touchend once the element is at rest.
function _pauseBlur() {
  const els = [
    document.getElementById('fullscreen-player'),
    document.getElementById('mini-player'),
    document.getElementById('queue-panel'),
  ];
  els.forEach(el => {
    if (!el) return;
    el.style.backdropFilter       = 'none';
    el.style.webkitBackdropFilter = 'none';
  });
}

function _resumeBlur() {
  const els = [
    document.getElementById('fullscreen-player'),
    document.getElementById('mini-player'),
    document.getElementById('queue-panel'),
  ];
  els.forEach(el => {
    if (!el) return;
    el.style.backdropFilter       = '';
    el.style.webkitBackdropFilter = '';
  });
}

// ─── GESTURE: MINI PLAYER ────────────────────────────────────────────────────
function setupMiniGesture() {
  const mp = document.getElementById('mini-player');
  let startY = 0, startX = 0, isDragging = false, startTime = 0, moved = false, rafId = null;

  mp.addEventListener('touchstart', e => {
    e.stopPropagation();
    startY = e.touches[0].clientY; startX = e.touches[0].clientX;
    isDragging = true; startTime = Date.now(); moved = false;
    mp.style.transition  = 'none';
    // Hardware Promotion: GPU-composite the layer before drag starts
    mp.style.willChange  = 'transform';
    mp.style.transform   = mp.style.transform || 'translateZ(0)';
    // Smart Blur: kill expensive blur during movement
    _pauseBlur();
  }, { passive: true });

  mp.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dy = e.touches[0].clientY - startY;
    const dx = Math.abs(e.touches[0].clientX - startX);
    if (!moved && dx > Math.abs(dy)) { isDragging = false; _resumeBlur(); return; }
    if (Math.abs(dy) > 6) {
      moved = true;
      e.preventDefault();
      const clamped = dy < 0 ? Math.max(-70, dy * 0.3) : Math.min(80, dy * 0.45);
      // cappedRaf: batch DOM writes to 30fps to keep device cool
      if (rafId) cancelAnimationFrame(rafId);
      rafId = cappedRaf(() => { mp.style.transform = `translateY(${clamped}px)`; rafId = null; });
    }
  }, { passive: false });

  mp.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return; isDragging = false;
    const dy  = e.changedTouches[0].clientY - startY;
    const dt  = Date.now() - startTime;
    const vel = Math.abs(dy) / dt;
    mp.style.transition = '';
    mp.style.willChange = '';
    // Smart Blur: restore blur now that element is at rest
    _resumeBlur();
    if (!moved) return;

    if (dy < -30 || vel > 0.45) {
      mp.style.transform = ''; openFullscreen();
    } else if (dy > 60 || (vel > 0.55 && dy > 25)) {
      _miniPlayerDismissed = true;
      mp.style.transform = '';
      mp.classList.remove('show');
      if (isPlaying) { audio.pause(); isPlaying = false; _syncPlayIcons(); _syncPlayingClass(); }
    } else {
      mp.style.transform = '';
    }
  }, { passive: true });
}

// ─── GESTURE: FULLSCREEN PLAYER ──────────────────────────────────────────────
function setupFullPlayerGesture() {
  const fp = document.getElementById('fullscreen-player');
  const qp = document.getElementById('queue-panel');
  let startY = 0, startX = 0, isDragging = false, startTime = 0, gestureTarget = null, moved = false, rafId = null;

  function isGestureZone(el) {
    return el.closest('#fp-drag-hint')          ||
           el.closest('.fp-header')             ||
           el.closest('.fp-info')               ||
           el.closest('.queue-panel-handle')    ||
           el.closest('#queue-drag-handle');
  }

  fp.addEventListener('touchstart', e => {
    const qpOpen        = qp.classList.contains('open');
    const onQueueHandle = e.target.closest('#queue-drag-handle');
    const onQueueBody   = qp.contains(e.target) && !onQueueHandle;
    if (qpOpen && onQueueBody)              { isDragging = false; return; }
    if (!qpOpen && !isGestureZone(e.target)) { isDragging = false; return; }
    startY = e.touches[0].clientY; startX = e.touches[0].clientX;
    isDragging = true; startTime = Date.now(); moved = false;
    gestureTarget = qpOpen ? 'queue' : 'player';

    // Hardware Promotion: elevate to GPU composite layer immediately
    const target = gestureTarget === 'player' ? fp : qp;
    target.style.willChange = 'transform';
    target.style.transform  = target.style.transform || 'translateZ(0)';

    fp.classList.add('dragging'); qp.classList.add('dragging');

    // Smart Blur: disable backdrop-filter for all drag participants
    _pauseBlur();
  }, { passive: true });

  fp.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dy = e.touches[0].clientY - startY;
    const dx = Math.abs(e.touches[0].clientX - startX);
    if (!moved && dx > Math.abs(dy) + 4) {
      isDragging = false; fp.classList.remove('dragging'); qp.classList.remove('dragging');
      _resumeBlur(); return;
    }
    if (Math.abs(dy) < 4 && !moved) return;
    moved = true;
    if (gestureTarget === 'player' && dy > 0) {
      e.preventDefault();
      const clamped = Math.max(0, dy * 0.5);
      // cappedRaf: 30fps DOM write — prevents GPU thrashing
      if (rafId) cancelAnimationFrame(rafId);
      rafId = cappedRaf(() => { fp.style.transform = `translateY(${clamped}px)`; rafId = null; });
    } else if (gestureTarget === 'queue' && dy > 0) {
      e.preventDefault();
      const clamped = Math.min(120, dy * 0.55);
      if (rafId) cancelAnimationFrame(rafId);
      rafId = cappedRaf(() => { qp.style.transform = `translateY(${clamped}px)`; rafId = null; });
    } else if (gestureTarget === 'player' && dy < -60) {
      e.preventDefault();
      openQueuePanel();
      isDragging = false;
      fp.classList.remove('dragging'); qp.classList.remove('dragging');
      fp.style.transform = ''; qp.style.transform = '';
      _resumeBlur();
    }
  }, { passive: false });

  fp.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return; isDragging = false;
    fp.classList.remove('dragging'); qp.classList.remove('dragging');
    fp.style.willChange = ''; qp.style.willChange = '';
    const dy  = e.changedTouches[0].clientY - startY;
    const dt  = Date.now() - startTime;
    const vel = Math.abs(dy) / dt;
    // Smart Blur: restore blur — element is now at rest
    _resumeBlur();
    if (gestureTarget === 'player') {
      if (dy > 90 || (vel > 0.55 && dy > 30)) { fp.style.transform = ''; closeFullscreen(); }
      else fp.style.transform = '';
    } else if (gestureTarget === 'queue') {
      if (dy > 70 || (vel > 0.5 && dy > 20)) { qp.style.transform = ''; closeQueuePanel(); }
      else qp.style.transform = '';
    }
  }, { passive: true });
}

// ─── GESTURE: ART SWIPE ──────────────────────────────────────────────────────
function setupArtSwipeGesture() {
  const artWrap = document.getElementById('fp-art-wrap');
  if (!artWrap) return;
  let startX = 0, startY = 0, isDragging = false, moved = false, startTime = 0, rafId = null;

  artWrap.addEventListener('touchstart', e => {
    startX = e.touches[0].clientX; startY = e.touches[0].clientY;
    isDragging = true; moved = false; startTime = Date.now();
    artWrap.style.transition = 'none';
    // Hardware Promotion: both transform and opacity will animate — promote both
    artWrap.style.willChange = 'transform,opacity';
    artWrap.style.transform  = artWrap.style.transform || 'translateZ(0)';
  }, { passive: true });

  artWrap.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dx = e.touches[0].clientX - startX;
    const dy = Math.abs(e.touches[0].clientY - startY);
    if (!moved && dy > Math.abs(dx) + 8) { isDragging = false; artWrap.style.willChange = ''; return; }
    if (Math.abs(dx) > 8) {
      moved = true;
      e.preventDefault();
      const clamped = dx * 0.72;
      const tilt    = clamped * 0.018;
      const fade    = Math.max(0.28, 1 - Math.abs(dx) / 280);
      // cappedRaf: 30fps visual write
      if (rafId) cancelAnimationFrame(rafId);
      rafId = cappedRaf(() => {
        artWrap.style.transform = `translateX(${clamped}px) rotate(${tilt}deg)`;
        artWrap.style.opacity   = fade;
        rafId = null;
      });
    }
  }, { passive: false });

  artWrap.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return; isDragging = false;
    artWrap.style.willChange = '';
    const dx  = e.changedTouches[0].clientX - startX;
    const dt  = Date.now() - startTime;
    const vel = Math.abs(dx) / dt;
    if (!moved) { _resetArtWrap(); return; }
    if      (dx < -55 || (vel > 0.38 && dx < -18)) { _animateArtSwipe('left',  nextTrack); }
    else if (dx >  55 || (vel > 0.38 && dx >  18)) { _animateArtSwipe('right', prevTrack); }
    else {
      artWrap.style.transition = 'transform .32s cubic-bezier(0.34,1.56,0.64,1), opacity .22s ease';
      _resetArtWrap();
    }
  }, { passive: true });

  artWrap.addEventListener('touchcancel', () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging = false; artWrap.style.willChange = '';
    _resetArtWrap();
  }, { passive: true });
}

function _resetArtWrap() {
  const artWrap = document.getElementById('fp-art-wrap');
  if (!artWrap) return;
  artWrap.style.transform = '';
  artWrap.style.opacity   = '';
}

function _animateArtSwipe(direction, callback) {
  const artWrap = document.getElementById('fp-art-wrap');
  if (!artWrap) { callback(); return; }
  const xOut = direction === 'left' ? '-115%' : '115%';
  const xIn  = direction === 'left' ? '115%'  : '-115%';

  artWrap.style.transition = 'transform .2s cubic-bezier(0.4,0,1,1), opacity .18s ease';
  artWrap.style.transform  = `translateX(${xOut}) rotate(${direction === 'left' ? -4 : 4}deg)`;
  artWrap.style.opacity    = '0';

  setTimeout(() => {
    artWrap.style.transition = 'none';
    artWrap.style.transform  = `translateX(${xIn}) rotate(${direction === 'left' ? 4 : -4}deg)`;
    artWrap.style.opacity    = '0';
    callback();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      artWrap.style.transition = 'transform .42s cubic-bezier(0.22,1,0.36,1), opacity .3s ease';
      artWrap.style.transform  = '';
      artWrap.style.opacity    = '';
    }));
  }, 185);
}

// ─── TV: D-PAD NAVIGATION ────────────────────────────────────────────────────
function setupTVNavigation() {
  document.addEventListener('keydown', e => {
    switch(e.key) {
      case 'ArrowRight':  nextTrack();  e.preventDefault(); break;
      case 'ArrowLeft':   prevTrack();  e.preventDefault(); break;
      case 'Enter':       togglePlay(); e.preventDefault(); break;
      case 'ArrowUp':     setVolume(Math.min(1, audio.volume + 0.1)); e.preventDefault(); break;
      case 'ArrowDown':   setVolume(Math.max(0, audio.volume - 0.1)); e.preventDefault(); break;
      case 'Backspace':
      case 'GoBack':      closeFullscreen(); e.preventDefault(); break;
    }
  });
}

// ─── PLAYER OPEN / CLOSE ─────────────────────────────────────────────────────
function openFullscreen() {
  const fp = document.getElementById('fullscreen-player');
  const mp = document.getElementById('mini-player');
  fp.style.transform = '';
  fp.classList.add('open');
  if (mp) {
    mp.style.transition    = 'opacity 0.2s ease, transform 0.2s ease';
    mp.style.opacity       = '0';
    mp.style.pointerEvents = 'none';
  }
  updateNextStrip();
  // Start viz only now that the player is actually visible
  if (!document.hidden && !isLowEnd) _startViz();
  if (!isLowEnd) document.getElementById('ambient-canvas')?.classList.add('orbs-active');
}

function closeFullscreen() {
  const fp = document.getElementById('fullscreen-player');
  const mp = document.getElementById('mini-player');
  fp.style.transform = '';
  fp.classList.remove('open');
  closeQueuePanel();
  // Stop viz as soon as player is no longer open — zero background drain
  _stopViz();
  document.getElementById('ambient-canvas')?.classList.remove('orbs-active');
  if (mp) {
    setTimeout(() => {
      mp.style.transition    = '';
      mp.style.opacity       = '';
      mp.style.pointerEvents = '';
    }, 220);
  }
}

// ─── QUEUE PANEL ─────────────────────────────────────────────────────────────
function toggleQueuePanel() { queuePanelOpen ? closeQueuePanel() : openQueuePanel(); }

function openQueuePanel() {
  queuePanelOpen = true;
  document.getElementById('queue-panel').classList.add('open');
  document.getElementById('fp-queue-btn').classList.add('queue-open');
  updateQueuePanel();
}

function closeQueuePanel() {
  queuePanelOpen = false;
  document.getElementById('queue-panel').classList.remove('open');
  document.getElementById('fp-queue-btn').classList.remove('queue-open');
}

function updateQueuePanel() {
  const body    = document.getElementById('queue-panel-body');
  const countEl = document.getElementById('queue-count');
  body.innerHTML = '';
  if (!currentQueue.length) {
    body.innerHTML = '<div style="padding:32px;text-align:center;color:var(--text3);font-size:12px;">Queue is empty</div>'; return;
  }
  const remaining = currentQueue.length - currentIndex - 1;
  if (countEl) countEl.textContent = remaining + ' songs';
  if (currentTrack) {
    const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = 'Now Playing';
    body.appendChild(sec);
    body.appendChild(makeQueueItem(currentTrack, currentIndex, true));
  }
  const nextSongs = currentQueue.slice(currentIndex + 1, currentIndex + 16);
  if (nextSongs.length) {
    const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = 'Up Next';
    body.appendChild(sec);
    nextSongs.forEach((s, i) => body.appendChild(makeQueueItem(s, currentIndex + 1 + i, false)));
  }
  if (currentIndex > 0) {
    const prevSongs = currentQueue.slice(Math.max(0, currentIndex - 5), currentIndex);
    if (prevSongs.length) {
      const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = 'Previously Played';
      body.appendChild(sec);
      prevSongs.forEach((s, i) => body.appendChild(makeQueueItem(s, Math.max(0, currentIndex - prevSongs.length) + i, false)));
    }
  }
  updateNextStrip();
}

function makeQueueItem(song, qIdx, isCurrent) {
  const item = document.createElement('div');
  item.className = 'queue-item' + (isCurrent ? ' current' : '');
  item.dataset.trackId = song.trackId;
  const artUrl = getArtUrl(song, '300x300');
  const dur    = song.trackTimeMillis ? formatMs(song.trackTimeMillis) : '';
  item.dataset.dur = dur;
  const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
  setImgSrc(img, artUrl);
  item.appendChild(img);
  const info = document.createElement('div'); info.className = 'queue-item-info';
  info.innerHTML = `<div class="queue-item-title">${esc(song.trackName)}</div><div class="queue-item-artist">${esc(song.artistName)}</div>`;
  item.appendChild(info);
  if (isCurrent && isPlaying) {
    const bar = document.createElement('div'); bar.className = 'queue-now-playing';
    bar.innerHTML = '<span></span><span></span><span></span>'; item.appendChild(bar);
  } else {
    const d = document.createElement('span'); d.className = 'queue-item-dur'; d.textContent = dur; item.appendChild(d);
  }
  if (!isCurrent) item.onclick = () => { currentIndex = qIdx; loadTrack(currentQueue[currentIndex]); updateQueuePanel(); };
  return item;
}

// ─── RECOMMENDATIONS ─────────────────────────────────────────────────────────
async function fetchRecommendations(song) {
  if (!song) return;
  try {
    const artist = song.artistName?.split(/[&,]|feat\.|ft\./i)[0].trim() || '';
    const r = await fetch(`/api/songs?q=${encodeURIComponent(artist + ' songs')}`);
    const d = await r.json();
    const recs = (d.results || []).filter(s => s.previewUrl && String(s.trackId) !== String(song.trackId));
    if (recs.length) {
      const existingIds = new Set(currentQueue.map(s => String(s.trackId)));
      const newRecs     = recs.filter(s => !existingIds.has(String(s.trackId))).slice(0, 8);
      currentQueue      = [...currentQueue, ...newRecs];
      if (queuePanelOpen) updateQueuePanel();
    }
  } catch(e) {}
}

// ─── RECENTLY PLAYED ─────────────────────────────────────────────────────────
function addToRecentlyPlayed(song) {
  recentlyPlayed = recentlyPlayed.filter(s => String(s.trackId) !== String(song.trackId));
  recentlyPlayed.unshift(song);
  if (recentlyPlayed.length > 20) recentlyPlayed = recentlyPlayed.slice(0, 20);
  localStorage.setItem('aurum_recent_played', JSON.stringify(recentlyPlayed));
  renderQuickResume();
}

// ─── HOME SECTIONS ────────────────────────────────────────────────────────────
const SECTION_POOL = [
  { id:'recent',   title:'Continue Listening', type:'wide',     fn: getRecentlyPlayedSongs },
  { id:'featured', title:'Made For You',        type:'featured', queries:['top bollywood songs hits','best hindi songs','latest bollywood hits','top hindi songs trending','best bollywood songs playlist'] },
  { id:'trending', title:'Trending Now',        type:'cards',    queries:['trending hindi songs chart','bollywood chart toppers','top hindi songs this week','most popular bollywood songs'] },
  { id:'mood',     title:'Mood: Late Night',    type:'bw',       queries:['sad emotional bollywood songs','heartbreak hindi songs arijit','late night slow songs hindi','emotional romantic songs hindi'] },
  { id:'classic',  title:'Golden Era',          type:'bw',       queries:['90s bollywood romantic classic songs','80s hindi classic songs','old is gold bollywood songs kishore kumar','retro bollywood hits lata mangeshkar'] },
  { id:'hiphop',   title:'Desi Hip-Hop',        type:'cards',    queries:['divine emiway bantai rap hindi','desi hip hop india rap songs','yo yo honey singh badshah rap','india rap gully boy songs'] },
  { id:'lofi',     title:'Lo-Fi Chill',         type:'cards',    queries:['lofi chill beats hindi songs','lofi bollywood remix chill','lo-fi hindi songs study chill','lofi beats india relaxing'] },
  { id:'arijit',   title:'Arijit Singh',         type:'rows',     queries:['arijit singh best romantic songs','arijit singh top hits','arijit singh soulful songs','arijit singh emotional hits'] },
  { id:'workout',  title:'Energy Boost',         type:'cards',    queries:['workout hindi songs gym','upbeat dance bollywood songs','party hindi songs badshah','high energy bollywood beats'] },
  { id:'new',      title:'New Releases',          type:'cards',    queries:['new hindi songs latest','new bollywood songs released','latest hindi songs hits','new bollywood songs trending'] },
];

const genreMap = {
  all:'top bollywood songs hits', bollywood:'bollywood romantic songs hits',
  hiphop:'desi hip hop rap india', pop:'pop hits bollywood', rock:'rock songs hindi',
  indie:'indie bollywood songs', rnb:'rnb soul songs india', lofi:'lofi chill beats hindi songs',
};
const genreSections = {
  bollywood:['featured','trending','classic','arijit'],
  hiphop:   ['hiphop','trending','featured','new'],
  pop:      ['featured','new','trending','workout'],
  rock:     ['featured','trending','classic','new'],
  indie:    ['featured','lofi','trending','new'],
  rnb:      ['featured','mood','trending','new'],
  lofi:     ['lofi','mood','featured','classic'],
};
const BOLLYWOOD_META = [
  {color:'#c48c28',genre:'Romance'},{color:'#b83838',genre:'Love'},
  {color:'#9838b8',genre:'Romantic'},{color:'#3878c8',genre:'Sad Vibes'},
  {color:'#5434a8',genre:'Heartbreak'},{color:'#b82858',genre:'Dance'},
  {color:'#286c3c',genre:'Chill'},{color:'#6c4c18',genre:'Classic'},
];

function getRecentlyPlayedSongs() { return Promise.resolve(recentlyPlayed); }
function _pickQuery(sec) { return sec.queries ? sec.queries[Math.floor(Math.random() * sec.queries.length)] : sec.query; }

async function loadHomeSection(sec) {
  try {
    if (sec.fn) return await sec.fn();
    const q    = _pickQuery(sec);
    const ctrl = new AbortController();
    const to   = setTimeout(() => ctrl.abort(), 12000);
    try {
      const r = await fetch(`/api/songs?q=${encodeURIComponent(q)}`, { signal: ctrl.signal });
      clearTimeout(to);
      const d = await r.json();
      let songs = (d.results || []).filter(s => s.previewUrl);
      for (let i = songs.length - 1; i > 0; i--) { const j = Math.floor(Math.random()*(i+1)); [songs[i],songs[j]]=[songs[j],songs[i]]; }
      return songs;
    } catch(fe) {
      clearTimeout(to);
      if (sec.queries && sec.queries.length > 1) {
        const fq = sec.queries[Math.floor(Math.random() * sec.queries.length)];
        const r2 = await fetch(`/api/songs?q=${encodeURIComponent(fq)}`);
        const d2 = await r2.json();
        return (d2.results || []).filter(s => s.previewUrl);
      }
      return [];
    }
  } catch(e) { return []; }
}

function refreshHomeSections() { sectionCache = {}; haptic(15); buildHomeSections(currentGenre || 'all'); showToast('Refreshed'); }

function renderSkeletonSection(type, count = 4) {
  let html = '<div class="h-scroll-row" style="padding-right:20px;">';
  for (let i = 0; i < count; i++) {
    if      (type === 'wide') html += `<div class="wide-sk"><div class="wide-sk-cover"></div><div class="wide-sk-line" style="width:80%"></div><div class="wide-sk-line" style="width:50%;margin-top:4px;"></div></div>`;
    else if (type === 'bw')   html += `<div class="bw-sk"><div class="bw-sk-cover"></div><div class="bw-sk-line w70"></div><div class="bw-sk-line w45"></div></div>`;
    else                      html += `<div class="quick-sk"><div class="quick-sk-cover"></div><div class="bw-sk-line w70" style="margin-top:8px;"></div><div class="bw-sk-line w45" style="margin-top:5px;"></div></div>`;
  }
  html += '</div>'; return html;
}

function renderRowSkeleton(count = 5) {
  let html = '';
  for (let i = 0; i < count; i++) html += `<div class="sk-row"><div class="sk-art"></div><div class="sk-info"><div class="sk-line l1"></div><div class="sk-line l2"></div></div></div>`;
  return html;
}

async function buildHomeSections(genre = 'all') {
  const container = document.getElementById('home-sections');
  container.innerHTML = '';
  currentGenre = genre;
  let sections;
  if (genre === 'all') {
    const pinned = [SECTION_POOL[0], SECTION_POOL[1]];
    const rest   = SECTION_POOL.slice(2).sort(() => Math.random() - 0.5).slice(0, 6);
    sections = [...pinned, ...rest];
  } else {
    const ids = genreSections[genre] || ['featured','trending','new','classic'];
    sections  = ids.map(id => SECTION_POOL.find(s => s.id === id)).filter(Boolean);
  }

  sections.forEach(sec => {
    const wrap = document.createElement('div'); wrap.className = 'section'; wrap.id = 'sec-wrap-' + sec.id;
    if (sec.id === 'recent' && !recentlyPlayed.length) wrap.style.display = 'none';
    const type      = sec.type === 'featured' ? 'cards' : sec.type;
    const typeCount = type === 'bw' ? 5 : type === 'wide' ? 5 : type === 'rows' ? 0 : 5;
    wrap.innerHTML  = `<div class="section-head"><h2>${sec.title}</h2><span onclick="refreshSection('${sec.id}')">Refresh</span></div><div id="sec-${sec.id}">${type === 'rows' ? renderRowSkeleton() : renderSkeletonSection(type, typeCount)}</div>`;
    container.appendChild(wrap);
    _renderSection(sec, wrap);
  });
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
    songs.slice(0, 12).forEach((s, i) => el.appendChild(makeSongRow(s, i, songs)));
  } else if (type === 'wide') {
    const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
    songs.slice(0, 8).forEach((s, i) => row.appendChild(makeWideCard(s, i, songs))); el.appendChild(row);
  } else if (type === 'bw') {
    const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
    songs.slice(0, 8).forEach((s, i) => row.appendChild(makeBwCard(s, i, songs, BOLLYWOOD_META[i % BOLLYWOOD_META.length]))); el.appendChild(row);
  } else {
    const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
    songs.slice(0, 8).forEach((s, i) => row.appendChild(makeQuickCard(s, i, songs))); el.appendChild(row);
  }
}

async function refreshSection(secId) {
  const sec  = SECTION_POOL.find(s => s.id === secId); if (!sec) return;
  const wrap = document.getElementById('sec-wrap-' + secId); if (!wrap) return;
  const el   = document.getElementById('sec-' + secId);       if (!el) return;
  const type = sec.type === 'featured' ? 'cards' : sec.type;
  el.innerHTML = type === 'rows' ? renderRowSkeleton() : renderSkeletonSection(type, 5);
  haptic(10);
  _renderSection(sec, wrap);
}

function renderQuickResume() {
  const wrap = document.getElementById('sec-wrap-recent');
  const el   = document.getElementById('sec-recent');
  if (!el) return;
  if (!recentlyPlayed.length) { if (wrap) wrap.style.display = 'none'; return; }
  if (wrap) wrap.style.display = '';
  el.innerHTML = '';
  const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
  recentlyPlayed.slice(0, 8).forEach((s, i) => row.appendChild(makeWideCard(s, i, recentlyPlayed)));
  el.appendChild(row);
}

// ─── CARD MAKERS ─────────────────────────────────────────────────────────────
function makeQuickCard(s, i, queue) {
  const div = document.createElement('div'); div.className = 'quick-card anim-in';
  div.style.animationDelay = (i * 0.05) + 's';
  const img = document.createElement('img'); img.alt = esc(s.trackName); img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, '400x400'));
  div.appendChild(img);
  const info = document.createElement('div'); info.className = 'quick-card-info';
  info.innerHTML = `<div class="quick-card-title">${esc(s.trackName)}</div><div class="quick-card-artist">${esc(s.artistName)}</div>`;
  div.appendChild(info);
  if (isTV) div.tabIndex = 0;
  div.onclick = () => playSongs(queue, i); return div;
}

function makeWideCard(s, i, queue) {
  const div   = document.createElement('div'); div.className = 'wide-card anim-in';
  div.style.animationDelay = (i * 0.05) + 's';
  const cover = document.createElement('div'); cover.className = 'wide-card-cover';
  const img   = document.createElement('img'); img.alt = esc(s.trackName); img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, '400x400'));
  const play = document.createElement('div'); play.className = 'wide-card-play';
  play.innerHTML = '<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3" fill="white"/></svg>';
  cover.appendChild(img); cover.appendChild(play);
  const info = document.createElement('div'); info.className = 'wide-card-info';
  info.innerHTML = `<div class="wide-card-title">${esc(s.trackName)}</div><div class="wide-card-sub">${esc(s.artistName)}</div>`;
  div.appendChild(cover); div.appendChild(info);
  if (isTV) div.tabIndex = 0;
  div.onclick = () => playSongs(queue, i); return div;
}

function makeBwCard(s, i, queue, meta) {
  meta = meta || { color:'#b89640', genre:'Music' };
  const div   = document.createElement('div'); div.className = 'bw-card anim-in';
  div.style.animationDelay = (i * 0.05) + 's';
  const cover = document.createElement('div'); cover.className = 'bw-card-cover';
  const img   = document.createElement('img'); img.alt = esc(s.trackName); img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, '400x400'));
  cover.appendChild(img);
  const overlay = document.createElement('div'); overlay.className = 'bw-card-overlay';
  overlay.innerHTML = `<div class="bw-card-genre" style="color:${meta.color}">${meta.genre}</div><div class="bw-card-title">${esc(s.trackName)}</div><div class="bw-card-sub">${esc(s.artistName)}</div>`;
  cover.appendChild(overlay);
  const info = document.createElement('div'); info.className = 'bw-card-info';
  info.innerHTML = `<div class="bw-card-name">${esc(s.trackName)}</div><div class="bw-card-artist">${esc(s.artistName)}</div>`;
  div.appendChild(cover); div.appendChild(info);
  if (isTV) div.tabIndex = 0;
  div.onclick = () => playSongs(queue, i); return div;
}

function makeSongRow(s, i, queue) {
  const row = document.createElement('div'); row.className = 'song-row anim-in';
  row.dataset.trackId = s.trackId;
  row.style.animationDelay = (i * 0.034) + 's';
  const dur = s.trackTimeMillis ? formatMs(s.trackTimeMillis) : '';
  row.dataset.dur = dur;

  const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, '300x300'));
  row.appendChild(img);

  const info = document.createElement('div'); info.className = 'song-row-info';
  info.innerHTML = `<div class="song-row-title">${esc(s.trackName)}</div><div class="song-row-artist">${esc(s.artistName)}</div>`;
  row.appendChild(info);

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
  row.onclick = () => { playSongs(queue, i); haptic(8); };
  row._song = s;

  if (!isTV) {
    let pt;
    row.addEventListener('pointerdown', () => {
      pt = setTimeout(() => { row.classList.add('long-press-active'); haptic([20,40,20]); openSongModal(s); setTimeout(() => row.classList.remove('long-press-active'), 300); }, 480);
    });
    row.addEventListener('pointerup',     () => clearTimeout(pt));
    row.addEventListener('pointercancel', () => clearTimeout(pt));
  }
  return row;
}

// ─── GENRE FILTER ────────────────────────────────────────────────────────────
function filterHome(genre, chip) {
  document.querySelectorAll('#home-chips .chip').forEach(c => c.classList.remove('active'));
  chip.classList.add('active', 'popping');
  chip.addEventListener('animationend', () => chip.classList.remove('popping'), { once: true });
  haptic(8); buildHomeSections(genre);
}

// ─── SEARCH ──────────────────────────────────────────────────────────────────
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

document.getElementById('search-input').addEventListener('focus', function() { if (!this.value.trim()) renderSearchIdle(); });
document.getElementById('search-input').addEventListener('input', function() {
  const v = this.value.trim();
  document.getElementById('search-clear').style.display = v ? 'flex' : 'none';
  clearTimeout(_searchTimeout);
  if (!v) { renderSearchIdle(); return; }
  showSearchSkeleton();
  _searchTimeout = setTimeout(() => doSearch(v), 360);
});

function clearSearch() {
  document.getElementById('search-input').value = '';
  document.getElementById('search-clear').style.display = 'none';
  clearTimeout(_searchTimeout); renderSearchIdle();
}

function _saveSearchToStorage(searches) {
  localStorage.setItem('aurum_recent', JSON.stringify(searches));
}

function renderSearchIdle() {
  let html = '';
  if (recentSearches.length) {
    html += `<div class="recent-section"><div class="recent-head"><h4>Recent</h4><button onclick="clearAllRecent()">Clear</button></div><div class="recent-chips-wrap">`;
    recentSearches.forEach((q, i) => {
      html += `<div class="recent-chip" onclick="tapRecentSearch('${esc(q)}')">${esc(q)}<button class="rc-rm" onclick="removeRecent(event,${i})"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>`;
    });
    html += `</div></div>`;
  }
  html += `<div class="browse-section"><div class="browse-label">Browse</div><div class="browse-grid">`;
  browseCategories.forEach(c => {
    html += `<div class="browse-card ${c.cls}" onclick="browseGenre('${c.genre}')"${isTV ? ' tabindex="0"' : ''}><div class="browse-card-label">${c.label}</div><div class="browse-card-sub">${c.sub}</div></div>`;
  });
  html += `</div></div>`;
  document.getElementById('search-body').innerHTML = html;
}

function browseGenre(genre) {
  const q = genreMap[genre] || extraGenreMap[genre] || genre;
  document.getElementById('search-input').value = q;
  document.getElementById('search-clear').style.display = 'flex';
  saveRecentSearch(q); doSearch(q);
}
function tapRecentSearch(q) { document.getElementById('search-input').value = q; document.getElementById('search-clear').style.display = 'flex'; doSearch(q); }
function saveRecentSearch(q) {
  recentSearches = recentSearches.filter(r => r.toLowerCase() !== q.toLowerCase());
  recentSearches.unshift(q);
  if (recentSearches.length > 6) recentSearches = recentSearches.slice(0, 6);
  _saveSearchToStorage(recentSearches);
}
function removeRecent(e, i) { e.stopPropagation(); recentSearches.splice(i, 1); _saveSearchToStorage(recentSearches); renderSearchIdle(); }
function clearAllRecent() { recentSearches = []; _saveSearchToStorage(recentSearches); renderSearchIdle(); }

function showSearchSkeleton() {
  let html = '<div style="padding:4px 0">';
  for (let i = 0; i < 5; i++) html += `<div class="sk-row"><div class="sk-art"></div><div class="sk-info"><div class="sk-line l1"></div><div class="sk-line l2"></div></div></div>`;
  html += '</div>';
  document.getElementById('search-body').innerHTML = html;
}

async function doSearch(q) {
  showSearchSkeleton();
  try {
    const r = await fetch(`/api/songs?q=${encodeURIComponent(q)}`);
    const d = await r.json();
    saveRecentSearch(q);
    const songs = (d.results || []).filter(s => s.previewUrl);
    renderSearchResults(songs, q);
  } catch(e) {
    document.getElementById('search-body').innerHTML = `<div class="search-placeholder"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg><h3>Something went wrong</h3><p>Check your connection and try again</p></div>`;
  }
}

function renderSearchResults(songs, q) {
  const body = document.getElementById('search-body');
  if (!songs.length) {
    body.innerHTML = `<div class="search-placeholder"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg><h3>Nothing found</h3><p>Try a different name or artist</p></div>`; return;
  }
  body.innerHTML = `<div style="font-size:11px;color:var(--text3);padding:0 24px 10px;font-weight:500;">${songs.length} results for "${esc(q)}"</div><div id="search-results-list"></div>`;
  const list = document.getElementById('search-results-list');
  songs.forEach((s, i) => list.appendChild(makeSongRow(s, i, songs)));
}

// ─── NAVIGATION ──────────────────────────────────────────────────────────────
function goPage(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'library') renderLibrary();
  if (name === 'search')  renderSearchIdle();
}

// ─── SAVE / LIBRARY ──────────────────────────────────────────────────────────
function isSaved(song) { return savedSongs.some(s => String(s.trackId) === String(song.trackId)); }
function toggleSaveCurrentTrack() { if (!currentTrack) return; toggleSave(currentTrack); updateSaveBtn(); }
function toggleSave(song) {
  if (isSaved(song)) { savedSongs = savedSongs.filter(s => String(s.trackId) !== String(song.trackId)); showToast('Removed from library'); }
  else { savedSongs.push(song); showToast('Saved to library'); }
  localStorage.setItem('aurum_saved', JSON.stringify(savedSongs));
  renderLibrary(); updateSaveBtn();
}
function playLikedSongs() { if (!savedSongs.length) { showToast('Save some songs first'); return; } playSongs(savedSongs, 0); openFullscreen(); }

function switchLibTab(tab, el) {
  document.querySelectorAll('.lib-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active'); currentLibTab = tab;
  document.getElementById('lib-playlists').style.display  = tab === 'playlists'  ? '' : 'none';
  document.getElementById('lib-saved').style.display      = tab === 'saved'      ? '' : 'none';
  const dlEl = document.getElementById('lib-downloads');
  if (dlEl) dlEl.style.display = tab === 'downloads' ? '' : 'none';
  haptic(8); renderLibrary();
}

function renderLibrary() {
  renderPlaylists(); renderSavedSongs(); renderDownloadedSongs();
  const lc = document.getElementById('liked-count');
  if (lc) lc.textContent = savedSongs.length ? savedSongs.length + ' song' + (savedSongs.length !== 1 ? 's' : '') : 'Nothing saved yet';
  const st = document.getElementById('saved-tab');
  if (st) st.textContent = 'Liked' + (savedSongs.length ? ` (${savedSongs.length})` : '');
  const dlMeta = JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]');
  const dt = document.getElementById('dl-tab');
  if (dt) dt.textContent = 'Downloads' + (dlMeta.length ? ` (${dlMeta.length})` : '');
}

function renderPlaylists() {
  const grid = document.getElementById('playlist-grid'); if (!grid) return;
  grid.innerHTML = '';
  if (!playlists.length) return;
  playlists.forEach((pl, i) => {
    const card  = document.createElement('div'); card.className = 'playlist-card';
    if (isTV) card.tabIndex = 0;
    card.onclick = () => openPlaylistDetail(i);
    const songs = pl.songs || [];
    const coverWrap = document.createElement('div'); coverWrap.className = 'playlist-card-cover';
    if (songs.length >= 4) {
      const grid4 = document.createElement('div'); grid4.className = 'playlist-card-grid';
      songs.slice(0, 4).forEach(s => {
        const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
        setImgSrc(img, getArtUrl(s, '300x300')); grid4.appendChild(img);
      });
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
    card.appendChild(opts);
    grid.appendChild(card);
  });
}

function renderSavedSongs() {
  const list = document.getElementById('saved-songs-list'); if (!list) return;
  list.innerHTML = '';
  if (!savedSongs.length) {
    list.innerHTML = `<div class="empty-library"><svg viewBox="0 0 24 24"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg><h3>No liked songs yet</h3><p>Tap the heart on any song to save it here</p></div>`; return;
  }
  savedSongs.forEach((s, i) => list.appendChild(makeSongRow(s, i, savedSongs)));
}

// ─── DOWNLOADS (IndexedDB) ────────────────────────────────────────────────────
const DL_DB_NAME = 'aurum_downloads'; const DL_DB_VER = 1; let _dlDb = null;

function openDlDb() {
  if (_dlDb) return Promise.resolve(_dlDb);
  return new Promise((res, rej) => {
    const req = indexedDB.open(DL_DB_NAME, DL_DB_VER);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('songs')) db.createObjectStore('songs', { keyPath:'trackId' });
    };
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

// ─── PERSISTENT STORAGE REQUEST ──────────────────────────────────────────────
async function requestPersistentStorage() {
  if (!navigator.storage?.persist) return;
  const already = await navigator.storage.persisted();
  if (already) return;
  const granted = await navigator.storage.persist();
  if (!granted) showToast('Tip: Add to Home Screen for permanent downloads');
}

async function downloadSongOffline(song, customUrl, customQuality) {
  const url     = customUrl || (_currentSaavnUrl && currentQuality === 'full' ? _currentSaavnUrl : null) || song.previewUrl;
  const quality = customQuality || _currentSaavnQuality || 'preview';
  showToast('Saving offline…');
  try {
    const r = await fetch(url); if (!r.ok) throw new Error('fetch failed');
    const blob = await r.blob();
    await saveToDb({ ...song, _quality: quality }, blob);
    const metas = JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]').filter(s => String(s.trackId) !== String(song.trackId));
    metas.unshift({ trackId:song.trackId, trackName:song.trackName, artistName:song.artistName, artworkUrl100:song.artworkUrl100, _quality:quality, _savedAt:Date.now() });
    localStorage.setItem('aurum_dl_meta', JSON.stringify(metas));
    haptic([20,50,20]); showToast('Saved offline ✓'); renderLibrary();
  } catch(e) { showToast('Download failed — try again'); }
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
      if (_lastObjectUrl) { URL.revokeObjectURL(_lastObjectUrl); _lastObjectUrl = null; }
      const url = URL.createObjectURL(rec._blob);
      _lastObjectUrl = url;
      audio.pause(); audio.src = url; audio.load();
      audio.play().then(() => {
        isPlaying = true; currentTrack = rec; currentQuality = 'full';
        _miniPlayerDismissed = false;
        updatePlayerUI(); showMiniPlayer();
      }).catch(() => {});
    };
  } catch(e) { showToast('Cannot play'); }
}

async function deleteDownload(trackId) {
  await deleteFromDb(trackId);
  const metas = JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]').filter(s => String(s.trackId) !== String(trackId));
  localStorage.setItem('aurum_dl_meta', JSON.stringify(metas));
  haptic(15); showToast('Removed'); renderLibrary();
}

function renderDownloadedSongs() {
  const list = document.getElementById('downloaded-songs-list'); if (!list) return;
  list.innerHTML = '';
  const songs = JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]');
  if (!songs.length) {
    list.innerHTML = `<div class="empty-library"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><h3>No downloads yet</h3><p>Save songs offline from the player or song menu</p></div>`; return;
  }
  const hdr = document.createElement('div');
  hdr.style.cssText = 'padding:4px 22px 10px;display:flex;align-items:center;justify-content:space-between;';
  hdr.innerHTML = `<span style="font-size:11px;color:var(--text3);font-weight:600;">${songs.length} song${songs.length!==1?'s':''} saved offline</span><button style="font-size:11px;color:var(--text3);background:none;border:none;cursor:pointer;font-family:Sora,sans-serif;" onclick="confirmClearDownloads()">Clear all</button>`;
  list.appendChild(hdr);
  songs.forEach(s => {
    const row = document.createElement('div'); row.className = 'song-row anim-in'; row.dataset.trackId = s.trackId;
    if (isTV) row.tabIndex = 0;
    const img = document.createElement('img'); img.alt=''; img.loading='lazy';
    setImgSrc(img, getArtUrl(s, '300x300')); row.appendChild(img);
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
  });
}

function confirmClearDownloads() {
  if (!confirm('Remove all downloaded songs?')) return;
  openDlDb().then(db => {
    const tx = db.transaction('songs', 'readwrite');
    tx.objectStore('songs').clear();
    tx.oncomplete = () => { localStorage.removeItem('aurum_dl_meta'); renderLibrary(); showToast('Downloads cleared'); };
  });
}

// ─── PLAYLIST DETAIL ─────────────────────────────────────────────────────────
function openPlaylistDetail(i) {
  currentPlaylistIndex = i; const pl = playlists[i];
  document.getElementById('pl-detail-name').textContent  = pl.name;
  document.getElementById('pl-detail-title').textContent = pl.name;
  const songs = pl.songs || [];
  document.getElementById('pl-detail-sub').textContent   = songs.length + ' songs';
  const coverEl = document.getElementById('pl-big-cover');
  if (!songs.length) {
    const emptyDiv = document.createElement('div'); emptyDiv.id = 'pl-big-cover';
    emptyDiv.style.cssText = 'width:100%;max-width:248px;aspect-ratio:1;border-radius:20px;background:var(--surface2);display:flex;align-items:center;justify-content:center;';
    coverEl.replaceWith(emptyDiv);
  } else if (songs.length < 4) {
    const img = document.createElement('img'); img.id = 'pl-big-cover'; img.className = 'pl-big-cover'; img.alt = '';
    setImgSrc(img, getArtUrl(songs[0], '500x500'));
    coverEl.replaceWith(img);
  } else {
    const g = document.createElement('div'); g.id = 'pl-big-cover'; g.className = 'pl-big-cover-grid';
    songs.slice(0, 4).forEach(s => {
      const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
      setImgSrc(img, getArtUrl(s, '300x300')); g.appendChild(img);
    });
    coverEl.replaceWith(g);
  }
  const sl = document.getElementById('pl-songs-list'); sl.innerHTML = '';
  if (!songs.length) sl.innerHTML = `<div style="text-align:center;padding:38px 22px;color:var(--text3);font-size:12px;">No songs yet — find some in Search</div>`;
  else songs.forEach((s, i) => sl.appendChild(makeSongRow(s, i, songs)));
  document.getElementById('playlist-detail').classList.add('open');
}

function closePlaylistDetail() { document.getElementById('playlist-detail').classList.remove('open'); renderLibrary(); }
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
  shuffleOn = true; document.getElementById('shuffle-btn').querySelector('svg').style.stroke = 'var(--gold-l)';
  closePlaylistDetail(); openFullscreen();
}

// ─── PLAYLIST OPTIONS ─────────────────────────────────────────────────────────
function openPlaylistOpts(e, i) {
  e.stopPropagation(); optsPlaylistIndex = i;
  document.getElementById('pl-opts-title').textContent = playlists[i]?.name || 'Playlist';
  document.getElementById('playlist-opts-modal').classList.add('open');
}
function closePlaylistOpts(e) {
  if (e && !e.target.closest) return;
  if (e && e.target.closest('.modal-sheet')) return;
  document.getElementById('playlist-opts-modal').classList.remove('open'); optsPlaylistIndex = null;
}
function openRenameModal() {
  document.getElementById('playlist-opts-modal').classList.remove('open');
  if (optsPlaylistIndex === null) return;
  document.getElementById('rename-input').value = playlists[optsPlaylistIndex].name || '';
  document.getElementById('rename-modal').classList.add('open');
  setTimeout(() => document.getElementById('rename-input').focus(), 360);
}
function closeRenameModal()  { document.getElementById('rename-modal').classList.remove('open'); }
function confirmRename() {
  const name = document.getElementById('rename-input').value.trim();
  if (!name || optsPlaylistIndex === null) { showToast('Enter a name'); return; }
  playlists[optsPlaylistIndex].name = name;
  localStorage.setItem('aurum_playlists', JSON.stringify(playlists));
  closeRenameModal(); renderPlaylists(); showToast('Renamed');
}
function confirmDeletePlaylist() {
  if (optsPlaylistIndex === null) return;
  const name = playlists[optsPlaylistIndex].name;
  playlists.splice(optsPlaylistIndex, 1);
  localStorage.setItem('aurum_playlists', JSON.stringify(playlists));
  document.getElementById('playlist-opts-modal').classList.remove('open');
  optsPlaylistIndex = null; renderPlaylists(); showToast(`"${name}" deleted`);
}

// ─── CREATE PLAYLIST ─────────────────────────────────────────────────────────
function openCreatePlaylist() {
  document.getElementById('create-playlist-modal').classList.add('open');
  setTimeout(() => document.getElementById('playlist-name-input').focus(), 360);
}
function closeCreatePlaylist() {
  document.getElementById('create-playlist-modal').classList.remove('open');
  document.getElementById('playlist-name-input').value = '';
}
function createPlaylist() {
  const name = document.getElementById('playlist-name-input').value.trim();
  if (!name) { showToast('Enter a playlist name'); return; }
  playlists.push({ name, songs:[] });
  localStorage.setItem('aurum_playlists', JSON.stringify(playlists));
  closeCreatePlaylist(); renderPlaylists(); showToast('"' + name + '" created');
}

// ─── SONG MODAL ──────────────────────────────────────────────────────────────
function openSongModal(song) {
  if (!song) return; modalTrack = song;
  const art = document.getElementById('modal-song-art');
  if (art) setImgSrc(art, getArtUrl(song, '300x300'));
  document.getElementById('modal-song-title').textContent  = song.trackName  || 'Unknown';
  document.getElementById('modal-song-artist').textContent = song.artistName || 'Unknown';
  document.getElementById('modal-save-label').textContent  = isSaved(song) ? 'Remove from Library' : 'Save to Library';
  document.getElementById('song-modal').classList.add('open');
}
function closeSongModal(e) {
  if (e && e.target.closest?.('.modal-sheet')) return;
  document.getElementById('song-modal').classList.remove('open'); modalTrack = null;
}
function modalSave() {
  if (!modalTrack) return;
  toggleSave(modalTrack);
  document.getElementById('modal-save-label').textContent = isSaved(modalTrack) ? 'Remove from Library' : 'Save to Library';
  document.getElementById('song-modal').classList.remove('open'); modalTrack = null;
}
function playNext() {
  if (!modalTrack) return;
  currentQueue.splice(currentIndex + 1, 0, modalTrack);
  showToast('Playing next');
  document.getElementById('song-modal').classList.remove('open'); modalTrack = null;
  updateQueuePanel();
}
function modalDownload() {
  if (!modalTrack) return;
  const s = modalTrack;
  document.getElementById('song-modal').classList.remove('open');
  _downloadSong = s; modalTrack = null;
  openDownloadModal();
}

// ─── ADD TO PLAYLIST ─────────────────────────────────────────────────────────
function openAddToPlaylistModal() {
  const songToAdd = modalTrack;
  document.getElementById('song-modal').classList.remove('open');
  const opts = document.getElementById('add-playlist-options'); opts.innerHTML = '';
  if (!playlists.length) {
    opts.innerHTML = `<div style="padding:12px 0;text-align:center;color:var(--text3);font-size:12px;">No playlists yet.</div>`;
  } else {
    playlists.forEach((pl, i) => {
      const div = document.createElement('div'); div.className = 'modal-option';
      div.innerHTML = `<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg><span>${esc(pl.name)} <span style="color:var(--text3);font-size:10px;">(${pl.songs.length})</span></span>`;
      div.onclick = () => addToPlaylist(i, songToAdd); opts.appendChild(div);
    });
  }
  document.getElementById('add-playlist-modal').classList.add('open');
}
function closeAddToPlaylistModal(e) {
  if (e && e.target.closest?.('.modal-sheet')) return;
  document.getElementById('add-playlist-modal').classList.remove('open');
}
function addToPlaylist(i, song) {
  const s = song || modalTrack; if (!s) return;
  const pl = playlists[i];
  if (pl.songs.some(x => String(x.trackId) === String(s.trackId))) {
    showToast('Already in "' + pl.name + '"');
  } else {
    pl.songs.push(s);
    localStorage.setItem('aurum_playlists', JSON.stringify(playlists));
    showToast('Added to "' + pl.name + '"');
  }
  document.getElementById('add-playlist-modal').classList.remove('open');
  modalTrack = null;
}

// ─── QUALITY MODAL ───────────────────────────────────────────────────────────
function openQualitySheet() {
  if (!currentTrack) { showToast('Play a song first'); return; }
  const sub = document.getElementById('qs-track-name');
  if (sub) sub.textContent = `${currentTrack.trackName || 'Unknown'} · ${currentTrack.artistName || 'Unknown'}`;
  updateQualityLabel();
  document.getElementById('quality-modal').classList.add('open');
}
function closeQualitySheet(e) {
  if (e && e.target.closest?.('.quality-sheet')) return;
  document.getElementById('quality-modal').classList.remove('open');
}
function selectQuality(q) {
  if (q === 'preview') { _fallbackToPreview(currentTrack); document.getElementById('quality-modal').classList.remove('open'); }
  else { if (_fullSongAbort) { _fullSongAbort.abort(); _fullSongAbort = null; } _autoFetchFullSong(currentTrack); document.getElementById('quality-modal').classList.remove('open'); }
}

// ─── DOWNLOAD ─────────────────────────────────────────────────────────────────
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
    if (desc)  desc.textContent  = currentQuality === 'loading' ? 'Fetching stream…' : 'Play song first';
    if (badge) { badge.textContent = '—'; badge.className = 'dl-kbps-badge b128'; }
  }
  document.getElementById('download-modal').classList.add('open');
}
function closeDownloadModal(e) {
  if (e && e.target.closest?.('.dl-sheet')) return;
  document.getElementById('download-modal').classList.remove('open'); _downloadSong = null;
}

async function triggerDownload(quality) {
  const song = _downloadSong || currentTrack; _downloadSong = null;
  if (!song) { showToast('No track selected'); return; }
  document.getElementById('download-modal').classList.remove('open');

  if (quality === 'preview' || quality === 'ringtone') {
    try {
      showToast(quality === 'ringtone' ? 'Saving ringtone…' : 'Saving preview…');
      const res = await fetch(song.previewUrl); if (!res.ok) throw new Error();
      const blob    = await res.blob();
      const objUrl  = URL.createObjectURL(blob);
      const a       = document.createElement('a'); a.href = objUrl;
      a.download    = (song.trackName||'audio').replace(/[/\?%*:|"<>]/g,'-') + (quality === 'ringtone' ? '_ringtone' : '_preview') + '.m4a';
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(objUrl), 5000);
      showToast(quality === 'ringtone' ? 'Ringtone saved ✓' : 'Preview saved ✓');
      haptic([10,30,10]);
    } catch(e) { showToast('Download failed'); }

  } else if (quality === 'full') {
    if (!_currentSaavnUrl) { showToast('Play full song first'); return; }
    await downloadSongOffline(song, _currentSaavnUrl);

  } else if (quality === 'gift') {
    showToast('Fetching 320 kbps…');
    try {
      const cleanTitle  = (song.trackName  || '').replace(/\(.*?\)/g,'').trim();
      const cleanArtist = (song.artistName || '').split(/[&,]/)[0].trim();
      const q = encodeURIComponent(`${cleanTitle} ${cleanArtist}`);
      const r = await fetch(`/api/saavn?q=${q}&artist=${encodeURIComponent(cleanArtist)}`);
      const d = await r.json();
      if (!d.success || !d.url) {
        showToast('320 kbps not available');
        if (_currentSaavnUrl) await downloadSongOffline(song, _currentSaavnUrl);
        return;
      }
      if (!_titleMatches(d.title, song.trackName)) { showToast('Song mismatch — aborted'); return; }
      const proxyUrl = `/api/stream?url=${encodeURIComponent(d.url)}`;
      await downloadSongOffline(song, proxyUrl, d.quality);
    } catch(e) { showToast('Owner Gift failed'); }
  }
}

// ─── TOAST ────────────────────────────────────────────────────────────────────
let _toastTimer = null;
function showToast(msg) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

// ─── UTILS ────────────────────────────────────────────────────────────────────
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function formatMs(ms)  { const s = Math.floor((ms||0)/1000); return `${Math.floor(s/60)}:${(s%60).toString().padStart(2,'0')}`; }
function formatSec(s)  { s = Math.floor(s||0); return `${Math.floor(s/60)}:${(s%60).toString().padStart(2,'0')}`; }
function haptic(pat)   { try { if (navigator.vibrate && (typeof appSettings === 'undefined' || appSettings.hapticFeedback !== false)) navigator.vibrate(pat); } catch(e) {} }

if (!isTV) {
  document.addEventListener('keydown', e => {
    if (e.code === 'Space' && e.target.tagName !== 'INPUT') { e.preventDefault(); togglePlay(); }
  });
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  // --vh: set once now, then on resize/rotation (Blank Fix)
  setVh();

  // Theme color sync
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  function syncThemeColor(isLight) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = isLight ? '#f9f6f0' : '#050508';
  }
  mq.addEventListener('change', e => syncThemeColor(e.matches));
  syncThemeColor(mq.matches);

  if (isLowEnd) document.documentElement.classList.add('low-end');
  if (isTV)     document.documentElement.classList.add('is-tv');

  requestPersistentStorage();

  if (!isTV) {
    setupMiniGesture();
    setupFullPlayerGesture();
    setupArtSwipeGesture();
  }

  if (isTV) setupTVNavigation();

  // initViz just builds the bar DOM — does NOT start the loop
  initViz();

  buildHomeSections('all');
  renderSearchIdle();
  renderLibrary();

  const vs = document.getElementById('fp-vol-slider');
  if (vs) vs.style.setProperty('--vol', '100%');

  // Orbs: activate on first real touch (not on TV)
  if (!isTV && !isLowEnd) {
    document.addEventListener('touchstart', () => {
      document.getElementById('ambient-canvas')?.classList.add('orbs-active');
    }, { once: true, passive: true });
  }

  // TV: monkey-patch playSongs to auto-open fullscreen
  if (isTV) {
    const origPlaySongs = playSongs;
    window.playSongs = function(queue, index) {
      origPlaySongs(queue, index);
      setTimeout(() => openFullscreen(), 100);
    };
  }
});

// ─── SERVICE WORKER ───────────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js').catch(() => {});
  });
}

// ─── SETTINGS BRIDGE ─────────────────────────────────────────────────────────
window._getAurumAudio = () => audio;

function _applyVolumeNormalization() {
  if (typeof appSettings === 'undefined') return;
  if (appSettings.volumeNormalize) audio.volume = Math.min(audio.volume, 0.85);
}

// ─── GAPLESS PLAYBACK ────────────────────────────────────────────────────────
let _gaplessBuffer = null;
audio.addEventListener('timeupdate', () => {
  if (typeof appSettings === 'undefined' || !appSettings.gaplessPlayback) return;
  if (!currentQueue.length || currentIndex >= currentQueue.length - 1) return;
  const timeLeft = isFinite(audio.duration) ? audio.duration - audio.currentTime : 999;
  if (timeLeft < 8 && !_gaplessBuffer) {
    const next = currentQueue[currentIndex + 1];
    if (next?.previewUrl) {
      _gaplessBuffer = new Audio();
      _gaplessBuffer.preload = 'auto';
      _gaplessBuffer.src = next.previewUrl;
      _gaplessBuffer.load();
    }
  }
});
audio.addEventListener('ended', () => { _gaplessBuffer = null; });
