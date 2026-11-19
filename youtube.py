"""yt-dlp wrappers. All functions here are BLOCKING — call them through
loop.run_in_executor() from async code.

Only metadata + direct stream URLs are extracted; nothing is downloaded
(except tracks the owner replies to in chat, which Telegram delivers itself).
"""

import logging
import os
import re

import yt_dlp

import time

import config
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
    # YouTube scores IPv6 blocks (/64) as one unit and judges them far
    # more harshly — a frequent "works on my laptop, fails on the VPS"
    # cause. Laptops sit behind IPv4 NAT; force the same here.
    "force_ipv4": True,
    # Node is installed in the Docker image (for the PO-token server);
    # yt-dlp needs a JS runtime enabled to solve YouTube's player
    # challenges for the web clients. Harmless where node is absent.
    "js_runtimes": {"node": {}},
}

_AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

if config.YT_VIA_WARP:
    # All yt-dlp traffic leaves via the WARP tunnel (see run.sh).
    # Saavn/Deezer use urllib directly and stay on the host IP.
    _BASE_OPTS["proxy"] = config.WARP_PROXY

_COOKIE_PATH = "/tmp/yt_cookies.txt"


def _cookiefile():
    """Write the YT_COOKIES env var (a Netscape cookies.txt export from a
    logged-in YouTube session) to disk once and return its path.

    This is the reliable cure for YouTube's datacenter-IP bot wall: with a
    real account's cookies, extraction works from hosting IPs. Use a
    throwaway Google account — cookies expire every few weeks and must be
    re-exported when songs start failing again.
    """
    if config.YT_VIA_WARP:
        # Under the WARP tunnel, cookies are poison: the spare account
        # is flagged, and with its cookies attached YouTube serves
        # storyboard-only responses even through the tunnel. Verified
        # live: the boot probe (cookieless) got real audio while /play
        # (cookies attached) hit the storyboard wall minutes later.
        # The tunnel replaces the need for cookies entirely.
        return None
    raw = os.environ.get("YT_COOKIES", "").strip()
    if not raw:
        log.warning("YT_COOKIES env var is NOT set — extraction is cookieless")
        print("YT_COOKIES check: NOT SET", flush=True)
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
        print(f"YT_COOKIES check: {len(raw)} chars, {valid} valid cookie "
              f"lines", flush=True)
        if valid == 0:
            log.warning("YT_COOKIES has NO valid cookie lines — the tabs "
                        "were probably stripped while copying. Re-paste "
                        "directly from the cookies.txt file.")
    return _COOKIE_PATH


import threading

_YDL_INIT_LOCK = threading.Lock()


def _make_ydl(opts: dict):
    """Build a YoutubeDL instance under a lock: yt-dlp's plugin
    registration (the bgutil PO-token provider) is process-global
    and races when two threads construct instances at once
    ('PoTokenProvider already registered'). Construction is fast —
    the extraction that follows stays parallel."""
    with _YDL_INIT_LOCK:
        return yt_dlp.YoutubeDL(opts)


def _extract(query_or_url: str, opts: dict, use_cookies: bool = False):
    merged = {**_BASE_OPTS, **opts}
    if use_cookies:
        cookiefile = _cookiefile()
        if cookiefile:
            merged.setdefault("cookiefile", cookiefile)
    try:
        with _make_ydl(merged) as ydl:
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
    ["tv_downgraded"],
    ["web_creator"],
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
    errors = []
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
            # Keep a compact per-client record for the final error, so a
            # failure report shows HOW each client was refused (the bare
            # last error hides whether e.g. web+PO-token got further).
            msg = str(exc)
            if msg.startswith("[youtube]") and ": " in msg:
                msg = msg.split(": ", 1)[1]
            errors.append(f"{'+'.join(clients) if clients else 'default'}"
                          f" → {msg[:110]}")
    raise YoutubeError("Every client refused: " + " | ".join(errors))


def _raw_formats(url: str, clients, use_cookies: bool) -> str:
    """Diagnostic: extract with process=False so we see the format list
    YouTube actually returned, even when format selection would fail."""
    opts = {**_BASE_OPTS,
            "extractor_args": {"youtube": {"player_client": clients}}}
    if use_cookies:
        cookiefile = _cookiefile()
        if cookiefile:
            opts["cookiefile"] = cookiefile
    try:
        with _make_ydl(opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    except Exception as exc:
        return f"EXTRACT FAIL {str(exc)[:110]}"
    fmts = (info or {}).get("formats") or []
    desc = ", ".join(
        f"{f.get('format_id')}/{f.get('ext')}"
        f"/a:{(f.get('acodec') or 'none')[:4]}"
        f"/url:{'y' if f.get('url') else 'n'}"
        for f in fmts[:14])
    return (f"{len(fmts)} formats, "
            f"audio-only: {sum(1 for f in fmts if f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none'))}"
            f" — {desc or 'NONE'}")


def self_test() -> str:
    """Startup probe: one Saavn search (cheap, no YouTube calls — this
    host's IP is bot-walled there and extra extraction attempts only
    tighten the flag), plus a Deezer decryption probe when an ARL is
    configured."""
    try:
        import saavn
        tracks = saavn.search_tracks("Tum Hi Ho", limit=1)
        if tracks and tracks[0].stream_url:
            verdict = (f"SELFTEST OK — Saavn resolved {tracks[0].title!r} "
                       f"with a direct stream URL")
        else:
            verdict = "SELFTEST FAIL — Saavn search returned no playable track"
    except Exception as exc:
        verdict = f"SELFTEST FAIL — Saavn unreachable: {exc!r}"[:300]
    if config.DEEZER_ARL:
        import deezer
        verdict += " | " + deezer.self_test()
    if config.YT_VIA_WARP:
        verdict += " | " + _warp_probe()
    return verdict


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
        view_count=int(info.get("view_count") or 0),
    )


_STOP_WORDS = {"the", "of", "to", "in", "on", "a", "an", "by", "and",
               "or", "from", "with", "song", "songs", "full", "video",
               "audio", "lyrics", "lyrical"}
_JUNK_TAGS = ("nightcore", "sped up", "sped-up", "slowed", "reverb",
              "8d audio", "bass boosted", "chipmunk", "karaoke",
              "instrumental", "ringtone", "cover")


def _saavn_confident(query: str, track) -> bool:
    """Decide whether Saavn's top hit is really the song asked for.
    Saavn's weak spot is English queries, where its first result is
    often a cover or a nightcore/sped-up upload by someone else.
    Rules: a junk-version tag in the title that the query didn't ask
    for disqualifies; at least half the query's words must appear in
    the title or the artist field (a 2-word title + artist name, e.g.
    "believer imagine dragons", must not fail the title-only half
    rule when the artist matches); and any remaining query words
    (usually the artist's name) must show up in the artist field."""
    q = query.lower()
    title = track.title.lower()
    if any(tag in title for tag in _JUNK_TAGS if tag not in q):
        return False
    words = [w for w in re.findall(r"[a-z0-9]+", q)
             if len(w) > 1 and w not in _STOP_WORDS]
    if not words:
        return True
    title_words = set(re.findall(r"[a-z0-9]+", title))
    artist_words = set(re.findall(r"[a-z0-9]+",
                                  (track.uploader or "").lower()))
    in_title = [w for w in words if w in title_words]
    rest = [w for w in words if w not in title_words]
    covered = [w for w in words
               if w in title_words or w in artist_words]
    if len(covered) / len(words) < 0.5:
        return False
    if rest and not any(w in artist_words for w in rest):
        return False
    if not rest and (track.view_count or 0) < 500_000:
        # Title-only match — no artist words to cross-check against.
        # A famous song's original has hundreds of thousands of plays
        # on Saavn; a low-play exact-title hit is a cover riding the
        # name (e.g. "Believer" by an unknown singer at 31k plays,
        # while the Imagine Dragons original doesn't appear in
        # Saavn's results at all). Don't trust it on its own.
        return False
    return True


def _clearly_famous(candidate, reference) -> bool:
    """True when the candidate out-views the reference by 50x or more
    — the signature of 'the real original vs a cover riding its name'.
    Guards the fall-through sources so they can only override a held
    Saavn hit when they are unmistakably the bigger recording."""
    return (candidate.view_count or 0) >= \
        50 * max(reference.view_count or 0, 1)


def search_tracks(query: str, limit: int = 1, requester_id: int = 0,
                  requester_name: str = "", dedication=None):
    """Search -> list[Track] (best match first). Order: JioSaavn when
    its top hit actually matches the query (its catalog API works from
    server IPs and is strongest for Indian music), then SoundCloud
    (also server-friendly; carries much of the English catalog Saavn
    lacks), then YouTube as the last resort (bot-walled from this
    host, kept for hosts where it works).

    Saavn runs first on its own — its API answers in about a second
    and a confident Saavn hit shouldn't wait on yt-dlp. Only when
    Saavn isn't trusted do SoundCloud and YouTube get searched, and
    those two run CONCURRENTLY instead of stacking their latency.
    The trust rules that run over the results are unchanged."""
    import concurrent.futures

    def _from_saavn():
        try:
            import saavn
            return saavn.search_tracks(query, limit, requester_id,
                                       requester_name, dedication)
        except Exception:
            log.warning("Saavn search failed; trying other sources",
                        exc_info=True)
            return []

    def _from_sc():
        try:
            return _soundcloud_search_tracks(query, limit, requester_id,
                                             requester_name, dedication)
        except YoutubeError:
            return []

    def _from_yt():
        try:
            return _youtube_search_tracks(query, limit, requester_id,
                                          requester_name, dedication)
        except YoutubeError:
            return []

    saavn_tracks = _from_saavn()
    if saavn_tracks and _saavn_confident(query, saavn_tracks[0]):
        return saavn_tracks
    if config.DEEZER_ARL:
        # Deezer: real originals incl. the English catalog Saavn lacks,
        # and guaranteed playable (no DRM trap) once an ARL is set.
        try:
            import deezer
            dz_tracks = deezer.search_tracks(query, limit, requester_id,
                                             requester_name, dedication)
            if dz_tracks:
                return dz_tracks
        except Exception:
            log.warning("Deezer search failed; trying other sources",
                        exc_info=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_sc = pool.submit(_from_sc)
        f_yt = pool.submit(_from_yt)
        sc_tracks = f_sc.result()
        yt_tracks = f_yt.result()
    if sc_tracks:
        winner = sc_tracks[0]
        if 0 < (winner.duration or 0) <= 95 and yt_tracks and \
                (yt_tracks[0].duration or 0) > winner.duration + 60:
            # SoundCloud's winner is a preview-length listing and
            # YouTube has the full song — go straight to YouTube
            # instead of paying a SoundCloud extraction at resolve
            # time to learn it's a snippet.
            if not saavn_tracks or _clearly_famous(yt_tracks[0],
                                                    saavn_tracks[0]):
                return yt_tracks
            return saavn_tracks
        if not saavn_tracks or _clearly_famous(winner,
                                                saavn_tracks[0]):
            return sc_tracks
        return saavn_tracks
    if yt_tracks:
        if not saavn_tracks or _clearly_famous(yt_tracks[0],
                                                saavn_tracks[0]):
            return yt_tracks
        return saavn_tracks
    # Last resort: Saavn's weak hit (often a cover for English queries)
    # still beats an error in a listening group.
    if saavn_tracks:
        return saavn_tracks
    return _youtube_search_tracks(query, limit, requester_id,
                                  requester_name, dedication)


_SEARCH_CACHE: dict = {}


def _search_extract(search_url: str) -> dict:
    """_extract for ytsearch/scsearch URLs, memoised for 2 minutes —
    the resolve step often re-searches what the search step just
    searched (the SoundCloud preview/DRM fallbacks), and a repeated
    group request shouldn't pay the extraction twice."""
    now = time.time()
    hit = _SEARCH_CACHE.get(search_url)
    if hit and now - hit[0] < 120:
        return hit[1]
    info = _extract(search_url, {"noplaylist": True, "extract_flat": True})
    if len(_SEARCH_CACHE) > 64:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[search_url] = (now, info)
    return info


def _soundcloud_search_tracks(query: str, limit: int, requester_id: int,
                              requester_name: str, dedication=None):
    """SoundCloud search (flat) -> list[Track]; streams resolve lazily
    in resolve_stream via _soundcloud_page_stream.

    Fetches several candidates and ranks them original-first:
    SoundCloud's raw #1 for a famous song is often a cover (the
    original is DRM-locked, covers stream freely), so taking the top
    hit blindly kept serving random singers. If even the best-ranked
    candidate is a tagged version, SoundCloud is rejected entirely
    and the caller falls through to YouTube."""
    info = _search_extract(f"scsearch{max(limit, 8)}:{query}")
    tracks = []
    for e in (info.get("entries") or []):
        if not e:
            continue
        e = dict(e)
        if not e.get("thumbnail"):
            thumbs = e.get("thumbnails") or []
            if thumbs:
                e["thumbnail"] = thumbs[-1].get("url", "")
        track = track_from_info(e, requester_id, requester_name, dedication)
        track.source = "soundcloud"
        tracks.append(track)
    tracks = _rank_originals(tracks, query)
    if tracks and _has_version_tag(tracks[0].title) and \
            not _has_version_tag(query):
        return []
    return tracks[:limit]


def _youtube_search_tracks(query: str, limit: int = 1, requester_id: int = 0,
                           requester_name: str = "", dedication=None):
    """YouTube search -> list[Track] (best match first).

    extract_flat is ESSENTIAL: without it yt-dlp fully extracts the top
    result's watch page right here — cookieless, default client — which
    hits the bot wall before resolve_stream (cookies + PO token) ever
    gets a chance. Flat entries already carry id/title/duration/uploader
    from the search API; the real extraction happens later, in
    resolve_stream.
    """
    info = _search_extract(f"ytsearch{max(limit, 5)}:{query}")
    entries = info.get("entries") or []
    tracks = []
    for e in entries:
        if not e:
            continue
        e = dict(e)
        if not e.get("thumbnail"):
            thumbs = e.get("thumbnails") or []
            if thumbs:
                e["thumbnail"] = thumbs[-1].get("url", "")
        tracks.append(track_from_info(e, requester_id, requester_name,
                                      dedication))
    return _rank_originals(tracks, query)[:limit]


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


def _soundcloud_page_stream(page_url: str):
    """Best direct stream URL from a SoundCloud track page, or None.
    Prefers a plain progressive file over HLS when both exist."""
    if not page_url:
        return None
    try:
        full = _extract(page_url, {})
    except YoutubeError:
        return None
    formats = [f for f in (full.get("formats") or []) if f.get("url")]
    if not formats:
        return None
    progressive = [f for f in formats
                   if "m3u8" not in (f.get("protocol") or "")
                   and "m3u8" not in f.get("url", "")]
    chosen = progressive[-1] if progressive else formats[-1]
    return chosen["url"]


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
    return _soundcloud_page_stream(top.get("webpage_url") or top.get("url"))


def _prune_youtube_cache(cache: str, keep: int = 15) -> None:
    try:
        files = [os.path.join(cache, f) for f in os.listdir(cache)]
        files.sort(key=os.path.getmtime)
        for old in files[:-keep]:
            os.unlink(old)
    except Exception:
        pass


def _validate_file(path: str) -> bool:
    """True when path is a real, playable audio file (>100 KB with a
    probeable duration). Local copy — youtube.py must not depend on
    another source module for something this small."""
    import subprocess
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


_DL_FORMAT = ("bestaudio[ext=m4a][abr<=192]/bestaudio[abr<=192]/"
              "bestaudio[ext=m4a]/bestaudio/best")


def _http_get(url: str, headers: dict, byte_range: str | None = None,
              timeout: int = 30):
    """One HTTP/1.1 GET over a (optionally WARP-proxied) TLS socket.

    Returns (status, headers, body). googlevideo serves media with a
    Content-Length and no chunking, which keeps this simple. Follows
    up to 3 redirects."""
    import socket
    import ssl
    import urllib.parse
    for _ in range(4):
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        use_tls = parsed.scheme != "http"
        port = parsed.port or (443 if use_tls else 80)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        if config.YT_VIA_WARP:
            import socks
            proxy = urllib.parse.urlparse(config.WARP_PROXY)
            raw = socks.create_connection(
                (host, port), proxy_type=socks.SOCKS5,
                proxy_addr=proxy.hostname, proxy_port=proxy.port or 1080,
                proxy_rdns=True, timeout=timeout)
        else:
            raw = socket.create_connection((host, port), timeout=timeout)
        try:
            if use_tls:
                sock = ssl.create_default_context().wrap_socket(
                    raw, server_hostname=host)
            else:
                sock = raw
            req = {"Host": host, "Connection": "close"}
            req.update(headers or {})
            if byte_range:
                req["Range"] = byte_range
            lines = [f"GET {path} HTTP/1.1"] + \
                [f"{k}: {v}" for k, v in req.items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    raise YoutubeError("connection closed during headers")
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            hlines = head.decode("iso-8859-1").split("\r\n")
            status = int(hlines[0].split()[1])
            resp = {}
            for line in hlines[1:]:
                if ": " in line:
                    k, v = line.split(": ", 1)
                    resp[k.lower()] = v
            if status in (301, 302, 303, 307, 308) and resp.get("location"):
                url = resp["location"]
                continue
            length = int(resp.get("content-length") or 0)
            if length:
                while len(body) < length:
                    chunk = sock.recv(262144)
                    if not chunk:
                        break
                    body += chunk
                body = body[:length]
            else:
                while True:
                    chunk = sock.recv(262144)
                    if not chunk:
                        break
                    body += chunk
            return status, resp, body
        finally:
            try:
                raw.close()
            except Exception:
                pass
    raise YoutubeError("too many redirects")


def _pick_download_format(info: dict):
    """Best download format: audio-only, prefer <=192 kbps (the voice
    chat re-encodes anyway — bigger files only slow the download),
    m4a over webm."""
    formats = [f for f in (info.get("formats") or [])
               if f.get("url") and f.get("acodec") not in (None, "none")]
    if not formats:
        if info.get("url"):
            return {"url": info["url"],
                    "http_headers": info.get("http_headers") or {},
                    "ext": info.get("ext") or "m4a"}
        return None

    def key(f):
        abr = f.get("abr") or 0
        over = 1 if abr > 192 else 0
        has_video = 0 if f.get("vcodec") in (None, "none") else 1
        return (has_video, over, -abr if not over else abr,
                0 if f.get("ext") == "m4a" else 1)

    return sorted(formats, key=key)[0]


def _parallel_download(fmt: dict, dest: str) -> bool:
    """Download fmt's URL to dest in parallel byte ranges through the
    tunnel. Falls back to a single full download when the server
    doesn't honour ranges. True on success."""
    import concurrent.futures
    url = fmt["url"]
    headers = fmt.get("http_headers") or {}
    status, resp, _ = _http_get(url, headers, "bytes=0-0")
    total = None
    if status == 206:
        crange = resp.get("content-range", "")
        if "/" in crange:
            try:
                total = int(crange.rsplit("/", 1)[1])
            except ValueError:
                total = None
    if not total:
        status, resp, data = _http_get(url, headers, None, timeout=60)
        if status != 200 or len(data) < 100_000:
            return False
        with open(dest, "wb") as fh:
            fh.write(data)
        return True
    nparts = 6 if total > 3_000_000 else 3
    step = (total + nparts - 1) // nparts
    bounds = [(s, min(s + step, total) - 1)
              for s in range(0, total, step)]

    def fetch(index: int, start: int, end: int) -> str:
        part = f"{dest}.part{index}"
        for _ in range(2):
            st, _, data = _http_get(url, headers,
                                    f"bytes={start}-{end}", timeout=60)
            if st == 206 and len(data) == end - start + 1:
                with open(part, "wb") as fh:
                    fh.write(data)
                return part
        raise YoutubeError(f"range {start}-{end} failed")

    parts = []
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(bounds)) as pool:
            futures = [pool.submit(fetch, i, s, e)
                       for i, (s, e) in enumerate(bounds)]
            parts = [f.result() for f in futures]
        with open(dest, "wb") as out:
            for part in parts:
                with open(part, "rb") as fh:
                    out.write(fh.read())
        return os.path.getsize(dest) == total
    except Exception:
        return False
    finally:
        for part in parts:
            try:
                os.unlink(part)
            except OSError:
                pass


def _download_youtube_file(track: Track) -> None:
    """Download a YouTube track's audio through the WARP tunnel to the
    local cache and point track.file_path at it. Rotates client
    identities like extraction does. Raises YoutubeError if every
    client fails or produces an invalid file."""
    import glob
    cache = os.path.join(config.DOWNLOAD_DIR, "youtube")
    os.makedirs(cache, exist_ok=True)
    for existing in glob.glob(os.path.join(cache, track.video_id + ".*")):
        if _validate_file(existing):
            track.file_path = existing
            track.stream_resolved_at = time.time()
            return
    url = track.webpage_url or \
        f"https://www.youtube.com/watch?v={track.video_id}"
    # Fast path: one extraction + a parallel ranged download through
    # the tunnel — several times quicker than yt-dlp's single stream.
    try:
        info = _extract_resilient(url, {"format": _DL_FORMAT})
        fmt = _pick_download_format(info)
        if fmt:
            dest = os.path.join(
                cache, f"{track.video_id}.{fmt.get('ext') or 'm4a'}")
            if _parallel_download(fmt, dest) and _validate_file(dest):
                track.file_path = dest
                track.stream_resolved_at = time.time()
                if info.get("duration"):
                    track.duration = int(info["duration"])
                log.info("youtube fast download ok: %s", track.title)
                _prune_youtube_cache(cache)
                return
    except Exception as exc:
        log.info("fast download failed for %r (%s); yt-dlp fallback",
                 track.title, str(exc)[:120])
    errors = []
    if _cookiefile():
        client_sets = [["web"], ["web_safari"], ["mweb"]] + _CLIENT_SETS
    else:
        client_sets = _CLIENT_SETS
    for clients in client_sets:
        label = "+".join(clients) if clients else "default"
        opts = {**_BASE_OPTS,
                "format": _DL_FORMAT,
                "outtmpl": os.path.join(cache, track.video_id + ".%(ext)s"),
                "noplaylist": True,
                "overwrites": True}
        if clients:
            opts["extractor_args"] = {
                "youtube": {"player_client": clients}}
        cookiefile = _cookiefile()
        if cookiefile:
            opts["cookiefile"] = cookiefile
        try:
            with _make_ydl(opts) as ydl:
                ydl.download([url])
        except Exception as exc:
            errors.append(f"{label}: {str(exc)[:90]}")
            continue
        for path in glob.glob(os.path.join(cache, track.video_id + ".*")):
            if _validate_file(path):
                track.file_path = path
                track.stream_resolved_at = time.time()
                log.info("youtube download via tunnel ok (%s): %s",
                         label, track.title)
                _prune_youtube_cache(cache)
                return
        errors.append(f"{label}: no valid file produced")
    raise YoutubeError("Tunnel download failed — " + " | ".join(errors[:3]))


_VERSION_TAGS_YT = ("remix", "cover", "sped up", "sped-up", "slowed",
                    "reverb", "8d", "nightcore", "lofi", "lo-fi",
                    "instrumental", "karaoke", "tribute", "mashup",
                    "live")


def _has_version_tag(title: str) -> bool:
    """True when a title carries a version tag (cover/remix/...).
    Word-boundary matched so 'Discover' doesn't trip on 'cover'."""
    text = (title or "").lower()
    return any(re.search(r"\b" + re.escape(tag) + r"\b", text)
               for tag in _VERSION_TAGS_YT)


def _rank_originals(tracks, query: str):
    """Reorder search results so the real original wins over covers:
    version-tagged titles sink (unless the query asks for one), then
    bigger view counts win — originals out-view covers by orders of
    magnitude — and the source's own relevance order breaks ties."""
    import math
    q = (query or "").lower()
    asked_tags = [tag for tag in _VERSION_TAGS_YT
                  if re.search(r"\b" + re.escape(tag) + r"\b", q)]
    qwords = [w for w in re.findall(r"[a-z0-9]+", q)
              if len(w) > 1 and w not in _STOP_WORDS]

    def key(item):
        idx, t = item
        title = (t.title or "").lower()
        bucket = int(math.log10((t.view_count or 0) + 1))
        # Relevance before fame: a candidate missing query words
        # ("Dark Knight Dummo" for "dark red") must not outrank the
        # song actually asked for just because it's famous.
        twords = set(re.findall(r"[a-z0-9]+", title))
        missing = len([w for w in qwords if w not in twords])
        if asked_tags:
            # The query asks for a version (remix, slowed, ...):
            # titles carrying that tag go first.
            wanted = any(re.search(r"\b" + re.escape(tag) + r"\b", title)
                         for tag in asked_tags)
            return (0 if wanted else 1, missing, -bucket, idx)
        tagged = _has_version_tag(t.title)
        return (1 if tagged else 0, missing, -bucket, idx)

    return [t for _, t in sorted(enumerate(tracks), key=key)]


def _closest_to_original(candidates, reference):
    """Pick the candidate most likely to be the same recording as the
    reference track: no version tags (unless the reference has them),
    then closest duration. Stops the SoundCloud-DRM fallback from
    grabbing a 2:20 remix of a 3:53 original."""
    ref_title = (reference.title or "").lower()

    def key(t):
        title = (t.title or "").lower()
        tagged = any(tag in title for tag in _VERSION_TAGS_YT) and \
            not any(tag in ref_title for tag in _VERSION_TAGS_YT)
        gap = abs((t.duration or 0) - (reference.duration or 0)) \
            if reference.duration else 0
        return (1 if tagged else 0, gap)

    return sorted(candidates, key=key)[0]


def _warp_probe() -> str:
    """Boot-log verdict for the WARP route: one proxied extraction."""
    try:
        info = _extract("https://www.youtube.com/watch?v=rfTgO9rpqck",
                        {"format": _AUDIO_FORMAT})
        formats = info.get("formats") or []
        audio = [f for f in formats
                 if f.get("acodec") not in (None, "none")]
        if audio or info.get("url"):
            return (f"WARP OK — YouTube served playable audio via the "
                    f"tunnel ({info.get('title')!r})")
        return ("WARP FAIL — tunnel answered but YouTube still served "
                "no audio formats (storyboard wall)")
    except Exception as exc:
        return f"WARP FAIL — {str(exc)[:170]}"


def _youtube_fallback_for(track: Track, longer_than: int = 0) -> Track:
    """Resolve a SoundCloud track's song via YouTube — used when
    SoundCloud DRM-locks it or serves only a short preview. Searches
    YouTube, picks the candidate closest to the original, resolves
    it, and copies the playable result onto `track` in place. With
    longer_than > 0, raises YoutubeError unless YouTube's best
    candidate runs at least 60s longer — the caller then keeps its
    SoundCloud stream. Raises YoutubeError on any failure."""
    yt = _youtube_search_tracks(
        f"{track.title} {track.uploader}".strip(), 5,
        track.requester_id, track.requester_name, track.dedication)
    if longer_than:
        # The reference duration is a PREVIEW length, so "closest to
        # the original" is meaningless — it picked a 3:35 edit of a
        # 4:25 song for being nearer to 90s. Rank by originality and
        # views instead: the full album version wins.
        pick = _rank_originals(
            yt, f"{track.title} {track.uploader}")[0]
        if (pick.duration or 0) <= longer_than + 60:
            raise YoutubeError(
                "YouTube has no substantially longer version")
    else:
        pick = _closest_to_original(yt, track)
    resolved = resolve_stream(pick)
    track.stream_url = resolved.stream_url
    # The tunnel path DOWNLOADS instead of returning a URL — without
    # copying file_path the player gets neither and streams the
    # SoundCloud page itself (silence, then the watchdog skip).
    track.file_path = resolved.file_path
    track.webpage_url = resolved.webpage_url
    track.video_id = resolved.video_id
    if resolved.duration:
        track.duration = resolved.duration
    track.stream_resolved_at = resolved.stream_resolved_at
    track.source = "youtube"
    return track


def resolve_stream(track: Track) -> Track:
    """Fetch the direct audio URL for a track (mutates and returns it)."""
    if track.source == "deezer":
        # Downloads + decrypts to a local file; sets track.file_path.
        import deezer
        try:
            deezer.resolve_stream(track)
        except deezer.DeezerError as exc:
            raise YoutubeError(str(exc)) from exc
        import time
        track.stream_resolved_at = time.time()
        return track
    if track.source == "saavn":
        # Saavn tracks carry their direct CDN URL from search time; the
        # links are static, so just refresh the freshness stamp.
        import time
        track.stream_resolved_at = time.time()
        return track
    if track.source == "soundcloud":
        import time
        try:
            info = _extract(track.webpage_url, {})
        except YoutubeError as exc:
            if "DRM" in str(exc):
                # Last resort before giving up: the same song on
                # YouTube.
                try:
                    return _youtube_fallback_for(track)
                except Exception:
                    pass
                yt_note = ("the YouTube route through the tunnel also "
                           "failed just now" if config.YT_VIA_WARP else
                           "YouTube is blocked from this server")
                raise YoutubeError(
                    f"🔒 **{track.title}** — the original is DRM-locked "
                    f"on SoundCloud (only its own app may play it), and "
                    f"{yt_note}. I can't play the original of this one "
                    "right now.")
            raise YoutubeError(
                "Could not get a playable stream from SoundCloud.")
        formats = [f for f in (info.get("formats") or []) if f.get("url")]
        if not formats:
            raise YoutubeError(
                "Could not get a playable stream from SoundCloud.")
        sc_duration = int(info.get("duration") or 0)
        if 0 < sc_duration <= 95:
            # SoundCloud serves major-label tracks as short previews
            # (30s/90s) through the API — playable, but only a snippet
            # of the song. If YouTube's version is substantially
            # longer, this is a preview: play YouTube's instead.
            try:
                return _youtube_fallback_for(track,
                                             longer_than=sc_duration)
            except Exception:
                pass
        progressive = [f for f in formats
                       if "m3u8" not in (f.get("protocol") or "")
                       and "m3u8" not in f.get("url", "")]
        chosen = progressive[-1] if progressive else formats[-1]
        # Download the track with yt-dlp and stream the local file.
        # SoundCloud now serves most tracks as HLS: the format URL is
        # a playlist whose segment URLs are minted for the extracting
        # IP (the tunnel's), so the player fetching them directly
        # gets silence — and a playlist is not a file you can save
        # with one GET either. yt-dlp downloads HLS properly through
        # the same proxy route.
        import glob
        try:
            cache = os.path.join(config.DOWNLOAD_DIR, "soundcloud")
            os.makedirs(cache, exist_ok=True)
            stem = os.path.join(cache, track.video_id or "sc")
            existing = [p for p in glob.glob(stem + ".*")
                        if _validate_file(p)]
            if existing:
                track.file_path = existing[0]
            else:
                dl_opts = {**_BASE_OPTS, "format": "bestaudio/best",
                           "outtmpl": stem + ".%(ext)s",
                           "noplaylist": True, "overwrites": True}
                with _make_ydl(dl_opts) as ydl:
                    ydl.download([track.webpage_url])
                got = [p for p in glob.glob(stem + ".*")
                       if _validate_file(p)]
                if not got:
                    raise YoutubeError("yt-dlp produced no valid file")
                track.file_path = got[0]
            track.stream_resolved_at = time.time()
            _prune_youtube_cache(cache)
            return track
        except Exception as exc:
            log.warning("SoundCloud download failed for %r (%s); "
                        "using the direct stream URL", track.title,
                        str(exc)[:100])
            track.stream_url = chosen["url"]
            track.stream_resolved_at = time.time()
            return track
    if config.YT_VIA_WARP and track.video_id:
        # The stream URL would be bound to the WARP egress IP and the
        # player could not fetch it from here — download the file
        # through the tunnel instead and stream it locally.
        try:
            _download_youtube_file(track)
            return track
        except Exception as exc:
            log.warning("WARP download failed for %r: %s — trying "
                        "direct extraction", track.title, exc)
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
