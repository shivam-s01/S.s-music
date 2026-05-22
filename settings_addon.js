// ─── AURUM SETTINGS v3.2 · PREMIUM GATES EDITION ────────────────────────────
// ✅ FIX: Shake to Skip stays OFF after toggle off — no ghost re-attach
// ✅ FIX: Bass Boost, Virtualizer, Loudness Enhancer, Shake → login gate
// ✅ FIX: _tog() persists setting correctly
// ✅ FIX: blurredArtworkBg applies/removes in light+dark
// ✅ FIX: smartSaver saves state before applying
// ✅ All settings intact — nothing removed
'use strict';

// ─── DEFAULTS ─────────────────────────────────────────────────────────────────
const DEFAULT_SETTINGS = {
  theme:'dark', dataSaver:false, streamQuality:'auto',
  animations:true, dynamicColor:true, showVisualizer:true,
  visualizerStyle:'bars', crossfade:false, crossfadeDuration:3,
  gaplessPlayback:false, playbackSpeed:1.0, volumeNormalize:false,
  eqEnabled:false, eqPreset:'flat', eqBands:[0,0,0,0,0,0,0,0,0,0],
  bassBoost:false, virtualizer:false, loudnessEnhancer:false,
  cornerRadius:'rounded', accentColor:'gold', glassIntensity:50,
  ambientEdgeGlow:true, blurredArtworkBg:true, shakeToSkip:false,
  hapticFeedback:true, headphoneAutoPause:true, audioDucking:false,
  showTabTitle:true, historyLimit:20, saveHistory:true,
  sleepTimerEnd:null, sleepMode:'timer', smartSaver:false,
};

let appSettings = Object.assign({}, DEFAULT_SETTINGS,
  JSON.parse(localStorage.getItem('aurum_settings') || '{}'));

// ─── DEVICE CAPS ──────────────────────────────────────────────────────────────
const _dev = (() => {
  const isLowEnd =
    (navigator.deviceMemory !== undefined && navigator.deviceMemory <= 2) ||
    (navigator.hardwareConcurrency !== undefined && navigator.hardwareConcurrency <= 2) ||
    /Android [1-6]\./i.test(navigator.userAgent);
  return {
    isLowEnd,
    hasVibrate: 'vibrate' in navigator,
    hasBackdrop: CSS.supports('backdrop-filter','blur(1px)') ||
                 CSS.supports('-webkit-backdrop-filter','blur(1px)'),
  };
})();

// ─── HAPTIC ───────────────────────────────────────────────────────────────────
function _haptic(p) {
  if (!appSettings.hapticFeedback || !_dev.hasVibrate) return;
  try { navigator.vibrate(p); } catch(e) {}
}

// ─── SAVE ─────────────────────────────────────────────────────────────────────
function saveSetting(key, value) {
  appSettings[key] = value;
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  applySettings();
}

// ─── PREMIUM FEATURE GATE ─────────────────────────────────────────────────────
// Returns true = BLOCKED (not logged in), false = ALLOWED
const _PREMIUM_KEYS = ['bassBoost', 'virtualizer', 'loudnessEnhancer', 'shakeToSkip'];

function _premiumGate(key) {
  if (window.userAuth && window.userAuth.isLoggedIn) return false;
  // Force setting back to false
  appSettings[key] = false;
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  // Flip the checkbox back off in DOM
  const checkboxes = document.querySelectorAll('input[type="checkbox"]');
  checkboxes.forEach(cb => {
    const attr = cb.getAttribute('onchange') || '';
    if (attr.includes(key)) cb.checked = false;
  });
  // Show login modal
  if (typeof checkFeatureAccess === 'function') {
    const featureNames = {
      bassBoost: 'audio',
      virtualizer: 'audio',
      loudnessEnhancer: 'audio',
      shakeToSkip: 'default',
    };
    checkFeatureAccess(featureNames[key] || 'default');
  } else if (typeof openLoginModal === 'function') {
    openLoginModal();
  }
  return true; // blocked
}

// ─── DYNAMIC STYLE TAG ────────────────────────────────────────────────────────
let _styleEl = null;
function _injectStyles() {
  if (!_styleEl) {
    _styleEl    = document.createElement('style');
    _styleEl.id = 'aurum-dyn';
    document.head.appendChild(_styleEl);
  }

  const isLight = appSettings.theme === 'light' ||
    (appSettings.theme !== 'dark' && appSettings.theme !== 'amoled' &&
     window.matchMedia('(prefers-color-scheme: light)').matches);
  const doBlur = appSettings.blurredArtworkBg && !appSettings.smartSaver;

  let css = '';

  if (isLight) {
    css += `#fp-bg-art{display:none!important}`;
    css += `#fp-bg-overlay{background:var(--bg)!important}`;
    css += `#fp-ambient-glow{display:none!important}`;
  } else if (doBlur) {
    css += `#fp-bg-art{filter:blur(32px) saturate(1.9) brightness(0.48)!important;transform:scale(1.18)!important;opacity:1!important}`;
    css += `#fp-bg-overlay{background:linear-gradient(to bottom,rgba(0,0,0,.18) 0%,rgba(0,0,0,.42) 45%,rgba(0,0,0,.82) 80%,rgba(0,0,0,.96) 100%)!important}`;
  } else {
    css += `#fp-bg-art{filter:blur(0px) brightness(0.3)!important;transform:scale(1.05)!important}`;
  }

  if (appSettings.smartSaver) {
    css += `*,*::before,*::after{animation-duration:.001ms!important;transition-duration:.05ms!important}`;
    css += `[class*="glass"],.modal-sheet{backdrop-filter:none!important;-webkit-backdrop-filter:none!important}`;
    css += `#fp-visualizer,#ambient-edge-glow,.orb,.ph-orb{display:none!important}`;
  }

  if (_styleEl.textContent !== css) _styleEl.textContent = css;
}

// ─── APPLY SETTINGS ───────────────────────────────────────────────────────────
let _lastTheme = null, _lastAccent = null, _lastRadius = null;

function applySettings() {
  const root = document.documentElement;
  const s    = appSettings;

  // Theme
  if (s.theme !== _lastTheme) {
    root.dataset.theme = s.theme === 'light' ? 'light' : (s.theme === 'amoled' ? 'dark' : 'dark');
    const themeVars = {
      amoled:{'--bg':'#000','--surface':'#000','--surface2':'#0a0a0a','--surface3':'#111'},
      light: {'--bg':'#f5f2ed','--surface':'#fff','--surface2':'#f0ece4','--surface3':'#e8e2d8',
               '--text':'#1a1814','--text2':'#4a4540','--text3':'#8a8278'},
    };
    const vars = themeVars[s.theme];
    if (vars) Object.entries(vars).forEach(([k,v]) => root.style.setProperty(k,v));
    else ['--bg','--surface','--surface2','--surface3','--text','--text2','--text3']
      .forEach(p => root.style.removeProperty(p));
    _lastTheme = s.theme;
  }

  // Accent
  if (s.accentColor !== _lastAccent) {
    const ac = {
      gold:{main:'#b89640',light:'#d4af55'}, rose:{main:'#c05f7a',light:'#e07090'},
      sky:{main:'#4a9cc8',light:'#6ab8e0'},  sage:{main:'#5a9e72',light:'#7abf90'},
      violet:{main:'#8b5fcf',light:'#a878e8'}, ember:{main:'#c4622d',light:'#e07840'},
    }[s.accentColor] || {main:'#b89640',light:'#d4af55'};
    root.style.setProperty('--gold',   ac.main);
    root.style.setProperty('--gold-l', ac.light);
    _lastAccent = s.accentColor;
  }

  // Corner radius
  if (s.cornerRadius !== _lastRadius) {
    root.style.setProperty('--radius', {rounded:'12px',pill:'999px',sharp:'4px'}[s.cornerRadius] || '12px');
    _lastRadius = s.cornerRadius;
  }

  // Animations
  if (!s.animations || s.smartSaver) {
    root.style.setProperty('--anim-speed', '0s');
  } else {
    root.style.removeProperty('--anim-speed');
  }

  // Visualizer
  const viz = document.getElementById('fp-visualizer');
  if (viz) viz.style.display = (s.showVisualizer && !s.smartSaver) ? '' : 'none';

  // Dynamic glow
  const glow = document.getElementById('fp-ambient-glow');
  if (glow) glow.style.display = s.dynamicColor ? '' : 'none';

  // Edge glow
  _updateEdgeGlow();

  // Glass
  root.style.setProperty('--glass-blur',  (s.glassIntensity / 5) + 'px');
  root.style.setProperty('--glass-alpha', (s.glassIntensity / 400).toFixed(3));

  // Playback speed
  const aud = window._aurumAudio || document.querySelector('audio');
  if (aud && aud.playbackRate !== s.playbackSpeed) aud.playbackRate = s.playbackSpeed;

  // Shake — only attach if logged in
  if (s.shakeToSkip && window.userAuth && window.userAuth.isLoggedIn) {
    _attachShake();
  } else {
    _detachShake();
    // If somehow setting got saved as true but user not logged in, reset it
    if (s.shakeToSkip && !(window.userAuth && window.userAuth.isLoggedIn)) {
      appSettings.shakeToSkip = false;
      localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
    }
  }

  // Tab title
  if (!s.showTabTitle) document.title = 'Aurum';

  // Normalization
  if (_gainNode) _applyNorm();

  // Crossfade
  _setupCrossfade();

  // CSS
  _injectStyles();
}

// ─── EDGE GLOW ────────────────────────────────────────────────────────────────
let _edgeEl = null, _edgeRaf = null;

function _updateEdgeGlow() {
  if (!appSettings.ambientEdgeGlow || appSettings.smartSaver) {
    if (_edgeEl) _edgeEl.style.display = 'none';
    return;
  }
  if (!_edgeEl) {
    _edgeEl    = document.createElement('div');
    _edgeEl.id = 'ambient-edge-glow';
    const st   = document.createElement('style');
    st.textContent = `
      #ambient-edge-glow{position:fixed;inset:0;pointer-events:none;z-index:9998;
        --er:184;--eg:150;--eb:64;
        background:
          radial-gradient(ellipse 60% 20% at 50% 0%,  rgba(var(--er),var(--eg),var(--eb),.18) 0%,transparent 100%),
          radial-gradient(ellipse 60% 20% at 50% 100%,rgba(var(--er),var(--eg),var(--eb),.18) 0%,transparent 100%),
          radial-gradient(ellipse 20% 60% at 0%   50%,rgba(var(--er),var(--eg),var(--eb),.14) 0%,transparent 100%),
          radial-gradient(ellipse 20% 60% at 100% 50%,rgba(var(--er),var(--eg),var(--eb),.14) 0%,transparent 100%);
        animation:_eg-pulse 3s ease-in-out infinite;}
      @keyframes _eg-pulse{0%,100%{opacity:.7}50%{opacity:1}}`;
    document.head.appendChild(st);
    document.body.appendChild(_edgeEl);
  }
  _edgeEl.style.display = '';
}

window._setEdgeGlowColor = function(r, g, b) {
  if (_edgeRaf) return;
  _edgeRaf = requestAnimationFrame(() => {
    _edgeRaf = null;
    if (_edgeEl && appSettings.ambientEdgeGlow && !appSettings.smartSaver) {
      _edgeEl.style.setProperty('--er', r);
      _edgeEl.style.setProperty('--eg', g);
      _edgeEl.style.setProperty('--eb', b);
    }
  });
};

// ─── SINGLE AUDIO CONTEXT ─────────────────────────────────────────────────────
function _getCtx() {
  if (!window._aurumAudioCtx) {
    try {
      window._aurumAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch(e) { return null; }
  }
  return window._aurumAudioCtx;
}

function _getSrc() {
  const ctx = _getCtx(); if (!ctx) return null;
  if (!window._aurumSrcNode) {
    const aud = window._aurumAudio || document.querySelector('audio');
    if (!aud) return null;
    try { window._aurumSrcNode = ctx.createMediaElementSource(aud); }
    catch(e) { return null; }
  }
  return window._aurumSrcNode;
}

// ─── VOLUME NORMALIZATION ─────────────────────────────────────────────────────
let _gainNode = null;

function _applyNorm() {
  const ctx = _getCtx(); if (!ctx) return;
  if (!_gainNode) {
    const src = _getSrc(); if (!src) return;
    _gainNode = ctx.createGain();
    _gainNode.gain.value = 1.0;
    src.connect(_gainNode);
    _gainNode.connect(ctx.destination);
  }
  const target = appSettings.volumeNormalize ? 0.75 : 1.0;
  _gainNode.gain.linearRampToValueAtTime(target, ctx.currentTime + 0.4);
}

// ─── CROSSFADE ────────────────────────────────────────────────────────────────
let _cfTimer = null, _cfAudio = null;

function _setupCrossfade() {
  if (!appSettings.crossfade) {
    clearInterval(_cfTimer); _cfTimer = null;
    if (_cfAudio) { _cfAudio.pause(); _cfAudio.src = ''; _cfAudio.remove(); _cfAudio = null; }
  }
}

window.aurumCrossfadeTo = function(nextSrc, onComplete) {
  const primary = window._aurumAudio || document.querySelector('audio');
  if (!primary) return;
  if (!appSettings.crossfade || !nextSrc) {
    primary.src = nextSrc;
    primary.play().catch(()=>{});
    if (typeof onComplete === 'function') onComplete();
    return;
  }
  const dur = (appSettings.crossfadeDuration || 3) * 1000;
  const steps = 30, stepMs = dur / steps;
  if (!_cfAudio) {
    _cfAudio = document.createElement('audio');
    _cfAudio.style.display = 'none';
    document.body.appendChild(_cfAudio);
  }
  _cfAudio.src = nextSrc; _cfAudio.volume = 0;
  _cfAudio.play().catch(()=>{});
  const startVol = primary.volume || 1;
  let step = 0;
  clearInterval(_cfTimer);
  _cfTimer = setInterval(() => {
    step++;
    const t = step / steps;
    const eIn = t*t*(3-2*t), eOut = 1 - eIn;
    primary.volume  = Math.max(0, startVol * eOut);
    _cfAudio.volume = Math.min(1, eIn);
    if (step >= steps) {
      clearInterval(_cfTimer); _cfTimer = null;
      primary.src = nextSrc; primary.volume = 1;
      primary.currentTime = _cfAudio.currentTime;
      primary.play().catch(()=>{});
      _cfAudio.pause(); _cfAudio.src = '';
      if (typeof onComplete === 'function') onComplete();
    }
  }, stepMs);
};

// ─── SMART SAVER ──────────────────────────────────────────────────────────────
function applySmartSaver(enable) {
  Object.assign(appSettings, enable ? {
    smartSaver:true, animations:false, blurredArtworkBg:false,
    ambientEdgeGlow:false, showVisualizer:false, glassIntensity:10,
  } : (_dev.isLowEnd ? {
    smartSaver:false, animations:false, blurredArtworkBg:false,
    ambientEdgeGlow:false, showVisualizer:true, glassIntensity:20,
  } : {
    smartSaver:false, animations:true, blurredArtworkBg:true,
    ambientEdgeGlow:true, showVisualizer:true, glassIntensity:50,
  }));
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  applySettings();
  if (typeof showToast === 'function')
    showToast(enable ? '⚡ Smart Saver on' : 'Smart Saver off');
  renderSettingsPage();
}

// ─── EXPORT / IMPORT ──────────────────────────────────────────────────────────
function exportConfig() {
  const data = Object.assign({}, appSettings); delete data.sleepTimerEnd;
  const json = JSON.stringify(data, null, 2);
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(json)
      .then(() => { if (typeof showToast==='function') showToast('✓ Config copied'); _haptic([10,30,10]); })
      .catch(() => _exportModal(json));
  } else _exportModal(json);
}

function _exportModal(json) {
  _removeEl('aurum-export-modal');
  const m = _mkModal('aurum-export-modal', `
    <div class="modal-handle"></div><div class="picker-title">Export Config</div>
    <textarea id="aurum-export-txt" readonly style="flex:1;background:var(--surface2);color:var(--text);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px;font-family:monospace;font-size:11px;resize:none;min-height:200px;outline:none">${json}</textarea>
    <button onclick="document.getElementById('aurum-export-txt').select();document.execCommand('copy');if(typeof showToast==='function')showToast('Copied!');_removeEl('aurum-export-modal')"
      style="background:var(--gold);color:#000;border:none;border-radius:var(--radius,12px);padding:13px;font-weight:700;font-size:14px;cursor:pointer;width:100%">Copy</button>`);
  document.body.appendChild(m);
}

function importConfig() {
  _removeEl('aurum-import-modal');
  const m = _mkModal('aurum-import-modal', `
    <div class="modal-handle"></div><div class="picker-title">Import Config</div>
    <p style="color:var(--text2);font-size:13px;margin:0">Paste Aurum config JSON below.</p>
    <textarea id="aurum-import-txt" placeholder='{"theme":"dark",...}'
      style="flex:1;background:var(--surface2);color:var(--text);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px;font-family:monospace;font-size:11px;resize:none;min-height:160px;outline:none"></textarea>
    <button onclick="window._doImport()"
      style="background:var(--gold);color:#000;border:none;border-radius:var(--radius,12px);padding:13px;font-weight:700;font-size:14px;cursor:pointer;width:100%">Apply</button>`);
  document.body.appendChild(m);
}

window._doImport = function() {
  const txt = document.getElementById('aurum-import-txt')?.value?.trim();
  if (!txt) return;
  let parsed; try { parsed = JSON.parse(txt); } catch(e) { if(typeof showToast==='function') showToast('⚠ Invalid JSON'); return; }
  let n = 0;
  Object.keys(DEFAULT_SETTINGS).forEach(k => { if (parsed[k] !== undefined) { appSettings[k] = parsed[k]; n++; } });
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  applySettings(); _removeEl('aurum-import-modal'); renderSettingsPage();
  if (typeof showToast==='function') showToast(`✓ ${n} settings applied`);
  _haptic([15,40,15]);
};

function smartReset() {
  _confirm('Reset all settings to defaults?', () => {
    appSettings = Object.assign({}, DEFAULT_SETTINGS);
    if (_dev.isLowEnd) Object.assign(appSettings, {animations:false,blurredArtworkBg:false,ambientEdgeGlow:false,glassIntensity:15,smartSaver:true});
    _lastTheme = _lastAccent = _lastRadius = null;
    localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
    applySettings(); renderSettingsPage();
    if (typeof showToast==='function') showToast(_dev.isLowEnd ? 'Reset · Optimized defaults' : 'Reset · Full quality defaults');
  });
}

// ─── CUSTOM CONFIRM ───────────────────────────────────────────────────────────
function _confirm(msg, onYes) {
  _removeEl('aurum-confirm-modal');
  const m = document.createElement('div');
  m.id = 'aurum-confirm-modal';
  m.className = 'modal-overlay open';
  m.style.cssText = 'display:flex;align-items:center;justify-content:center;padding:24px';
  m.innerHTML = `<div style="background:var(--sheet-bg);border-radius:20px;padding:24px;width:100%;max-width:320px;display:flex;flex-direction:column;gap:16px">
    <p style="font-size:15px;font-weight:600;color:var(--text);margin:0;text-align:center">${msg}</p>
    <div style="display:flex;gap:8px">
      <button onclick="_removeEl('aurum-confirm-modal')"
        style="flex:1;padding:12px;border:none;border-radius:12px;background:var(--surface3);color:var(--text2);font-family:Sora,sans-serif;font-weight:700;cursor:pointer">Cancel</button>
      <button id="_confirm-yes"
        style="flex:1;padding:12px;border:none;border-radius:12px;background:#d84444;color:#fff;font-family:Sora,sans-serif;font-weight:700;cursor:pointer">Confirm</button>
    </div>
  </div>`;
  m.querySelector('#_confirm-yes').onclick = () => { _removeEl('aurum-confirm-modal'); onYes(); };
  document.body.appendChild(m);
}

function _removeEl(id) { document.getElementById(id)?.remove(); }
function _mkModal(id, innerHtml) {
  const m = document.createElement('div');
  m.id = id; m.className = 'modal-overlay open';
  m.innerHTML = `<div class="modal-sheet picker-sheet" style="display:flex;flex-direction:column;gap:12px;max-height:80vh">${innerHtml}</div>`;
  m.onclick = e => { if (e.target === m) m.remove(); };
  return m;
}

// ─── FETCH PATCH ──────────────────────────────────────────────────────────────
const _origFetch = window.fetch;
window.fetch = function(url, opts) {
  if (typeof url === 'string' && url.includes('/api/saavn?') &&
      (appSettings.dataSaver || appSettings.streamQuality === 'low'))
    url += '&low_quality=true';
  return _origFetch.call(this, url, opts);
};

// ─── APP.JS PATCHES ───────────────────────────────────────────────────────────
function _patchApp() {
  if (typeof extractDominantColor === 'function') {
    const _o = extractDominantColor;
    window.extractDominantColor = function(img, cb) {
      _o(img, (r,g,b) => { cb(r,g,b); window._setEdgeGlowColor(r,g,b); });
    };
  }

  if (typeof updatePlayerUI === 'function') {
    const _o = updatePlayerUI;
    window.updatePlayerUI = function() {
      _o.apply(this, arguments);
      if (appSettings.showTabTitle && typeof currentTrack !== 'undefined' && currentTrack)
        document.title = `${currentTrack.trackName || '♪'} · ${currentTrack.artistName || ''} — Aurum`;
      else if (!appSettings.showTabTitle) document.title = 'Aurum';
    };
  }

  if (typeof addToRecentlyPlayed === 'function') {
    const _o = addToRecentlyPlayed;
    window.addToRecentlyPlayed = function(song) {
      if (!appSettings.saveHistory) return;
      _o.apply(this, arguments);
      const lim = appSettings.historyLimit === 0 ? 500 : (appSettings.historyLimit || 20);
      if (typeof recentlyPlayed !== 'undefined' && recentlyPlayed.length > lim) {
        recentlyPlayed = recentlyPlayed.slice(0, lim);
        localStorage.setItem('aurum_recent_played', JSON.stringify(recentlyPlayed));
      }
    };
  }

  const aud = window._aurumAudio || document.querySelector('audio');
  if (aud) {
    aud.addEventListener('ended', () => window._checkSleepOnTrackEnd?.(), { passive:true });

    if ('mediaDevices' in navigator) {
      navigator.mediaDevices.addEventListener('devicechange', async () => {
        if (!appSettings.headphoneAutoPause) return;
        try {
          const devs = await navigator.mediaDevices.enumerateDevices();
          if (devs.filter(d => d.kind === 'audiooutput').length <= 1
              && typeof isPlaying !== 'undefined' && isPlaying) {
            if (typeof togglePlay === 'function') togglePlay();
            if (typeof showToast  === 'function') showToast('Headphones disconnected · Paused');
          }
        } catch(e) {}
      });
    }

    document.addEventListener('visibilitychange', () => {
      if (!appSettings.audioDucking) return;
      if (document.hidden) { aud._preDuck = aud.volume; aud.volume = Math.max(0, aud.volume * 0.3); }
      else if (aud._preDuck !== undefined) { aud.volume = aud._preDuck; delete aud._preDuck; }
    }, { passive:true });
  }
}

// ─── OPEN / CLOSE ─────────────────────────────────────────────────────────────
function openSettings() {
  renderSettingsPage();
  document.getElementById('settings-panel')?.classList.add('open');
  _haptic(10);
}
function closeSettings() {
  document.getElementById('settings-panel')?.classList.remove('open');
}

// ─── RENDER SETTINGS ──────────────────────────────────────────────────────────
let _renderRaf = null;
function renderSettingsPage() {
  if (_renderRaf) return;
  _renderRaf = requestAnimationFrame(() => { _renderRaf = null; _doRender(); });
}

function _doRender() {
  const body = document.getElementById('settings-body');
  if (!body) return;
  const s = appSettings;
  const loggedIn = !!(window.userAuth && window.userAuth.isLoggedIn);

  const qL  = {auto:'Auto (Best)',high:'High (320kbps)',low:'Low (128kbps)'}[s.streamQuality] || 'Auto';
  const spL  = s.playbackSpeed === 1 ? 'Normal (1×)' : s.playbackSpeed + '×';
  const thL  = {dark:'Dark',amoled:'AMOLED Black',light:'Light'}[s.theme] || 'Dark';
  const rrL  = {rounded:'Rounded',pill:'Pill',sharp:'Sharp'}[s.cornerRadius] || 'Rounded';
  const vzL  = {bars:'Bars',wave:'Waveform',circular:'Circular'}[s.visualizerStyle] || 'Bars';
  const acL  = {gold:'Gold',rose:'Rose',sky:'Sky Blue',sage:'Sage Green',violet:'Violet',ember:'Ember'}[s.accentColor] || 'Gold';
  const eqL  = {flat:'Flat',bass:'Bass Boost',vocal:'Vocal Clarity',pop:'Pop',rock:'Rock',classical:'Classical',custom:'Custom'}[s.eqPreset] || 'Flat';
  const slL  = _sleepLabel();
  const htL  = s.historyLimit === 0 ? 'Unlimited' : s.historyLimit + ' songs';

  const acHex = {gold:'#b89640',rose:'#c05f7a',sky:'#4a9cc8',sage:'#5a9e72',violet:'#8b5fcf',ember:'#c4622d'}[s.accentColor] || '#b89640';
  const rrPx  = {rounded:'12px',pill:'24px',sharp:'4px'}[s.cornerRadius] || '12px';
  const gBlur = (s.glassIntensity/5).toFixed(1);
  const gAlph = (s.glassIntensity/400).toFixed(3);

  const chev = () => `<svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>`;

  // Lock icon for premium features
  const lockIcon = `<svg viewBox="0 0 24 24" fill="none" stroke="rgba(184,150,64,0.7)" stroke-width="1.8" stroke-linecap="round" style="width:13px;height:13px;flex-shrink:0"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`;

  // Premium badge for locked items
  const premBadge = `<span style="font-size:9px;font-weight:700;color:var(--gold);background:rgba(184,150,64,0.12);border:1px solid rgba(184,150,64,0.25);padding:2px 7px;border-radius:100px;letter-spacing:0.05em;white-space:nowrap">PRO</span>`;

  // Toggle — premium version shows lock + PRO badge instead of toggle when not logged in
  const tog = (key, chk, extra) => {
    const isPremKey = _PREMIUM_KEYS.includes(key);
    if (isPremKey && !loggedIn) {
      // Show PRO badge + lock, clicking will open login
      return `<div style="display:flex;align-items:center;gap:6px;cursor:pointer" onclick="_premiumGate('${key}')">${premBadge}</div>`;
    }
    return `<label class="settings-toggle"><input type="checkbox"${chk?' checked':''} onchange="_tog('${key}',this.checked,${extra})"><span class="settings-toggle-track"></span></label>`;
  };

  function sec(id, iconSvg, title, content) {
    return `
    <div class="settings-section-header" onclick="toggleSection('sec-${id}')" id="hdr-${id}">
      <div class="ssh-left"><div class="ssh-icon">${iconSvg}</div><span>${title}</span></div>
      <svg class="ssh-chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
    </div>
    <div class="settings-section-body open" id="sec-${id}">${content}</div>`;
  }

  function row(icon, title, sub, right, active) {
    return `<div class="settings-item">
      <div class="settings-item-left">
        <div class="settings-item-icon${active?' icon-active':''}">${icon}</div>
        <div class="settings-item-info"><div class="settings-item-title">${title}</div><div class="settings-item-sub">${sub}</div>
      </div></div>${right}</div>`;
  }

  function link(icon, title, sub, fn, active) {
    return `<div class="settings-item" onclick="${fn}">
      <div class="settings-item-left">
        <div class="settings-item-icon${active?' icon-active':''}">${icon}</div>
        <div class="settings-item-info"><div class="settings-item-title">${title}</div><div class="settings-item-sub">${sub}</div>
      </div></div>${chev()}</div>`;
  }

  function danger(icon, title, sub, fn) {
    return `<div class="settings-item danger-item" onclick="${fn}">
      <div class="settings-item-left">
        <div class="settings-item-icon danger-icon">${icon}</div>
        <div class="settings-item-info"><div class="settings-item-title danger-text">${title}</div><div class="settings-item-sub">${sub}</div>
      </div></div>${chev()}</div>`;
  }

  // Premium row — shows lock overlay if not logged in
  function premRow(icon, title, sub, key, chk, active) {
    if (!loggedIn) {
      return `<div class="settings-item" onclick="_premiumGate('${key}')" style="cursor:pointer">
        <div class="settings-item-left">
          <div class="settings-item-icon" style="background:rgba(184,150,64,0.08)">${icon}</div>
          <div class="settings-item-info">
            <div class="settings-item-title" style="display:flex;align-items:center;gap:7px">${title} ${premBadge}</div>
            <div class="settings-item-sub">${sub}</div>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:6px">${lockIcon}</div>
      </div>`;
    }
    return row(icon, title, sub, tog(key, chk, `()=>toggleAudioFX('${key}',this.checked)`), active);
  }

  const I = {
    eq:   `<svg viewBox="0 0 24 24"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>`,
    bolt: `<svg viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>`,
    gear: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>`,
    sun:  `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>`,
    moon: `<svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`,
    vol:  `<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>`,
    note: `<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`,
    spd:  `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`,
    shk:  `<svg viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 0 20"/><path d="M12 6v6l3 3"/><path d="M18 14l2 2-2 2"/><path d="M22 16h-4"/></svg>`,
    hph:  `<svg viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>`,
    tap:  `<svg viewBox="0 0 24 24"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12a19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 3.6 1.27h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.9a16 16 0 0 0 6 6l.91-.91a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 21.73 16.92z"/></svg>`,
    duck: `<svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>`,
    rect: `<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5" ry="5"/></svg>`,
    img:  `<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>`,
    dot:  `<svg viewBox="0 0 24 24"><circle cx="13.5" cy="6.5" r="2.5"/><circle cx="19" cy="11" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/><circle cx="8.5" cy="7.5" r="2.5"/><circle cx="6.5" cy="12" r="2.5"/></svg>`,
    edge: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/></svg>`,
    wave: `<svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>`,
    play: `<svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>`,
    cf:   `<svg viewBox="0 0 24 24"><path d="M16 3h5v5"/><path d="M4 20L21 3"/><path d="M21 16v5h-5"/><path d="M15 15l5.1 5.1"/><path d="M4 4l5 5"/></svg>`,
    up:   `<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>`,
    dn:   `<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>`,
    hist: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>`,
    shld: `<svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>`,
    tab:  `<svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>`,
    box:  `<svg viewBox="0 0 24 24"><path d="M22 12H2"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>`,
    del:  `<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>`,
    rst:  `<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>`,
    info: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>`,
    srch: `<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>`,
    x:    `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
    user: `<svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>`,
    star: `<svg viewBox="0 0 24 24"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>`,
  };

  const parts = [];

  // ── ACCOUNT CARD (top of settings, Spotify-style) ─────────────────────────
  if (loggedIn) {
    const u = window.userAuth.user;
    parts.push(`
    <div class="settings-account-card" onclick="openUserMenu()">
      <img class="settings-account-avatar" src="${u.picture || ''}" alt="avatar"
           onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
      <div class="settings-account-avatar-placeholder" style="display:none">
        ${I.user}
      </div>
      <div class="settings-account-info">
        <div class="settings-account-name">${u.name || 'User'}</div>
        <div class="settings-account-sub">${u.email || ''}</div>
      </div>
      <div class="settings-account-badge">✦ Pro</div>
      ${chev()}
    </div>`);
  } else {
    parts.push(`
    <div class="settings-account-card" onclick="openLoginModal()">
      <div class="settings-account-avatar-placeholder">
        ${I.user}
      </div>
      <div class="settings-account-info">
        <div class="settings-account-name">Sign in to Aurum</div>
        <div class="settings-account-sub">Unlock Bass Boost, AI picks & more</div>
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:11px;font-weight:700;color:var(--gold);background:linear-gradient(135deg,rgba(184,150,64,0.18),rgba(184,150,64,0.06));border:1px solid rgba(184,150,64,0.3);padding:5px 12px;border-radius:100px;white-space:nowrap">Sign In</span>
      </div>
    </div>`);
  }

  // Live preview card
  parts.push(`
  <div class="settings-live-preview" id="live-preview-card"
    style="border-radius:${rrPx};border-color:${acHex}40;
           backdrop-filter:blur(${gBlur}px);-webkit-backdrop-filter:blur(${gBlur}px);
           background:rgba(255,255,255,${gAlph})">
    <div class="slp-artwork" style="border-radius:calc(${rrPx} - 2px)">
      <div class="slp-artwork-placeholder" style="background:linear-gradient(135deg,${acHex}55,${acHex}22)">
        <svg viewBox="0 0 24 24" fill="none" stroke="${acHex}" stroke-width="1.5" width="28" height="28">
          <path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
        </svg>
      </div>
    </div>
    <div class="slp-info">
      <div class="slp-title">Aurum · Live Preview</div>
      <div class="slp-sub" style="color:${acHex}">${acL} · ${thL}</div>
      <div class="slp-meta">Glass ${s.glassIntensity}% · ${rrL}</div>
    </div>
    <div class="slp-badge" style="background:${acHex}22;color:${acHex};border-radius:calc(${rrPx}/2)">Live</div>
  </div>`);

  // Audio Engine
  parts.push(sec('audio', I.eq, 'Audio Engine', `
    ${link(I.wave,'Stream Quality',qL,'openStreamQualityPicker()')}
    ${row(I.bolt,'Data Saver',s.dataSaver?'128kbps · Low data':'Off',tog('dataSaver',s.dataSaver,`()=>toggleDataSaver(this.checked)`),s.dataSaver)}
    ${link(I.eq,'Equalizer',(s.eqEnabled?eqL:'Off')+' · 10-Band','openEQSheet()',s.eqEnabled)}
    ${premRow(I.note,'Bass Boost','Enhance low frequencies','bassBoost',s.bassBoost,s.bassBoost)}
    ${premRow(I.hph,'Virtualizer','Spatial / 3D surround','virtualizer',s.virtualizer,s.virtualizer)}
    ${premRow(I.vol,'Loudness Enhancer','Boost perceived loudness','loudnessEnhancer',s.loudnessEnhancer,s.loudnessEnhancer)}
    ${row(I.wave,'Volume Normalization','Smooth gain leveling',tog('volumeNormalize',s.volumeNormalize,`()=>{saveSetting('volumeNormalize',this.checked);if(typeof showToast==='function')showToast(this.checked?'Normalization on':'Normalization off')}`),s.volumeNormalize)}
    <div class="settings-item settings-item-expandable"><div class="settings-item-full">
      <div class="settings-item-row-top">
        <div class="settings-item-left">
          <div class="settings-item-icon${s.crossfade?' icon-active':''}">${I.cf}</div>
          <div class="settings-item-info"><div class="settings-item-title">Crossfade</div><div class="settings-item-sub">${s.crossfade?s.crossfadeDuration+'s overlap':'Off'}</div></div>
        </div>
        <label class="settings-toggle" style="margin-right:8px"><input type="checkbox"${s.crossfade?' checked':''} onchange="_tog('crossfade',this.checked,()=>toggleCrossfade(this.checked))"><span class="settings-toggle-track"></span></label>
        <button class="expand-toggle" onclick="toggleExpand('cf-exp',this)"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></button>
      </div>
      <div class="settings-sub-expand${s.crossfade?' open':''}" id="cf-exp">
        <div class="expand-label-row"><span>Fade Duration</span><span class="expand-value" id="cf-val">${s.crossfadeDuration}s</span></div>
        <input type="range" class="settings-slider" min="1" max="8" step="1" value="${s.crossfadeDuration}"
          oninput="document.getElementById('cf-val').textContent=this.value+'s';saveSetting('crossfadeDuration',+this.value);_haptic([5])">
      </div>
    </div></div>
    ${row(I.play,'Gapless Playback','Zero silence between tracks',tog('gaplessPlayback',s.gaplessPlayback,`()=>saveSetting('gaplessPlayback',this.checked)`),s.gaplessPlayback)}
    ${link(I.spd,'Playback Speed',spL,'openPlaybackSpeedPicker()')}
    ${link(I.moon,'Sleep Timer',slL,'openSleepTimerSheet()',(s.sleepTimerEnd||s.sleepMode==='track'))}
  `));

  // Visuals
  parts.push(sec('visuals', I.sun, 'Visuals & Theme', `
    ${link(I.moon,'Theme',thL,'openThemePicker()')}
    ${link(`<span class="accent-dot accent-${s.accentColor}"></span>`,'Accent Color',acL,'openAccentColorPicker()')}
    ${link(I.rect,'Corner Style',rrL,'openCornerRadiusPicker()')}
    <div class="settings-item settings-item-expandable"><div class="settings-item-full">
      <div class="settings-item-row-top">
        <div class="settings-item-left">
          <div class="settings-item-icon">${I.rect}</div>
          <div class="settings-item-info"><div class="settings-item-title">Glass Intensity</div><div class="settings-item-sub">Blur &amp; transparency · <span id="glass-display">${s.glassIntensity}%</span></div></div>
        </div>
        <button class="expand-toggle" onclick="toggleExpand('glass-exp',this)"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></button>
      </div>
      <div class="settings-sub-expand" id="glass-exp">
        <div class="expand-label-row"><span>Blur strength</span><span class="expand-value" id="glass-val">${s.glassIntensity}%</span></div>
        <input type="range" class="settings-slider" min="0" max="100" step="5" value="${s.glassIntensity}"
          oninput="document.getElementById('glass-val').textContent=this.value+'%';document.getElementById('glass-display').textContent=this.value+'%';_liveGlass(+this.value);saveSetting('glassIntensity',+this.value);_haptic([3])">
      </div>
    </div></div>
    ${row(I.img,'Blurred Artwork BG','YT Music style player BG',tog('blurredArtworkBg',s.blurredArtworkBg,`()=>saveSetting('blurredArtworkBg',this.checked)`),s.blurredArtworkBg)}
    ${row(I.dot,'Dynamic Color','UI adapts to artwork',tog('dynamicColor',s.dynamicColor,`()=>saveSetting('dynamicColor',this.checked)`),s.dynamicColor)}
    ${row(I.edge,'Ambient Edge Glow','Screen edges glow with artwork',tog('ambientEdgeGlow',s.ambientEdgeGlow,`()=>saveSetting('ambientEdgeGlow',this.checked)`),s.ambientEdgeGlow)}
    ${link(I.wave,'Visualizer Style',vzL+' · '+(s.showVisualizer?'On':'Off'),'openVisualizerStylePicker()')}
    ${row(I.play,'Animations','Disable to improve performance',tog('animations',s.animations,`()=>saveSetting('animations',this.checked)`),s.animations)}
  `));

  // Performance
  parts.push(sec('perf', I.bolt, 'Performance', `
    <div class="settings-item smart-saver-item${s.smartSaver?' smart-saver-active':''}">
      <div class="settings-item-left">
        <div class="settings-item-icon${s.smartSaver?' icon-active':''}">${I.bolt}</div>
        <div class="settings-item-info">
          <div class="settings-item-title">⚡ Smart Saver</div>
          <div class="settings-item-sub">${s.smartSaver?'Active · GPU effects off':_dev.isLowEnd?'Recommended for your device':'One-tap optimize'}</div>
        </div>
      </div>
      <label class="settings-toggle"><input type="checkbox"${s.smartSaver?' checked':''} onchange="_tog('smartSaver',this.checked,()=>applySmartSaver(this.checked))"><span class="settings-toggle-track"></span></label>
    </div>
    ${loggedIn
      ? row(I.shk,'Shake to Skip','Shake phone → next track',`<label class="settings-toggle"><input type="checkbox"${s.shakeToSkip?' checked':''} onchange="toggleShakeToSkip(this.checked)"><span class="settings-toggle-track"></span></label>`,s.shakeToSkip)
      : `<div class="settings-item" onclick="_premiumGate('shakeToSkip')" style="cursor:pointer">
          <div class="settings-item-left">
            <div class="settings-item-icon" style="background:rgba(184,150,64,0.08)">${I.shk}</div>
            <div class="settings-item-info">
              <div class="settings-item-title" style="display:flex;align-items:center;gap:7px">Shake to Skip ${premBadge}</div>
              <div class="settings-item-sub">Shake phone → next track</div>
            </div>
          </div>
          <div style="display:flex;align-items:center">${lockIcon}</div>
        </div>`
    }
    ${row(I.tap,'Haptic Feedback','Vibration on tap &amp; swipe',tog('hapticFeedback',s.hapticFeedback,`()=>saveSetting('hapticFeedback',this.checked)`),s.hapticFeedback)}
    ${row(I.hph,'Headphone Auto-Pause','Pause on disconnect',tog('headphoneAutoPause',s.headphoneAutoPause,`()=>saveSetting('headphoneAutoPause',this.checked)`),s.headphoneAutoPause)}
    ${row(I.duck,'Audio Ducking','Lower vol on notification',tog('audioDucking',s.audioDucking,`()=>saveSetting('audioDucking',this.checked)`),s.audioDucking)}
  `));

  // System
  parts.push(sec('system', I.gear, 'System', `
    ${row(I.tab,'Browser Tab Title','Show song in tab',tog('showTabTitle',s.showTabTitle,`()=>saveSetting('showTabTitle',this.checked)`),s.showTabTitle)}
    ${link(I.hist,'History Limit',htL,'openHistoryLimitPicker()')}
    ${row(I.shld,'Save History',s.saveHistory?'History tracked':'Incognito mode',tog('saveHistory',s.saveHistory,`()=>{saveSetting('saveHistory',this.checked);renderSettingsPage()}`),s.saveHistory)}
    ${link(I.up,'Export Config','Share your settings as JSON','exportConfig()')}
    ${link(I.dn,'Import Config','Paste JSON to apply','importConfig()')}
    ${danger(I.srch,'Clear Search History','Remove all searches','confirmClearSearch()')}
    <div class="settings-item">
      <div class="settings-item-left">
        <div class="settings-item-icon">${I.box}</div>
        <div class="settings-item-info"><div class="settings-item-title">Downloaded Songs</div><div class="settings-item-sub" id="storage-count-text">Calculating…</div></div>
      </div>
    </div>
    ${danger(I.del,'Clear Downloads','Remove all offline songs','confirmClearCache()')}
    ${danger(I.x,'Clear All Data','Reset playlists, likes, history','confirmClearAllData()')}
    ${danger(I.rst,'Reset All Settings',_dev.isLowEnd?'Apply optimized defaults':'Restore factory defaults','smartReset()')}
    <div class="settings-item">
      <div class="settings-item-left">
        <div class="settings-item-icon">${I.info}</div>
        <div class="settings-item-info"><div class="settings-item-title">Aurum</div><div class="settings-item-sub">Version 3.2 · Made with ♪</div></div>
      </div>
    </div>
    <div class="settings-item settings-item-developer" onclick="window.open('https://www.instagram.com/shivam_shrma.01?igsh=c3gxNjFnb21xYTM1','_blank')">
      <div class="settings-item-left">
        <div class="settings-item-icon insta-icon">
          <svg viewBox="0 0 24 24" fill="none">
            <rect x="2" y="2" width="20" height="20" rx="6" stroke="url(#ig-g)" stroke-width="1.8"/>
            <circle cx="12" cy="12" r="4.5" stroke="url(#ig-g)" stroke-width="1.8"/>
            <circle cx="17.5" cy="6.5" r="1.2" fill="url(#ig-gf)"/>
            <defs>
              <linearGradient id="ig-g" x1="2" y1="22" x2="22" y2="2" gradientUnits="userSpaceOnUse">
                <stop offset="0%" stop-color="#f9a825"/><stop offset="40%" stop-color="#e91e8c"/><stop offset="100%" stop-color="#6a3de8"/>
              </linearGradient>
              <linearGradient id="ig-gf" x1="0" y1="1" x2="1" y2="0" gradientUnits="objectBoundingBox">
                <stop offset="0%" stop-color="#f9a825"/><stop offset="100%" stop-color="#e91e8c"/>
              </linearGradient>
            </defs>
          </svg>
        </div>
        <div class="settings-item-info"><div class="settings-item-title">Developer</div><div class="settings-item-sub">@shivam_shrma.01 · Tap to follow ↗</div></div>
      </div>${chev()}</div>
  `));

  body.innerHTML = parts.join('');
  _calcStorage();
}

// ─── _tog() — FIXED: saves + premium gate ─────────────────────────────────────
function _tog(key, value, callback) {
  _haptic([8, 20, 8]);
  // Block premium keys if not logged in
  if (value && _PREMIUM_KEYS.includes(key)) {
    if (_premiumGate(key)) return;
  }
  saveSetting(key, value);
  if (typeof callback === 'function') callback();
}

// ─── SECTION COLLAPSE ─────────────────────────────────────────────────────────
function toggleSection(id) {
  const body = document.getElementById(id); if (!body) return;
  const open = body.classList.toggle('open');
  const hdr  = document.getElementById(id.replace('sec-','hdr-'));
  const cv   = hdr?.querySelector('.ssh-chevron');
  if (cv) { cv.style.transition = 'transform .3s cubic-bezier(.33,1,.68,1)'; cv.style.transform = open ? '' : 'rotate(-90deg)'; }
  _haptic([6]);
}

// ─── EXPAND ───────────────────────────────────────────────────────────────────
function toggleExpand(id, btn) {
  const el = document.getElementById(id); if (!el) return;
  el.classList.toggle('open');
  const sv = btn?.querySelector('svg');
  if (sv) { sv.style.transition = 'transform .28s cubic-bezier(.33,1,.68,1)'; sv.style.transform = el.classList.contains('open') ? 'rotate(180deg)' : ''; }
  _haptic([5]);
}

// ─── LIVE GLASS ───────────────────────────────────────────────────────────────
function _liveGlass(v) {
  const c = document.getElementById('live-preview-card'); if (!c) return;
  const b = (v/5).toFixed(1);
  c.style.backdropFilter = c.style.webkitBackdropFilter = `blur(${b}px)`;
  c.style.background = `rgba(255,255,255,${(v/400).toFixed(3)})`;
}

// ─── STORAGE INFO ─────────────────────────────────────────────────────────────
async function _calcStorage() {
  const metas = JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]');
  const el    = document.getElementById('storage-count-text'); if (!el) return;
  if (!metas.length) { el.textContent = 'No downloads'; return; }
  el.textContent = `${metas.length} song${metas.length !== 1 ? 's' : ''} saved offline`;
  try {
    if ('storage' in navigator && 'estimate' in navigator.storage) {
      const { usage } = await navigator.storage.estimate();
      if (usage && el) el.textContent += ` · ${(usage/1024/1024).toFixed(1)} MB`;
    }
  } catch(e) {}
}

// ─── SLEEP TIMER ──────────────────────────────────────────────────────────────
let _sleepInterval = null;

function _sleepLabel() {
  if (appSettings.sleepMode === 'track' && appSettings.sleepTimerEnd === -1) return 'End of track';
  if (appSettings.sleepTimerEnd > 0) {
    const rem = Math.max(0, appSettings.sleepTimerEnd - Date.now());
    if (rem > 0) return `Stops in ${Math.ceil(rem/60000)} min`;
  }
  return 'Off';
}

function openSleepTimerSheet() {
  _removeEl('sleep-sheet');
  const opts = [5,10,15,20,30,45,60,90];
  const act  = appSettings.sleepTimerEnd;
  const trkM = appSettings.sleepMode === 'track' && act === -1;
  const m    = document.createElement('div');
  m.id       = 'sleep-sheet'; m.className = 'modal-overlay open';
  m.innerHTML = `<div class="modal-sheet picker-sheet sleep-sheet-inner">
    <div class="modal-handle"></div><div class="picker-title">Sleep Timer</div>
    <div class="sleep-options">
      <div class="picker-option${trkM?' selected':''}" onclick="_setSleepTrack()">
        <div class="picker-option-info"><div class="picker-option-label">End of Current Track</div><div class="picker-option-sub">Stops when this song ends</div></div>
        <div class="picker-radio">${trkM?'<svg viewBox="0 0 24 24" fill="var(--gold)"><circle cx="12" cy="12" r="8"/></svg>':''}</div>
      </div>
      ${opts.map(mn => {
        const sel = act > 0 && Math.abs(Math.ceil((act - Date.now())/60000) - mn) < 2;
        return `<div class="picker-option${sel?' selected':''}" onclick="_setSleepMin(${mn})">
          <div class="picker-option-info"><div class="picker-option-label">${mn} minutes</div><div class="picker-option-sub">${mn < 60 ? mn+' min from now' : '1 hour from now'}</div></div>
          <div class="picker-radio">${sel?'<svg viewBox="0 0 24 24" fill="var(--gold)"><circle cx="12" cy="12" r="8"/></svg>':''}</div>
        </div>`;
      }).join('')}
      ${(trkM || (act > 0 && act > Date.now())) ? `<div class="picker-option danger-item" onclick="_cancelSleep()"><div class="picker-option-info"><div class="picker-option-label danger-text">Cancel Timer</div><div class="picker-option-sub">Turn off sleep timer</div></div></div>` : ''}
    </div>
  </div>`;
  m.onclick = e => { if (e.target === m) m.remove(); };
  document.body.appendChild(m);
}

window._setSleepMin = function(m) {
  appSettings.sleepTimerEnd = Date.now() + m*60000; appSettings.sleepMode = 'timer';
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  _startSleep(); _removeEl('sleep-sheet'); renderSettingsPage();
  if (typeof showToast==='function') showToast(`Sleep · ${m} min`);
};
window._setSleepTrack = function() {
  appSettings.sleepTimerEnd = -1; appSettings.sleepMode = 'track';
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  _removeEl('sleep-sheet'); renderSettingsPage();
  if (typeof showToast==='function') showToast('Stops after this track');
};
window._cancelSleep = function() {
  appSettings.sleepTimerEnd = null; appSettings.sleepMode = 'timer';
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  clearInterval(_sleepInterval); _removeEl('sleep-sheet'); renderSettingsPage();
  if (typeof showToast==='function') showToast('Sleep timer cancelled');
};

function _startSleep() {
  clearInterval(_sleepInterval);
  _sleepInterval = setInterval(() => {
    if (!appSettings.sleepTimerEnd || appSettings.sleepTimerEnd < 0) { clearInterval(_sleepInterval); return; }
    if (Date.now() >= appSettings.sleepTimerEnd) {
      clearInterval(_sleepInterval); appSettings.sleepTimerEnd = null;
      localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
      const a = window._aurumAudio || document.querySelector('audio');
      if (a) a.pause();
      if (typeof showToast      ==='function') showToast('Sleep timer · Music stopped 🌙');
      if (typeof updatePlayerUI ==='function') updatePlayerUI();
    }
  }, 10000);
}

window._checkSleepOnTrackEnd = function() {
  if (appSettings.sleepMode === 'track' && appSettings.sleepTimerEnd === -1) {
    appSettings.sleepTimerEnd = null;
    localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
    const a = window._aurumAudio || document.querySelector('audio');
    if (a) a.pause();
    if (typeof showToast      ==='function') showToast('Good night 🌙');
    if (typeof updatePlayerUI ==='function') updatePlayerUI();
  }
};

if (appSettings.sleepTimerEnd > Date.now()) _startSleep();

// ─── EQUALIZER ────────────────────────────────────────────────────────────────
const EQ_PRESETS = {
  flat:[0,0,0,0,0,0,0,0,0,0], bass:[6,5,4,2,0,0,0,0,0,0],
  vocal:[-2,-2,0,2,4,4,2,0,-2,-2], pop:[-1,2,4,4,2,0,0,-1,-1,-1],
  rock:[4,3,2,0,-1,-1,2,4,5,5], classical:[4,3,2,0,0,0,0,2,3,4],
};
const EQ_FREQS = ['32','64','125','250','500','1k','2k','4k','8k','16k'];

let _eqFilters = [], _bassNode = null, _loudNode = null, _eqInit = false;

function _initEQ() {
  if (_eqInit) return;
  const ctx = _getCtx(); if (!ctx) return;
  const src = _getSrc(); if (!src) return;
  try {
    _eqFilters = EQ_FREQS.map((f, i) => {
      const fl = ctx.createBiquadFilter();
      fl.type  = i === 0 ? 'lowshelf' : i === 9 ? 'highshelf' : 'peaking';
      fl.frequency.value = parseFloat(f) * (f.includes('k') ? 1000 : 1);
      fl.gain.value = 0; return fl;
    });
    _bassNode = ctx.createBiquadFilter();
    _bassNode.type = 'lowshelf'; _bassNode.frequency.value = 200; _bassNode.gain.value = 0;
    _loudNode = ctx.createDynamicsCompressor();
    _loudNode.threshold.value = -24; _loudNode.knee.value = 30;
    _loudNode.ratio.value = 4; _loudNode.attack.value = 0.003; _loudNode.release.value = 0.25;
    let prev = src;
    _eqFilters.forEach(f => { prev.connect(f); prev = f; });
    prev.connect(_bassNode); _bassNode.connect(_loudNode); _loudNode.connect(ctx.destination);
    _eqInit = true; _applyEQ();
  } catch(e) { console.warn('[EQ]', e.message); }
}

function _applyEQ() {
  if (!_eqFilters.length) return;
  const bands = appSettings.eqEnabled ? appSettings.eqBands : [0,0,0,0,0,0,0,0,0,0];
  _eqFilters.forEach((f, i) => { try { f.gain.value = bands[i] || 0; } catch(e) {} });
  if (_bassNode) _bassNode.gain.value = appSettings.bassBoost ? 8 : 0;
  if (_loudNode) { _loudNode.threshold.value = appSettings.loudnessEnhancer ? -36 : -24; _loudNode.ratio.value = appSettings.loudnessEnhancer ? 12 : 4; }
}

function openEQSheet() {
  _initEQ();
  const s = appSettings;
  const pLabels = {flat:'Flat',bass:'Bass Boost',vocal:'Vocal',pop:'Pop',rock:'Rock',classical:'Classical'};
  _removeEl('eq-sheet');
  const sheet = document.createElement('div');
  sheet.id = 'eq-sheet'; sheet.className = 'modal-overlay open';
  sheet.innerHTML = `<div class="modal-sheet eq-sheet-inner">
    <div class="modal-handle"></div>
    <div class="eq-header">
      <div class="picker-title" style="margin-bottom:0">Equalizer</div>
      <label class="settings-toggle" style="margin-left:auto">
        <input type="checkbox" id="eq-master"${s.eqEnabled?' checked':''} onchange="_toggleEQMaster(this.checked)">
        <span class="settings-toggle-track"></span>
      </label>
    </div>
    <div class="eq-presets">${Object.keys(pLabels).map(p=>`<button class="eq-preset-btn${s.eqPreset===p?' active':''}" onclick="_setEQPreset('${p}')">${pLabels[p]}</button>`).join('')}</div>
    <div class="eq-bands">${EQ_FREQS.map((f,i)=>`
      <div class="eq-band">
        <span class="eq-gain" id="eq-g${i}">${s.eqBands[i]>=0?'+':''}${s.eqBands[i]}dB</span>
        <input type="range" class="eq-fader" orient="vertical" min="-12" max="12" step="1" value="${s.eqBands[i]}" oninput="_setEQBand(${i},+this.value)">
        <span class="eq-freq">${f}</span>
      </div>`).join('')}
    </div>
  </div>`;
  sheet.onclick = e => { if (e.target === sheet) sheet.remove(); };
  document.body.appendChild(sheet);
}

window._toggleEQMaster = function(on) { saveSetting('eqEnabled', on); _applyEQ(); };
window._setEQPreset    = function(p)  {
  appSettings.eqPreset = p; appSettings.eqBands = [...(EQ_PRESETS[p] || EQ_PRESETS.flat)];
  saveSetting('eqEnabled', p !== 'flat'); _applyEQ();
  EQ_FREQS.forEach((_, i) => {
    const el = document.querySelectorAll('.eq-fader')[i]; if (el) el.value = appSettings.eqBands[i];
    const g  = document.getElementById('eq-g'+i);
    if (g) g.textContent = (appSettings.eqBands[i]>=0?'+':'')+appSettings.eqBands[i]+'dB';
  });
  document.querySelectorAll('.eq-preset-btn').forEach(b => b.classList.toggle('active', b.textContent === {flat:'Flat',bass:'Bass Boost',vocal:'Vocal',pop:'Pop',rock:'Rock',classical:'Classical'}[p]));
};
window._setEQBand = function(i, v) {
  appSettings.eqBands[i] = v; appSettings.eqPreset = 'custom';
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  const g = document.getElementById('eq-g'+i);
  if (g) g.textContent = (v>=0?'+':'')+v+'dB';
  if (_eqFilters[i] && appSettings.eqEnabled) _eqFilters[i].gain.value = v;
};

function toggleAudioFX(key, enabled) {
  // Gate premium audio features
  if (enabled && _premiumGate(key)) return;
  saveSetting(key, enabled); _initEQ(); _applyEQ();
  if (typeof showToast==='function') showToast(`${({bassBoost:'Bass Boost',virtualizer:'Virtualizer',loudnessEnhancer:'Loudness Enhancer'}[key])} ${enabled?'on':'off'}`);
  renderSettingsPage();
}

// ─── PICKERS ──────────────────────────────────────────────────────────────────
function openThemePicker()          { _picker('Theme',[{value:'dark',label:'Dark',sub:'Default dark'},{value:'amoled',label:'AMOLED Black',sub:'Pure black · OLED'},{value:'light',label:'Light',sub:'Warm light'}],appSettings.theme,v=>{saveSetting('theme',v);renderSettingsPage();}); }
function openStreamQualityPicker()  { _picker('Stream Quality',[{value:'auto',label:'Auto',sub:'Best quality'},{value:'high',label:'High',sub:'320 kbps'},{value:'low',label:'Low',sub:'128 kbps'}],appSettings.streamQuality,v=>{saveSetting('streamQuality',v);renderSettingsPage();}); }
function openPlaybackSpeedPicker()  { _picker('Playback Speed',[{value:0.5,label:'0.5×',sub:'Half speed'},{value:0.75,label:'0.75×',sub:'Slightly slower'},{value:1.0,label:'Normal (1×)',sub:'Default'},{value:1.25,label:'1.25×',sub:'Faster'},{value:1.5,label:'1.5×',sub:'Fast'},{value:2.0,label:'2×',sub:'Double'}],appSettings.playbackSpeed,v=>{saveSetting('playbackSpeed',+v);const a=window._aurumAudio||document.querySelector('audio');if(a)a.playbackRate=+v;renderSettingsPage();}); }
function openVisualizerStylePicker(){ _picker('Visualizer',[{value:'bars',label:'Bars',sub:'Frequency bars'},{value:'wave',label:'Waveform',sub:'Audio line'},{value:'circular',label:'Circular',sub:'Radial beats'}],appSettings.visualizerStyle,v=>{saveSetting('visualizerStyle',v);renderSettingsPage();}); }
function openCornerRadiusPicker()   { _picker('Corner Style',[{value:'rounded',label:'Rounded',sub:'Smooth'},{value:'pill',label:'Pill',sub:'Fully rounded'},{value:'sharp',label:'Sharp',sub:'Geometric'}],appSettings.cornerRadius,v=>{saveSetting('cornerRadius',v);renderSettingsPage();}); }
function openHistoryLimitPicker()   { _picker('History Limit',[{value:10,label:'10 songs',sub:'Last 10'},{value:20,label:'20 songs',sub:'Recommended'},{value:50,label:'50 songs',sub:'Extended'},{value:0,label:'Unlimited',sub:'Never trim'}],appSettings.historyLimit,v=>{saveSetting('historyLimit',+v);renderSettingsPage();}); }

function openAccentColorPicker() {
  _removeEl('settings-picker-sheet');
  const colors = [{value:'gold',label:'Gold',hex:'#b89640'},{value:'rose',label:'Rose',hex:'#c05f7a'},{value:'sky',label:'Sky',hex:'#4a9cc8'},{value:'sage',label:'Sage',hex:'#5a9e72'},{value:'violet',label:'Violet',hex:'#8b5fcf'},{value:'ember',label:'Ember',hex:'#c4622d'}];
  const m = document.createElement('div');
  m.id = 'settings-picker-sheet'; m.className = 'modal-overlay open';
  m.innerHTML = `<div class="modal-sheet picker-sheet"><div class="modal-handle"></div><div class="picker-title">Accent Color</div>
    <div class="color-grid">${colors.map(c=>`<div class="color-swatch${c.value===appSettings.accentColor?' selected':''}" onclick="_setAccent('${c.value}')"><div class="color-circle" style="background:${c.hex}">${c.value===appSettings.accentColor?'<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>':''}</div><span>${c.label}</span></div>`).join('')}
    </div></div>`;
  m.onclick = e => { if (e.target === m) m.remove(); };
  document.body.appendChild(m);
}
window._setAccent = function(v) {
  saveSetting('accentColor', v); _removeEl('settings-picker-sheet'); renderSettingsPage(); _haptic([10,30,10]);
};

let _pickerOpts = [], _pickerCb = null;
function _picker(title, opts, current, onSelect) {
  _removeEl('settings-picker-sheet');
  const m = document.createElement('div');
  m.id = 'settings-picker-sheet'; m.className = 'modal-overlay open';
  m.innerHTML = `<div class="modal-sheet picker-sheet"><div class="modal-handle"></div><div class="picker-title">${title}</div>
    <div id="picker-options">${opts.map(o=>`<div class="picker-option${String(o.value)===String(current)?' selected':''}" onclick="_pickerSel('${o.value}')">
      <div class="picker-option-info"><div class="picker-option-label">${o.label}</div><div class="picker-option-sub">${o.sub}</div></div>
      <div class="picker-radio">${String(o.value)===String(current)?'<svg viewBox="0 0 24 24" fill="var(--gold)"><circle cx="12" cy="12" r="8"/></svg>':''}</div>
    </div>`).join('')}</div></div>`;
  m.onclick = e => { if (e.target === m) m.remove(); };
  document.body.appendChild(m);
  _pickerOpts = opts; _pickerCb = onSelect;
}
window._pickerSel = function(v) {
  const match = _pickerOpts.find(o => String(o.value) === String(v));
  _pickerCb(match ? match.value : v);
  _removeEl('settings-picker-sheet'); _haptic([8,20,8]);
};

// ─── TOGGLES ──────────────────────────────────────────────────────────────────
function toggleDataSaver(e) {
  saveSetting('dataSaver', e);
  if (typeof showToast==='function') showToast(e ? 'Data Saver on · 128kbps' : 'Data Saver off');
  renderSettingsPage();
}
function toggleCrossfade(e) {
  saveSetting('crossfade', e);
  const el = document.getElementById('cf-exp');
  if (el) el.classList.toggle('open', e);
}

// ─── SHAKE TO SKIP ────────────────────────────────────────────────────────────
const _SK = { P1:12, P2:10, WIN:600, COOL:1400, DROP:60, EMA:0.03 };
let _skLast = 0, _skPeak1 = false, _skPeak1T = 0;
let _skBase = 0, _skBaseN = 0, _skOn = false;

function _shakeHandler(e) {
  if (!appSettings.shakeToSkip || !e.accelerationIncludingGravity) return;
  const {x=0,y=0,z=0} = e.accelerationIncludingGravity;
  const G   = Math.sqrt(x*x + y*y + z*z);
  if (_skBaseN < 20) { _skBase = G; _skBaseN++; return; }
  _skBase = _skBase * (1 - _SK.EMA) + G * _SK.EMA;
  const dG  = G - _skBase;
  const now = Date.now();
  if (G > _SK.DROP && !_skPeak1) return;
  if (now - _skLast < _SK.COOL) return;
  if (!_skPeak1) {
    if (dG > _SK.P1) { _skPeak1 = true; _skPeak1T = now; }
  } else {
    if (now - _skPeak1T > _SK.WIN) { _skPeak1 = false; return; }
    if (dG > _SK.P2) {
      _skPeak1 = false; _skLast = now;
      if (typeof nextTrack === 'function') nextTrack();
      if (typeof showToast === 'function') showToast('↪ Shake · Next track');
      _haptic([15, 50, 15]);
    }
  }
}

function _attachShake() {
  if (_skOn) return;
  _skBaseN = 0; _skBase = 0; _skPeak1 = false;
  window.addEventListener('devicemotion', _shakeHandler, { passive:true });
  _skOn = true;
}

function _detachShake() {
  if (!_skOn) return;
  window.removeEventListener('devicemotion', _shakeHandler);
  _skOn = false; _skPeak1 = false;
}

function toggleShakeToSkip(enabled) {
  // ── PREMIUM GATE ──
  if (enabled && !(window.userAuth && window.userAuth.isLoggedIn)) {
    // Force setting off + flip checkbox back
    appSettings.shakeToSkip = false;
    localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
    const cb = document.querySelector('input[onchange*="toggleShakeToSkip"]');
    if (cb) cb.checked = false;
    if (typeof checkFeatureAccess === 'function') checkFeatureAccess('default');
    else if (typeof openLoginModal === 'function') openLoginModal();
    return;
  }

  if (enabled) {
    if (typeof DeviceMotionEvent !== 'undefined' && typeof DeviceMotionEvent.requestPermission === 'function') {
      DeviceMotionEvent.requestPermission()
        .then(state => {
          if (state === 'granted') {
            saveSetting('shakeToSkip', true); _attachShake();
            if (typeof showToast==='function') showToast('Shake to skip · On');
          } else {
            // Permission denied — reset setting
            saveSetting('shakeToSkip', false);
            const cb = document.querySelector('input[onchange*="toggleShakeToSkip"]');
            if (cb) cb.checked = false;
            if (typeof showToast==='function') showToast('Motion permission denied');
          }
          renderSettingsPage();
        })
        .catch(() => {
          saveSetting('shakeToSkip', false);
          if (typeof showToast==='function') showToast('Permission error');
          renderSettingsPage();
        });
    } else {
      saveSetting('shakeToSkip', true); _attachShake();
      if (typeof showToast==='function') showToast('Shake to skip · On');
      renderSettingsPage();
    }
  } else {
    // Turning OFF — always allowed, always detach
    saveSetting('shakeToSkip', false);
    _detachShake();
    if (typeof showToast==='function') showToast('Shake to skip · Off');
    renderSettingsPage();
  }
}

// Boot: only attach if BOTH setting is on AND user is logged in
if (appSettings.shakeToSkip &&
    window.userAuth && window.userAuth.isLoggedIn &&
    (typeof DeviceMotionEvent === 'undefined' ||
     typeof DeviceMotionEvent.requestPermission !== 'function')) {
  _attachShake();
}

// ─── CSS for account card (injected once) ─────────────────────────────────────
(function _injectAccountCardCSS() {
  if (document.getElementById('aurum-account-card-css')) return;
  const st = document.createElement('style');
  st.id = 'aurum-account-card-css';
  st.textContent = `
  .settings-account-card{
    margin:12px 16px 4px;padding:14px 16px;
    background:var(--surface2);
    border:1px solid rgba(184,150,64,0.15);
    border-radius:18px;
    display:flex;align-items:center;gap:14px;
    cursor:pointer;-webkit-tap-highlight-color:transparent;
    transition:background 0.18s ease;
  }
  .settings-account-card:active{background:var(--surface3)}
  .settings-account-avatar{
    width:44px;height:44px;border-radius:50%;
    object-fit:cover;border:2px solid rgba(184,150,64,0.4);flex-shrink:0;
  }
  .settings-account-avatar-placeholder{
    width:44px;height:44px;border-radius:50%;
    background:rgba(184,150,64,0.08);
    border:1.5px dashed rgba(184,150,64,0.28);
    display:flex;align-items:center;justify-content:center;flex-shrink:0;
  }
  .settings-account-avatar-placeholder svg{
    width:20px;height:20px;stroke:rgba(184,150,64,0.55);fill:none;
    stroke-width:1.8;stroke-linecap:round;
  }
  .settings-account-info{flex:1;min-width:0}
  .settings-account-name{
    font-size:14px;font-weight:700;color:var(--text);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  .settings-account-sub{font-size:11px;color:var(--text3);margin-top:2px}
  .settings-account-badge{
    font-size:10px;font-weight:700;color:var(--gold);
    background:rgba(184,150,64,0.1);border:1px solid rgba(184,150,64,0.2);
    padding:3px 9px;border-radius:100px;white-space:nowrap;
  }`;
  document.head.appendChild(st);
})();

// ─── CLEAR / DATA ─────────────────────────────────────────────────────────────
function confirmClearSearch() {
  _confirm('Clear all search history?', () => {
    localStorage.removeItem('aurum_recent');
    if (typeof recentSearches !== 'undefined') recentSearches = [];
    if (typeof showToast==='function') showToast('Search history cleared');
  });
}
function confirmClearCache() {
  _confirm('Remove all downloaded songs?', () => {
    if (typeof openDlDb === 'function') {
      openDlDb().then(db => {
        const tx = db.transaction('songs','readwrite');
        tx.objectStore('songs').clear();
        tx.oncomplete = () => {
          localStorage.removeItem('aurum_dl_meta');
          if (typeof renderLibrary==='function') renderLibrary();
          renderSettingsPage();
          if (typeof showToast==='function') showToast('Downloads cleared');
        };
      });
    }
  });
}
function confirmClearAllData() {
  _confirm('Reset ALL app data? Cannot be undone.', () => {
    Object.keys(localStorage).filter(k => k.startsWith('aurum_') && k !== 'aurum_settings')
      .forEach(k => localStorage.removeItem(k));
    try { if (typeof openDlDb!=='undefined') openDlDb().then(db => db.transaction('songs','readwrite').objectStore('songs').clear()).catch(()=>{}); } catch(e) {}
    ['savedSongs','playlists','recentlyPlayed','recentSearches'].forEach(n => { if (typeof window[n]!=='undefined') window[n]=[]; });
    if (typeof renderLibrary==='function') renderLibrary();
    if (typeof showToast    ==='function') showToast('All data cleared');
  });
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
applySettings();
setTimeout(_patchApp, 0);
