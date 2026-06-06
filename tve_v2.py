"""
TVE v2.0 — Track Verification Engine
=====================================
7-Stage Zero-Guess Matching Pipeline for Aurum Music.

Architecture:
  Stage 1 → Metadata Cleaning
  Stage 2 → Artist Verification        (mandatory, >= 0.90)
  Stage 3 → Duration Validation        (graduated penalty)
  Stage 4 → Version Detection          (configurable blacklist)
  Stage 5 → Channel Reliability        (source trust scoring)
  Stage 6 → Confidence Scoring         (weighted model)
  Stage 7 → Final Verification         (hard threshold >= 0.90)

Design principle:
  Never guess. Fewer correct results > more wrong results.
  A wrong song is a system failure.
"""

from __future__ import annotations
import re
import math
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("aurum.tve")

# ══════════════════════════════════════════════════════════════════════════════
# STAGE 1 ── METADATA CLEANING
# ══════════════════════════════════════════════════════════════════════════════

# Noise tokens stripped from both Saavn and YT/SC titles before comparison
_NOISE_TOKENS = re.compile(
    r'(?i)\b('
    r'official|video|audio|lyrics|lyrical|full\s+video|full\s+song|'
    r'hd|hq|4k|8k|mp3|'
    r'feat(?:uring)?|ft|'
    r'new\s+song|latest|superhit|super\s+hit|blockbuster|'
    r'2019|2020|2021|2022|2023|2024|2025|'
    r'music|visualizer|reaction|episode|'
    r'song|gana|gaana|'
    r'hindi\s+song|bhojpuri\s+song|punjabi\s+song|'
    r'dj\s+wale|wala|wali|wale|'
    r'jukebox|nonstop|back\s+to\s+back'
    r')\b'
)
_BRACKET_RE    = re.compile(r'[\[\](){}<>]')
_SPECIAL_RE    = re.compile(r'[-|_/\\+]+')
_WHITESPACE_RE = re.compile(r'\s{2,}')
_DEVANAGARI_RE = re.compile(r'[\u0900-\u097F]+')

# Trailing meta suffix: "Song Name - Official Video" → "Song Name"
_TRAILING_META = re.compile(
    r'(?i)\s*[-|]\s*(?:official|audio|video|lyrics|full\s+song|hd|hq|'
    r'ft\.?\s+\w+|feat\.?\s+\w+)\s*$'
)


def clean_title(text: str) -> str:
    """
    Stage 1: Aggressive metadata cleaning.
    Returns normalized lowercase title ready for comparison.
    """
    if not text:
        return ''
    t = text.lower().strip()
    t = _DEVANAGARI_RE.sub(' ', t)        # strip Devanagari noise
    t = _BRACKET_RE.sub(' ', t)           # remove brackets
    t = _TRAILING_META.sub('', t)         # strip trailing "- Official Audio"
    t = _NOISE_TOKENS.sub(' ', t)         # remove noise tokens
    t = _SPECIAL_RE.sub(' ', t)           # normalize special chars
    t = _WHITESPACE_RE.sub(' ', t).strip()
    return t


def clean_artist(text: str) -> str:
    """
    Stage 1: Normalize artist string.
    Strips featuring clauses, normalizes separators.
    """
    if not text:
        return ''
    t = text.lower().strip()
    # Remove featuring clause entirely
    t = re.sub(r'\s*(?:feat\.?|ft\.?|featuring|with|x|&)\s+.*', '', t)
    t = re.sub(r'\s*\(.*?\)', '', t)      # remove parenthetical
    t = re.sub(r'[^a-z0-9\s,]', ' ', t)  # keep letters, numbers, commas
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def primary_artist(text: str) -> str:
    """Extract first/primary artist from a multi-artist string."""
    c = clean_artist(text)
    parts = re.split(r'\s*[,&]\s*|\s+x\s+|\s+and\s+', c)
    return parts[0].strip() if parts else c


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 4 ── VERSION DETECTION (defined before Stage 2 — imported by it)
# ══════════════════════════════════════════════════════════════════════════════

# Configurable blacklist — add/remove without touching logic
VERSION_BLACKLIST: set[str] = {
    # Remixes & DJ edits
    'remix', 'remixed', 'dj remix', 'dj mix', 'dj edit', 'dj version',
    'dj drop', 'dj mashup', 'dj cut', 'dj blend', 'dj flip',
    'dj bootleg', 'bootleg', 'flip', 'edit', 'radio edit',
    'extended mix', 'extended version', 'club mix', 'club version', 'club edit',
    'dance mix', 'mashup', 'mash up', 'mash-up',
    # Covers
    'cover', 'cover version', 'tribute', 'dedication',
    # Live
    'live', 'live version', 'live at', 'live from', 'live session',
    'live performance', 'concert', 'unplugged', 'mtv unplugged',
    'coke studio', 'studio session', 'stripped', 'acoustic',
    'acoustic version', 'acoustic cover',
    # Degraded / altered audio
    'slowed', 'slowed down', 'slowed reverb', 'reverb', 'reverb version',
    'nightcore', 'sped up', 'speed up', 'sped-up', 'pitched',
    'bass boosted', 'bass boost', '8d audio', '8d', '8d music',
    'chopped', 'screwed', 'lofi', 'lo-fi', 'lo fi',
    'chill mix', 'chill version',
    # Karaoke / Instrumental
    'karaoke', 'karaoke version', 'instrumental', 'instrumental version',
    'minus one', 'backing track',
    # Indian-specific
    'jhankar', 'jhankar beats', 'tapori mix', 'dhol mix',
    'wedding mix', 'bhangra mix', 'dandiya mix', 'garba mix',
    'festival mix', 'party mix', 'beats version',
    # Other
    'fan made', 'fanmade', 'fan video', 'fan edit',
    'lyric video', 'lyrics video', 'lyrical video',
    'piano version', 'guitar version', 'violin version',
    'recreated', 'remake', 'remaster', 'remastered',
    'alternate', 'alternate version', 'alternate take',
    'reprise', 'bonus track', 'demo', 'demo version',
    'ost version', 'film version', 'movie version',
}

# Multi-word phrases need substring search; single words use word-boundary
_VERSION_MULTI  = {v for v in VERSION_BLACKLIST if ' ' in v}
_VERSION_SINGLE = {v for v in VERSION_BLACKLIST if ' ' not in v}
_VERSION_SINGLE_RE = re.compile(
    r'(?i)\b(' + '|'.join(re.escape(v) for v in sorted(_VERSION_SINGLE, key=len, reverse=True)) + r')\b'
)

# DJ artist names — must NOT be treated as remix indicators
_KNOWN_DJ_ARTISTS = {
    'dj snake', 'dj khaled', 'dj bravo', 'dj bobo',
    'dj antoine', 'dj fresh', 'dj shadow', 'dj premier',
}


def is_version_variant(title: str, artist: str = '') -> tuple[bool, str]:
    """
    Stage 4: Detect version variants that should be rejected.
    Returns (is_version: bool, matched_indicator: str).

    Special cases:
    - Artist names like "DJ Snake" are NOT remix indicators
    - Saavn title itself contains "remix" → ALLOW (user wanted remix)
    """
    t_lower = title.lower()

    # Multi-word blacklist — substring search
    for phrase in _VERSION_MULTI:
        if phrase in t_lower:
            # Guard: is phrase part of artist name?
            if artist and phrase in artist.lower():
                continue
            return True, phrase

    # Single-word blacklist — word boundary
    m = _VERSION_SINGLE_RE.search(title)
    if m:
        matched = m.group(1).lower()
        # Guard: DJ artist names
        if matched == 'dj':
            # "DJ Snake" at start of title or artist = artist name, not remix
            if artist and any(artist.lower().startswith(dj) for dj in _KNOWN_DJ_ARTISTS):
                return False, ''
            # Check if it's a known DJ artist in title
            if any(dj in t_lower for dj in _KNOWN_DJ_ARTISTS):
                return False, ''
            # "DJ" elsewhere in title = remix indicator
            return True, 'dj'
        # Guard: "live" can be part of artist name e.g. "Live" the band
        if matched == 'live' and artist:
            if 'live' in primary_artist(artist).lower():
                return False, ''
        return True, matched

    return False, ''


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 2 ── ARTIST VERIFICATION
# ══════════════════════════════════════════════════════════════════════════════

# Minimum artist similarity — mandatory, not optional
ARTIST_MIN_CONFIDENCE = 0.90

try:
    from rapidfuzz import fuzz as _rf
    _RAPIDFUZZ = True
except ImportError:
    _RAPIDFUZZ = False
    log.warning("[TVE] rapidfuzz not available — falling back to difflib (lower accuracy)")


def _fuzzy(a: str, b: str) -> float:
    if _RAPIDFUZZ:
        return _rf.token_sort_ratio(a, b) / 100.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


def artist_similarity(req_artist: str, result_artist: str) -> float:
    """
    Stage 2: Multi-strategy artist similarity.
    Returns 0.0–1.0.

    Strategies (in priority order):
    1. Full normalized token_sort
    2. Primary-vs-primary token_sort
    3. Full name containment (min 8 chars — blocks short noise)
    4. Shared-surname penalty — if both multi-token AND first tokens differ → cap at 0.52
    """
    if not req_artist or not result_artist:
        return 1.0  # unknown → neutral, let other stages decide

    rq = clean_artist(req_artist)
    rr = clean_artist(result_artist)
    pq = primary_artist(req_artist)
    pr = primary_artist(result_artist)

    if not rq or not rr:
        return 1.0

    s1 = _fuzzy(rq, rr)
    s2 = _fuzzy(pq, pr)

    # Full name containment (e.g. "narendra chanchal" in "pt narendra chanchal")
    s3 = 0.0
    if len(pq) >= 8 and pq in rr: s3 = 0.95
    if len(pr) >= 8 and pr in rq: s3 = max(s3, 0.95)
    if len(rq) >= 8 and rq in rr: s3 = max(s3, 0.95)
    if len(rr) >= 8 and rr in rq: s3 = max(s3, 0.95)

    score = max(s1, s2, s3)

    # Shared-surname penalty: both multi-token, first tokens differ
    # Prevents "Neha Kakkar" matching "Tony Kakkar" via shared "kakkar"
    fq = rq.split()[0] if rq.split() else ''
    fr = rr.split()[0] if rr.split() else ''
    if ' ' in rq and ' ' in rr and fq and fr and len(fq) >= 3 and len(fr) >= 3:
        first_sim = _fuzzy(fq, fr)
        if first_sim < 0.60:
            score = min(score, 0.52)

    return score


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 3 ── DURATION VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def duration_score(saavn_s: int, target_s: int) -> tuple[float, str]:
    """
    Stage 3: Graduated duration scoring.
    Returns (score: 0.0–1.0, grade: str).

    Rules:
      0–2s   → 1.00  (perfect)
      3–5s   → 0.80  (small penalty)
      6–10s  → 0.50  (major penalty)
      > 10s  → 0.00  (reject)
      Either 0 → 0.70 (unknown — moderate penalty, don't skip)
    """
    if saavn_s <= 0 or target_s <= 0:
        return 0.70, 'unknown'
    delta = abs(saavn_s - target_s)
    if delta <= 2:
        return 1.00, 'perfect'
    if delta <= 5:
        return 0.80, 'small_penalty'
    if delta <= 10:
        return 0.50, 'major_penalty'
    return 0.00, 'reject'


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 5 ── CHANNEL RELIABILITY
# ══════════════════════════════════════════════════════════════════════════════

# Source reliability tiers — score 0.0–1.0
_CHANNEL_TRUST: dict[str, float] = {
    # Tier 1 — Official
    'youtube_music_official':  1.00,   # music.youtube.com official topic
    'official_artist_channel': 1.00,
    'verified_label':          0.95,
    # Tier 2 — Reliable aggregators
    'saavn':                   1.00,
    'jiosavan':                1.00,
    'ytmusic':                 0.90,   # YouTube Music topic
    'youtube':                 0.75,   # Generic YT
    # Tier 3 — Lower trust
    'soundcloud':              0.65,
    'piped':                   0.70,
    'invidious':               0.65,
    # Tier 4 — Unreliable (detected patterns)
    'lyrics_channel':          0.40,
    'fan_channel':             0.35,
    'mashup_channel':          0.20,
    'unknown':                 0.50,
}

# Patterns in channel/uploader name → downgrade trust
_LOW_TRUST_PATTERNS = re.compile(
    r'(?i)\b(lyrics?|lyrical|karaoke|remix|mashup|cover|fan|'
    r'beats?|lofi|chill|slowed|nightcore|bass\s*boost|'
    r'8d|unplugged|acoustic)\b'
)
_TOPIC_CHANNEL_RE = re.compile(r'(?i)\s*-\s*topic\s*$')


def channel_trust_score(source: str, uploader: str = '', webpage_url: str = '') -> float:
    """
    Stage 5: Channel reliability scoring.

    Priority:
    1. music.youtube.com Topic channel → highest trust
    2. Low-trust pattern in uploader name → downgrade
    3. Source type fallback
    """
    # YouTube Music topic channel ("Artist Name - Topic")
    if webpage_url and 'music.youtube.com' in webpage_url:
        if _TOPIC_CHANNEL_RE.search(uploader or ''):
            return _CHANNEL_TRUST['youtube_music_official']
        return _CHANNEL_TRUST['ytmusic']

    # Uploader name contains low-trust patterns
    if uploader and _LOW_TRUST_PATTERNS.search(uploader):
        return _CHANNEL_TRUST['lyrics_channel']

    return _CHANNEL_TRUST.get(source, _CHANNEL_TRUST['unknown'])


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 6 ── CONFIDENCE SCORING
# ══════════════════════════════════════════════════════════════════════════════

# Weighted confidence model
_W_TITLE    = 0.40
_W_ARTIST   = 0.30
_W_DURATION = 0.20
_W_SOURCE   = 0.10

# Minimum final confidence to accept a match
TVE_MIN_CONFIDENCE = 0.90

# Minimum artist similarity — hard gate, not weighted
TVE_ARTIST_HARD_MIN = 0.90


@dataclass
class TVEResult:
    """Result from TVE pipeline — either a verified match or a rejection."""
    passed:     bool
    confidence: float = 0.0
    stage:      str   = ''     # stage where decision was made
    reason:     str   = ''     # rejection reason or 'ok'

    # Scores per dimension
    title_score:    float = 0.0
    artist_score:   float = 0.0
    duration_score: float = 0.0
    source_score:   float = 0.0
    duration_grade: str   = ''

    # Candidate details (for logging)
    candidate_title:  str = ''
    candidate_artist: str = ''
    candidate_dur:    int = 0


@dataclass
class TVELog:
    """Rejection log entry — stored permanently for audit."""
    ts:               float  = field(default_factory=time.time)
    saavn_title:      str    = ''
    saavn_artist:     str    = ''
    candidate_title:  str    = ''
    candidate_artist: str    = ''
    candidate_dur:    int    = 0
    confidence:       float  = 0.0
    stage:            str    = ''
    reason:           str    = ''

    def to_dict(self) -> dict:
        return {
            'ts':               self.ts,
            'saavn_title':      self.saavn_title,
            'saavn_artist':     self.saavn_artist,
            'candidate_title':  self.candidate_title,
            'candidate_artist': self.candidate_artist,
            'candidate_dur':    self.candidate_dur,
            'confidence':       round(self.confidence, 4),
            'stage':            self.stage,
            'reason':           self.reason,
        }


# In-memory rejection log (last 500 entries)
_rejection_log: list[TVELog] = []
_MAX_LOG = 500


def _log_rejection(entry: TVELog) -> None:
    global _rejection_log
    _rejection_log.append(entry)
    if len(_rejection_log) > _MAX_LOG:
        _rejection_log = _rejection_log[-_MAX_LOG:]
    log.debug(
        f"[TVE] REJECT stage={entry.stage} reason={entry.reason} "
        f"conf={entry.confidence:.3f} "
        f"candidate='{entry.candidate_title}' by '{entry.candidate_artist}'"
    )


def get_rejection_log() -> list[dict]:
    """Return recent rejection log as list of dicts (for /api/tve/log endpoint)."""
    return [e.to_dict() for e in reversed(_rejection_log)]


def title_similarity(saavn_title: str, target_title: str) -> float:
    """Title similarity on cleaned strings."""
    a = clean_title(saavn_title)
    b = clean_title(target_title)
    if not a or not b:
        return 0.0
    return _fuzzy(a, b)


def compute_tve_confidence(
    title_sim:    float,
    artist_sim:   float,
    dur_score:    float,
    source_score: float,
) -> float:
    """
    Stage 6: Weighted confidence.
    Title 40% + Artist 30% + Duration 20% + Source 10%
    """
    return (
        title_sim    * _W_TITLE    +
        artist_sim   * _W_ARTIST   +
        dur_score    * _W_DURATION +
        source_score * _W_SOURCE
    )


# ══════════════════════════════════════════════════════════════════════════════
# STAGE 7 ── FINAL VERIFICATION (core pipeline)
# ══════════════════════════════════════════════════════════════════════════════

def tve_verify(
    # Saavn ground truth
    saavn_title:    str,
    saavn_artist:   str,
    saavn_dur_s:    int,
    saavn_language: str = '',
    # Candidate from YT/SC
    target_title:   str = '',
    target_artist:  str = '',
    target_dur_s:   int = 0,
    source:         str = '',          # 'youtube', 'ytmusic', 'soundcloud', etc.
    uploader:       str = '',          # raw channel/uploader name
    webpage_url:    str = '',          # for topic channel detection
) -> TVEResult:
    """
    7-Stage TVE Pipeline. Core verification function.

    Returns TVEResult(passed=True, confidence>=0.90) on success.
    Returns TVEResult(passed=False, reason=...) on any rejection.

    Never guesses. If confidence < 0.90 → reject.
    """

    def _reject(stage: str, reason: str, conf: float = 0.0,
                 t_score: float = 0.0, a_score: float = 0.0,
                 d_score: float = 0.0, s_score: float = 0.0,
                 d_grade: str = '') -> TVEResult:
        entry = TVELog(
            saavn_title=saavn_title, saavn_artist=saavn_artist,
            candidate_title=target_title, candidate_artist=target_artist,
            candidate_dur=target_dur_s, confidence=conf,
            stage=stage, reason=reason,
        )
        _log_rejection(entry)
        return TVEResult(
            passed=False, confidence=conf, stage=stage, reason=reason,
            title_score=t_score, artist_score=a_score,
            duration_score=d_score, source_score=s_score, duration_grade=d_grade,
            candidate_title=target_title, candidate_artist=target_artist,
            candidate_dur=target_dur_s,
        )

    # ── Stage 1: Metadata Cleaning (applied implicitly in each stage) ─────────
    # Cleaning happens inside title_similarity() and artist_similarity()

    # ── Stage 2: Artist Verification (MANDATORY HARD GATE) ───────────────────
    # SoundCloud exception: uploader ≠ artist — skip hard gate, use 0.70 neutral
    if source == 'soundcloud':
        a_score = 0.70  # SC uploader unreliable
    else:
        a_score = artist_similarity(saavn_artist, target_artist)
        if a_score < TVE_ARTIST_HARD_MIN:
            return _reject(
                stage='S2_artist',
                reason=f'artist_sim={a_score:.3f}<{TVE_ARTIST_HARD_MIN}',
                a_score=a_score,
            )

    # ── Stage 3: Duration Validation ─────────────────────────────────────────
    d_score, d_grade = duration_score(saavn_dur_s, target_dur_s)
    if d_grade == 'reject':
        return _reject(
            stage='S3_duration',
            reason=f'duration_delta={abs(saavn_dur_s - target_dur_s)}s>10s',
            a_score=a_score, d_score=d_score, d_grade=d_grade,
        )

    # ── Stage 4: Version Detection ────────────────────────────────────────────
    # Check if Saavn query itself is a version (user explicitly wants it)
    saavn_is_version, _ = is_version_variant(saavn_title)
    target_is_version, version_match = is_version_variant(target_title, target_artist)

    if not saavn_is_version and target_is_version:
        return _reject(
            stage='S4_version',
            reason=f'version_detected:{version_match}',
            a_score=a_score, d_score=d_score, d_grade=d_grade,
        )

    # ── Stage 5: Channel Reliability ─────────────────────────────────────────
    s_score = channel_trust_score(source, uploader, webpage_url)

    # Low-trust channel: lyrics/fan/mashup → hard reject
    # These channels almost always serve incorrect audio regardless of title match
    if s_score <= 0.40:
        return _reject(
            stage='S5_channel',
            reason=f"low_trust_channel:'{uploader}'_score={s_score:.2f}",
            a_score=a_score, d_score=d_score, s_score=s_score,
        )

    # ── Stage 6: Title Similarity ─────────────────────────────────────────────
    t_score = title_similarity(saavn_title, target_title)

    # Language cross-check: only block CLEARLY English results for Hindi/regional queries
    # Skip for SoundCloud — uploader name is channel name, not artist, unreliable for lang detection
    if saavn_language and source != 'soundcloud':
        _lang = saavn_language.lower()
        if _lang in ('hindi', 'bhojpuri', 'punjabi', 'rajasthani', 'haryanvi'):
            _tgt_text = (clean_title(target_title) + ' ' + clean_artist(target_artist)).lower()
            _has_indian = bool(re.search(
                r'\b(kumar|singh|sharma|yadav|ji|lal|devi|baba|shri|'
                r'rahman|arijit|neha|kakkar|badshah|atif|udit|sonu|lata|'
                r'asha|rafi|kishore|mukesh|gulshan|shreya|sunidhi|'
                r'narendra|chanchal|pawan|khesari|bhosle|mangeshkar|'
                r'diljit|dosanjh|guru|honey|ranveer|mohit|kailash|'
                r'vishal|shekhar|pritam|shankar|ehsaan|rajesh|suresh|'
                r'ankit|tiwari|armaan|malik|jubin|nautiyal|darshan|'
                r'raval|bhoomi|trivedi|sachin|jigar|talat|geeta|noor)\b',
                _tgt_text
            ))
            if not _has_indian and bool(re.search(r'[a-z]{4,}', _tgt_text)):
                return _reject(
                    stage='S6_language',
                    reason=f'lang_cross:saavn={_lang}_target=clearly_english',
                    t_score=t_score, a_score=a_score, d_score=d_score, s_score=s_score,
                )

    # ── Stage 6: Confidence Scoring ───────────────────────────────────────────
    confidence = compute_tve_confidence(t_score, a_score, d_score, s_score)

    # ── Stage 7: Final Verification ───────────────────────────────────────────
    # SoundCloud: artist score is always 0.70 neutral (uploader ≠ artist)
    # So effective ceiling for SC is lower — use 0.85 threshold for SC
    _min_conf = 0.85 if source == 'soundcloud' else TVE_MIN_CONFIDENCE
    if confidence < _min_conf:
        return _reject(
            stage='S7_confidence',
            reason=f'confidence={confidence:.3f}<{_min_conf}',
            conf=confidence,
            t_score=t_score, a_score=a_score, d_score=d_score, s_score=s_score,
            d_grade=d_grade,
        )

    # ── PASS ──────────────────────────────────────────────────────────────────
    log.debug(
        f"[TVE] ✓ PASS conf={confidence:.3f} "
        f"t={t_score:.2f} a={a_score:.2f} d={d_score:.2f}({d_grade}) s={s_score:.2f} "
        f"'{target_title}' by '{target_artist}'"
    )
    return TVEResult(
        passed=True, confidence=confidence, stage='S7_pass', reason='ok',
        title_score=t_score, artist_score=a_score,
        duration_score=d_score, source_score=s_score, duration_grade=d_grade,
        candidate_title=target_title, candidate_artist=target_artist,
        candidate_dur=target_dur_s,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ANCHOR-BASED VERIFY (uses Saavn metadata dict directly)
# ══════════════════════════════════════════════════════════════════════════════

def tve_verify_anchored(
    anchor: dict,
    target_title:  str,
    target_artist: str,
    target_dur_s:  int  = 0,
    source:        str  = '',
    uploader:      str  = '',
    webpage_url:   str  = '',
) -> TVEResult:
    """
    Anchor-based verification.
    Uses stored Saavn metadata dict as ground truth.
    More accurate than string params — language/duration are known, not guessed.
    """
    return tve_verify(
        saavn_title    = anchor.get('title', ''),
        saavn_artist   = anchor.get('artist', ''),
        saavn_dur_s    = int(anchor.get('duration_s', 0) or 0),
        saavn_language = anchor.get('language', ''),
        target_title   = target_title,
        target_artist  = target_artist,
        target_dur_s   = target_dur_s,
        source         = source,
        uploader       = uploader,
        webpage_url    = webpage_url,
    )


# ══════════════════════════════════════════════════════════════════════════════
# TOP-N PICKER — used in fetcher loops
# ══════════════════════════════════════════════════════════════════════════════

NO_MATCH_FOUND = {
    'status':  'NO_MATCH_FOUND',
    'message': 'TVE: No verified track found after evaluating all candidates',
}


def tve_pick_best(
    candidates:     list,          # list of dicts
    saavn_title:    str   = '',
    saavn_artist:   str   = '',
    saavn_dur_s:    int   = 0,
    saavn_language: str   = '',
    source:         str   = '',
    anchor:         dict  = None,
    max_candidates: int   = 5,
    # dict key names in candidates
    title_key:      str   = 'title',
    artist_key:     str   = 'artist',
    dur_key:        str   = 'duration_s',
    uploader_key:   str   = 'uploader',
    url_key:        str   = 'webpage_url',
) -> tuple[Optional[dict], TVEResult]:
    """
    Evaluate up to max_candidates through the 7-stage TVE pipeline.
    Returns (best_candidate_dict, TVEResult) on success.
    Returns (None, TVEResult(NO_MATCH_FOUND)) if all candidates fail.

    Never returns a wrong result. If uncertain → NO_MATCH_FOUND.
    """
    best_candidate: Optional[dict] = None
    best_result:    Optional[TVEResult] = None

    for i, candidate in enumerate(candidates[:max_candidates]):
        c_title   = candidate.get(title_key, '')
        c_artist  = candidate.get(artist_key, '')
        c_dur     = int(candidate.get(dur_key, 0) or 0)
        c_upload  = candidate.get(uploader_key, '')
        c_url     = candidate.get(url_key, '')

        if anchor:
            result = tve_verify_anchored(
                anchor, c_title, c_artist, c_dur,
                source=source, uploader=c_upload, webpage_url=c_url,
            )
        else:
            result = tve_verify(
                saavn_title=saavn_title, saavn_artist=saavn_artist,
                saavn_dur_s=saavn_dur_s, saavn_language=saavn_language,
                target_title=c_title, target_artist=c_artist,
                target_dur_s=c_dur, source=source,
                uploader=c_upload, webpage_url=c_url,
            )

        if result.passed:
            # If we already have a best, only replace if confidence is meaningfully higher
            if best_result is None or result.confidence > best_result.confidence + 0.02:
                best_candidate = candidate
                best_result    = result
            # Early exit if near-perfect
            if best_result.confidence >= 0.97:
                break

    if best_candidate is not None:
        log.info(
            f"[TVE] ✓ Best match: conf={best_result.confidence:.3f} "
            f"'{best_result.candidate_title}' by '{best_result.candidate_artist}'"
        )
        return best_candidate, best_result

    # All candidates failed
    log.warning(
        f"[TVE] NO_MATCH_FOUND for '{saavn_title}' by '{saavn_artist}' "
        f"after {min(len(candidates), max_candidates)} candidates"
    )
    return None, TVEResult(
        passed=False, stage='NO_MATCH', reason='all_candidates_failed',
        candidate_title=saavn_title, candidate_artist=saavn_artist,
    )

