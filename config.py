"""Configuration for Paattu Petti — everything comes from environment variables.

Required:
    BOT_TOKEN       Bot token from BotFather (bot owned by the main account)
    API_ID          API ID from my.telegram.org (alt / assistant account)
    API_HASH        API Hash from my.telegram.org (alt / assistant account)
    SESSION_STRING  Session string for the assistant account (gen_session.py)

Optional:
    DB_PATH         SQLite file path (default: paattu_petti.db)
    DOWNLOAD_DIR    Where replied-to audio files are saved (default: downloads)
"""

import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
SESSION_STRING = os.environ.get("SESSION_STRING", "").strip()

try:
    API_ID = int(os.environ.get("API_ID", "0") or 0)
except ValueError:
    API_ID = 0

DB_PATH = os.environ.get("DB_PATH", "paattu_petti.db")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")

# Playback limits
MAX_TRACK_SECONDS = 30 * 60          # reject tracks longer than 30 minutes
MAX_QUEUE_PER_USER = 5               # upcoming tracks per user, per group
LEAVE_GRACE_SECONDS = 30             # idle time before the assistant leaves the VC
PLAYLIST_MAX_TRACKS = 20             # cap for /play <playlist url>

REQUIRED_VARS = ("BOT_TOKEN", "API_ID", "API_HASH", "SESSION_STRING")


def missing_vars() -> list:
    """Names of required environment variables that are not set."""
    values = {
        "BOT_TOKEN": BOT_TOKEN,
        "API_ID": API_ID,
        "API_HASH": API_HASH,
        "SESSION_STRING": SESSION_STRING,
    }
    return [name for name in REQUIRED_VARS if not values.get(name)]
