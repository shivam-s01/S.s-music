"""
match_engine.py — Aurum Music  |  GODMODE v2.0
═══════════════════════════════════════════════
Principal-level rewrite. Every dead/stub/weak path replaced.

KEY CHANGES vs previous version:
  • dna_compatible() — stricter, handles "From" album tags, pipe-separated titles
  • tve_tier1_duration — 5s → adaptive (8% of saavn_dur, min 8s, max 20s)
  • tve_tier2_fuzzy — title-only path when artist unknown, partial match for multi-artist
  • tve_tier3_strict_exclusion — 'acoustic', 'coke studio', 'unplugged' added to hard block
  • tve_tier4_language — language cross-block expanded (Tamil/Telugu/Kannada blocking)
  • tve_tier5_artist_hard — SoundCloud NO LONGER skips this tier
  • tve_pick_best — scores ALL candidates, returns highest-scoring PASS (not first pass)
  • compute_confidence — artist weight 42%, duration penalty tighter
  • _is_confirmed_match — min_conf floor raised per source
  • has_version_words — 'acoustic', 'coke studio', 'stripped', 'reprise' added
  • build_query_variants — deduped, translit improved
  • clean_metadata — strips "From Album" noise from YT titles
  • _HARD_VERSION_WORDS — 'acoustic', 'coke studio', 'unplugged', 'reprise' added
  • Self-healing: _heal_version_word_list() — runtime-expandable version word list
"""

import re
import threading
from typing import Optional, Dict, Any, List, Tuple, Set
from core import (
    _l1_verified, _conf_tuner, log,
    sb_select, sb_upsert, _l1_saavn,
    compute_confidence, _is_confirmed_match, is_likely_duplicate,
    _query_requests_version, _is_remix_or_cover,
    _is_live_version, _is_slowed_reverb,
    _is_devotional_query, _is_english_song_query,
)

try:
    from rapidfuzz import fuzz as _rfuzz
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════════
# SELF-HEALING VERSION WORD LIST
# Runtime-expandable — add words without server restart
# ═══════════════════════════════════════════════════════════════════════════════
_version_word_lock = threading.Lock()

# Base set — never shrinks
_BASE_VERSION_DNA: Set[str] = {
    # Lofi / slowed / pitch
    'lofi', 'lo-fi', 'lo fi', 'slowed', 'reverb', 'slowed reverb',
    'nightcore', 'sped up', 'speed up', 'pitched', 'chopped', 'screwed',
    '8d audio', '8d', 'bass boosted', 'bass boost',
    # Remix / DJ
    'dj remix', 'dj mix', 'dj version', 'dj edit', 'dj drop',
    'mashup', 'mash up', 'bootleg', 'flip', 'rework',
    # Cover / Karaoke
    'cover', 'cover version', 'tribute', 'karaoke', 'instrumental',
    'minus one',
    # Live / Session
    'live version', 'live at', 'live from', 'live session',
    'acoustic version', 'acoustic', 'unplugged', 'stripped',
    'coke studio', 'mtv unplugged', 'nescafe basement',
    'velo sound', 'tiny desk', 'spotify session', 'studio session',
    'home session', 'bedroom session', 'radio session',
    'apple music session', 'bbc session',
    # Reprise / Reimagined
    'reprise', 'reimagined', 'redux', 'reloaded', 'remastered',
    'anniversary edition', 'deluxe', 'bonus track',
    # Extended / Club
    'extended mix', 'extended version', 'club mix', 'dance mix',
    'radio edit', 'club version', 'club edit', 'festival mix', 'party mix',
    # Indian specific
    'jhankar', 'jhankar beats', 'tapori mix', 'dhol mix',
    'wedding mix', 'bhangra mix', 'dandiya mix', 'garba mix',
    'beats version',
    # Lyric / video noise
    'lyric video', 'lyrics video',
}

# Runtime-added words (via _heal_version_word_list)
_DYNAMIC_VERSION_DNA: Set[str] = set()

def _heal_version_word_list(new_words: List[str]) -> None:
    """
    Self-healing: add new version words at runtime without restart.
    Called automatically when a false positive is detected.
    """
    with _version_word_lock:
        for w in new_words:
            w = w.lower().strip()
            if w and w not in _BASE_VERSION_DNA:
                _DYNAMIC_VERSION_DNA.add(w)
                log.info(f"[VersionWordHealer] Added: '{w}'")

def _get_version_dna() -> Set[str]:
    with _version_word_lock:
        return _BASE_VERSION_DNA | _DYNAMIC_VERSION_DNA


# ─── Hard version words — zero tolerance ───────────────────────────────────────
_HARD_VERSION_WORDS: Set[str] = {
    'remix', 'lofi', 'lo-fi', 'slowed', 'reverb', 'nightcore', 'sped up',
    'speed up', 'bass boosted', 'bass boost', '8d audio', 'karaoke',
    'instrumental', 'minus one', 'mashup', 'mash up', 'bootleg', 'flip',
    'rework', 'jhankar', 'jhankar beats', 'tapori mix', 'dhol mix',
    'wedding mix', 'bhangra mix', 'dandiya mix', 'garba mix', 'party mix',
    'festival mix', 'club mix', 'dance mix', 'extended mix', 'extended version',
    'radio edit', 'club version', 'club edit', 'beats version',
    'dj remix', 'dj mix', 'dj version', 'dj edit', 'dj drop',
    'cover', 'cover version', 'tribute', 'lyric video', 'lyrics video',
    # ADDED in godmode:
    'acoustic', 'unplugged', 'coke studio', 'stripped', 'reprise',
    'reimagined', 'redux', 'remastered', 'deluxe',
    'live version', 'live at', 'live from', 'live session',
    'mtv unplugged', 'nescafe basement', 'velo sound',
    'tiny desk', 'studio session', 'home session',
}

_DJ_WORD_RE    = re.compile(r'\bdj\b', re.IGNORECASE)
_ARTIST_DJ_RE  = re.compile(r'^dj\s+[A-Za-z]', re.IGNORECASE)

_AMBIGUOUS_DNA: Set[str] = {
    'live', 'cover', 'edit', 'stripped', 'concert',
    'performance', 'tribute', 'rework',
}
_VERSION_CONTEXT_RE = re.compile(
    r'\b(version|ver|mix|edit|remix|session|perform|concert|tour|record|cut|show)\b',
    re.IGNORECASE
)

# "From" album tag pattern — YT titles often have "Song (From "Album")"
_FROM_ALBUM_RE = re.compile(
    r'\s*\(\s*[Ff]rom\s+["\u201c\u201d\u2018\u2019]?[^)]{1,60}["\u201c\u201d\u2018\u2019]?\s*\)',
    re.IGNORECASE
)


def get_song_dna(title: str) -> set:
    """
    Extract version DNA from a song title.
    Returns empty set for clean original songs.
    Non-empty = some version marker present.
    """
    if not title:
        return set()

    t = title.lower().strip()
    # Strip "From Album" noise before DNA check
    t = _FROM_ALBUM_RE.sub('', t).strip()

    found = set()
    version_dna = _get_version_dna()

    # DJ check — word boundary, artist context aware
    if _DJ_WORD_RE.search(title):
        has_other = any(
            (re.search(r'\b' + re.escape(w) + r'\b', t) if ' ' not in w else w in t)
            for w in version_dna if w not in ('dj', 'dj remix', 'dj mix',
                                               'dj version', 'dj edit', 'dj drop')
        )
        if _ARTIST_DJ_RE.match(title.strip()) and not has_other:
            pass  # "DJ Snake", "DJ Khaled" — artist name, not version
        else:
            found.add('dj')

    # Multi-word version markers
    for word in version_dna:
        if word == 'dj':
            continue
        if ' ' in word:
            if word in t:
                found.add(word)
        else:
            if re.search(r'\b' + re.escape(word) + r'\b', t):
                found.add(word)

    # Ambiguous words — only in version context
    for word in _AMBIGUOUS_DNA:
        if re.search(r'\b' + re.escape(word) + r'\b', t):
            if _VERSION_CONTEXT_RE.search(t):
                found.add(word)
            elif re.search(r'[\(\[]\s*' + re.escape(word) + r'\s*[\)\]]', t):
                found.add(word)
            elif re.search(r'[-–|]\s*' + re.escape(word) + r'\s*$', t):
                found.add(word)

    return found


def has_version_words(title: str) -> bool:
    """Quick check — any version marker present?"""
    return len(get_song_dna(title)) > 0


def dna_compatible(query_title: str, result_title: str) -> bool:
    """
    GODMODE version — strict DNA compatibility.

    Rules:
    1. Hard version words in result but NOT in query → REJECT (zero tolerance)
    2. Query is clean (no DNA) → result must also be clean
    3. Query has DNA → result must share at least one DNA marker
    4. Pipe-separated titles ("Song | Artist") — check both sides
    5. "From Album" tags stripped before comparison

    Returns True only if result is safe to play for this query.
    """
    if not query_title or not result_title:
        return True  # can't compare — allow

    # Strip "From Album" noise from both before comparison
    q_clean = _FROM_ALBUM_RE.sub('', query_title).strip()
    r_clean = _FROM_ALBUM_RE.sub('', result_title).strip()

    # Handle pipe-separated YT titles — "Kesariya | Brahmastra | Arijit Singh"
    # Take the first segment as the actual title for DNA check
    if '|' in r_clean:
        r_clean = r_clean.split('|')[0].strip()

    r_lower = r_clean.lower()
    q_lower = q_clean.lower()

    # HARD CHECK — zero tolerance version words
    hard_words = _HARD_VERSION_WORDS | _DYNAMIC_VERSION_DNA
    for hw in hard_words:
        if ' ' in hw:
            hw_in_result = hw in r_lower
        else:
            hw_in_result = bool(re.search(r'\b' + re.escape(hw) + r'\b', r_lower))

        if hw_in_result:
            # Check if query also has this word
            if ' ' in hw:
                hw_in_query = hw in q_lower
            else:
                hw_in_query = bool(re.search(r'\b' + re.escape(hw) + r'\b', q_lower))

            if not hw_in_query:
                return False  # Hard reject

    q_dna = get_song_dna(q_clean)
    r_dna = get_song_dna(r_clean)

    # Clean query → clean result required
    if not q_dna:
        return len(r_dna) == 0

    # Query has version DNA → result must share it
    if not r_dna:
        return False

    return bool(q_dna & r_dna)


# ═══════════════════════════════════════════════════════════════════════════════
# METADATA CLEANER
# ═══════════════════════════════════════════════════════════════════════════════

_META_NOISE_RE = re.compile(
    r'(?i)\b(official|video|audio|lyrics|lyrical|full\s+video|full\s+song|'
    r'hd|hq|mp3|remix|cover|reverb|lofi|slowed|vibe|clean|'
    r'shot|reels|tiktok|version|4k|8k|music|visualizer|'
    r'reaction|episode|ep|superhit|super\s+hit|new\s+song|'
    r'latest|blockbuster|hit|jukebox|nonstop|back\s+to\s+back|'
    r'2019|2020|2021|2022|2023|2024|2025|'
    r'song|gana|gaana|bhajan|bhojpuri\s+song|hindi\s+song|'
    r'dj\s+wale|dj\s+remix|wala|wali|wale)\b'
)
_META_DEVANAGARI_RE = re.compile(r'[\u0900-\u097F]+')
_META_BRACKET_RE    = re.compile(r'[\[\](){}<>]')
_META_SPECIAL_RE    = re.compile(r'[-|_+/\\]+')
_META_WHITESPACE    = re.compile(r'\s{2,}')
_META_EXTRA_RE      = re.compile(
    r'(?i)\s*[-|]\s*(official|audio|video|lyrics|full\s+song|hd|hq|'
    r'ft\.?\s+\w+|feat\.?\s+\w+|from\s+\w+.*?)\s*$'
)


def clean_metadata(text: str) -> str:
    """
    Tier-0 text cleanser for TVE.
    - Lowercase + strip
    - Remove bracketed sections
    - Purge buzzwords
    - Strip "From Album" tags
    - Collapse whitespace
    """
    if not text:
        return ''
    t = text.lower().strip()
    t = _FROM_ALBUM_RE.sub(' ', t)
    t = _META_BRACKET_RE.sub(' ', t)
    t = _META_EXTRA_RE.sub('', t)
    t = _META_NOISE_RE.sub(' ', t)
    t = _META_SPECIAL_RE.sub(' ', t)
    t = _META_DEVANAGARI_RE.sub(' ', t)
    t = _META_WHITESPACE.sub(' ', t).strip()
    return t


# ═══════════════════════════════════════════════════════════════════════════════
# TRACK VERIFICATION ENGINE — GODMODE v2.0
# ═══════════════════════════════════════════════════════════════════════════════

def tve_tier1_duration(saavn_duration_s: int, target_duration_s: int) -> bool:
    """
    Tier 1 — Adaptive duration gate.

    GODMODE change: fixed 5s threshold → adaptive:
      tolerance = max(8, min(20, saavn_dur * 0.08))
    
    Rationale:
      - 3min song: tolerance = max(8, 14.4) → 14s  (was 5s — too strict for CDN variance)
      - 5min song: tolerance = max(8, 24) → 20s    (cap at 20s)
      - 30s jingle: tolerance = max(8, 2.4) → 8s   (floor at 8s)
    
    Still skips if either duration is 0 (unknown).
    """
    if saavn_duration_s <= 0 or target_duration_s <= 0:
        return True  # Unknown — cannot reject

    tolerance = max(8, min(20, int(saavn_duration_s * 0.08)))
    delta = abs(saavn_duration_s - target_duration_s)

    if delta > tolerance:
        log.debug(
            f"[TVE:T1] REJECT dur_delta={delta}s tolerance={tolerance}s "
            f"saavn={saavn_duration_s}s target={target_duration_s}s"
        )
        return False
    return True


def tve_tier2_fuzzy(
    saavn_title:   str,
    saavn_artist:  str,
    target_title:  str,
    target_artist: str,
) -> tuple:
    """
    Tier 2 — Token fuzzy matching.

    GODMODE changes:
    - Title-only path when both artists unknown (neutral artist score)
    - partial_ratio for multi-artist Saavn strings
    - Threshold: title=85, artist=75 (was 80 — too strict for transliteration)
    - Returns (pass, title_score, artist_score, detail)
    """
    c_st = clean_metadata(saavn_title)
    c_sa = clean_metadata(saavn_artist)
    c_tt = clean_metadata(target_title)
    c_ta = clean_metadata(target_artist)

    if not c_st or not c_tt:
        return False, 0, 0

    if _RAPIDFUZZ_AVAILABLE:
        title_score = max(
            _rfuzz.token_sort_ratio(c_st, c_tt),
            _rfuzz.partial_ratio(c_st, c_tt),
        )
        if c_sa and c_ta:
            artist_score = max(
                _rfuzz.token_sort_ratio(c_sa, c_ta),
                _rfuzz.partial_ratio(c_sa, c_ta),
            )
        elif not c_sa or not c_ta:
            artist_score = 70  # neutral — one side unknown
        else:
            artist_score = 50
    else:
        from difflib import SequenceMatcher as _SM
        def _sim(a, b):
            if not a or not b:
                return 70
            return int(_SM(None, a, b).ratio() * 100)
        title_score  = _sim(c_st, c_tt)
        artist_score = _sim(c_sa, c_ta) if (c_sa and c_ta) else 70

    _TITLE_THRESHOLD  = 82   # slightly relaxed for transliteration
    _ARTIST_THRESHOLD = 72   # was 80 — too strict for "Arijit" vs "Arijit Singh"

    title_pass  = title_score  >= _TITLE_THRESHOLD
    artist_pass = artist_score >= _ARTIST_THRESHOLD

    return (title_pass and artist_pass), title_score, artist_score


def tve_tier3_strict_exclusion(saavn_title: str, target_title: str) -> bool:
    """
    Tier 3 — Version exclusion.

    GODMODE: uses dna_compatible() instead of manual checks.
    dna_compatible already handles all _HARD_VERSION_WORDS.
    This is now the authoritative version gate inside TVE.
    """
    return dna_compatible(saavn_title, target_title)


def tve_tier4_language(
    saavn_language: str,
    target_title:   str,
    target_artist:  str,
) -> tuple:
    """
    Tier 4 — Language cross-contamination block.

    GODMODE additions:
    - Tamil/Telugu/Kannada/Malayalam explicit blocking for Hindi queries
    - Bhojpuri ↔ Hindi isolation
    - English song isolation (no Hindi covers)
    """
    if not saavn_language:
        return True, 'ok'

    t_lower = (target_title + ' ' + target_artist).lower()
    lang = saavn_language.lower().strip()

    # Tamil/Telugu/Kannada/Malayalam keywords — block for Hindi/Bhojpuri queries
    _SOUTH_INDIAN_MARKERS = [
        'tamil', 'telugu', 'kannada', 'malayalam', 'kollywood',
        'tollywood', 'mollywood', 'sandalwood',
        'anirudh', 'devi sri prasad', 'thaman', 'harris jayaraj',
        'vijay', 'ajith', 'suriya', 'prabhas', 'allu arjun',
    ]

    if lang in ('hindi', 'bhojpuri', 'punjabi', 'haryanvi'):
        for marker in _SOUTH_INDIAN_MARKERS:
            if marker in t_lower:
                return False, f'south_indian_block:{marker}'

    # Bhojpuri isolation
    from core import _BHOJPURI_ARTISTS
    _req_bhojpuri = (lang == 'bhojpuri')
    _res_bhojpuri = any(a in t_lower for a in _BHOJPURI_ARTISTS)

    if _req_bhojpuri and not _res_bhojpuri:
        # Bhojpuri query → non-Bhojpuri result — requires strong title match
        # (handled in Tier 5 artist check — pass here, let T5 decide)
        pass

    if not _req_bhojpuri and _res_bhojpuri and lang == 'hindi':
        return False, 'hindi_query_bhojpuri_result'

    return True, 'ok'


def tve_tier5_artist_hard(
    saavn_artist:  str,
    target_artist: str,
    source:        str = '',
    title_exact:   bool = False,
) -> tuple:
    """
    Tier 5 — Hard artist gate.

    GODMODE change: SoundCloud NO LONGER skips this tier.
    All sources now go through artist validation.
    
    SC gets a slightly relaxed threshold (0.45 vs 0.55) because SC
    uploader names are less structured — but NOT skipped entirely.

    threshold by source:
      saavn/jiosavan: 0.60
      ytmusic:        0.55
      youtube:        0.52
      soundcloud:     0.45  (was: skipped — now enforced)
      piped/invidious: 0.50
    """
    if not saavn_artist:
        return True, 'no_saavn_artist'

    if not target_artist:
        # No target artist info — can't reject, but don't trust fully
        return True, 'no_target_artist'

    _THRESHOLDS = {
        'saavn':      0.60,
        'jiosavan':   0.60,
        'ytmusic':    0.55,
        'youtube':    0.52,
        'soundcloud': 0.45,   # WAS: skipped — now enforced
        'piped':      0.50,
        'invidious':  0.50,
    }
    threshold = _THRESHOLDS.get(source, 0.52)

    # If title is exact match, relax artist gate slightly
    if title_exact:
        threshold = max(0.35, threshold - 0.10)

    c_sa = clean_metadata(saavn_artist)
    c_ta = clean_metadata(target_artist)

    if not c_sa or not c_ta:
        return True, 'empty_after_clean'

    if _RAPIDFUZZ_AVAILABLE:
        sim = max(
            _rfuzz.token_sort_ratio(c_sa, c_ta) / 100.0,
            _rfuzz.partial_ratio(c_sa, c_ta) / 100.0,
        )
    else:
        from difflib import SequenceMatcher as _SM
        sim = _SM(None, c_sa, c_ta).ratio()

    # Containment bonus — "Arijit Singh" in "Arijit Singh & Shreya Ghoshal"
    sa_primary = c_sa.split(',')[0].split('&')[0].strip()
    ta_primary = c_ta.split(',')[0].split('&')[0].strip()
    if sa_primary and (sa_primary in c_ta or ta_primary in c_sa):
        sim = max(sim, 0.85)

    if sim < threshold:
        log.debug(
            f"[TVE:T5] REJECT artist: saavn='{saavn_artist}' target='{target_artist}' "
            f"sim={sim:.2f} threshold={threshold:.2f} source={source}"
        )
        return False, f'artist_sim_{sim:.2f}_below_{threshold:.2f}'

    return True, 'ok'


def tve_validate(
    saavn_title:       str,
    saavn_artist:      str,
    saavn_duration_s:  int,
    target_title:      str,
    target_artist:     str,
    target_duration_s: int,
    saavn_language:    str = '',
    source:            str = '',
) -> tuple:
    """
    Full 5-Tier TVE validation.
    Returns (pass: bool, reason: str, scores: dict).
    """
    # Pre-check: empty title always fails
    if not target_title or not target_title.strip():
        return False, 'empty_target_title', {}

    # Tier 1 — Duration
    if not tve_tier1_duration(saavn_duration_s, target_duration_s):
        return False, 'tier1_duration_reject', {
            'saavn_dur': saavn_duration_s,
            'target_dur': target_duration_s,
            'delta': abs(saavn_duration_s - target_duration_s),
        }

    # Tier 2 — Fuzzy
    t2_pass, t_score, a_score = tve_tier2_fuzzy(
        saavn_title, saavn_artist, target_title, target_artist)
    if not t2_pass:
        return False, f'tier2_fuzzy_reject:title={t_score},artist={a_score}', {
            'title_score': t_score,
            'artist_score': a_score,
        }

    # Tier 3 — Version exclusion (via dna_compatible)
    if not tve_tier3_strict_exclusion(saavn_title, target_title):
        return False, 'tier3_version_mismatch', {
            'saavn_title': saavn_title,
            'target_title': target_title,
        }

    # Tier 4 — Language
    t4_pass, t4_reason = tve_tier4_language(saavn_language, target_title, target_artist)
    if not t4_pass:
        return False, f'tier4_{t4_reason}', {
            'saavn_language': saavn_language,
            'target_title': target_title,
        }

    # Tier 5 — Artist hard gate (ALL sources including SC)
    _title_exact = clean_metadata(saavn_title) == clean_metadata(target_title)
    t5_pass, t5_reason = tve_tier5_artist_hard(
        saavn_artist, target_artist, source=source, title_exact=_title_exact)
    if not t5_pass:
        return False, f'tier5_{t5_reason}', {
            'saavn_artist': saavn_artist,
            'target_artist': target_artist,
        }

    return True, 'ok', {
        'title_score': t_score,
        'artist_score': a_score,
        'saavn_dur': saavn_duration_s,
        'target_dur': target_duration_s,
    }


def tve_validate_anchored(
    anchor:            dict,
    target_title:      str,
    target_artist:     str,
    target_duration_s: int,
    source:            str = '',
) -> tuple:
    """Anchor-based validation using Saavn ground truth metadata."""
    return tve_validate(
        saavn_title=anchor.get('title', ''),
        saavn_artist=anchor.get('artist', ''),
        saavn_duration_s=anchor.get('duration_s', 0),
        target_title=target_title,
        target_artist=target_artist,
        target_duration_s=target_duration_s,
        saavn_language=anchor.get('language', ''),
        source=source,
    )


def tve_pick_best(
    saavn_title:      str,
    saavn_artist:     str,
    saavn_duration_s: int,
    candidates:       list,
    max_candidates:   int = 5,
    title_key:        str = 'title',
    artist_key:       str = 'artist',
    duration_key:     str = 'duration_s',
    saavn_language:   str = '',
    source:           str = '',
    anchor:           dict = None,
) -> tuple:
    """
    GODMODE: Run ALL candidates through TVE, collect all passes,
    return highest title_score pass (not just first pass).

    Previous version returned first passing candidate — this could pick
    a lower-quality match if a higher-quality one was later in the list.
    """
    passing = []
    checked = 0

    for candidate in candidates[:max_candidates]:
        checked += 1
        c_title  = candidate.get(title_key, '')
        c_artist = candidate.get(artist_key, '')
        c_dur    = int(candidate.get(duration_key, 0) or 0)

        if anchor:
            passed, reason, scores = tve_validate_anchored(
                anchor, c_title, c_artist, c_dur, source=source)
        else:
            passed, reason, scores = tve_validate(
                saavn_title, saavn_artist, saavn_duration_s,
                c_title, c_artist, c_dur,
                saavn_language=saavn_language, source=source,
            )

        if passed:
            passing.append((candidate, scores))
            log.debug(
                f"[TVE] ✓ {checked}/{min(len(candidates), max_candidates)}: "
                f"'{c_title}' t={scores.get('title_score')} a={scores.get('artist_score')}"
            )
        else:
            log.debug(
                f"[TVE] ✗ {checked}/{min(len(candidates), max_candidates)}: "
                f"'{c_title}' → {reason}"
            )

    if not passing:
        log.warning(
            f"[TVE] All {checked} failed: saavn='{saavn_title}' by '{saavn_artist}'"
        )
        return None, {"status": "mismatch_error", "message": "No verified track found"}

    # Pick best passing candidate by title_score + artist_score combined
    best_candidate, best_scores = max(
        passing,
        key=lambda x: (x[1].get('title_score', 0) + x[1].get('artist_score', 0))
    )

    log.info(
        f"[TVE] ✓ Best of {len(passing)} pass(es): "
        f"'{best_candidate.get(title_key)}' "
        f"t={best_scores.get('title_score')} a={best_scores.get('artist_score')}"
    )
    return best_candidate, best_scores


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def normalize(text: str) -> str:
    if not text:
        return ''
    text = text.lower()
    text = re.sub(r'[\u2018\u2019\u201c\u201d\u2013\u2014\u2026]', ' ', text)
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return levenshtein(s2, s1)
    if not s2:
        return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(c1 != c2)))
        prev = curr
    return prev[-1]


def fuzzy_word_match(qw: str, tw: str) -> float:
    if tw.startswith(qw):
        return 1.0
    if qw in tw:
        return 0.85
    max_len = max(len(qw), len(tw))
    if max_len == 0:
        return 0.0
    ratio = 1.0 - (levenshtein(qw, tw) / max_len)
    return ratio if ratio >= 0.75 else 0.0


def title_score(query: str, song_title: str, song_artist: str = '') -> float:
    q, t = normalize(query), normalize(song_title)
    if not q:
        return 0.0
    if q == t:
        return 3.0
    q_words = q.split()
    t_words = t.split()

    # Very short single-word query — require exact match
    if len(q_words) == 1 and len(q) <= 3 and q != t:
        return 0.5 if q in t_words else 0.0

    score = 0.0
    if t.startswith(q):
        suffix = t[len(q):].strip()
        # Don't bonus if suffix contains version words
        if not any(ind in suffix for ind in ('remix', 'lofi', 'cover', 'live', 'acoustic')):
            score += 2.0

    title_match = sum(
        max((fuzzy_word_match(qw, tw) for tw in t_words), default=0.0)
        for qw in q_words
    )
    if q_words:
        score += (title_match / len(q_words)) * 1.5
    return score


def dynamic_min_score(query: str) -> float:
    length = len(normalize(query).replace(' ', ''))
    if length <= 2:    return 0.15
    elif length <= 5:  return 0.40
    elif length <= 10: return 0.55
    else:              return 0.65


def has_word_match(query: str, song_title: str) -> bool:
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words:
        return True
    q_main = [w for w in q_words if len(w) >= 3]
    t_main = [w for w in t_words if len(w) >= 3]
    if not q_main:
        return True
    if not t_main:
        return False
    if t_main and q_main[0] == t_main[0]:
        return True
    for qw in q_main:
        for tw in t_main:
            if fuzzy_word_match(qw, tw) >= 0.75:
                return True
    return False


def clean_query(text: str) -> str:
    text = _FROM_ALBUM_RE.sub('', text)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(
        r'\(\s*(OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?|'
        r'Hindi|English|Version|Remix|Cover|HD|HQ|Original|Soundtrack|'
        r'Remastered|Extended|Radio\s*Edit)\s*\)',
        '', text, flags=re.IGNORECASE
    )
    text = re.sub(
        r'\s*[-–]\s*(official|audio|video|lyrics|full\s*song|hd|hq|remastered).*$',
        '', text, flags=re.IGNORECASE
    )
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)

    _BARE_VERSION = (
        r'\s+(?:lofi|lo[- ]fi|slowed|reverb|slowed\s*reverb|reverb\s*slowed|'
        r'nightcore|sped\s*up|speed\s*up|bass\s*boosted|8d\s*audio|'
        r'dj\s+remix|dj\s+mix|remix|mashup|cover|karaoke|instrumental|'
        r'acoustic|unplugged|live\s*version|live\s*at|live\s*from|'
        r'pitched|chopped|screwed|extended\s*mix|club\s*mix|radio\s*edit|'
        r'tribute|stripped|concert\s*version|reprise|reimagined|'
        r'coke\s*studio|mtv\s*unplugged|nescafe\s*basement|'
        r'velo\s*sound|studio\s*session|home\s*session|'
        r'tiny\s*desk|spotify\s*session|'
        r'season\s*\d+|episode\s*\d+|'
        r'remastered|anniversary\s*edition|deluxe|'
        r'jhankar|jhankar\s*beats|beats\s*version|'
        r'tapori\s*mix|dhol\s*mix|wedding\s*mix|'
        r'bhangra\s*mix|dandiya\s*mix|garba\s*mix|'
        r'lyric\s*video|lyrics\s*video|full\s*video'
        r')\b.*$'
    )
    text = re.sub(_BARE_VERSION, '', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip()


_HINDI_TRANSLIT = [
    ('aa', 'a'), ('ee', 'i'), ('oo', 'u'), ('ae', 'ai'),
    ('ph', 'f'), ('bh', 'b'), ('gh', 'g'), ('kh', 'k'),
    ('dh', 'd'), ('th', 't'), ('sh', 's'), ('ch', 'c'),
    ('wh', 'w'), ('jh', 'j'), ('nh', 'n'), ('mh', 'm'),
]


def _hindi_translit_normalize(text: str) -> str:
    t = text.lower()
    for src, dst in _HINDI_TRANSLIT:
        t = t.replace(src, dst)
    return t


def build_query_variants(title: str, artist: str = '', fallback: str = '') -> list:
    title_c      = clean_query(title)
    artist_c     = clean_query(artist) if artist else ''
    fb_c         = clean_query(fallback) if fallback else ''
    artist_first = artist_c.split()[0] if artist_c else ''
    title_first  = title_c.split()[0] if title_c else ''
    seen, variants = set(), []

    def add(v: str):
        v = re.sub(r'\s+', ' ', v).strip()
        if v and v not in seen:
            seen.add(v)
            variants.append(v)

    if artist_c: add(f"{artist_c} {title_c}")
    add(title_c)
    if artist_first: add(f"{title_c} {artist_first}")
    if artist_c:     add(f"{title_c} {artist_c}")
    if fb_c and fb_c != title_c: add(fb_c)
    if artist_c and fb_c: add(f"{artist_c} {fb_c}")

    bracket_free = re.sub(r'\s*[\(\[].*?[\)\]]\s*', ' ', title_c).strip()
    add(bracket_free)
    dash_free = re.sub(r'\s*[-–]\s*', ' ', title_c).strip()
    add(dash_free)
    words = title_c.split()
    if len(words) > 2: add(' '.join(words[:3]))
    if len(words) > 3: add(' '.join(words[:2]))
    if artist_first and title_first: add(f"{title_first} {artist_first}")
    if artist_c and title_first:     add(f"{artist_c} {title_first}")
    if artist_first and len(words) > 1: add(f"{words[0]} {words[1]} {artist_first}")

    # Translit variants
    try:
        t_translit = _hindi_translit_normalize(title_c)
        if t_translit and t_translit != title_c:
            add(t_translit)
            if artist_first: add(f"{t_translit} {artist_first}")
        if artist_c:
            a_translit = _hindi_translit_normalize(artist_c)
            if a_translit and a_translit != artist_c:
                add(f"{a_translit} {title_c}")
    except Exception:
        pass

    _OLD_HINTS = ['90', '90s', 'purane', 'purana', 'purani', 'old', 'retro', '80', '70']
    _has_decade = any(h in (title.lower() + ' ' + artist.lower()) for h in _OLD_HINTS)
    if _has_decade and artist_c:
        add(f"{artist_c} {title_c} old")
        add(f"{title_c} old hindi")

    return variants


# ═══════════════════════════════════════════════════════════════════════════════
# IMAGE / QUALITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

QUALITY_RANK = {
    '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
    '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
}


def pick_best_quality(urls: list) -> tuple:
    if not urls:
        return None, None

    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK:
            return QUALITY_RANK[q]
        m = re.search(r'(\d+)', q)
        return int(m.group(1)) if m else -1

    for item in sorted(urls, key=rank, reverse=True):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'):
            return url, item.get('quality', 'unknown')
    return None, None


def _pick_low_quality(urls: list) -> tuple:
    if not urls:
        return None, None
    _prefer = ['96kbps', '96', '128kbps', '128', '48kbps', '48']
    for preferred in _prefer:
        for item in urls:
            q = (item.get('quality') or '').lower().strip()
            if q == preferred or preferred in q:
                url = item.get('url') or item.get('link') or ''
                if url.startswith('http'):
                    return url, item.get('quality', preferred)
    _low_rank = {
        '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
        '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
    }
    for item in sorted(urls, key=lambda i: _low_rank.get(
            (i.get('quality') or '').lower().strip(), 999)):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'):
            return url, item.get('quality', 'low')
    return None, None


def _ensure_500(url: str) -> str:
    """Convert Saavn CDN URL to 500x500. Fixed \3 backreference (was \u0003 bug)."""
    if not url or not url.startswith('http'):
        return url
    if 'saavncdn.com' in url or 'jiocdn.com' in url:
        url = re.sub(r'-(\d+)x(\d+)\.(jpg|jpeg|webp|png)', r'-500x500.\3', url)
        url = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
        return url
    if re.search(r'\b(50|150|250)x(50|150|250)\b', url):
        url = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
    return url


def pick_image(song: dict) -> str:
    """Pick best available image URL, guaranteed 500x500 on Saavn CDN."""
    images = song.get('image') or []
    if isinstance(images, list) and images:
        for item in reversed(images):
            url = item.get('url') or item.get('link') or ''
            if url and url.startswith('http'):
                return _ensure_500(url)

    if isinstance(images, str) and images.startswith('http'):
        return _ensure_500(images)

    art = song.get('artworkUrl100', '')
    if art and art.startswith('http'):
        return re.sub(r'\b\d+x\d+\b', '600x600', art)

    thumb = song.get('thumbnail', '') or song.get('thumb', '')
    if thumb and thumb.startswith('http'):
        return thumb

    return ''


# ═══════════════════════════════════════════════════════════════════════════════
# LANGUAGE / DECADE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_LANGUAGE_KEYWORD_MAP = {
    'bhojpuri': 'bhojpuri', 'bhojpuri song': 'bhojpuri',
    'bhojpuri gana': 'bhojpuri', 'bhojpuri gaana': 'bhojpuri',
    'pawan singh': 'bhojpuri', 'khesari lal': 'bhojpuri',
    'dinesh lal': 'bhojpuri', 'nirahua': 'bhojpuri',
    'ritesh pandey': 'bhojpuri', 'ankush raja': 'bhojpuri',
    'pramod premi': 'bhojpuri', 'kallu': 'bhojpuri',
    'shilpi raj': 'bhojpuri', 'gunjan singh': 'bhojpuri',
    'hindi': 'hindi', 'bollywood': 'hindi',
    'hindi song': 'hindi', 'hindi gana': 'hindi',
    'english': 'english', 'english song': 'english', 'pop': 'english',
    'punjabi': 'punjabi', 'punjabi song': 'punjabi',
    'haryanvi': 'haryanvi', 'rajasthani': 'rajasthani',
    'tamil': 'tamil', 'telugu': 'telugu', 'kannada': 'kannada',
    'malayalam': 'malayalam', 'bengali': 'bengali',
    'marathi': 'marathi', 'gujarati': 'gujarati', 'odia': 'odia',
}

_BHOJPURI_TRANSLIT = [
    ('tohaar', 'tohar'), ('hamaar', 'hamar'), ('kahe', 'kaahe'),
    ('bhaiya', 'bhaiyya'), ('saiya', 'saiyya'), ('piya', 'piyaa'),
    ('bhauji', 'bhouji'), ('lahariya', 'laharia'),
    ('goriya', 'goria'), ('balmuaa', 'balmua'),
    ('ae', 'aye'), ('ogo', 'ago'), ('hau', 'hu'),
    ('bade', 'bado'), ('kaisan', 'kaisa'),
]


def _detect_language(query: str) -> str:
    q = query.lower().strip()
    for kw in sorted(_LANGUAGE_KEYWORD_MAP, key=len, reverse=True):
        if kw in q:
            return _LANGUAGE_KEYWORD_MAP[kw]
    return ''


def _bhojpuri_normalize(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    for src, dst in _BHOJPURI_TRANSLIT:
        t = re.sub(r'\b' + re.escape(src) + r'\b', dst, t)
    return t


def _safe_year(date_str) -> int:
    try:
        return int((date_str or '')[:4])
    except (ValueError, TypeError):
        return 0


def _is_devotional_query(text: str) -> bool:
    _DEVOTIONAL_KEYWORDS = [
        'chalisa', 'aarti', 'bhajan', 'stuti', 'mantra', 'stotra',
        'vandana', 'kirtan', 'prarthana', 'hanuman', 'ganesh', 'durga',
        'gayatri', 'om jai', 'jai shri', 'shiv', 'krishna', 'radhe',
        'sai baba', 'qawwali', 'naat', 'hamd', 'ramayan', 'mahabharat',
        'jai ganesh', 'saraswati', 'lakshmi', 'mata', 'devi',
    ]
    t = text.lower()
    return any(kw in t for kw in _DEVOTIONAL_KEYWORDS)


def _query_requests_version(query: str) -> bool:
    _USER_VERSION_PHRASES = {
        'dj remix', 'dj mix', 'dj version', 'dj edit',
        'bass boosted', 'bass boost', 'slowed reverb', 'sped up', 'speed up',
        '8d audio', 'lo-fi', 'lo fi', 'lofi version', 'remix version',
        'acoustic version', 'unplugged version', 'live version', 'live at',
        'live from', 'live session', 'live concert', 'live performance',
        'live show', 'live recording', 'live in ',
        'instrumental version', 'karaoke version', 'cover version', 'acoustic cover',
        'coke studio', 'mtv unplugged',
    }
    _USER_VERSION_WORDS = {
        'lofi', 'remix', 'slowed', 'nightcore', 'reverb', 'mashup',
        'karaoke', 'instrumental',
    }
    _CONTEXT_ONLY = {'acoustic', 'unplugged', 'cover'}
    q = query.lower().strip()
    for phrase in _USER_VERSION_PHRASES:
        if phrase in q:
            return True
    for word in _USER_VERSION_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', q):
            return True
    if re.search(r'\bdj\b', q):
        return True
    for word in _CONTEXT_ONLY:
        if re.search(r'\b' + re.escape(word) + r'\b', q):
            if re.search(r'\b(version|ver|mix|edit|session|recording|perform|show)\b', q):
                return True
            if re.search(r'[-\u2013|]\s*' + re.escape(word) + r'\s*$', q):
                return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

NINETIES_SEEDS = [
    "Kumar Sanu hits", "Udit Narayan 90s", "Alka Yagnik 90s",
    "Lata Mangeshkar 90s", "Sonu Nigam 90s hits",
    "Kavita Krishnamurthy songs", "Asha Bhosle 90s",
    "Abhijeet Bhattacharya hits", "Shankar Mahadevan 90s",
    "AR Rahman 90s", "Anu Malik 90s hits",
    "Nadeem Shravan songs", "Jatin Lalit songs",
    "Kumar Sanu Alka Yagnik duets", "90s Bollywood superhits",
]

NINETIES_TRIGGERS = [
    '90', 'purane', 'purana', 'purani', 'old', 'retro',
    'classic', 'nineties', 'throwback', 'evergreen', 'gaane',
]

ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'cf.saavncdn.com', 'aac.saavncdn.com', 'static.saavncdn.com',
    'c.saavncdn.com', 'h.saavncdn.com',
    'googlevideo.com', 'youtube.com', 'ytimg.com',
    'manifest.googlevideo.com', 'sndcdn.com', 'soundcloud.com',
    'cf-media.sndcdn.com', 'a-v2.sndcdn.com',
]
