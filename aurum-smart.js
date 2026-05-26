// ════════════════════════════════════════════════════════════════════════════
// AURUM SMART FEATURES v1.0
// Requires: window.userAuth.isLoggedIn, _listenHistory, currentTrack,
//           showToast, haptic, playSongs, openFullscreen (from app.js)
// ════════════════════════════════════════════════════════════════════════════

(function() {
  'use strict';

  // ── Guard: only run when logged in ────────────────────────────────────────
  function _isLoggedIn() {
    return !!(window.userAuth && window.userAuth.isLoggedIn);
  }

  // ── Constants ─────────────────────────────────────────────────────────────
  const SKIP_GUARD_THRESHOLD = 3;
  const SKIP_GUARD_WINDOW_MS = 5 * 60 * 1000; // 5 minutes
  const NIGHT_VOLUME_HOUR    = 23;             // 11 PM
  const NIGHT_VOLUME_LEVEL   = 0.4;
  const MAX_AI_RESULTS       = 12;
  const BADGE_STORAGE_KEY    = 'aurum_play_counts';

  // ── State ─────────────────────────────────────────────────────────────────
  let _skipTimestamps     = [];
  let _skipGuardShown     = false;
  let _sleepTimerEnd      = null;
  let _sleepTimerRafId    = null;
  let _sleepFading        = false;
  let _moodActive         = null;
  let _playCounts         = {};
  let _nightVolumeApplied = false;
  let _smartInited        = false;

  // Load play counts from storage
  try { _playCounts = JSON.parse(localStorage.getItem(BADGE_STORAGE_KEY) || '{}'); } catch(e) { _playCounts = {}; }

  // ─────────────────────────────────────────────────────────────────────────
  // 1. CSS INJECTION
  // ─────────────────────────────────────────────────────────────────────────
  function _injectStyles() {
    if (document.getElementById('aurum-smart-css')) return;
    const s = document.createElement('style');
    s.id = 'aurum-smart-css';
    s.textContent = `
      /* ── Play count badge ── */
      .song-row[data-play-count]::after,
      .quick-card[data-play-count]::after,
      .wide-card[data-play-count]::after {
        display: none; /* hidden; shown by JS via explicit span */
      }
      .aurum-play-badge {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        font-family: 'Sora', sans-serif;
        font-size: 8.5px;
        font-weight: 700;
        letter-spacing: 0.04em;
        color: rgba(184,150,64,0.75);
        background: rgba(184,150,64,0.1);
        border: 1px solid rgba(184,150,64,0.2);
        border-radius: 6px;
        padding: 1px 5px;
        margin-left: 5px;
        vertical-align: middle;
        flex-shrink: 0;
      }

      /* ── New song badge ── */
      .aurum-new-badge {
        display: inline-flex;
        align-items: center;
        font-family: 'Sora', sans-serif;
        font-size: 7.5px;
        font-weight: 800;
        letter-spacing: 0.09em;
        text-transform: uppercase;
        color: #050508;
        background: linear-gradient(135deg, #d4b85a, #b89640);
        border-radius: 5px;
        padding: 1.5px 5px;
        margin-left: 5px;
        vertical-align: middle;
        flex-shrink: 0;
      }

      /* ── Mood DJ bar ── */
      #aurum-mood-bar {
        position: absolute;
        bottom: 0; left: 0; right: 0;
        background: linear-gradient(to top, rgba(5,5,8,0.97) 80%, transparent);
        border-radius: 22px 22px 0 0;
        padding: 14px 20px 52px;
        transform: translateY(100%);
        transition: transform 0.38s cubic-bezier(0.33,1,0.68,1);
        will-change: transform;
        z-index: 25;
        -webkit-tap-highlight-color: transparent;
      }
      #aurum-mood-bar.open { transform: translateY(0); }
      .mood-bar-handle {
        width: 32px; height: 4px;
        background: rgba(255,255,255,0.12);
        border-radius: 2px;
        margin: 0 auto 14px;
      }
      .mood-bar-title {
        font-family: 'Sora', sans-serif;
        font-size: 11px; font-weight: 700;
        letter-spacing: 0.12em; text-transform: uppercase;
        color: rgba(255,255,255,0.35);
        text-align: center;
        margin-bottom: 14px;
      }
      .mood-chips-row {
        display: flex;
        gap: 8px;
        justify-content: center;
        flex-wrap: wrap;
      }
      .mood-chip {
        display: flex; align-items: center; gap: 5px;
        font-family: 'Sora', sans-serif;
        font-size: 12px; font-weight: 700;
        background: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.1);
        border-radius: 20px;
        padding: 8px 14px;
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
        transition: background 0.18s ease, border-color 0.18s ease, transform 0.14s ease;
        color: rgba(255,255,255,0.7);
      }
      .mood-chip:active { transform: scale(0.92); }
      .mood-chip.active {
        background: linear-gradient(135deg, rgba(184,150,64,0.22), rgba(184,150,64,0.08));
        border-color: rgba(184,150,64,0.5);
        color: #d4b85a;
      }
      .mood-chip-icon { font-size: 15px; line-height: 1; }

      /* ── Skip guard popup ── */
      #aurum-skip-guard {
        position: absolute;
        bottom: 80px; left: 50%; transform: translateX(-50%) translateY(20px);
        background: rgba(10,9,8,0.94);
        border: 1px solid rgba(184,150,64,0.28);
        border-radius: 18px;
        padding: 14px 18px 16px;
        display: none;
        flex-direction: column;
        align-items: center;
        gap: 10px;
        z-index: 30;
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        box-shadow: 0 8px 40px rgba(0,0,0,0.65);
        max-width: 280px;
        width: calc(100% - 48px);
        opacity: 0;
        transition: opacity 0.3s ease, transform 0.3s cubic-bezier(0.22,1,0.36,1);
      }
      #aurum-skip-guard.visible {
        opacity: 1;
        transform: translateX(-50%) translateY(0);
      }
      .sg-emoji { font-size: 22px; line-height: 1; }
      .sg-text {
        font-family: 'Sora', sans-serif;
        font-size: 12.5px; font-weight: 600;
        color: rgba(255,255,255,0.8);
        text-align: center; line-height: 1.4;
      }
      .sg-mood-row {
        display: flex; gap: 6px; flex-wrap: wrap; justify-content: center;
      }
      .sg-mood-btn {
        font-family: 'Sora', sans-serif;
        font-size: 11px; font-weight: 700;
        background: rgba(184,150,64,0.12);
        border: 1px solid rgba(184,150,64,0.28);
        border-radius: 14px;
        padding: 5px 12px;
        cursor: pointer;
        color: #d4b85a;
        -webkit-tap-highlight-color: transparent;
        transition: background 0.15s ease;
      }
      .sg-mood-btn:active { background: rgba(184,150,64,0.22); }
      .sg-dismiss {
        font-family: 'Sora', sans-serif;
        font-size: 10px; font-weight: 600;
        color: rgba(255,255,255,0.25);
        background: none; border: none; cursor: pointer;
        -webkit-tap-highlight-color: transparent;
        padding: 2px 8px;
      }

      /* ── Sleep timer toast-style pill ── */
      #aurum-sleep-pill {
        position: absolute;
        top: 52px; left: 50%;
        transform: translateX(-50%) translateY(-8px);
        background: rgba(10,9,8,0.9);
        border: 1px solid rgba(184,150,64,0.25);
        border-radius: 20px;
        padding: 6px 14px;
        font-family: 'Sora', sans-serif;
        font-size: 11px; font-weight: 700;
        color: rgba(184,150,64,0.8);
        letter-spacing: 0.04em;
        z-index: 30;
        opacity: 0;
        pointer-events: none;
        transition: opacity 0.3s ease, transform 0.3s ease;
        white-space: nowrap;
        backdrop-filter: blur(12px);
        -webkit-backdrop-filter: blur(12px);
      }
      #aurum-sleep-pill.visible {
        opacity: 1;
        transform: translateX(-50%) translateY(0);
      }

      /* ── AI suggestion strip ── */
      .smart-ai-strip {
        padding: 0 20px 8px;
      }
      .smart-ai-label {
        font-family: 'Sora', sans-serif;
        font-size: 10px; font-weight: 700;
        letter-spacing: 0.14em; text-transform: uppercase;
        color: rgba(184,150,64,0.6);
        margin-bottom: 10px;
        display: flex; align-items: center; gap: 6px;
      }
      .smart-ai-label::before {
        content: '';
        display: inline-block;
        width: 5px; height: 5px; border-radius: 50%;
        background: var(--gold, #b89640);
        box-shadow: 0 0 6px rgba(184,150,64,0.8);
        animation: aiDotPulse 1.6s ease-in-out infinite;
      }
      .smart-ai-songs { display: flex; flex-direction: column; gap: 0; }
      .smart-ai-song-row {
        display: flex; align-items: center; gap: 11px;
        padding: 9px 12px;
        border-radius: 12px;
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
        transition: background 0.16s ease;
        -webkit-tap-highlight-color: transparent;
      }
      .smart-ai-song-row:active { background: rgba(255,255,255,0.05); }
      .smart-ai-song-art {
        width: 38px; height: 38px; border-radius: 8px;
        object-fit: cover; flex-shrink: 0;
        background: var(--surface2, #1a1a22);
      }
      .smart-ai-song-info { flex: 1; min-width: 0; }
      .smart-ai-song-title {
        font-family: 'Sora', sans-serif;
        font-size: 12.5px; font-weight: 600;
        color: rgba(255,255,255,0.88);
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        letter-spacing: -0.1px;
      }
      .smart-ai-song-sub {
        font-family: 'Sora', sans-serif;
        font-size: 10.5px; font-weight: 500;
        color: rgba(255,255,255,0.35);
        margin-top: 1px;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      .smart-ai-reason {
        font-family: 'Sora', sans-serif;
        font-size: 9px; font-weight: 700;
        letter-spacing: 0.06em;
        color: rgba(184,150,64,0.55);
        background: rgba(184,150,64,0.08);
        border: 1px solid rgba(184,150,64,0.15);
        border-radius: 6px;
        padding: 2px 6px;
        flex-shrink: 0;
        white-space: nowrap;
      }

      /* ── Sleep timer settings row ── */
      .sleep-timer-row {
        display: flex; align-items: center; gap: 10px;
        padding: 12px 20px;
        cursor: pointer;
        border-radius: 14px;
        -webkit-tap-highlight-color: transparent;
        transition: background 0.16s ease;
      }
      .sleep-timer-row:active { background: rgba(255,255,255,0.04); }
    `;
    document.head.appendChild(s);
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 2. PLAY COUNT BADGES
  // ─────────────────────────────────────────────────────────────────────────
  function _incrementPlayCount(trackId) {
    const key = String(trackId);
    _playCounts[key] = (_playCounts[key] || 0) + 1;
    try { localStorage.setItem(BADGE_STORAGE_KEY, JSON.stringify(_playCounts)); } catch(e) {}
  }

  function getPlayCount(trackId) {
    return _playCounts[String(trackId)] || 0;
  }

  // Inject badge into a song-row element
  function _injectRowBadge(rowEl, song) {
    if (!rowEl || !song) return;
    const count = getPlayCount(song.trackId);
    const titleEl = rowEl.querySelector('.song-row-title');
    if (!titleEl) return;

    // Remove existing badges
    titleEl.querySelectorAll('.aurum-play-badge, .aurum-new-badge').forEach(b => b.remove());

    if (count === 0) {
      // "New" badge if song has never been played (added in last 7 days? or just always for 0)
      const badge = document.createElement('span');
      badge.className = 'aurum-new-badge';
      badge.textContent = 'New';
      titleEl.appendChild(badge);
    } else if (count >= 5) {
      const badge = document.createElement('span');
      badge.className = 'aurum-play-badge';
      badge.textContent = count >= 50 ? '🔥' : (count >= 20 ? '♥' + count : '▶' + count);
      titleEl.appendChild(badge);
    }
  }

  // Patch the global makeSongRow to inject badges
  function _patchMakeSongRow() {
    const orig = window.makeSongRow;
    if (!orig || orig._smartPatched) return;
    window.makeSongRow = function(s, i, queue) {
      const row = orig(s, i, queue);
      if (row && s) {
        setTimeout(() => _injectRowBadge(row, s), 0);
      }
      return row;
    };
    window.makeSongRow._smartPatched = true;
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 3. SKIP GUARD
  // ─────────────────────────────────────────────────────────────────────────
  function _recordSkip() {
    const now = Date.now();
    _skipTimestamps.push(now);
    // Keep only skips within the window
    _skipTimestamps = _skipTimestamps.filter(t => now - t < SKIP_GUARD_WINDOW_MS);

    if (_skipTimestamps.length >= SKIP_GUARD_THRESHOLD && !_skipGuardShown) {
      _skipGuardShown = true;
      _showSkipGuard();
      // Reset after 2 minutes so it can show again
      setTimeout(() => { _skipGuardShown = false; }, 2 * 60 * 1000);
    }
  }

  function _showSkipGuard() {
    const fp = document.getElementById('fullscreen-player');
    if (!fp || !fp.classList.contains('open')) return;

    let guard = document.getElementById('aurum-skip-guard');
    if (!guard) {
      guard = document.createElement('div');
      guard.id = 'aurum-skip-guard';
      guard.innerHTML = `
        <span class="sg-emoji">🎧</span>
        <div class="sg-text">Skipping a lot? Switch your mood!</div>
        <div class="sg-mood-row">
          <button class="sg-mood-btn" data-mood="chill">😌 Chill</button>
          <button class="sg-mood-btn" data-mood="hype">⚡ Hype</button>
          <button class="sg-mood-btn" data-mood="sad">🌙 Sad</button>
          <button class="sg-mood-btn" data-mood="focus">🎯 Focus</button>
        </div>
        <button class="sg-dismiss" id="sg-dismiss-btn">Keep skipping</button>
      `;
      fp.appendChild(guard);

      guard.querySelectorAll('.sg-mood-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          const mood = btn.dataset.mood;
          _hideSkipGuard();
          _activateMood(mood);
          _skipTimestamps = [];
        });
      });

      const dismissBtn = guard.querySelector('#sg-dismiss-btn');
      if (dismissBtn) {
        dismissBtn.addEventListener('click', () => {
          _hideSkipGuard();
          _skipTimestamps = [];
        });
      }
    }

    guard.style.display = 'flex';
    requestAnimationFrame(() => requestAnimationFrame(() => {
      guard.classList.add('visible');
    }));

    // Auto-hide after 6 seconds
    setTimeout(() => _hideSkipGuard(), 6000);
  }

  function _hideSkipGuard() {
    const guard = document.getElementById('aurum-skip-guard');
    if (!guard) return;
    guard.classList.remove('visible');
    setTimeout(() => { guard.style.display = 'none'; }, 320);
  }

  // Patch nextTrack to count skips
  function _patchNextTrack() {
    const origNext = window.nextTrack;
    if (!origNext || origNext._skipPatched) return;
    window.nextTrack = function() {
      if (_isLoggedIn() && window.audio && !window.audio.ended) {
        // Only count as skip if < 80% played
        const audio = window.audio || window._aurumAudio;
        if (audio) {
          const dur = audio.duration;
          const pct = dur > 0 ? audio.currentTime / dur : 0;
          if (pct < 0.8) _recordSkip();
        }
      }
      return origNext.apply(this, arguments);
    };
    window.nextTrack._skipPatched = true;
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 4. AUTO NIGHT VOLUME
  // ─────────────────────────────────────────────────────────────────────────
  function _checkNightVolume() {
    if (!_isLoggedIn()) return;
    const hour = new Date().getHours();
    const audio = window._aurumAudio || window.audio;
    if (!audio) return;

    if (hour >= NIGHT_VOLUME_HOUR && !_nightVolumeApplied) {
      _nightVolumeApplied = true;
      const prev = audio.volume;
      if (prev > NIGHT_VOLUME_LEVEL + 0.05) {
        // Fade down smoothly over 3 seconds
        const step = (prev - NIGHT_VOLUME_LEVEL) / 30;
        let current = prev;
        const fade = setInterval(() => {
          current = Math.max(NIGHT_VOLUME_LEVEL, current - step);
          audio.volume = current;
          const slider = document.getElementById('fp-vol-slider');
          if (slider) slider.value = current;
          if (current <= NIGHT_VOLUME_LEVEL) {
            clearInterval(fade);
            if (window.showToast) showToast('🌙 Night mode — volume lowered');
          }
        }, 100);
      }
    } else if (hour < NIGHT_VOLUME_HOUR && _nightVolumeApplied) {
      // Reset flag in the morning
      _nightVolumeApplied = false;
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 5. SLEEP TIMER
  // ─────────────────────────────────────────────────────────────────────────
  function _updateSleepPill() {
    if (!_sleepTimerEnd) return;
    const pill = document.getElementById('aurum-sleep-pill');
    if (!pill) return;
    const rem = Math.max(0, Math.ceil((_sleepTimerEnd - Date.now()) / 60000));
    if (rem <= 0) {
      pill.classList.remove('visible');
      return;
    }
    pill.textContent = `⏾ Sleep in ${rem}m`;
    _sleepTimerRafId = requestAnimationFrame(() => {
      setTimeout(_updateSleepPill, 30000);
    });
  }

  function setSleepTimer(minutes) {
    if (!_isLoggedIn()) {
      if (window.showToast) showToast('Sign in to use sleep timer');
      return;
    }

    // Clear existing
    clearSleepTimer();

    if (!minutes || minutes <= 0) {
      if (window.showToast) showToast('Sleep timer off');
      return;
    }

    _sleepTimerEnd = Date.now() + minutes * 60000;

    // Inject pill into fullscreen player
    const fp = document.getElementById('fullscreen-player');
    if (fp && !document.getElementById('aurum-sleep-pill')) {
      const pill = document.createElement('div');
      pill.id = 'aurum-sleep-pill';
      fp.appendChild(pill);
    }

    const pill = document.getElementById('aurum-sleep-pill');
    if (pill) {
      pill.classList.add('visible');
      _updateSleepPill();
    }

    if (window.showToast) showToast(`⏾ Sleep timer: ${minutes} min`);
    if (window.haptic) haptic(15);

    // Actual fade + stop
    _sleepTimerRafId = setTimeout(() => {
      _startSleepFade();
    }, (minutes - 0.5) * 60000); // start fade 30s before
  }

  function _startSleepFade() {
    if (_sleepFading) return;
    _sleepFading = true;
    const audio = window._aurumAudio || window.audio;
    if (!audio) return;
    const startVol = audio.volume;
    const steps = 60;
    const stepMs = 500;
    let i = 0;

    const fade = setInterval(() => {
      i++;
      const vol = Math.max(0, startVol * (1 - i / steps));
      audio.volume = vol;
      if (i >= steps) {
        clearInterval(fade);
        audio.pause();
        if (window.isPlaying !== undefined) window.isPlaying = false;
        if (window.updatePlayerUI) window.updatePlayerUI();
        _sleepFading = false;
        _sleepTimerEnd = null;
        const pill = document.getElementById('aurum-sleep-pill');
        if (pill) pill.classList.remove('visible');
        if (window.showToast) showToast('😴 Goodnight — music stopped');
      }
    }, stepMs);
  }

  function clearSleepTimer() {
    if (_sleepTimerRafId) { clearTimeout(_sleepTimerRafId); _sleepTimerRafId = null; }
    _sleepTimerEnd = null;
    _sleepFading = false;
    const pill = document.getElementById('aurum-sleep-pill');
    if (pill) pill.classList.remove('visible');
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 6. MOOD DJ
  // ─────────────────────────────────────────────────────────────────────────
  const MOOD_QUERIES = {
    chill:  ['lofi chill beats hindi songs', 'chill romantic bollywood songs', 'slow hindi songs relaxing'],
    hype:   ['upbeat dance bollywood songs', 'party hits hindi badshah', 'high energy bollywood gym'],
    sad:    ['sad hindi songs heartbreak arijit', 'emotional bollywood songs', 'breakup hindi songs'],
    focus:  ['lofi study beats instrumental', 'ambient chill focus music', 'soft instrumental hindi background'],
  };

  const MOOD_META = {
    chill:  { emoji: '😌', label: 'Chill',  desc: 'Smooth & relaxed' },
    hype:   { emoji: '⚡', label: 'Hype',   desc: 'Energy boost' },
    sad:    { emoji: '🌙', label: 'Sad',    desc: 'Feel it all' },
    focus:  { emoji: '🎯', label: 'Focus',  desc: 'Deep work' },
  };

  function _activateMood(mood) {
    if (!_isLoggedIn()) {
      if (window.showToast) showToast('Sign in to use Mood DJ');
      return;
    }
    _moodActive = mood;
    const meta = MOOD_META[mood] || { emoji: '🎵', label: mood };
    if (window.showToast) showToast(`${meta.emoji} ${meta.label} mode activated`);
    if (window.haptic) haptic([10, 25, 10]);

    const queries = MOOD_QUERIES[mood];
    if (!queries) return;
    const q = queries[Math.floor(Math.random() * queries.length)];

    fetch(`/api/songs?q=${encodeURIComponent(q)}`)
      .then(r => r.json())
      .then(d => {
        const songs = (d.results || []).filter(s => s.previewUrl);
        if (!songs.length) return;
        // Shuffle
        for (let i = songs.length - 1; i > 0; i--) {
          const j = Math.floor(Math.random() * (i + 1));
          [songs[i], songs[j]] = [songs[j], songs[i]];
        }
        if (window.playSongs) {
          playSongs(songs, 0);
          if (window.openFullscreen) openFullscreen();
        }
        _updateMoodBarUI(mood);
      })
      .catch(() => { if (window.showToast) showToast('Could not load mood playlist'); });
  }

  function _ensureMoodBar() {
    let bar = document.getElementById('aurum-mood-bar');
    if (bar) return bar;

    const fp = document.getElementById('fullscreen-player');
    if (!fp) return null;

    bar = document.createElement('div');
    bar.id = 'aurum-mood-bar';
    bar.innerHTML = `
      <div class="mood-bar-handle"></div>
      <div class="mood-bar-title">Mood DJ</div>
      <div class="mood-chips-row">
        <div class="mood-chip" data-mood="chill"><span class="mood-chip-icon">😌</span> Chill</div>
        <div class="mood-chip" data-mood="hype"><span class="mood-chip-icon">⚡</span> Hype</div>
        <div class="mood-chip" data-mood="sad"><span class="mood-chip-icon">🌙</span> Sad</div>
        <div class="mood-chip" data-mood="focus"><span class="mood-chip-icon">🎯</span> Focus</div>
      </div>
    `;
    fp.appendChild(bar);

    bar.querySelectorAll('.mood-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        const mood = chip.dataset.mood;
        bar.querySelectorAll('.mood-chip').forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        _activateMood(mood);
        setTimeout(() => _hideMoodBar(), 800);
      });
    });

    return bar;
  }

  function _updateMoodBarUI(mood) {
    const bar = document.getElementById('aurum-mood-bar');
    if (!bar) return;
    bar.querySelectorAll('.mood-chip').forEach(c => {
      c.classList.toggle('active', c.dataset.mood === mood);
    });
  }

  function showMoodBar() {
    if (!_isLoggedIn()) {
      if (window.openLoginModal) window.openLoginModal('mood');
      return;
    }
    const bar = _ensureMoodBar();
    if (!bar) return;
    bar.classList.add('open');
  }

  function _hideMoodBar() {
    const bar = document.getElementById('aurum-mood-bar');
    if (bar) bar.classList.remove('open');
  }

  function toggleMoodBar() {
    const bar = document.getElementById('aurum-mood-bar');
    if (bar && bar.classList.contains('open')) _hideMoodBar();
    else showMoodBar();
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 7. DETERMINISTIC AI SUGGESTIONS
  // ─────────────────────────────────────────────────────────────────────────
  function _buildAiQuery(track) {
    // Use primaryGenreName, artistName, releaseDate from iTunes metadata
    const genre  = track.primaryGenreName || '';
    const artist = (track.artistName || '').split(/[&,]|feat\.|ft\./i)[0].trim();
    const year   = track.releaseDate ? new Date(track.releaseDate).getFullYear() : null;

    const parts = [];
    if (artist) parts.push(artist);
    if (genre && genre !== 'Music') parts.push(genre);
    if (year && year < 2020) parts.push('classic');
    parts.push('similar songs');

    return parts.join(' ');
  }

  function _getListenHistoryArtists(limit = 3) {
    const hist = window._listenHistory || {};
    return Object.entries(hist)
      .sort((a, b) => b[1].count - a[1].count)
      .slice(0, limit)
      .map(([artist]) => artist);
  }

  async function fetchAiSuggestions(track, containerEl) {
    if (!_isLoggedIn() || !track || !containerEl) return;

    // Build a deterministic query from track metadata + listen history
    const primaryQuery = _buildAiQuery(track);
    const topArtists   = _getListenHistoryArtists(2);
    const fallbackQuery = topArtists.length
      ? topArtists[0] + ' similar songs'
      : 'top bollywood songs';

    let songs = [];
    try {
      const r = await fetch(`/api/songs?q=${encodeURIComponent(primaryQuery)}`);
      const d = await r.json();
      songs = (d.results || []).filter(s =>
        s.previewUrl && String(s.trackId) !== String(track.trackId)
      );
    } catch(e) {}

    if (songs.length < 4) {
      try {
        const r2 = await fetch(`/api/songs?q=${encodeURIComponent(fallbackQuery)}`);
        const d2 = await r2.json();
        const extra = (d2.results || []).filter(s =>
          s.previewUrl &&
          String(s.trackId) !== String(track.trackId) &&
          !songs.find(x => String(x.trackId) === String(s.trackId))
        );
        songs = [...songs, ...extra];
      } catch(e) {}
    }

    if (!songs.length) {
      containerEl.innerHTML = '';
      return;
    }

    // Shuffle deterministically seeded by trackId
    const seed = parseInt(String(track.trackId).slice(-4), 10) || 42;
    songs = _seededShuffle(songs, seed).slice(0, MAX_AI_RESULTS);

    _renderAiSuggestions(containerEl, songs, track);
  }

  function _seededShuffle(arr, seed) {
    const a = [...arr];
    let s = seed;
    for (let i = a.length - 1; i > 0; i--) {
      s = (s * 1664525 + 1013904223) & 0xffffffff;
      const j = Math.abs(s) % (i + 1);
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  function _renderAiSuggestions(container, songs, basedOnTrack) {
    const esc = window.esc || (s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'));
    const getArtUrl = window.getArtUrl || (s => s.artworkUrl100 || '');

    const reasonLabel = basedOnTrack.primaryGenreName
      ? esc(basedOnTrack.primaryGenreName)
      : 'For You';

    let html = `<div class="smart-ai-strip">`;
    html += `<div class="smart-ai-label">Based on your taste</div>`;
    html += `<div class="smart-ai-songs" id="ai-songs-list">`;
    for (let i = 0; i < songs.length; i++) {
      const s = songs[i];
      const artSrc = getArtUrl(s, '300x300');
      html += `
        <div class="smart-ai-song-row" data-idx="${i}" data-track-id="${esc(String(s.trackId))}">
          <img class="smart-ai-song-art" src="" data-lazy="${esc(artSrc)}" alt="">
          <div class="smart-ai-song-info">
            <div class="smart-ai-song-title">${esc(s.trackName)}</div>
            <div class="smart-ai-song-sub">${esc(s.artistName)}</div>
          </div>
          <span class="smart-ai-reason">${i === 0 ? reasonLabel : (i < 3 ? 'Similar' : 'You might like')}</span>
        </div>
      `;
    }
    html += `</div></div>`;
    container.innerHTML = html;

    // Lazy-load images
    container.querySelectorAll('img[data-lazy]').forEach(img => {
      img.src = img.dataset.lazy;
      img.onload = () => img.classList.add('loaded');
    });

    // Bind clicks
    container.querySelectorAll('.smart-ai-song-row').forEach((row, i) => {
      row.addEventListener('click', () => {
        if (window.playSongs) playSongs(songs, i);
        if (window.haptic) haptic(8);
      });
    });
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 8. TRACK PLAY HOOK (counts plays, checks night volume)
  // ─────────────────────────────────────────────────────────────────────────
  function _hookAudioPlayback() {
    const audio = window._aurumAudio || window.audio;
    if (!audio || audio._smartHooked) return;
    audio._smartHooked = true;

    audio.addEventListener('playing', () => {
      const track = window.currentTrack;
      if (track) _incrementPlayCount(track.trackId);
      _checkNightVolume();
    }, { passive: true });
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 9. SLEEP TIMER SETTINGS ENTRY
  // ─────────────────────────────────────────────────────────────────────────
  function _injectSleepTimerEntry() {
    // Look for settings body and inject sleep timer row if not present
    const settingsBody = document.getElementById('settings-body');
    if (!settingsBody || settingsBody.querySelector('.sleep-timer-row')) return;

    const row = document.createElement('div');
    row.className = 'sleep-timer-row';
    row.innerHTML = `
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="rgba(184,150,64,0.7)"
           stroke-width="1.8" stroke-linecap="round" style="flex-shrink:0;">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
      <div style="flex:1;font-family:'Sora',sans-serif;font-size:13px;font-weight:600;color:rgba(255,255,255,0.75);">
        Sleep Timer
        <div style="font-size:10.5px;font-weight:500;color:rgba(255,255,255,0.3);margin-top:1px;" id="sleep-timer-status">
          ${_sleepTimerEnd ? 'Active' : 'Off'}
        </div>
      </div>
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="rgba(255,255,255,0.25)"
           stroke-width="2" stroke-linecap="round">
        <polyline points="9 18 15 12 9 6"/>
      </svg>
    `;
    row.addEventListener('click', () => _showSleepTimerPicker());
    settingsBody.prepend(row);
  }

  function _showSleepTimerPicker() {
    const options = [
      { label: 'Off',     min: 0  },
      { label: '15 min',  min: 15 },
      { label: '30 min',  min: 30 },
      { label: '45 min',  min: 45 },
      { label: '1 hour',  min: 60 },
      { label: '90 min',  min: 90 },
    ];

    // Reuse the existing modal sheet pattern
    let picker = document.getElementById('sleep-timer-picker');
    if (!picker) {
      picker = document.createElement('div');
      picker.id = 'sleep-timer-picker';
      picker.className = 'modal-overlay';
      picker.innerHTML = `
        <div class="modal-sheet">
          <div class="modal-handle"></div>
          <div class="modal-title" style="font-family:'Sora',sans-serif;font-size:15px;font-weight:700;
               padding:0 20px 16px;color:rgba(255,255,255,0.88);">Sleep Timer</div>
          <div id="sleep-picker-options"></div>
        </div>
      `;
      picker.addEventListener('click', e => {
        if (!e.target.closest('.modal-sheet')) {
          picker.classList.remove('open');
        }
      });
      document.getElementById('app').appendChild(picker);
    }

    const optsEl = picker.querySelector('#sleep-picker-options');
    optsEl.innerHTML = '';
    options.forEach(opt => {
      const btn = document.createElement('button');
      btn.className = 'modal-option';
      const isActive = (opt.min === 0 && !_sleepTimerEnd) ||
                       (opt.min > 0 && _sleepTimerEnd &&
                        Math.abs(_sleepTimerEnd - Date.now() - opt.min * 60000) < 5 * 60000);
      btn.innerHTML = `
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none"
             stroke="${isActive ? '#d4b85a' : 'rgba(255,255,255,0.3)'}"
             stroke-width="1.8" stroke-linecap="round">
          ${opt.min === 0
            ? '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'
            : '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'}
        </svg>
        <span style="${isActive ? 'color:#d4b85a;font-weight:700;' : ''}">${opt.label}</span>
      `;
      btn.addEventListener('click', () => {
        setSleepTimer(opt.min);
        picker.classList.remove('open');
        // Update status text
        const status = document.getElementById('sleep-timer-status');
        if (status) status.textContent = opt.min > 0 ? `${opt.label}` : 'Off';
      });
      optsEl.appendChild(btn);
    });

    picker.classList.add('open');
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 10. EXPOSE aurumAI.render for the panel
  // ─────────────────────────────────────────────────────────────────────────
  function _setupAurumAI() {
    // Wait for auth to set up window.aurumAI properly
    if (!window.aurumAI) window.aurumAI = {};

    window.aurumAI.render = function(el, track) {
      if (!el) return;
      if (!_isLoggedIn()) { el.innerHTML = ''; return; }
      const targetTrack = track || window.currentTrack;
      if (!targetTrack) { el.innerHTML = ''; return; }
      fetchAiSuggestions(targetTrack, el);
    };

    // Also expose to home section strip (div#aurum-ai-suggestions)
    const strip = document.getElementById('aurum-ai-suggestions');
    if (strip && window.currentTrack && _isLoggedIn()) {
      strip.style.display = 'block';
      fetchAiSuggestions(window.currentTrack, strip);
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 11. MOOD DJ BUTTON IN FULLSCREEN PLAYER
  // ─────────────────────────────────────────────────────────────────────────
  function _injectMoodBtn() {
    if (document.getElementById('fp-mood-btn')) return;
    const fpBottom = document.querySelector('#fullscreen-player .fp-bottom');
    if (!fpBottom) return;

    const btn = document.createElement('button');
    btn.id = 'fp-mood-btn';
    btn.className = 'fp-bottom-icon';
    btn.setAttribute('aria-label', 'Mood DJ');
    btn.title = 'Mood DJ';
    btn.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
           stroke-linecap="round" stroke-linejoin="round" width="20" height="20">
        <path d="M9 18V5l12-2v13"/>
        <path d="M9 9l12-2"/>
        <circle cx="6" cy="18" r="3"/>
        <circle cx="18" cy="16" r="3"/>
      </svg>
      <span class="fp-lyrics-btn-label" style="font-size:8.5px;">Mood</span>
    `;
    btn.addEventListener('click', () => {
      if (!_isLoggedIn()) {
        if (window.openLoginModal) openLoginModal('mood');
        else if (window.showToast) showToast('Sign in to use Mood DJ');
        return;
      }
      toggleMoodBar();
    });

    // Insert before the download button
    const dlBtn = document.getElementById('fp-dl-btn');
    if (dlBtn) fpBottom.insertBefore(btn, dlBtn);
    else fpBottom.appendChild(btn);
  }

  // ─────────────────────────────────────────────────────────────────────────
  // 12. INITIALIZATION
  // ─────────────────────────────────────────────────────────────────────────
  function init() {
    if (_smartInited) return;
    _smartInited = true;

    _injectStyles();
    _patchMakeSongRow();
    _patchNextTrack();
    _hookAudioPlayback();
    _setupAurumAI();
    _injectMoodBtn();

    // Night volume check every minute
    setInterval(_checkNightVolume, 60000);
    _checkNightVolume();

    // Settings sleep timer entry — inject when settings open
    const settingsPanel = document.getElementById('settings-panel');
    if (settingsPanel) {
      new MutationObserver(() => {
        if (settingsPanel.classList.contains('open')) {
          _injectSleepTimerEntry();
        }
      }).observe(settingsPanel, { attributes: true, attributeFilter: ['class'] });
    }

    // Re-run AI suggestions when track changes
    const origLoadTrack = window.loadTrack;
    if (origLoadTrack && !origLoadTrack._smartPatched) {
      window.loadTrack = function(song, autoplay) {
        const result = origLoadTrack.apply(this, arguments);
        if (_isLoggedIn() && song) {
          setTimeout(() => {
            // Update home strip
            const strip = document.getElementById('aurum-ai-suggestions');
            if (strip && strip.style.display !== 'none') {
              fetchAiSuggestions(song, strip);
            }
          }, 1200);
        }
        return result;
      };
      window.loadTrack._smartPatched = true;
    }
  }

  // Init after DOM is ready and auth is loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    // Defer slightly to let auth.js run first
    setTimeout(init, 300);
  }

  // Re-init when user logs in
  document.addEventListener('aurumUserLogin', init, { passive: true });

  // ─────────────────────────────────────────────────────────────────────────
  // 13. PUBLIC API
  // ─────────────────────────────────────────────────────────────────────────
  window.aurumSmart = {
    setSleepTimer,
    clearSleepTimer,
    showMoodBar,
    toggleMoodBar,
    fetchAiSuggestions,
    getPlayCount,
    activateMood: _activateMood,
    init,
  };

})();
// ════════════════════════════════════════════════════════════════════════════
// END AURUM SMART FEATURES v1.0
// ════════════════════════════════════════════════════════════════════════════
