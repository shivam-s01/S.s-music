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

# ═══════════════════════════════════════════════════════════════════════════════
# VERSION DNA — PERMANENT MISMATCH PREVENTION
# ═══════════════════════════════════════════════════════════════════════════════

# Definite version words — any of these in result → REJECT when user didn't ask
_VERSION_DNA = {
    # Lofi / slowed
    'lofi', 'lo-fi', 'lo fi', 'slowed', 'reverb', 'slowed reverb',
    'nightcore', 'sped up', 'speed up', 'pitched', 'chopped', 'screwed',
    '8d audio', '8d', 'bass boosted', 'bass boost',
    # Remix / DJ
    'remix', 'dj remix', 'dj mix', 'dj version', 'dj edit', 'dj drop',
    'mashup', 'mash up', 'bootleg', 'flip', 'rework',
    # Cover / Karaoke
    'cover', 'cover version', 'tribute', 'karaoke', 'instrumental',
    'minus one',
    # Live / Session
    'live version', 'live at', 'live from', 'live session',
    'acoustic version',
    # BUG-48 FIX: 'unplugged' moved here from _AMBIGUOUS_DNA — it is ALWAYS a version
    'unplugged', 'stripped',
    'coke studio', 'mtv unplugged', 'nescafe basement',
    'velo sound', 'tiny desk', 'spotify session', 'studio session',
    # Extended / Club
    'extended mix', 'extended version', 'club mix', 'dance mix',
    'radio edit', 'club version', 'club edit',
    'festival mix', 'party mix',
    # Indian specific
    'jhankar', 'jhankar beats', 'tapori mix', 'dhol mix',
    'wedding mix', 'bhangra mix', 'dandiya mix', 'garba mix',
    'beats version',
    # Lyric video (not the actual song)
    'lyric video', 'lyrics video',
    # ── BUG-02 FIX: Gender versions were missing — these are DIFFERENT songs ──
    'female version', 'male version', 'girl version', 'boy version',
    'female cover', 'male cover',
    # Speed-altered
    'slow version', 'fast version', 'speed version',
    # Additional missed patterns (BUG-38 FIX)
    'slowed + reverb', 'slowed+reverb', 'lofi remix', 'chill beats',
    'sad version', 'trending version', 'latest version',
    # BUG-45 FIX: 'remastered' moved here — it is always a different audio release
    'remastered', 'remastered version', 'anniversary edition',
    'ost version', 'film version', 'movie version',
    'promo version', 'title track version',
}

# DJ word boundary check (special case — avoids matching words like 'djinn')
_DJ_WORD_RE = re.compile(r'\bdj\b', re.IGNORECASE)

# Context-dependent words — only flag when a version-context word is nearby
# BUG-03 FIX: Added bracket-only guard for standalone "live", "acoustic", etc.
# BUG-44/45 FIX: 'unplugged', 'stripped', 'remastered' removed — now in _VERSION_DNA
_AMBIGUOUS_DNA = {
    'live', 'acoustic', 'cover', 'edit',
    'concert', 'performance', 'tribute',
}
_VERSION_CONTEXT_RE = re.compile(
    r'\b(version|ver|mix|edit|remix|session|perform|concert|tour|record|cut|show)\b',
    re.IGNORECASE
)


def get_song_dna(title: str) -> set:
    """
    Extract version DNA from song title.
    Returns: set of version types found {'lofi', 'remix', 'live', etc.}
    Empty set = clean original song.

    BUG-02 FIX: Now detects 'female version', 'male version', etc.
    BUG-03 FIX: Ambiguous words now require bracket OR context guard.
    BUG-39 FIX: 'jhankar' bare word now always flagged (it's always a version).
    """
    t = title.lower().strip()
    found = set()

    # DJ check (word boundary)
    if _DJ_WORD_RE.search(title):
        found.add('remix')

    # BUG-39 FIX: jhankar is NEVER an original song — always flag it
    if re.search(r'\bjhankar\b', t):
        found.add('jhankar')

    # Definite version words
    for word in _VERSION_DNA:
        if word == 'jhankar': continue  # already handled above
        if ' ' in word:
            if word in t:
                found.add(word)
        else:
            if re.search(r'\b' + re.escape(word) + r'\b', t):
                found.add(word)

    # Ambiguous words — require either:
    # (a) explicit version-context word nearby, OR
    # (b) word appears after dash/pipe at end of title, OR
    # (c) word is inside brackets/parentheses
    for word in _AMBIGUOUS_DNA:
        if re.search(r'\b' + re.escape(word) + r'\b', t):
            _has_context  = bool(_VERSION_CONTEXT_RE.search(t))
            _has_dash_end = bool(re.search(r'[-–|]\s*' + re.escape(word) + r'\s*$', t))
            _has_bracket  = bool(re.search(r'[\(\[]\s*' + re.escape(word) + r'\s*[\)\]]', t))
            # BUG-10 FIX: Also flag if it's the ONLY word in brackets at end
            _has_paren_end = bool(re.search(r'\(\s*' + re.escape(word) + r'\s*\)\s*$', t))
            if _has_context or _has_dash_end or _has_bracket or _has_paren_end:
                found.add(word)

    return found


def dna_compatible(query_title: str, result_title: str) -> bool:
    """
    PERMANENT MISMATCH PREVENTION:
    Query and result DNA must be compatible.

    - User asked for normal song → any version in result = REJECT
    - User asked for remix → result must have remix DNA
    - User asked for lofi → result must have lofi DNA

    BUG-01 FIX: Previously "Tum Live" (bare word "live" in title without
    context) was not detected as a version. Now uses the corrected get_song_dna
    which requires bracket/context/dash guard for ambiguous words —
    preventing false positives on song titles that legitimately contain
    ambiguous words (e.g., "Live and Let Die" is NOT a live version).

    BUG-10 FIX: Empty q_dna now correctly checks r_dna is also empty.
    This function is NEVER bypassed — always runs before confidence scoring.
    """
    q_dna = get_song_dna(query_title)
    r_dna = get_song_dna(result_title)

    # User asked for clean original song
    if not q_dna:
        # ANY version word in result → REJECT
        return len(r_dna) == 0

    # User asked for a specific version → result must share that version type
    return bool(q_dna & r_dna)


def has_version_words(title: str) -> bool:
    """Quick check — does title contain any version word."""
    return len(get_song_dna(title)) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def clean_query(text):
    """
    Strip metadata noise from query text.
    BUG-07 FIX: Now also strips 'female version', 'male version', etc.
    """
    text = re.sub(r'\(From\s+["\u201c\u201d\u2018\u2019]?[^)]*["\u201c\u201d\u2018\u2019]?\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(
        r'\(\s*(OST|official|audio|video|lyrics|full\s*song|feat\.?.*?|ft\.?.*?|'
        r'Hindi|English|Version|Remix|Cover|HD|HQ|Original|Soundtrack|Remastered|'
        r'Extended|Radio\s*Edit)\s*\)',
        '', text, flags=re.IGNORECASE
    )
    text = re.sub(r'\s*[-–]\s*(official|audio|video|lyrics|full\s*song|hd|hq|remastered).*$',
                  '', text, flags=re.IGNORECASE)
    text = re.sub(r'["\u201c\u201d\u2018\u2019\'()]', '', text)

    # BUG-07 FIX: Added female/male version patterns
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
        r'lyric\s*video|lyrics\s*video|full\s*video|'
        r'female\s*version|male\s*version|girl\s*version|boy\s*version|'
        r'female\s*cover|male\s*cover|slow\s*version|fast\s*version'
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

    try:
        # BUG-47 FIX: Pass already-cleaned title_c to translit, not raw title.
        # Raw title may contain brackets/parens that break word-boundary regexes.
        t_translit = _hindi_translit_normalize(title_c)
        if t_translit and t_translit != title_c:
            add(t_translit)
            if artist_first: add(f"{t_translit} {artist_first}")
    except Exception:
        pass

    _OLD_HINTS = ['90', '90s', 'purane', 'purana', 'purani', 'old', 'retro', '80', '70']
    _has_decade = any(h in (title.lower() + ' ' + artist.lower()) for h in _OLD_HINTS)
    if _has_decade and artist_c:
        add(f"{artist_c} {title_c} old")
        add(f"{title_c} old hindi")

    return variants


# BUG-08 FIX: Transliteration rules applied with word-boundary regex
# Previously some rules matched mid-word (e.g., 'aa' inside 'baad')
_HINDI_TRANSLIT = [
    ('pyaar', 'pyar'),   # multi-char replacements first
    ('dill',  'dil'),
    ('ishk',  'ishq'),
    ('hain',  'hai'),
    ('nah',   'na'),
    ('aa',    'a'),
    ('ee',    'i'),
    ('oo',    'u'),
    ('ae',    'ai'),
    ('ph',    'f'),
    ('bh',    'b'),
    ('gh',    'g'),
    ('kh',    'k'),
    ('th',    't'),
    ('dh',    'd'),
    ('sh',    's'),
    ('ch',    'c'),
    ('ie',    'i'),
    ('ey',    'ai'),
    ('ay',    'ai'),
    ('oi',    'oy'),
    ('ou',    'u'),
    ('ue',    'u'),
    ('hai',   'he'),
    ('ho',    'hu'),
    ('ki',    'ke'),
    ('ko',    'ku'),
]


def _hindi_translit_normalize(text: str) -> str:
    """
    BUG-08 FIX: Longer patterns applied first to avoid partial matches.
    Word boundary \b added where safe. Short vowel pairs (aa→a, ee→i)
    applied only when bounded by non-alpha characters to avoid corrupting
    words like 'baad' → 'bad' incorrectly (baad IS 'bad' in Hindi so this
    is actually correct, but 'saavn' should not become 'svn').
    """
    t = text.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    for src, dst in _HINDI_TRANSLIT:
        if len(src) >= 3:
            # Longer patterns: word boundary safe
            t = re.sub(r'\b' + re.escape(src) + r'\b', dst, t)
        else:
            # Short vowel digraphs: only at word boundaries to avoid
            # corrupting internal vowel clusters
            t = re.sub(r'(?<=\b)' + re.escape(src) + r'(?=\b)', dst, t)
    return t


def normalize(text):
    text = text.lower()
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
    """
    BUG-05 FIX: Threshold was 0.55 → caused "Dil" to match "Dal", "Teri" to
    match "Tere", and other short-word collisions producing wrong-song hits.
    Raised to 0.80 for short words (≤4 chars) and 0.75 for longer words.
    Prefix match and substring match kept as high-confidence fast paths.
    """
    if tw.startswith(qw): return 1.0
    if qw in tw: return 0.85
    max_len = max(len(qw), len(tw))
    if max_len == 0: return 0.0
    ratio = 1.0 - (levenshtein(qw, tw) / max_len)
    # Stricter floor for short words where one edit = big ratio drop
    min_ratio = 0.80 if max_len <= 4 else 0.75
    return ratio if ratio >= min_ratio else 0.0


def title_score(query, song_title, song_artist=''):
    """
    BUG-04 FIX: song_artist parameter was accepted but mixed into title scoring
    logic in some call sites. This function now ONLY scores the title match.
    Artist scoring is always done separately in compute_confidence().
    The parameter is kept for API compatibility but has no effect on scoring.
    """
    q, t = normalize(query), normalize(song_title)
    if not q: return 0.0
    if q == t: return 3.0
    q_words = q.split(); t_words = t.split()
    score = 0.0
    if t.startswith(q): score += 2.0
    title_match = sum(
        max((fuzzy_word_match(qw, tw) for tw in t_words), default=0.0)
        for qw in q_words
    )
    if q_words: score += (title_match / len(q_words)) * 1.5
    return score


def dynamic_min_score(query):
    """
    BUG-09 FIX: Single/double character queries had floor of 0.20 which is
    dangerously low and caused any random song to pass. Raised floors:
    - ≤2 chars: 0.40  (was 0.20 — practically no filter)
    - ≤5 chars: 0.50  (was 0.40)
    - ≤10 chars: 0.60  (was 0.55)
    - >10 chars: 0.68  (was 0.65)
    """
    length = len(normalize(query).replace(' ', ''))
    if length <= 2:    return 0.40   # BUG-09 FIX: was 0.20
    elif length <= 5:  return 0.50   # BUG-09 FIX: was 0.40
    elif length <= 10: return 0.60   # BUG-09 FIX: was 0.55
    else:              return 0.68   # BUG-09 FIX: was 0.65


def has_word_match(query, song_title):
    """
    BUG-06 FIX: Threshold was 0.55 — same problem as fuzzy_word_match.
    Raised to 0.75 to prevent wrong short-word songs from passing the
    word-match gate that precedes confidence scoring.
    BUG-46 FIX: Single short-word queries (≤4 chars) now require the first
    title word to START WITH the query word, preventing "Kal" matching "Dil".
    """
    q_words = normalize(query).split()
    t_words = normalize(song_title).split()
    if not q_words or not t_words: return True
    q_main  = [w for w in q_words if len(w) >= 3]
    t_main  = [w for w in t_words if len(w) >= 3]
    if not q_main: return True
    # BUG-46 FIX: Single short query word — require strong prefix match on t_words[0]
    if len(q_main) == 1 and len(q_main[0]) <= 4:
        return t_main and t_main[0].startswith(q_main[0])
    if t_main and q_main[0] == t_main[0]: return True
    for qw in q_main:
        for tw in t_main:
            if fuzzy_word_match(qw, tw) >= 0.75: return True  # BUG-06 FIX: was 0.55
    return False


def pick_best_quality(urls):
    if not urls: return None, None
    QUALITY_RANK = {
        '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
        '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
    }
    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK: return QUALITY_RANK[q]
        m = re.search(r'(\d+)', q)
        return int(m.group(1)) if m else -1
    for item in sorted(urls, key=rank, reverse=True):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'):
            return url, item.get('quality', 'unknown')
    return None, None


def pick_image(song):
    images = song.get('image') or []
    if isinstance(images, list) and images:
        for item in reversed(images):
            url = item.get('url') or item.get('link') or ''
            if url.startswith('http'):
                url = re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', url)
                return url
    if isinstance(images, str) and images.startswith('http'):
        return re.sub(r'\b(50|150|250)x(50|150|250)\b', '500x500', images)
    return ''


def _pick_low_quality(urls):
    if not urls: return None, None
    for preferred in ['96kbps', '96', '128kbps', '128', '48kbps', '48']:
        for item in urls:
            q = (item.get('quality') or '').lower().strip()
            if q == preferred or preferred in q:
                url = item.get('url') or item.get('link') or ''
                if url.startswith('http'): return url, item.get('quality', preferred)
    QUALITY_RANK = {
        '320kbps': 7, '320': 7, '160kbps': 5, '160': 5,
        '96kbps': 3, '96': 3, '48kbps': 2, '48': 2, '12kbps': 1, '12': 1,
    }
    def rank(item):
        q = (item.get('quality') or '').lower().strip()
        if q in QUALITY_RANK: return QUALITY_RANK[q]
        m = re.search(r'(\d+)', q)
        return int(m.group(1)) if m else 999
    for item in sorted(urls, key=rank):
        url = item.get('url') or item.get('link') or ''
        if url.startswith('http'): return url, item.get('quality', 'low')
    return None, None


def _safe_year(date_str):
    try: return int((date_str or '')[:4])
    except (ValueError, TypeError): return 0


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
