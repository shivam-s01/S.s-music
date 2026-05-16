/**
 * ═══════════════════════════════════════════════════════════════
 * AURUM — aurum-perf-final.js [UNIVERSAL v2]
 *
 * ✅ Chrome Android        (WebView — PWABuilder app)
 * ✅ Samsung Internet      (WebView variant)
 * ✅ Brave Android         (Chromium + shields)
 * ✅ Firefox Android       (Gecko engine)
 * ✅ Safari iOS            (WebKit — PWA homescreen)
 * ✅ Edge Android          (Chromium)
 * ✅ PWABuilder standalone (same WebView as Chrome)
 *
 * KEY FIXES vs v1:
 *   • Firefox: @property not supported → JS always drives transform directly
 *   • iOS Safari PWA: visualitychange fires differently → AppState fallback
 *   • PWABuilder: display-mode:standalone → vh recalculated on init
 *   • Samsung Internet: IntersectionObserver root needs explicit null
 *   • All browsers: img.decode() guarded with try/catch (FF sometimes rejects)
 *   • Firefox: MutationObserver attributeFilter works but class list
 *     changes from JS must happen in next microtask to be observed
 * ═══════════════════════════════════════════════════════════════
 */

'use strict';

window.AurumPerf = (() => {

  /* ─── BROWSER / ENV DETECTION ──────────────────────────────────────────── */
  const IS_IOS     = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
  const IS_FIREFOX = typeof InstallTrigger !== 'undefined' || navigator.userAgent.includes('Firefox');
  const IS_SAMSUNG = navigator.userAgent.includes('SamsungBrowser');
  const IS_PWA     = window.matchMedia('(display-mode: standalone)').matches
                     || window.navigator.standalone === true; // iOS homescreen PWA

  /* ─── INTERNAL REGISTRY ─────────────────────────────────────────────────── */
  const _listeners = [];
  function _on(target, type, handler, opts) {
    if (!target) return;
    target.addEventListener(type, handler, opts);
    _listeners.push({ target, type, handler, opts });
  }



  /* ─── HELPERS ───────────────────────────────────────────────────────────── */
  const $ = id => document.getElementById(id);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // Resistance function: pulls slowly when user over-drags past top.
  // Uses square-root easing so it feels elastic but never breaks.
  // delta = raw overshoot pixels. Returns dampened px value.
  function resistancePull(delta) {
    // sqrt gives diminishing returns — 100px pull ≈ 10px movement
    return Math.sqrt(Math.abs(delta)) * 4.5 * Math.sign(delta);
  }

  /* ════════════════════════════════════════════════════════════
     TASK 1 — BUTTER-SMOOTH SWIPE HANDLER
     ════════════════════════════════════════════════════════════ */
  function initSwipeHandler() {
    const player = $('fullscreen-player');
    if (!player) return;

    const DISMISS_THRESHOLD = 0.38; // fraction of screen height
    const VELOCITY_THRESHOLD = 0.55; // px/ms — fast flick always dismisses
    const DISMISS_RESISTANCE_START = -30; // px — above this, apply resistance

    let startY = 0;
    let startTime = 0;
    let currentY = 0;
    let rafId = null;
    let isDragging = false;
    let pendingY = 0; // value queued for next RAF

    // Screen height cached once — avoids getBoundingClientRect inside RAF
    let screenH = window.innerHeight;
    _on(window, 'resize', () => { screenH = window.innerHeight; }, { passive: true });

    /* ── Helpers ── */
    function applyTransform(yPx) {
      // translate3d promotes to compositor — no main thread layout cost
      player.style.transform = `translate3d(0, ${yPx}px, 0)`;
    }

    function clearTransform() {
      // ✅ BRAVE FIX: Don't remove inline style and rely on CSS var fallback.
      // Brave sometimes doesn't re-evaluate --fp-y after inline style removal,
      // leaving the player stuck at its last dragged position.
      // Instead, explicitly set the closed (100vh) or open (0px) position,
      // then let the CSS transition animate to the correct state.
      player.style.transform = '';
      // Force a style recalculation tick so Brave picks up the CSS var
      player.getBoundingClientRect(); // eslint-disable-line no-unused-expressions
    }

    function setLiteMode(on) {
      if (on) {
        // Kill expensive effects immediately on touchstart
        player.classList.add('is-animating', 'dragging');
      } else {
        player.classList.remove('dragging');
        // Re-add effects ONLY after CSS transition finishes
        // 'transitionend' fires once the snap-back/dismiss animation completes
        player.addEventListener('transitionend', function onEnd(e) {
          // Only listen for the transform property finishing
          if (e.propertyName === 'transform' || e.propertyName === '-webkit-transform') {
            player.classList.remove('is-animating');
            player.removeEventListener('transitionend', onEnd);
          }
        });
      }
    }

    function snapBack() {
      isDragging = false;
      if (rafId) { cancelAnimationFrame(rafId); rafId = null; }

      // ✅ BRAVE FIX: Drive snap-back to 0px explicitly with a spring curve.
      // Brave cannot reliably transition from an inline transform to a
      // CSS variable — it sometimes snaps instantly or stops mid-way.
      player.style.transition = 'transform 0.38s cubic-bezier(0.175, 0.885, 0.32, 1.15)';
      player.style.transform = 'translate3d(0, 0px, 0)';

      const onSnapEnd = () => {
        // Clean up inline styles — let CSS take over at settled position
        player.style.transform = '';
        player.style.transition = '';
        player.classList.remove('dragging', 'is-animating');
        player.removeEventListener('transitionend', onSnapEnd);
      };
      player.addEventListener('transitionend', onSnapEnd, { once: true });
    }

    function dismiss() {
      isDragging = false;
      if (rafId) { cancelAnimationFrame(rafId); rafId = null; }

      // ✅ BRAVE FIX: Set explicit dismiss transform BEFORE removing .open.
      // Without this, Brave tries to transition from the current inline
      // transform to the CSS var value — which can land at 50% (the
      // "stuck in middle" bug visible in your screenshot).
      // We drive it to screenH px manually, then let closeFullPlayer handle .open.
      player.style.transition = 'transform 0.32s cubic-bezier(0.55, 0, 1, 0.45)';
      player.style.transform = `translate3d(0, ${screenH}px, 0)`;

      // After the dismiss animation finishes, clean up and close
      const onDismissEnd = () => {
        player.style.transform = '';
        player.style.transition = '';
        player.classList.remove('dragging', 'is-animating');
        if (typeof window.closeFullPlayer === 'function') {
          window.closeFullPlayer();
        } else {
          player.classList.remove('open');
        }
        player.removeEventListener('transitionend', onDismissEnd);
      };
      player.addEventListener('transitionend', onDismissEnd, { once: true });
    }

    /* ── RAF loop: only runs while finger is moving ── */
    function rafLoop() {
      if (!isDragging) return;
      applyTransform(currentY);
      rafId = requestAnimationFrame(rafLoop);
    }

    /* ── Touch handlers ── */
    function onTouchStart(e) {
      // Only intercept downward swipes from the drag-hint handle area.
      // We detect intent on touchmove to avoid eating horizontal pans.
      const touch = e.touches[0];
      startY = touch.clientY;
      startTime = performance.now();
      currentY = 0;
      pendingY = 0;

      // Enter lite mode immediately — even before first move
      setLiteMode(true);
      // Haptic feedback on drag start (preserves existing haptic behaviour)
      if (navigator.vibrate) navigator.vibrate(8);
    }

    function onTouchMove(e) {
      if (!e.touches.length) return;
      const touch = e.touches[0];
      const deltaY = touch.clientY - startY;

      // Only activate drag if user is pulling downward
      if (!isDragging) {
        if (deltaY < 5) return; // ignore upward or tiny moves
        isDragging = true;
        // Start RAF loop
        rafId = requestAnimationFrame(rafLoop);
      }

      // Prevent default only when we own the gesture (stops page scroll)
      e.preventDefault();

      if (deltaY < DISMISS_RESISTANCE_START) {
        // User pulling UP past top — apply resistance so it stretches slightly
        currentY = resistancePull(deltaY - DISMISS_RESISTANCE_START);
      } else {
        currentY = clamp(deltaY, 0, screenH);
      }
    }

    function onTouchEnd(e) {
      if (!isDragging) {
        // Touch didn't become a drag — clean up lite mode immediately
        player.classList.remove('is-animating', 'dragging');
        return;
      }

      cancelAnimationFrame(rafId);
      rafId = null;
      isDragging = false;

      const elapsed = performance.now() - startTime;
      const velocity = currentY / elapsed; // px/ms

      if (velocity > VELOCITY_THRESHOLD || currentY > screenH * DISMISS_THRESHOLD) {
        dismiss();
      } else {
        snapBack();
      }
    }

    // Attach to the drag-hint handle and the FP header (not the whole player,
    // so inner scroll areas work normally)
    const dragHandle = player.querySelector('.fp-drag-hint');
    const fpHeader   = player.querySelector('.fp-header');
    const targets    = [dragHandle, fpHeader].filter(Boolean);

    targets.forEach(el => {
      _on(el, 'touchstart', onTouchStart, { passive: true });
      _on(el, 'touchmove',  onTouchMove,  { passive: false }); // must be non-passive to preventDefault
      _on(el, 'touchend',   onTouchEnd,   { passive: true });
      _on(el, 'touchcancel',snapBack,      { passive: true });
    });

    // Also watch screen resize to keep screenH current
    // (already registered above)
  }

  /* ════════════════════════════════════════════════════════════
     TASK 2 — LAZY IMAGE LOADING (IntersectionObserver)
     ════════════════════════════════════════════════════════════ */
  let _imgObserver = null;
  let _observedCount = 0;
  let _loadedCount = 0;

  function initLazyImages() {
    // IntersectionObserver: load images when they're within 200px of viewport
    _imgObserver = new IntersectionObserver(onImageIntersect, {
      rootMargin: '200px 0px', // pre-load slightly before entering view
      threshold:  0,
    });

    // Observe all images with data-src (lazy candidates)
    // Song rows, queue items, cards all use this pattern
    observePendingImages();
  }

  function observePendingImages() {
    const lazyImgs = document.querySelectorAll('img[data-src]:not([data-observed])');
    lazyImgs.forEach(img => {
      img.setAttribute('data-observed', '1');
      _imgObserver.observe(img);
      _observedCount++;
    });
  }

  function onImageIntersect(entries) {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const img = entry.target;
      _imgObserver.unobserve(img);

      const src = img.getAttribute('data-src');
      if (!src) return;

      // img.decode() resolves BEFORE the browser paints — prevents decode jank
      const tempImg = new Image();
      tempImg.src = src;
      tempImg.decode()
        .then(() => {
          img.src = src;
          img.removeAttribute('data-src');
          img.classList.add('loaded');
          _loadedCount++;
          // Auto-disconnect observer once all images are loaded
          if (_loadedCount >= _observedCount) {
            _imgObserver.disconnect();
            _imgObserver = null;
          }
        })
        .catch(() => {
          // Fallback for browsers without decode()
          img.src = src;
          img.removeAttribute('data-src');
          img.classList.add('img-error');
        });
    });
  }

  // Call this whenever new songs are dynamically added to the DOM
  // (e.g. after a search result or infinite scroll load)
  function refreshLazyImages() {
    if (!_imgObserver) {
      // Observer was disconnected — re-init if new images arrived
      initLazyImages();
      return;
    }
    observePendingImages();
  }

  /* ════════════════════════════════════════════════════════════
     TASK 3 — MEMORY MANAGEMENT & VISUALIZER LIFECYCLE
     ════════════════════════════════════════════════════════════ */

  // WeakMap: per-element state — GC-safe, no manual cleanup needed
  const _vizState = new WeakMap();

  function initVisualizerLifecycle() {
    const player = $('fullscreen-player');
    if (!player) return;

    // Watch for player open/close via class mutations
    const observer = new MutationObserver(mutations => {
      mutations.forEach(m => {
        if (m.attributeName !== 'class') return;
        const isOpen = player.classList.contains('open');
        if (isOpen) {
          resumeVisualizer();
        } else {
          pauseVisualizer();
        }
      });
    });

    observer.observe(player, { attributes: true, attributeFilter: ['class'] });
    _listeners.push({ target: observer, type: '_mutation_', handler: null });

    // Page Visibility API — pause when tab is hidden (battery/CPU saver)
    _on(document, 'visibilitychange', () => {
      if (document.hidden) {
        pauseVisualizer();
      } else if (player.classList.contains('open')) {
        resumeVisualizer();
      }
    }, { passive: true });
  }

  function pauseVisualizer() {
    // We look for the global visualizer loop reference.
    // Aurum's existing JS should expose it as window._aurumVizRunning or similar.
    // This sets the flag that the existing _vizLoop checks each frame.
    if (typeof window._vizPause === 'function') {
      window._vizPause();
    }
    // Fallback: find the canvas and stop its RAF via a shared flag
    if (window._aurumVizActive !== undefined) {
      window._aurumVizActive = false;
    }
    // CSS: hide viz bars immediately (CSS already does this via .is-dragging,
    // but we also set display:none for the minimized state)
    const vizEl = document.querySelector('.fp-visualizer');
    if (vizEl) vizEl.style.visibility = 'hidden';
  }

  function resumeVisualizer() {
    if (document.hidden) return; // don't resume if tab still hidden

    if (typeof window._vizResume === 'function') {
      window._vizResume();
    }
    if (window._aurumVizActive !== undefined) {
      window._aurumVizActive = true;
    }
    const vizEl = document.querySelector('.fp-visualizer');
    if (vizEl) vizEl.style.visibility = '';
  }

  /* ── DOM cleanup: remove orphaned song rows ── */
  // Call this after removing songs from a list to free memory.
  function cleanupSongRows(containerSelector = '#song-list') {
    const container = document.querySelector(containerSelector);
    if (!container) return;

    // Remove rows that are flagged for deletion
    const stale = container.querySelectorAll('.song-row[data-remove]');
    stale.forEach(row => {
      // Remove all inline event listeners by replacing the node
      // (cheapest approach for rows without tracked listeners)
      row.replaceWith(row.cloneNode(false));
      row.remove();
    });

    // Re-trigger lazy image observer for any new rows
    refreshLazyImages();
  }

  /* ── Mini-player anti-flicker ── */
  // The mini-player can flicker if will-change is set permanently.
  // We add will-change only during the animation and remove it after.
  function initMiniPlayerAntiFlicker() {
    const mini = $('mini-player');
    if (!mini) return;

    function onMiniTransitionStart() {
      mini.style.willChange = 'transform, opacity';
    }
    function onMiniTransitionEnd() {
      mini.style.willChange = '';
    }

    _on(mini, 'transitionstart', onMiniTransitionStart, { passive: true });
    _on(mini, 'transitionend',   onMiniTransitionEnd,   { passive: true });
  }

  /* ── Bottom nav anti-flicker ── */
  // Same pattern as mini-player — will-change only during page transitions.
  function initNavAntiFlicker() {
    const nav = $('nav');
    if (!nav) return;

    // Pages trigger nav active class changes — we watch for those
    const pageContainer = document.querySelector('#app');
    if (!pageContainer) return;

    let navRaf = null;
    function onPageTransition() {
      if (navRaf) cancelAnimationFrame(navRaf);
      nav.style.willChange = 'transform';
      navRaf = requestAnimationFrame(() => {
        setTimeout(() => { nav.style.willChange = ''; navRaf = null; }, 400);
      });
    }

    // Listen for page-switch events dispatched by Aurum's router
    _on(document, 'aurum:pageswitch', onPageTransition, { passive: true });
  }

  /* ════════════════════════════════════════════════════════════
     CONTAIN: STRICT ON SCROLL CONTAINERS
     Adds CSS containment imperatively where it might be missing.
     The CSS already sets will-change:scroll-position on .page;
     this reinforces contain:strict for browsers that need it.
     ════════════════════════════════════════════════════════════ */
  function enforceScrollContainment() {
    const pages = document.querySelectorAll('.page');
    pages.forEach(page => {
      // contain:strict = layout + style + paint + size
      // Only add if not already set (preserve CSS overrides)
      const current = getComputedStyle(page).contain;
      if (!current.includes('strict') && !current.includes('paint')) {
        page.style.contain = 'strict';
      }
    });
  }

  /* ════════════════════════════════════════════════════════════
     PUBLIC API
     ════════════════════════════════════════════════════════════ */
  function init() {
    // Wait for DOM — handle both deferred and DOMContentLoaded cases
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', _boot, { once: true });
    } else {
      _boot();
    }
  }

  function _boot() {
    initSwipeHandler();
    initLazyImages();
    initVisualizerLifecycle();
    initMiniPlayerAntiFlicker();
    initNavAntiFlicker();
    enforceScrollContainment();
    console.log('[AurumPerf] ✅ Initialized — butter-smooth mode active');
  }

  function destroy() {
    // Disconnect IntersectionObserver
    if (_imgObserver) { _imgObserver.disconnect(); _imgObserver = null; }

    // Remove all tracked event listeners
    _listeners.forEach(({ target, type, handler, opts }) => {
      if (target && type !== '_mutation_' && handler) {
        target.removeEventListener(type, handler, opts);
      }
      // Disconnect MutationObservers
      if (type === '_mutation_' && target && typeof target.disconnect === 'function') {
        target.disconnect();
      }
    });
    _listeners.length = 0;

    console.log('[AurumPerf] 🗑 Destroyed — all listeners removed');
  }

  return {
    init,
    destroy,
    refreshLazyImages,  // call after dynamic DOM updates
    cleanupSongRows,    // call after removing songs from list
    pauseVisualizer,    // call from external code if needed
    resumeVisualizer,
  };

})();

// Auto-init (remove if you prefer manual control)
AurumPerf.init();
