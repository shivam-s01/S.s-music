import re
from typing import Optional, Dict, Any, List, Tuple
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
# TRACK VERIFICATION ENGINE — v1.0
# Permanently fixes song mismatch from YT/SoundCloud results.
# Architecture: Metadata Cleaner → 3-Tier Pipeline → Cache-Aware Loop
# ═══════════════════════════════════════════════════════════════════════════════

# ── Metadata Cleaning & Normalization ──────────────────────────────────────────

_META_NOISE_RE = re.compile(
    r'(?i)\b(official|video|audio|lyrics|lyrical|full\s+video|full\s+song|'
    r'hd|hq|mp3|remix|cover|reverb|lofi|slowed|vibe|clean|'
    r'shot|reels|tiktok|version|4k|8k|music|visualizer|'
    r'reaction|episode|ep|superhit|super\s+hit|new\s+song|'
    r'latest|blockbuster|hit|jukebox|nonstop|back\s+to\s+back|'
    r'2020|2021|2022|2023|2024|2025|'
    r'song|gana|gaana|bhajan|bhojpuri\s+song|hindi\s+song|'
    r'dj\s+wale|dj\s+remix|wala|wali|wale)\b'
)

# Hindi/Devanagari noise — strip if present
_META_DEVANAGARI_RE = re.compile(r'[\u0900-\u097F]+')

_META_BRACKET_RE  = re.compile(r'[\[\](){}<>]')
_META_SPECIAL_RE  = re.compile(r'[-|_+/\\]+')
_META_WHITESPACE  = re.compile(r'\s{2,}')

# Saavn/YT title clutter patterns
_META_EXTRA_RE = re.compile(
    r'(?i)\s*[-|]\s*(official|audio|video|lyrics|full\s+song|hd|hq|'
    r'ft\.?\s+\w+|feat\.?\s+\w+)\s*$'
)


def clean_metadata(text: str) -> str:
    """
    Tier-0 text cleanser.
    - Lowercase + strip whitespace
    - Strip punctuation: [], (), -, |, _, +, /
    - Purge non-music metadata buzzwords
    - Collapses leftover whitespace
    """
    if not text:
        return ''
    t = text.lower().strip()

    # Remove bracketed sections first
    t = _META_BRACKET_RE.sub(' ', t)

    # Remove trailing meta labels after dash/pipe
    t = _META_EXTRA_RE.sub('', t)

    # Purge buzzword tokens
    t = _META_NOISE_RE.sub(' ', t)

    # Replace special chars with space
    t = _META_SPECIAL_RE.sub(' ', t)

    # Strip Devanagari script noise (Hindi titles on YT often mix scripts)
    t = _META_DEVANAGARI_RE.sub(' ', t)

    # Collapse whitespace
    t = _META_WHITESPACE.sub(' ', t).strip()

    return t


# ── Three-Tier Validation Pipeline ────────────────────────────────────────────

# Thresholds
_TVE_DURATION_MAX_DELTA_S  = 5     # Tier 1: reject if > 5s difference
_TVE_TITLE_THRESHOLD       = 85    # Tier 2: rapidfuzz token_sort_ratio min
_TVE_ARTIST_THRESHOLD      = 85    # Tier 2: artist match min — raised 80→85


def tve_tier1_duration(saavn_duration_s: int, target_duration_s: int) -> bool:
    """
    [GODMODE] Adaptive duration: max(8, min(20, dur*0.08))
    3min→14s, 5min→20s, 30s jingle→8s tolerance.
    """
    if saavn_duration_s <= 0 or target_duration_s <= 0:
        return True
    tolerance = max(8, min(20, int(saavn_duration_s * 0.08)))
    delta = abs(saavn_duration_s - target_duration_s)
    if delta > tolerance:
        log.debug(f"[TVE:T1] REJECT delta={delta}s tol={tolerance}s")
        return False
    return True


def tve_tier2_fuzzy(
    saavn_title:  str,
    saavn_artist: str,
    target_title: str,
    target_artist: str,
) -> tuple:
    """
    Tier 2 — Token-based fuzzy matching (rapidfuzz).
    Returns (pass: bool, title_score: int, artist_score: int).
    Falls back to SequenceMatcher if rapidfuzz unavailable.
    [FIX-TVE-2] Artist threshold raised 80→85 to prevent same-title-different-artist pass.
    [FIX-TVE-2b] When title is near-exact match, artist check becomes STRICTER (not looser).
    """
    c_saavn_t  = clean_metadata(saavn_title)
    c_saavn_a  = clean_metadata(saavn_artist)
    c_target_t = clean_metadata(target_title)
    c_target_a = clean_metadata(target_artist)

    if _RAPIDFUZZ_AVAILABLE:
        title_score  = _rfuzz.token_sort_ratio(c_saavn_t, c_target_t)
        # Artist: check if Saavn primary artist exists within target artist string
        # Use partial_ratio for artist — handles "Arijit Singh" inside "Arijit Singh, Shreya Ghoshal"
        if c_saavn_a and c_target_a:
            artist_score = max(
                _rfuzz.token_sort_ratio(c_saavn_a, c_target_a),
                _rfuzz.partial_ratio(c_saavn_a, c_target_a),
            )
        else:
            artist_score = 50  # neutral when artist unknown
    else:
        # Fallback — difflib SequenceMatcher
        from difflib import SequenceMatcher as _SM
        def _sim(a, b):
            if not a or not b: return 50
            return int(_SM(None, a, b).ratio() * 100)
        title_score  = _sim(c_saavn_t, c_target_t)
        artist_score = _sim(c_saavn_a, c_target_a)

    title_pass  = title_score  >= _TVE_TITLE_THRESHOLD
    # [FIX-TVE-2b] High title match pe artist check strict karo
    # "Tum Hi Ho" Arijit vs "Tum Hi Ho" cover — title=100, artist must also match well
    _effective_artist_threshold = _TVE_ARTIST_THRESHOLD
    if title_score >= 95 and c_saavn_a and c_target_a:
        _effective_artist_threshold = 85  # stricter when title is near-identical
    artist_pass = artist_score >= _effective_artist_threshold
    return (title_pass and artist_pass), title_score, artist_score


def tve_tier3_strict_exclusion(saavn_title: str, target_title: str) -> bool:
    """
    Tier 3 — Strict version exclusion.
    Returns True (PASS) if no version mismatch.
    Returns False (REJECT) if:
      - Saavn query is a clean song (no remix/cover)
      - But target result contains remix/cover markers
    """
    saavn_is_version  = _is_remix_or_cover(saavn_title) or _is_slowed_reverb(saavn_title) or _is_live_version(saavn_title)
    target_is_version = _is_remix_or_cover(target_title) or _is_slowed_reverb(target_title) or _is_live_version(target_title)

    if not saavn_is_version and target_is_version:
        return False  # REJECT — clean query, version result
    return True  # PASS


def tve_tier4_language(saavn_language: str, target_title: str, target_artist: str) -> tuple:
    """
    Tier 4 — Language cross-contamination block.
    Bhojpuri Saavn song pe Hindi/English YT result nahi aana chahiye.
    Returns (pass: bool, reason: str).
    """
    if not saavn_language:
        return True, 'no_language_info'

    _BHOJPURI_MARKERS = [
        'bhojpuri', 'pawan singh', 'khesari', 'nirahua', 'dinesh lal',
        'ritesh pandey', 'ankush raja', 'pramod premi', 'arvind akela',
        'samar singh', 'indu sonali', 'akshara singh',
    ]
    _HINDI_MAINSTREAM = [
        'arijit singh', 'jubin nautiyal', 'armaan malik', 'atif aslam',
        'sonu nigam', 'udit narayan', 'kumar sanu', 'lata mangeshkar',
        'asha bhosle', 'shreya ghoshal', 'alka yagnik', 'neha kakkar',
        'darshan raval', 'mohd rafi', 'kishore kumar',
    ]

    t_lower  = target_title.lower()
    a_lower  = target_artist.lower()
    combined = t_lower + ' ' + a_lower

    if saavn_language == 'bhojpuri':
        # Bhojpuri song pe Hindi mainstream singer ka result reject
        if any(m in a_lower for m in _HINDI_MAINSTREAM):
            # Check: kya Bhojpuri marker bhi hai?
            if not any(m in combined for m in _BHOJPURI_MARKERS):
                return False, 'bhojpuri_saavn_hindi_result'
        # Bhojpuri song pe English result reject
        from core import _is_english_song_query
        if _is_english_song_query(target_title, target_artist):
            return False, 'bhojpuri_saavn_english_result'

    elif saavn_language == 'english':
        # English song pe Bhojpuri result reject
        if any(m in combined for m in _BHOJPURI_MARKERS):
            return False, 'english_saavn_bhojpuri_result'

    return True, 'ok'


def tve_tier5_artist_hard(
    saavn_artist: str,
    target_artist: str,
    source: str = '',
    title_exact: bool = False,
) -> tuple:
    """
    Tier 5 — Artist hard reject (final gate).
    SoundCloud pe skip karo — SC uploader names unreliable hain.
    Returns (pass: bool, reason: str).
    [FIX-TVE-5] Same-surname different-firstname case handle karo.
    """
    # [GODMODE] SoundCloud NO LONGER skips — enforced at threshold 0.45
    # SC ke uploader names unreliable, lekin completely skip karna mismatch deta tha

    if not saavn_artist or not target_artist:
        return True, 'artist_unknown'

    def _norm_artist(t):
        t = t.lower()
        t = re.sub(r'\s*(feat\.?|ft\.?|featuring|presents|prod\.?).*', '', t)
        t = re.sub(r'\s*\(.*?\)', '', t)
        t = re.sub(r'[^a-z0-9\s]', '', t)
        return re.sub(r'\s+', ' ', t).strip()

    def _primary(t):
        parts = re.split(r'\s*[&,]\s*|\s+x\s+|\s+and\s+|\s+\+\s+', _norm_artist(t))
        return parts[0].strip() if parts else _norm_artist(t)

    sa = _norm_artist(saavn_artist)
    ta = _norm_artist(target_artist)
    sp = _primary(saavn_artist)
    tp = _primary(target_artist)

    if not sa or not ta:
        return True, 'empty_after_norm'

    # Multi-strategy similarity
    from difflib import SequenceMatcher as _SM
    def _ratio(a, b):
        if not a or not b: return 0.0
        return _SM(None, a, b).ratio()

    s1 = _ratio(sa, ta)
    s2 = _ratio(sp, ta)
    s3 = _ratio(sa, tp)

    # Substring containment
    s4 = 0.0
    if sp and sp in ta: s4 = 0.90
    if tp and tp in sa: s4 = max(s4, 0.90)

    best_sim = max(s1, s2, s3, s4)

    # [FIX-TVE-5] Same surname, different first name — penalize
    sa_words = sa.split()
    ta_words = ta.split()
    if len(sa_words) >= 2 and len(ta_words) >= 2:
        first_sim = _ratio(sa_words[0], ta_words[0])
        last_sim  = _ratio(sa_words[-1], ta_words[-1])
        if first_sim < 0.60 and last_sim >= 0.85:
            # e.g. "Neha Kakkar" vs "Tony Kakkar" — same surname, different person
            best_sim = min(best_sim, 0.35)

    # Threshold — title exact match pe thoda loose (0.45), otherwise strict (0.55)
    # [GODMODE] Source-specific thresholds
    _THRESHOLDS = {
        'saavn': 0.60, 'jiosavan': 0.60,
        'ytmusic': 0.55, 'youtube': 0.52,
        'soundcloud': 0.45,  # was: skipped — now enforced
        'piped': 0.50, 'invidious': 0.50,
    }
    _threshold = _THRESHOLDS.get(source, 0.52)
    if title_exact:
        _threshold = max(0.35, _threshold - 0.10)
    if best_sim < _threshold:
        return False, f'artist_sim_{best_sim:.3f}_below_{_threshold}'

    return True, 'ok'


def tve_validate(
    saavn_title:      str,
    saavn_artist:     str,
    saavn_duration_s: int,
    target_title:     str,
    target_artist:    str,
    target_duration_s: int,
    saavn_language:   str = '',
    source:           str = '',
) -> tuple:
    """
    Full 5-Tier TVE validation.
    Returns (pass: bool, reason: str, scores: dict).
    """
    # Tier 1 — Duration fast drop
    if not tve_tier1_duration(saavn_duration_s, target_duration_s):
        return False, 'tier1_duration_reject', {
            'saavn_dur': saavn_duration_s,
            'target_dur': target_duration_s,
            'delta': abs(saavn_duration_s - target_duration_s),
        }

    # Tier 2 — Fuzzy match (SC artist skip handled inside tier2)
    t2_pass, t_score, a_score = tve_tier2_fuzzy(
        saavn_title, saavn_artist, target_title, target_artist)
    if not t2_pass:
        return False, f'tier2_fuzzy_reject:title={t_score},artist={a_score}', {
            'title_score': t_score,
            'artist_score': a_score,
        }

    # Tier 3 — Strict version exclusion
    if not tve_tier3_strict_exclusion(saavn_title, target_title):
        return False, 'tier3_version_mismatch', {
            'saavn_title': saavn_title,
            'target_title': target_title,
        }

    # Tier 4 — Language cross-block
    t4_pass, t4_reason = tve_tier4_language(saavn_language, target_title, target_artist)
    if not t4_pass:
        return False, f'tier4_{t4_reason}', {
            'saavn_language': saavn_language,
            'target_title': target_title,
        }

    # Tier 5 — Artist hard reject
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


def tve_validate_anchored(anchor: dict, target_title: str, target_artist: str,
                           target_duration_s: int, source: str = '') -> tuple:
    """
    Anchor-based validation — uses Saavn ground truth metadata.
    More accurate than string-based because language/year/album are known.
    """
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
    Run up to max_candidates through the 5-Tier TVE pipeline.
    - anchor: if provided, uses tve_validate_anchored (more accurate)
    - source: passed to Tier 5 (SC skips artist check)
    - saavn_language: passed to Tier 4 (language gate)
    Returns (best_candidate_dict, scores_dict) or
            (None, {"status": "mismatch_error", ...}) if all fail.
    """
    checked = 0
    passing = []  # [GODMODE] collect ALL passing candidates, return best

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
            log.debug(f"[TVE] ✓ {checked}: '{c_title}' t={scores.get('title_score')} a={scores.get('artist_score')}")
        else:
            log.debug(f"[TVE] ✗ {checked}: '{c_title}' → {reason}")

    if not passing:
        log.warning(f"[TVE] All {checked} failed: saavn='{saavn_title}' by '{saavn_artist}'")
        return None, {"status": "mismatch_error", "message": "No verified track found"}

    # [GODMODE] Best of all passes — highest combined title+artist score
    best_candidate, best_scores = max(
        passing,
        key=lambda x: (x[1].get('title_score', 0) + x[1].get('artist_score', 0))
    )
    log.info(f"[TVE] ✓ Best of {len(passing)}: '{best_candidate.get(title_key)}' t={best_scores.get('title_score')} a={best_scores.get('artist_score')}")
    return best_candidate, best_scores

# ═══════════════════════════════════════════════════════════════════════════════
# VERSION DNA
# ═══════════════════════════════════════════════════════════════════════════════

# [GODMODE] "From Album" tag pattern — YT titles often have "Song (From "Album")"
_FROM_ALBUM_RE = re.compile(
    r'\s*\(\s*[Ff]rom\s+["\u201c\u201d\u2018\u2019]?[^)]{1,60}["\u201c\u201d\u2018\u2019]?\s*\)',
    re.IGNORECASE
)

# [GODMODE] Self-healing version word list — runtime expandable
import threading as _threading
_version_word_lock = _threading.Lock()
_DYNAMIC_VERSION_DNA: set = set()

def _heal_version_word_list(new_words):
    """Add new version words at runtime without restart."""
    with _version_word_lock:
        for w in new_words:
            w = w.lower().strip()
            if w:
                _DYNAMIC_VERSION_DNA.add(w)
                log.info(f"[VersionHealer] Added: '{w}'")

_VERSION_DNA = {
    # Lofi / slowed
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
    'acoustic version', 'unplugged', 'stripped',
    'coke studio', 'mtv unplugged', 'nescafe basement',
    'velo sound', 'tiny desk', 'spotify session', 'studio session',
    # Extended / Club
    'extended mix', 'extended version', 'club mix', 'dance mix',
    'radio edit', 'club version', 'club edit', 'festival mix', 'party mix',
    # Indian specific
    'jhankar', 'jhankar beats', 'tapori mix', 'dhol mix',
    'wedding mix', 'bhangra mix', 'dandiya mix', 'garba mix',
    'beats version',
    # Lyric video
    'lyric video', 'lyrics video',
}

# [FIX-1] DJ word boundary — 'djinn', 'dja', 'Django' etc avoid karo
_DJ_WORD_RE = re.compile(r'\bdj\b', re.IGNORECASE)

# Context-dependent words — sirf version context mein flag honge
_AMBIGUOUS_DNA = {'live', 'acoustic', 'cover', 'edit', 'stripped', 'concert', 'performance', 'tribute'}
_VERSION_CONTEXT_RE = re.compile(
    r'\b(version|ver|mix|edit|remix|session|perform|concert|tour|record|cut|show)\b',
    re.IGNORECASE
)

# [FIX-1] Artist name mein "DJ" hona song version DNA nahi hai
_ARTIST_DJ_RE = re.compile(r'^dj\s+[A-Za-z]', re.IGNORECASE)

# [FIX-12] Hard version words — clean query pe result mein ye hone pe ALWAYS reject
# In words ko koi bhi context pass nahi karega agar user ne nahi manga
_HARD_VERSION_WORDS = {
    'remix', 'lofi', 'lo-fi', 'slowed', 'reverb', 'nightcore', 'sped up',
    'speed up', 'bass boosted', 'bass boost', '8d audio', 'karaoke',
    'instrumental', 'minus one', 'mashup', 'mash up', 'bootleg', 'flip',
    'rework', 'jhankar', 'jhankar beats', 'tapori mix', 'dhol mix',
    'wedding mix', 'bhangra mix', 'dandiya mix', 'garba mix', 'party mix',
    'festival mix', 'club mix', 'dance mix', 'extended mix', 'extended version',
    'radio edit', 'club version', 'club edit', 'beats version',
    'dj remix', 'dj mix', 'dj version', 'dj edit', 'dj drop',
    # [FIX-COVER] plain 'cover' bhi hard reject — 'cover version' already tha
    # "Tum Hi Ho (Cover)" clean query pe nahi aana chahiye
    'cover', 'cover version', 'tribute', 'lyric video', 'lyrics video',
}


def get_song_dna(title: str) -> set:
    """
    Song title se version DNA extract karo.
    [GODMODE] _FROM_ALBUM_RE strip + _DYNAMIC_VERSION_DNA support
    """
    if not title:
        return set()
    # [GODMODE] Strip "From Album" noise before DNA check
    title = _FROM_ALBUM_RE.sub('', title).strip()
    t = title.lower().strip()
    found = set()
    _all_dna = _VERSION_DNA | _DYNAMIC_VERSION_DNA

    # DJ check — word boundary
    if _DJ_WORD_RE.search(title):
        has_other_version = any(
            (re.search(r'\b' + re.escape(w) + r'\b', t) if ' ' not in w else w in t)
            for w in _VERSION_DNA if w != 'dj'
        )
        if _ARTIST_DJ_RE.match(title.strip()) and not has_other_version:
            pass  # DJ as artist name — not a version marker
        else:
            found.add('dj')

    # Definite multi-word version words
    for word in _all_dna:
        if word == 'dj':
            continue
        if ' ' in word:
            if word in t:
                found.add(word)
        else:
            if re.search(r'\b' + re.escape(word) + r'\b', t):
                found.add(word)

    # [FIX-3] Ambiguous words — strict context only
    for word in _AMBIGUOUS_DNA:
        if re.search(r'\b' + re.escape(word) + r'\b', t):
            if _VERSION_CONTEXT_RE.search(t):
                found.add(word)
            elif re.search(r'[\(\[]\s*' + re.escape(word) + r'\s*[\)\]]', t):
                found.add(word)
            elif re.search(r'[-–|]\s*' + re.escape(word) + r'\s*$', t):
                found.add(word)

    return found


def dna_compatible(query_title: str, result_title: str) -> bool:
    """
    [GODMODE] PERMANENT MISMATCH PREVENTION.
    - _FROM_ALBUM_RE strip before comparison
    - Pipe-separated YT titles: check first segment only
    - Hard version word zero-tolerance
    """
    if not query_title or not result_title:
        return True

    # [GODMODE] Strip "From Album" noise
    q_clean = _FROM_ALBUM_RE.sub('', query_title).strip()
    r_clean = _FROM_ALBUM_RE.sub('', result_title).strip()

    # [GODMODE] Pipe-separated YT titles — "Kesariya | Brahmastra | Arijit Singh"
    if '|' in r_clean:
        r_clean = r_clean.split('|')[0].strip()

    q_dna = get_song_dna(q_clean)
    r_dna = get_song_dna(r_clean)

    # HARD CHECK: result mein hard version word — user ne nahi manga → reject
    r_lower = r_clean.lower()
    for hw in (_HARD_VERSION_WORDS | _DYNAMIC_VERSION_DNA):
        if ' ' in hw:
            hw_present = hw in r_lower
        else:
            hw_present = bool(re.search(r'\b' + re.escape(hw) + r'\b', r_lower))
        if hw_present:
            # Check karo kya user ne yeh manga tha
            q_lower = q_clean.lower()
            if ' ' in hw:
                q_has = hw in q_lower
            else:
                q_has = bool(re.search(r'\b' + re.escape(hw) + r'\b', q_lower))
            if not q_has:
                return False  # Hard reject — user ne nahi manga tha

    # User ne clean song manga
    if not q_dna:
        return len(r_dna) == 0

    # User ne specific version manga — result mein woh version hona chahiye
    if not r_dna:
        return False

    # [FIX-2] Strict intersection — "lofi" query pe "remix" result nahi chalega
    return bool(q_dna & r_dna)


def has_version_words(title: str) -> bool:
    """Quick check — koi bhi version word hai ya nahi."""
    return len(get_song_dna(title)) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def clean_query(text):
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(
        r'\(\s*(OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?|Hindi|English|Version|Remix|Cover|HD|HQ|Original|Soundtrack|Remastered|Extended|Radio\s*Edit)\s*\)',
        '', text, flags=re.IGNORECASE
    )
    text = re.sub(r'\s*[-–]\s*(official|audio|video|lyrics|full\s*song|hd|hq|remastered).*$', '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)
    _BARE_VERSION_PATTERN = (
        r'\s+(?:lofi|lo[- ]fi|slowed|reverb|slowed\s*reverb|reverb\s*slowed|'
        r'nightcore|sped\s*up|speed\s*up|bass\s*boosted|8d\s*audio|'
        r'dj\s+remix|dj\s+mix|remix|mashup|cover|karaoke|instrumental|'
        r'acoustic|unplugged|live\s*version|live\s*at|live\s*from|'
        r'pitched|chopped|screwed|extended\s*mix|club\s*mix|radio\s*edit|'
        r'tribute|stripped|concert\s*version|'
        r'coke\s*studio|mtv\s*unplugged|nescafe\s*basement|'
        r'velo\s*sound|studio\s*session|home\s*session|'
        r'tiny\s*desk|spotify\s*session|'
        r'season\s*\d+|episode\s*\d+|'
        r'remastered|anniversary\s*edition|'
        r'jhankar|jhankar\s*beats|beats\s*version|'
        r'tapori\s*mix|dhol\s*mix|wedding\s*mix|'
        r'bhangra\s*mix|dandiya\s*mix|garba\s*mix|'
        r'lyric\s*video|lyrics\s*video|full\s*video'
        r')\b.*$'
    )
    text = re.sub(_BARE_VERSION_PATTERN, '', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip()


def build_query_variants(title, artist='', fallback=''):
    title_c      = clean_query(title)
    artist_c     = clean_query(artist) if artist else ''
    fb_c         = clean_query(fallback) if fallback else ''
    artist_first = artist_c.split()[0] if artist_c else ''
    title_first  = title_c.split()[0] if title_c else ''
    seen, variants = set(), []

    def add(v):
        v = re.sub(r'\s+', ' ', v).strip()
        if v and v not in seen:
            seen.add(v); variants.append(v)

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

    # [FIX-10] Translit — duplicate check in 'seen' set
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


_HINDI_TRANSLIT = [
    ('aa', 'a'), ('ee', 'i'), ('oo', 'u'), ('ae', 'ai'),
    ('ph', 'f'), ('bh', 'b'), ('gh', 'g'), ('kh', 'k'),
    ('dh', 'd'), ('th', 't'), ('sh', 's'), ('ch', 'c'),
    ('wh', 'w'), ('jh', 'j'), ('nh', 'n'), ('mh', 'm'),
]


def _hindi_translit_normalize(text: str) -> str:
    # [FIX-11] In-word replacement karo, not just word-boundary
    # "aa" is almost never a standalone word in Hindi titles
    # Word-boundary regex was missing in-word occurrences like "baarish" → "baris"
    t = text.lower()
    for src, dst in _HINDI_TRANSLIT:
        # Replace as substring (not word boundary) — Hindi transliteration is in-word
        t = t.replace(src, dst)
    return t


# [FIX-6] normalize — consistent, strip unicode noise
def normalize(text):
    if not text: return ''
    text = text.lower()
    text = re.sub(r'[\u2018\u2019\u201c\u201d\u2013\u2014\u2026]', ' ', text)
    text = re.sub(r'[^a-z0-9\s]', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def levenshtein(s1, s2):
    if len(s1) < len(s2): return levenshtein(s2, s1)
    if not s2: return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(c1 != c2)))
        prev = curr
    return prev[-1]


def fuzzy_word_match(qw, tw):
    if tw.startswith(qw): return 1.0
    if qw in tw: return 0.85
    max_len = max(len(qw), len(tw))
    if max_len == 0: return 0.0
    ratio = 1.0 - (levenshtein(qw, tw) / max_len)
    return ratio if ratio >= 0.75 else 0.0


# [FIX-7] title_score: single short word query — exact match tighten
def title_score(query, song_title, song_artist=''):
    q, t = normalize(query), normalize(song_title)
    if not q: return 0.0
    if q == t: return 3.0
    q_words = q.split(); t_words = t.split()

    if len(q_words) == 1 and len(q) <= 3 and q != t:
        return 0.5 if q in t_words else 0.0

    score = 0.0
    if t.startswith(q): score += 2.0
    title_match = sum(
        max((fuzzy_word_match(qw, tw) for tw in t_words), default=0.0)
        for qw in q_words
    )
    if q_words: score += (title_match / len(q_words)) * 1.5
    return score


# [FIX-8] dynamic_min_score: very short queries
def dynamic_min_score(query):
    length = len(normalize(query).replace(' ', ''))
    if length <= 2:    return 0.15
    elif length <= 5:  return 0.40
    elif length <= 10: return 0.55
    else:              return 0.65


# [FIX-9] has_word_match: empty word-list edge case
def has_word_match(query, song_title):
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words: return True
    q_main  = [w for w in q_words if len(w) >= 3]
    t_main  = [w for w in t_words if len(w) >= 3]
    if not q_main: return True
    if not t_main: return False
    if t_main and q_main[0] == t_main[0]: return True
    for qw in q_main:
        for tw in t_main:
            if fuzzy_word_match(qw, tw) >= 0.75: return True
    return False


def pick_best_quality(urls, preferred_quality: str = None):
    """
    [FIX-QUALITY-1] Adaptive quality selection.
    - preferred_quality: caller pass kar sakta hai ('96kbps', '160kbps', '320kbps')
    - Agar preferred_quality nahi diya toh highest available pick karo (original behavior)
    - Saavn URLs pe quality options hamesha available hoti hain (12/48/96/160/320)
    """
    if not urls: return None, None

    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK: return QUALITY_RANK[q]
        m = re.search(r'(\d+)', q)
        return int(m.group(1)) if m else -1

    # [FIX-QUALITY-1] Preferred quality — exact match pehle try karo
    if preferred_quality:
        pq = preferred_quality.lower().strip()
        for item in urls:
            q = (item.get('quality') or '').lower().strip()
            url = item.get('url') or item.get('link') or ''
            if url.startswith('http') and (q == pq or pq.rstrip('kbps') in q):
                return url, item.get('quality', preferred_quality)
        # Exact match nahi mila — nearest lower quality try karo
        pq_num = int(re.search(r'(\d+)', pq).group(1)) if re.search(r'(\d+)', pq) else 320
        sorted_urls = sorted(urls, key=rank, reverse=True)
        for item in sorted_urls:
            q = (item.get('quality') or '').lower().strip()
            url = item.get('url') or item.get('link') or ''
            if not url.startswith('http'): continue
            q_num = int(re.search(r'(\d+)', q).group(1)) if re.search(r'(\d+)', q) else 0
            if q_num <= pq_num:
                return url, item.get('quality', 'unknown')

    # Default — highest available
    for item in sorted(urls, key=rank, reverse=True):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'):
            return url, item.get('quality', 'unknown')
    return None, None


def detect_preferred_quality(request_headers: dict = None) -> str:
    """
    [FIX-QUALITY-2] Client network hint se quality decide karo.
    Save-Data header ya custom X-Quality-Hint header check karo.
    Frontend se header bhejo:
      - Save-Data: on  → 96kbps
      - X-Quality-Hint: low → 96kbps
      - X-Quality-Hint: medium → 160kbps
      - X-Quality-Hint: high / kuch nahi → 320kbps
    """
    if not request_headers:
        return '320kbps'

    # Save-Data: on — browser/Android ka data saver mode
    save_data = (request_headers.get('Save-Data') or
                 request_headers.get('save-data') or '').lower()
    if save_data == 'on':
        return '96kbps'

    # Custom quality hint from frontend
    quality_hint = (request_headers.get('X-Quality-Hint') or
                    request_headers.get('x-quality-hint') or '').lower()
    if quality_hint == 'low':    return '96kbps'
    if quality_hint == 'medium': return '160kbps'
    if quality_hint == 'high':   return '320kbps'

    # ECT (Effective Connection Type) — Chrome/Android network info API
    ect = (request_headers.get('ECT') or
           request_headers.get('ect') or '').lower()
    if ect in ('slow-2g', '2g'):  return '96kbps'
    if ect == '3g':               return '160kbps'
    # 4g ya kuch nahi — best quality
    return '320kbps'


def _ensure_500(url: str) -> str:
    """
    [FIX-13] Saavn/JioCDN image URL ko 500x500 mein convert karo.
    CRITICAL BUG FIXED: pehle \\u0003 (control char) tha replacement mein
    jo URLs corrupt karta tha. Ab \\3 backreference sahi hai.
    Non-Saavn URLs pe bhi safe size upgrade.
    """
    if not url or not url.startswith('http'):
        return url
    if 'saavncdn.com' in url or 'jiocdn.com' in url:
        # [FIX-13] \\3 = correct backreference for extension group (was \\u0003 = BUG)
        url = re.sub(r'-(\d+)x(\d+)\.(jpg|jpeg|webp|png)', r'-500x500.\3', url)
        url = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
        return url
    # Non-Saavn CDN — safe size upgrade
    if re.search(r'\b(50|150|250)x(50|150|250)\b', url):
        url = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
    return url


# [FIX-4] pick_image: 500x500 force + multi-source fallback
def pick_image(song):
    """
    Image pick karo with guaranteed 500x500 on Saavn CDN.
    [FIX-4]  Multiple fallback sources
    [FIX-13] _ensure_500 uses \\3 (correct) not \\u0003 (corrupt)
    """
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


def _pick_low_quality(urls):
    if not urls: return None, None
    for preferred in ['96kbps', '96', '128kbps', '128', '48kbps', '48']:
        for item in urls:
            q = (item.get('quality') or '').lower().strip()
            if q == preferred or preferred in q:
                url = item.get('url') or item.get('link') or ''
                if url.startswith('http'): return url, item.get('quality', preferred)
    # [FIX-5] Local _low_rank — no duplicate with global QUALITY_RANK
    _low_rank = {
        '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
        '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
    }
    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in _low_rank: return _low_rank[q]
        m = re.search(r'(\d+)', q)
        return int(m.group(1)) if m else 999
    for item in sorted(urls, key=rank):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'): return url, item.get('quality', 'low')
    return None, None


def _safe_year(date_str):
    try: return int((date_str or '')[:4])
    except (ValueError, TypeError): return 0


# Global QUALITY_RANK (used by fetchers.py)
QUALITY_RANK = {
    '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
    '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
}

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


ALLOWED_STREAM_DOMAINS = [
    'akamaized.net', 'jiocdn.com', 'saavncdn.com',
    'cf.saavncdn.com', 'aac.saavncdn.com', 'static.saavncdn.com',
    'c.saavncdn.com', 'h.saavncdn.com',
    'googlevideo.com', 'youtube.com', 'ytimg.com',
    'manifest.googlevideo.com', 'sndcdn.com', 'soundcloud.com',
    'cf-media.sndcdn.com', 'a-v2.sndcdn.com',
]
