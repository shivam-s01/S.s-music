// ═════════════════════════════════════════════════════════════════
// AURUM TV FIX — Apply this patch to app.js
// ═════════════════════════════════════════════════════════════════

// PATCH 1: Better TV Detection
const isTV = (() => {
  const ua = navigator.userAgent;
  const tvPattern = /Web0S|Tizen|SmartTV|AppleTV|GoogleTV|BRAVIA|LGTV|SMART-TV|Hisense|Vidaa|Roku|FireTV|TV Browser|WebOS|Opera TV|Viera|HbbTV/i;
  if (tvPattern.test(ua)) return true;
  
  // Screen size detection (1280+ width on non-mobile)
  if (window.innerWidth >= 1280) {
    const hasCoarsePointer = window.matchMedia('(pointer:coarse)').matches;
    const isMobileUA = /android|linux/i.test(ua) && !ua.includes('Windows') && !ua.includes('Mac');
    if (isMobileUA && hasCoarsePointer) return true;
  }
  
  return false;
})();

// PATCH 2: Fix Low-End Detection
const isLowEnd = (() => {
  if (isTV) {
    // TV devices are assumed lower-end unless flagged otherwise
    if (navigator.hardwareConcurrency && navigator.hardwareConcurrency > 6) return false;
    if (navigator.deviceMemory && navigator.deviceMemory > 3) return false;
    return true;
  }
  
  return (navigator.hardwareConcurrency || 8) <= 4 || 
         (typeof navigator.deviceMemory !== 'undefined' && navigator.deviceMemory <= 2);
})();

// PATCH 3: Disable Mobile Gestures on TV EARLY
if (isTV) {
  document.documentElement.classList.add('is-tv');
  document.body.classList.add('is-tv');
}

// PATCH 4: Prevent Multiple Fullscreen Opens
let _fullscreenLocking = false;
const originalOpenFullscreen = window.openFullscreen || (() => {});
window.openFullscreen = function() {
  if (_fullscreenLocking) return;
  _fullscreenLocking = true;
  const fp = document.getElementById('fullscreen-player');
  if (fp && !fp.classList.contains('open')) {
    fp.style.transform = '';
    fp.classList.add('open');
    if (!isTV) {
      const mp = document.getElementById('mini-player');
      if (mp) { mp.style.transition = 'opacity 0.2s ease, transform 0.2s ease'; mp.style.opacity = '0'; mp.style.pointerEvents = 'none'; }
    }
    updateNextStrip?.();
    setTimeout(() => { if (typeof _attachArtSwipe === 'function' && !isTV) _attachArtSwipe(); }, 100);
    if (!document.hidden && !isLowEnd) _startViz?.();
    if (!isLowEnd) document.getElementById('ambient-canvas')?.classList.add('orbs-active');
  }
  setTimeout(() => { _fullscreenLocking = false; }, 250);
};

// PATCH 5: TV Navigation Setup (Better)
function setupTVNavigation() {
  if (!isTV) return;
  
  // Force fullscreen only once per track change
  let lastTrackId = null;
  const checkForTrackChange = setInterval(() => {
    if (typeof currentTrack !== 'undefined' && currentTrack && currentTrack.trackId !== lastTrackId) {
      lastTrackId = currentTrack.trackId;
      const fp = document.getElementById('fullscreen-player');
      if (fp && !fp.classList.contains('open')) {
        window.openFullscreen();
      }
    }
  }, 800);
  
  document.addEventListener('keydown', (e) => {
    const fp = document.getElementById('fullscreen-player');
    const fpOpen = fp && fp.classList.contains('open');
    const qp = document.getElementById('queue-panel');
    const qpOpen = qp && qp.classList.contains('open');
    
    // Volume control in fullscreen
    if (fpOpen) {
      if (e.key === 'ArrowUp') { 
        e.preventDefault(); 
        setVolume?.(Math.min(1, (audio?.volume || 0) + 0.1)); 
        showToast?.(`🔊 ${Math.round((audio?.volume || 0) * 100)}%`); 
        return; 
      }
      if (e.key === 'ArrowDown') { 
        e.preventDefault(); 
        setVolume?.(Math.max(0, (audio?.volume || 0) - 0.1)); 
        showToast?.(`🔇 ${Math.round((audio?.volume || 0) * 100)}%`); 
        return; 
      }
    }
    
    // Navigation keys
    switch (e.key) {
      case 'ArrowRight': 
        e.preventDefault();
        if (fpOpen) { nextTrack?.(); } 
        else { focusNextElement?.(); } 
        break;
        
      case 'ArrowLeft': 
        e.preventDefault();
        if (fpOpen) { prevTrack?.(); } 
        else { focusPrevElement?.(); } 
        break;
        
      case 'Enter': 
      case ' ':
        e.preventDefault();
        if (fpOpen) { togglePlay?.(); } 
        else if (document.activeElement?.click) { document.activeElement.click(); }
        break;
        
      case 'Escape': 
      case 'Backspace':
        e.preventDefault();
        if (fpOpen) { closeFullscreen?.(); } 
        else if (qpOpen) { closeQueuePanel?.(); }
        break;
    }
  });
  
  function getFocusableElements() {
    return Array.from(document.querySelectorAll(
      '.song-row, .quick-card, .browse-card, .playlist-card, .nav-btn, .fp-close, button:not([disabled]), [tabindex="0"]'
    )).filter(el => {
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    });
  }
  
  function focusNextElement() {
    const focusable = getFocusableElements();
    if (!focusable.length) return;
    const current = document.activeElement;
    const idx = focusable.indexOf(current);
    const next = focusable[(idx + 1) % focusable.length];
    next?.focus();
    next?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  
  function focusPrevElement() {
    const focusable = getFocusableElements();
    if (!focusable.length) return;
    const current = document.activeElement;
    const idx = focusable.indexOf(current);
    const prev = focusable[(idx - 1 + focusable.length) % focusable.length];
    prev?.focus();
    prev?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  
  // Auto-focus first interactive element
  setTimeout(() => {
    const first = document.querySelector('.quick-card, .song-row, button');
    first?.focus?.();
  }, 600);
}

if (isTV) {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupTVNavigation);
  } else {
    setupTVNavigation();
  }
}

// PATCH 6: Reduce Visualizer Load on TV
if (isLowEnd && typeof VIZ_COUNT !== 'undefined') {
  window.VIZ_COUNT = 0;
  const vizInitCode = window._vizInit = function() {
    const c = document.getElementById('fp-visualizer');
    if (c) c.innerHTML = '';
  };
}

// PATCH 7: Disable Backdrop Filter on TV
if (isTV) {
  const style = document.createElement('style');
  style.textContent = `
    #mini-player,
    #fullscreen-player,
    #queue-panel,
    #nav {
      backdrop-filter: none !important;
      -webkit-backdrop-filter: none !important;
    }
  `;
  document.head.appendChild(style);
}

// PATCH 8: Better Image Loading on TV
if (isTV) {
  const origSetImgSrc = window.setImgSrc;
  window.setImgSrc = function(img, src) {
    if (!src) { img.src = IMG_PLACEHOLDER || ''; return; }
    img.src = src;
    if (img.complete && img.naturalWidth > 0) img.classList.add('loaded');
  };
}

// PATCH 9: Clear Cache Periodically
setInterval(() => {
  if (typeof sectionCache !== 'undefined') {
    const keep = new Set(['recent', 'featured']);
    Object.keys(sectionCache).forEach(k => {
      if (!keep.has(k)) delete sectionCache[k];
    });
  }
}, 45000);

// PATCH 10: Reduce Motion for TV
if (isTV) {
  document.documentElement.style.setProperty('--t-spring', '0.12s ease');
  document.documentElement.style.setProperty('--t-smooth', '0.08s ease');
  document.documentElement.style.setProperty('--t-sheet', '0.16s ease');
}

console.log(`[Aurum] Device: ${isTV ? '📺 TV' : '📱 Mobile'} | Low-End: ${isLowEnd ? '⚠️ Yes' : '✅ No'}`);
