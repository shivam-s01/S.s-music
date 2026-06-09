# sources.py — Saavn mirrors, Piped, Invidious, SoundCloud instance management
# Imported by: fetchers.py, server.py
import re
import os
import time
import logging
import threading
import requests
from concurrent.futures import as_completed
from typing import Dict
from core import (
    log, _executor_bg, _LRUCache,
    SUPABASE_URL, SUPABASE_KEY
)

# ═══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE SOURCE HEALTH SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════
_QUARANTINE_SECS       = 60
_REP_RECOVERY_SECS     = 30
_REP_FAIL_COST         = 6
_REP_MIN_FOR_TRAFFIC   = 15
_ADAPTIVE_TIMEOUT_BASE = 2.0
_ADAPTIVE_TIMEOUT_MAX  = 4.0

class _SourceHealth:
    def __init__(self):
        self._data: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def _get(self, url: str) -> dict:
        if url not in self._data:
            self._data[url] = {
                'fails': 0, 'successes': 0, 'consecutive_ok': 0,
                'last_fail': 0.0, 'last_ok': 0.0, 'avg_ms': 500.0,
                'quarantined': False, 'quarantine_ts': 0.0, 'total_hits': 0,
            }
        return self._data[url]

    def record_ok(self, url: str, elapsed_ms: float = 0.0):
        with self._lock:
            h = self._get(url)
            h['successes'] += 1; h['consecutive_ok'] += 1
            h['last_ok'] = time.time(); h['total_hits'] += 1
            h['fails'] = max(0, h['fails'] - 1)
            if elapsed_ms > 0:
                h['avg_ms'] = h['avg_ms'] * 0.75 + elapsed_ms * 0.25
            if h['quarantined'] and h['consecutive_ok'] >= 3:
                h['quarantined'] = False
                log.info(f'[Health] ✓ Recovered: {url[:50]}')

    def record_fail(self, url: str):
        with self._lock:
            h = self._get(url)
            h['fails'] += 1; h['consecutive_ok'] = 0
            h['last_fail'] = time.time(); h['total_hits'] += 1
            if self.reputation(url, locked=True) < _REP_MIN_FOR_TRAFFIC:
                h['quarantined'] = True; h['quarantine_ts'] = time.time()
                log.warning(f'[Health] ⚠ Quarantined: {url[:50]}')

    def reputation(self, url: str, locked: bool = False) -> float:
        h = self._data.get(url, {})
        if not h: return 50.0
        fails = h.get('fails', 0); last_ok = h.get('last_ok', 0.0)
        avg_ms = h.get('avg_ms', 500.0); hits = h.get('total_hits', 0)
        succ = h.get('successes', 0)
        age_ok = time.time() - last_ok if last_ok else 9999.0
        sr = (succ / max(hits, 1)) if hits else 0.5
        rep = 100.0
        rep -= min(fails * _REP_FAIL_COST, 60)
        rep -= min(age_ok / 60.0, 40.0)
        rep -= min(avg_ms / 100.0, 30.0)
        rep += sr * 10.0
        return max(0.0, rep)

    def is_alive(self, url: str) -> bool:
        with self._lock:
            h = self._get(url)
            if h.get('quarantined', False):
                if time.time() - h.get('quarantine_ts', 0) > _REP_RECOVERY_SECS:
                    return True
                return False
            return True

    def adaptive_timeout(self, url: str) -> float:
        with self._lock:
            avg_ms = self._data.get(url, {}).get('avg_ms', 500.0)
        t = max(_ADAPTIVE_TIMEOUT_BASE, avg_ms / 1000.0 * 2.5)
        return min(t, _ADAPTIVE_TIMEOUT_MAX)

    def sort_by_reputation(self, urls: list) -> list:
        return sorted(urls, key=lambda u: self.reputation(u), reverse=True)

    def summary(self, url: str) -> dict:
        with self._lock:
            h = self._get(url)
        return {
            'url': url, 'reputation': round(self.reputation(url), 1),
            'avg_ms': round(h.get('avg_ms', 0)), 'fails': h.get('fails', 0),
            'quarantined': h.get('quarantined', False),
            'status': ('quarantined' if h.get('quarantined') else
                       'ok' if self.reputation(url) >= 60 else 'degraded'),
        }


_health = _SourceHealth()

def _health_record_ok(url: str, elapsed_ms: float = 0):
    _health.record_ok(url, elapsed_ms)

def _health_record_fail(url: str):
    _health.record_fail(url)

def _health_score(url: str) -> float:
    return _health.reputation(url)

def _is_source_alive(url: str) -> bool:
    return _health.is_alive(url)


# ═══════════════════════════════════════════════════════════════════════════════
# SAAVN MIRRORS
# [FIX] Vercel + Railway mirrors PEHLE — ye always-on hain, spin down nahi hote
# Render mirrors BAAD mein — ye free tier pe so jaate hain (50s delay)
# ═══════════════════════════════════════════════════════════════════════════════
_BASE_MIRRORS = [
    # ── TUMHARA APNA INSTANCE — SABSE PEHLE ──────────────────────────────────
    'https://jiosaavn-op.onrender.com',   # Shivam ka apna — alive confirmed
    # ── ALWAYS-ON (Vercel + Railway) — pehle try karo ────────────────────────
    'https://jiosaavn-api-privatecvc2.vercel.app',
    'https://saavn-api-sigma.vercel.app',
    'https://jiosaavn-api2.vercel.app',
    'https://jiosaavn-api-ts.vercel.app',
    'https://saavn-api-eight.vercel.app',
    'https://jiosaavn-api.vercel.app',
    'https://saavn-api-three.vercel.app',
    'https://jiosaavn-api-production.up.railway.app',
    'https://saavn-api-ruby.vercel.app',
    'https://jiosaavn.vercel.app',
    'https://saavn-api-blond.vercel.app',
    'https://jiosaavn-api-five.vercel.app',
    'https://saavn-api-nu.vercel.app',
    'https://jiosaavn-api-six.vercel.app',
    'https://jiosaavn-api-nine.vercel.app',
    'https://jiosaavn-api-smoky.vercel.app',
    'https://saavn-api-seven.vercel.app',
    'https://jiosaavn-api-seven.vercel.app',
    'https://saavn-api-two.vercel.app',
    # ── RENDER (free tier — spin down hote hain, last resort) ────────────────
    'https://jiosaavn-op.onrender.com',
    'https://jio-saavn-api.onrender.com',
    'https://my-jiosaavn-api.onrender.com',
    'https://saavn-backend.onrender.com',
    'https://jiosaavn-api-node.onrender.com',
]

SAAVN_MIRRORS   = list(_BASE_MIRRORS)
_mirror_lock    = threading.Lock()
_discovered_set = set(_BASE_MIRRORS)

_mirror_fail_count   = {}
_mirror_fail_time    = {}
MIRROR_FAIL_COOLDOWN = 30

def _mirror_ok(mirror):
    if not _health.is_alive(mirror): return False
    with _mirror_lock:
        fails     = _mirror_fail_count.get(mirror, 0)
        last_fail = _mirror_fail_time.get(mirror, 0)
    if fails < 3: return True
    if time.time() - last_fail > MIRROR_FAIL_COOLDOWN:
        with _mirror_lock:
            _mirror_fail_count[mirror] = 0
        return True
    return False

def _mirror_failed(mirror):
    with _mirror_lock:
        _mirror_fail_count[mirror] = _mirror_fail_count.get(mirror, 0) + 1
        _mirror_fail_time[mirror]  = time.time()
        dead_count = sum(1 for m in SAAVN_MIRRORS if _mirror_fail_count.get(m, 0) >= 5)
        _do_heal   = dead_count >= max(1, len(SAAVN_MIRRORS) // 2)
    _health.record_fail(mirror)
    if _do_heal:
        _maybe_reactive_heal('saavn')

def _best_mirrors(n: int = 8) -> list:
    with _mirror_lock:
        alive = [m for m in SAAVN_MIRRORS if _mirror_ok(m)]
    if not alive:
        with _mirror_lock:
            alive = list(SAAVN_MIRRORS)
    return _health.sort_by_reputation(alive)[:n]


# ═══════════════════════════════════════════════════════════════════════════════
# MIRROR DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════
_DISCOVERY_PATTERNS = ['jiosaavn-api', 'saavn-api', 'jiosaavn', 'saavn', 'jio-saavn', 'saavnapi', 'jiosaavnapi']
_DISCOVERY_SUFFIXES = ['', '-v2', '-v3', '-v4', '-new', '-prod', '-main', '-app', '-api', '-server',
                       '-backend', '-public', '-open', '-free', '-node', '-express', '-privatecvc',
                       '-privatecvc2', '-privatecvc3', '-one', '-two', '-three', '-four', '-five',
                       '-six', '-seven', '-eight', '-nine', '-ten']
_DISCOVERY_PREFIXES = ['', 'the-', 'my-', 'open-', 'free-', 'public-']
_DISCOVERY_HOSTS    = ['.vercel.app', '.up.railway.app', '.onrender.com']

def _test_mirror_working(url: str) -> bool:
    for endpoint in ['/api/search/songs', '/api/search', '/search/songs']:
        try:
            t0 = time.time()
            r  = requests.get(f'{url}{endpoint}',
                              params={'query': 'arijit singh', 'q': 'arijit singh', 'limit': 2},
                              timeout=5, headers={'User-Agent': 'Mozilla/5.0'})
            elapsed = (time.time() - t0) * 1000
            if r.status_code != 200: continue
            data    = r.json()
            results = (data.get('data', {}).get('results') or data.get('results') or
                       data.get('songs', {}).get('results') or [])
            if results and len(results) > 0:
                _health.record_ok(url, elapsed)
                return True
        except Exception:
            continue
    _health.record_fail(url)
    return False

def _discover_mirrors():
    global SAAVN_MIRRORS
    log.info('[Discovery] Starting Saavn mirror scan...')
    candidates = []
    for pattern in _DISCOVERY_PATTERNS:
        for prefix in _DISCOVERY_PREFIXES:
            for suffix in _DISCOVERY_SUFFIXES:
                for host in _DISCOVERY_HOSTS:
                    url = f'https://{prefix}{pattern}{suffix}{host}'
                    with _mirror_lock:
                        if url not in _discovered_set:
                            candidates.append(url)
    log.info(f'[Discovery] Testing {len(candidates)} candidates...')
    new_found = []
    futures   = {_executor_bg.submit(_test_mirror_working, url): url for url in candidates}
    try:
        for future in as_completed(futures, timeout=30):
            url = futures[future]
            try:
                if future.result():
                    with _mirror_lock:
                        if url not in _discovered_set:
                            _discovered_set.add(url)
                            new_found.append(url)
                            log.info(f'[Discovery] ✓ New mirror: {url}')
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[Discovery] Timeout: {e}')
    if new_found:
        with _mirror_lock:
            SAAVN_MIRRORS = list(_discovered_set)
        log.info(f'[Discovery] Added {len(new_found)} mirrors. Total: {len(SAAVN_MIRRORS)}')

def _verify_existing_mirrors():
    global SAAVN_MIRRORS
    to_remove = []
    with _mirror_lock:
        current = list(SAAVN_MIRRORS)
    for url in current:
        if _mirror_fail_count.get(url, 0) >= 15:
            if not _test_mirror_working(url):
                to_remove.append(url)
            else:
                _mirror_fail_count[url] = 0
    if to_remove:
        with _mirror_lock:
            for url in to_remove:
                if url in SAAVN_MIRRORS:
                    SAAVN_MIRRORS.remove(url)
                _discovered_set.discard(url)
        _executor_bg.submit(_discover_mirrors)


# ═══════════════════════════════════════════════════════════════════════════════
# PIPED INSTANCES
# ═══════════════════════════════════════════════════════════════════════════════
_BASE_PIPED = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.tokhmi.xyz',
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
    'https://pipedapi.reallyaweso.me',
    'https://pipedapi.in.projectsegfau.lt',
]
PIPED_INSTANCES = list(_BASE_PIPED)
_piped_lock     = threading.Lock()
_piped_known    = set(_BASE_PIPED)

def _test_piped_instance(url: str) -> bool:
    try:
        t0 = time.time()
        r  = requests.get(f'{url}/search',
                          params={'q': 'arijit singh', 'filter': 'music_songs'},
                          timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200 and r.json().get('items'):
            _health.record_ok(url, elapsed)
            return True
    except Exception:
        pass
    _health.record_fail(url)
    return False

def _heal_piped():
    global PIPED_INSTANCES
    try:
        r = requests.get('https://piped-instances.kavin.rocks/', timeout=10,
                         headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            instances      = r.json()
            new_candidates = [inst.get('api_url', '').rstrip('/') for inst in instances
                              if inst.get('api_url', '').rstrip('/') not in _piped_known]
            futures = {_executor_bg.submit(_test_piped_instance, u): u for u in new_candidates if u}
            try:
                for future in as_completed(futures, timeout=30):
                    url = futures[future]
                    try:
                        if future.result():
                            with _piped_lock:
                                if url not in _piped_known:
                                    _piped_known.add(url)
                                    PIPED_INSTANCES.append(url)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[SelfHeal:Piped] {e}')
    dead = [url for url in PIPED_INSTANCES
            if _health.reputation(url) < _REP_MIN_FOR_TRAFFIC
            and not _test_piped_instance(url)]
    if dead:
        with _piped_lock:
            for url in dead:
                if url in PIPED_INSTANCES:
                    PIPED_INSTANCES.remove(url)


# ═══════════════════════════════════════════════════════════════════════════════
# INVIDIOUS INSTANCES
# ═══════════════════════════════════════════════════════════════════════════════
_BASE_INVIDIOUS = [
    'https://invidious.snopyta.org',
    'https://vid.puffyan.us',
    'https://invidious.kavin.rocks',
    'https://y.com.sb',
    'https://invidious.nerdvpn.de',
]
INVIDIOUS_INSTANCES = list(_BASE_INVIDIOUS)
_invidious_lock     = threading.Lock()
_invidious_known    = set(_BASE_INVIDIOUS)

def _test_invidious_instance(url: str) -> bool:
    try:
        t0 = time.time()
        r  = requests.get(f'{url}/api/v1/search',
                          params={'q': 'arijit singh', 'type': 'video', 'page': 1},
                          timeout=7, headers={'User-Agent': 'Mozilla/5.0'})
        elapsed = (time.time() - t0) * 1000
        if r.status_code == 200 and isinstance(r.json(), list) and len(r.json()) > 0:
            _health.record_ok(url, elapsed)
            return True
    except Exception:
        pass
    _health.record_fail(url)
    return False

def _heal_invidious():
    global INVIDIOUS_INSTANCES
    try:
        r = requests.get('https://api.invidious.io/instances.json',
                         params={'sort_by': 'health'}, timeout=10,
                         headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 200:
            new_candidates = [inst[1].get('uri', '').rstrip('/') for inst in r.json()
                              if isinstance(inst, list) and len(inst) >= 2
                              and inst[1].get('uri', '').startswith('https')
                              and inst[1].get('api', False)
                              and inst[1].get('uri', '').rstrip('/') not in _invidious_known]
            futures = {_executor_bg.submit(_test_invidious_instance, u): u for u in new_candidates[:20] if u}
            try:
                for future in as_completed(futures, timeout=40):
                    url = futures[future]
                    try:
                        if future.result():
                            with _invidious_lock:
                                if url not in _invidious_known:
                                    _invidious_known.add(url)
                                    INVIDIOUS_INSTANCES.append(url)
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        log.warning(f'[SelfHeal:Invidious] {e}')


# ═══════════════════════════════════════════════════════════════════════════════
# SOUNDCLOUD CLIENT ID AUTO-REFRESH
# ═══════════════════════════════════════════════════════════════════════════════
SOUNDCLOUD_CLIENT_ID     = os.environ.get('SOUNDCLOUD_CLIENT_ID', 'a3e059563d7fd3372b49b37f00a00bcf')
_sc_client_id_lock       = threading.Lock()
_sc_client_id_last_check = 0
_SC_ID_REFRESH_INTERVAL  = 3600

def _refresh_soundcloud_client_id():
    global SOUNDCLOUD_CLIENT_ID
    try:
        r = requests.get('https://soundcloud.com', timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code != 200: return
        script_urls = re.findall(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', r.text)
        for script_url in script_urls[-5:]:
            try:
                sr = requests.get(script_url, timeout=8, headers={'User-Agent': 'Mozilla/5.0'})
                if sr.status_code != 200: continue
                match = re.search(r'client_id\s*[:=]\s*["\']([a-zA-Z0-9]{32})["\']', sr.text)
                if match:
                    new_id = match.group(1)
                    with _sc_client_id_lock:
                        if new_id != SOUNDCLOUD_CLIENT_ID:
                            log.info(f'[SelfHeal:SC] Client ID refreshed: {new_id[:8]}...')
                            SOUNDCLOUD_CLIENT_ID = new_id
                    return
            except Exception:
                continue
    except Exception as e:
        log.warning(f'[SelfHeal:SC] {e}')

def _maybe_refresh_sc_id():
    global _sc_client_id_last_check
    now = time.time()
    if now - _sc_client_id_last_check > _SC_ID_REFRESH_INTERVAL:
        _sc_client_id_last_check = now
        _executor_bg.submit(_refresh_soundcloud_client_id)


# ═══════════════════════════════════════════════════════════════════════════════
# MASTER HEAL LOOP
# ═══════════════════════════════════════════════════════════════════════════════
_reactive_heal_cooldown      = {}
_reactive_heal_cooldown_lock = threading.Lock()
_REACTIVE_COOLDOWN_S         = 120

def _maybe_reactive_heal(source_type: str):
    now = time.time()
    with _reactive_heal_cooldown_lock:
        last = _reactive_heal_cooldown.get(source_type, 0)
        if now - last < _REACTIVE_COOLDOWN_S:
            return
        _reactive_heal_cooldown[source_type] = now
    log.info(f'[SelfHeal:Reactive] Triggering heal for {source_type}')
    if source_type == 'saavn':
        _executor_bg.submit(_discover_mirrors)
        _executor_bg.submit(_verify_existing_mirrors)
    elif source_type == 'piped':
        _executor_bg.submit(_heal_piped)
    elif source_type == 'invidious':
        _executor_bg.submit(_heal_invidious)
    elif source_type == 'soundcloud':
        _executor_bg.submit(_refresh_soundcloud_client_id)

_HEAL_MIN_SAAVN_MIRRORS  = 5
_HEAL_MIN_PIPED          = 2
_HEAL_MIN_INVIDIOUS      = 2
_HEAL_EMERGENCY_INTERVAL = 120
_HEAL_FULL_INTERVAL      = 3600

_last_emergency_heal: Dict[str, float] = {}
_emergency_heal_lock = threading.Lock()

def _should_emergency_heal(source: str, interval: float = _HEAL_EMERGENCY_INTERVAL) -> bool:
    now = time.time()
    with _emergency_heal_lock:
        if now - _last_emergency_heal.get(source, 0) > interval:
            _last_emergency_heal[source] = now
            return True
    return False

def _count_alive_mirrors() -> int:
    with _mirror_lock:
        return sum(1 for m in SAAVN_MIRRORS if _mirror_ok(m))

def _count_alive_piped() -> int:
    with _piped_lock:
        return sum(1 for p in PIPED_INSTANCES if _health.is_alive(p))

def _count_alive_invidious() -> int:
    with _invidious_lock:
        return sum(1 for i in INVIDIOUS_INSTANCES if _health.is_alive(i))

def _strong_heal_saavn():
    log.info('[StrongHeal:Saavn] Starting...')
    with _mirror_lock:
        current = list(SAAVN_MIRRORS)
    probe_futures = {_executor_bg.submit(_test_mirror_working, m): m for m in current}
    dead, recovered = [], []
    try:
        for future in as_completed(probe_futures, timeout=20):
            m = probe_futures[future]
            try:
                alive = future.result()
                if alive:
                    recovered.append(m)
                    _mirror_fail_count[m] = 0
                elif _mirror_fail_count.get(m, 0) >= 20:
                    dead.append(m)
            except Exception:
                pass
    except Exception:
        pass
    if dead:
        with _mirror_lock:
            for m in dead:
                if m not in _BASE_MIRRORS and m in SAAVN_MIRRORS:
                    SAAVN_MIRRORS.remove(m)
                    _discovered_set.discard(m)
        log.info(f'[StrongHeal:Saavn] Removed {len(dead)} dead mirrors')
    for m in recovered:
        h = _health._data.get(m, {})
        if h.get('quarantined'):
            h['quarantined'] = False
            h['consecutive_ok'] = 3
            log.info(f'[StrongHeal:Saavn] ✓ Un-quarantined: {m[:50]}')
    alive_count = _count_alive_mirrors()
    if alive_count < _HEAL_MIN_SAAVN_MIRRORS:
        log.info(f'[StrongHeal:Saavn] Only {alive_count} alive, discovering new...')
        _discover_mirrors()
    else:
        log.info(f'[StrongHeal:Saavn] ✓ {alive_count} mirrors alive — OK')

def _strong_heal_piped():
    log.info('[StrongHeal:Piped] Starting...')
    with _piped_lock:
        current = list(PIPED_INSTANCES)
    for inst in current:
        if not _health.is_alive(inst):
            if _test_piped_instance(inst):
                h = _health._data.get(inst, {})
                h['quarantined'] = False; h['consecutive_ok'] = 3
    if _count_alive_piped() < _HEAL_MIN_PIPED:
        _heal_piped()

def _strong_heal_invidious():
    log.info('[StrongHeal:Invidious] Starting...')
    with _invidious_lock:
        current = list(INVIDIOUS_INSTANCES)
    for inst in current:
        if not _health.is_alive(inst):
            if _test_invidious_instance(inst):
                h = _health._data.get(inst, {})
                h['quarantined'] = False; h['consecutive_ok'] = 3
    if _count_alive_invidious() < _HEAL_MIN_INVIDIOUS:
        _heal_invidious()

def _master_heal_loop():
    time.sleep(20)
    last_full_heal = 0.0
    while True:
        try:
            now = time.time()
            saavn_alive = _count_alive_mirrors()
            piped_alive = _count_alive_piped()
            inv_alive   = _count_alive_invidious()
            if saavn_alive < _HEAL_MIN_SAAVN_MIRRORS:
                if _should_emergency_heal('saavn'):
                    log.warning(f'[StrongHeal] ⚠ Emergency: only {saavn_alive} Saavn mirrors!')
                    _executor_bg.submit(_strong_heal_saavn)
            if piped_alive < _HEAL_MIN_PIPED:
                if _should_emergency_heal('piped'):
                    _executor_bg.submit(_strong_heal_piped)
            if inv_alive < _HEAL_MIN_INVIDIOUS:
                if _should_emergency_heal('invidious'):
                    _executor_bg.submit(_strong_heal_invidious)
            if now - last_full_heal > _HEAL_FULL_INTERVAL:
                last_full_heal = now
                log.info('[StrongHeal] Starting scheduled full heal cycle...')
                _executor_bg.submit(_strong_heal_saavn)
                _executor_bg.submit(_strong_heal_piped)
                _executor_bg.submit(_strong_heal_invidious)
                _executor_bg.submit(_refresh_soundcloud_client_id)
                log.info(f'[StrongHeal] ✓ Scheduled — Saavn:{saavn_alive} Piped:{piped_alive} Inv:{inv_alive}')
        except Exception as e:
            log.error(f'[StrongHeal] Master loop error: {e}')
        time.sleep(60)

threading.Thread(target=_master_heal_loop, daemon=True).start()
log.info('[StrongHeal] Strong master heal loop started (60s interval)')
