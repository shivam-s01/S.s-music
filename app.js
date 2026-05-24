(function (W, D) {
  'use strict';

  // ─── 1. TV DETECTION ────────────────────────────────────────
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
    '#ambient-canvas,.ph-ambient,.ph-orb-a,.ph-orb-b,.ph-noise,',
    '.fp-visualizer,#fp-visualizer,#fp-ambient-glow,#ambient-edge-glow,',
    '.orb,.orb-1,.orb-2,.orb-3{display:none!important}',
    '#mini-player,#nav,#queue-panel,#fullscreen-player,',
    '[class*="glass"],.modal-sheet{',
    'backdrop-filter:none!important;',
    '-webkit-backdrop-filter:none!important}',
    '#fp-bg-art{filter:blur(4px) brightness(0.20)!important;',
    'transform:none!important;will-change:auto!important}',
    '#fp-bg{filter:none!important}',
    '*:not(.settings-section-body):not(.settings-section-body *)::before,',
    '*:not(.settings-section-body):not(.settings-section-body *)::after,',
    '*:not(.settings-section-body):not(.settings-section-body *){',
    'animation-duration:.01ms!important;animation-delay:0ms!important}',
    '*{transition-duration:.07s!important}',
    '.settings-section-body{',
    'transition:grid-template-rows .32s cubic-bezier(.33,1,.68,1),',
    'opacity .22s ease!important;transition-duration:.32s!important}',
    '.settings-section-body.open{grid-template-rows:1fr!important;opacity:1!important}',
    '.quick-card,.wide-card,.bw-card,.song-row img,',
    '.fp-play-circle,.pl-big-cover{box-shadow:none!important}',
    '.fp-track-title.marquee-active span,',
    '.fp-artist.marquee-active span{animation:none!important;transform:none!important}',
    '.bw-sk-cover,.bw-sk-line,.sk-art,.sk-line,',
    '.wide-sk-cover,.wide-sk-line,.quick-sk-cover{',
    'animation:none!important;opacity:.45!important}',
    '.now-playing-bar span,.queue-now-playing span{',
    'animation:none!important;transform:scaleY(.55)!important}',
    '.quick-card:active,.bw-card:active,.wide-card:active,',
    '.song-row:active,.playlist-card:active{transform:none!important}',
    'img{transition:opacity .06s ease!important}',
    '::-webkit-scrollbar{display:none!important}',
    '.is-tv *:focus{outline:3px solid #c8a858!important;',
    'outline-offset:3px!important;border-radius:10px!important}',
    '.is-tv #mini-player{display:none!important}',
    '.is-tv .nav-btn:focus{background:rgba(184,150,64,.14)!important}',
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
  var _statePushed = false;

  function _pushSentinel() {
    if (_statePushed) return;
    _statePushed = true;
    try { history.pushState({ aurumTV: true }, '', location.href); } catch (e) {}
  }

  function _handleBack() {
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

    W.showMiniPlayer = function () {
      if (W.currentTrack) {
        var fp = D.getElementById('fullscreen-player');
        if (fp && !fp.classList.contains('open')) {
          if (typeof W.openFullscreen === 'function') W.openFullscreen();
        }
      }
    };

    var mp = D.getElementById('mini-player');
    if (mp) mp.style.display = 'none';

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

    try {
      var ac = D.getElementById('ambient-canvas');
      if (ac) { ac.style.display = 'none'; ac.innerHTML = ''; }
    } catch (e) {}

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

    try {
      W.getArtUrl = function (song) {
        return ((song && song.artworkUrl100) || '')
          .replace('100x100', '300x300');
      };
    } catch (e) {}

    try {
      W.fetchRecommendations = function () {};
      W._autoFetchFullSong   = function () {};
    } catch (e) {}

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

    try {
      D.querySelectorAll('.song-row,.queue-item').forEach(function (el) {
        el.style.contentVisibility    = 'auto';
        el.style.containIntrinsicSize = '0 64px';
      });
    } catch (e) {}

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

    _sid(setTimeout(function () {
      var first = D.querySelector(
        '.nav-btn,[tabindex="0"],button,.quick-card,.song-row'
      );
      if (first) first.focus();
    }, 800));

    D.addEventListener('visibilitychange', function () {
      if (D.hidden) {
        try { if (typeof W._stopViz === 'function') W._stopViz(); } catch (_) {}
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
        try {
          D.querySelectorAll('img[data-lazy-src]').forEach(function (img) {
            img.src = W.IMG_PLACEHOLDER || '';
          });
        } catch (_) {}
      } else {
        try {
          var a = _audio();
          if (W.isPlaying && a && a.paused) {
            a.play().catch(function () {});
          }
        } catch (_) {}
      }
    }, { passive: true });

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

    body.querySelectorAll('[data-tv-sec]').forEach(function (hdr) {
      var sid2 = hdr.getAttribute('data-tv-sec');
      hdr.addEventListener('click', function () {
        if (typeof W.toggleSection === 'function') W.toggleSection('tvs-' + sid2);
      });
    });

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

// ═══════════════════════════════════════════════════════════════
// app.js · Aurum Music Player · Main Application
// ═══════════════════════════════════════════════════════════════

// ─── DEVICE DETECTION ────────────────────────────────────────────────────────
const isTV = window.__IS_TV__ || (
  /SmartTV|SMART-TV|WebOS|Tizen|BRAVIA|HbbTV|TVBrowser|Viera|Vidaa|NetCast|PhilipsTV/i.test(navigator.userAgent) ||
  (window.innerWidth >= 1280 && !window.matchMedia('(pointer:fine)').matches)
);

const isMobile = /Android|iPhone|iPad/i.test(navigator.userAgent) && !isTV;

const isLowEnd = isTV ||
  (navigator.hardwareConcurrency || 8) <= 4 ||
  (typeof navigator.deviceMemory !== 'undefined' && navigator.deviceMemory <= 2);

// ─── DYNAMIC VIEWPORT ────────────────────────────────────────────────────────
function setVh() {
  document.documentElement.style.setProperty('--vh', (window.innerHeight * 0.01) + 'px');
}
window.addEventListener('resize', setVh, { passive: true });
window.addEventListener('orientationchange', () => setTimeout(setVh, 300), { passive: true });
setVh();

// ─── FPS CAP (30fps) — for VISUALIZER only ───────────────────────────────────
const TARGET_FPS   = 30;
const FRAME_BUDGET = 1000 / TARGET_FPS;
let _lastRafTime   = 0;

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
let sectionCache         = {};
let queuePanelOpen       = false;
let _lastObjectUrl       = null;
let _lastTuTime          = 0;
let _uiHidden            = false;
let _dismissedTrackId    = null;

// Lyrics view state
let lyricsViewActive = false;
let originalArtworkHTML = null;

// ─── LISTEN HISTORY / ALGORITHM ──────────────────────────────────────────────
let _listenHistory = JSON.parse(localStorage.getItem('aurum_listen_history') || '{}');

function _trackListen(song) {
  if (!song?.artistName) return;
  const artists = song.artistName.split(/[&,]|feat\.|ft\./i).map(a => a.trim()).filter(Boolean);
  artists.forEach(artist => {
    if (!_listenHistory[artist]) _listenHistory[artist] = { count: 0, lastSeen: 0, songs: [] };
    _listenHistory[artist].count++;
    _listenHistory[artist].lastSeen = Date.now();
    const existing = _listenHistory[artist].songs;
    if (!existing.find(s => String(s.trackId) === String(song.trackId))) {
      existing.unshift(song);
      if (existing.length > 20) existing.pop();
    }
  });
  const keys = Object.keys(_listenHistory);
  if (keys.length > 50) {
    const oldest = keys.sort((a, b) => _listenHistory[a].lastSeen - _listenHistory[b].lastSeen)[0];
    delete _listenHistory[oldest];
  }
  localStorage.setItem('aurum_listen_history', JSON.stringify(_listenHistory));
}

function _getTopArtists(limit = 5) {
  return Object.entries(_listenHistory)
    .sort((a, b) => b[1].count - a[1].count)
    .slice(0, limit)
    .map(([artist, data]) => ({ artist, ...data }));
}

// ─── AUDIO ENGINE ─────────────────────────────────────────────────────────────
const audio = new Audio();
audio.preload = 'none';
audio.crossOrigin = 'anonymous';
audio.setAttribute('playsinline', '');
audio.setAttribute('webkit-playsinline', '');
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

  _dismissedTrackId = null;

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
  fetchLyrics(song);
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

    // ── Step 1: /api/saavn try karo ──────────────────────────────
    let d        = null;
    let proxyUrl = null;

    try {
      const r1 = await fetch(`/api/saavn?q=${primaryQ}&artist=${artistQ}&fallback=${fallbackQ}`, { signal: ctrl.signal });
      if (r1.ok) {
        const j1 = await r1.json();
        if (j1.success && j1.url) {
          // Title check sirf saavn ke liye
          if (j1.source === 'saavn' && !_titleMatches(j1.title, requested.trackName)) {
            console.warn(`[Mismatch/Saavn] Asked="${requested.trackName}" Got="${j1.title}" — trying resolve`);
          } else {
            d        = j1;
            proxyUrl = `/api/stream?url=${encodeURIComponent(j1.url)}`;
            console.info(`[AutoFetch] Saavn ✓ quality=${j1.quality}`);
          }
        }
      }
    } catch(e1) {
      if (e1.name === 'AbortError') return;
      console.info('[AutoFetch] Saavn failed, trying resolve:', e1.message);
    }

    // ── Step 2: /api/resolve fallback (Piped/Invidious) ──────────
    if (!proxyUrl) {
      try {
        const r2 = await fetch(`/api/resolve?q=${primaryQ}&artist=${artistQ}&fallback=${fallbackQ}`, { signal: ctrl.signal });
        if (r2.ok) {
          const j2 = await r2.json();
          if (j2.success && j2.url) {
            d        = j2;
            proxyUrl = j2.url; // resolve already /api/stream proxy URL deta hai
            console.info(`[AutoFetch] Resolve ✓ source=${j2.source} quality=${j2.quality}`);
          }
        }
      } catch(e2) {
        if (e2.name === 'AbortError') return;
        console.info('[AutoFetch] Resolve also failed:', e2.message);
      }
    }

    if (ctrl.signal.aborted) return;
    if (currentTrack?.trackId !== requested.trackId) return;
    if (!d || !proxyUrl) {
      console.info('[AutoFetch] No source found — staying on preview');
      return;
    }

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

    audio.addEventListener('loadedmetadata', () => {
      if (isFinite(pos) && pos > 0 && pos < audio.duration) audio.currentTime = pos;
    }, { once: true });

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

audio.addEventListener('timeupdate', () => {
  const now = Date.now();
  if (now - _lastTuTime < 250) return;
  _lastTuTime = now;
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
window.addEventListener('online', _handleConnectivity, { passive: true });
window.addEventListener('offline', _handleConnectivity, { passive: true });
_handleConnectivity();

// ─── SCREEN OFF / ON ─────────────────────────────────────────────────────────
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    _uiHidden = true;
    _stopViz();
    document.getElementById('ambient-canvas')?.classList.remove('orbs-active');
    const fp = document.getElementById('fullscreen-player');
    const ac = document.getElementById('ambient-canvas');
    if (fp) fp.style.setProperty('visibility', 'hidden', 'important');
    if (ac) ac.style.setProperty('display', 'none', 'important');
    const keep = new Set(['recent', 'featured']);
    Object.keys(sectionCache).forEach(k => { if (!keep.has(k)) delete sectionCache[k]; });
  } else {
    _uiHidden = false;
    const fp = document.getElementById('fullscreen-player');
    const ac = document.getElementById('ambient-canvas');
    if (fp) fp.style.removeProperty('visibility');
    if (ac) ac.style.removeProperty('display');
    if (fp?.classList.contains('open') && !isLowEnd) _startViz();
    if (!isLowEnd) document.getElementById('ambient-canvas')?.classList.add('orbs-active');
    if (currentTrack) {
      _syncPlayIcons();
      _syncPlayingClass();
      updateQualityLabel();
      if (isPlaying && audio.paused) {
        audio.play().catch(() => {});
      }
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
  if (fp) isPlaying ? fp.classList.add('playing') : fp.classList.remove('playing');
  if (mp) isPlaying ? mp.classList.add('playing-glow') : mp.classList.remove('playing-glow');
}

function updatePlayerUI() {
  if (!currentTrack) return;
  const artUrl = getArtUrl(currentTrack, '600x600');
  const miniArt = document.getElementById('mini-art');
  if (miniArt) setImgSrc(miniArt, artUrl);
  const fpArt = document.getElementById('fp-art');
  if (fpArt) setImgSrc(fpArt, artUrl);
  const mt = document.getElementById('mini-title'); if (mt) mt.textContent = currentTrack.trackName || 'Unknown';
  const ma = document.getElementById('mini-artist'); if (ma) ma.textContent = currentTrack.artistName || 'Unknown';
  const ft = document.getElementById('fp-title'); if (ft) ft.textContent = currentTrack.trackName || 'Unknown';
  const fa = document.getElementById('fp-artist'); if (fa) fa.textContent = currentTrack.artistName || 'Unknown';
  _syncPlayIcons();
  _syncPlayingClass();
  updateSaveBtn();
  updateActiveRows();
  updateAmbientPlayer(artUrl);
  updateNextStrip();
  updateMediaSession();
  showMiniPlayer();
}

// ─── LYRICS TOGGLE VIEW ───────────────────────────────────────────────────────
function toggleLyricsView() {
  const wrap      = document.getElementById('fp-lyrics-wrap');
  const lyricsBtn = document.getElementById('fp-lyrics-toggle');
  if (!wrap) return;

  const isOpen = wrap.style.display !== 'none' && wrap.style.display !== '';

  if (isOpen) {
    wrap.style.display = 'none';
    lyricsViewActive = false;
    if (lyricsBtn) lyricsBtn.classList.remove('active');
  } else {
    const el = document.getElementById('fp-lyrics');
    if (!el || !el.textContent.trim()) { showToast('No lyrics available'); return; }
    wrap.style.display = 'block';
    el.scrollTop = 0;
    lyricsViewActive = true;
    if (lyricsBtn) lyricsBtn.classList.add('active');
  }
}

// ─── updateNextStrip ─────────────────────────────────────────────────────────
function updateNextStrip() {
  const strip = document.getElementById('fp-next-strip');
  if (!strip) return;

  const remainingCount = currentQueue.length - currentIndex - 1;

  if (!currentQueue.length || currentQueue.length < 2) {
    strip.style.display = 'none';
    return;
  }

  let nextIdx;
  if (shuffleOn) {
    nextIdx = Math.floor(Math.random() * currentQueue.length);
    while (nextIdx === currentIndex && currentQueue.length > 1) {
      nextIdx = Math.floor(Math.random() * currentQueue.length);
    }
  } else {
    nextIdx = (currentIndex + 1) % currentQueue.length;
  }

  const nextSong = currentQueue[nextIdx];
  if (!nextSong) { strip.style.display = 'none'; return; }

  strip.style.display = 'flex';

  const tag = strip.querySelector('.fp-next-tag');
  if (tag && remainingCount > 0) {
    tag.innerHTML = `UP<br>NEXT<br><span style="font-size:7px; margin-top:2px;">${remainingCount}</span>`;
  } else if (tag) {
    tag.innerHTML = 'UP<br>NEXT';
  }

  const wasHidden = !strip.style.opacity || strip.style.opacity === '0' || getComputedStyle(strip).opacity === '0';
  if (wasHidden) {
    strip.style.opacity = '0';
    strip.style.transform = 'translateY(8px)';
    strip.style.transition = 'opacity 0.35s cubic-bezier(0.22,1,0.36,1), transform 0.35s cubic-bezier(0.22,1,0.36,1)';
    requestAnimationFrame(() => requestAnimationFrame(() => {
      strip.style.opacity = '';
      strip.style.transform = '';
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
      nextArtEl.style.transition = 'opacity 0.25s ease';
      nextArtEl.style.opacity = '0';
      setTimeout(() => {
        setImgSrc(nextArtEl, newSrc);
        nextArtEl.style.opacity = '';
      }, 180);
    }
  }

  if (nextTitleEl)  nextTitleEl.textContent  = nextSong.trackName  || 'Unknown';
  if (nextArtistEl) nextArtistEl.textContent = nextSong.artistName || 'Unknown';
}

function updateMediaSession() {
  if (!('mediaSession' in navigator) || !currentTrack) return;
  try {
    const artUrl = getArtUrl(currentTrack, '512x512');
    navigator.mediaSession.metadata = new MediaMetadata({
      title: currentTrack.trackName || 'Unknown',
      artist: currentTrack.artistName || 'Unknown',
      artwork: [{ src: artUrl, sizes: '512x512', type: 'image/jpeg' }]
    });
    navigator.mediaSession.setActionHandler('play', () => { audio.play().catch(()=>{}); isPlaying = true; updatePlayerUI(); });
    navigator.mediaSession.setActionHandler('pause', () => { audio.pause(); isPlaying = false; updatePlayerUI(); });
    navigator.mediaSession.setActionHandler('nexttrack', nextTrack);
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
  if (isTV) return;
  if (_dismissedTrackId && currentTrack && String(_dismissedTrackId) === String(currentTrack.trackId)) return;
  const mp = document.getElementById('mini-player');
  if (!mp) return;
  mp.style.opacity = '';
  mp.style.pointerEvents = '';
  mp.classList.add('show');
}

function updateActiveRows() {
  document.querySelectorAll('.song-row,.queue-item').forEach(r => {
    const isCurrent = currentTrack && (String(r.dataset.trackId) === String(currentTrack.trackId));
    r.classList.toggle('playing', isCurrent);
    r.classList.toggle('current', isCurrent);
    const rightDiv = r.querySelector('.song-row-right');
    if (!rightDiv) return;
    const existing = rightDiv.querySelector('.now-playing-bar');
    const durSpan = rightDiv.querySelector('.song-row-duration');
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

// ─── AMBIENT PLAYER BG ───────────────────────────────────────────────────────
let _lastAmbientSrc = '';
function updateAmbientPlayer(artUrl) {
  if (isLowEnd) return;
  if (!artUrl || artUrl === _lastAmbientSrc) return;
  _lastAmbientSrc = artUrl;
  const bgArt = document.getElementById('fp-bg-art');
  if (!bgArt) return;
  bgArt.onerror = () => { bgArt.style.opacity = '0'; };
  bgArt.onload = () => {
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

// ─── VISUALIZER ───────────────────────────────────────────────────────────────
const VIZ_COUNT = isLowEnd ? 0 : 44;
let vizBars = [];
let vizRaf = null;
let vizPhase = 0;
let vizTarget = [];
let _lastVizTime = 0;
const vizRandOffsets = Array.from({ length: VIZ_COUNT }, () => Math.random() * 6.28);

function initViz() {
  if (isLowEnd) return;
  const c = document.getElementById('fp-visualizer'); if (!c) return;
  c.innerHTML = ''; vizBars = []; vizTarget = [];
  for (let i = 0; i < VIZ_COUNT; i++) {
    const b = document.createElement('div'); b.className = 'fp-viz-bar';
    c.appendChild(b); vizBars.push(b); vizTarget.push(0.05);
  }
}

function _startViz() {
  if (isLowEnd || vizRaf !== null) return;
  vizRaf = requestAnimationFrame(_vizLoop);
}

function _stopViz() {
  if (vizRaf) { cancelAnimationFrame(vizRaf); vizRaf = null; }
}

function _vizLoop(ts) {
  const fp = document.getElementById('fullscreen-player');
  if (document.hidden || !fp?.classList.contains('open') || isLowEnd) {
    vizRaf = null;
    return;
  }
  if (ts - _lastVizTime < FRAME_BUDGET) {
    vizRaf = requestAnimationFrame(_vizLoop);
    return;
  }
  _lastVizTime = ts;
  vizPhase += 0.034 + Math.sin(vizPhase * 0.1) * 0.004;
  vizBars.forEach((b, i) => {
    if (!isPlaying) {
      vizTarget[i] = vizTarget[i] * 0.88 + 0.05 * 0.12;
      b.style.transform = `scaleY(${vizTarget[i].toFixed(3)})`; return;
    }
    const norm = i / VIZ_COUNT;
    const freqCurve = norm < 0.12 ? (norm / 0.12) : norm < 0.44 ? 1 - (norm - 0.12) * 0.55 : Math.max(0.1, 0.8 - (norm - 0.44) * 1.35);
    const rOff = vizRandOffsets[i];
    const o1 = Math.sin(vizPhase * 2.0 + i * 0.36 + rOff) * 0.38 + 0.38;
    const o2 = Math.sin(vizPhase * 1.3 + i * 0.68 + rOff * 0.7 + 0.9) * 0.22 + 0.22;
    const o3 = Math.sin(vizPhase * 3.1 + i * 0.22 + rOff * 0.4 + 2.1) * 0.11 + 0.11;
    const target = Math.max(0.05, Math.min(1, (o1 + o2 * 0.58 + o3 * 0.38) * freqCurve * 0.88));
    vizTarget[i] = vizTarget[i] * 0.74 + target * 0.26;
    b.style.transform = `scaleY(${vizTarget[i].toFixed(3)})`;
  });
  vizRaf = requestAnimationFrame(_vizLoop);
}

function tickViz() { _startViz(); }

// ─── SMART BLUR HELPERS ───────────────────────────────────────────────────────
function _pauseBlur() {
  [
    document.getElementById('fullscreen-player'),
    document.getElementById('mini-player'),
    document.getElementById('queue-panel'),
  ].forEach(el => {
    if (!el) return;
    el.style.backdropFilter = 'none';
    el.style.webkitBackdropFilter = 'none';
  });
}

function _resumeBlur() {
  [
    document.getElementById('fullscreen-player'),
    document.getElementById('mini-player'),
    document.getElementById('queue-panel'),
  ].forEach(el => {
    if (!el) return;
    el.style.backdropFilter = '';
    el.style.webkitBackdropFilter = '';
  });
}

// ═══════════════════════════════════════════════════════════════════════════════
// ─── GESTURE SYSTEM ───────────────────────────────────────────────────────────
// ═══════════════════════════════════════════════════════════════════════════════

function setupMiniGesture() {
  const mp = document.getElementById('mini-player');
  if (!mp) return;

  let startY = 0, startX = 0, isDragging = false, startTime = 0;
  let moved = false, rafId = null;
  let axisLocked = null;

  function snapBack() {
    mp.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
    mp.style.transform = '';
    mp.style.willChange = '';
    setTimeout(() => { mp.style.transition = ''; }, 340);
    _resumeBlur();
  }

  mp.addEventListener('touchstart', e => {
    startY     = e.touches[0].clientY;
    startX     = e.touches[0].clientX;
    isDragging = true;
    startTime  = Date.now();
    moved      = false;
    axisLocked = null;
    mp.style.transition  = 'none';
    mp.style.willChange  = 'transform';
    mp.style.transform   = mp.style.transform || 'translateZ(0)';
    _pauseBlur();
  }, { passive: true });

  mp.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dy    = e.touches[0].clientY - startY;
    const dx    = e.touches[0].clientX - startX;
    const absDy = Math.abs(dy);
    const absDx = Math.abs(dx);

    if (!axisLocked && (absDy > 8 || absDx > 8)) {
      axisLocked = absDx > absDy ? 'horizontal' : 'vertical';
    }

    if (axisLocked === 'horizontal') {
      isDragging = false;
      mp.style.willChange = '';
      snapBack();
      return;
    }

    if (axisLocked === 'vertical' && absDy > 4) {
      moved = true;
      e.preventDefault();
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        rafId = null;
        mp.style.transform = `translateY(${dy}px)`;
      });
    }
  }, { passive: false });

  mp.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return;
    isDragging  = false;
    axisLocked  = null;

    const dy  = e.changedTouches[0].clientY - startY;
    const dt  = Math.max(1, Date.now() - startTime);
    const vel = dy / dt;

    mp.style.willChange = '';
    _resumeBlur();

    if (!moved) { snapBack(); return; }

    if (dy < -30 || vel < -0.45) {
      mp.style.transform = '';
      mp.style.transition = '';
      openFullscreen();
      return;
    }

    if (dy > 100 || (vel > 0.55 && dy > 30)) {
      if (currentTrack) _dismissedTrackId = currentTrack.trackId;
      mp.style.transition = 'transform 0.25s ease, opacity 0.2s ease';
      mp.style.transform  = 'translateY(120px)';
      mp.style.opacity    = '0';
      setTimeout(() => {
        mp.classList.remove('show');
        mp.style.transform  = '';
        mp.style.opacity    = '';
        mp.style.transition = '';
      }, 250);
      if (isPlaying) { audio.pause(); isPlaying = false; _syncPlayIcons(); _syncPlayingClass(); }
      return;
    }

    snapBack();
  }, { passive: true });

  mp.addEventListener('touchcancel', () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging  = false;
    axisLocked  = null;
    mp.style.willChange = '';
    snapBack();
  }, { passive: true });
}

function setupFullPlayerGesture() {
  const fp = document.getElementById('fullscreen-player');
  const qp = document.getElementById('queue-panel');
  if (!fp || !qp) return;

  let startY = 0, startX = 0, isDragging = false, startTime = 0;
  let gestureTarget = null, moved = false, rafId = null;
  let axisLocked = null;
  let queueOpenTriggered = false;

  function snapBackFp() {
    fp.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
    fp.style.transform  = '';
    fp.style.willChange = '';
    setTimeout(() => { fp.style.transition = ''; }, 340);
    _resumeBlur();
  }

  function snapBackQp() {
    qp.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
    qp.style.transform  = qp.classList.contains('open') ? 'translateY(0)' : 'translateY(100%)';
    qp.style.willChange = '';
    setTimeout(() => {
      qp.style.transform  = '';
      qp.style.transition = '';
    }, 340);
    _resumeBlur();
  }

  function cleanupGesture() {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging          = false;
    axisLocked          = null;
    moved               = false;
    queueOpenTriggered  = false;
    fp.classList.remove('dragging');
    qp.classList.remove('dragging');
    fp.style.willChange = '';
    qp.style.willChange = '';
  }

  function isGestureZone(el) {
    return el.closest('#fp-drag-hint') || el.closest('.fp-header') || el.closest('.fp-info') ||
           el.closest('.fp-art-wrap') || el.closest('.fp-progress-wrap') || el.closest('.fp-controls') ||
           el.closest('.fp-bottom') || el.closest('.fp-next-strip') || el.closest('.queue-panel-handle') ||
           el.closest('#queue-drag-handle');
  }

  fp.addEventListener('touchstart', e => {
    const qpOpen      = qp.classList.contains('open');
    const onQueueHandle = e.target.closest('#queue-drag-handle');
    const onQueueBody   = qp.contains(e.target) && !onQueueHandle;

    if (qpOpen && onQueueBody) { isDragging = false; return; }
    if (!qpOpen && !isGestureZone(e.target)) { isDragging = false; return; }

    startY             = e.touches[0].clientY;
    startX             = e.touches[0].clientX;
    isDragging         = true;
    startTime          = Date.now();
    moved              = false;
    axisLocked         = null;
    queueOpenTriggered = false;
    gestureTarget      = qpOpen ? 'queue' : 'player';

    const target = gestureTarget === 'player' ? fp : qp;
    target.style.willChange = 'transform';
    target.style.transform  = target.style.transform || 'translateZ(0)';
    fp.classList.add('dragging');
    qp.classList.add('dragging');
    _pauseBlur();
  }, { passive: true });

  fp.addEventListener('touchmove', e => {
    if (!isDragging || queueOpenTriggered) return;

    const dy    = e.touches[0].clientY - startY;
    const dx    = e.touches[0].clientX - startX;
    const absDy = Math.abs(dy);
    const absDx = Math.abs(dx);

    if (!axisLocked && (absDy > 6 || absDx > 6)) {
      axisLocked = absDx > absDy ? 'horizontal' : 'vertical';
    }

    if (axisLocked === 'horizontal') { isDragging = false; return; }

    if (axisLocked === 'vertical' && absDy > 4) {
      moved = true;

      if (gestureTarget === 'player' && dy > 0) {
        e.preventDefault();
        if (rafId) cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(() => {
          fp.style.transform = `translateY(${dy}px)`;
          rafId = null;
        });
      } else if (gestureTarget === 'queue' && dy > 0) {
        e.preventDefault();
        if (rafId) cancelAnimationFrame(rafId);
        rafId = requestAnimationFrame(() => {
          qp.style.transform = `translateY(${dy}px)`;
          rafId = null;
        });
      } else if (gestureTarget === 'player' && dy < -60) {
        e.preventDefault();
        queueOpenTriggered = true;
        cleanupGesture();
        fp.style.transform = '';
        qp.style.transform = '';
        _resumeBlur();
        openQueuePanel();
      }
    }
  }, { passive: false });

  fp.addEventListener('touchend', e => {
    if (!isDragging) return;
    const dy      = e.changedTouches[0].clientY - startY;
    const dt      = Math.max(1, Date.now() - startTime);
    const vel     = dy / dt;
    const wasTarget = gestureTarget;
    const wasMoved  = moved;
    cleanupGesture();
    _resumeBlur();

    if (!wasMoved) {
      wasTarget === 'player' ? snapBackFp() : snapBackQp();
      return;
    }

    if (wasTarget === 'player') {
      if (dy > 100 || (vel > 0.5 && dy > 40)) {
        fp.style.transform = '';
        closeFullscreen();
      } else {
        snapBackFp();
      }
    } else if (wasTarget === 'queue') {
      if (dy > 90 || (vel > 0.5 && dy > 25)) {
        qp.style.transform = '';
        closeQueuePanel();
      } else {
        snapBackQp();
      }
    }
  }, { passive: true });

  fp.addEventListener('touchcancel', () => {
    const wasTarget = gestureTarget;
    cleanupGesture();
    _resumeBlur();
    if (wasTarget === 'player') snapBackFp();
    else if (wasTarget === 'queue') snapBackQp();
  }, { passive: true });
}

function _attachArtSwipe() {
  const artWrap = document.getElementById('fp-art-wrap');
  if (!artWrap || artWrap._swipeAttached) return;
  artWrap._swipeAttached = true;

  let startX = 0, startY = 0, isDragging = false, moved = false, startTime = 0, rafId = null;
  let axisLocked = null;

  function resetArt() {
    artWrap.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1), opacity 0.22s ease';
    artWrap.style.transform  = '';
    artWrap.style.opacity    = '';
    artWrap.style.willChange = '';
    setTimeout(() => { artWrap.style.transition = ''; }, 350);
  }

  artWrap.addEventListener('touchstart', e => {
    startX     = e.touches[0].clientX;
    startY     = e.touches[0].clientY;
    isDragging = true;
    moved      = false;
    axisLocked = null;
    startTime  = Date.now();
    artWrap.style.transition = 'none';
    artWrap.style.willChange = 'transform,opacity';
    artWrap.style.transform  = artWrap.style.transform || 'translateZ(0)';
  }, { passive: true });

  artWrap.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dx    = e.touches[0].clientX - startX;
    const dy    = e.touches[0].clientY - startY;
    const absDx = Math.abs(dx);
    const absDy = Math.abs(dy);

    if (!axisLocked && (absDx > 8 || absDy > 8)) {
      axisLocked = absDx > absDy ? 'horizontal' : 'vertical';
    }

    if (axisLocked === 'vertical') {
      isDragging = false;
      artWrap.style.willChange = '';
      resetArt();
      return;
    }

    if (axisLocked === 'horizontal' && absDx > 8) {
      moved = true;
      e.preventDefault();
      const clamped = dx * 0.72;
      const tilt    = clamped * 0.018;
      const fade    = Math.max(0.28, 1 - Math.abs(dx) / 280);
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        artWrap.style.transform = `translateX(${clamped}px) rotate(${tilt}deg)`;
        artWrap.style.opacity   = String(fade);
        rafId = null;
      });
    }
  }, { passive: false });

  artWrap.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return;
    isDragging = false;
    artWrap.style.willChange = '';
    const dx  = e.changedTouches[0].clientX - startX;
    const dt  = Math.max(1, Date.now() - startTime);
    const vel = dx / dt;
    if (!moved) { resetArt(); return; }
    if (dx < -55 || vel < -0.38)     { _animateArtSwipe('left',  nextTrack); }
    else if (dx > 55 || vel > 0.38)  { _animateArtSwipe('right', prevTrack); }
    else                              { resetArt(); }
  }, { passive: true });

  artWrap.addEventListener('touchcancel', () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging = false;
    axisLocked = null;
    artWrap.style.willChange = '';
    resetArt();
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

function setupShakeGesture() {
  if (!window.DeviceMotionEvent) return;
  const THRESHOLD = 18;
  const COOLDOWN  = 1500;
  let lastShake = 0;
  let lastX = 0, lastY = 0, lastZ = 0;
  let initialized = false;

  function onMotion(e) {
    if (typeof appSettings !== 'undefined' && appSettings?.shakeToShuffle === false) return;
    const acc = e.accelerationIncludingGravity;
    if (!acc) return;
    if (!initialized) { lastX = acc.x||0; lastY = acc.y||0; lastZ = acc.z||0; initialized = true; return; }
    const dx = Math.abs((acc.x||0) - lastX);
    const dy = Math.abs((acc.y||0) - lastY);
    const dz = Math.abs((acc.z||0) - lastZ);
    lastX = acc.x||0; lastY = acc.y||0; lastZ = acc.z||0;
    if (dx + dy + dz > THRESHOLD) {
      const now = Date.now();
      if (now - lastShake < COOLDOWN) return;
      lastShake = now;
      if (currentQueue.length > 1) {
        haptic([20, 50, 20]);
        shuffleOn = true;
        const shuffleBtn = document.getElementById('shuffle-btn');
        if (shuffleBtn) shuffleBtn.querySelector('svg').style.stroke = 'var(--gold-l)';
        showToast('🔀 Shuffled!');
        nextTrack();
      } else if (currentTrack) {
        haptic([10, 30]);
        audio.currentTime = 0;
        showToast('🔀 Replaying');
      }
    }
  }

  if (typeof DeviceMotionEvent.requestPermission === 'function') {
    document.addEventListener('touchend', function askPerm() {
      DeviceMotionEvent.requestPermission()
        .then(r => { if (r === 'granted') window.addEventListener('devicemotion', onMotion, { passive: true }); })
        .catch(() => {});
      document.removeEventListener('touchend', askPerm);
    }, { once: true });
  } else {
    window.addEventListener('devicemotion', onMotion, { passive: true });
  }
}

// ─── PLAYER OPEN / CLOSE ─────────────────────────────────────────────────────
function openFullscreen() {
  const fp = document.getElementById('fullscreen-player');
  const mp = document.getElementById('mini-player');
  fp.style.transform = '';
  fp.classList.add('open');
  if (mp) {
    mp.style.transition  = 'opacity 0.2s ease, transform 0.2s ease';
    mp.style.opacity     = '0';
    mp.style.pointerEvents = 'none';
  }
  updateNextStrip();
  setTimeout(() => _attachArtSwipe(), 100);
  if (!document.hidden && !isLowEnd) _startViz();
  if (!isLowEnd) document.getElementById('ambient-canvas')?.classList.add('orbs-active');
}

function closeFullscreen() {
  const fp = document.getElementById('fullscreen-player');
  const mp = document.getElementById('mini-player');
  if (!fp.classList.contains('open')) return;
  fp.style.transform = '';
  fp.classList.remove('open');
  closeQueuePanel();
  _stopViz();
  document.getElementById('ambient-canvas')?.classList.remove('orbs-active');
  if (mp) {
    fp._closeId = (fp._closeId || 0) + 1;
    const closeId = fp._closeId;
    setTimeout(() => {
      if (fp._closeId !== closeId) return;
      mp.style.transition    = '';
      mp.style.opacity       = '';
      mp.style.pointerEvents = '';
      if (currentTrack) showMiniPlayer();
    }, 220);
  }
}

// ─── QUEUE PANEL ─────────────────────────────────────────────────────────────
function toggleQueuePanel() { queuePanelOpen ? closeQueuePanel() : openQueuePanel(); }

function openQueuePanel() {
  const panel = document.getElementById('queue-panel');
  const btn   = document.getElementById('fp-queue-btn');
  if (!panel) return;
  panel.style.transform = '';
  panel.style.transition = '';
  queuePanelOpen = true;
  panel.classList.add('open');
  if (btn) btn.classList.add('queue-open');
  requestAnimationFrame(() => {
    if (typeof updateQueuePanel === 'function') updateQueuePanel();
    if (typeof setupSwipeToRemove === 'function') setupSwipeToRemove();
  });
}

function closeQueuePanel() {
  const panel = document.getElementById('queue-panel');
  const btn   = document.getElementById('fp-queue-btn');
  if (!panel) return;
  queuePanelOpen = false;
  panel.style.transform = '';
  panel.classList.remove('open');
  if (btn) btn.classList.remove('queue-open');
}

function updateQueuePanel() {
  const body    = document.getElementById('queue-panel-body');
  const countEl = document.getElementById('queue-count');
  if (!body) return;

  body.innerHTML = '';
  if (!currentQueue.length) {
    body.innerHTML = '<div style="padding:32px;text-align:center;color:var(--text3);font-size:12px;">Queue is empty</div>';
    return;
  }
  const remaining = currentQueue.length - currentIndex - 1;
  if (countEl) countEl.textContent = remaining + ' songs remaining';

  if (currentTrack) {
    const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = 'NOW PLAYING';
    body.appendChild(sec);
    body.appendChild(makeQueueItem(currentTrack, currentIndex, true));
  }

  const nextSongs = currentQueue.slice(currentIndex + 1);
  if (nextSongs.length) {
    const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = `UP NEXT (${nextSongs.length})`;
    body.appendChild(sec);
    nextSongs.forEach((s, i) => body.appendChild(makeQueueItem(s, currentIndex + 1 + i, false)));
  }

  if (currentIndex > 0) {
    const prevSongs = currentQueue.slice(Math.max(0, currentIndex - 8), currentIndex);
    if (prevSongs.length) {
      const sec = document.createElement('div'); sec.className = 'queue-section-label'; sec.textContent = 'PREVIOUSLY PLAYED';
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
  const dur = song.trackTimeMillis ? formatMs(song.trackTimeMillis) : '';
  item.dataset.dur = dur;
  const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
  setImgSrc(img, artUrl);
  item.appendChild(img);
  const info = document.createElement('div'); info.className = 'queue-item-info';
  info.innerHTML = `<div class="queue-item-title">${esc(song.trackName)}</div><div class="queue-item-artist">${esc(song.artistName)}</div>`;
  item.appendChild(info);
  if (isCurrent && isPlaying) {
    const bar = document.createElement('div'); bar.className = 'queue-now-playing';
    bar.innerHTML = '<span></span><span></span><span></span>';
    item.appendChild(bar);
  } else {
    const d = document.createElement('span'); d.className = 'queue-item-dur'; d.textContent = dur;
    item.appendChild(d);
  }
  if (!isCurrent) item.onclick = () => { currentIndex = qIdx; loadTrack(currentQueue[currentIndex]); updateQueuePanel(); closeQueuePanel(); };
  return item;
}

// ─── SWIPE TO REMOVE FROM QUEUE ───────────────────────────────────────────────
function setupSwipeToRemove() {
  const queueBody = document.getElementById('queue-panel-body');
  if (!queueBody || queueBody._swipeAttached) return;
  queueBody._swipeAttached = true;

  let startX = 0, startY = 0, startTime = 0;
  let currentItem = null;
  let isSwiping = false;
  let swipeThreshold = 80;

  queueBody.addEventListener('touchstart', (e) => {
    const item = e.target.closest('.queue-item');
    if (!item || item.classList.contains('current')) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    startTime = Date.now();
    currentItem = item;
    isSwiping = true;
    item.style.transition = 'none';
  }, { passive: true });

  queueBody.addEventListener('touchmove', (e) => {
    if (!isSwiping || !currentItem) return;
    const dx = e.touches[0].clientX - startX;
    const dy = e.touches[0].clientY - startY;
    if (Math.abs(dx) < 15 && Math.abs(dy) < 15) return;
    if (Math.abs(dx) > Math.abs(dy) && dx < 0) {
      e.preventDefault();
      const translateX = Math.max(-swipeThreshold, dx);
      const opacity = 1 - (Math.abs(translateX) / swipeThreshold) * 0.8;
      currentItem.style.transform = `translateX(${translateX}px)`;
      currentItem.style.opacity = opacity;
      currentItem.style.transition = 'none';
    }
  }, { passive: false });

  queueBody.addEventListener('touchend', (e) => {
    if (!isSwiping || !currentItem) { resetSwipe(); return; }
    const endX = e.changedTouches[0].clientX;
    const dx = endX - startX;
    const dt = Date.now() - startTime;
    const velocity = Math.abs(dx) / dt;
    if (dx < -swipeThreshold || (dx < -40 && velocity > 0.3)) {
      const trackId = currentItem.dataset.trackId;
      removeFromQueue(trackId);
      currentItem.style.transition = 'transform 0.2s ease, opacity 0.15s ease';
      currentItem.style.transform = 'translateX(-100%)';
      currentItem.style.opacity = '0';
      setTimeout(() => {
        if (currentItem && currentItem.parentNode) currentItem.remove();
        updateQueuePanel();
        updateNextStrip();
      }, 180);
    } else {
      currentItem.style.transition = 'transform 0.25s cubic-bezier(0.2,0.9,0.4,1.1), opacity 0.2s ease';
      currentItem.style.transform = '';
      currentItem.style.opacity = '';
    }
    resetSwipe();
  });

  function resetSwipe() {
    if (currentItem) {
      currentItem.style.transition = '';
      currentItem.style.transform = '';
      currentItem.style.opacity = '';
    }
    isSwiping = false;
    currentItem = null;
    startX = 0;
    startY = 0;
  }

  queueBody.addEventListener('touchcancel', resetSwipe);
}

function removeFromQueue(trackId) {
  const index = currentQueue.findIndex(s => String(s.trackId) === String(trackId));
  if (index === -1) return;
  if (index === currentIndex) { showToast("Can't remove currently playing song"); return; }
  currentQueue.splice(index, 1);
  if (index < currentIndex) currentIndex--;
  updateQueuePanel();
  updateNextStrip();
  haptic(15);
  showToast('Removed from queue');
}

// ─── QUEUE PANEL DIRECT GESTURE ───────────────────────────────────────────────
function setupQueuePanelGesture() {
  const qp = document.getElementById('queue-panel');
  if (!qp) return;

  let startY = 0, startX = 0, isDragging = false, startTime = 0;
  let moved = false, rafId = null, axisLocked = null;

  function snapBack() {
    qp.style.transition = 'transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
    qp.style.transform  = '';
    qp.style.willChange = '';
    setTimeout(() => { qp.style.transition = ''; }, 340);
    _resumeBlur();
  }

  qp.addEventListener('touchstart', e => {
    const onHandle = e.target.closest('#queue-drag-handle') || e.target.closest('.queue-panel-handle');
    const body = document.getElementById('queue-panel-body');
    const bodyAtTop = !body || body.scrollTop <= 0;
    if (!onHandle && !bodyAtTop) return;
    startY     = e.touches[0].clientY;
    startX     = e.touches[0].clientX;
    isDragging = true;
    startTime  = Date.now();
    moved      = false;
    axisLocked = null;
    qp.style.transition  = 'none';
    qp.style.willChange  = 'transform';
    qp.classList.add('dragging');
    _pauseBlur();
  }, { passive: true });

  qp.addEventListener('touchmove', e => {
    if (!isDragging) return;
    const dy    = e.touches[0].clientY - startY;
    const dx    = e.touches[0].clientX - startX;
    const absDy = Math.abs(dy);
    const absDx = Math.abs(dx);
    if (!axisLocked && (absDy > 6 || absDx > 6)) {
      axisLocked = absDx > absDy ? 'horizontal' : 'vertical';
    }
    if (axisLocked === 'horizontal') { isDragging = false; return; }
    if (axisLocked === 'vertical' && dy > 0 && absDy > 4) {
      moved = true;
      e.preventDefault();
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(() => {
        qp.style.transform = `translateY(${dy}px)`;
        rafId = null;
      });
    }
  }, { passive: false });

  qp.addEventListener('touchend', e => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    if (!isDragging) return;
    isDragging = false;
    qp.classList.remove('dragging');
    qp.style.willChange = '';
    const dy  = e.changedTouches[0].clientY - startY;
    const dt  = Math.max(1, Date.now() - startTime);
    const vel = dy / dt;
    _resumeBlur();
    if (!moved) { snapBack(); return; }
    if (dy > 90 || (vel > 0.45 && dy > 25)) {
      qp.style.transform = '';
      closeQueuePanel();
    } else {
      snapBack();
    }
  }, { passive: true });

  qp.addEventListener('touchcancel', () => {
    if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
    isDragging = false;
    qp.classList.remove('dragging');
    qp.style.willChange = '';
    snapBack();
  }, { passive: true });
}

// ─── ARTIST PAGE ─────────────────────────────────────────────────────────────
function openArtistPage(artistName, songs, artUrl) {
  let page = document.getElementById('artist-page');
  if (!page) {
    page = document.createElement('div');
    page.id = 'artist-page';
    page.className = 'artist-page';
    document.getElementById('app').appendChild(page);
  }

  const thumbUrl = artUrl || (songs[0] ? getArtUrl(songs[0], '600x600') : '');

  page.innerHTML = `
    <div class="artist-page-hero">
      <img class="artist-page-bg" src="${thumbUrl}" alt="" crossorigin="anonymous">
      <div class="artist-page-overlay"></div>
      <div class="artist-page-topbar">
        <button class="ap-back-btn" onclick="closeArtistPage()" aria-label="Back">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
        <div class="ap-logo">
          <svg viewBox="0 0 28 28" fill="none" width="20" height="20"><path d="M4 23L10 7L14 16L18 7L24 23" stroke="rgba(184,150,64,0.28)" stroke-width="1" stroke-linecap="round"/><path d="M6.5 23L12 8.5L14 13" stroke="var(--gold-l)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M14 13L16 8.5L21.5 23" stroke="var(--gold)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
          <span>Aurum</span>
        </div>
        <button class="ap-share-btn" onclick="_shareArtist('${esc(artistName)}')" aria-label="Share">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
        </button>
      </div>
      <div class="artist-page-info">
        <div class="ap-artist-name">${esc(artistName)}</div>
        <div class="ap-track-count">${songs.length} songs</div>
      </div>
      <div class="artist-page-actions">
        <button class="ap-play-btn" onclick="_playArtistAll()">
          <svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3" fill="currentColor"/></svg>
          Play All
        </button>
        <button class="ap-shuffle-btn" onclick="_playArtistShuffle()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/></svg>
        </button>
      </div>
    </div>
    <div class="artist-page-songs" id="ap-songs-list"></div>
  `;

  page._songs = songs;
  page._artistName = artistName;

  const list = document.getElementById('ap-songs-list');
  songs.forEach((s, i) => list.appendChild(makeSongRow(s, i, songs)));

  requestAnimationFrame(() => page.classList.add('open'));
}

function closeArtistPage() {
  const page = document.getElementById('artist-page');
  if (!page) return;
  page.classList.remove('open');
  setTimeout(() => page.remove(), 340);
}

function _playArtistAll() {
  const page = document.getElementById('artist-page');
  if (!page?._songs?.length) return;
  playSongs(page._songs, 0);
  openFullscreen();
}

function _playArtistShuffle() {
  const page = document.getElementById('artist-page');
  if (!page?._songs?.length) return;
  playSongs(page._songs, Math.floor(Math.random() * page._songs.length));
  shuffleOn = true;
  const sb = document.getElementById('shuffle-btn');
  if (sb) sb.querySelector('svg').style.stroke = 'var(--gold-l)';
  showToast('Shuffle on');
  openFullscreen();
}

function _shareArtist(artistName) {
  if (navigator.share) {
    navigator.share({ title: artistName + ' on Aurum', text: 'Check out ' + artistName + ' on Aurum!', url: window.location.href }).catch(() => {});
  } else {
    navigator.clipboard?.writeText(window.location.href).then(() => showToast('Link copied'));
  }
}

async function openArtistPageFromName(artistName) {
  showToast('Loading ' + artistName + '…');
  try {
    const q = encodeURIComponent(artistName + ' songs');
    const r = await fetch(`/api/songs?q=${q}`);
    const d = await r.json();
    const songs = (d.results || []).filter(s => s.previewUrl);
    if (!songs.length) { showToast('No songs found'); return; }
    openArtistPage(artistName, songs, getArtUrl(songs[0], '600x600'));
  } catch(e) { showToast('Could not load artist'); }
}

async function fetchRecommendations(song) {
  if (!song) return;
  try {
    const artist = song.artistName?.split(/[&,]|feat\.|ft\./i)[0].trim() || '';
    const r = await fetch(`/api/songs?q=${encodeURIComponent(artist + ' songs')}`);
    const d = await r.json();
    const recs = (d.results || []).filter(s => s.previewUrl && String(s.trackId) !== String(song.trackId));
    if (recs.length) {
      const existingIds = new Set(currentQueue.map(s => String(s.trackId)));
      const newRecs = recs.filter(s => !existingIds.has(String(s.trackId))).slice(0, 8);
      currentQueue = [...currentQueue, ...newRecs];
      if (queuePanelOpen) updateQueuePanel();
      updateNextStrip();
    }
  } catch(e) {}
}

// ─── RECENTLY PLAYED ─────────────────────────────────────────────────────────
function addToRecentlyPlayed(song) {
  recentlyPlayed = recentlyPlayed.filter(s => String(s.trackId) !== String(song.trackId));
  recentlyPlayed.unshift(song);
  if (recentlyPlayed.length > 20) recentlyPlayed = recentlyPlayed.slice(0, 20);
  localStorage.setItem('aurum_recent_played', JSON.stringify(recentlyPlayed));
  _trackListen(song); // ← algorithm tracking
  renderQuickResume();
}

// ─── HOME SECTIONS ────────────────────────────────────────────────────────────
const SECTION_POOL = [
  { id:'recent',   title:'Continue Listening', type:'wide',  fn: getRecentlyPlayedSongs },
  { id:'featured', title:'Made For You',       type:'featured', queries:['top bollywood songs hits','best hindi songs','latest bollywood hits','top hindi songs trending','best bollywood songs playlist'] },
  { id:'trending', title:'Trending Now',       type:'cards', queries:['trending hindi songs chart','bollywood chart toppers','top hindi songs this week','most popular bollywood songs'] },
  { id:'mood',     title:'Mood: Late Night',   type:'bw',    queries:['sad emotional bollywood songs','heartbreak hindi songs arijit','late night slow songs hindi','emotional romantic songs hindi'] },
  { id:'romantic', title:'Bollywood Romantic', type:'bw',    queries:['bollywood romantic songs hits','best romantic hindi songs','love songs bollywood','romantic songs arijit atif'] },
  { id:'classic',  title:'Golden Era',         type:'bw',    queries:['90s bollywood romantic classic songs','80s hindi classic songs','old is gold bollywood songs kishore kumar','retro bollywood hits lata mangeshkar'] },
  { id:'hiphop',   title:'Desi Hip-Hop',       type:'cards', queries:['divine emiway bantai rap hindi','desi hip hop india rap songs','yo yo honey singh badshah rap','india rap gully boy songs'] },
  { id:'lofi',     title:'Lo-Fi Chill',        type:'cards', queries:['lofi chill beats hindi songs','lofi bollywood remix chill','lo-fi hindi songs study chill','lofi beats india relaxing'] },
  { id:'arijit',   title:'Arijit Singh',       type:'rows',  queries:['arijit singh best romantic songs','arijit singh top hits','arijit singh soulful songs','arijit singh emotional hits'] },
  { id:'atif',     title:'Atif Aslam',         type:'rows',  queries:['atif aslam best songs','atif aslam top hits hindi','atif aslam romantic songs','atif aslam soulful'] },
  { id:'shreya',   title:'Shreya Ghoshal',     type:'rows',  queries:['shreya ghoshal best songs','shreya ghoshal romantic hits','shreya ghoshal top songs'] },
  { id:'neha',     title:'Neha Kakkar',        type:'rows',  queries:['neha kakkar best songs','neha kakkar hits','neha kakkar popular songs'] },
  { id:'kumar',    title:'Kumar Sanu',         type:'rows',  queries:['kumar sanu 90s hits','kumar sanu best songs','kumar sanu alka yagnik duets'] },
  { id:'kishore',  title:'Kishore Kumar',      type:'rows',  queries:['kishore kumar best songs','kishore kumar classics','kishore kumar evergreen hits'] },
  { id:'workout',  title:'Energy Boost',       type:'cards', queries:['workout hindi songs gym','upbeat dance bollywood songs','party hindi songs badshah','high energy bollywood beats'] },
  { id:'sad',      title:'Heartbreak',         type:'bw',    queries:['sad hindi songs breakup','dil tod ke chali gayi','heartbreak bollywood songs','tere bina hindi sad songs'] },
  { id:'party',    title:'Party Hits',         type:'cards', queries:['bollywood party songs dance','badshah party hits','punjabi party songs','hindi dance floor hits'] },
  { id:'new',      title:'New Releases',       type:'cards', queries:['new hindi songs latest','new bollywood songs released','latest hindi songs hits','new bollywood songs trending'] },
];

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
      for (let i = songs.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [songs[i], songs[j]] = [songs[j], songs[i]];
      }
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
    if (type === 'wide')      html += `<div class="wide-sk"><div class="wide-sk-cover"></div><div class="wide-sk-line" style="width:80%"></div><div class="wide-sk-line" style="width:50%;margin-top:4px;"></div></div>`;
    else if (type === 'bw')   html += `<div class="bw-sk"><div class="bw-sk-cover"></div><div class="bw-sk-line w70"></div><div class="bw-sk-line w45"></div></div>`;
    else                      html += `<div class="quick-sk"><div class="quick-sk-cover"></div><div class="bw-sk-line w70" style="margin-top:8px;"></div><div class="bw-sk-line w45" style="margin-top:5px;"></div></div>`;
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
  container.innerHTML = '';
  currentGenre = genre;

  let sections = [];

  if (genre === 'all') {
    // ── Always pinned ──────────────────────────────────────────
    sections.push(SECTION_POOL.find(s => s.id === 'recent'));
    sections.push(SECTION_POOL.find(s => s.id === 'featured'));

    // ── Algorithm: top artists from listen history ─────────────
    const topArtists = _getTopArtists(3);
    topArtists.forEach(({ artist, count }) => {
      const id = 'artist_' + artist.replace(/\s+/g, '_').toLowerCase();
      // Agar already SECTION_POOL mein hai to use wahi
      const existing = SECTION_POOL.find(s => s.title === artist);
      if (existing) {
        sections.push(existing);
      } else {
        // Dynamic section banao
        sections.push({
          id,
          title: artist,
          type: 'rows',
          queries: [
            `${artist} best songs`,
            `${artist} top hits`,
            `${artist} popular songs`,
          ],
          _isAlgo: true,
          _listenCount: count,
        });
      }
    });

    // ── Trending always aaye ───────────────────────────────────
    sections.push(SECTION_POOL.find(s => s.id === 'trending'));

    // ── Baaki sections random rotate karo (history ke artists remove kar ke) ──
    const usedIds = new Set(sections.map(s => s?.id));
    const rest = SECTION_POOL
      .filter(s => s && !usedIds.has(s.id))
      .sort(() => Math.random() - 0.5)
      .slice(0, 4);
    sections.push(...rest);

  } else {
    const ids = genreSections[genre] || ['featured','trending','new','classic'];
    sections = ids.map(id => SECTION_POOL.find(s => s.id === id)).filter(Boolean);
  }

  sections = sections.filter(Boolean);

  sections.forEach(sec => {
    const wrap = document.createElement('div'); wrap.className = 'section'; wrap.id = 'sec-wrap-' + sec.id;
    if (sec.id === 'recent' && !recentlyPlayed.length) wrap.style.display = 'none';
    const type      = sec.type === 'featured' ? 'cards' : sec.type;
    const typeCount = type === 'bw' ? 5 : type === 'wide' ? 5 : type === 'rows' ? 0 : 5;
    // Algorithm section ke liye badge dikhao
    const badge = sec._isAlgo ? ` <span style="font-size:9px;background:rgba(184,150,64,0.15);color:var(--gold);padding:2px 7px;border-radius:20px;font-weight:700;vertical-align:middle;">FOR YOU</span>` : '';
    wrap.innerHTML  = `<div class="section-head"><h2>${sec.title}${badge}</h2><span onclick="refreshSection('${sec.id}')">Refresh</span></div><div id="sec-${sec.id}">${type === 'rows' ? renderRowSkeleton() : renderSkeletonSection(type, typeCount)}</div>`;
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
    songs.slice(0, 8).forEach((s, i) => row.appendChild(makeWideCard(s, i, songs)));
    el.appendChild(row);
  } else if (type === 'bw') {
    const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
    songs.slice(0, 8).forEach((s, i) => row.appendChild(makeBwCard(s, i, songs, BOLLYWOOD_META[i % BOLLYWOOD_META.length])));
    el.appendChild(row);
  } else {
    const row = document.createElement('div'); row.className = 'h-scroll-row'; row.style.paddingRight = '20px';
    songs.slice(0, 8).forEach((s, i) => row.appendChild(makeQuickCard(s, i, songs)));
    el.appendChild(row);
  }
}

async function refreshSection(secId) {
  const sec  = SECTION_POOL.find(s => s.id === secId); if (!sec) return;
  const wrap = document.getElementById('sec-wrap-' + secId); if (!wrap) return;
  const el   = document.getElementById('sec-' + secId); if (!el) return;
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
  div.onclick = () => playSongs(queue, i);
  return div;
}

function makeWideCard(s, i, queue) {
  const div = document.createElement('div'); div.className = 'wide-card anim-in';
  div.style.animationDelay = (i * 0.05) + 's';
  const cover = document.createElement('div'); cover.className = 'wide-card-cover';
  const img = document.createElement('img'); img.alt = esc(s.trackName); img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, '400x400'));
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
  const cover = document.createElement('div'); cover.className = 'bw-card-cover';
  const img = document.createElement('img'); img.alt = esc(s.trackName); img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, '400x400'));
  cover.appendChild(img);
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
  row.dataset.trackId = s.trackId;
  row.style.animationDelay = (i * 0.034) + 's';
  const dur = s.trackTimeMillis ? formatMs(s.trackTimeMillis) : '';
  row.dataset.dur = dur;

  const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy';
  setImgSrc(img, getArtUrl(s, '300x300'));
  row.appendChild(img);

  const info = document.createElement('div'); info.className = 'song-row-info';
  const titleDiv = document.createElement('div'); titleDiv.className = 'song-row-title'; titleDiv.textContent = s.trackName || '';
  const artistDiv = document.createElement('div'); artistDiv.className = 'song-row-artist';
  const artistSpan = document.createElement('span'); artistSpan.className = 'artist-link'; artistSpan.textContent = s.artistName || '';
  artistDiv.appendChild(artistSpan);
  info.appendChild(titleDiv); info.appendChild(artistDiv);
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
  row._song = s;

  // ── Pointer tracking — artist click vs row click vs long press ──
  let _pt        = null;
  let _longFired = false;
  let _moved     = false;
  let _downX     = 0;
  let _downY     = 0;

  row.addEventListener('pointerdown', e => {
    _longFired = false;
    _moved     = false;
    _downX     = e.clientX;
    _downY     = e.clientY;
    if (isTV) return;
    _pt = setTimeout(() => {
      if (_moved) return;
      _longFired = true;
      row.classList.add('long-press-active');
      haptic([20, 40, 20]);
      openSongModal(s);
      setTimeout(() => row.classList.remove('long-press-active'), 300);
    }, 480);
  }, { passive: true });

  row.addEventListener('pointermove', e => {
    const dx = Math.abs(e.clientX - _downX);
    const dy = Math.abs(e.clientY - _downY);
    if (dx > 8 || dy > 8) {
      _moved = true;
      if (_pt) { clearTimeout(_pt); _pt = null; }
    }
  }, { passive: true });

  row.addEventListener('pointerup', () => {
    if (_pt) { clearTimeout(_pt); _pt = null; }
  }, { passive: true });

  row.addEventListener('pointercancel', () => {
    if (_pt) { clearTimeout(_pt); _pt = null; }
    row.classList.remove('long-press-active');
    _moved = false;
  }, { passive: true });

  // ── Click — artist span check karo pehle ────────────────────────
  row.addEventListener('click', e => {
    if (_longFired) return;
    if (_moved) return;
    if (e.target.closest('.song-row-heart') || e.target.closest('.song-row-more')) return;

    // Artist span click — playlist open karo
    if (e.target === artistSpan || artistSpan.contains(e.target)) {
      e.stopPropagation();
      const name = (s.artistName || '').split(/[&,]/)[0].trim();
      if (name) openArtistPageFromName(name);
      return;
    }

    // Baaki jagah click — song play karo
    playSongs(queue, i);
    haptic(8);
  });

  return row;
}

// ─── GENRE FILTER ────────────────────────────────────────────────────────────
function filterHome(genre, chip) {
  document.querySelectorAll('#home-chips .chip').forEach(c => c.classList.remove('active'));
  chip.classList.add('active', 'popping');
  chip.addEventListener('animationend', () => chip.classList.remove('popping'), { once: true });
  haptic(8);
  buildHomeSections(genre);
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

document.getElementById('search-input').addEventListener('focus', function() {
  if (!this.value.trim()) renderSearchIdle();
});
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
  clearTimeout(_searchTimeout);
  renderSearchIdle();
}

function _saveSearchToStorage(searches) { localStorage.setItem('aurum_recent', JSON.stringify(searches)); }

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
  saveRecentSearch(q);
  doSearch(q);
}

function tapRecentSearch(q) {
  document.getElementById('search-input').value = q;
  document.getElementById('search-clear').style.display = 'flex';
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
    body.innerHTML = `<div class="search-placeholder"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg><h3>Nothing found</h3><p>Try a different name or artist</p></div>`;
    return;
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
  if (isSaved(song)) {
    savedSongs = savedSongs.filter(s => String(s.trackId) !== String(song.trackId));
    showToast('Removed from library');
  } else {
    savedSongs.push(song);
    showToast('Saved to library');
  }
  localStorage.setItem('aurum_saved', JSON.stringify(savedSongs));
  renderLibrary();
  updateSaveBtn();
}

function playLikedSongs() {
  if (!savedSongs.length) { showToast('Save some songs first'); return; }
  playSongs(savedSongs, 0);
  openFullscreen();
}

function switchLibTab(tab, el) {
  document.querySelectorAll('.lib-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  currentLibTab = tab;
  document.getElementById('lib-playlists').style.display  = tab === 'playlists'  ? '' : 'none';
  document.getElementById('lib-saved').style.display      = tab === 'saved'      ? '' : 'none';
  const dlEl = document.getElementById('lib-downloads');
  if (dlEl) dlEl.style.display = tab === 'downloads' ? '' : 'none';
  haptic(8);
  renderLibrary();
}

function renderLibrary() {
  renderPlaylists();
  renderSavedSongs();
  renderDownloadedSongs();
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
    const card = document.createElement('div'); card.className = 'playlist-card';
    if (isTV) card.tabIndex = 0;
    card.onclick = () => openPlaylistDetail(i);
    const songs = pl.songs || [];
    const coverWrap = document.createElement('div'); coverWrap.className = 'playlist-card-cover';
    if (songs.length >= 4) {
      const grid4 = document.createElement('div'); grid4.className = 'playlist-card-grid';
      songs.slice(0, 4).forEach(s => { const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy'; setImgSrc(img, getArtUrl(s, '300x300')); grid4.appendChild(img); });
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
    list.innerHTML = `<div class="empty-library"><svg viewBox="0 0 24 24"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg><h3>No liked songs yet</h3><p>Tap the heart on any song to save it here</p></div>`;
    return;
  }
  savedSongs.forEach((s, i) => list.appendChild(makeSongRow(s, i, savedSongs)));
}

// ─── DOWNLOADS (IndexedDB) ────────────────────────────────────────────────────
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
    tx.oncomplete = () => res();
    tx.onerror    = () => rej(tx.error);
  });
}

async function deleteFromDb(trackId) {
  const db = await openDlDb();
  return new Promise((res, rej) => {
    const tx = db.transaction('songs', 'readwrite');
    tx.objectStore('songs').delete(trackId);
    tx.oncomplete = () => res();
    tx.onerror    = () => rej(tx.error);
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
async function downloadSongOffline(song, customUrl, customQuality) {
  const rawUrl = customUrl || song.previewUrl;
  const quality = customQuality || 'preview';
  await _warnIfStorageNotPersisted();
  showToast('Saving to app…');
  try {
    let blob = null;
    const urls = [rawUrl, song.previewUrl].filter(Boolean);
    for (const url of urls) {
      try {
        const r = await fetch(url);
        if (r.ok) { blob = await r.blob(); break; }
      } catch(e) { continue; }
    }
    if (!blob) throw new Error('All URLs failed');
    await saveToDb({ ...song, _quality: quality }, blob);
    const metas = JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]')
      .filter(s => String(s.trackId) !== String(song.trackId));
    metas.unshift({
      trackId: song.trackId,
      trackName: song.trackName,
      artistName: song.artistName,
      artworkUrl100: song.artworkUrl100,
      _quality: quality,
      _savedAt: Date.now()
    });
    localStorage.setItem('aurum_dl_meta', JSON.stringify(metas));
    haptic([20, 50, 20]);
    showToast('Saved to app ✓');
    renderLibrary();
  } catch(e) {
    showToast('Save failed — check connection');
    console.error('[Offline Save]', e);
  }
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
      audio.pause();
      audio.src = newUrl;
      audio.load();
      audio.play().then(() => {
        if (_lastObjectUrl && _lastObjectUrl !== newUrl) {
          URL.revokeObjectURL(_lastObjectUrl);
        }
        _lastObjectUrl = newUrl;
        isPlaying = true; currentTrack = rec; currentQuality = 'full';
        _dismissedTrackId = null;
        updatePlayerUI(); showMiniPlayer();
      }).catch(() => {
        URL.revokeObjectURL(newUrl);
      });
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
    list.innerHTML = `<div class="empty-library"><svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><h3>No downloads yet</h3><p>Save songs offline from the player or song menu</p></div>`;
    return;
  }
  const hdr = document.createElement('div');
  hdr.style.cssText = 'padding:4px 22px 10px;display:flex;align-items:center;justify-content:space-between;';
  hdr.innerHTML = `<span style="font-size:11px;color:var(--text3);font-weight:600;">${songs.length} song${songs.length!==1?'s':''} saved offline</span><button style="font-size:11px;color:var(--text3);background:none;border:none;cursor:pointer;font-family:Sora,sans-serif;" onclick="confirmClearDownloads()">Clear all</button>`;
  list.appendChild(hdr);
  songs.forEach(s => {
    const row = document.createElement('div'); row.className = 'song-row anim-in'; row.dataset.trackId = s.trackId;
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
  currentPlaylistIndex = i;
  const pl   = playlists[i];
  const songs = pl.songs || [];
  document.getElementById('pl-detail-name').textContent  = pl.name;
  document.getElementById('pl-detail-title').textContent = pl.name;
  document.getElementById('pl-detail-sub').textContent   = songs.length + ' songs';
  const coverEl = document.getElementById('pl-big-cover');
  if (!songs.length) {
    const emptyDiv = document.createElement('div'); emptyDiv.id = 'pl-big-cover';
    emptyDiv.style.cssText = 'width:100%;max-width:248px;aspect-ratio:1;border-radius:20px;background:var(--surface2);display:flex;align-items:center;justify-content:center;';
    coverEl.replaceWith(emptyDiv);
  } else if (songs.length < 4) {
    const img = document.createElement('img'); img.id = 'pl-big-cover'; img.className = 'pl-big-cover'; img.alt = '';
    setImgSrc(img, getArtUrl(songs[0], '500x500')); coverEl.replaceWith(img);
  } else {
    const g = document.createElement('div'); g.id = 'pl-big-cover'; g.className = 'pl-big-cover-grid';
    songs.slice(0, 4).forEach(s => { const img = document.createElement('img'); img.alt = ''; img.loading = 'lazy'; setImgSrc(img, getArtUrl(s, '300x300')); g.appendChild(img); });
    coverEl.replaceWith(g);
  }
  const sl = document.getElementById('pl-songs-list');
  sl.innerHTML = '';
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
  shuffleOn = true;
  document.getElementById('shuffle-btn').querySelector('svg').style.stroke = 'var(--gold-l)';
  closePlaylistDetail(); openFullscreen();
}

function openPlaylistOpts(e, i) {
  e.stopPropagation(); optsPlaylistIndex = i;
  document.getElementById('pl-opts-title').textContent = playlists[i]?.name || 'Playlist';
  const modal = document.getElementById('playlist-opts-modal');
  modal.style.display = '';
  modal.classList.add('open');
}
function closePlaylistOpts(e) {
  if (e && !e.target.closest) return;
  if (e && e.target.closest('.modal-sheet')) return;
  const modal = document.getElementById('playlist-opts-modal');
  modal.classList.remove('open');
  modal.style.display = 'none';
  optsPlaylistIndex = null;
}
function openRenameModal() {
  closePlaylistOpts();
  if (optsPlaylistIndex === null) return;
  document.getElementById('rename-input').value = playlists[optsPlaylistIndex].name || '';
  const modal = document.getElementById('rename-modal');
  modal.style.display = '';
  modal.classList.add('open');
  setTimeout(() => document.getElementById('rename-input').focus(), 360);
}
function closeRenameModal() {
  const modal = document.getElementById('rename-modal');
  modal.classList.remove('open');
  modal.style.display = 'none';
}
function confirmRename() { const name = document.getElementById('rename-input').value.trim(); if (!name || optsPlaylistIndex === null) { showToast('Enter a name'); return; } playlists[optsPlaylistIndex].name = name; localStorage.setItem('aurum_playlists', JSON.stringify(playlists)); closeRenameModal(); renderPlaylists(); showToast('Renamed'); }
function confirmDeletePlaylist() { if (optsPlaylistIndex === null) return; const name = playlists[optsPlaylistIndex].name; playlists.splice(optsPlaylistIndex, 1); localStorage.setItem('aurum_playlists', JSON.stringify(playlists)); closePlaylistOpts(); renderPlaylists(); showToast(`"${name}" deleted`); }
function openCreatePlaylist() {
  const modal = document.getElementById('create-playlist-modal');
  modal.style.display = '';
  modal.classList.add('open');
  setTimeout(() => document.getElementById('playlist-name-input').focus(), 360);
}
function closeCreatePlaylist() {
  const modal = document.getElementById('create-playlist-modal');
  modal.classList.remove('open');
  modal.style.display = 'none';
  document.getElementById('playlist-name-input').value = '';
}
function createPlaylist() { const name = document.getElementById('playlist-name-input').value.trim(); if (!name) { showToast('Enter a playlist name'); return; } playlists.push({ name, songs:[] }); localStorage.setItem('aurum_playlists', JSON.stringify(playlists)); closeCreatePlaylist(); renderPlaylists(); showToast('"' + name + '" created'); }

function openSongModal(song) {
  if (!song) return;
  modalTrack = song;
  const art = document.getElementById('modal-song-art'); if (art) setImgSrc(art, getArtUrl(song, '300x300'));
  document.getElementById('modal-song-title').textContent  = song.trackName  || 'Unknown';
  document.getElementById('modal-song-artist').textContent = song.artistName || 'Unknown';
  document.getElementById('modal-save-label').textContent  = isSaved(song) ? 'Remove from Library' : 'Save to Library';
  document.getElementById('song-modal').classList.add('open');
}

function closeSongModal(e) { if (e && e.target.closest?.('.modal-sheet')) return; document.getElementById('song-modal').classList.remove('open'); modalTrack = null; }
function modalSave() { if (!modalTrack) return; toggleSave(modalTrack); document.getElementById('modal-save-label').textContent = isSaved(modalTrack) ? 'Remove from Library' : 'Save to Library'; document.getElementById('song-modal').classList.remove('open'); modalTrack = null; }
function playNext() { if (!modalTrack) return; currentQueue.splice(currentIndex + 1, 0, modalTrack); showToast('Playing next'); document.getElementById('song-modal').classList.remove('open'); modalTrack = null; updateQueuePanel(); }
function modalDownload() { if (!modalTrack) return; const s = modalTrack; document.getElementById('song-modal').classList.remove('open'); _downloadSong = s; modalTrack = null; openDownloadModal(); }

function openAddToPlaylistModal() {
  const songToAdd = modalTrack;
  document.getElementById('song-modal').classList.remove('open');
  const opts = document.getElementById('add-playlist-options');
  opts.innerHTML = '';
  if (!playlists.length) {
    opts.innerHTML = `<div style="padding:12px 0;text-align:center;color:var(--text3);font-size:12px;">No playlists yet.</div>`;
  } else {
    playlists.forEach((pl, i) => {
      const div = document.createElement('div'); div.className = 'modal-option';
      div.innerHTML = `<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg><span>${esc(pl.name)} <span style="color:var(--text3);font-size:10px;">(${pl.songs.length})</span></span>`;
      div.onclick = () => addToPlaylist(i, songToAdd);
      opts.appendChild(div);
    });
  }
  document.getElementById('add-playlist-modal').classList.add('open');
}

function closeAddToPlaylistModal(e) { if (e && e.target.closest?.('.modal-sheet')) return; document.getElementById('add-playlist-modal').classList.remove('open'); }
function addToPlaylist(i, song) { const s = song || modalTrack; if (!s) return; const pl = playlists[i]; if (pl.songs.some(x => String(x.trackId) === String(s.trackId))) { showToast('Already in "' + pl.name + '"'); } else { pl.songs.push(s); localStorage.setItem('aurum_playlists', JSON.stringify(playlists)); showToast('Added to "' + pl.name + '"'); } document.getElementById('add-playlist-modal').classList.remove('open'); modalTrack = null; }

function openQualitySheet() {
  if (!currentTrack) { showToast('Play a song first'); return; }
  const sub = document.getElementById('qs-track-name');
  if (sub) sub.textContent = `${currentTrack.trackName || 'Unknown'} · ${currentTrack.artistName || 'Unknown'}`;
  updateQualityLabel();
  const modal = document.getElementById('quality-modal');
  modal.style.display = '';
  modal.classList.add('open');
}

function closeQualitySheet(e) {
  if (e && e.target.closest?.('.quality-sheet')) return;
  const modal = document.getElementById('quality-modal');
  modal.classList.remove('open');
  modal.style.display = 'none';
}
function selectQuality(q) { if (q === 'preview') { _fallbackToPreview(currentTrack); closeQualitySheet(); } else { if (_fullSongAbort) { _fullSongAbort.abort(); _fullSongAbort = null; } _autoFetchFullSong(currentTrack); closeQualitySheet(); } }

// ─── DOWNLOAD MODAL ───────────────────────────────────────────────────────────
function openDownloadModal() {
  const song = _downloadSong || currentTrack;
  if (!song) { showToast('Play a song first'); return; }
  _downloadSong = song;

  const sub = document.getElementById('dl-track-name');
  if (sub) sub.textContent = `${song.trackName || 'Unknown'} · ${song.artistName || 'Unknown'}`;

  if (_currentSaavnQuality) {
    _updateDlSheetQuality(_currentSaavnQuality);
  } else {
    const desc  = document.getElementById('dl-full-desc');
    const badge = document.getElementById('dl-full-badge');
    if (desc)  desc.textContent  = currentQuality === 'loading' ? 'Fetching stream…' : 'Play song first';
    if (badge) { badge.textContent = '—'; badge.className = 'dl-kbps-badge b128'; }
  }

  const modal = document.getElementById('download-modal');
  modal.style.display = '';
  modal.classList.add('open');
}

function closeDownloadModal(e) {
  if (e && e.target.closest?.('.dl-sheet')) return;
  const modal = document.getElementById('download-modal');
  modal.classList.remove('open');
  modal.style.display = 'none';
  _downloadSong = null;
}

async function triggerDownload(quality) {
  // ── FIX #2 — validateFeature gate (auth.js) ──────────────────────────
  if (quality === 'ringtone' && !window.validateFeature('ringtone')) return;
  if ((quality === 'full' || quality === 'gift') && !window.validateFeature('download')) return;
  // ─────────────────────────────────────────────────────────────────────

  const song = _downloadSong || currentTrack;
  _downloadSong = null;
  if (!song) { showToast('No track selected'); return; }

  const modal = document.getElementById('download-modal');
  modal.classList.remove('open');
  modal.style.display = 'none';

  const cleanTitle  = (song.trackName  || 'audio').replace(/[/\?%*:|"<>]/g, '-');
  const cleanArtist = (song.artistName || '').replace(/[/\?%*:|"<>]/g, '-');

  if (quality === 'preview') {
    try {
      showToast('Downloading preview…');
      const res = await fetch(song.previewUrl);
      if (!res.ok) throw new Error('fetch failed');
      const blob   = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      const a      = document.createElement('a');
      a.href       = objUrl;
      a.download   = `${cleanTitle}_preview.m4a`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(objUrl), 5000);
      haptic([10, 30, 10]);
      showToast('Preview saved ✓');
    } catch(e) { showToast('Download failed'); }
    return;
  }

  if (quality === 'ringtone') {
    try {
      showToast('Saving ringtone…');
      const res = await fetch(song.previewUrl);
      if (!res.ok) throw new Error('fetch failed');
      const blob   = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      const a      = document.createElement('a');
      a.href       = objUrl;
      a.download   = `${cleanTitle}_ringtone.m4a`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(objUrl), 5000);
      haptic([10, 30, 10]);
      showToast('Ringtone saved ✓');
    } catch(e) { showToast('Download failed'); }
    return;
  }

  if (quality === 'full') {
  const modal = document.getElementById('download-modal');
  modal.classList.remove('open');
  modal.style.display = 'none';

  // App ke andar save (IndexedDB)
  downloadSongOffline(song, _currentSaavnUrl, _currentSaavnQuality);

  // Device pe bhi file download
  try {
    const cleanTitle  = (song.trackName  || 'audio').replace(/[/\?%*:|"<>]/g, '-');
    const cleanArtist = (song.artistName || '').replace(/[/\?%*:|"<>]/g, '-');
    const q      = encodeURIComponent(song.trackName  || '');
    const artist = encodeURIComponent(song.artistName || '');
    const dlUrl  = `/api/download?q=${q}&artist=${artist}&quality=full`;
    const a      = document.createElement('a');
    a.href       = dlUrl;
    a.download   = `${cleanTitle} - ${cleanArtist}.mp3`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    haptic([10, 30, 10]);
    showToast('Saving to app & downloading…');
  } catch(e) {
    showToast('Saved to app ✓');
  }

  _downloadSong = null;
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
      const a      = document.createElement('a');
      a.href       = dlUrl;
      a.download   = `${cleanTitle} - ${cleanArtist}_320.mp3`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      haptic([10, 30, 10]);
      showToast('320 kbps download started ✓');
    } catch(e) { showToast('Owner Gift failed'); }
    return;
  }
}

// ─── fetchLyrics ─────────────────────────────────────────────────────────────
async function fetchLyrics(song) {
  const wrap      = document.getElementById('fp-lyrics-wrap');
  const el        = document.getElementById('fp-lyrics');
  const lyricsBtn = document.getElementById('fp-lyrics-toggle');
  if (!wrap || !el) return;

  wrap.style.display = 'none';
  el.textContent     = '';
  lyricsViewActive   = false;
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
  } catch(e) {
    if (lyricsBtn) lyricsBtn.style.display = 'none';
  }
}

// ─── BACKGROUND AUDIO KEEP-ALIVE ─────────────────────────────────────────────
let _wakeLock = null;
let _bgPingInterval = null;
let _bgAudioCtx = null;

async function _acquireWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try {
    _wakeLock = await navigator.wakeLock.request('screen');
    _wakeLock.addEventListener('release', () => { _wakeLock = null; });
  } catch(e) {}
}

function _releaseWakeLock() {
  if (_wakeLock) { _wakeLock.release().catch(()=>{}); _wakeLock = null; }
}

function _setupBgAudioPing() {
  try {
    _bgAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    _bgPingInterval = setInterval(() => {
      if (!isPlaying) return;
      try {
        const buf = _bgAudioCtx.createBuffer(1, 1, 22050);
        const src = _bgAudioCtx.createBufferSource();
        src.buffer = buf;
        src.connect(_bgAudioCtx.destination);
        src.start(0);
      } catch(e) {}
    }, 5000);
  } catch(e) {}
}

audio.addEventListener('playing', () => { _acquireWakeLock(); });
audio.addEventListener('pause',   () => { if (!isPlaying) _releaseWakeLock(); });

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && isPlaying) _acquireWakeLock();
}, { passive: true });

let _toastTimer = null;
function showToast(msg) {
  const t = document.getElementById('toast'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), 2200);
}

// ─── UTILS ────────────────────────────────────────────────────────────────────
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function formatMs(ms) { const s = Math.floor((ms||0)/1000); return `${Math.floor(s/60)}:${(s%60).toString().padStart(2,'0')}`; }
function formatSec(s) { s = Math.floor(s||0); return `${Math.floor(s/60)}:${(s%60).toString().padStart(2,'0')}`; }
function haptic(pat) { try { if (navigator.vibrate && (typeof appSettings === 'undefined' || appSettings?.hapticFeedback !== false)) navigator.vibrate(pat); } catch(e) {} }

if (!isTV) {
  document.addEventListener('keydown', e => {
    if (e.code === 'Space' && e.target.tagName !== 'INPUT') { e.preventDefault(); togglePlay(); }
  });
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  setVh();
  initViz();
  buildHomeSections('all');
  renderLibrary();
  renderSearchIdle();
  setupMiniGesture();
  setupFullPlayerGesture();
  setupQueuePanelGesture();
  setupArtSwipeGesture();
  setupShakeGesture();
  _setupBgAudioPing();
  requestPersistentStorage();
});
