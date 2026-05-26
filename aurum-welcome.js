// ════════════════════════════════════════════════════════════════════════════
// AURUM WELCOME SCREEN v1.0
// Cinematic one-time welcome after Google login — no dependencies
// Triggered by: handleGoogleCredential() in auth.js
// ════════════════════════════════════════════════════════════════════════════

(function() {
  'use strict';

  const WELCOME_KEY = 'aurum_welcome_shown';

  // ── Public API ────────────────────────────────────────────────────────────
  window.aurumWelcome = {
    show: showWelcome,
    reset: function() { try { localStorage.removeItem(WELCOME_KEY); } catch(e) {} }
  };

  // ── Inject CSS once ───────────────────────────────────────────────────────
  function _injectStyles() {
    if (document.getElementById('aurum-welcome-css')) return;
    const style = document.createElement('style');
    style.id = 'aurum-welcome-css';
    style.textContent = `
      /* ── Overlay ── */
      #aurum-welcome-overlay {
        position: fixed;
        inset: 0;
        z-index: 999999;
        background: #050508;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        overflow: hidden;
        -webkit-tap-highlight-color: transparent;
        user-select: none;
      }

      /* ── Ambient orbs ── */
      #aurum-welcome-overlay .wc-orb {
        position: absolute;
        border-radius: 50%;
        pointer-events: none;
        will-change: transform, opacity;
        filter: blur(72px);
        opacity: 0;
        transition: opacity 1.2s ease;
      }
      #aurum-welcome-overlay .wc-orb-a {
        width: 340px; height: 340px;
        background: radial-gradient(circle, rgba(184,150,64,0.22) 0%, transparent 70%);
        top: -80px; left: -60px;
        animation: wcOrbFloat 7s ease-in-out 0.6s infinite alternate;
      }
      #aurum-welcome-overlay .wc-orb-b {
        width: 260px; height: 260px;
        background: radial-gradient(circle, rgba(184,150,64,0.14) 0%, transparent 70%);
        bottom: -60px; right: -40px;
        animation: wcOrbFloat 9s ease-in-out 1.4s infinite alternate-reverse;
      }
      @keyframes wcOrbFloat {
        from { transform: translate(0, 0) scale(1); }
        to   { transform: translate(18px, -22px) scale(1.08); }
      }

      /* ── Grain overlay ── */
      #aurum-welcome-overlay::before {
        content: '';
        position: absolute;
        inset: 0;
        background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");
        opacity: 0.55;
        pointer-events: none;
        z-index: 1;
      }

      /* ── Content wrapper ── */
      #aurum-welcome-overlay .wc-content {
        position: relative;
        z-index: 2;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 0;
        padding: 0 32px;
        max-width: 340px;
        width: 100%;
      }

      /* ── Logo mark ── */
      #aurum-welcome-overlay .wc-logo-mark {
        width: 52px; height: 52px;
        opacity: 0;
        transform: scale(0.7) translateY(12px);
        transition: opacity 0.7s cubic-bezier(0.22,1,0.36,1),
                    transform 0.7s cubic-bezier(0.22,1,0.36,1);
        margin-bottom: 16px;
      }
      #aurum-welcome-overlay .wc-logo-mark.visible {
        opacity: 1;
        transform: scale(1) translateY(0);
      }

      /* ── App name ── */
      #aurum-welcome-overlay .wc-app-name {
        font-family: 'Sora', sans-serif;
        font-size: 13px;
        font-weight: 700;
        letter-spacing: 0.22em;
        text-transform: uppercase;
        background: linear-gradient(138deg, #ecd9b0 0%, #c8a858 55%, #b89640 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
        opacity: 0;
        transform: translateY(10px);
        transition: opacity 0.55s ease 0.18s, transform 0.55s ease 0.18s;
        margin-bottom: 32px;
      }
      #aurum-welcome-overlay .wc-app-name.visible {
        opacity: 1;
        transform: translateY(0);
      }

      /* ── Gold line divider ── */
      #aurum-welcome-overlay .wc-divider {
        width: 0;
        height: 1px;
        background: linear-gradient(90deg, transparent, rgba(184,150,64,0.55), transparent);
        margin-bottom: 32px;
        transition: width 0.7s cubic-bezier(0.22,1,0.36,1) 0.35s;
      }
      #aurum-welcome-overlay .wc-divider.visible { width: 120px; }

      /* ── Greeting text ── */
      #aurum-welcome-overlay .wc-greeting {
        font-family: 'Sora', sans-serif;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: rgba(184,150,64,0.7);
        opacity: 0;
        transform: translateY(8px);
        transition: opacity 0.5s ease 0.5s, transform 0.5s ease 0.5s;
        margin-bottom: 8px;
      }
      #aurum-welcome-overlay .wc-greeting.visible {
        opacity: 1;
        transform: translateY(0);
      }

      /* ── Welcome headline ── */
      #aurum-welcome-overlay .wc-headline {
        font-family: 'Sora', sans-serif;
        font-size: 26px;
        font-weight: 800;
        letter-spacing: -0.5px;
        color: #fff;
        text-align: center;
        line-height: 1.18;
        opacity: 0;
        transform: translateY(14px);
        transition: opacity 0.65s cubic-bezier(0.22,1,0.36,1) 0.62s,
                    transform 0.65s cubic-bezier(0.22,1,0.36,1) 0.62s;
        margin-bottom: 6px;
      }
      #aurum-welcome-overlay .wc-headline.visible {
        opacity: 1;
        transform: translateY(0);
      }
      #aurum-welcome-overlay .wc-headline .wc-name-gold {
        background: linear-gradient(138deg, #ecd9b0 0%, #c8a858 60%, #b89640 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
      }

      /* ── Perks list ── */
      #aurum-welcome-overlay .wc-perks {
        display: flex;
        flex-direction: column;
        gap: 0;
        width: 100%;
        margin-top: 32px;
        margin-bottom: 36px;
      }
      #aurum-welcome-overlay .wc-perk {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 11px 16px;
        border-radius: 14px;
        background: rgba(255,255,255,0);
        opacity: 0;
        transform: translateX(-18px);
        transition: opacity 0.5s ease, transform 0.5s cubic-bezier(0.22,1,0.36,1),
                    background 0.3s ease;
      }
      #aurum-welcome-overlay .wc-perk.visible {
        opacity: 1;
        transform: translateX(0);
        background: rgba(184,150,64,0.06);
        border: 1px solid rgba(184,150,64,0.12);
      }
      #aurum-welcome-overlay .wc-perk-icon {
        width: 32px; height: 32px; flex-shrink: 0;
        border-radius: 10px;
        background: linear-gradient(135deg, rgba(184,150,64,0.22), rgba(184,150,64,0.07));
        border: 1px solid rgba(184,150,64,0.28);
        display: flex; align-items: center; justify-content: center;
      }
      #aurum-welcome-overlay .wc-perk-icon svg {
        width: 15px; height: 15px;
        stroke: #d4b85a;
        fill: none;
        stroke-width: 1.8;
        stroke-linecap: round;
        stroke-linejoin: round;
      }
      #aurum-welcome-overlay .wc-perk-text {
        flex: 1;
      }
      #aurum-welcome-overlay .wc-perk-title {
        font-family: 'Sora', sans-serif;
        font-size: 13px;
        font-weight: 700;
        color: rgba(255,255,255,0.92);
        letter-spacing: -0.1px;
        line-height: 1.2;
      }
      #aurum-welcome-overlay .wc-perk-sub {
        font-family: 'Sora', sans-serif;
        font-size: 10.5px;
        font-weight: 500;
        color: rgba(255,255,255,0.38);
        margin-top: 1px;
      }

      /* ── Tagline ── */
      #aurum-welcome-overlay .wc-tagline {
        font-family: 'Sora', sans-serif;
        font-size: 12px;
        font-weight: 500;
        letter-spacing: 0.08em;
        color: rgba(255,255,255,0.28);
        text-align: center;
        opacity: 0;
        transition: opacity 0.55s ease;
      }
      #aurum-welcome-overlay .wc-tagline.visible { opacity: 1; }

      /* ── Dismiss pulse ring ── */
      #aurum-welcome-overlay .wc-dismiss-ring {
        position: absolute;
        bottom: 48px;
        left: 50%;
        transform: translateX(-50%);
        z-index: 3;
        opacity: 0;
        transition: opacity 0.4s ease;
      }
      #aurum-welcome-overlay .wc-dismiss-ring.visible { opacity: 1; }
      #aurum-welcome-overlay .wc-dismiss-ring-inner {
        width: 36px; height: 36px; border-radius: 50%;
        border: 1.5px solid rgba(184,150,64,0.35);
        display: flex; align-items: center; justify-content: center;
        position: relative;
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
      }
      #aurum-welcome-overlay .wc-dismiss-ring-inner::after {
        content: '';
        position: absolute;
        inset: -5px;
        border-radius: 50%;
        border: 1px solid rgba(184,150,64,0.18);
        animation: wcRingPulse 1.8s ease-in-out infinite;
      }
      @keyframes wcRingPulse {
        0%,100% { transform: scale(1); opacity: 0.7; }
        50%     { transform: scale(1.3); opacity: 0; }
      }
      #aurum-welcome-overlay .wc-dismiss-ring-inner svg {
        width: 14px; height: 14px;
        stroke: rgba(184,150,64,0.6);
        fill: none;
        stroke-width: 1.8;
        stroke-linecap: round;
      }

      /* ── Fade-out transition ── */
      #aurum-welcome-overlay.wc-exit {
        opacity: 0;
        transform: scale(1.03);
        transition: opacity 0.55s cubic-bezier(0.4,0,1,1),
                    transform 0.55s cubic-bezier(0.4,0,1,1);
        pointer-events: none;
      }
    `;
    document.head.appendChild(style);
  }

  // ── Build overlay DOM ─────────────────────────────────────────────────────
  function _buildOverlay(firstName) {
    const overlay = document.createElement('div');
    overlay.id = 'aurum-welcome-overlay';

    overlay.innerHTML = `
      <div class="wc-orb wc-orb-a"></div>
      <div class="wc-orb wc-orb-b"></div>

      <div class="wc-content">
        <!-- Logo mark -->
        <svg class="wc-logo-mark" viewBox="0 0 52 52" fill="none">
          <path d="M6 44L16 12L22 27L28 12L38 44"
                stroke="rgba(184,150,64,0.28)" stroke-width="1.4" stroke-linecap="round"/>
          <path d="M9 44L19 14L22 21.5"
                stroke="rgba(184,150,64,0.14)" stroke-width="1" stroke-linecap="round"/>
          <path d="M11 44L21 14L26 26"
                stroke="#d4b85a" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M26 26L29 14L41 44"
                stroke="#b89640" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>

        <!-- App name -->
        <div class="wc-app-name">Aurum</div>

        <!-- Divider -->
        <div class="wc-divider"></div>

        <!-- Greeting -->
        <div class="wc-greeting">Welcome back</div>

        <!-- Headline -->
        <div class="wc-headline">
          Hey, <span class="wc-name-gold">${_escapeHtml(firstName)}</span>.<br>Your music awaits.
        </div>

        <!-- Perks -->
        <div class="wc-perks">
          <div class="wc-perk">
            <div class="wc-perk-icon">
              <svg viewBox="0 0 24 24">
                <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
              </svg>
            </div>
            <div class="wc-perk-text">
              <div class="wc-perk-title">AI-Powered Picks</div>
              <div class="wc-perk-sub">Music that understands your mood</div>
            </div>
          </div>
          <div class="wc-perk">
            <div class="wc-perk-icon">
              <svg viewBox="0 0 24 24">
                <line x1="8" y1="6" x2="21" y2="6"/>
                <line x1="8" y1="12" x2="21" y2="12"/>
                <line x1="8" y1="18" x2="21" y2="18"/>
                <line x1="3" y1="6" x2="3.01" y2="6"/>
                <line x1="3" y1="12" x2="3.01" y2="12"/>
                <line x1="3" y1="18" x2="3.01" y2="18"/>
              </svg>
            </div>
            <div class="wc-perk-text">
              <div class="wc-perk-title">Unlimited Queue</div>
              <div class="wc-perk-sub">Never run out of songs</div>
            </div>
          </div>
          <div class="wc-perk">
            <div class="wc-perk-icon">
              <svg viewBox="0 0 24 24">
                <path d="M12 2L2 7l10 5 10-5-10-5z"/>
                <path d="M2 17l10 5 10-5M2 12l10 5 10-5"/>
              </svg>
            </div>
            <div class="wc-perk-text">
              <div class="wc-perk-title">Cross-Device Sync</div>
              <div class="wc-perk-sub">Mobile, TV, everywhere</div>
            </div>
          </div>
        </div>

        <!-- Tagline -->
        <div class="wc-tagline">Your music, elevated.</div>
      </div>

      <!-- Dismiss chevron -->
      <div class="wc-dismiss-ring" id="wc-dismiss-ring">
        <div class="wc-dismiss-ring-inner" id="wc-dismiss-inner">
          <svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
        </div>
      </div>
    `;

    return overlay;
  }

  // ── Orchestrate the reveal sequence ──────────────────────────────────────
  function _runRevealSequence(overlay, onDone) {
    const orbs    = overlay.querySelectorAll('.wc-orb');
    const logo    = overlay.querySelector('.wc-logo-mark');
    const appName = overlay.querySelector('.wc-app-name');
    const divider = overlay.querySelector('.wc-divider');
    const greeting= overlay.querySelector('.wc-greeting');
    const headline= overlay.querySelector('.wc-headline');
    const perks   = overlay.querySelectorAll('.wc-perk');
    const tagline = overlay.querySelector('.wc-tagline');
    const dismissRing = overlay.querySelector('#wc-dismiss-ring');

    // t=0: orbs fade in
    orbs.forEach(o => { o.style.opacity = ''; });

    // t=120ms: logo animates in
    setTimeout(() => { if (logo) logo.classList.add('visible'); }, 120);

    // t=300ms: app name
    setTimeout(() => { if (appName) appName.classList.add('visible'); }, 300);

    // t=480ms: divider draws
    setTimeout(() => { if (divider) divider.classList.add('visible'); }, 480);

    // t=680ms: greeting
    setTimeout(() => { if (greeting) greeting.classList.add('visible'); }, 680);

    // t=820ms: headline
    setTimeout(() => { if (headline) headline.classList.add('visible'); }, 820);

    // t=1100ms, 1380ms, 1640ms: perks stagger in
    const perkDelays = [1100, 1360, 1600];
    perks.forEach((perk, i) => {
      setTimeout(() => { perk.classList.add('visible'); }, perkDelays[i] || 1600 + i * 200);
    });

    // t=2000ms: tagline
    setTimeout(() => { if (tagline) tagline.classList.add('visible'); }, 2000);

    // t=2300ms: dismiss ring appears
    setTimeout(() => { if (dismissRing) dismissRing.classList.add('visible'); }, 2300);

    // t=2800ms: auto-dismiss
    const autoTimer = setTimeout(() => { _exitOverlay(overlay, onDone); }, 2800);

    // Manual dismiss on tap anywhere
    function _onTap(e) {
      // Only allow dismiss after headline is visible (t>820ms)
      if (!headline || !headline.classList.contains('visible')) return;
      e.stopPropagation();
      clearTimeout(autoTimer);
      _exitOverlay(overlay, onDone);
    }
    overlay.addEventListener('touchend', _onTap, { passive: true });
    overlay.addEventListener('click', _onTap);

    // Dismiss ring click / tap
    const dismissInner = overlay.querySelector('#wc-dismiss-inner');
    if (dismissInner) {
      dismissInner.addEventListener('touchend', e => {
        e.stopPropagation();
        clearTimeout(autoTimer);
        _exitOverlay(overlay, onDone);
      }, { passive: true });
    }
  }

  // ── Smooth exit ───────────────────────────────────────────────────────────
  function _exitOverlay(overlay, onDone) {
    if (overlay._exiting) return;
    overlay._exiting = true;
    overlay.classList.add('wc-exit');
    setTimeout(() => {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      if (typeof onDone === 'function') onDone();
    }, 560);
  }

  // ── Safe name extraction ──────────────────────────────────────────────────
  function _getFirstName(user) {
    if (!user) return 'there';
    const name = user.given_name || user.name || user.displayName || '';
    const firstName = name.split(' ')[0].trim();
    return firstName.length > 0 && firstName.length < 22 ? firstName : 'there';
  }

  function _escapeHtml(s) {
    return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ── Main entry point ──────────────────────────────────────────────────────
  function showWelcome(user, onDone) {
    // Only show once per browser session (first login)
    try {
      if (localStorage.getItem(WELCOME_KEY) === '1') {
        if (typeof onDone === 'function') onDone();
        return;
      }
      localStorage.setItem(WELCOME_KEY, '1');
    } catch(e) {}

    _injectStyles();

    const firstName = _getFirstName(user);
    const overlay   = _buildOverlay(firstName);

    // Mount into app container (respects max-width)
    const app = document.getElementById('app') || document.body;
    app.appendChild(overlay);

    // Give browser one frame to paint before starting animations
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        _runRevealSequence(overlay, onDone);
      });
    });
  }

})();
// ════════════════════════════════════════════════════════════════════════════
// END AURUM WELCOME v1.0
// ════════════════════════════════════════════════════════════════════════════
