// ═══════════════════════════════════════════════════════════════
// AURUM — auth.js  (Production-Hardened v2)
// Google OAuth + Premium Feature Guards + Sync + AI Suggestions
// ═══════════════════════════════════════════════════════════════

'use strict';

// ─── 1. GLOBAL AUTH STATE ────────────────────────────────────────────────────
window.userAuth = {
  isLoggedIn : false,
  user       : null,   // { name, email, picture, sub }
  token      : null,   // Google credential JWT (for backend calls)
};

const FREE_QUEUE_LIMIT = 5;

// ─── 2. BOOT — restore session from localStorage ─────────────────────────────
// SESSION EXPIRE GUARD: parse JWT iat, flush if > 7 days stale
(function _restoreSession() {
  try {
    const saved = localStorage.getItem('aurum_user');
    if (!saved) return;
    const u = JSON.parse(saved);
    if (!u?.email) return;

    // ── JWT expiry check ──────────────────────────────────────
    const rawToken = localStorage.getItem('aurum_raw_token');
    if (rawToken) {
      try {
        const parts   = rawToken.split('.');
        const pad     = s => s + '='.repeat((4 - s.length % 4) % 4);
        const payload = JSON.parse(atob(pad(parts[1].replace(/-/g, '+').replace(/_/g, '/'))));
        const issuedAt = payload.iat; // seconds epoch
        if (issuedAt) {
          const ageMs = Date.now() - issuedAt * 1000;
          // ✅ FIX: 24h → 7 days (was causing 1-second logout on page load)
          if (ageMs > 7 * 24 * 60 * 60 * 1000) {
            window.signOutUser();
            return;
          }
        }
      } catch (_) { /* malformed token — continue */ }
    }

    window.userAuth.isLoggedIn = true;
    window.userAuth.user       = u;
    _applyLoggedInUI(u);
    // Fetch cloud state silently on boot (for TV handshake)
    setTimeout(_fetchAndApplyCloudState, 1200);
  } catch(e) {}
})();

// ─── 3. GOOGLE CREDENTIAL CALLBACK ──────────────────────────────────────────
window.handleGoogleCredential = function(response) {
  try {
    const parts   = response.credential.split('.');
    const pad     = s => s + '='.repeat((4 - s.length % 4) % 4);
    const payload = JSON.parse(atob(pad(parts[1].replace(/-/g, '+').replace(/_/g, '/'))));
    const user = {
      name    : payload.name    || 'User',
      email   : payload.email   || '',
      picture : payload.picture || '',
      sub     : payload.sub     || '',
    };

    window.userAuth.isLoggedIn = true;
    window.userAuth.user       = user;
    window.userAuth.token      = response.credential;

    localStorage.setItem('aurum_user', JSON.stringify(user));
    localStorage.setItem('aurum_raw_token', response.credential);

    _applyLoggedInUI(user);
    closeLoginModal();
    showToast('Welcome, ' + user.name.split(' ')[0] + ' ✓');
    haptic([20, 50, 20]);

    _sendTokenToBackend(response.credential);
    setTimeout(_fetchAndApplyCloudState, 800);

  } catch(e) {
    showToast('Sign-in failed — try again');
    console.error('[Aurum Auth]', e);
  }
};

// ─── 4. APPLY LOGGED-IN UI ───────────────────────────────────────────────────
function _applyLoggedInUI(user) {
  const chip   = document.getElementById('aurum-user-chip');
  const avatar = document.getElementById('aurum-user-avatar');
  if (chip)   chip.style.display = 'flex';
  if (avatar && user.picture) avatar.src = user.picture;

  const mAvatar = document.getElementById('aurum-menu-avatar');
  const mName   = document.getElementById('aurum-menu-name');
  const mEmail  = document.getElementById('aurum-menu-email');
  if (mAvatar && user.picture) mAvatar.src = user.picture;
  if (mName)  mName.textContent  = user.name  || 'User';
  if (mEmail) mEmail.textContent = user.email || '';
}

// ─── 5. SIGN OUT ─────────────────────────────────────────────────────────────
window.signOutUser = function() {
  window.userAuth.isLoggedIn = false;
  window.userAuth.user       = null;
  window.userAuth.token      = null;
  localStorage.removeItem('aurum_user');
  localStorage.removeItem('aurum_raw_token');

  const chip = document.getElementById('aurum-user-chip');
  if (chip) chip.style.display = 'none';
  closeUserMenu();

  try { google.accounts.id.disableAutoSelect(); } catch(e) {}
  showToast('Signed out');
};

// ─── 6. DETERMINISTIC FEATURE VALIDATOR ─────────────────────────────────────
window.validateFeature = function(featureName) {
  if (window.userAuth.isLoggedIn) return true;

  const headlines = {
    'ringtone' : 'Download Ringtone',
    'queue'    : 'Unlimited Queue',
    'sync'     : 'Cross-Device Sync',
    'download' : 'Full Song Download',
    'ai'       : 'Aurum AI Suggestions',
    'default'  : 'Premium Feature',
  };
  const descs = {
    'ringtone' : 'Sign in to export & download ringtones instantly.',
    'queue'    : 'Free users can queue up to 5 songs. Sign in for unlimited.',
    'sync'     : 'Sign in to sync your music across Mobile & TV.',
    'download' : 'Sign in to download full songs.',
    'ai'       : 'Sign in to unlock personalised AI music picks.',
    'default'  : 'Sign in to unlock all premium features.',
  };

  const key      = headlines[featureName] ? featureName : 'default';
  const headline = document.getElementById('aurum-login-headline');
  const desc     = document.getElementById('aurum-login-desc');
  if (headline) headline.textContent = headlines[key];
  if (desc)     desc.textContent     = descs[key];

  openLoginModal();
  haptic(15);
  showToast('Premium feature: Please sign in');
  return false;
};

// Keep legacy alias
window.checkFeatureAccess = window.validateFeature;

// ─── 7. LOGIN MODAL OPEN/CLOSE ───────────────────────────────────────────────
window.openLoginModal = function() {
  const overlay = document.getElementById('aurum-login-overlay');
  if (!overlay) return;
  overlay.style.display = 'flex';
  requestAnimationFrame(() => {
    requestAnimationFrame(() => overlay.classList.add('open'));
  });
};

window.closeLoginModal = function() {
  const overlay = document.getElementById('aurum-login-overlay');
  if (!overlay) return;
  overlay.classList.remove('open');
  setTimeout(() => { overlay.style.display = 'none'; }, 400);
};

document.addEventListener('click', function(e) {
  const overlay = document.getElementById('aurum-login-overlay');
  if (overlay && overlay.style.display !== 'none') {
    if (!e.target.closest('.aurum-login-sheet')) closeLoginModal();
  }
});

// ─── 8. USER MENU OPEN/CLOSE ─────────────────────────────────────────────────
window.openUserMenu = function() {
  const menu = document.getElementById('aurum-user-menu');
  if (!menu) return;
  menu.style.display = 'flex';
  requestAnimationFrame(() => {
    requestAnimationFrame(() => menu.classList.add('open'));
  });
};

window.closeUserMenu = function() {
  const menu = document.getElementById('aurum-user-menu');
  if (!menu) return;
  menu.classList.remove('open');
  setTimeout(() => { menu.style.display = 'none'; }, 400);
};

document.addEventListener('click', function(e) {
  const menu = document.getElementById('aurum-user-menu');
  if (menu && menu.style.display !== 'none') {
    if (!e.target.closest('.aurum-user-menu-sheet') && !e.target.closest('#aurum-user-chip')) {
      closeUserMenu();
    }
  }
});

// ─── 9. HARD QUEUE INTERCEPT ─────────────────────────────────────────────────
(function _installSetQueueInterceptor() {
  let _originalSetQueue = window.setQueue || null;

  Object.defineProperty(window, 'setQueue', {
    configurable: true,
    get: function() {
      return function(newQueue) {
        if (!window.userAuth.isLoggedIn && Array.isArray(newQueue) && newQueue.length > FREE_QUEUE_LIMIT) {
          newQueue = newQueue.slice(0, FREE_QUEUE_LIMIT);
          showToast('Free tier: Queue limited to 5 songs');
        }
        if (_originalSetQueue) return _originalSetQueue(newQueue);
        if (typeof window.currentQueue !== 'undefined') window.currentQueue = newQueue;
      };
    },
    set: function(fn) {
      _originalSetQueue = fn;
    },
  });
})();

// ─── 10. QUEUE GUARD ─────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', function() {
  setTimeout(_installQueueGuard, 300);
});

function _installQueueGuard() {
  const _origFetchRec = window.fetchRecommendations;
  window.fetchRecommendations = async function(song) {
    if (window.userAuth.isLoggedIn) {
      return _origFetchRec ? _origFetchRec(song) : undefined;
    }
    const before = window.currentQueue ? window.currentQueue.length : 0;
    if (before >= FREE_QUEUE_LIMIT) return;
    if (_origFetchRec) await _origFetchRec(song);
    _trimQueueToLimit();
  };

  const _origPlayNext = window.playNext;
  window.playNext = function() {
    if (!window.userAuth.isLoggedIn) {
      const qLen = window.currentQueue ? window.currentQueue.length : 0;
      if (qLen >= FREE_QUEUE_LIMIT) {
        window.validateFeature('queue');
        return;
      }
    }
    if (_origPlayNext) _origPlayNext();
  };
}

function _trimQueueToLimit() {
  if (!window.currentQueue || window.userAuth.isLoggedIn) return;
  if (window.currentQueue.length > FREE_QUEUE_LIMIT) {
    const idx = window.currentIndex || 0;
    window.currentQueue = window.currentQueue.slice(0, idx + FREE_QUEUE_LIMIT);
  }
}

// ─── 11. DOWNLOAD GATE ───────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', function() {
  setTimeout(_installDownloadGuard, 400);
});

function _installDownloadGuard() {
  const _origTrigger = window.triggerDownload;
  window.triggerDownload = async function(quality) {
    if (quality === 'ringtone') {
      if (!window.validateFeature('ringtone')) return;
    }
    if ((quality === 'full' || quality === 'gift')) {
      if (!window.validateFeature('download')) return;
    }
    if (_origTrigger) return _origTrigger(quality);
  };
}

// ─── 12. CLOUD SYNC WITH EXPONENTIAL RETRY ───────────────────────────────────
const _SYNC_BUFFER_KEY    = 'aurum_sync_buffer';
const _SYNC_MAX_RETRIES   = 3;
const _SYNC_RETRY_DELAYS  = [2000, 4000, 8000];

window.syncStateToCloud = async function() {
  if (!window.userAuth.isLoggedIn || !window.userAuth.user) return;

  const state = {
    userId    : window.userAuth.user.sub,
    songId    : window.currentTrack?.trackId    || null,
    songTitle : window.currentTrack?.trackName  || null,
    artist    : window.currentTrack?.artistName || null,
    artUrl    : window.currentTrack?.artworkUrl100 || null,
    progress  : window.audio ? Math.floor(window.audio.currentTime) : 0,
    timestamp : Date.now(),
    device    : window.__IS_TV__ ? 'tv' : 'mobile',
  };

  let attempt = 0;

  const _attempt = async () => {
    try {
      const res = await fetch('/api/sync/state', {
        method  : 'POST',
        headers : {
          'Content-Type'  : 'application/json',
          'Authorization' : 'Bearer ' + (window.userAuth.token || ''),
        },
        body: JSON.stringify(state),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      localStorage.removeItem(_SYNC_BUFFER_KEY);
    } catch(e) {
      attempt++;
      if (attempt < _SYNC_MAX_RETRIES) {
        console.warn(`[Aurum Sync] Retry ${attempt}/${_SYNC_MAX_RETRIES} in ${_SYNC_RETRY_DELAYS[attempt - 1]}ms`);
        setTimeout(_attempt, _SYNC_RETRY_DELAYS[attempt - 1]);
      } else {
        console.warn('[Aurum Sync] All retries failed — buffering payload');
        try {
          localStorage.setItem(_SYNC_BUFFER_KEY, JSON.stringify(state));
        } catch(_) {}
      }
    }
  };

  // Flush buffered payload first
  const buffered = localStorage.getItem(_SYNC_BUFFER_KEY);
  if (buffered) {
    try {
      await fetch('/api/sync/state', {
        method  : 'POST',
        headers : {
          'Content-Type'  : 'application/json',
          'Authorization' : 'Bearer ' + (window.userAuth.token || ''),
        },
        body: buffered,
      });
      localStorage.removeItem(_SYNC_BUFFER_KEY);
    } catch(_) {}
  }

  await _attempt();
};

// ─── 13. CLOUD SYNC — fetch state on TV / new device ─────────────────────────
async function _fetchAndApplyCloudState() {
  if (!window.userAuth.isLoggedIn) return;
  try {
    const r = await fetch('/api/sync/state', {
      headers: { 'Authorization': 'Bearer ' + (window.userAuth.token || '') },
    });
    if (!r.ok) return;
    const data = await r.json();
    if (!data?.songId) return;

    const isTV        = !!window.__IS_TV__;
    const savedDevice = data.device;

    if (isTV && savedDevice === 'mobile') { _resumeFromCloudState(data); }
    else if (!isTV && savedDevice === 'tv') { _resumeFromCloudState(data); }
  } catch(e) {
    console.warn('[Aurum Sync] Could not fetch state:', e.message);
  }
}

function _resumeFromCloudState(data) {
  const song = {
    trackId      : data.songId,
    trackName    : data.songTitle  || 'Unknown',
    artistName   : data.artist     || '',
    artworkUrl100: data.artUrl     || '',
    previewUrl   : null,
    _syncedAt    : data.progress   || 0,
  };
  const fromDevice = data.device === 'tv' ? 'TV' : 'Mobile';
  const mins  = Math.floor((data.progress || 0) / 60);
  const secs  = String((data.progress || 0) % 60).padStart(2, '0');
  showToast('▶ Continue from ' + fromDevice + ' at ' + mins + ':' + secs + '?');
  window._pendingCloudResume = { song, progress: data.progress || 0 };

  const _origLoad = window.loadTrack;
  window.loadTrack = function(s, autoplay) {
    if (_origLoad) _origLoad(s, autoplay);
    if (window._pendingCloudResume && String(s?.trackId) === String(data.songId)) {
      const target = window._pendingCloudResume.progress;
      setTimeout(() => {
        if (window.audio && isFinite(window.audio.duration) && target > 0) {
          window.audio.currentTime = target;
        }
      }, 1200);
      window._pendingCloudResume = null;
      window.loadTrack = _origLoad;
    }
  };
}

// ─── 14. AUTO-SYNC EVERY 30s while playing ───────────────────────────────────
setInterval(function() {
  if (window.userAuth.isLoggedIn && window.isPlaying) {
    window.syncStateToCloud();
  }
}, 30000);

// ─── 15. AURUM AI SUGGESTIONS ────────────────────────────────────────────────
window.aurumAI = (function() {
  const AI_CONTAINER_ID = 'aurum-ai-suggestions';

  async function _fetchAISuggestions(contextSong) {
    const res = await fetch('/api/ai/suggestions', {
      method  : 'POST',
      headers : {
        'Content-Type'  : 'application/json',
        'Authorization' : 'Bearer ' + (window.userAuth.token || ''),
      },
      body: JSON.stringify({
        userId   : window.userAuth.user?.sub || null,
        email    : window.userAuth.user?.email || null,
        track    : contextSong?.trackName  || null,
        artist   : contextSong?.artistName || null,
        history  : (window.currentQueue || []).slice(0, 10).map(s => ({
          t: s.trackName, a: s.artistName
        })),
        timestamp: Date.now(),
      }),
    });
    if (!res.ok) throw new Error('AI API ' + res.status);
    return res.json();
  }

  async function render(targetEl, contextSong) {
    if (!window.validateFeature('ai')) return;

    targetEl = targetEl || document.getElementById(AI_CONTAINER_ID);
    if (!targetEl) return;

    targetEl.innerHTML = `
      <div class="aurum-ai-header">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
        </svg>
        <span>Aurum AI <span class="aurum-ai-badge">For You</span></span>
      </div>
      <div class="aurum-ai-loading">
        <span class="aurum-ai-dot"></span><span class="aurum-ai-dot"></span><span class="aurum-ai-dot"></span>
      </div>`;

    try {
      const data  = await _fetchAISuggestions(contextSong);
      const songs = data?.suggestions || [];

      if (!songs.length) {
        targetEl.innerHTML += '<p class="aurum-ai-empty">No picks right now — keep listening!</p>';
        return;
      }

      const list = document.createElement('div');
      list.className = 'aurum-ai-list';
      songs.forEach((s, i) => {
        const card = document.createElement('div');
        card.className = 'aurum-ai-card';
        card.innerHTML = `
          <div class="aurum-ai-rank">${i + 1}</div>
          <div class="aurum-ai-info">
            <div class="aurum-ai-title">${_esc(s.trackName)}</div>
            <div class="aurum-ai-artist">${_esc(s.artistName)}</div>
            <div class="aurum-ai-reason">${_esc(s.reason || '')}</div>
          </div>
          <button class="aurum-ai-play" onclick="window.aurumAI.playSuggestion(${i})" aria-label="Play">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          </button>`;
        list.appendChild(card);
      });

      targetEl.querySelector('.aurum-ai-loading')?.remove();
      targetEl.appendChild(list);
      window.aurumAI._lastSuggestions = songs;

    } catch(e) {
      targetEl.querySelector('.aurum-ai-loading')?.remove();
      targetEl.innerHTML += '<p class="aurum-ai-empty">Couldn\'t load picks — try again</p>';
      console.warn('[Aurum AI]', e);
    }
  }

  function playSuggestion(index) {
    if (!window.validateFeature('ai')) return;
    const songs = window.aurumAI._lastSuggestions || [];
    if (!songs[index]) return;
    const s = songs[index];
    if (window.playSongs) window.playSongs([s], 0);
  }

  function togglePanel() {
    if (!window.validateFeature('ai')) return;
    const panel = document.getElementById('aurum-ai-panel');
    if (!panel) return;
    const isOpen = panel.classList.contains('open');
    if (isOpen) {
      panel.classList.remove('open');
    } else {
      const body = document.getElementById('aurum-ai-panel-body');
      if (body && !body._loaded) {
        body._loaded = true;
        render(body, window.currentTrack || null);
      }
      panel.classList.add('open');
    }
  }

  function _esc(s) {
    return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  return { render, playSuggestion, togglePanel, _lastSuggestions: [] };
})();

// ─── 16. SEND TOKEN TO BACKEND ───────────────────────────────────────────────
async function _sendTokenToBackend(credential) {
  try {
    await fetch('/api/auth/google', {
      method  : 'POST',
      headers : { 'Content-Type': 'application/json' },
      body    : JSON.stringify({ credential }),
    });
  } catch(e) {}
}

// ─── 17. KEYBOARD SHORTCUT: Escape ───────────────────────────────────────────
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const overlay = document.getElementById('aurum-login-overlay');
    if (overlay && overlay.style.display !== 'none') closeLoginModal();
    const menu = document.getElementById('aurum-user-menu');
    if (menu && menu.style.display !== 'none') closeUserMenu();
  }
});
