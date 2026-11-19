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
import shutil
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


def search_tracks(query: str, limit: int = 1, requester_id: int = 0,
                  requester_name: str = "", dedication=None):
    """Saavn search -> list[Track] with stream_url already resolved
    (Saavn hands out direct CDN links, so no second extraction step)."""
    data = _get_json("/api/search/songs?" + urllib.parse.urlencode(
        {"query": query, "limit": max(1, limit)}))
    results = ((data.get("data") or {}).get("results")) or []
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
        ))
    return tracks


def download_track(track) -> bool:
    """Download the track's audio into the local cache and point
    track.file_path at it. Streaming a remote URL through the free
    tier's throttled CPU stalls mid-song; a local file doesn't.
    Best-effort: on any failure the caller falls back to the URL."""
    if not track.stream_url or not track.video_id:
        return False
    try:
        cache_dir = os.path.join(config.DOWNLOAD_DIR, "saavn")
        os.makedirs(cache_dir, exist_ok=True)
        dest = os.path.join(cache_dir, f"{track.video_id}.mp4")
        if os.path.exists(dest) and os.path.getsize(dest) > 100_000:
            track.file_path = dest
            return True
        req = urllib.request.Request(
            track.stream_url, headers={"User-Agent": "PaattuPetti/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(dest + ".part", "wb") as fh:
            shutil.copyfileobj(resp, fh, 256 * 1024)
        os.replace(dest + ".part", dest)
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
