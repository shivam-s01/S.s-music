// ─── SETTINGS SYSTEM ──────────────────────────────────────────────────────────
// Default settings
const DEFAULT_SETTINGS = {
  theme: 'dark',           // 'dark' | 'light' | 'amoled'
  dataSaver: false,        // true = low quality stream
  streamQuality: 'auto',   // 'auto' | 'high' | 'low'
  animations: true,
  dynamicColor: true,
  showVisualizer: true,
  language: 'en',
  crossfade: false,
  gaplessPlayback: false,
};

let appSettings = { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem('aurum_settings') || '{}') };

function saveSetting(key, value) {
  appSettings[key] = value;
  localStorage.setItem('aurum_settings', JSON.stringify(appSettings));
  applySettings();
}

function applySettings() {
  const root = document.documentElement;

  // Theme
  root.classList.remove('theme-light', 'theme-amoled', 'theme-dark');
  root.classList.add('theme-' + appSettings.theme);

  // AMOLED override
  if (appSettings.theme === 'amoled') {
    root.style.setProperty('--bg', '#000000');
    root.style.setProperty('--surface', '#000000');
    root.style.setProperty('--surface2', '#0a0a0a');
    root.style.setProperty('--surface3', '#111111');
  } else if (appSettings.theme === 'light') {
    root.style.setProperty('--bg', '#f5f2ed');
    root.style.setProperty('--surface', '#ffffff');
    root.style.setProperty('--surface2', '#f0ece4');
    root.style.setProperty('--surface3', '#e8e2d8');
    root.style.setProperty('--text', '#1a1814');
    root.style.setProperty('--text2', '#4a4540');
    root.style.setProperty('--text3', '#8a8278');
  } else {
    // Reset to dark defaults
    root.style.removeProperty('--bg');
    root.style.removeProperty('--surface');
    root.style.removeProperty('--surface2');
    root.style.removeProperty('--surface3');
    root.style.removeProperty('--text');
    root.style.removeProperty('--text2');
    root.style.removeProperty('--text3');
  }

  // Animations
  if (!appSettings.animations) {
    root.style.setProperty('--anim-speed', '0s');
  } else {
    root.style.removeProperty('--anim-speed');
  }

  // Visualizer
  const viz = document.getElementById('fp-visualizer');
  if (viz) viz.style.display = appSettings.showVisualizer ? '' : 'none';

  // Dynamic color (ambient)
  if (!appSettings.dynamicColor) {
    const glow = document.getElementById('fp-ambient-glow');
    if (glow) glow.style.display = 'none';
  } else {
    const glow = document.getElementById('fp-ambient-glow');
    if (glow) glow.style.display = '';
  }
}

// ─── DATA SAVER: patch _autoFetchFullSong to respect quality setting ───────────
// We override the fetch URL to pass low_quality param when data saver is on
const _origAutoFetch = _autoFetchFullSong;
window._getQualityParam = function() {
  if (appSettings.dataSaver) return '&low_quality=true';
  if (appSettings.streamQuality === 'low') return '&low_quality=true';
  return '';
};

// Patch: intercept saavn API calls to add quality param
const _origFetch = window.fetch;
window.fetch = function(url, opts) {
  if (typeof url === 'string' && url.includes('/api/saavn?')) {
    url = url + window._getQualityParam();
  }
  return _origFetch.call(this, url, opts);
};

// ─── SETTINGS UI ──────────────────────────────────────────────────────────────
function openSettings() {
  renderSettingsPage();
  document.getElementById('settings-panel').classList.add('open');
  haptic(10);
}

function closeSettings() {
  document.getElementById('settings-panel').classList.remove('open');
}

function renderSettingsPage() {
  const body = document.getElementById('settings-body');
  const s = appSettings;

  body.innerHTML = `
    <!-- PLAYBACK -->
    <div class="settings-section">
      <div class="settings-section-label">Playback</div>

      <div class="settings-item" onclick="openStreamQualityPicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Stream Quality</div>
            <div class="settings-item-sub">Value: ${s.streamQuality === 'auto' ? 'Auto (Best)' : s.streamQuality === 'high' ? 'High (320kbps)' : 'Low (128kbps)'}</div>
          </div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon data-saver-icon ${s.dataSaver ? 'active' : ''}">
            <svg viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Data Saver</div>
            <div class="settings-item-sub">Streams at 128kbps instead of 320kbps</div>
          </div>
        </div>
        <label class="settings-toggle">
          <input type="checkbox" ${s.dataSaver ? 'checked' : ''} onchange="toggleDataSaver(this.checked)">
          <span class="settings-toggle-track"></span>
        </label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Gapless Playback</div>
            <div class="settings-item-sub">Removes silence between tracks</div>
          </div>
        </div>
        <label class="settings-toggle">
          <input type="checkbox" ${s.gaplessPlayback ? 'checked' : ''} onchange="saveSetting('gaplessPlayback', this.checked)">
          <span class="settings-toggle-track"></span>
        </label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Crossfade</div>
            <div class="settings-item-sub">Smooth transition between songs</div>
          </div>
        </div>
        <label class="settings-toggle">
          <input type="checkbox" ${s.crossfade ? 'checked' : ''} onchange="saveSetting('crossfade', this.checked)">
          <span class="settings-toggle-track"></span>
        </label>
      </div>
    </div>

    <!-- LOOK & FEEL -->
    <div class="settings-section">
      <div class="settings-section-label">Look & Feel</div>

      <div class="settings-item" onclick="openThemePicker()">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 0 1 0 20"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Theme</div>
            <div class="settings-item-sub">Value: ${s.theme === 'dark' ? 'Dark' : s.theme === 'light' ? 'Light' : 'AMOLED Black'}</div>
          </div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><circle cx="13.5" cy="6.5" r="2.5"/><circle cx="19" cy="11" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/><circle cx="8.5" cy="7.5" r="2.5"/><circle cx="6.5" cy="12" r="2.5"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Dynamic Color</div>
            <div class="settings-item-sub">Player color adapts to track artwork</div>
          </div>
        </div>
        <label class="settings-toggle">
          <input type="checkbox" ${s.dynamicColor ? 'checked' : ''} onchange="saveSetting('dynamicColor', this.checked)">
          <span class="settings-toggle-track"></span>
        </label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Visualizer</div>
            <div class="settings-item-sub">Animated bars in fullscreen player</div>
          </div>
        </div>
        <label class="settings-toggle">
          <input type="checkbox" ${s.showVisualizer ? 'checked' : ''} onchange="saveSetting('showVisualizer', this.checked)">
          <span class="settings-toggle-track"></span>
        </label>
      </div>

      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Animations</div>
            <div class="settings-item-sub">Turning off may improve performance</div>
          </div>
        </div>
        <label class="settings-toggle">
          <input type="checkbox" ${s.animations ? 'checked' : ''} onchange="saveSetting('animations', this.checked)">
          <span class="settings-toggle-track"></span>
        </label>
      </div>
    </div>

    <!-- STORAGE -->
    <div class="settings-section">
      <div class="settings-section-label">Storage</div>

      <div class="settings-item" id="storage-info-item">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><path d="M22 12H2"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Downloaded Songs</div>
            <div class="settings-item-sub" id="storage-count-text">Calculating…</div>
          </div>
        </div>
      </div>

      <div class="settings-item danger-item" onclick="confirmClearCache()">
        <div class="settings-item-left">
          <div class="settings-item-icon danger-icon">
            <svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title danger-text">Clear Downloads</div>
            <div class="settings-item-sub">Remove all offline saved songs</div>
          </div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>

      <div class="settings-item danger-item" onclick="confirmClearAllData()">
        <div class="settings-item-left">
          <div class="settings-item-icon danger-icon">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title danger-text">Clear All Data</div>
            <div class="settings-item-sub">Resets app — playlists, likes, history</div>
          </div>
        </div>
        <svg class="settings-chevron" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
      </div>
    </div>

    <!-- ABOUT -->
    <div class="settings-section">
      <div class="settings-section-label">About</div>
      <div class="settings-item">
        <div class="settings-item-left">
          <div class="settings-item-icon">
            <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          </div>
          <div class="settings-item-info">
            <div class="settings-item-title">Aurum</div>
            <div class="settings-item-sub">Version 1.0 · Made with ♪</div>
          </div>
        </div>
      </div>
    </div>
  `;

  // Calculate storage usage
  _calcStorageInfo();
}

async function _calcStorageInfo() {
  const metas = JSON.parse(localStorage.getItem('aurum_dl_meta') || '[]');
  const el = document.getElementById('storage-count-text');
  if (!el) return;
  if (!metas.length) { el.textContent = 'No downloads'; return; }
  el.textContent = `${metas.length} song${metas.length !== 1 ? 's' : ''} saved offline`;
  try {
    if ('storage' in navigator && 'estimate' in navigator.storage) {
      const { usage } = await navigator.storage.estimate();
      if (usage) el.textContent += ` · ${(usage / 1024 / 1024).toFixed(1)} MB used`;
    }
  } catch(e) {}
}

// ─── THEME PICKER ─────────────────────────────────────────────────────────────
function openThemePicker() {
  const current = appSettings.theme;
  const options = [
    { value: 'dark',   label: 'Dark',        sub: 'Default dark background' },
    { value: 'amoled', label: 'AMOLED Black', sub: 'Pure black · Best for OLED screens' },
    { value: 'light',  label: 'Light',        sub: 'Warm light background' },
  ];
  _openPickerSheet('Theme', options, current, (val) => {
    saveSetting('theme', val);
    renderSettingsPage();
  });
}

// ─── STREAM QUALITY PICKER ────────────────────────────────────────────────────
function openStreamQualityPicker() {
  const current = appSettings.streamQuality;
  const options = [
    { value: 'auto', label: 'Auto',        sub: 'Best quality available (recommended)' },
    { value: 'high', label: 'High',        sub: '320 kbps · Uses more data' },
    { value: 'low',  label: 'Low',         sub: '128 kbps · Saves data' },
  ];
  _openPickerSheet('Stream Quality', options, current, (val) => {
    saveSetting('streamQuality', val);
    renderSettingsPage();
  });
}

function _openPickerSheet(title, options, current, onSelect) {
  // Remove existing picker if any
  const existing = document.getElementById('settings-picker-sheet');
  if (existing) existing.remove();

  const sheet = document.createElement('div');
  sheet.id = 'settings-picker-sheet';
  sheet.className = 'modal-overlay open';

  let optHtml = options.map(o => `
    <div class="picker-option ${o.value === current ? 'selected' : ''}" onclick="_pickerSelect('${o.value}')">
      <div class="picker-option-info">
        <div class="picker-option-label">${o.label}</div>
        <div class="picker-option-sub">${o.sub}</div>
      </div>
      <div class="picker-radio">${o.value === current ? '<svg viewBox="0 0 24 24" fill="var(--gold)"><circle cx="12" cy="12" r="8"/></svg>' : ''}</div>
    </div>
  `).join('');

  sheet.innerHTML = `
    <div class="modal-sheet picker-sheet">
      <div class="modal-handle"></div>
      <div class="picker-title">${title}</div>
      <div id="picker-options">${optHtml}</div>
    </div>
  `;

  sheet.onclick = (e) => { if (e.target === sheet) sheet.remove(); };
  document.body.appendChild(sheet);

  window._pickerCurrentOptions = options;
  window._pickerOnSelect = onSelect;
}

window._pickerSelect = function(val) {
  window._pickerOnSelect(val);
  const sheet = document.getElementById('settings-picker-sheet');
  if (sheet) sheet.remove();
};

// ─── DATA SAVER TOGGLE ────────────────────────────────────────────────────────
function toggleDataSaver(enabled) {
  saveSetting('dataSaver', enabled);
  // Update icon style
  const icon = document.querySelector('.data-saver-icon');
  if (icon) icon.classList.toggle('active', enabled);
  showToast(enabled ? 'Data Saver on · 128kbps' : 'Data Saver off · Full quality');
  renderSettingsPage();
}

// ─── CLEAR DATA ───────────────────────────────────────────────────────────────
function confirmClearCache() {
  if (!confirm('Remove all downloaded songs? This cannot be undone.')) return;
  openDlDb().then(db => {
    const tx = db.transaction('songs', 'readwrite');
    tx.objectStore('songs').clear();
    tx.oncomplete = () => {
      localStorage.removeItem('aurum_dl_meta');
      renderLibrary();
      renderSettingsPage();
      showToast('Downloads cleared');
    };
  });
}

function confirmClearAllData() {
  if (!confirm('This will reset ALL app data — playlists, liked songs, history. Are you sure?')) return;
  const keysToKeep = ['aurum_settings'];
  const keys = Object.keys(localStorage).filter(k => k.startsWith('aurum_') && !keysToKeep.includes(k));
  keys.forEach(k => localStorage.removeItem(k));
  // Clear IndexedDB too
  openDlDb().then(db => { const tx = db.transaction('songs', 'readwrite'); tx.objectStore('songs').clear(); }).catch(() => {});
  // Reset in-memory state
  savedSongs = []; playlists = []; recentlyPlayed = []; recentSearches = [];
  renderLibrary();
  showToast('All data cleared');
}

// ─── INIT SETTINGS ────────────────────────────────────────────────────────────
applySettings();
