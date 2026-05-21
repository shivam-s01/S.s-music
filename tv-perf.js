// ═══════════════════════════════════════════════════════════════
// tv-perf.js · Aurum TV Optimizer · v8.1 · BUG-FREE EDITION
// ───────────────────────────────────────────────────────────────
// Load BEFORE app.js + settings_addon.js
//
// Fixes over v8.0:
//   • Unified TV detection (matches app.js — no dual-listener bug)
//   • setupTVNavigation stub set IMMEDIATELY (before DOMContentLoaded)
//   • _statePushed guard prevents history stack blowup
//   • eval() replaced with safe function lookup for link items
//   • Logo color interval removed (was firing even with no track change)
//   • _aurumAudio fallback added everywhere W.audio is used
//   • visibilitychange: audio resume uses W._aurumAudio fallback
//   • section cache GC uses plain object key check (no Set — lighter)
//   • All timers registered in _timers[] — zero leaks on unload
// ═══════════════════════════════════════════════════════════════

(function (W, D) {
  'use strict';

  // ─── 1. TV DETECTION ────────────────────────────────────────
  // Single source of truth — checks __IS_TV__ first so app.js
  // can just do:  const isTV = window.__IS_TV__ || false;
  var UA = navigator.userAgent || '';
  var IS_TV = (
    /SmartTV|SMART-TV|WebOS|Tizen|BRAVIA|HbbTV|TVBrowser|Viera|Vidaa|NetCast|PhilipsTV/i.test(UA)
  ) || (
    W.innerWidth >= 1280 &&
    !W.matchMedia('(pointer:fine)').matches
  );

  if (!IS_TV) return;

  // ─── 2. GLOBALS ─────────────────────────────────────────────
  W.__IS_TV__      = true;
  W.__TV_VERSION__ = '8.1';
  W.isTV           = true;
  W.isLowEnd       = true;

  // ─── 3. STUB setupTVNavigation IMMEDIATELY ──────────────────
  // Must happen before DOMContentLoaded so app.js never registers
  // its own keydown listener (which would double-fire).
  W.setupTVNavigation = function () { /* replaced by tv-perf */ };

  // ─── 4. TIMER REGISTRY ──────────────────────────────────────
  var _timers = [];
  function _sid(id) { _timers.push(id); return id; }
  function _clearAll() {
    for (var i = 0; i < _timers.length; i++) {
      clearTimeout(_timers[i]);
      clearInterval(_timers[i]);
    }
    _timers = [];
  }

  // ─── 5. LIGHTWEIGHT CSS ─────────────────────────────────────
  var CSS = [
    // Kill heavy GPU elements
    '#ambient-canvas,.ph-ambient,.ph-orb-a,.ph-orb-b,.ph-noise,',
    '.fp-visualizer,#fp-visualizer,#fp-ambient-glow,#ambient-edge-glow,',
    '.orb,.orb-1,.orb-2,.orb-3{display:none!important}',

    // Backdrop-filter off — biggest GPU win on TV
    '#mini-player,#nav,#queue-panel,#fullscreen-player,',
    '[class*="glass"],.modal-sheet{',
    'backdrop-filter:none!important;',
    '-webkit-backdrop-filter:none!important}',

    // BG art — cheap static blur
    '#fp-bg-art{filter:blur(4px) brightness(0.20)!important;',
    'transform:none!important;will-change:auto!important}',
    '#fp-bg{filter:none!important}',

    // Kill ALL animations except settings accordion
    '*:not(.settings-section-body):not(.settings-section-body *)::before,',
    '*:not(.settings-section-body):not(.settings-section-body *)::after,',
    '*:not(.settings-section-body):not(.settings-section-body *){',
    'animation-duration:.01ms!important;animation-delay:0ms!important}',

    // Cap transitions — snappy, not janky
    '*{transition-duration:.07s!important}',

    // Settings accordion — full transition restored
    '.settings-section-body{',
    'transition:grid-template-rows .32s cubic-bezier(.33,1,.68,1),',
    'opacity .22s ease!important;transition-duration:.32s!important}',
    '.settings-section-body.open{grid-template-rows:1fr!important;opacity:1!important}',

    // Remove shadows
    '.quick-card,.wide-card,.bw-card,.song-row img,',
    '.fp-play-circle,.pl-big-cover{box-shadow:none!important}',

    // Marquee off
    '.fp-track-title.marquee-active span,',
    '.fp-artist.marquee-active span{animation:none!important;transform:none!important}',

    // Skeleton shimmer — static
    '.bw-sk-cover,.bw-sk-line,.sk-art,.sk-line,',
    '.wide-sk-cover,.wide-sk-line,.quick-sk-cover{',
    'animation:none!important;opacity:.45!important}',

    // EQ bars — static
    '.now-playing-bar span,.queue-now-playing span{',
    'animation:none!important;transform:scaleY(.55)!important}',

    // No scale on :active
    '.quick-card:active,.bw-card:active,.wide-card:active,',
    '.song-row:active,.playlist-card:active{transform:none!important}',

    // Fast image fade
    'img{transition:opacity .06s ease!important}',

    // Scrollbar hidden
    '::-webkit-scrollbar{display:none!important}',

    // TV Focus Ring
    '.is-tv *:focus{outline:3px solid #c8a858!important;',
    'outline-offset:3px!important;border-radius:10px!important}',

    // TV: mini player gone
    '.is-tv #mini-player{display:none!important}',

    // TV: nav focus
    '.is-tv .nav-btn:focus{background:rgba(184,150,64,.14)!important}',

    // Exit Overlay
    '#tv-exit-warn{position:fixed;inset:0;z-index:99999;',
    'display:flex;align-items:center;justify-content:center;',
    'background:rgba(0,0,0,.72);opacity:0;pointer-events:none;',
    'transition:opacity .16s ease!important}',
    '#tv-exit-warn.show{opacity:1;pointer-events:all}',
    '#tv-exit-warn-box{background:#0f0f15;',
    'border:1px solid rgba(184,150,64,.28);border-radius:18px;',
    'padding:28px 36px;text-align:center;max-width:320px}',
    '#tv-exit-warn-box h3{font-size:18px;font-weight:800;',
    'color:#ede8e0;margin-bottom:8px}',
    '#tv-exit-warn-box p{font-size:13px;color:#908880;margin-bottom:20px}',
    '.warn-btns{display:flex;gap:10px;justify-content:center}',
    '.warn-btn{padding:10px 28px;border-radius:100px;border:none;',
    'font-size:14px;font-weight:700;cursor:pointer;font-family:inherit}',
    '#tv-btn-stay{background:rgba(255,255,255,.08);color:#ede8e0}',
    '#tv-btn-exit{background:linear-gradient(138deg,#d4af55,#b89640);color:#050508}',
  ].join('');

  var _style        = D.createElement('style');
  _style.id         = 'tv-perf-v8';
  _style.textContent = CSS;
  (D.head || D.documentElement).appendChild(_style);

  // ─── 6. EXIT OVERLAY ────────────────────────────────────────
  var _exitEl    = null;
  var _exitTimer = null;
  var _exitShown = false;

  function _buildExitOverlay() {
    if (D.getElementById('tv-exit-warn')) {
      _exitEl = D.getElementById('tv-exit-warn');
      return;
    }
    var el = D.createElement('div');
    el.id  = 'tv-exit-warn';
    el.innerHTML = [
      '<div id="tv-exit-warn-box">',
      '<h3>Exit Aurum?</h3>',
      '<p>Press Back again to exit,<br>or Stay to keep listening.</p>',
      '<div class="warn-btns">',
      '<button class="warn-btn" id="tv-btn-stay">Stay</button>',
      '<button class="warn-btn" id="tv-btn-exit">Exit</button>',
      '</div></div>',
    ].join('');
    D.body.appendChild(el);
    _exitEl = el;
    D.getElementById('tv-btn-stay').addEventListener('click', _tvStay);
    D.getElementById('tv-btn-exit').addEventListener('click', _tvExit);
  }

  function _showExit() {
    _buildExitOverlay();
    _exitShown = true;
    _exitEl.classList.add('show');
    clearTimeout(_exitTimer);
    _exitTimer = _sid(setTimeout(_tvStay, 4000));
    var stay = D.getElementById('tv-btn-stay');
    if (stay) stay.focus();
  }

  function _tvStay() {
    _exitShown = false;
    clearTimeout(_exitTimer);
    if (_exitEl) _exitEl.classList.remove('show');
    var nb = D.querySelector('.nav-btn');
    if (nb) nb.focus();
  }

  function _tvExit() {
    try { W.close(); } catch (e) {}
    history.back();
  }

  // ─── 7. BACK BUTTON — SAFE HISTORY ──────────────────────────
  // Push ONE sentinel so popstate fires before browser navigates.
  // _statePushed prevents double-stacking.
  var _statePushed = false;

  function _pushSentinel() {
    if (_statePushed) return;
    _statePushed = true;
    try { history.pushState({ aurumTV: true }, '', location.href); } catch (e) {}
  }

  function _handleBack() {
    // Re-arm for next Back — but not while exit warn is showing
    if (!_exitShown) {
      _statePushed = false;
      _pushSentinel();
    }

    var fp     = D.getElementById('fullscreen-player');
    var qp     = D.getElementById('queue-panel');
    var sp     = D.getElementById('settings-panel');
    var fpOpen = !!(fp && fp.classList.contains('open'));
    var qpOpen = !!(qp && qp.classList.contains('open'));
    var spOpen = !!(sp && sp.classList.contains('open'));

    if (_exitShown)                                               { _tvExit(); return; }
    if (qpOpen && typeof W.closeQueuePanel === 'function')        { W.closeQueuePanel(); }
    else if (spOpen && typeof W.closeSettings === 'function')     { W.closeSettings(); }
    else if (fpOpen && typeof W.closeFullscreen === 'function')   { W.closeFullscreen(); }
    else                                                          { _showExit(); }
  }

  W.addEventListener('popstate', _handleBack);

  // ─── 8. REMOTE KEYDOWN — SINGLE LISTENER ────────────────────
  function _audio() { return W.audio || W._aurumAudio || null; }

  function _moveFocus(dir) {
    var SEL = [
      '.nav-btn', '.song-row', '.quick-card', '.wide-card',
      '.bw-card', '.browse-card', '.queue-item', '.modal-option',
      '.settings-section-header', '.settings-item',
      'button:not([disabled])', '[tabindex="0"]',
    ].join(',');
    var all = Array.prototype.filter.call(
      D.querySelectorAll(SEL),
      function (el) { return el.offsetParent !== null; }
    );
    if (!all.length) return;
    var idx  = all.indexOf(D.activeElement);
    var next = all[Math.max(0, Math.min(all.length - 1, idx + dir))];
    if (next && next !== D.activeElement) {
      next.focus();
      next.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  function _toast(msg) {
    if (typeof W.showToast === 'function') W.showToast(msg);
  }

  D.addEventListener('keydown', function (e) {
    var fp     = D.getElementById('fullscreen-player');
    var qp     = D.getElementById('queue-panel');
    var sp     = D.getElementById('settings-panel');
    var fpOpen = !!(fp && fp.classList.contains('open'));
    var qpOpen = !!(qp && qp.classList.contains('open'));
    var spOpen = !!(sp && sp.classList.contains('open'));
    var inInput = e.target &&
      (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA');

    switch (e.key) {

      case 'ArrowRight':
        e.preventDefault();
        if (fpOpen && !qpOpen && !spOpen) {
          if (typeof W.nextTrack === 'function') W.nextTrack();
        } else { _moveFocus(1); }
        break;

      case 'ArrowLeft':
        e.preventDefault();
        if (fpOpen && !qpOpen && !spOpen) {
          if (typeof W.prevTrack === 'function') W.prevTrack();
        } else { _moveFocus(-1); }
        break;

      case 'ArrowUp':
        e.preventDefault();
        if (fpOpen && !qpOpen && !spOpen) {
          var a1 = _audio();
          if (a1) {
            var v1 = Math.min(1, +(a1.volume || 0) + 0.1);
            if (typeof W.setVolume === 'function') W.setVolume(v1);
            _toast('🔊 ' + Math.round(v1 * 100) + '%');
          }
        } else { _moveFocus(-1); }
        break;

      case 'ArrowDown':
        e.preventDefault();
        if (fpOpen && !qpOpen && !spOpen) {
          var a2 = _audio();
          if (a2) {
            var v2 = Math.max(0, +(a2.volume || 1) - 0.1);
            if (typeof W.setVolume === 'function') W.setVolume(v2);
            _toast('🔉 ' + Math.round(v2 * 100) + '%');
          }
        } else { _moveFocus(1); }
        break;

      case 'Enter':
      case ' ':
        if (inInput) break;
        e.preventDefault();
        var ae = D.activeElement;
        if (ae && ae !== D.body && ae !== D.documentElement) {
          ae.click();
        } else if (fpOpen && typeof W.togglePlay === 'function') {
          W.togglePlay();
        }
        break;

      case 'GoBack':
      case 'Backspace':
        if (inInput) break;
        e.preventDefault();
        if (_exitShown) { _tvExit(); break; }
        if (qpOpen && typeof W.closeQueuePanel === 'function') {
          W.closeQueuePanel();
        } else if (spOpen && typeof W.closeSettings === 'function') {
          W.closeSettings();
        } else if (fpOpen && typeof W.closeFullscreen === 'function') {
          W.closeFullscreen();
        } else {
          _showExit();
        }
        break;

      case 'Escape':
        e.preventDefault();
        if (_exitShown) { _tvStay(); break; }
        if (qpOpen && typeof W.closeQueuePanel === 'function') {
          W.closeQueuePanel();
        } else if (spOpen && typeof W.closeSettings === 'function') {
          W.closeSettings();
        } else if (fpOpen && typeof W.closeFullscreen === 'function') {
          W.closeFullscreen();
        }
        break;

      case 's':
      case 'S':
        e.preventDefault();
        if (typeof W._setSleepMin === 'function') {
          W._setSleepMin(30);
        } else {
          _toast('⏱ Sleep · 30 min');
        }
        break;

      case 'm':
      case 'M': {
        e.preventDefault();
        var am = _audio();
        if (am) {
          am.muted = !am.muted;
          _toast(am.muted ? '🔇 Muted' : '🔊 Unmuted');
        }
        break;
      }
    }
  });

  // ─── 9. DOM READY ───────────────────────────────────────────
  function _onReady() {

    D.documentElement.classList.add('is-tv');
    D.body.classList.add('is-tv');

    _pushSentinel();

    // Mini player → open fullscreen on TV
    W.showMiniPlayer = function () {
      if (W.currentTrack) {
        var fp = D.getElementById('fullscreen-player');
        if (fp && !fp.classList.contains('open')) {
          if (typeof W.openFullscreen === 'function') W.openFullscreen();
        }
      }
    };

    // Hide mini player DOM node too
    var mp = D.getElementById('mini-player');
    if (mp) mp.style.display = 'none';

    // Kill visualizer
    try {
      if (typeof W._stopViz === 'function') W._stopViz();
      if (W.vizRaf) { cancelAnimationFrame(W.vizRaf); W.vizRaf = null; }
      W._startViz = function () {};
      W.tickViz   = function () {};
      W.initViz   = function () {
        var c = D.getElementById('fp-visualizer');
        if (c) { c.innerHTML = ''; c.style.display = 'none'; }
      };
    } catch (e) {}

    // Ambient canvas off
    try {
      var ac = D.getElementById('ambient-canvas');
      if (ac) { ac.style.display = 'none'; ac.innerHTML = ''; }
    } catch (e) {}

    // Image loading — no IntersectionObserver needed on TV
    try {
      if (W.imgObserver) { W.imgObserver.disconnect(); W.imgObserver = null; }
      W.setImgSrc = function (img, src) {
        if (!img) return;
        var ph = W.IMG_PLACEHOLDER || '';
        if (!src) { img.src = ph; img.classList.add('loaded'); return; }
        img.onerror = function () {
          if (this.src !== ph) this.src = ph;
          this.classList.add('img-error', 'loaded');
          this.onerror = null;
        };
        img.onload = function () { this.classList.add('loaded'); };
        img.src = src;
        if (img.complete && img.naturalWidth > 0) img.classList.add('loaded');
      };
    } catch (e) {}

    // Artwork: 300px only
    try {
      W.getArtUrl = function (song) {
        return ((song && song.artworkUrl100) || '')
          .replace('100x100', '300x300');
      };
    } catch (e) {}

    // Stub heavy background fetches
    try {
      W.fetchRecommendations = function () {};
      W._autoFetchFullSong   = function () {};
    } catch (e) {}

    // BG audio ping: 2 min (keeps audio alive in Android WebView)
    try {
      if (W._bgPingInterval) {
        clearInterval(W._bgPingInterval);
        W._bgPingInterval = _sid(setInterval(function () {
          if (!W.isPlaying) return;
          try {
            var ctx = W._bgAudioCtx || W._aurumAudioCtx;
            if (!ctx) return;
            var buf = ctx.createBuffer(1, 1, 22050);
            var src = ctx.createBufferSource();
            src.buffer = buf;
            src.connect(ctx.destination);
            src.start(0);
          } catch (_) {}
        }, 120000));
      }
    } catch (e) {}

    // Section cache GC: every 30s — keep only recent + featured
    _sid(setInterval(function () {
      try {
        if (typeof sectionCache !== 'undefined') {
          var keys = Object.keys(sectionCache);
          for (var i = 0; i < keys.length; i++) {
            if (keys[i] !== 'recent' && keys[i] !== 'featured') {
              delete sectionCache[keys[i]];
            }
          }
        }
      } catch (_) {}
    }, 30000));

    // content-visibility on list items
    try {
      D.querySelectorAll('.song-row,.queue-item').forEach(function (el) {
        el.style.contentVisibility    = 'auto';
        el.style.containIntrinsicSize = '0 64px';
      });
    } catch (e) {}

    // Override settings to TV panel
    _sid(setTimeout(function () {
      var _origOpen   = W.openSettings;
      var _origRender = W.renderSettingsPage;

      W.openSettings = function () {
        if (typeof _origOpen === 'function') _origOpen();
        _sid(setTimeout(_renderTVSettings, 30));
      };

      W.renderSettingsPage = function () {
        var sp2 = D.getElementById('settings-panel');
        if (sp2 && sp2.classList.contains('open')) {
          _renderTVSettings();
        } else if (typeof _origRender === 'function') {
          _origRender();
        }
      };
    }, 200));

    // Initial focus
    _sid(setTimeout(function () {
      var first = D.querySelector(
        '.nav-btn,[tabindex="0"],button,.quick-card,.song-row'
      );
      if (first) first.focus();
    }, 800));

    // ── BACKGROUND: ZERO LOAD ────────────────────────────────
    D.addEventListener('visibilitychange', function () {
      if (D.hidden) {
        try { if (typeof W._stopViz === 'function') W._stopViz(); } catch (_) {}
        // Trim cache
        try {
          if (typeof sectionCache !== 'undefined') {
            var ks = Object.keys(sectionCache);
            for (var i = 0; i < ks.length; i++) {
              if (ks[i] !== 'recent' && ks[i] !== 'featured') {
                delete sectionCache[ks[i]];
              }
            }
          }
        } catch (_) {}
        // Pause lazy images
        try {
          D.querySelectorAll('img[data-lazy-src]').forEach(function (img) {
            img.src = W.IMG_PLACEHOLDER || '';
          });
        } catch (_) {}
      } else {
        // Foreground: resume audio if stalled
        try {
          var a = _audio();
          if (W.isPlaying && a && a.paused) {
            a.play().catch(function () {});
          }
        } catch (_) {}
      }
    }, { passive: true });

    // ── CLEANUP ON UNLOAD ────────────────────────────────────
    W.addEventListener('beforeunload', function () {
      try {
        var au = _audio();
        if (au) { au.pause(); au.src = ''; }
      } catch (_) {}
      _clearAll();
      try {
        var ctx2 = W._bgAudioCtx || W._aurumAudioCtx;
        if (ctx2) { ctx2.close(); }
      } catch (_) {}
    }, { passive: true });

    console.log(
      '%c[Aurum TV v8.1] ✅ Bug-Free · Lightweight · Zero background load',
      'color:#c8a858;font-weight:800;font-size:13px'
    );
    console.log(
      '%c[Aurum TV v8.1] 🎮 ◀▶=Nav | ▲▼=Vol | OK=Select | Back=Smart | S=Sleep | M=Mute',
      'color:#666'
    );
  }

  // ─── 10. TV SETTINGS PANEL ──────────────────────────────────
  function _renderTVSettings() {
    var body = D.getElementById('settings-body');
    if (!body) return;
    var s = (typeof appSettings !== 'undefined') ? appSettings : {};

    // Toggle — uses addEventListener (CSP-safe, no eval)
    function tog(key, chk) {
      var id   = 'tv-tog-' + key;
      var chkd = chk ? ' checked' : '';
      return (
        '<label class="settings-toggle">' +
        '<input type="checkbox" id="' + id + '"' + chkd + '>' +
        '<span class="settings-toggle-track"></span></label>'
      );
    }

    function row(icon, title, sub, right, active) {
      return (
        '<div class="settings-item" tabindex="0">' +
        '<div class="settings-item-left">' +
        '<div class="settings-item-icon' + (active ? ' icon-active' : '') + '">' + icon + '</div>' +
        '<div class="settings-item-info">' +
        '<div class="settings-item-title">' + title + '</div>' +
        '<div class="settings-item-sub">' + sub + '</div>' +
        '</div></div>' + right + '</div>'
      );
    }

    // Safe link — stores fn name in data attribute, no eval()
    function link(icon, title, sub, fnName, active) {
      return (
        '<div class="settings-item" tabindex="0" data-tv-fn="' + fnName + '">' +
        '<div class="settings-item-left">' +
        '<div class="settings-item-icon' + (active ? ' icon-active' : '') + '">' + icon + '</div>' +
        '<div class="settings-item-info">' +
        '<div class="settings-item-title">' + title + '</div>' +
        '<div class="settings-item-sub">' + sub + '</div>' +
        '</div></div>' +
        '<svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>' +
        '</div>'
      );
    }

    function sec(id, icon, title, content) {
      return (
        '<div class="settings-section-header" tabindex="0" data-tv-sec="' + id + '">' +
        '<div class="ssh-left"><div class="ssh-icon">' + icon + '</div>' +
        '<span>' + title + '</span></div>' +
        '<svg class="ssh-chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></div>' +
        '<div class="settings-section-body open" id="tvs-' + id + '">' + content + '</div>'
      );
    }

    var I = {
      eq:   '<svg viewBox="0 0 24 24"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
      bolt: '<svg viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
      sun:  '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>',
      moon: '<svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>',
      vol:  '<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>',
      gear: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
      note: '<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
      dot:  '<svg viewBox="0 0 24 24"><circle cx="13.5" cy="6.5" r="2.5"/><circle cx="19" cy="11" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/><circle cx="8.5" cy="7.5" r="2.5"/><circle cx="6.5" cy="12" r="2.5"/></svg>',
      rst:  '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>',
      info: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>',
      spd:  '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
      rmt:  '<svg viewBox="0 0 24 24"><rect x="7" y="2" width="10" height="20" rx="3"/><circle cx="12" cy="17" r="1.5" fill="currentColor"/><line x1="10" y1="7" x2="14" y2="7"/><line x1="12" y1="5" x2="12" y2="9"/></svg>',
    };

    var qL  = { auto: 'Auto', high: '320 kbps', low: '128 kbps' }[s.streamQuality] || 'Auto';
    var thL = { dark: 'Dark', amoled: 'AMOLED', light: 'Light' }[s.theme] || 'Dark';
    var acL = { gold: 'Gold', rose: 'Rose', sky: 'Sky', sage: 'Sage', violet: 'Violet', ember: 'Ember' }[s.accentColor] || 'Gold';
    var slL = (function () {
      if (s.sleepMode === 'track' && s.sleepTimerEnd === -1) return 'End of track ✓';
      if (s.sleepTimerEnd > 0) {
        var rem = Math.max(0, s.sleepTimerEnd - Date.now());
        if (rem > 0) return 'Stops in ' + Math.ceil(rem / 60000) + ' min ✓';
      }
      return 'Off';
    }());

    var badge = (
      '<div style="margin:12px 16px 4px;padding:10px 16px;border-radius:12px;' +
      'background:rgba(184,150,64,.10);border:1px solid rgba(184,150,64,.22);' +
      'display:flex;align-items:center;gap:10px">' +
      I.rmt +
      '<div>' +
      '<div style="font-size:12px;font-weight:700;color:var(--gold-l)">TV Mode v8.1</div>' +
      '<div style="font-size:10px;color:var(--text3);margin-top:2px">' +
      '◀▶ Nav · ▲▼ Vol · OK=Select · Back=Smart · S=Sleep · M=Mute' +
      '</div></div></div>'
    );

    var html = badge + [
      sec('audio', I.eq, 'Audio', [
        link(I.eq,   'Stream Quality',   qL,  'openStreamQualityPicker'),
        row( I.bolt, 'Data Saver',       s.dataSaver ? '128kbps · Low data' : 'Off',
             tog('dataSaver', s.dataSaver), s.dataSaver),
        row( I.vol,  'Bass Boost',       'Enhance low frequencies',
             tog('bassBoost', s.bassBoost), s.bassBoost),
        row( I.note, 'Volume Normalize', 'Smooth gain leveling',
             tog('volumeNormalize', s.volumeNormalize), s.volumeNormalize),
        link(I.spd,  'Sleep Timer',      slL, 'openSleepTimerSheet', !!(s.sleepTimerEnd)),
      ].join('')),

      sec('visuals', I.sun, 'Visuals', [
        link(I.moon, 'Theme',        thL, 'openThemePicker'),
        link(I.dot,  'Accent Color', acL, 'openAccentColorPicker'),
        row( I.bolt, 'Smart Saver',
             s.smartSaver ? '⚡ Active' : 'Optimize performance',
             tog('smartSaver', s.smartSaver), s.smartSaver),
      ].join('')),

      sec('system', I.gear, 'System', [
        link(I.rst, 'Reset Settings', 'Restore defaults', 'smartReset'),
        '<div class="settings-item">' +
        '<div class="settings-item-left">' +
        '<div class="settings-item-icon">' + I.info + '</div>' +
        '<div class="settings-item-info">' +
        '<div class="settings-item-title">Aurum</div>' +
        '<div class="settings-item-sub">v3.1 · TV Optimizer v8.1</div>' +
        '</div></div></div>',
      ].join('')),
    ].join('');

    body.innerHTML = html;

    // Wire toggles — CSP-safe, no inline handlers
    var toggleMap = {
      'tv-tog-dataSaver': function (v) {
        if (typeof saveSetting === 'function') saveSetting('dataSaver', v);
        if (typeof showToast   === 'function') showToast(v ? 'Data Saver on' : 'Data Saver off');
      },
      'tv-tog-bassBoost': function (v) {
        if (typeof toggleAudioFX === 'function') toggleAudioFX('bassBoost', v);
        else if (typeof saveSetting === 'function') saveSetting('bassBoost', v);
      },
      'tv-tog-volumeNormalize': function (v) {
        if (typeof saveSetting === 'function') saveSetting('volumeNormalize', v);
      },
      'tv-tog-smartSaver': function (v) {
        if (typeof applySmartSaver === 'function') applySmartSaver(v);
        else if (typeof saveSetting === 'function') saveSetting('smartSaver', v);
      },
    };

    Object.keys(toggleMap).forEach(function (id) {
      var el = D.getElementById(id);
      if (!el) return;
      el.addEventListener('change', function () { toggleMap[id](this.checked); });
    });

    // Wire link items — safe function lookup, no eval()
    body.querySelectorAll('[data-tv-fn]').forEach(function (el) {
      var fnName = el.getAttribute('data-tv-fn');
      function _call() {
        if (typeof W[fnName] === 'function') W[fnName]();
      }
      el.addEventListener('click', _call);
      el.addEventListener('keydown', function (e2) {
        if (e2.key === 'Enter' || e2.key === ' ') { e2.preventDefault(); _call(); }
      });
    });

    // Wire section headers
    body.querySelectorAll('[data-tv-sec]').forEach(function (hdr) {
      var sid2 = hdr.getAttribute('data-tv-sec');
      hdr.addEventListener('click', function () {
        if (typeof W.toggleSection === 'function') W.toggleSection('tvs-' + sid2);
      });
    });

    // Make all items focusable
    body.querySelectorAll('.settings-item,.settings-section-header').forEach(function (el) {
      if (!el.hasAttribute('tabindex')) el.setAttribute('tabindex', '0');
    });
  }

  // ─── BOOT ───────────────────────────────────────────────────
  if (D.readyState === 'loading') {
    D.addEventListener('DOMContentLoaded', _onReady, { once: true });
  } else {
    _onReady();
  }

}(window, document));
