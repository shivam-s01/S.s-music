// ─── SETTINGS SYSTEM ──────────────────────────────────────────────────────────
const DEFAULT_SETTINGS = {
  theme: 'dark',
  dataSaver: false,
  streamQuality: 'auto',
  animations: true,
  dynamicColor: true,
  showVisualizer: true,
  visualizerStyle: 'bars',
  crossfade: false,
  crossfadeDuration: 3,
  gaplessPlayback: false,
  playbackSpeed: 1.0,
  volumeNormalize: false,
  eqEnabled: false,
  eqPreset: 'flat',
  eqBands: [0,0,0,0,0,0,0,0,0,0],
  bassBoost: false,
  virtualizer: false,
  loudnessEnhancer: false,
  cornerRadius: 'rounded',
  accentColor: 'gold',
  glassIntensity: 50,
  ambientEdgeGlow: true,
  blurredArtworkBg: true,
  shakeToSkip: false,
  hapticFeedback: true,
  headphoneAutoPause: true,
  audioDucking: false,
  showTabTitle: true,
  historyLimit: 20,
  saveHistory: true,
  sleepTimerEnd: null,
  sleepMode: 'timer',
};

let appSettings = {
  ...DEFAULT_SETTINGS,
  ...JSON.parse(localStorage.getItem('aurum_settings') || '{}')
};

function saveSetting(key, value) {
  appSettings[key] = value;
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  applySettings();
}

// ─── DYNAMIC STYLE INJECTION ──────────────────────────────────────────────────
function _injectStyles() {
  let el = document.getElementById('aurum-dynamic-styles');
  if (!el) {
    el = document.createElement('style');
    el.id = 'aurum-dynamic-styles';
    document.head.appendChild(el);
  }

  const blurCSS = appSettings.blurredArtworkBg ? `
    #fp-bg-art {
      filter: blur(32px) saturate(1.9) brightness(0.48) !important;
      transform: scale(1.18) !important;
      opacity: 1 !important;
      transition: opacity 1.2s ease, filter 1s ease !important;
    }
    #fp-bg-overlay {
      background: linear-gradient(
        to bottom,
        rgba(0,0,0,0.18) 0%,
        rgba(0,0,0,0.42) 45%,
        rgba(0,0,0,0.82) 80%,
        rgba(0,0,0,0.96) 100%
      ) !important;
    }
    #fullscreen-player {
      --fp-blur-active: 1;
    }
    .fp-art-wrap img {
      box-shadow: 0 24px 64px rgba(0,0,0,0.72), 0 0 0 1px rgba(255,255,255,0.06) !important;
    }
  ` : `
    #fp-bg-art {
      filter: blur(0px) brightness(0.3) !important;
      transform: scale(1.05) !important;
    }
  `;

  el.textContent = blurCSS;
}

function applySettings() {
  const root = document.documentElement;

  // Theme
  root.classList.remove('theme-light','theme-amoled','theme-dark');
  root.classList.add('theme-' + appSettings.theme);
  if (appSettings.theme === 'amoled') {
    root.style.setProperty('--bg','#000000');
    root.style.setProperty('--surface','#000000');
    root.style.setProperty('--surface2','#0a0a0a');
    root.style.setProperty('--surface3','#111111');
  } else if (appSettings.theme === 'light') {
    root.style.setProperty('--bg','#f5f2ed');
    root.style.setProperty('--surface','#ffffff');
    root.style.setProperty('--surface2','#f0ece4');
    root.style.setProperty('--surface3','#e8e2d8');
    root.style.setProperty('--text','#1a1814');
    root.style.setProperty('--text2','#4a4540');
    root.style.setProperty('--text3','#8a8278');
  } else {
    ['--bg','--surface','--surface2','--surface3','--text','--text2','--text3']
      .forEach(p => root.style.removeProperty(p));
  }

  // Accent Color
  const accents = {
    gold:   { main:'#b89640', light:'#d4af55' },
    rose:   { main:'#c05f7a', light:'#e07090' },
    sky:    { main:'#4a9cc8', light:'#6ab8e0' },
    sage:   { main:'#5a9e72', light:'#7abf90' },
    violet: { main:'#8b5fcf', light:'#a878e8' },
    ember:  { main:'#c4622d', light:'#e07840' },
  };
  const ac = accents[appSettings.accentColor] || accents.gold;
  root.style.setProperty('--gold', ac.main);
  root.style.setProperty('--gold-l', ac.light);

  // Corner Radius
  const radii = { rounded:'12px', pill:'999px', sharp:'4px' };
  root.style.setProperty('--radius', radii[appSettings.cornerRadius] || '12px');

  // Animations
  if (!appSettings.animations) root.style.setProperty('--anim-speed','0s');
  else root.style.removeProperty('--anim-speed');

  // Visualizer
  const viz = document.getElementById('fp-visualizer');
  if (viz) viz.style.display = appSettings.showVisualizer ? '' : 'none';

  // Dynamic Color
  const glow = document.getElementById('fp-ambient-glow');
  if (glow) glow.style.display = appSettings.dynamicColor ? '' : 'none';

  // Ambient Edge Glow element
  let edgeEl = document.getElementById('ambient-edge-glow');
  if (appSettings.ambientEdgeGlow && !edgeEl) {
    edgeEl = document.createElement('div');
    edgeEl.id = 'ambient-edge-glow';
    document.body.appendChild(edgeEl);

    // Edge glow CSS inject karo agar settings.css mein nahi hai
    if (!document.getElementById('edge-glow-styles')) {
      const s = document.createElement('style');
      s.id = 'edge-glow-styles';
      s.textContent = `
        #ambient-edge-glow {
          position: fixed; inset: 0; pointer-events: none; z-index: 9998;
          --eg-r:184; --eg-g:150; --eg-b:64;
          background:
            radial-gradient(ellipse 60% 20% at 50% 0%, rgba(var(--eg-r),var(--eg-g),var(--eg-b),0.18) 0%, transparent 100%),
            radial-gradient(ellipse 60% 20% at 50% 100%, rgba(var(--eg-r),var(--eg-g),var(--eg-b),0.18) 0%, transparent 100%),
            radial-gradient(ellipse 20% 60% at 0% 50%, rgba(var(--eg-r),var(--eg-g),var(--eg-b),0.14) 0%, transparent 100%),
            radial-gradient(ellipse 20% 60% at 100% 50%, rgba(var(--eg-r),var(--eg-g),var(--eg-b),0.14) 0%, transparent 100%);
          transition: background 1.2s ease;
          animation: edge-pulse 3s ease-in-out infinite;
        }
        @keyframes edge-pulse { 0%,100%{opacity:.7} 50%{opacity:1} }
      `;
      document.head.appendChild(s);
    }
  }
  if (edgeEl) edgeEl.style.display = appSettings.ambientEdgeGlow ? '' : 'none';

  // Glass
  root.style.setProperty('--glass-blur', (appSettings.glassIntensity / 5) + 'px');
  root.style.setProperty('--glass-alpha', (appSettings.glassIntensity / 400).toFixed(3));

  // Playback speed
  const aud = document.querySelector('audio');
  if (aud) aud.playbackRate = appSettings.playbackSpeed;

  // Shake
  window._shakeActive = appSettings.shakeToSkip;

  // Tab title
  if (!appSettings.showTabTitle) document.title = 'Aurum';

  // Blurred artwork background
  _injectStyles();
}

// ─── FETCH PATCH ──────────────────────────────────────────────────────────────
window._getQualityParam = function() {
  if (appSettings.dataSaver || appSettings.streamQuality === 'low') return '&low_quality=true';
  return '';
};
const _origFetch = window.fetch;
window.fetch = function(url, opts) {
  if (typeof url === 'string' && url.includes('/api/saavn?')) url += window._getQualityParam();
  return _origFetch.call(this, url, opts);
};

// ─── PATCH APP.JS ─────────────────────────────────────────────────────────────
function _patchAppFunctions() {
  // 1. extractDominantColor → edge glow color
  if (typeof extractDominantColor === 'function') {
    const _orig = extractDominantColor;
    window.extractDominantColor = function(imgEl, callback) {
      _orig(imgEl, function(r, g, b) {
        callback(r, g, b);
        const el = document.getElementById('ambient-edge-glow');
        if (el && appSettings.ambientEdgeGlow) {
          el.style.setProperty('--eg-r', r);
          el.style.setProperty('--eg-g', g);
          el.style.setProperty('--eg-b', b);
        }
      });
    };
  }

  // 2. updatePlayerUI → browser tab title
  if (typeof updatePlayerUI === 'function') {
    const _orig = updatePlayerUI;
    window.updatePlayerUI = function() {
      _orig.apply(this, arguments);
      if (appSettings.showTabTitle && typeof currentTrack !== 'undefined' && currentTrack) {
        document.title = `${currentTrack.trackName || '♪'} · ${currentTrack.artistName || ''} — Aurum`;
      } else if (!appSettings.showTabTitle) {
        document.title = 'Aurum';
      }
    };
  }

  // 3. addToRecentlyPlayed → history limit + incognito
  if (typeof addToRecentlyPlayed === 'function') {
    const _orig = addToRecentlyPlayed;
    window.addToRecentlyPlayed = function(song) {
      if (!appSettings.saveHistory) return;
      _orig.apply(this, arguments);
      const limit = appSettings.historyLimit === 0 ? 500 : (appSettings.historyLimit || 20);
      if (typeof recentlyPlayed !== 'undefined' && recentlyPlayed.length > limit) {
        recentlyPlayed = recentlyPlayed.slice(0, limit);
        localStorage.setItem('aurum_recent_played', JSON.stringify(recentlyPlayed));
      }
    };
  }

  // 4. Sleep timer track-end hook
  const aud = document.querySelector('audio');
  if (aud) {
    aud.addEventListener('ended', () => window._checkSleepOnTrackEnd?.());

    // 5. Headphone auto-pause
    if ('mediaDevices' in navigator) {
      navigator.mediaDevices.addEventListener('devicechange', async () => {
        if (!appSettings.headphoneAutoPause) return;
        try {
          const devices = await navigator.mediaDevices.enumerateDevices();
          const outs = devices.filter(d => d.kind === 'audiooutput').length;
          if (outs <= 1 && typeof isPlaying !== 'undefined' && isPlaying) {
            if (typeof togglePlay === 'function') togglePlay();
            if (typeof showToast === 'function') showToast('Headphones disconnected · Paused');
          }
        } catch(e) {}
      });
    }

    // 6. Audio ducking
    document.addEventListener('visibilitychange', () => {
      if (!appSettings.audioDucking) return;
      if (document.hidden) {
        aud._preDuck = aud.volume;
        aud.volume = Math.max(0, aud.volume * 0.3);
      } else {
        if (aud._preDuck !== undefined) { aud.volume = aud._preDuck; delete aud._preDuck; }
      }
    });
  }
}

// ─── OPEN / CLOSE ─────────────────────────────────────────────────────────────
function openSettings() {
  renderSettingsPage();
  document.getElementById('settings-panel').classList.add('open');
  if (typeof haptic === 'function') haptic(10);
}
function closeSettings() {
  document.getElementById('settings-panel').classList.remove('open');
}

// ─── RENDER SETTINGS ──────────────────────────────────────────────────────────
function renderSettingsPage() {
  const body = document.getElementById('settings-body');
  const s = appSettings;

  const qualLabel   = s.streamQuality === 'auto' ? 'Auto (Best)' : s.streamQuality === 'high' ? 'High (320kbps)' : 'Low (128kbps)';
  const speedLabel  = s.playbackSpeed === 1 ? 'Normal (1×)' : s.playbackSpeed + '×';
  const themeLabel  = {dark:'Dark',amoled:'AMOLED Black',light:'Light'}[s.theme] || 'Dark';
  const radiusLabel = {rounded:'Rounded',pill:'Pill / Capsule',sharp:'Sharp'}[s.cornerRadius] || 'Rounded';
  const vizLabel    = {bars:'Bars',wave:'Waveform',circular:'Circular'}[s.visualizerStyle] || 'Bars';
  const accentLabel = {gold:'Gold',rose:'Rose',sky:'Sky Blue',sage:'Sage Green',violet:'Violet',ember:'Ember'}[s.accentColor] || 'Gold';
  const eqLabel     = {flat:'Flat',bass:'Bass Boost',vocal:'Vocal Clarity',pop:'Pop',rock:'Rock',classical:'Classical',custom:'Custom'}[s.eqPreset] || 'Flat';
  const sleepLabel  = _getSleepTimerLabel();
  const histLabel   = s.historyLimit === 0 ? 'Unlimited' : s.historyLimit + ' songs';
  const accentDot   = `<span class="accent-dot accent-${s.accentColor}"></span>`;

  body.innerHTML = `

    <!-- PLAYBACK -->
    <div class="settings-section">
      <div class="settings-section-label">Playback</div>

      <div class="settings-item" onclick="openStreamQualityPicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Stream Quality</div><div class="settings-item-sub">${qualLabel}</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item" onclick="openPlaybackSpeedPicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Playback Speed</div><div class="settings-item-sub">${speedLabel}</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon data-saver-icon ${s.dataSaver?'active':''}"><svg viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Data Saver</div><div class="settings-item-sub">128kbps instead of 320kbps</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.dataSaver?'checked':''} onchange="toggleDataSaver(this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Gapless Playback</div><div class="settings-item-sub">No silence between tracks</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.gaplessPlayback?'checked':''} onchange="saveSetting('gaplessPlayback',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item settings-item-expandable">
        <div class="settings-item-full">
          <div class="settings-item-row-top">
            <div class="settings-item-left">
              <div class="settings-item-icon"><svg viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg></div>
              <div class="settings-item-info"><div class="settings-item-title">Crossfade</div><div class="settings-item-sub">${s.crossfade ? s.crossfadeDuration+'s transition' : 'Smooth song transitions'}</div></div>
            </div>
            <label class="settings-toggle"><input type="checkbox" ${s.crossfade?'checked':''} onchange="toggleCrossfade(this.checked)"><span class="settings-toggle-track"></span></label>
          </div>
          <div class="settings-sub-expand ${s.crossfade?'open':''}" id="crossfade-expand">
            <div class="expand-label-row"><span>Duration</span><span class="expand-value" id="cf-val">${s.crossfadeDuration}s</span></div>
            <input type="range" class="settings-slider" min="1" max="10" step="1" value="${s.crossfadeDuration}" oninput="document.getElementById('cf-val').textContent=this.value+'s';saveSetting('crossfadeDuration',+this.value)">
          </div>
        </div>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="15" y1="9" x2="21" y2="15"/><line x1="21" y1="9" x2="15" y2="15"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Volume Normalization</div><div class="settings-item-sub">Same loudness across all tracks</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.volumeNormalize?'checked':''} onchange="saveSetting('volumeNormalize',this.checked);showToast(this.checked?'Normalization on':'Normalization off')"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item" onclick="openSleepTimerSheet()">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.sleepTimerEnd||s.sleepMode==='track'?'icon-active':''}"><svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Sleep Timer</div><div class="settings-item-sub">${sleepLabel}</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
    </div>

    <!-- AUDIO ENGINE -->
    <div class="settings-section">
      <div class="settings-section-label">Audio Engine</div>

      <div class="settings-item" onclick="openEQSheet()">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.eqEnabled?'icon-active':''}"><svg viewBox="0 0 24 24"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Equalizer</div><div class="settings-item-sub">${s.eqEnabled?eqLabel:'Off'} · 10-Band</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.bassBoost?'icon-active':''}"><svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Bass Boost</div><div class="settings-item-sub">Enhance low frequencies</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.bassBoost?'checked':''} onchange="toggleAudioFX('bassBoost',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.virtualizer?'icon-active':''}"><svg viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Virtualizer</div><div class="settings-item-sub">Spatial / 3D surround effect</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.virtualizer?'checked':''} onchange="toggleAudioFX('virtualizer',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.loudnessEnhancer?'icon-active':''}"><svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Loudness Enhancer</div><div class="settings-item-sub">Boost perceived loudness</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.loudnessEnhancer?'checked':''} onchange="toggleAudioFX('loudnessEnhancer',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>
    </div>

    <!-- LOOK & FEEL -->
    <div class="settings-section">
      <div class="settings-section-label">Look & Feel</div>

      <div class="settings-item" onclick="openThemePicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 0 1 0 20"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Theme</div><div class="settings-item-sub">${themeLabel}</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item" onclick="openAccentColorPicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon">${accentDot}</div>
          <div class="settings-item-info"><div class="settings-item-title">Accent Color</div><div class="settings-item-sub">${accentLabel}</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item" onclick="openCornerRadiusPicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5" ry="5"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Corner Style</div><div class="settings-item-sub">${radiusLabel}</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <!-- Blurred Artwork Background -->
      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.blurredArtworkBg?'icon-active':''}">
            <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Blurred Artwork Background</div>
            <div class="settings-item-sub">YT Music style player background</div>
          </div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.blurredArtworkBg?'checked':''} onchange="saveSetting('blurredArtworkBg',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <!-- Glass expandable -->
      <div class="settings-item settings-item-expandable">
        <div class="settings-item-full">
          <div class="settings-item-row-top">
            <div class="settings-item-left">
              <div class="settings-item-icon"><svg viewBox="0 0 24 24"><rect x="2" y="8" width="20" height="13" rx="3" fill="none" stroke-dasharray="3 2"/><path d="M6 8V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v2"/></svg></div>
              <div class="settings-item-info"><div class="settings-item-title">Glass Intensity</div><div class="settings-item-sub">Blur & transparency · ${s.glassIntensity}%</div></div>
            </div>
            <button class="expand-toggle" onclick="toggleExpand('glass-expand',this)"><svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg></button>
          </div>
          <div class="settings-sub-expand" id="glass-expand">
            <div class="expand-label-row"><span>Blur strength</span><span class="expand-value" id="glass-val">${s.glassIntensity}%</span></div>
            <input type="range" class="settings-slider" min="0" max="100" step="5" value="${s.glassIntensity}" oninput="document.getElementById('glass-val').textContent=this.value+'%';saveSetting('glassIntensity',+this.value)">
          </div>
        </div>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><circle cx="13.5" cy="6.5" r="2.5"/><circle cx="19" cy="11" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/><circle cx="8.5" cy="7.5" r="2.5"/><circle cx="6.5" cy="12" r="2.5"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Dynamic Color</div><div class="settings-item-sub">UI adapts to track artwork</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.dynamicColor?'checked':''} onchange="saveSetting('dynamicColor',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.ambientEdgeGlow?'icon-active':''}"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Ambient Edge Glow</div><div class="settings-item-sub">Screen edges glow with artwork color</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.ambientEdgeGlow?'checked':''} onchange="saveSetting('ambientEdgeGlow',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item" onclick="openVisualizerStylePicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Visualizer Style</div><div class="settings-item-sub">${vizLabel} · ${s.showVisualizer?'On':'Off'}</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Animations</div><div class="settings-item-sub">Disable to improve performance</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.animations?'checked':''} onchange="saveSetting('animations',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>
    </div>

    <!-- GESTURES & FEEDBACK -->
    <div class="settings-section">
      <div class="settings-section-label">Gestures & Feedback</div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.shakeToSkip?'icon-active':''}"><svg viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 0 20"/><path d="M12 6v6l3 3"/><path d="M18 14l2 2-2 2"/><path d="M22 16h-4"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Shake to Skip</div><div class="settings-item-sub">Shake phone → next track</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.shakeToSkip?'checked':''} onchange="toggleShakeToSkip(this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12a19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 3.6 1.27h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.9a16 16 0 0 0 6 6l.91-.91a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 21.73 16.92z"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Haptic Feedback</div><div class="settings-item-sub">Subtle vibration on tap & swipe</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.hapticFeedback?'checked':''} onchange="saveSetting('hapticFeedback',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.headphoneAutoPause?'icon-active':''}"><svg viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Headphone Auto-Pause</div><div class="settings-item-sub">Pause when headphones disconnect</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.headphoneAutoPause?'checked':''} onchange="saveSetting('headphoneAutoPause',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Audio Ducking</div><div class="settings-item-sub">Lower volume on call / notification</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.audioDucking?'checked':''} onchange="saveSetting('audioDucking',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>
    </div>

    <!-- LIBRARY & BEHAVIOR -->
    <div class="settings-section">
      <div class="settings-section-label">Library & Behavior</div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon ${s.showTabTitle?'icon-active':''}"><svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Browser Tab Title</div><div class="settings-item-sub">Show song name in browser tab</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.showTabTitle?'checked':''} onchange="saveSetting('showTabTitle',this.checked)"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item" onclick="openHistoryLimitPicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">History Limit</div><div class="settings-item-sub">${histLabel}</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
    </div>

    <!-- PRIVACY -->
    <div class="settings-section">
      <div class="settings-section-label">Privacy</div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Save Listening History</div><div class="settings-item-sub">${s.saveHistory?'History tracked':'Incognito — nothing saved'}</div></div>
        </div>
        <label class="settings-toggle"><input type="checkbox" ${s.saveHistory?'checked':''} onchange="saveSetting('saveHistory',this.checked);renderSettingsPage()"><span class="settings-toggle-track"></span></label>
      </div>

      <div class="settings-item danger-item" onclick="confirmClearSearch()">
        <div class="settings-item-left">
          <div class="settings-item-icon danger-icon"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title danger-text">Clear Search History</div><div class="settings-item-sub">Remove all past searches</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
    </div>

    <!-- STORAGE -->
    <div class="settings-section">
      <div class="settings-section-label">Storage</div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><path d="M22 12H2"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Downloaded Songs</div><div class="settings-item-sub" id="storage-count-text">Calculating…</div></div>
        </div>
      </div>

      <div class="settings-item danger-item" onclick="confirmClearCache()">
        <div class="settings-item-left">
          <div class="settings-item-icon danger-icon"><svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title danger-text">Clear Downloads</div><div class="settings-item-sub">Remove all offline saved songs</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item danger-item" onclick="confirmClearAllData()">
        <div class="settings-item-left">
          <div class="settings-item-icon danger-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title danger-text">Clear All Data</div><div class="settings-item-sub">Resets app — playlists, likes, history</div></div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
    </div>

    <!-- ABOUT -->
    <div class="settings-section">
      <div class="settings-section-label">About</div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div>
          <div class="settings-item-info"><div class="settings-item-title">Aurum</div><div class="settings-item-sub">Version 1.0 · Made with ♪</div></div>
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
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
    </div>
  `;

  _calcStorageInfo();
}

// ─── HELPERS ──────────────────────────────────────────────────────────────────
function toggleExpand(id, btn) {
  const el = document.getElementById(id); if (!el) return;
  el.classList.toggle('open');
  const svg = btn.querySelector('svg');
  if (svg) svg.style.transform = el.classList.contains('open') ? 'rotate(180deg)' : '';
}

async function _calcStorageInfo() {
  const metas = JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]');
  const el = document.getElementById('storage-count-text'); if (!el) return;
  if (!metas.length) { el.textContent = 'No downloads'; return; }
  el.textContent = `${metas.length} song${metas.length !== 1 ? 's' : ''} saved offline`;
  try {
    if ('storage' in navigator && 'estimate' in navigator.storage) {
      const { usage } = await navigator.storage.estimate();
      if (usage) el.textContent += ` · ${(usage/1024/1024).toFixed(1)} MB`;
    }
  } catch(e) {}
}

// ─── SLEEP TIMER ──────────────────────────────────────────────────────────────
let _sleepTimerInterval = null;

function _getSleepTimerLabel() {
  if (appSettings.sleepMode === 'track' && appSettings.sleepTimerEnd === -1) return 'End of current track';
  if (appSettings.sleepTimerEnd && appSettings.sleepTimerEnd > 0) {
    const rem = Math.max(0, appSettings.sleepTimerEnd - Date.now());
    if (rem > 0) return `Stops in ${Math.ceil(rem/60000)} min`;
  }
  return 'Off';
}

function openSleepTimerSheet() {
  document.getElementById('sleep-sheet')?.remove();
  const sheet = document.createElement('div');
  sheet.id = 'sleep-sheet'; sheet.className = 'modal-overlay open';
  const opts = [5,10,15,20,30,45,60,90];
  const active = appSettings.sleepTimerEnd;
  const trackMode = appSettings.sleepMode === 'track' && active === -1;
  const hasActive = (active && active > 0 && active > Date.now()) || trackMode;
  sheet.innerHTML = `
    <div class="modal-sheet picker-sheet sleep-sheet-inner">
      <div class="modal-handle"></div>
      <div class="picker-title">Sleep Timer</div>
      <div class="sleep-options">
        <div class="picker-option ${trackMode?'selected':''}" onclick="_setSleepTrack()">
          <div class="picker-option-info"><div class="picker-option-label">End of Current Track</div><div class="picker-option-sub">Stops when this song ends</div></div>
          <div class="picker-radio">${trackMode?'<svg viewBox="0 0 24 24" fill="var(--gold)"><circle cx="12" cy="12" r="8"/></svg>':''}</div>
        </div>
        ${opts.map(m=>{
          const sel = active && active > 0 && Math.abs(Math.ceil((active-Date.now())/60000)-m) < 2;
          return `<div class="picker-option ${sel?'selected':''}" onclick="_setSleepMinutes(${m})">
            <div class="picker-option-info"><div class="picker-option-label">${m} minutes</div><div class="picker-option-sub">${m<60?m+' min from now':'1 hour from now'}</div></div>
            <div class="picker-radio">${sel?'<svg viewBox="0 0 24 24" fill="var(--gold)"><circle cx="12" cy="12" r="8"/></svg>':''}</div>
          </div>`;
        }).join('')}
        ${hasActive?`<div class="picker-option danger-item" onclick="_cancelSleep()" style="margin-top:6px;"><div class="picker-option-info"><div class="picker-option-label danger-text">Cancel Timer</div><div class="picker-option-sub">Turn off sleep timer</div></div></div>`:''}
      </div>
    </div>`;
  sheet.onclick = e => { if (e.target===sheet) sheet.remove(); };
  document.body.appendChild(sheet);
}

window._setSleepMinutes = function(m) {
  appSettings.sleepTimerEnd = Date.now()+m*60000; appSettings.sleepMode='timer';
  localStorage.setItem('aurum_settings',JSON.stringify(appSettings));
  _startSleepCountdown();
  document.getElementById('sleep-sheet')?.remove();
  renderSettingsPage();
  if(typeof showToast==='function') showToast(`Sleep timer · ${m} min`);
};
window._setSleepTrack = function() {
  appSettings.sleepTimerEnd=-1; appSettings.sleepMode='track';
  localStorage.setItem('aurum_settings',JSON.stringify(appSettings));
  document.getElementById('sleep-sheet')?.remove();
  renderSettingsPage();
  if(typeof showToast==='function') showToast('Stops after this track');
};
window._cancelSleep = function() {
  appSettings.sleepTimerEnd=null; appSettings.sleepMode='timer';
  localStorage.setItem('aurum_settings',JSON.stringify(appSettings));
  clearInterval(_sleepTimerInterval);
  document.getElementById('sleep-sheet')?.remove();
  renderSettingsPage();
  if(typeof showToast==='function') showToast('Sleep timer cancelled');
};

function _startSleepCountdown() {
  clearInterval(_sleepTimerInterval);
  _sleepTimerInterval = setInterval(()=>{
    if(!appSettings.sleepTimerEnd||appSettings.sleepTimerEnd<0){clearInterval(_sleepTimerInterval);return;}
    if(Date.now()>=appSettings.sleepTimerEnd){
      clearInterval(_sleepTimerInterval);
      appSettings.sleepTimerEnd=null;
      localStorage.setItem('aurum_settings',JSON.stringify(appSettings));
      const a=document.querySelector('audio'); if(a)a.pause();
      if(typeof showToast==='function') showToast('Sleep timer · Music stopped 🌙');
      if(typeof updatePlayerUI==='function') updatePlayerUI();
    }
  }, 8000);
}

window._checkSleepOnTrackEnd = function() {
  if(appSettings.sleepMode==='track'&&appSettings.sleepTimerEnd===-1){
    appSettings.sleepTimerEnd=null;
    localStorage.setItem('aurum_settings',JSON.stringify(appSettings));
    const a=document.querySelector('audio'); if(a)a.pause();
    if(typeof showToast==='function') showToast('Good night 🌙');
    if(typeof updatePlayerUI==='function') updatePlayerUI();
  }
};

if(appSettings.sleepTimerEnd&&appSettings.sleepTimerEnd>Date.now()) _startSleepCountdown();

// ─── EQUALIZER ────────────────────────────────────────────────────────────────
const EQ_PRESETS = {
  flat:[0,0,0,0,0,0,0,0,0,0],bass:[6,5,4,2,0,0,0,0,0,0],
  vocal:[-2,-2,0,2,4,4,2,0,-2,-2],pop:[-1,2,4,4,2,0,0,-1,-1,-1],
  rock:[4,3,2,0,-1,-1,2,4,5,5],classical:[4,3,2,0,0,0,0,2,3,4],
};
const EQ_FREQS=['32','64','125','250','500','1k','2k','4k','8k','16k'];
let _audioCtx=null,_eqFilters=[],_bassNode=null,_loudNode=null;

function _initWebAudio(){
  if(_audioCtx)return;
  const aud=document.querySelector('audio');if(!aud)return;
  try{
    _audioCtx=new(window.AudioContext||window.webkitAudioContext)();
    const src=_audioCtx.createMediaElementSource(aud);
    _eqFilters=EQ_FREQS.map((f,i)=>{
      const fl=_audioCtx.createBiquadFilter();
      fl.type=i===0?'lowshelf':i===9?'highshelf':'peaking';
      fl.frequency.value=parseFloat(f)*(f.includes('k')?1000:1);
      fl.gain.value=0;return fl;
    });
    _bassNode=_audioCtx.createBiquadFilter();
    _bassNode.type='lowshelf';_bassNode.frequency.value=200;_bassNode.gain.value=0;
    _loudNode=_audioCtx.createDynamicsCompressor();
    _loudNode.threshold.value=-24;_loudNode.knee.value=30;
    _loudNode.ratio.value=4;_loudNode.attack.value=0.003;_loudNode.release.value=0.25;
    let prev=src;
    _eqFilters.forEach(f=>{prev.connect(f);prev=f;});
    prev.connect(_bassNode);_bassNode.connect(_loudNode);_loudNode.connect(_audioCtx.destination);
    _applyEQ();
  }catch(e){console.warn('[EQ]',e.message);}
}

function _applyEQ(){
  if(!_eqFilters.length)return;
  const bands=appSettings.eqEnabled?appSettings.eqBands:[0,0,0,0,0,0,0,0,0,0];
  _eqFilters.forEach((f,i)=>{try{f.gain.value=bands[i]||0;}catch(e){}});
  if(_bassNode)_bassNode.gain.value=appSettings.bassBoost?8:0;
  if(_loudNode){_loudNode.threshold.value=appSettings.loudnessEnhancer?-36:-24;_loudNode.ratio.value=appSettings.loudnessEnhancer?12:4;}
}

function openEQSheet(){
  _initWebAudio();
  const s=appSettings;
  const presets=['flat','bass','vocal','pop','rock','classical'];
  const pL={flat:'Flat',bass:'Bass Boost',vocal:'Vocal',pop:'Pop',rock:'Rock',classical:'Classical'};
  document.getElementById('eq-sheet')?.remove();
  const sheet=document.createElement('div');
  sheet.id='eq-sheet';sheet.className='modal-overlay open';
  sheet.innerHTML=`
    <div class="modal-sheet eq-sheet-inner">
      <div class="modal-handle"></div>
      <div class="eq-header">
        <div class="picker-title" style="margin-bottom:0">Equalizer</div>
        <label class="settings-toggle" style="margin-left:auto">
          <input type="checkbox" id="eq-master" ${s.eqEnabled?'checked':''} onchange="_toggleEQMaster(this.checked)">
          <span class="settings-toggle-track"></span>
        </label>
      </div>
      <div class="eq-presets">
        ${presets.map(p=>`<button class="eq-preset-btn ${s.eqPreset===p?'active':''}" onclick="_setEQPreset('${p}')">${pL[p]}</button>`).join('')}
      </div>
      <div class="eq-bands">
        ${EQ_FREQS.map((f,i)=>`
          <div class="eq-band">
            <span class="eq-gain" id="eq-g${i}">${s.eqBands[i]>=0?'+':''}${s.eqBands[i]}dB</span>
            <input type="range" class="eq-fader" orient="vertical" min="-12" max="12" step="1" value="${s.eqBands[i]}" oninput="_setEQBand(${i},+this.value)">
            <span class="eq-freq">${f}</span>
          </div>`).join('')}
      </div>
    </div>`;
  sheet.onclick=e=>{if(e.target===sheet)sheet.remove();};
  document.body.appendChild(sheet);
}

window._toggleEQMaster=function(on){saveSetting('eqEnabled',on);_applyEQ();};
window._setEQPreset=function(p){
  appSettings.eqPreset=p;appSettings.eqBands=[...(EQ_PRESETS[p]||EQ_PRESETS.flat)];
  saveSetting('eqEnabled',p!=='flat');_applyEQ();
  EQ_FREQS.forEach((_,i)=>{
    const el=document.querySelectorAll('.eq-fader')[i];if(el)el.value=appSettings.eqBands[i];
    const g=document.getElementById('eq-g'+i);if(g)g.textContent=(appSettings.eqBands[i]>=0?'+':'')+appSettings.eqBands[i]+'dB';
  });
  const pL={flat:'Flat',bass:'Bass Boost',vocal:'Vocal',pop:'Pop',rock:'Rock',classical:'Classical'};
  document.querySelectorAll('.eq-preset-btn').forEach(b=>b.classList.toggle('active',b.textContent===pL[p]));
};
window._setEQBand=function(i,v){
  appSettings.eqBands[i]=v;appSettings.eqPreset='custom';
  localStorage.setItem('aurum_settings',JSON.stringify(appSettings));
  const g=document.getElementById('eq-g'+i);if(g)g.textContent=(v>=0?'+':'')+v+'dB';
  if(_eqFilters[i]&&appSettings.eqEnabled)_eqFilters[i].gain.value=v;
};

function toggleAudioFX(key,enabled){
  saveSetting(key,enabled);_initWebAudio();_applyEQ();
  const l={bassBoost:'Bass Boost',virtualizer:'Virtualizer',loudnessEnhancer:'Loudness Enhancer'};
  if(typeof showToast==='function')showToast(`${l[key]} ${enabled?'on':'off'}`);
  renderSettingsPage();
}

// ─── PICKERS ──────────────────────────────────────────────────────────────────
function openThemePicker(){_openPickerSheet('Theme',[{value:'dark',label:'Dark',sub:'Default dark background'},{value:'amoled',label:'AMOLED Black',sub:'Pure black · Best for OLED'},{value:'light',label:'Light',sub:'Warm light background'}],appSettings.theme,v=>{saveSetting('theme',v);renderSettingsPage();});}
function openStreamQualityPicker(){_openPickerSheet('Stream Quality',[{value:'auto',label:'Auto',sub:'Best quality (recommended)'},{value:'high',label:'High',sub:'320 kbps · Uses more data'},{value:'low',label:'Low',sub:'128 kbps · Saves data'}],appSettings.streamQuality,v=>{saveSetting('streamQuality',v);renderSettingsPage();});}
function openPlaybackSpeedPicker(){_openPickerSheet('Playback Speed',[{value:0.5,label:'0.5×',sub:'Half speed'},{value:0.75,label:'0.75×',sub:'Slightly slower'},{value:1.0,label:'Normal (1×)',sub:'Default'},{value:1.25,label:'1.25×',sub:'Slightly faster'},{value:1.5,label:'1.5×',sub:'Faster'},{value:2.0,label:'2×',sub:'Double speed'}],appSettings.playbackSpeed,v=>{saveSetting('playbackSpeed',+v);const a=document.querySelector('audio');if(a)a.playbackRate=+v;renderSettingsPage();});}
function openVisualizerStylePicker(){_openPickerSheet('Visualizer Style',[{value:'bars',label:'Bars',sub:'Classic frequency bars'},{value:'wave',label:'Waveform',sub:'Audio waveform line'},{value:'circular',label:'Circular',sub:'Radial beat visualizer'}],appSettings.visualizerStyle,v=>{saveSetting('visualizerStyle',v);renderSettingsPage();});}
function openCornerRadiusPicker(){_openPickerSheet('Corner Style',[{value:'rounded',label:'Rounded',sub:'Smooth, modern corners'},{value:'pill',label:'Pill / Capsule',sub:'Fully rounded ends'},{value:'sharp',label:'Sharp',sub:'Minimal, geometric look'}],appSettings.cornerRadius,v=>{saveSetting('cornerRadius',v);renderSettingsPage();});}
function openHistoryLimitPicker(){_openPickerSheet('History Limit',[{value:10,label:'10 songs',sub:'Keep last 10 played'},{value:20,label:'20 songs',sub:'Recommended'},{value:50,label:'50 songs',sub:'Extended history'},{value:0,label:'Unlimited',sub:'Never trim history'}],appSettings.historyLimit,v=>{saveSetting('historyLimit',+v);renderSettingsPage();});}

function openAccentColorPicker(){
  document.getElementById('settings-picker-sheet')?.remove();
  const colors=[{value:'gold',label:'Gold',hex:'#b89640'},{value:'rose',label:'Rose',hex:'#c05f7a'},{value:'sky',label:'Sky Blue',hex:'#4a9cc8'},{value:'sage',label:'Sage Green',hex:'#5a9e72'},{value:'violet',label:'Violet',hex:'#8b5fcf'},{value:'ember',label:'Ember',hex:'#c4622d'}];
  const sheet=document.createElement('div');sheet.id='settings-picker-sheet';sheet.className='modal-overlay open';
  sheet.innerHTML=`<div class="modal-sheet picker-sheet"><div class="modal-handle"></div><div class="picker-title">Accent Color</div><div class="color-grid">${colors.map(c=>`<div class="color-swatch ${c.value===appSettings.accentColor?'selected':''}" onclick="_setAccent('${c.value}')"><div class="color-circle" style="background:${c.hex}">${c.value===appSettings.accentColor?'<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>':''}</div><span>${c.label}</span></div>`).join('')}</div></div>`;
  sheet.onclick=e=>{if(e.target===sheet)sheet.remove();};
  document.body.appendChild(sheet);
}
window._setAccent=function(v){saveSetting('accentColor',v);document.getElementById('settings-picker-sheet')?.remove();renderSettingsPage();};

function _openPickerSheet(title,options,current,onSelect){
  document.getElementById('settings-picker-sheet')?.remove();
  const sheet=document.createElement('div');sheet.id='settings-picker-sheet';sheet.className='modal-overlay open';
  sheet.innerHTML=`<div class="modal-sheet picker-sheet"><div class="modal-handle"></div><div class="picker-title">${title}</div><div id="picker-options">${options.map(o=>`<div class="picker-option ${String(o.value)===String(current)?'selected':''}" onclick="_pickerSelect('${o.value}')"><div class="picker-option-info"><div class="picker-option-label">${o.label}</div><div class="picker-option-sub">${o.sub}</div></div><div class="picker-radio">${String(o.value)===String(current)?'<svg viewBox="0 0 24 24" fill="var(--gold)"><circle cx="12" cy="12" r="8"/></svg>':''}</div></div>`).join('')}</div></div>`;
  sheet.onclick=e=>{if(e.target===sheet)sheet.remove();};
  document.body.appendChild(sheet);
  window._pickerCurrentOptions=options;window._pickerOnSelect=onSelect;
}
window._pickerSelect=function(v){const m=(window._pickerCurrentOptions||[]).find(o=>String(o.value)===String(v));window._pickerOnSelect(m?m.value:v);document.getElementById('settings-picker-sheet')?.remove();};

// ─── TOGGLES ──────────────────────────────────────────────────────────────────
function toggleDataSaver(e){saveSetting('dataSaver',e);if(typeof showToast==='function')showToast(e?'Data Saver on · 128kbps':'Data Saver off · Full quality');renderSettingsPage();}
function toggleCrossfade(e){saveSetting('crossfade',e);const el=document.getElementById('crossfade-expand');if(el)el.classList.toggle('open',e);}

// ─── SHAKE TO SKIP ────────────────────────────────────────────────────────────
let _shakeLastTime=0;
function toggleShakeToSkip(enabled){
  if(enabled&&typeof DeviceMotionEvent!=='undefined'&&typeof DeviceMotionEvent.requestPermission==='function'){
    DeviceMotionEvent.requestPermission().then(s=>{
      if(s==='granted'){saveSetting('shakeToSkip',true);if(typeof showToast==='function')showToast('Shake to skip · Enabled');renderSettingsPage();}
      else{if(typeof showToast==='function')showToast('Motion permission denied');renderSettingsPage();}
    }).catch(()=>{if(typeof showToast==='function')showToast('Permission error');});
  }else{
    saveSetting('shakeToSkip',enabled);
    if(typeof showToast==='function')showToast(enabled?'Shake to skip · On':'Shake to skip · Off');
    renderSettingsPage();
  }
}
window.addEventListener('devicemotion',function(e){
  if(!appSettings.shakeToSkip)return;
  const a=e.accelerationIncludingGravity;if(!a)return;
  const f=Math.abs(a.x||0)+Math.abs(a.y||0)+Math.abs(a.z||0);
  const n=Date.now();
  if(f>30&&n-_shakeLastTime>1500){_shakeLastTime=n;if(typeof nextTrack==='function')nextTrack();if(typeof showToast==='function')showToast('↩ Skipped');if(typeof haptic==='function')haptic([20,40,20]);}
});

// ─── CLEAR ────────────────────────────────────────────────────────────────────
function confirmClearSearch(){if(!confirm('Clear all search history?'))return;localStorage.removeItem('aurum_recent');if(typeof recentSearches!=='undefined')recentSearches=[];if(typeof showToast==='function')showToast('Search history cleared');}
function confirmClearCache(){
  if(!confirm('Remove all downloaded songs? Cannot be undone.'))return;
  if(typeof openDlDb==='function'){openDlDb().then(db=>{const tx=db.transaction('songs','readwrite');tx.objectStore('songs').clear();tx.oncomplete=()=>{localStorage.removeItem('aurum_dl_meta');if(typeof renderLibrary==='function')renderLibrary();renderSettingsPage();if(typeof showToast==='function')showToast('Downloads cleared');};});}
}
function confirmClearAllData(){
  if(!confirm('This will reset ALL app data — playlists, liked songs, history. Are you sure?'))return;
  const keep=['aurum_settings'];
  Object.keys(localStorage).filter(k=>k.startsWith('aurum_')&&!keep.includes(k)).forEach(k=>localStorage.removeItem(k));
  if(typeof openDlDb==='function')openDlDb().then(db=>{const tx=db.transaction('songs','readwrite');tx.objectStore('songs').clear();}).catch(()=>{});
  if(typeof savedSongs!=='undefined')savedSongs=[];
  if(typeof playlists!=='undefined')playlists=[];
  if(typeof recentlyPlayed!=='undefined')recentlyPlayed=[];
  if(typeof recentSearches!=='undefined')recentSearches=[];
  if(typeof renderLibrary==='function')renderLibrary();
  if(typeof showToast==='function')showToast('All data cleared');
}

// ─── INIT ─────────────────────────────────────────────────────────────────────
applySettings();
setTimeout(_patchAppFunctions, 0);
