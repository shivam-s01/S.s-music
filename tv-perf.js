// ═══════════════════════════════════════════════════════════════
// tv-perf.js — Load BEFORE app.js
// TV pe sirf zaruri features, baaki sab kill
// ═══════════════════════════════════════════════════════════════

(function () {

  // ── 1. TV DETECTION ──────────────────────────────────────────
  // PWABuilder/WebView TV pe UA mein "TV" nahi hota
  // Screen size + pointer type se detect karo
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

  if (!isTV) return; // Mobile hai — kuch mat karo

  // ── 2. GLOBAL FLAGS — app.js se pehle set ho jayenge ─────────
  window.__IS_TV__ = true;
  // isTV aur isLowEnd ko override karo
  // (app.js mein var/let hai to ye kaam nahi karega directly,
  //  isliye document par flag rakhte hain aur CSS inject karte hain)

  // ── 3. CSS INJECT — sabse pehle, repaint se pehle ────────────
  const style = document.createElement('style');
  style.id = 'tv-perf-css';
  style.textContent = `

    /* ── Kill ALL backdrop filters ── */
    *, *::before, *::after {
      backdrop-filter: none !important;
      -webkit-backdrop-filter: none !important;
    }

    /* ── Kill ALL animations ── */
    *, *::before, *::after {
      animation: none !important;
      animation-duration: 0.01ms !important;
      animation-delay: 0ms !important;
      transition-duration: 0.08s !important;
    }

    /* ── Kill heavy visual elements ── */
    #ambient-canvas,
    #fp-ambient-glow,
    .fp-visualizer,
    #fp-visualizer,
    .ph-ambient,
    .ph-orb-a,
    .ph-orb-b,
    .ph-noise,
    .orb,
    .anim-in {
      display: none !important;
    }

    /* ── Simplified backgrounds ── */
    #fp-bg-art {
      filter: blur(6px) brightness(0.20) !important;
      transform: none !important;
      transition: opacity 0.3s ease !important;
    }

    /* ── No box shadows (expensive on TV GPU) ── */
    #mini-player,
    .quick-card,
    .wide-card,
    .bw-card,
    .song-row img,
    .fp-play-circle,
    .pl-big-cover {
      box-shadow: none !important;
      filter: none !important;
    }

    /* ── Fullscreen player bg — simple color, no blur ── */
    #fp-bg {
      filter: none !important;
    }

    /* ── Now playing bar — static, no animation ── */
    .now-playing-bar span,
    .queue-now-playing span {
      animation: none !important;
      transform: scaleY(0.6) !important;
    }

    /* ── Skeleton shimmer — kill ── */
    .bw-sk-cover, .bw-sk-line, .sk-art, .sk-line,
    .wide-sk-cover, .wide-sk-line, .quick-sk-cover {
      animation: none !important;
      opacity: 0.5 !important;
    }

    /* ── Cards — no hover transforms ── */
    .quick-card:active,
    .bw-card:active,
    .wide-card:active,
    .song-row:active,
    .playlist-card:active {
      transform: none !important;
    }

    /* ── Marquee text — off ── */
    .fp-track-title.marquee-active span,
    .fp-artist.marquee-active span {
      animation: none !important;
    }

    /* ── Next strip pulse — off ── */
    .fp-next-strip > svg {
      animation: none !important;
    }

    /* ── Chip pop — off ── */
    .chip.popping {
      animation: none !important;
    }

    /* ── Image fade-in — instant ── */
    img {
      transition: opacity 0.1s ease !important;
    }

    /* ── TV focus ring — simple ── */
    *:focus {
      outline: 2px solid #c8a858 !important;
      outline-offset: 2px !important;
    }

    /* ── Scrollbar hide ── */
    ::-webkit-scrollbar { display: none !important; }

  `;

  // <head> mein inject — DOMContentLoaded ka wait nahi
  (document.head || document.documentElement).appendChild(style);

  // ── 4. INTERCEPT — app.js ke functions override karo ─────────
  // DOMContentLoaded pe override karo (app.js ke baad)
  document.addEventListener('DOMContentLoaded', function () {

    // ── 4a. isTV aur isLowEnd force karo ──
    // app.js ne shayad already set kar diya — reassign karo
    try {
      // eslint-disable-next-line no-global-assign
      if (typeof isTV !== 'undefined') {
        // global scope mein reassign (var tha to kaam karega)
        window.isTV = true;
        window.isLowEnd = true;
      }
    } catch (e) {}

    // ── 4b. Visualizer completely kill ──
    try {
      if (typeof _stopViz === 'function') _stopViz();
      if (typeof vizRaf !== 'undefined' && vizRaf) {
        cancelAnimationFrame(vizRaf);
        window.vizRaf = null;
      }
      window._startViz = function () {}; // no-op
      window.tickViz   = function () {};
      window.initViz   = function () {};
      const vizEl = document.getElementById('fp-visualizer');
      if (vizEl) vizEl.innerHTML = '';
    } catch (e) {}

    // ── 4c. Ambient canvas kill ──
    try {
      const ac = document.getElementById('ambient-canvas');
      if (ac) { ac.style.display = 'none'; ac.width = 1; ac.height = 1; }
    } catch (e) {}

    // ── 4d. Background audio ping — reduce to 60s ──
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
        }, 60000); // 25s se badhakar 60s
      }
    } catch (e) {}

    // ── 4e. Section cache — sirf 2 sections load karo ──
    // (baki lazy load hoga jab user scroll kare)
    try {
      if (typeof SECTION_POOL !== 'undefined') {
        // TV pe sirf 4 sections — featured, trending, arijit, new
        window._tv_section_limit = 4;
      }
    } catch (e) {}

    // ── 4f. Lazy image loading — TV pe direct load ──
    // IntersectionObserver TV pe rootMargin sahi kaam nahi karta
    try {
      if (window.imgObserver) {
        window.imgObserver.disconnect();
        window.imgObserver = null;
      }
      // setImgSrc override — lazy nahi, direct load
      const _origSetImgSrc = window.setImgSrc;
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

    // ── 4g. Art quality — TV pe 300x300 kafi hai ──
    try {
      const _origGetArtUrl = window.getArtUrl;
      window.getArtUrl = function (song, size) {
        return ((song && song.artworkUrl100) || '').replace('100x100', '300x300');
      };
    } catch (e) {}

    // ── 4h. Recommendations fetch — TV pe off ──
    // Queue mein songs already hain, extra API calls wasteful
    try {
      window.fetchRecommendations = function () {};
    } catch (e) {}

    // ── 4i. Section cache — aggressive cleanup ──
    try {
      setInterval(function () {
        if (typeof sectionCache !== 'undefined') {
          const keep = new Set(['recent', 'featured']);
          Object.keys(sectionCache).forEach(function (k) {
            if (!keep.has(k)) delete sectionCache[k];
          });
        }
      }, 30000);
    } catch (e) {}

    // ── 4j. TV Navigation setup ──
    _setupTVNav();

    console.log('[tv-perf] ✅ TV mode active — all heavy features disabled');
  });

  // ── 5. TV KEYBOARD NAVIGATION ────────────────────────────────
  function _setupTVNav() {
    // Body class
    document.documentElement.classList.add('is-tv');
    document.body.classList.add('is-tv');

    // Mini player hide
    const mp = document.getElementById('mini-player');
    if (mp) mp.style.display = 'none';

    // showMiniPlayer override — fullscreen open karo instead
    window.showMiniPlayer = function () {
      const fp = document.getElementById('fullscreen-player');
      if (fp && !fp.classList.contains('open') && window.currentTrack) {
        if (typeof openFullscreen === 'function') openFullscreen();
      }
    };

    // Remote control keyboard handler
    document.addEventListener('keydown', function (e) {
      const fp  = document.getElementById('fullscreen-player');
      const fpOpen = fp && fp.classList.contains('open');
      const qp  = document.getElementById('queue-panel');
      const qpOpen = qp && qp.classList.contains('open');

      switch (e.key) {
        case 'ArrowRight':
          e.preventDefault();
          if (fpOpen && typeof nextTrack === 'function') nextTrack();
          else _tvFocus(1, 'h');
          break;

        case 'ArrowLeft':
          e.preventDefault();
          if (fpOpen && typeof prevTrack === 'function') prevTrack();
          else _tvFocus(-1, 'h');
          break;

        case 'ArrowUp':
          e.preventDefault();
          if (fpOpen) {
            const vol = Math.min(1, ((window.audio && window.audio.volume) || 0) + 0.1);
            if (typeof setVolume === 'function') setVolume(vol);
            if (typeof showToast === 'function') showToast('🔊 ' + Math.round(vol * 100) + '%');
          } else {
            _tvFocus(-1, 'v');
          }
          break;

        case 'ArrowDown':
          e.preventDefault();
          if (fpOpen) {
            const vol = Math.max(0, ((window.audio && window.audio.volume) || 1) - 0.1);
            if (typeof setVolume === 'function') setVolume(vol);
            if (typeof showToast === 'function') showToast('🔉 ' + Math.round(vol * 100) + '%');
          } else {
            _tvFocus(1, 'v');
          }
          break;

        case 'Enter':
        case ' ':
          e.preventDefault();
          if (fpOpen) {
            if (typeof togglePlay === 'function') togglePlay();
          } else if (document.activeElement && document.activeElement !== document.body) {
            document.activeElement.click();
          }
          break;

        case 'Backspace':
        case 'GoBack':
        case 'Escape':
          e.preventDefault();
          if (qpOpen && typeof closeQueuePanel === 'function') closeQueuePanel();
          else if (fpOpen && typeof closeFullscreen === 'function') closeFullscreen();
          break;
      }
    });

    // Auto-focus first element
    setTimeout(function () {
      const first = document.querySelector('.quick-card, .song-row, .nav-btn');
      if (first) first.focus();
    }, 800);
  }

  function _tvFocus(dir, axis) {
    const sel = [
      '.song-row', '.quick-card', '.bw-card', '.wide-card',
      '.browse-card', '.playlist-card', '.nav-btn', '.queue-item',
      'button:not([disabled])', '[tabindex="0"]'
    ].join(',');

    const all = Array.from(document.querySelectorAll(sel)).filter(function (el) {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.top >= 0 && r.bottom <= window.innerHeight + 100;
    });

    if (!all.length) return;
    const cur = document.activeElement;
    const idx = all.indexOf(cur);
    const next = all[Math.max(0, Math.min(all.length - 1, idx + dir))];
    if (next) {
      next.focus();
      next.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

})();
