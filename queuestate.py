"""Per-chat queue and playback state.

This module is deliberately free of Telegram / pytgcalls imports so the queue
logic can be unit-tested without any credentials. The streaming layer
(player.py) drives a ChatState; everything here is plain data + rules.
"""

import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

LOOP_OFF = "off"
LOOP_TRACK = "track"
LOOP_QUEUE = "queue"
LOOP_MODES = (LOOP_OFF, LOOP_TRACK, LOOP_QUEUE)


@dataclass
class Track:
    title: str
    requester_id: int
    requester_name: str
    duration: int = 0                       # seconds, 0 = unknown
    video_id: Optional[str] = None
    uploader: str = ""
    webpage_url: str = ""
    thumbnail: str = ""
    dedication: Optional[str] = None        # "@user" the song is dedicated to
    source: str = "youtube"                 # "youtube" | "file"
    file_path: Optional[str] = None         # for source == "file"
    stream_url: Optional[str] = None        # pre-fetched direct audio URL
    stream_resolved_at: float = 0.0

    def stream_fresh(self, max_age: float = 5 * 3600) -> bool:
        """YouTube direct URLs expire after ~6h; treat >5h old as stale."""
        return bool(self.stream_url) and (time.time() - self.stream_resolved_at) < max_age


@dataclass
class ChatState:
    chat_id: int
    current: Optional[Track] = None
    queue: List[Track] = field(default_factory=list)
    loop_mode: str = LOOP_OFF
    paused: bool = False
    effect: Optional[str] = None            # current ffmpeg effect name
    in_call: bool = False                   # assistant is in the voice chat
    card_message_id: Optional[int] = None   # Now Playing card (bot message)
    hidden_current: bool = False            # current track is a Guess round (no card)
    # per-current-track interaction state (reset on every track change)
    voteskip_voters: set = field(default_factory=set)
    ratings: dict = field(default_factory=dict)        # user_id -> "fire" | "poop"
    suppress_stream_end: bool = False       # set around manual stream changes

    # ------------------------------------------------------------ queue ops

    def queued_by(self, user_id: int) -> int:
        return sum(1 for t in self.queue if t.requester_id == user_id)

    def enqueue(self, track: Track) -> int:
        """Append to the queue; returns the 1-based position in the queue."""
        self.queue.append(track)
        return len(self.queue)

    def insert_front(self, track: Track) -> None:
        self.queue.insert(0, track)

    def remove(self, position: int) -> Optional[Track]:
        """Remove by 1-based queue position; returns the track or None."""
        if 1 <= position <= len(self.queue):
            return self.queue.pop(position - 1)
        return None

    def shuffle_queue(self) -> None:
        random.shuffle(self.queue)

    def clear(self) -> None:
        self.queue.clear()
        self.current = None
        self.paused = False
        self.effect = None
        self.hidden_current = False
        self._reset_track_votes()

    # ------------------------------------------------------------ playback flow

    def start_track(self, track: Track, hidden: bool = False) -> None:
        self.current = track
        self.paused = False
        self.hidden_current = hidden
        self._reset_track_votes()

    def advance(self, skipped: bool = False) -> Optional[Track]:
        """Move to the next track after the current one ends / is skipped.

        Loop rules:
          - loop=track: a natural end replays the same track; a skip ignores it.
          - loop=queue: the finished track goes to the back of the queue.
        Returns the new current track (or None when nothing is left).
        """
        finished = self.current
        if finished is not None and not skipped and self.loop_mode == LOOP_TRACK:
            self._reset_track_votes()
            return finished
        if finished is not None and self.loop_mode == LOOP_QUEUE:
            self.queue.append(finished)
        self.current = self.queue.pop(0) if self.queue else None
        self.paused = False
        self.hidden_current = False
        self._reset_track_votes()
        return self.current

    def cycle_loop(self) -> str:
        idx = LOOP_MODES.index(self.loop_mode)
        self.loop_mode = LOOP_MODES[(idx + 1) % len(LOOP_MODES)]
        return self.loop_mode

    # ------------------------------------------------------------ votes / ratings

    def _reset_track_votes(self) -> None:
        self.voteskip_voters = set()
        self.ratings = {}

    def add_voteskip(self, user_id: int) -> bool:
        """True if this is a new vote (False for a repeat vote)."""
        if user_id in self.voteskip_voters:
            return False
        self.voteskip_voters.add(user_id)
        return True

    def add_rating(self, user_id: int, rating: str) -> bool:
        """One rating per user per track. True if recorded/changed."""
        if self.ratings.get(user_id) == rating:
            return False
        self.ratings[user_id] = rating
        return True

    def rating_counts(self):
        fire = sum(1 for r in self.ratings.values() if r == "fire")
        poop = sum(1 for r in self.ratings.values() if r == "poop")
        return fire, poop
