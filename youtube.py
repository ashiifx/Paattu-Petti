"""yt-dlp wrappers. All functions here are BLOCKING — call them through
loop.run_in_executor() from async code.

Only metadata + direct stream URLs are extracted; nothing is downloaded
(except tracks the owner replies to in chat, which Telegram delivers itself).
"""

import logging
import os

import yt_dlp

from queuestate import Track

log = logging.getLogger(__name__)


class YoutubeError(Exception):
    pass


_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 15,
    "retries": 2,
}

_AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

_COOKIE_PATH = "/tmp/yt_cookies.txt"


def _cookiefile():
    """Write the YT_COOKIES env var (a Netscape cookies.txt export from a
    logged-in YouTube session) to disk once and return its path.

    This is the reliable cure for YouTube's datacenter-IP bot wall: with a
    real account's cookies, extraction works from hosting IPs. Use a
    throwaway Google account — cookies expire every few weeks and must be
    re-exported when songs start failing again.
    """
    raw = os.environ.get("YT_COOKIES", "").strip()
    if not raw:
        log.warning("YT_COOKIES env var is NOT set — extraction is cookieless")
        return None
    if not os.path.exists(_COOKIE_PATH):
        with open(_COOKIE_PATH, "w") as fh:
            fh.write(raw)
        # Validate the Netscape format: real cookie lines are TAB-separated
        # with 7 fields. If the text was copied through a chat app or web
        # page, the tabs get eaten and yt-dlp silently loads ZERO cookies —
        # extraction then behaves exactly like having no cookies at all.
        valid = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if len(line.split("\t")) >= 7:
                valid += 1
        log.info("YT_COOKIES present (%d chars, %d valid cookie lines)",
                 len(raw), valid)
        if valid == 0:
            log.warning("YT_COOKIES has NO valid cookie lines — the tabs "
                        "were probably stripped while copying. Re-paste "
                        "directly from the cookies.txt file.")
    return _COOKIE_PATH


def _extract(query_or_url: str, opts: dict, use_cookies: bool = False):
    merged = {**_BASE_OPTS, **opts}
    if use_cookies:
        cookiefile = _cookiefile()
        if cookiefile:
            merged.setdefault("cookiefile", cookiefile)
    try:
        with yt_dlp.YoutubeDL(merged) as ydl:
            return ydl.extract_info(query_or_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise YoutubeError(str(exc)[:300]) from exc
    except Exception as exc:  # network, parse, geo-block, ...
        raise YoutubeError(str(exc)[:300]) from exc


# YouTube flags datacenter IPs (Render, most VPS hosts) with
# "Sign in to confirm you're not a bot" — but mostly for the default web
# client. The TV / Android / embedded app clients are usually let through,
# so stream resolution rotates through them before giving up.
_CLIENT_SETS = [
    ["android_vr"],
    ["tv"],
    ["ios"],
    ["web_embedded"],
    ["web_safari"],
    ["android"],
    None,  # yt-dlp default, last resort
]


def _extract_resilient(query_or_url: str, opts: dict):
    """_extract with YouTube player-client rotation (for stream resolution).

    With account cookies attached, the plain web client is the one that
    uses the session properly, so it goes first; without cookies, the
    app clients (android_vr, tv, ...) are the best bet against the
    datacenter-IP bot wall.
    """
    if _cookiefile():
        client_sets = [["web"], ["web_safari"], ["mweb"]] + _CLIENT_SETS
    else:
        client_sets = _CLIENT_SETS
    last_error = None
    for clients in client_sets:
        attempt = dict(opts)
        if clients:
            attempt["extractor_args"] = {
                "youtube": {"player_client": clients}
            }
        try:
            info = _extract(query_or_url, attempt, use_cookies=True)
            log.info("extract ok via client=%s", clients or "default")
            return info
        except YoutubeError as exc:
            log.warning("extract failed via client=%s: %s",
                        clients or "default", str(exc)[:160])
            last_error = exc
    raise last_error or YoutubeError("Could not extract that song.")


def track_from_info(info: dict, requester_id: int, requester_name: str,
                    dedication=None) -> Track:
    """Build a Track from a yt-dlp info dict. Pure — unit-testable."""
    video_id = info.get("id")
    webpage = info.get("webpage_url") or (
        f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
    )
    return Track(
        title=info.get("title") or "Unknown title",
        duration=int(info.get("duration") or 0),
        video_id=video_id,
        uploader=info.get("uploader") or info.get("channel") or "",
        webpage_url=webpage,
        thumbnail=info.get("thumbnail") or "",
        requester_id=requester_id,
        requester_name=requester_name,
        dedication=dedication,
        source="youtube",
    )


def search_tracks(query: str, limit: int = 1, requester_id: int = 0,
                  requester_name: str = "", dedication=None):
    """YouTube search -> list[Track] (best match first)."""
    info = _extract(f"ytsearch{limit}:{query}", {"noplaylist": True})
    entries = info.get("entries") or []
    return [
        track_from_info(e, requester_id, requester_name, dedication)
        for e in entries if e
    ]


def track_from_url(url: str, requester_id: int, requester_name: str,
                   dedication=None) -> Track:
    info = _extract_resilient(url, {})
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise YoutubeError("That playlist looks empty.")
        info = entries[0]
    return track_from_info(info, requester_id, requester_name, dedication)


def playlist_tracks(url: str, limit: int, requester_id: int,
                    requester_name: str):
    """Flat playlist extraction -> up to `limit` Tracks (URLs resolved later)."""
    info = _extract(url, {"noplaylist": False, "extract_flat": "in_playlist",
                          "playlistend": limit})
    tracks = []
    for entry in (info.get("entries") or []):
        if not entry:
            continue
        entry = dict(entry)
        if not entry.get("webpage_url") and entry.get("id"):
            entry["webpage_url"] = f"https://www.youtube.com/watch?v={entry['id']}"
        if not entry.get("thumbnail"):
            thumbs = entry.get("thumbnails") or []
            if thumbs:
                entry["thumbnail"] = thumbs[-1].get("url", "")
        tracks.append(track_from_info(entry, requester_id, requester_name))
        if len(tracks) >= limit:
            break
    return tracks


def _soundcloud_stream(title: str):
    """Fallback: find the song on SoundCloud (no bot wall there) and
    return a direct stream URL, or None."""
    try:
        info = _extract(f"scsearch1:{title}", {"noplaylist": True})
    except YoutubeError:
        return None
    entries = info.get("entries") or []
    if not entries:
        return None
    top = entries[0]
    try:
        full = _extract(top.get("webpage_url") or top.get("url"), {})
    except YoutubeError:
        return None
    formats = [f for f in (full.get("formats") or []) if f.get("url")]
    if not formats:
        return None
    # Prefer a plain progressive file over HLS when both exist.
    progressive = [f for f in formats
                   if "m3u8" not in (f.get("protocol") or "")
                   and "m3u8" not in f.get("url", "")]
    chosen = progressive[-1] if progressive else formats[-1]
    return chosen["url"]


def resolve_stream(track: Track) -> Track:
    """Fetch the direct audio URL for a track (mutates and returns it)."""
    target = track.webpage_url or track.video_id
    if not target:
        raise YoutubeError("Track has no YouTube reference to stream from.")
    log.info("resolving stream for %r (cookies=%s)",
             track.title, bool(_cookiefile()))
    try:
        info = _extract_resilient(target, {"format": _AUDIO_FORMAT})
    except YoutubeError:
        # YouTube is fully blocked from this host — try SoundCloud instead.
        log.warning("YouTube blocked; trying SoundCloud for %r", track.title)
        fallback_url = _soundcloud_stream(track.title)
        log.warning("SoundCloud fallback result: %s",
                    "got a stream" if fallback_url else "nothing")
        if not fallback_url:
            raise
        import time
        track.stream_url = fallback_url
        track.stream_resolved_at = time.time()
        track.source = "soundcloud"
        return track
    url = info.get("url")
    if not url:
        formats = [f for f in (info.get("formats") or []) if f.get("url")]
        if not formats:
            raise YoutubeError("No playable audio stream found for that song.")
        url = formats[-1]["url"]
    import time
    track.stream_url = url
    track.stream_resolved_at = time.time()
    if info.get("duration"):
        track.duration = int(info["duration"])
    if not track.thumbnail and info.get("thumbnail"):
        track.thumbnail = info["thumbnail"]
    return track


def related_track(track: Track, exclude_ids=()):
    """Best-effort 'play something similar next' for autoplay mode."""
    seeds = []
    if track.uploader:
        seeds.append(f"{track.uploader} songs")
    seeds.append(f"{track.title} similar songs")
    for seed in seeds:
        try:
            results = search_tracks(seed, limit=3)
        except YoutubeError:
            continue
        for candidate in results:
            if candidate.video_id and candidate.video_id != track.video_id \
                    and candidate.video_id not in exclude_ids:
                return candidate
    return None
