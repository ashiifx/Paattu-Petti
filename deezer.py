"""Deezer source: public search + ARL-based full-track streaming.

Search and metadata use the public api.deezer.com — no account needed,
and the originals are there (unlike JioSaavn's English catalog).

Streaming needs a free Deezer account's `arl` cookie (DEEZER_ARL env
var). The flow (the deemix route):
  1. gw-light deezer.getUserData (arl cookie) -> api token + a license
     token for the account.
  2. gw-light song.getData -> the track's TRACK_TOKEN.
  3. media.deezer.com/v1/get_url -> a Blowfish-CBC "stripe"-encrypted
     MP3 URL (free accounts: MP3_128).
  4. Download, decrypt (every 3rd 2048-byte chunk is encrypted), and
     stream the local file — same download-first pattern as Saavn.

The arl is a LOGIN cookie: env var only, never in chat or logs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request

import config
from queuestate import Track

log = logging.getLogger("paattu.deezer")

_API = "https://api.deezer.com"
_GW = "https://www.deezer.com/ajax/gw-light.php"
_MEDIA = "https://media.deezer.com/v1/get_url"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_BF_SECRET = "g4el58wc0zvf9na1"
_CHUNK = 2048
_TEST_TRACK_ID = "139470659"  # Shape of You — Ed Sheeran

_session: dict = {"api_token": "", "license_token": "", "expires": 0.0}


class DeezerError(Exception):
    pass


def _http_json(url: str, data: bytes | None = None,
               headers: dict | None = None, timeout: int = 25):
    h = {"User-Agent": _UA}
    if headers:
        h.update(headers)
    if data is not None:
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise DeezerError(
            f"Deezer answered HTTP {exc.code} — for the login call that "
            "means the ARL is invalid/expired (or this server's IP is "
            "being blocked); grab a fresh ARL from deezer.com."
        ) from exc
    except Exception as exc:
        raise DeezerError(f"Deezer unreachable: {exc!r}") from exc


# ------------------------------------------------------------------ #
# Search / metadata (public, no account)
# ------------------------------------------------------------------ #

_VERSION_TAGS = (
    "acoustic", "instrumental", "karaoke", "live", "remix", "sped up",
    "slowed", "reverb", "8d", "cover", "tribute", "piano", "lofi",
)


def _rank_key(query: str, item: dict) -> tuple:
    title = (item.get("title") or "").lower()
    q = query.lower()
    tagged = any(t in title for t in _VERSION_TAGS) and \
        not any(t in q for t in _VERSION_TAGS)
    return (1 if tagged else 0,)


def _to_track(item: dict, requester_id: int, requester_name: str,
              dedication: str = "") -> Track:
    tid = str(item.get("id"))
    album = item.get("album") or {}
    return Track(
        title=item.get("title") or "Unknown",
        source="deezer",
        requester_id=requester_id,
        requester_name=requester_name,
        dedication=dedication,
        video_id=tid,
        webpage_url=f"https://www.deezer.com/track/{tid}",
        duration=int(item.get("duration") or 0),
        thumbnail=album.get("cover_xl") or album.get("cover_big") or "",
        uploader=(item.get("artist") or {}).get("name", ""),
    )


def search_tracks(query: str, limit: int = 5, requester_id: int = 0,
                  requester_name: str = "",
                  dedication: str = "") -> list[Track]:
    url = _API + "/search?" + urllib.parse.urlencode(
        {"q": query, "limit": 25})
    try:
        data = _http_json(url)
    except Exception as exc:
        log.warning("deezer search failed: %r", exc)
        return []
    items = data.get("data") or []
    items.sort(key=lambda it: _rank_key(query, it))
    return [_to_track(it, requester_id, requester_name, dedication)
            for it in items[:limit]]


def track_from_url(url: str, requester_id: int, requester_name: str,
                   dedication: str = "") -> Track:
    tid = url.rstrip("/").split("/")[-1].split("?")[0]
    data = _http_json(f"{_API}/track/{tid}")
    if not data.get("id"):
        raise DeezerError("That Deezer link didn't resolve to a track.")
    return _to_track(data, requester_id, requester_name, dedication)


# ------------------------------------------------------------------ #
# Authenticated streaming (ARL)
# ------------------------------------------------------------------ #

def _gw(method: str, params: dict | None = None,
        api_token: str = "") -> dict:
    qs = urllib.parse.urlencode({
        "method": method, "input": 3, "api_version": "1.0",
        "api_token": api_token,
    })
    body = json.dumps(params or {}).encode()
    data = _http_json(f"{_GW}?{qs}", data=body, headers={
        "Cookie": f"arl={config.DEEZER_ARL}",
    })
    if data.get("error"):
        raise DeezerError(f"Deezer {method} error: {data['error']}")
    return data.get("results") or {}


def _ensure_session() -> None:
    if _session["license_token"] and time.time() < _session["expires"]:
        return
    if not config.DEEZER_ARL:
        raise DeezerError("DEEZER_ARL is not set on the server.")
    results = _gw("deezer.getUserData")
    user = results.get("USER") or {}
    if not user.get("USER_ID"):
        raise DeezerError(
            "The Deezer login (ARL) is invalid or expired — grab a fresh "
            "one from deezer.com and update DEEZER_ARL.")
    options = user.get("OPTIONS") or {}
    license_token = options.get("license_token") or ""
    if not license_token:
        raise DeezerError("Deezer returned no license token for this account.")
    expiration = float(options.get("expiration") or 0)
    if expiration > 1e12:  # milliseconds
        expiration /= 1000.0
    _session.update(
        api_token=results.get("checkForm") or "",
        license_token=license_token,
        expires=(expiration - 300) if expiration else time.time() + 1800,
    )
    log.info("deezer session ready (license cached)")


def _track_token(track_id: str) -> str:
    _ensure_session()
    for method, params in (
        ("song.getData", {"sng_id": track_id}),
        ("deezer.pageTrack", {"sng_id": track_id}),
    ):
        try:
            results = _gw(method, params, api_token=_session["api_token"])
            data = results.get("DATA") or results
            token = data.get("TRACK_TOKEN")
            if token:
                log.info("deezer track token via %s", method)
                return token
        except Exception as exc:
            log.info("deezer %s failed: %r", method, exc)
    raise DeezerError("Deezer would not hand out a stream token for "
                      "this track (it may be region-locked or removed).")


def _stream_url(track_id: str) -> str:
    token = _track_token(track_id)
    payload = {
        "license_token": _session["license_token"],
        "media": [{"type": "FULL", "formats": [
            {"cipher": "BF_CBC_STRIPE", "format": "MP3_128"},
            {"cipher": "BF_CBC_STRIPE", "format": "MP3_64"},
        ]}],
        "track_tokens": [token],
    }
    data = _http_json(_MEDIA, data=json.dumps(payload).encode())
    try:
        return data["data"][0]["media"][0]["sources"][0]["url"]
    except (KeyError, IndexError, TypeError):
        raise DeezerError(
            f"Deezer returned no playable URL: {str(data)[:200]}")


# ------------------------------------------------------------------ #
# Blowfish stripe decryption
# ------------------------------------------------------------------ #

def _blowfish_key(track_id: str) -> bytes:
    md5hex = hashlib.md5(str(track_id).encode()).hexdigest()
    return bytes(
        ord(_BF_SECRET[i]) ^ ord(md5hex[i]) ^ ord(md5hex[i + 16])
        for i in range(16)
    )


def decrypt_bytes(data: bytes, track_id: str) -> bytes:
    """Undo BF_CBC_STRIPE: chunks 0, 3, 6, ... (full 2048-byte chunks
    only) are Blowfish-CBC encrypted with IV 00..07."""
    from Crypto.Cipher import Blowfish
    cipher = Blowfish.new(_blowfish_key(track_id), Blowfish.MODE_CBC,
                          iv=bytes(range(8)))
    out = bytearray()
    for start in range(0, len(data), _CHUNK):
        chunk = data[start:start + _CHUNK]
        index = start // _CHUNK
        if index % 3 == 0 and len(chunk) == _CHUNK:
            out += cipher.decrypt(chunk)
        else:
            out += chunk
    return bytes(out)


def _validate_file(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) < 100_000:
        return False
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15)
        return float(out.stdout.strip() or 0) > 5
    except Exception:
        return False


def _cache_dir() -> str:
    path = os.path.join(config.DOWNLOAD_DIR, "deezer")
    os.makedirs(path, exist_ok=True)
    return path


def _prune_cache(keep: int = 15) -> None:
    try:
        files = [os.path.join(_cache_dir(), f)
                 for f in os.listdir(_cache_dir()) if f.endswith(".mp3")]
        files.sort(key=os.path.getmtime)
        for old in files[:-keep]:
            os.unlink(old)
    except Exception:
        pass


def resolve_stream(track: Track) -> str:
    """Download + decrypt the track to a local MP3 and point the track
    at it. Returns the file path."""
    cache = _cache_dir()
    final = os.path.join(cache, f"{track.video_id}.mp3")
    if _validate_file(final):
        track.file_path = final
        return final
    url = _stream_url(track.video_id)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        encrypted = resp.read()
    plain = decrypt_bytes(encrypted, track.video_id)
    tmp = final + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(plain)
    os.replace(tmp, final)
    if not _validate_file(final):
        try:
            os.unlink(final)
        except OSError:
            pass
        raise DeezerError(
            "Deezer's file didn't decode into playable audio "
            "(the decryption key may have rotated).")
    track.file_path = final
    _prune_cache()
    log.info("deezer ready: %s (%d KB)", track.title,
             os.path.getsize(final) // 1024)
    return final


def self_test() -> str:
    """One-line verdict for the boot log."""
    if not config.DEEZER_ARL:
        return "Deezer: ARL not set (source disabled)"
    try:
        track = Track(title="selftest", source="deezer",
                      requester_id=0, requester_name="selftest",
                      video_id=_TEST_TRACK_ID)
        path = resolve_stream(track)
        return f"Deezer OK — test track decrypted ({path})"
    except Exception as exc:
        return f"Deezer FAILED — {exc}"
