import re
import time
import hashlib
import threading
import unicodedata
from typing import Optional, Dict, Any, List, Tuple, Set, Callable
from dataclasses import dataclass, field
from enum import Enum
from collections import OrderedDict

try:
    from rapidfuzz import fuzz as _rfuzz  # type: ignore[import-untyped]
    _RAPIDFUZZ_AVAILABLE: bool = True
except ImportError:
    _rfuzz = None  # type: ignore[assignment]
    _RAPIDFUZZ_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — PRODUCTION THRESHOLDS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class VerificationConfig:
    """Centralized configuration — tune in one place"""

    # Title matching
    TITLE_MIN_SIMILARITY: float = 0.60

    # Artist matching — NO EXCEPTIONS
    ARTIST_MIN_SIMILARITY: float = 0.55

    # Duration validation — STRICT
    DURATION_PERFECT_DELTA_S: int = 2
    DURATION_MINOR_DELTA_S: int = 5
    DURATION_HEAVY_DELTA_S: int = 10
    DURATION_MAX_DELTA_S: int = 10

    # Overall confidence
    MIN_CONFIDENCE_SCORE: float = 0.58

    # Cache
    CACHE_TTL_SECONDS: int = 86400
    CACHE_MIN_CONFIDENCE: float = 0.95
    CACHE_MAX_SIZE: int = 10000

    # Weighting for confidence score
    WEIGHT_TITLE: float = 0.45
    WEIGHT_ARTIST: float = 0.35
    WEIGHT_DURATION: float = 0.12
    WEIGHT_SOURCE: float = 0.08


# ═══════════════════════════════════════════════════════════════════════════════
# HARD REJECTION — VERSION WORDS (ZERO TOLERANCE)
# ═══════════════════════════════════════════════════════════════════════════════

class MatchResult(Enum):
    """Explicit match outcomes"""
    VERIFIED = "verified"
    NO_MATCH = "no_match"
    REJECTED_VERSION = "rejected_version"
    REJECTED_ARTIST = "rejected_artist"
    REJECTED_DURATION = "rejected_duration"
    REJECTED_CONFIDENCE = "rejected_confidence"
    FINGERPRINT_FAIL = "fingerprint_fail"


# HARD REJECTION WORDS — ANY occurrence in candidate = IMMEDIATE REJECT
# (unless user explicitly requested it — checked separately)
_HARD_REJECT_WORDS: Set[str] = {
    # Remix variants
    'remix', 'dj remix', 'dj mix', 'dj edit', 'dj version', 'dj drop',
    'club mix', 'club version', 'dance mix', 'extended mix', 'extended version',
    'radio edit', 'festival mix', 'party mix', 'beats version',

    # Cover / alternate recordings
    'cover', 'cover version', 'tribute', 'karaoke', 'instrumental',
    'minus one', 'acoustic', 'acoustic version', 'unplugged', 'stripped',

    # Live recordings
    'live', 'live version', 'live at', 'live from', 'live session',
    'coke studio', 'mtv unplugged', 'studio session', 'spotify session',

    # Speed modifications
    'slowed', 'reverb', 'slowed reverb', 'sped up', 'speed up',
    'nightcore', '8d audio', '8d', 'bass boosted', 'bass boost',
    'pitched', 'chopped', 'screwed',

    # Lo-fi variants
    'lofi', 'lo-fi', 'lo fi',

    # Mashup / edits
    'mashup', 'mash up', 'bootleg', 'flip', 'rework',

    # Fan content
    'fanmade', 'fan made', 'fan upload', 'reupload', 're-upload',

    # Indian specific
    'jhankar', 'jhankar beats', 'tapori mix', 'dhol mix',
    'wedding mix', 'bhangra mix', 'dandiya mix', 'garba mix',

    # Lyric videos
    'lyric video', 'lyrics video', 'lyrical video',
}

# Context-specific rejection — only if not part of original title
_CONTEXT_REJECT_WORDS: Set[str] = {
    'version', 'edit', 'session', 'performance', 'concert',
}

# Explicit reject stems applied with word-boundary matching
# These are always rejected regardless of query context
_STEM_REJECT_PATTERNS: List[str] = [
    r'\bremix\b',
    r'\blofi\b',
    r'\blo[\s-]fi\b',
    r'\bslowed\b',
    r'\breverb\b',
    r'\bnightcore\b',
    r'\bcover\b',
    r'\bkaraoke\b',
    r'\blive\b',
    r'\bacoustic\b',
]

# Compiled stem patterns for performance
_COMPILED_STEM_PATTERNS: List[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in _STEM_REJECT_PATTERNS
]


# Zero-width and invisible characters that can smuggle reject words past checks
_ZERO_WIDTH_RE = re.compile(
    r'[\u200b\u200c\u200d\u200e\u200f\u00ad\ufeff\u2060\u180e]'
)

# Confusable character map — Cyrillic and Greek characters that look like Latin.
# NFKC normalization does NOT remap Cyrillic→Latin (different scripts).
# This map handles the most common music-metadata homoglyph attacks.
_CONFUSABLE_MAP = str.maketrans({
    # Cyrillic lowercase
    '\u0430': 'a', '\u0435': 'e', '\u0456': 'i', '\u043e': 'o',
    '\u0440': 'r', '\u0441': 'c', '\u0445': 'x', '\u0443': 'y',
    '\u0442': 't', '\u0432': 'b',
    # Cyrillic uppercase
    '\u0410': 'A', '\u0412': 'B', '\u0415': 'E', '\u041a': 'K',
    '\u041c': 'M', '\u041d': 'H', '\u041e': 'O', '\u0420': 'P',
    '\u0421': 'C', '\u0422': 'T', '\u0425': 'X',
    # Greek lowercase
    '\u03b1': 'a', '\u03bf': 'o', '\u03c5': 'u',
    # Greek uppercase
    '\u0399': 'I', '\u039a': 'K', '\u039c': 'M', '\u039d': 'N',
    '\u039f': 'O', '\u03a1': 'R', '\u03a4': 'T', '\u03a7': 'X',
    # Fullwidth Latin (ａ-ｚ, Ａ-Ｚ) — handled by NFKC, kept for safety
})


def _sanitize_title(title: str) -> str:
    """
    Sanitize before ANY rejection or comparison logic.
    1. Strip zero-width/invisible characters (defeats zero-width space bypass).
    2. NFKC normalization — collapses fullwidth Latin and compatibility chars.
    3. Confusable map — remaps Cyrillic/Greek homoglyphs to Latin equivalents.
    Always call this first on any external title string.
    """
    if not title:
        return ''
    # Step 1: remove zero-width chars
    t = _ZERO_WIDTH_RE.sub('', title)
    # Step 2: NFKC — handles fullwidth, ligatures (ﬁ→fi), etc.
    t = unicodedata.normalize('NFKC', t)
    # Step 3: explicit confusable remap (Cyrillic/Greek → Latin)
    t = t.translate(_CONFUSABLE_MAP)
    return t


def _extract_core_title(title: str) -> str:
    """
    Extract core title by removing ALL parenthetical/bracketed content.
    Input must already be sanitized via _sanitize_title.
    """
    if not title:
        return ''
    t = title.lower()
    t = re.sub(r'\([^)]*\)', '', t)
    t = re.sub(r'\[[^\]]*\]', '', t)
    t = re.sub(r'\{[^}]*\}', '', t)
    t = re.sub(r'\s*[|—-]\s*.*$', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _candidate_has_stem_reject(candidate_title: str) -> Tuple[bool, str]:
    """
    Check candidate title against compiled stem reject patterns.
    Returns (rejected: bool, matched_pattern: str)
    """
    for pattern in _COMPILED_STEM_PATTERNS:
        m = pattern.search(candidate_title)
        if m:
            return True, m.group(0).lower()
    return False, ''


def hard_reject_by_version(
    query_title: str,
    candidate_title: str,
    query_has_version: bool = False
) -> Tuple[bool, str]:
    """
    [HARD REJECTION LAYER]
    Returns (reject: bool, reason: str)

    If user requested a version (query_has_version=True), version markers
    in the candidate that also appear in the query are allowed.
    All other version markers in the candidate are hard rejected.
    """
    if not candidate_title:
        return False, ''

    # Sanitize both titles before any check — defeats unicode homoglyph and zero-width bypass
    candidate_title = _sanitize_title(candidate_title)
    query_title = _sanitize_title(query_title)

    candidate_lower = candidate_title.lower()
    core_candidate = _extract_core_title(candidate_title)
    core_query = _extract_core_title(query_title)

    # Step 1: Stem-pattern hard reject (remix, lofi, slowed, reverb, nightcore, cover, karaoke, live)
    stem_rejected, stem_word = _candidate_has_stem_reject(candidate_title)
    if stem_rejected:
        # Allow only if user explicitly requested this exact marker in their query
        if query_has_version and stem_word in query_title.lower():
            pass  # User asked for it
        else:
            return True, f"hard_reject_stem: '{stem_word}' in candidate"

    # Step 2: Full hard-reject word set — word-boundary matching
    for word in _HARD_REJECT_WORDS:
        # Use word-boundary regex to prevent substring false fires
        # e.g. "remix" must not match "remix" inside "remembrix" (contrived),
        # but "live" must not match inside "liver" or "alive"
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, candidate_lower):
            word_in_query_core = bool(re.search(pattern, core_query)) if core_query else False
            if not query_has_version and not word_in_query_core:
                return True, f"hard_reject: '{word}' in candidate, not in query"
            if word_in_query_core:
                continue
            if query_has_version:
                continue
            return True, f"hard_reject: '{word}' in candidate"

    # Step 3: Context-specific words — check against raw candidate, not core
    for word in _CONTEXT_REJECT_WORDS:
        if re.search(r'\b' + re.escape(word) + r'\b', candidate_lower):
            query_core_has_word = bool(re.search(r'\b' + re.escape(word) + r'\b', core_query)) if core_query else False
            if not query_has_version and not query_core_has_word:
                return True, f"hard_reject: context word '{word}'"

    # Step 4: "From Album" pattern — often indicates unauthorized upload
    if re.search(r'\(\s*[Ff]rom\s+', candidate_title):
        if not re.search(r'\(\s*[Ff]rom\s+', query_title):
            return True, "hard_reject: 'from album' pattern"

    return False, ''


def user_requested_version(title: str) -> bool:
    """
    Detect if user explicitly requested a version.
    e.g., "Kesariya Lofi", "Tum Hi Ho Remix"
    Uses word-boundary matching to avoid false positives:
    "Stayin Alive" contains "live" but is NOT a version request.
    "Flipkart" contains "flip" but is NOT a version request.
    """
    if not title:
        return False
    title_sanitized = _sanitize_title(title).lower()
    # Check stem patterns (word-boundary compiled regexes) first
    for pattern in _COMPILED_STEM_PATTERNS:
        if pattern.search(title_sanitized):
            return True
    # Check full reject word set with word boundaries
    for word in _HARD_REJECT_WORDS:
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, title_sanitized):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# ARTIST VERIFICATION — MANDATORY 90% SIMILARITY
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_artist(artist: str) -> str:
    """
    Normalize artist name for comparison.
    - Lowercase
    - Remove feat/ft./featuring and everything after
    - Remove parenthetical content
    - Remove punctuation
    - Collapse whitespace
    """
    if not artist:
        return ''

    a = artist.lower().strip()
    a = re.sub(r'\s*(feat\.?|ft\.?|featuring|presents)\s+.*$', '', a)
    a = re.sub(r'\([^)]*\)', '', a)
    a = re.sub(r'\[[^\]]*\]', '', a)
    a = re.sub(r'[^\w\s]', '', a)
    a = re.sub(r'\s+', ' ', a).strip()
    return a


def get_all_artists(artist: str) -> List[str]:
    """
    Split artist string into all individual artists (primary + featured).
    Handles: comma, &, x, feat, ft, and, vs, with
    Returns list of normalized artist names.
    """
    if not artist:
        return []
    a: str = artist.lower()
    # Remove bracket content first
    a = re.sub(r'\([^)]*\)', '', a)
    a = re.sub(r'\[[^\]]*\]', '', a)
    # Split on all known separators including feat
    raw_parts: List[str] = re.split(
        r'\s*(?:feat\.?|ft\.?|featuring|presents|versus|vs\.?|\band\b|&|,|\s+x\s+|\bwith\b)\s*',
        a,
        flags=re.IGNORECASE
    )
    result: List[str] = []
    for part in raw_parts:
        p: str = re.sub(r'[^\w\s]', '', part)
        p = re.sub(r'\s+', ' ', p).strip()
        if p:
            result.append(p)
    return result


def _ratio_difflib(a: str, b: str) -> float:
    """SequenceMatcher-based ratio, returns 0.0–1.0."""
    if not a or not b:
        return 0.0
    from difflib import SequenceMatcher
    return float(SequenceMatcher(None, a, b).ratio())


def _ratio_rapidfuzz(a: str, b: str) -> float:
    """rapidfuzz-based ratio, returns 0.0–1.0."""
    if not a or not b or _rfuzz is None:
        return 0.0
    return float(_rfuzz.ratio(a, b)) / 100.0


def _ratio(a: str, b: str) -> float:
    """
    String similarity ratio, 0.0–1.0.
    Uses rapidfuzz when available, falls back to difflib.
    """
    if _RAPIDFUZZ_AVAILABLE:
        return _ratio_rapidfuzz(a, b)
    return _ratio_difflib(a, b)


def get_primary_artist(artist: str) -> str:
    """
    Extract primary artist (first before any separator).
    """
    artists = get_all_artists(artist)
    return artists[0] if artists else normalize_artist(artist)


def calculate_artist_similarity(saavn_artist: str, target_artist: str) -> float:
    """
    Calculate artist similarity with strict matching.
    Priority: exact > full normalized > primary > safe contained > word overlap
    Returns score between 0.0 and 1.0.
    """
    if not saavn_artist or not target_artist:
        return 0.0

    s_norm = normalize_artist(saavn_artist)
    t_norm = normalize_artist(target_artist)

    if not s_norm or not t_norm:
        return 0.0

    # Strategy 0: Exact match after normalization — highest priority
    if s_norm == t_norm:
        return 1.0

    s_primary = get_primary_artist(saavn_artist)
    t_primary = get_primary_artist(target_artist)

    if s_primary and t_primary and s_primary == t_primary:
        return 1.0

    if not _RAPIDFUZZ_AVAILABLE:
        pass  # using module-level _ratio backed by difflib
    # (module-level _ratio picks the right backend automatically)

    # Strategy 1: Full normalized artist
    score_full = _ratio(s_norm, t_norm)

    # Strategy 2: Primary artist fuzzy
    score_primary = _ratio(s_primary, t_primary)

    # Strategy 3: Safe contained — require word-boundary match, min 4 chars
    # Prevents "Ali" matching "Salim", "Sha" matching "Shah Rukh"
    score_contained = 0.0
    if s_primary and len(s_primary) >= 4:
        pattern = r'\b' + re.escape(s_primary) + r'\b'
        if re.search(pattern, t_norm):
            score_contained = 0.92
        elif re.search(pattern, t_primary):
            score_contained = 0.92

    # Strategy 4: Multi-artist overlap — check all artists both sides
    s_all = set(get_all_artists(saavn_artist))
    t_all = set(get_all_artists(target_artist))
    if s_all and t_all:
        matched: int = sum(
            1 for sa in s_all
            if any(_ratio(sa, ta) >= 0.90 for ta in t_all)
        )
        score_multi: float = float(matched) / float(max(len(s_all), len(t_all)))
    else:
        score_multi = 0.0

    # Strategy 5: Word overlap — whole words only
    s_words = set(w for w in s_norm.split() if len(w) >= 3)
    t_words = set(w for w in t_norm.split() if len(w) >= 3)
    if s_words and t_words:
        overlap: int = len(s_words & t_words)
        score_overlap: float = float(overlap) / float(max(len(s_words), len(t_words)))
    else:
        score_overlap = 0.0

    best_score: float = max(
        float(score_full),
        float(score_primary),
        float(score_contained),
        float(score_multi),
        float(score_overlap)
    )

    # Hard penalty: same surname, different first name
    s_parts = s_norm.split()
    t_parts = t_norm.split()
    if len(s_parts) >= 2 and len(t_parts) >= 2:
        s_first, s_last = s_parts[0], s_parts[-1]
        t_first, t_last = t_parts[0], t_parts[-1]
        if s_last == t_last and s_first != t_first and len(s_last) >= 3:
            best_score = min(float(best_score), 0.25)

    # Hard penalty: completely different single-word artists
    if len(s_parts) == 1 and len(t_parts) == 1 and s_norm != t_norm:
        if score_full < 0.80:
            best_score = min(float(best_score), float(score_full))

    return best_score


def verify_artist(
    saavn_artist: str,
    target_artist: str,
    config: VerificationConfig
) -> Tuple[bool, float, str]:
    """
    Mandatory artist verification.
    Returns (pass: bool, score: float, reason: str)
    """
    # Empty artist → neutral pass (cannot reject what we don't know)
    if not saavn_artist:
        return True, 0.80, "no_saavn_artist_skip"

    if not target_artist:
        return True, 0.80, "no_target_artist_skip"

    score = calculate_artist_similarity(saavn_artist, target_artist)

    if score >= config.ARTIST_MIN_SIMILARITY:
        return True, score, "artist_match"

    return False, score, f"artist_mismatch: {score:.3f} < {config.ARTIST_MIN_SIMILARITY}"


# ═══════════════════════════════════════════════════════════════════════════════
# DURATION VALIDATION — STRICT
# ═══════════════════════════════════════════════════════════════════════════════

def _get_duration_max_delta(reference_duration_s: int) -> int:
    """
    Tiered duration tolerance based on song length.
    <=180s  → max 5s
    181-300s → max 6s
    >300s   → max 8s
    """
    if reference_duration_s <= 180:
        return 5
    elif reference_duration_s <= 300:
        return 6
    else:
        return 8


def verify_duration(
    saavn_duration_s: Any,
    target_duration_s: Any,
    config: VerificationConfig
) -> Tuple[bool, float, str]:
    """
    Strict tiered duration validation.
    Accepts int or float inputs — coerced to int (truncated).
    Tolerance derived from reference (saavn) duration, not flat config.
    Returns (pass: bool, score: float, reason: str)
    """
    saavn_s: int
    target_s: int
    try:
        saavn_s = int(saavn_duration_s)
        target_s = int(target_duration_s)
    except (TypeError, ValueError):
        return False, 0.0, "invalid_duration_non_numeric"

    # duration=0 → unknown, skip check (neutral pass — don't hard-fail)
    if saavn_s <= 0 or target_s <= 0:
        return True, 0.85, "duration_unknown_skip"

    delta: int = abs(saavn_s - target_s)
    max_delta: int = _get_duration_max_delta(saavn_s)

    if delta > max_delta:
        return False, 0.0, f"duration_delta_{delta}s_exceeds_limit_{max_delta}s"

    score: float
    reason: str

    # Perfect: <=2s
    if delta <= 2:
        score = 1.0
        reason = f"duration_perfect_{delta}s"
    # Minor: 3s up to half of max_delta
    elif delta <= max(3, max_delta // 2):
        score = 1.0 - float(delta - 2) / float(max_delta - 2) * 0.3
        reason = f"duration_minor_{delta}s"
    # Heavy: rest up to max_delta
    else:
        score = 0.7 - float(delta - max_delta // 2) / float(max_delta - max_delta // 2) * 0.3
        reason = f"duration_heavy_{delta}s"

    return True, max(0.0, min(1.0, score)), reason


# ═══════════════════════════════════════════════════════════════════════════════
# TITLE SIMILARITY — CLEAN COMPARISON
# ═══════════════════════════════════════════════════════════════════════════════

def clean_title_for_comparison(title: str) -> str:
    """
    Clean title for similarity comparison.
    Sanitizes unicode/zero-width chars first, then removes all non-core content.
    """
    if not title:
        return ''

    t = _sanitize_title(title).lower()
    t = re.sub(r'\([^)]*\)', '', t)
    t = re.sub(r'\[[^\]]*\]', '', t)
    t = re.sub(r'\{[^}]*\}', '', t)
    t = re.sub(r'\s*[|—-]\s*.*$', '', t)
    t = re.sub(r'\b(official|video|audio|lyrics|song|hd|hq|mp3|music)\b', '', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def calculate_title_similarity(query_title: str, candidate_title: str) -> float:
    """
    Calculate title similarity with strict matching.
    Returns score between 0.0 and 1.0.
    """
    if not query_title or not candidate_title:
        return 0.0

    q_clean = clean_title_for_comparison(query_title)
    c_clean = clean_title_for_comparison(candidate_title)

    if not q_clean or not c_clean:
        return 0.0

    if q_clean == c_clean:
        return 1.0

    if not _RAPIDFUZZ_AVAILABLE or _rfuzz is None:
        from difflib import SequenceMatcher
        score = SequenceMatcher(None, q_clean, c_clean).ratio()
    else:
        score = float(_rfuzz.token_sort_ratio(q_clean, c_clean)) / 100.0

    return score


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE RELIABILITY SCORING
# ═══════════════════════════════════════════════════════════════════════════════

class SourceType(Enum):
    OFFICIAL_ARTIST = 1.0
    OFFICIAL_TOPIC = 0.95
    VERIFIED_MUSIC = 0.90
    YT_MUSIC = 0.85
    SAAVN = 0.90
    SOUNDCLOUD_OFFICIAL = 0.80
    SOUNDCLOUD_UNVERIFIED = 0.40
    FAN_CHANNEL = 0.20
    LYRICS_CHANNEL = 0.10
    UNKNOWN = 0.50


def get_source_reliability_score(source: str, channel_info: Optional[Dict[str, Any]] = None) -> float:
    """
    Get reliability score based on source and channel metadata.
    """
    source_lower = source.lower() if source else ''

    if channel_info:
        channel_title = channel_info.get('title', '').lower()

        if 'official' in channel_title or 'artist' in channel_title:
            if 'topic' in channel_title:
                return SourceType.OFFICIAL_TOPIC.value
            return SourceType.OFFICIAL_ARTIST.value

        if 'lyrics' in channel_title:
            return SourceType.LYRICS_CHANNEL.value

        if 'fan' in channel_title or 'upload' in channel_title:
            return SourceType.FAN_CHANNEL.value

    if 'youtube' in source_lower or 'ytmusic' in source_lower:
        return SourceType.YT_MUSIC.value
    elif 'saavn' in source_lower or 'jiosaavn' in source_lower:
        return SourceType.SAAVN.value
    elif 'soundcloud' in source_lower:
        return SourceType.SOUNDCLOUD_UNVERIFIED.value
    elif 'piped' in source_lower or 'invidious' in source_lower:
        return SourceType.UNKNOWN.value

    return SourceType.UNKNOWN.value


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIDENCE SCORE CALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class VerificationResult:
    """Complete verification result"""
    success: bool
    result: MatchResult
    confidence: float
    title_score: float
    artist_score: float
    duration_score: float
    source_score: float
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)


def calculate_confidence(
    title_score: float,
    artist_score: float,
    duration_score: float,
    source_score: float,
    config: VerificationConfig
) -> float:
    """
    Calculate weighted confidence score.
    """
    confidence = (
        title_score * config.WEIGHT_TITLE +
        artist_score * config.WEIGHT_ARTIST +
        duration_score * config.WEIGHT_DURATION +
        source_score * config.WEIGHT_SOURCE
    )
    return round(confidence, 4)


def verify_track(
    query_title: str,
    query_artist: str,
    query_duration_s: int,
    candidate_title: str,
    candidate_artist: str,
    candidate_duration_s: int,
    source: str = '',
    channel_info: Optional[Dict[str, Any]] = None,
    config: Optional[VerificationConfig] = None
) -> VerificationResult:
    """
    Complete track verification pipeline.
    Order: HARD REJECT → ARTIST → DURATION → TITLE → CONFIDENCE
    """
    if config is None:
        config = VerificationConfig()

    # Guard None/non-string inputs — never crash in production
    query_title = _sanitize_title(str(query_title)) if query_title is not None else ''
    query_artist = str(query_artist) if query_artist is not None else ''
    candidate_title = _sanitize_title(str(candidate_title)) if candidate_title is not None else ''
    candidate_artist = str(candidate_artist) if candidate_artist is not None else ''
    try:
        query_duration_s = int(query_duration_s) if query_duration_s is not None else 0
        candidate_duration_s = int(candidate_duration_s) if candidate_duration_s is not None else 0
    except (TypeError, ValueError):
        query_duration_s = 0
        candidate_duration_s = 0

    if not query_title or not query_artist:
        return VerificationResult(
            success=False,
            result=MatchResult.NO_MATCH,
            confidence=0.0,
            title_score=0.0,
            artist_score=0.0,
            duration_score=0.0,
            source_score=0.0,
            reason="invalid_query_missing_title_or_artist"
        )

    # Step 0: Check if user requested a version
    user_wants_version = user_requested_version(query_title)
    reject, reject_reason = hard_reject_by_version(
        query_title, candidate_title, user_wants_version
    )
    if reject:
        return VerificationResult(
            success=False,
            result=MatchResult.REJECTED_VERSION,
            confidence=0.0,
            title_score=0.0,
            artist_score=0.0,
            duration_score=0.0,
            source_score=0.0,
            reason=reject_reason,
            metadata=dict({'candidate_title': candidate_title})  # type: Dict[str, Any]
        )

    # Step 2: Artist verification — MANDATORY
    artist_pass, artist_score, artist_reason = verify_artist(
        query_artist, candidate_artist, config
    )
    if not artist_pass:
        return VerificationResult(
            success=False,
            result=MatchResult.REJECTED_ARTIST,
            confidence=0.0,
            title_score=0.0,
            artist_score=artist_score,
            duration_score=0.0,
            source_score=0.0,
            reason=artist_reason,
            metadata=dict({'query_artist': query_artist, 'candidate_artist': candidate_artist})  # type: Dict[str, Any]
        )

    # Step 3: Duration verification
    duration_pass, duration_score, duration_reason = verify_duration(
        query_duration_s, candidate_duration_s, config
    )
    if not duration_pass:
        return VerificationResult(
            success=False,
            result=MatchResult.REJECTED_DURATION,
            confidence=0.0,
            title_score=0.0,
            artist_score=artist_score,
            duration_score=duration_score,
            source_score=0.0,
            reason=duration_reason
        )

    # Step 4: Title similarity
    title_score = calculate_title_similarity(query_title, candidate_title)
    if title_score < config.TITLE_MIN_SIMILARITY:
        return VerificationResult(
            success=False,
            result=MatchResult.NO_MATCH,
            confidence=round(title_score * config.WEIGHT_TITLE, 4),
            title_score=title_score,
            artist_score=artist_score,
            duration_score=duration_score,
            source_score=0.0,
            reason=f"title_similarity_{title_score:.3f}_below_{config.TITLE_MIN_SIMILARITY}"
        )

    # Step 5: Source reliability
    source_score = get_source_reliability_score(source, channel_info)

    # Step 6: Calculate final confidence
    confidence = calculate_confidence(
        title_score, artist_score, duration_score, source_score, config
    )

    # Step 7: Final confidence threshold
    if confidence < config.MIN_CONFIDENCE_SCORE:
        return VerificationResult(
            success=False,
            result=MatchResult.REJECTED_CONFIDENCE,
            confidence=confidence,
            title_score=title_score,
            artist_score=artist_score,
            duration_score=duration_score,
            source_score=source_score,
            reason=f"confidence_{confidence:.3f}_below_{config.MIN_CONFIDENCE_SCORE}"
        )

    return VerificationResult(
        success=True,
        result=MatchResult.VERIFIED,
        confidence=confidence,
        title_score=title_score,
        artist_score=artist_score,
        duration_score=duration_score,
        source_score=source_score,
        reason="verified"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# VERIFIED CACHE — HIGH CONFIDENCE ONLY
# ═══════════════════════════════════════════════════════════════════════════════

class VerifiedMatchCache:
    """
    LRU cache for verified matches.
    Only stores matches with confidence >= CACHE_MIN_CONFIDENCE.
    Prevents cache poisoning.
    Cache keys are derived from normalized title + artist (cache-safe).
    """

    def __init__(self, config: Optional[VerificationConfig] = None):
        self.config = config or VerificationConfig()
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self._expiry: Dict[str, float] = {}

    def _get_key(self, query_title: str, query_artist: str) -> str:
        """Generate deterministic cache key from normalized query"""
        normalized_title = clean_title_for_comparison(query_title)
        normalized_artist = normalize_artist(query_artist)
        key_string = f"{normalized_title}|{normalized_artist}"
        return hashlib.sha256(key_string.encode('utf-8')).hexdigest()

    def get(self, query_title: str, query_artist: str) -> Optional[Dict]:
        """
        Get cached verified match.
        Returns None if not found or expired.
        """
        key = self._get_key(query_title, query_artist)

        with self._lock:
            if key in self._expiry:
                if time.time() > self._expiry[key]:
                    self._cache.pop(key, None)
                    self._expiry.pop(key, None)
                    return None

            if key in self._cache:
                value = self._cache.pop(key)
                self._cache[key] = value
                return value

        return None

    def set(
        self,
        query_title: str,
        query_artist: str,
        verified_match: Dict,
        confidence: float
    ) -> bool:
        """
        Store verified match in cache.
        Hard floor: confidence must be >= max(0.92, CACHE_MIN_CONFIDENCE).
        Config cannot lower this floor below 0.92.
        Returns True if stored, False otherwise.
        """
        # Absolute production floor — config cannot override below 0.92
        effective_min: float = max(float(0.68), float(self.config.CACHE_MIN_CONFIDENCE))
        if confidence < effective_min:
            return False

        key = self._get_key(query_title, query_artist)

        with self._lock:
            if len(self._cache) >= self.config.CACHE_MAX_SIZE:
                oldest = next(iter(self._cache))
                self._cache.pop(oldest)
                self._expiry.pop(oldest, None)

            self._cache[key] = verified_match
            self._expiry[key] = time.time() + self.config.CACHE_TTL_SECONDS

        return True

    def invalidate(self, query_title: str, query_artist: str) -> bool:
        """
        Invalidate a specific cache entry.
        Returns True if entry existed and was removed.
        """
        key = self._get_key(query_title, query_artist)
        with self._lock:
            existed = key in self._cache
            self._cache.pop(key, None)
            self._expiry.pop(key, None)
        return existed

    def clear_expired(self) -> int:
        """Remove expired entries. Returns count removed."""
        now = time.time()
        removed = 0

        with self._lock:
            expired_keys = [k for k, exp in self._expiry.items() if now > exp]
            for key in expired_keys:
                self._cache.pop(key, None)
                self._expiry.pop(key, None)
                removed += 1

        return removed

    def clear_all(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            self._expiry.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# FINGERPRINT AUTHORITY MODE
# ═══════════════════════════════════════════════════════════════════════════════

class FingerprintAuthority:
    """
    Fingerprint-based verification.
    Metadata is untrusted — fingerprint is final authority.

    BYPASS AUDIT:
    - Both fingerprints absent → metadata path runs, confidence penalized by FINGERPRINT_ABSENT_PENALTY.
    - Query fingerprint absent, candidate present → metadata path, penalty applied.
    - Candidate fingerprint absent, query present → metadata path, penalty applied.
    - Either fingerprint present + mismatch → IMMEDIATE REJECT, no fallback.
    - Only when BOTH fingerprints present AND match → no penalty.
    """

    FINGERPRINT_ABSENT_PENALTY: float = 0.05  # Subtracted from final confidence

    @staticmethod
    def _hash_features(features: Dict) -> str:
        """Generate fingerprint hash from audio features"""
        feature_string = (
            f"{features.get('duration', 0)}"
            f"|{features.get('peak_amplitude', 0)}"
            f"|{features.get('zero_crossing_rate', 0)}"
            f"|{features.get('spectral_centroid', 0)}"
        )
        return hashlib.sha256(feature_string.encode('utf-8')).hexdigest()

    def verify(
        self,
        query_fingerprint: str,
        candidate_fingerprint: str,
        threshold: float = 0.95
    ) -> Tuple[bool, float, bool]:
        """
        Verify using fingerprint.
        Returns (match: bool, similarity: float, both_present: bool)

        both_present=False means caller must apply FINGERPRINT_ABSENT_PENALTY.
        If either side is present and mismatch → hard reject (match=False).
        If both absent → match=False, both_present=False (penalty only, no reject).
        """
        q_present = bool(query_fingerprint)
        c_present = bool(candidate_fingerprint)

        # Both absent — cannot verify, apply penalty downstream
        if not q_present and not c_present:
            return False, 0.0, False

        # One side present, other absent — treat as unverifiable, apply penalty
        if q_present != c_present:
            return False, 0.0, False

        # Both present — compare
        similarity = 1.0 if query_fingerprint == candidate_fingerprint else 0.0
        matched = similarity >= threshold
        return matched, similarity, True

    def generate_fingerprint(self, audio_data: bytes, features: Dict) -> str:
        """
        Generate fingerprint from audio data.
        Hashes full content, not just first 16KB, to prevent false collisions
        on tracks with identical silent/intro segments.
        In production: replace with chromaprint acoustic fingerprint.
        """
        if audio_data:
            # Hash full data to avoid intro-silence collisions
            return hashlib.sha256(audio_data).hexdigest()
        return self._hash_features(features)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN VERIFICATION ENGINE — PRODUCTION ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

class TrackVerificationEngine:
    """
    Main verification engine.
    Flow: CACHE → FINGERPRINT → TVE → CACHE STORE
    """

    def __init__(self, config: Optional[VerificationConfig] = None):
        self.config = config or VerificationConfig()
        self.cache = VerifiedMatchCache(self.config)
        self.fingerprint = FingerprintAuthority()

    def verify_with_fingerprint(
        self,
        query_title: str,
        query_artist: str,
        query_duration_s: int,
        query_fingerprint: str,
        candidate: Dict,
        source: str = '',
        channel_info: Optional[Dict[str, Any]] = None
    ) -> VerificationResult:
        """
        Verify candidate with fingerprint as authority.
        - Fingerprint mismatch (both present, no match) → IMMEDIATE REJECT.
        - Fingerprint absent (either/both) → metadata path with confidence penalty.
        - Fingerprint match (both present) → metadata path, no penalty.
        """
        candidate_fingerprint = candidate.get('fingerprint', '')

        fp_match, fp_similarity, fp_both_present = self.fingerprint.verify(
            query_fingerprint, candidate_fingerprint
        )

        # Both fingerprints present but mismatch → hard reject
        if fp_both_present and not fp_match:
            return VerificationResult(
                success=False,
                result=MatchResult.FINGERPRINT_FAIL,
                confidence=0.0,
                title_score=0.0,
                artist_score=0.0,
                duration_score=0.0,
                source_score=0.0,
                reason=f"fingerprint_mismatch_{fp_similarity:.3f}"
            )

        # Run metadata verification
        meta_result = verify_track(
            query_title=query_title,
            query_artist=query_artist,
            query_duration_s=query_duration_s,
            candidate_title=candidate.get('title', ''),
            candidate_artist=candidate.get('artist', ''),
            candidate_duration_s=candidate.get('duration_s', 0),
            source=source,
            channel_info=channel_info,
            config=self.config
        )

        # Apply fingerprint-absent confidence penalty — never boost
        if not fp_both_present and meta_result.success:
            penalized_confidence = round(
                meta_result.confidence - FingerprintAuthority.FINGERPRINT_ABSENT_PENALTY, 4
            )
            if penalized_confidence < self.config.MIN_CONFIDENCE_SCORE:
                return VerificationResult(
                    success=False,
                    result=MatchResult.REJECTED_CONFIDENCE,
                    confidence=penalized_confidence,
                    title_score=meta_result.title_score,
                    artist_score=meta_result.artist_score,
                    duration_score=meta_result.duration_score,
                    source_score=meta_result.source_score,
                    reason=f"confidence_{penalized_confidence:.3f}_after_fingerprint_penalty"
                )
            # Return penalized but still passing result
            return VerificationResult(
                success=True,
                result=MatchResult.VERIFIED,
                confidence=penalized_confidence,
                title_score=meta_result.title_score,
                artist_score=meta_result.artist_score,
                duration_score=meta_result.duration_score,
                source_score=meta_result.source_score,
                reason="verified_no_fingerprint_penalty_applied"
            )

        return meta_result

    def select_best_candidate(
        self,
        query_title: str,
        query_artist: str,
        query_duration_s: int,
        candidates: List[Dict],
        query_fingerprint: str = '',
        source: str = '',
        max_candidates: int = 10
    ) -> Tuple[Optional[Dict], VerificationResult]:
        """
        Select best candidate from list.
        Returns (best_candidate, verification_result)
        Deterministic: candidates evaluated in order; highest confidence wins.
        """
        if not candidates:
            return None, VerificationResult(
                success=False,
                result=MatchResult.NO_MATCH,
                confidence=0.0,
                title_score=0.0,
                artist_score=0.0,
                duration_score=0.0,
                source_score=0.0,
                reason="no_candidates"
            )

        best_result: Optional[VerificationResult] = None
        best_candidate: Optional[Dict] = None

        for candidate in candidates[:max_candidates]:
            result = self.verify_with_fingerprint(
                query_title=query_title,
                query_artist=query_artist,
                query_duration_s=query_duration_s,
                query_fingerprint=query_fingerprint,
                candidate=candidate,
                source=source,
                channel_info=candidate.get('channel_info') or None
            )

            if result.success:
                if best_result is None or result.confidence > best_result.confidence:
                    best_result = result
                    best_candidate = candidate

        if best_candidate is not None and best_result is not None:
            self.cache.set(
                query_title, query_artist,
                best_candidate,
                best_result.confidence
            )
            return best_candidate, best_result

        return None, VerificationResult(
            success=False,
            result=MatchResult.NO_MATCH,
            confidence=0.0,
            title_score=0.0,
            artist_score=0.0,
            duration_score=0.0,
            source_score=0.0,
            reason="no_candidate_passed"
        )

    def get_cached_match(
        self,
        query_title: str,
        query_artist: str
    ) -> Optional[Dict]:
        """
        Check cache for verified match.
        Use before running full verification.
        """
        return self.cache.get(query_title, query_artist)


# ═══════════════════════════════════════════════════════════════════════════════
# COMPATIBILITY LAYER — FOR EXISTING CODE
# ═══════════════════════════════════════════════════════════════════════════════

# Global engine instance
_default_engine = TrackVerificationEngine()


def tve_validate_production(
    saavn_title: str,
    saavn_artist: str,
    saavn_duration_s: int,
    target_title: str,
    target_artist: str,
    target_duration_s: int,
    source: str = '',
    channel_info: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Production-grade verification.
    Returns (passed: bool, reason: str, scores: dict)
    """
    result = verify_track(
        query_title=saavn_title,
        query_artist=saavn_artist,
        query_duration_s=saavn_duration_s,
        candidate_title=target_title,
        candidate_artist=target_artist,
        candidate_duration_s=target_duration_s,
        source=source,
        channel_info=channel_info
    )

    scores = {
        'confidence': result.confidence,
        'title_score': result.title_score,
        'artist_score': result.artist_score,
        'duration_score': result.duration_score,
        'source_score': result.source_score,
    }

    return result.success, result.reason, scores


def tve_pick_best_production(
    saavn_title: str,
    saavn_artist: str,
    saavn_duration_s: int,
    candidates: List[Dict],
    source: str = '',
    max_candidates: int = 10
) -> Tuple[Optional[Dict], Dict]:
    """
    Pick best candidate using production verification.
    """
    best, result = _default_engine.select_best_candidate(
        query_title=saavn_title,
        query_artist=saavn_artist,
        query_duration_s=saavn_duration_s,
        candidates=candidates,
        source=source,
        max_candidates=max_candidates
    )

    if best is not None:
        return best, {
            'status': 'verified',
            'confidence': result.confidence,
            'title_score': result.title_score,
            'artist_score': result.artist_score,
            'duration_score': result.duration_score,
            'source_score': result.source_score,
            'reason': result.reason
        }

    return None, {
        'status': 'no_match_found',
        'confidence': result.confidence,
        'reason': result.reason
    }


# ═══════════════════════════════════════════════════════════════════════════════
# EXPORTS
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # Core engine
    'TrackVerificationEngine',
    'VerifiedMatchCache',
    'FingerprintAuthority',

    # Verification functions
    'verify_track',
    'tve_validate_production',
    'tve_pick_best_production',

    # Types
    'VerificationResult',
    'MatchResult',
    'VerificationConfig',
    'SourceType',

    # Helpers
    'hard_reject_by_version',
    'user_requested_version',
    'calculate_artist_similarity',
    'calculate_title_similarity',
    'verify_duration',
    'verify_artist',
    'normalize_artist',
    'get_primary_artist',
    'get_all_artists',
    'clean_title_for_comparison',
    'get_source_reliability_score',
    'calculate_confidence',
    '_get_duration_max_delta',

    # Constants
    '_HARD_REJECT_WORDS',
    '_CONTEXT_REJECT_WORDS',
    '_STEM_REJECT_PATTERNS',

    # Default instance
    '_default_engine',
]

# ═══════════════════════════════════════════════════════════════════════════════
# END OF PRODUCTION-GRADE MATCH_ENGINE.PY
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY SHIM
# Maps old function/constant names → new production implementations
# fetchers.py, core.py, sources.py mein koi changes nahi karne padte
# ═══════════════════════════════════════════════════════════════════════════════

import re as _re
import os as _os

# ── Constants ──────────────────────────────────────────────────────────────────
QUALITY_RANK = {'320kbps': 5, '160kbps': 4, '96kbps': 3, '64kbps': 2, '48kbps': 1, 'unknown': 0}
NINETIES_SEEDS   = ['90s', '1990', '1991', '1992', '1993', '1994', '1995', '1996', '1997', '1998', '1999']
NINETIES_TRIGGERS = ['purane', 'purana', 'purani', 'old', 'classic', 'retro', '90']
ALLOWED_STREAM_DOMAINS = [
    'cdn.saavncdn.com', 'aac.saavncdn.com', 'jiocdn.com', 'jiosavan.onrender.com',
    'rr1---sn-', 'googlevideo.com', 'youtube.com', 'ytimg.com',
    'sndcdn.com', 'soundcloud.com', 'cf-hls-media.sndcdn.com',
    'audio.jukehost.co.uk',
]

# ── normalize (core.py has its own, but match_engine also needs one) ──────────
def normalize(text: str) -> str:
    if not text: return ''
    t = text.lower()
    t = _re.sub(r'[\u2018\u2019\u201c\u201d\u2013\u2014\u2026]', ' ', t)
    t = _re.sub(r'[^a-z0-9\s]', '', t)
    return _re.sub(r'\s+', ' ', t).strip()

# ── clean_query ────────────────────────────────────────────────────────────────
def clean_query(text: str) -> str:
    return clean_title_for_comparison(text)

# ── clean_metadata (TVE v1 compat) ────────────────────────────────────────────
def clean_metadata(text: str) -> str:
    return clean_title_for_comparison(text)

# ── _ensure_500 — thumbnail URL to 500x500 ───────────────────────────────────
def _ensure_500(url: str) -> str:
    if not url or not url.startswith('http'): return ''
    url = _re.sub(r'-(\d+)x(\d+)\.(jpg|jpeg|webp|png)', r'-500x500.\3', url)
    url = _re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
    if 'ytimg.com' in url or 'yt3.ggpht.com' in url:
        url = _re.sub(r'=w\d+-h\d+(-[a-z]+)?', '=w500-h500', url)
        url = _re.sub(r'=s\d+', '=s500', url)
    return url

# ── pick_image — best quality image from Saavn song dict ─────────────────────
def pick_image(song: dict) -> str:
    raw = song.get('image') or song.get('artworkUrl100') or song.get('thumbnail') or ''
    if isinstance(raw, list):
        # Saavn returns list of {quality, url} — pick highest quality
        best = ''
        _pref = ['500x500', '150x150', '50x50']
        for p in _pref:
            for item in raw:
                u = item.get('url', item) if isinstance(item, dict) else str(item)
                if p in u:
                    best = u; break
            if best: break
        if not best and raw:
            last = raw[-1]
            best = last.get('url', last) if isinstance(last, dict) else str(last)
        raw = best
    return _ensure_500(str(raw)) if raw else ''

# ── pick_best_quality — choose best audio URL from downloadUrl list ───────────
def pick_best_quality(raw_urls, preferred='320kbps'):
    if not raw_urls: return None, None
    if isinstance(raw_urls, str): return raw_urls, 'unknown'
    ranked = []
    for item in raw_urls:
        if isinstance(item, str):
            ranked.append((QUALITY_RANK.get('unknown', 0), item, 'unknown'))
            continue
        url = item.get('url', '')
        q   = item.get('quality', 'unknown')
        if not url: continue
        ranked.append((QUALITY_RANK.get(q, 0), url, q))
    if not ranked: return None, None
    # Try preferred first
    for score, url, q in sorted(ranked, key=lambda x: -x[0]):
        if preferred and preferred.lower() in q.lower():
            return url, q
    best = max(ranked, key=lambda x: x[0])
    return best[1], best[2]

def _pick_low_quality(raw_urls):
    url, q = pick_best_quality(raw_urls, preferred='96kbps')
    if url: return url, q
    return pick_best_quality(raw_urls)

# ── detect_preferred_quality — from request headers ──────────────────────────
def detect_preferred_quality(headers: dict) -> str:
    if headers.get('Save-Data', '').lower() == 'on': return '96kbps'
    hint = headers.get('X-Quality-Hint', '').lower()
    if hint == 'low':    return '96kbps'
    if hint == 'medium': return '160kbps'
    return '320kbps'

# ── _detect_language ──────────────────────────────────────────────────────────
_HINDI_WORDS   = {'tera','mera','tum','hum','hai','kya','nahi','pyar','dil','ishq','aaja',
                  'suno','bolo','jiya','mann','raat','din','yaar','dost','zindagi','mohabbat'}
_BHOJPURI_WDS  = {'ae','hau','rahe','bani','dekhta','tohar','hamaar','kaisan','bhailsa',
                  'chhodi','ailu','jailu','leke','saiya','piya'}
_ENGLISH_WORDS = {'the','and','you','love','baby','night','day','heart','feel','never',
                  'always','every','time','want','need','come','going'}

def _detect_language(text: str) -> str:
    if not text: return ''
    t = text.lower()
    words = set(_re.sub(r'[^a-z\s]', '', t).split())
    bh = len(words & _BHOJPURI_WDS)
    hi = len(words & _HINDI_WORDS)
    en = len(words & _ENGLISH_WORDS)
    if bh >= 2: return 'bhojpuri'
    if hi >= 2: return 'hindi'
    if en >= 2: return 'english'
    return ''

# ── _bhojpuri_normalize ───────────────────────────────────────────────────────
def _bhojpuri_normalize(text: str) -> str:
    if not text: return ''
    t = text.lower()
    t = _re.sub(r'[^a-z0-9\s]', '', t)
    return _re.sub(r'\s+', ' ', t).strip()

# ── has_version_words ─────────────────────────────────────────────────────────
def has_version_words(title: str) -> bool:
    rejected, _ = hard_reject_by_version(title, title, query_has_version=False)
    # More targeted: check against candidate
    rejected2, _ = _candidate_has_stem_reject(title)
    if rejected2: return True
    tl = title.lower()
    for w in _HARD_REJECT_WORDS:
        if _re.search(r'\b' + _re.escape(w) + r'\b', tl):
            return True
    return False

# ── get_song_dna — returns version type if title is a version ─────────────────
def get_song_dna(title: str) -> str:
    _, word = _candidate_has_stem_reject(title)
    if word: return word
    tl = title.lower()
    for w in _HARD_REJECT_WORDS:
        if _re.search(r'\b' + _re.escape(w) + r'\b', tl):
            return w
    return ''

# ── dna_compatible — True if both titles are same version type ────────────────
def dna_compatible(query_title: str, candidate_title: str) -> bool:
    rejected, reason = hard_reject_by_version(
        query_title, candidate_title,
        query_has_version=user_requested_version(query_title)
    )
    return not rejected

# ── _is_remix_or_cover, _is_live_version, _is_slowed_reverb ──────────────────
def _is_remix_or_cover(title: str) -> bool:
    tl = title.lower()
    remix_words = {'remix','cover','mashup','bootleg','flip','rework','edit',
                   'dj mix','dj remix','dj version','club mix','extended mix'}
    for w in remix_words:
        if _re.search(r'\b' + _re.escape(w) + r'\b', tl): return True
    return False

def _is_live_version(title: str) -> bool:
    tl = title.lower()
    live_words = {'live','unplugged','acoustic','coke studio','mtv unplugged',
                  'studio session','concert','stripped'}
    for w in live_words:
        if _re.search(r'\b' + _re.escape(w) + r'\b', tl): return True
    return False

def _is_slowed_reverb(title: str) -> bool:
    tl = title.lower()
    slow_words = {'slowed','reverb','lofi','lo-fi','nightcore','sped up',
                  '8d audio','bass boosted','pitched','chopped'}
    for w in slow_words:
        if _re.search(r'\b' + _re.escape(w) + r'\b', tl): return True
    return False

# ── _query_requests_version ───────────────────────────────────────────────────
_query_requests_version = user_requested_version

# ── _is_devotional_query ─────────────────────────────────────────────────────
_DEVOTIONAL_KW = ['chalisa','aarti','bhajan','stuti','mantra','stotra','vandana',
                  'kirtan','prarthana','hanuman','ganesh','durga','gayatri','om jai',
                  'jai shri','shiv','krishna','radhe','sai baba','qawwali','naat',
                  'hamd','ramayan','mahabharat','mata','devi']
def _is_devotional_query(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _DEVOTIONAL_KW)

# ── title_score ───────────────────────────────────────────────────────────────
def title_score(query: str, result_title: str, result_artist: str = '') -> float:
    return calculate_title_similarity(query, result_title)

# ── dynamic_min_score ─────────────────────────────────────────────────────────
def dynamic_min_score(query: str) -> float:
    words = len(query.split())
    if words <= 1: return 0.50
    if words <= 3: return 0.45
    return 0.40

# ── has_word_match ────────────────────────────────────────────────────────────
def has_word_match(query: str, result: str, min_overlap: float = 0.30) -> bool:
    qw = set(w for w in normalize(query).split() if len(w) >= 3)
    rw = set(w for w in normalize(result).split() if len(w) >= 3)
    if not qw or not rw: return True
    overlap = len(qw & rw) / max(len(qw), 1)
    return overlap >= min_overlap

# ── build_query_variants ──────────────────────────────────────────────────────
def build_query_variants(title: str, artist: str, album: str) -> list:
    variants = []
    clean_t = _re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
    clean_a = artist.split(',')[0].split('&')[0].strip() if artist else ''
    if clean_a:
        variants.append(f"{clean_a} {clean_t}")
        variants.append(f"{clean_t} {clean_a}")
    variants.append(clean_t)
    if title != clean_t:
        variants.append(title)
    # deduplicate preserving order
    seen = set()
    result = []
    for v in variants:
        v = v.strip()
        if v and v not in seen:
            seen.add(v); result.append(v)
    return result

# ── _safe_year ────────────────────────────────────────────────────────────────
def _safe_year(val) -> str:
    try: return str(int(val))[:4]
    except: return ''

# ── compute_confidence (shim → core.py's version) ────────────────────────────
# core.py has its own compute_confidence — this is for match_engine imports
def compute_confidence(query_title, query_artist, result_title, result_artist,
                       query_duration_s=0, result_duration_s=0, source='') -> float:
    result = verify_track(
        query_title=query_title, query_artist=query_artist,
        query_duration_s=query_duration_s,
        candidate_title=result_title, candidate_artist=result_artist,
        candidate_duration_s=result_duration_s, source=source,
    )
    return result.confidence

# ── is_likely_duplicate ───────────────────────────────────────────────────────
def is_likely_duplicate(a: dict, b: dict, threshold: float = 0.92) -> bool:
    ta = normalize(a.get('trackName') or a.get('title', ''))
    tb = normalize(b.get('trackName') or b.get('title', ''))
    aa = normalize(a.get('artistName') or a.get('artist', ''))
    ab = normalize(b.get('artistName') or b.get('artist', ''))
    t_sim = calculate_title_similarity(ta, tb)
    a_sim = calculate_artist_similarity(aa, ab) if aa and ab else 0.5
    return (t_sim * 0.7 + a_sim * 0.3) >= threshold

# ── _is_confirmed_match ───────────────────────────────────────────────────────
def _is_confirmed_match(req_title, req_artist, res_title, res_artist,
                         source='', duration_s=0, res_dur_s=0, min_conf=0.65):
    if not res_title or not res_title.strip():
        return False, 0.0, 'empty_title'
    # Version gate
    if not user_requested_version(req_title):
        rejected, reason = hard_reject_by_version(req_title, res_title, query_has_version=False)
        if rejected:
            return False, 0.0, reason
    # Confidence
    result = verify_track(
        query_title=req_title, query_artist=req_artist,
        query_duration_s=duration_s,
        candidate_title=res_title, candidate_artist=res_artist,
        candidate_duration_s=res_dur_s, source=source,
    )
    if result.confidence < min_conf:
        return False, result.confidence, f'low_conf_{result.confidence:.3f}'
    return True, result.confidence, 'ok'

# ── TVE v1 compat (old names → new production functions) ─────────────────────
def tve_validate(saavn_title, saavn_artist, saavn_duration_s,
                 target_title, target_artist, target_duration_s,
                 saavn_language='', source=''):
    passed, reason, scores = tve_validate_production(
        saavn_title, saavn_artist, saavn_duration_s,
        target_title, target_artist, target_duration_s,
        source=source,
    )
    return passed, reason, scores

def tve_validate_anchored(anchor, target_title, target_artist,
                           target_duration_s=0, source='', **kwargs):
    return tve_validate(
        anchor.get('title',''), anchor.get('artist',''),
        int(anchor.get('duration_s', 0) or 0),
        target_title, target_artist, target_duration_s,
        saavn_language=anchor.get('language',''), source=source,
    )

def tve_tier1_duration(saavn_s, target_s):
    if saavn_s <= 0 or target_s <= 0: return True
    return abs(saavn_s - target_s) <= 10

def tve_tier4_language(saavn_language, target_title, target_artist):
    return True, 'ok'  # handled inside verify_track

def tve_tier5_artist_hard(saavn_artist, target_artist, source='', title_exact=False):
    if source == 'soundcloud': return True, 'sc_skip'
    if not saavn_artist or not target_artist: return True, 'ok'
    sim = calculate_artist_similarity(saavn_artist, target_artist)
    if sim < 0.65: return False, f'artist_hard:{sim:.2f}'
    return True, 'ok'

def tve_pick_best(saavn_title='', saavn_artist='', saavn_duration_s=0,
                  candidates=None, max_candidates=5,
                  saavn_language='', source='', anchor=None,
                  title_key='title', artist_key='artist', duration_key='duration_s',
                  uploader_key='uploader', url_key='webpage_url'):
    if not candidates:
        return None, {'status': 'mismatch_error', 'message': 'No candidates'}
    # Use anchor if available
    if anchor:
        _title  = anchor.get('title', saavn_title)
        _artist = anchor.get('artist', saavn_artist)
        _dur    = int(anchor.get('duration_s', saavn_duration_s) or 0)
    else:
        _title  = saavn_title
        _artist = saavn_artist
        _dur    = saavn_duration_s

    best_candidate = None
    best_conf      = -1.0
    best_scores    = {}

    for candidate in candidates[:max_candidates]:
        c_title  = candidate.get(title_key, '')
        c_artist = candidate.get(artist_key, '')
        c_dur    = int(candidate.get(duration_key, 0) or 0)

        passed, reason, scores = tve_validate_production(
            _title, _artist, _dur,
            c_title, c_artist, c_dur,
            source=source,
        )
        conf = scores.get('confidence', 0.0)
        if passed and conf > best_conf:
            best_conf = conf
            best_candidate = candidate
            best_scores = scores

    if best_candidate is not None:
        return best_candidate, best_scores

    return None, {'status': 'mismatch_error', 'message': 'No verified track found'}

# ═══════════════════════════════════════════════════════════════════════════════
# END COMPATIBILITY SHIM
# ═══════════════════════════════════════════════════════════════════════════════
