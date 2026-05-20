// ═══════════════════════════════════════════════════════════════
// tv-perf.js — Load BEFORE app.js
// TV pe sirf zaruri features, baaki sab COMPLETELY KILL
// Zero memory leak, zero background activity
// ═══════════════════════════════════════════════════════════════

(function () {

  // ── 1. TV DETECTION ──────────────────────────────────────────
  const w = window.innerWidth || screen.width;
  const h = window.innerHeight || screen.height;
  const ua = navigator.userAgent || '';

  const isTV = (
    w >= 1280 && !window.matchMedia('(pointer:fine)').matches
  ) || (
    w >= 1920
  ) || (
    /SmartTV|SMART-TV|WebOS|Tizen|BRAVIA|HbbTV|TV Browser|Viera|Vidaa/i.test(ua)
  );

  if (!isTV) return; // Mobile hai — exit immediately

  // ── 2. GLOBAL FLAGS ──────────────────────────────────────────
  window.__IS_TV__ = true;
  window.__TV_SAFE_MODE__ = true;

  // ── 3. CSS INJECT (repaint se pehle) ────────────────────────
  const style = document.createElement('style');
  style.id = 'tv-perf-css';
  style.textContent = `
    *, *::before, *::after {
      backdrop-filter: none !important;
      -webkit-backdrop-filter: none !important;
      animation: none !important;
      animation-duration: 0.01ms !important;
      animation-delay: 0ms !important;
      transition-duration: 0.08s !important;
    }
    #ambient-canvas, #fp-ambient-glow, .fp-visualizer, #fp-visualizer,
    .ph-ambient, .ph-orb-a, .ph-orb-b, .ph-noise, .orb, .anim-in {
      display: none !important;
    }
    #fp-bg-art {
      filter: blur(6px) brightness(0.20) !important;
      transform: none !important;
      transition: opacity 0.3s ease !important;
    }
    #mini-player, .quick-card, .wide-card, .bw-card, .song-row img, .fp-play-circle, .pl-big-cover {
      box-shadow: none !important;
      filter: none !important;
    }
    #fp-bg { filter: none !important; }
    .now-playing-bar span, .queue-now-playing span {
      animation: none !important;
      transform: scaleY(0.6) !important;
    }
    .bw-sk-cover, .bw-sk-line, .sk-art, .sk-line, .wide-sk-cover, .wide-sk-line, .quick-sk-cover {
      animation: none !important;
      opacity: 0.5 !important;
    }
    .quick-card:active, .bw-card:active, .wide-card:active, .song-row:active, .playlist-card:active {
      transform: none !important;
    }
    .fp-track-title.marquee-active span, .fp-artist.marquee-active span {
      animation: none !important;
    }
    img { transition: opacity 0.1s ease !important; }
    *:focus { outline: 2px solid #c8a858 !important; outline-offset: 2px !important; }
    ::-webkit-scrollbar { display: none !important; }
  `;

  (document.head || document.documentElement).appendChild(style);

  // ── 4. KILL BACKGROUND PROCESSES ────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    
    try { window.isTV = true; window.isLowEnd = true; } catch (e) {}

    try {
      if (typeof _stopViz === 'function') _stopViz();
      if (window.vizRaf) { cancelAnimationFrame(window.vizRaf); window.vizRaf = null; }
      window._startViz = function () {};
      window.tickViz = function () {};
      window.initViz = function () {};
      const vizEl = document.getElementById('fp-visualizer');
      if (vizEl) vizEl.innerHTML = '';
    } catch (e) {}

    try {
      const ac = document.getElementById('ambient-canvas');
      if (ac) { ac.style.display = 'none'; ac.width = 1; ac.height = 1; ac.innerHTML = ''; }
    } catch (e) {}

    try {
      if (window.imgObserver) {
        window.imgObserver.disconnect();
        window.imgObserver = null;
      }
      window.setImgSrc = function (img, src) {
        if (!src) { img.src = window.IMG_PLACEHOLDER || ''; return; }
        img.classList.remove('loaded', 'img-error');
        img.onerror = function () {
          if (this.src !== window.IMG_PLACEHOLDER) this.src = window.IMG_PLACEHOLDER || '';
          this.classList.add('img-error', 'loaded');
          this.onerror = null;
        };
        img.onload = function () { this.classList.add('loaded'); };
        img.src = src;
        if (img.complete && img.naturalWidth > 0) img.classList.add('loaded');
      };
    } catch (e) {}

    try { window.getArtUrl = function (song, size) { return ((song && song.artworkUrl100) || '').replace('100x100', '300x300'); }; } catch (e) {}
    try { window.fetchRecommendations = function () {}; window._autoFetchFullSong = function () {}; } catch (e) {}

    try {
      if (window._bgPingInterval) {
        clearInterval(window._bgPingInterval);
        window._bgPingInterval = setInterval(function () {
          if (!window.isPlaying) return;
          try {
            if (window._bgAudioCtx) {
              const buf = window._bgAudioCtx.createBuffer(1, 1, 22050);
              const src = window._bgAudioCtx.createBufferSource();
              src.buffer = buf;
              src.connect(window._bgAudioCtx.destination);
              src.start(0);
            }
          } catch (e) {}
        }, 120000);
      }
    } catch (e) {}

    try {
      setInterval(function () {
        if (typeof sectionCache !== 'undefined') {
          const keep = new Set(['recent', 'featured']);
          Object.keys(sectionCache).forEach(function (k) {
            if (!keep.has(k)) delete sectionCache[k];
          });
        }
      }, 15000);
    } catch (e) {}

    // ── DATA SAVER ──────────────────────────────────────────────
    window.__TV_DATA_SAVER__ = true;

    // ── DYNAMIC LOGO COLOR ──────────────────────────────────────
    function _setLogoDynamicColor(artUrl) {
      if (!artUrl) return;
      try {
        const img = new Image();
        img.crossOrigin = 'anonymous';
        img.onload = function () {
          const canvas = document.createElement('canvas');
          canvas.width = 1; canvas.height = 1;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, 1, 1);
          const data = ctx.getImageData(0, 0, 1, 1).data;
          const r = data[0], g = data[1], b = data[2];
          const logo = document.querySelector('.ph-app-name');
          if (logo) logo.style.color = `rgb(${r},${g},${b})`;
          canvas.width = canvas.height = 0;
        };
        img.src = artUrl;
      } catch (e) {}
    }

    // ── SLEEP TIMER ─────────────────────────────────────────────
    window.__SLEEP_TIMER__ = null;
    window.__SLEEP_REMAINING__ = 0;

    function _setSleepTimer(minutes) {
      if (window.__SLEEP_TIMER__) clearInterval(window.__SLEEP_TIMER__);
      window.__SLEEP_REMAINING__ = minutes * 60;
      if (typeof showToast === 'function') showToast('Sleep timer: ' + minutes + ' min');

      window.__SLEEP_TIMER__ = setInterval(function () {
        window.__SLEEP_REMAINING__--;
        if (window.__SLEEP_REMAINING__ <= 0) {
          clearInterval(window.__SLEEP_TIMER__);
          if (window.audio) window.audio.pause();
          if (typeof showToast === 'function') showToast('Goodnight! 😴');
          return;
        }
        if (window.__SLEEP_REMAINING__ % 300 === 0) {
          const min = Math.floor(window.__SLEEP_REMAINING__ / 60);
          if (typeof showToast === 'function') showToast('Sleep in ' + min + ' min');
        }
      }, 1000);
    }

    // ── TV NAVIGATION ───────────────────────────────────────────
    function _setupTVNav() {
      document.documentElement.classList.add('is-tv');
      document.body.classList.add('is-tv');
      const mp = document.getElementById('mini-player');
      if (mp) mp.style.display = 'none';

      window.showMiniPlayer = function () {
        const fp = document.getElementById('fullscreen-player');
        if (fp && !fp.classList.contains('open') && window.currentTrack) {
          if (typeof openFullscreen === 'function') openFullscreen();
        }
      };

      document.addEventListener('keydown', function (e) {
        const fp = document.getElementById('fullscreen-player');
        const fpOpen = fp && fp.classList.contains('open');
        const qp = document.getElementById('queue-panel');
        const qpOpen = qp && qp.classList.contains('open');

        switch (e.key) {
          case 'ArrowRight':
            e.preventDefault();
            if (fpOpen && typeof nextTrack === 'function') nextTrack();
            break;
          case 'ArrowLeft':
            e.preventDefault();
            if (fpOpen && typeof prevTrack === 'function') prevTrack();
            break;
          case 'ArrowUp':
            e.preventDefault();
            if (fpOpen) {
              const vol = Math.min(1, ((window.audio && window.audio.volume) || 0) + 0.1);
              if (typeof setVolume === 'function') setVolume(vol);
              if (typeof showToast === 'function') showToast('🔊 ' + Math.round(vol * 100) + '%');
            }
            break;
          case 'ArrowDown':
            e.preventDefault();
            if (fpOpen) {
              const vol = Math.max(0, ((window.audio && window.audio.volume) || 1) - 0.1);
              if (typeof setVolume === 'function') setVolume(vol);
              if (typeof showToast === 'function') showToast('🔉 ' + Math.round(vol * 100) + '%');
            }
            break;
          case 'Enter':
          case ' ':
            e.preventDefault();
            if (fpOpen && typeof togglePlay === 'function') togglePlay();
            else if (document.activeElement && document.activeElement !== document.body) document.activeElement.click();
            break;
          case 'Backspace':
          case 'GoBack':
          case 'Escape':
            e.preventDefault();
            if (qpOpen && typeof closeQueuePanel === 'function') closeQueuePanel();
            else if (fpOpen && typeof closeFullscreen === 'function') closeFullscreen();
            break;
          case 's':
          case 'S':
            e.preventDefault();
            _setSleepTimer(30);
            break;
        }
      });

      setTimeout(function () {
        const first = document.querySelector('.quick-card, .song-row, .nav-btn');
        if (first) first.focus();
      }, 800);

      setInterval(function () {
        if (window.currentTrack && window.__TV_SAFE_MODE__) {
          const artUrl = (window.currentTrack.artworkUrl100 || '').replace('100x100', '300x300');
          _setLogoDynamicColor(artUrl);
        }
      }, 5000);
    }

    _setupTVNav();

    console.log('[tv-perf] ✅ TV ACTIVE — Zero memory leak, Zero background');
    console.log('[tv-perf] 🎮 Remote: S=Sleep(30min), Arrows=Nav, Enter=Play');

  });

})();
