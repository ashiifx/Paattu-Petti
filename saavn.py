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
import os
import time
import urllib.parse
import urllib.request

from queuestate import Track

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
