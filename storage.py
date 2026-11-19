"""SQLite storage: play/skip stats, ratings, Guess-the-Song scores, settings.

One connection guarded by a lock; safe to call from async handlers because
every operation is a couple of tiny indexed queries.
"""

import sqlite3
import threading
import time


class Storage:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    video_id TEXT,
                    title TEXT,
                    requester_id INTEGER,
                    requester_name TEXT,
                    action TEXT NOT NULL,          -- play | skip | finish
                    ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ratings (
                    chat_id INTEGER NOT NULL,
                    video_id TEXT NOT NULL,
                    title TEXT,
                    user_id INTEGER NOT NULL,
                    rating TEXT NOT NULL,          -- fire | poop
                    ts REAL NOT NULL,
                    PRIMARY KEY (chat_id, video_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS guess_scores (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT,
                    points INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    PRIMARY KEY (chat_id, key)
                );
                """
            )

    # ------------------------------------------------------------ actions / stats

    def record_action(self, chat_id, track, action: str) -> None:
        if track is None:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO actions (chat_id, video_id, title, requester_id,"
                " requester_name, action, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, track.video_id, track.title, track.requester_id,
                 track.requester_name, action, time.time()),
            )

    def top_requesters(self, chat_id: int, limit: int = 5):
        with self._lock:
            rows = self._conn.execute(
                "SELECT requester_name, COUNT(*) AS plays FROM actions"
                " WHERE chat_id = ? AND action = 'play' AND requester_id IS NOT NULL"
                " GROUP BY requester_id ORDER BY plays DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [(r["requester_name"], r["plays"]) for r in rows]

    def most_skipped(self, chat_id: int, limit: int = 5):
        """Requesters whose songs get skipped the most (the shame board)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT requester_name, COUNT(*) AS skips FROM actions"
                " WHERE chat_id = ? AND action = 'skip' AND requester_id IS NOT NULL"
                " GROUP BY requester_id ORDER BY skips DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [(r["requester_name"], r["skips"]) for r in rows]

    def recent_tracks(self, chat_id: int, limit: int = 10):
        """Distinct recently played (title, uploader-ish) for poll suggestions."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT title FROM actions WHERE chat_id = ? AND action = 'play'"
                " ORDER BY ts DESC LIMIT ?",
                (chat_id, limit * 3),
            ).fetchall()
        seen, titles = set(), []
        for r in rows:
            if r["title"] and r["title"] not in seen:
                seen.add(r["title"])
                titles.append(r["title"])
            if len(titles) >= limit:
                break
        return titles

    # ------------------------------------------------------------ ratings

    def record_rating(self, chat_id, track, user_id: int, rating: str) -> None:
        if track is None or not track.video_id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO ratings (chat_id, video_id, title,"
                " user_id, rating, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, track.video_id, track.title, user_id, rating, time.time()),
            )

    # ------------------------------------------------------------ guess game

    def add_guess_point(self, chat_id: int, user_id: int, user_name: str) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO guess_scores (chat_id, user_id, user_name, points)"
                " VALUES (?, ?, ?, 1)"
                " ON CONFLICT(chat_id, user_id) DO UPDATE SET"
                " points = points + 1, user_name = excluded.user_name",
                (chat_id, user_id, user_name),
            )
            row = self._conn.execute(
                "SELECT points FROM guess_scores WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
        return row["points"] if row else 1

    def guess_leaderboard(self, chat_id: int, limit: int = 5):
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_name, points FROM guess_scores WHERE chat_id = ?"
                " ORDER BY points DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [(r["user_name"], r["points"]) for r in rows]

    # ------------------------------------------------------------ settings

    def get_setting(self, chat_id: int, key: str, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE chat_id = ? AND key = ?",
                (chat_id, key),
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, chat_id: int, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (chat_id, key, value)"
                " VALUES (?, ?, ?)",
                (chat_id, key, value),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
