"""JioSaavn source — search via the open-source jiosaavn-api.

Why: YouTube hard-blocks this host's IP (with cookies + PO tokens the
player response still contains only storyboard "formats"). JioSaavn's
catalog API serves direct stream URLs and does not bot-wall servers,
and its catalog is strongest exactly where the group listens most —
Indian film music. YouTube remains the fallback in youtube.py.

The API base is an env var so a self-hosted instance can replace the
public one later without a code change:
    SAAVN_API_BASE (default https://saavn.sumit.co)

All functions here are BLOCKING (urllib) — call via run_in_executor.
"""

import html
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

import config
from queuestate import Track

log = logging.getLogger("paattu.saavn")

BASE = os.environ.get("SAAVN_API_BASE", "https://saavn.sumit.co")
_TIMEOUT = 15


def _get_json(path: str) -> dict:
    req = urllib.request.Request(
        BASE + path, headers={"User-Agent": "PaattuPetti/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _best_url(entries, key="url"):
    """Last entry of an ascending quality list = the best one."""
    entries = [e for e in (entries or []) if e.get(key)]
    return entries[-1][key] if entries else ""


_RANK_STOP = {"the", "of", "a", "an", "song", "songs", "full", "audio",
              "video", "lyrics", "lyrical", "from"}
_VERSION_TAGS = ("reprise", "unplugged", "remix", "cover", "lofi",
                 "lo-fi", "sad version", "instrumental", "karaoke",
                 "revisited", "acoustic", "live version", "8d",
                 "nightcore", "sped up", "slowed", "reverb", "mashup")


def _rank(results, query: str):
    """Reorder Saavn results so the ORIGINAL recording wins. Saavn's
    own relevance order often puts a reprise/cover/other-singer version
    of a famous song first; the original almost always has orders of
    magnitude more plays. Ranking: title must match the query and not
    carry a version tag the user didn't ask for, then most plays."""
    q = query.lower()
    qwords = [w for w in re.findall(r"[a-z0-9]+", q)
              if len(w) > 1 and w not in _RANK_STOP]

    def key(r):
        title = (r.get("name") or "").lower()
        twords = set(re.findall(r"[a-z0-9]+", title))
        overlap = (len([w for w in qwords if w in twords]) / len(qwords)
                   if qwords else 1.0)
        versioned = any(tag in title for tag in _VERSION_TAGS
                        if tag not in q)
        try:
            plays = int(r.get("playCount") or 0)
        except (TypeError, ValueError):
            plays = 0
        return (overlap >= 0.6 and not versioned, overlap, plays)

    return sorted(results, key=key, reverse=True)


def search_tracks(query: str, limit: int = 1, requester_id: int = 0,
                  requester_name: str = "", dedication=None):
    """Saavn search -> list[Track] with stream_url already resolved
    (Saavn hands out direct CDN links, so no second extraction step).
    Fetches extra candidates and ranks them so the original recording
    — not a cover or reprise — comes first."""
    data = _get_json("/api/search/songs?" + urllib.parse.urlencode(
        {"query": query, "limit": max(10, limit)}))
    results = _rank(((data.get("data") or {}).get("results")) or [], query)
    tracks = []
    for r in results:
        stream = _best_url(r.get("downloadUrl"))
        if not stream:
            continue
        artists = ", ".join(
            a.get("name", "") for a in
            ((r.get("artists") or {}).get("primary") or []) if a.get("name"))
        tracks.append(Track(
            title=html.unescape(r.get("name") or "Unknown title"),
            duration=int(r.get("duration") or 0),
            video_id=r.get("id"),
            uploader=html.unescape(artists),
            webpage_url=r.get("url") or "",
            thumbnail=_best_url(r.get("image")),
            requester_id=requester_id,
            requester_name=requester_name,
            dedication=dedication,
            source="saavn",
            stream_url=stream,
            stream_resolved_at=time.time(),
            view_count=int(r.get("playCount") or 0),
        ))
    return tracks[:max(1, limit)]


def _probe_ok(path: str):
    """True if ffprobe reads a real duration from the file, False if
    the file is junk (an error page saved as .mp4, a stub, a truncated
    download). None when ffprobe itself is unavailable — caller then
    falls back to trusting the file size."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20)
        return float((out.stdout or "").strip() or 0) > 1
    except FileNotFoundError:
        return None
    except Exception:
        return False


def download_track(track) -> bool:
    """Download the track's audio into the local cache and point
    track.file_path at it. Streaming a remote URL through the free
    tier's throttled CPU stalls mid-song; a local file doesn't.
    The download is validated with ffprobe — a dead Saavn link can
    return an error page or a stub that would otherwise 'play' as
    silence. Best-effort: on any failure the caller falls back to
    streaming the URL."""
    if not track.stream_url or not track.video_id:
        return False
    try:
        cache_dir = os.path.join(config.DOWNLOAD_DIR, "saavn")
        os.makedirs(cache_dir, exist_ok=True)
        dest = os.path.join(cache_dir, f"{track.video_id}.mp4")
        if os.path.exists(dest) and os.path.getsize(dest) > 100_000:
            ok = _probe_ok(dest)
            if ok is not False:
                track.file_path = dest
                return True
            os.remove(dest)  # cached junk — fetch it fresh
        req = urllib.request.Request(
            track.stream_url, headers={"User-Agent": "PaattuPetti/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(dest + ".part", "wb") as fh:
            shutil.copyfileobj(resp, fh, 256 * 1024)
        os.replace(dest + ".part", dest)
        if os.path.getsize(dest) <= 100_000 or _probe_ok(dest) is False:
            log.warning("Saavn download for %r is not real audio; "
                        "falling back to the stream URL", track.title)
            os.remove(dest)
            return False
        _prune_cache(cache_dir, keep=15)
        track.file_path = dest
        return True
    except Exception:
        log.warning("Saavn download failed for %r; will stream the URL",
                    track.title, exc_info=True)
        return False


def _prune_cache(cache_dir: str, keep: int = 15):
    try:
        files = [os.path.join(cache_dir, f) for f in os.listdir(cache_dir)
                 if f.endswith(".mp4")]
        files.sort(key=os.path.getmtime)
        for path in files[:-keep] if len(files) > keep else []:
            try:
                os.remove(path)
            except OSError:
                pass
    except OSError:
        pass
