"""Pure helper functions — no Telegram / network imports, fully unit-testable."""

import difflib
import re

# ---------------------------------------------------------------- durations

def format_duration(seconds) -> str:
    """Seconds -> '3:45' or '1:02:03'. Unknown/None -> 'live / unknown'."""
    if seconds is None:
        return "?:??"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?:??"
    if seconds < 0:
        seconds = 0
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------- text

def truncate(text: str, limit: int = 60) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
# Words that appear in YouTube titles but are never part of the song name.
_JUNK_WORDS = {
    "official", "video", "music", "audio", "lyric", "lyrics", "lyrical",
    "hd", "hq", "4k", "8k", "full", "new", "latest", "song", "songs",
    "ft", "feat", "featuring", "vs", "remix", "cover", "live",
}


def normalize_title(title: str) -> str:
    """Lowercase, strip bracketed junk + filler words, collapse spaces."""
    if not title:
        return ""
    text = _BRACKETED.sub(" ", title.lower())
    text = _NON_ALNUM.sub(" ", text)
    words = [w for w in text.split() if w not in _JUNK_WORDS]
    return " ".join(words)


def titles_match(guess: str, actual: str) -> bool:
    """Fuzzy 'did they name the song?' check used by Guess the Song."""
    g = normalize_title(guess)
    a = normalize_title(actual)
    if not g or not a:
        return False
    if g == a:
        return True
    # One side fully contained in the other (e.g. guess drops a subtitle).
    if len(g) >= 3 and (g in a or a in g):
        return True
    # Token overlap: at least 70% of the guess's words appear in the title.
    g_tokens, a_tokens = set(g.split()), set(a.split())
    if g_tokens and len(g_tokens & a_tokens) / len(g_tokens) >= 0.7:
        return True
    return difflib.SequenceMatcher(None, g, a).ratio() >= 0.85


# ---------------------------------------------------------------- votes

def votes_needed(participant_count) -> int:
    """Majority of VC participants; falls back to 3 when the count is unknown."""
    if participant_count is None:
        return 3
    try:
        count = int(participant_count)
    except (TypeError, ValueError):
        return 3
    if count <= 0:
        return 3
    return count // 2 + 1


# ---------------------------------------------------------------- /play parsing

_DEDICATION_RE = re.compile(r"^(?P<query>.*?)\s+for\s+(?P<who>@[\w.]+)\s*$", re.IGNORECASE)


def parse_dedication(text: str):
    """'/play kesariya for @adhi' -> ('kesariya', '@adhi'); no match -> (text, None)."""
    text = (text or "").strip()
    match = _DEDICATION_RE.match(text)
    if match and match.group("query").strip():
        return match.group("query").strip(), match.group("who")
    return text, None


_YT_URL_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/\S+",
    re.IGNORECASE,
)


def find_youtube_url(text: str):
    """Return the first YouTube URL in the text, or None."""
    match = _YT_URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0)
    if not url.startswith("http"):
        url = "https://" + url
    return url


def is_playlist_url(url: str) -> bool:
    return "list=" in (url or "") and "watch" not in (url or "")
