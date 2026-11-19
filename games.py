"""Fun layer: Guess the Song, Song Battle, Next-Song Poll.

Each game keeps its per-chat round in a small dict here; playback itself is
always delegated to the PlayerManager. Rounds auto-resolve with asyncio
timer tasks (30s votes, 60s guess rounds).
"""

import asyncio
import logging
import random

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import youtube
from queuestate import Track
from utils import titles_match, truncate

log = logging.getLogger("paattu.games")

POPULAR_SEEDS = [
    "Arijit Singh top hits", "Anirudh Ravichander hits", "K.S. Chithra evergreen hits",
    "Malayalam melody hits", "Vidyasagar Malayalam hits", "Sid Sriram songs",
    "Shreya Ghoshal hits", "Ilaiyaraaja evergreen songs", "Hesham Abdul Wahab songs",
    "Ed Sheeran hits", "A.R. Rahman top songs", "Mohit Chauhan hits",
    "Karthik singer hits", "Yuvan Shankar Raja hits", "Bruno Mars hits",
]

GUESS_ROUND_SECONDS = 60
VOTE_SECONDS = 30


async def _search(query, limit=1):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, youtube.search_tracks, query, limit)


class GuessManager:
    def __init__(self, bot, player, store):
        self.bot = bot
        self.player = player
        self.store = store
        self.rounds = {}   # chat_id -> {"track", "timeout_task", "hint_task"}

    def active(self, chat_id: int) -> bool:
        return chat_id in self.rounds

    async def start(self, chat_id: int):
        if chat_id in self.rounds:
            return "already"
        state = self.player.state(chat_id)
        if state.current is not None:
            return "busy"
        seed = random.choice(POPULAR_SEEDS)
        try:
            results = await _search(seed, limit=3)
        except youtube.YoutubeError:
            return "error"
        if not results:
            return "error"
        track = random.choice(results)
        track.requester_name = "🎲 Guess the Song"
        self.rounds[chat_id] = {"track": track}
        await self.player.enqueue(chat_id, track, hidden=True)
        self.rounds[chat_id]["timeout_task"] = asyncio.create_task(
            self._timeout(chat_id))
        self.rounds[chat_id]["hint_task"] = asyncio.create_task(
            self._hint(chat_id))
        return "started"

    async def _hint(self, chat_id: int):
        try:
            await asyncio.sleep(GUESS_ROUND_SECONDS // 2)
            round_ = self.rounds.get(chat_id)
            if round_:
                artist = round_["track"].uploader or "?"
                await self.bot.send_message(
                    chat_id, f"💡 Hint: the artist is **{artist}**")
        except asyncio.CancelledError:
            pass

    async def _timeout(self, chat_id: int):
        try:
            await asyncio.sleep(GUESS_ROUND_SECONDS)
            round_ = self.rounds.pop(chat_id, None)
            if round_:
                await self.bot.send_message(
                    chat_id,
                    f"⏰ Time up! The song was **{round_['track'].title}**. "
                    "Aarkum kittiyilla this time 😅")
        except asyncio.CancelledError:
            pass

    async def check_message(self, chat_id: int, user, text: str) -> bool:
        """Check a chat message against the active round. True if it won."""
        round_ = self.rounds.get(chat_id)
        if not round_ or not text:
            return False
        state = self.player.state(chat_id)
        if state.current is not round_["track"]:
            return False
        if not titles_match(text, round_["track"].title):
            return False
        self.rounds.pop(chat_id, None)
        for key in ("timeout_task", "hint_task"):
            task = round_.get(key)
            if task and not task.done():
                task.cancel()
        name = user.first_name or "Someone"
        points = self.store.add_guess_point(chat_id, user.id, name)
        await self.bot.send_message(
            chat_id,
            f"🎉 **{name}** got it! The song is **{round_['track'].title}** "
            f"(+1 point, total: {points})")
        return True

    def cancel(self, chat_id: int):
        round_ = self.rounds.pop(chat_id, None)
        if round_:
            for key in ("timeout_task", "hint_task"):
                task = round_.get(key)
                if task and not task.done():
                    task.cancel()


class BattleManager:
    def __init__(self, bot, player, store):
        self.bot = bot
        self.player = player
        self.store = store
        self.battles = {}  # chat_id -> {"challenger", "votes", "message_id", "task"}

    def _text(self, chat_id: int) -> str:
        battle = self.battles[chat_id]
        cur = sum(1 for v in battle["votes"].values() if v == "cur")
        chal = sum(1 for v in battle["votes"].values() if v == "chal")
        state = self.player.state(chat_id)
        current_title = state.current.title if state.current else "?"
        return (f"⚔️ **Song Battle!**\n"
                f"🎵 Playing: {current_title} — {cur} votes\n"
                f"🆚 Challenger: {battle['challenger'].title} — {chal} votes\n"
                f"Vote in {VOTE_SECONDS}s — loser gets skipped!")

    def _markup(self, chat_id: int):
        battle = self.battles[chat_id]
        cur = sum(1 for v in battle["votes"].values() if v == "cur")
        chal = sum(1 for v in battle["votes"].values() if v == "chal")
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🎵 Keep ({cur})", callback_data="battle:cur"),
            InlineKeyboardButton(f"🆚 Challenger ({chal})",
                                 callback_data="battle:chal"),
        ]])

    async def start(self, chat_id: int, challenger: Track):
        if chat_id in self.battles:
            return "already"
        state = self.player.state(chat_id)
        if state.current is None:
            return "idle"
        self.battles[chat_id] = {"challenger": challenger, "votes": {}}
        msg = await self.bot.send_message(
            chat_id, self._text(chat_id), reply_markup=self._markup(chat_id))
        self.battles[chat_id]["message_id"] = msg.id
        self.battles[chat_id]["task"] = asyncio.create_task(self._resolve(chat_id))
        return "started"

    async def vote(self, chat_id: int, user_id: int, side: str):
        battle = self.battles.get(chat_id)
        if not battle:
            return None
        battle["votes"][user_id] = side
        try:
            await self.bot.edit_message_text(
                chat_id, battle["message_id"],
                self._text(chat_id), reply_markup=self._markup(chat_id))
        except Exception:
            log.debug("battle edit failed", exc_info=True)
        cur = sum(1 for v in battle["votes"].values() if v == "cur")
        chal = sum(1 for v in battle["votes"].values() if v == "chal")
        return cur, chal

    async def _resolve(self, chat_id: int):
        try:
            await asyncio.sleep(VOTE_SECONDS)
        except asyncio.CancelledError:
            return
        battle = self.battles.pop(chat_id, None)
        if not battle:
            return
        cur = sum(1 for v in battle["votes"].values() if v == "cur")
        chal = sum(1 for v in battle["votes"].values() if v == "chal")
        challenger = battle["challenger"]
        if chal > cur:
            await self.bot.send_message(
                chat_id,
                f"⚔️ Challenger wins {chal}–{cur}! **{challenger.title}** "
                "takes the stage 🎤")
            await self.player.insert_front_and_play(chat_id, challenger)
        else:
            await self.bot.send_message(
                chat_id,
                f"⚔️ Battle over {chal}–{cur} — the current song survives! "
                "Challenger valichu kalanju 😄")


class PollManager:
    def __init__(self, bot, player, store):
        self.bot = bot
        self.player = player
        self.store = store
        self.polls = {}  # chat_id -> {"options", "votes", "message_id", "task"}

    async def start(self, chat_id: int):
        if chat_id in self.polls:
            return "already"
        state = self.player.state(chat_id)
        recent = self.store.recent_tracks(chat_id, limit=6)
        seeds = [f"{t} style songs" for t in recent[:3]]
        while len(seeds) < 3:
            seeds.append(random.choice(POPULAR_SEEDS))
        options, seen_ids = [], set()
        if state.current and state.current.video_id:
            seen_ids.add(state.current.video_id)
        for seed in seeds:
            try:
                results = await _search(seed, limit=2)
            except youtube.YoutubeError:
                continue
            for track in results:
                if track.video_id and track.video_id not in seen_ids:
                    track.requester_name = "🗳 Poll pick"
                    options.append(track)
                    seen_ids.add(track.video_id)
                    break
            if len(options) >= 3:
                break
        if len(options) < 2:
            return "error"
        self.polls[chat_id] = {"options": options, "votes": {}}
        buttons = [[InlineKeyboardButton(
            f"{i + 1}. {truncate(t.title, 40)} (0)",
            callback_data=f"poll:{i}")] for i, t in enumerate(options)]
        msg = await self.bot.send_message(
            chat_id,
            f"🗳 **Next song poll!** Vote in {VOTE_SECONDS}s — winner plays next.",
            reply_markup=InlineKeyboardMarkup(buttons))
        self.polls[chat_id]["message_id"] = msg.id
        self.polls[chat_id]["task"] = asyncio.create_task(self._resolve(chat_id))
        return "started"

    async def vote(self, chat_id: int, user_id: int, index: int):
        poll = self.polls.get(chat_id)
        if not poll or not (0 <= index < len(poll["options"])):
            return None
        poll["votes"][user_id] = index
        counts = [0] * len(poll["options"])
        for v in poll["votes"].values():
            counts[v] += 1
        buttons = [[InlineKeyboardButton(
            f"{i + 1}. {truncate(t.title, 40)} ({counts[i]})",
            callback_data=f"poll:{i}")] for i, t in enumerate(poll["options"])]
        try:
            await self.bot.edit_message_reply_markup(
                chat_id, poll["message_id"],
                reply_markup=InlineKeyboardMarkup(buttons))
        except Exception:
            log.debug("poll edit failed", exc_info=True)
        return counts

    async def _resolve(self, chat_id: int):
        try:
            await asyncio.sleep(VOTE_SECONDS)
        except asyncio.CancelledError:
            return
        poll = self.polls.pop(chat_id, None)
        if not poll:
            return
        counts = [0] * len(poll["options"])
        for v in poll["votes"].values():
            counts[v] += 1
        winner_idx = max(range(len(counts)), key=lambda i: counts[i])
        winner = poll["options"][winner_idx]
        await self.bot.send_message(
            chat_id, f"🗳 Poll closed! Next up: **{winner.title}**")
        state = self.player.state(chat_id)
        if state.current is None:
            await self.player.enqueue(chat_id, winner)
        else:
            async with self.player.lock(chat_id):
                state.insert_front(winner)
