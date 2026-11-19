"""Streaming layer: drives py-tgcalls from per-chat queue state.

The bot client posts Now Playing cards and messages; the assistant client
(via PyTgCalls) joins the voice chat and plays audio. All state transitions
that could race (skip vs. natural stream-end) are serialised per chat with
an asyncio.Lock, and manual stream changes set `suppress_stream_end` so the
StreamEnded update for the replaced stream is ignored exactly once.
"""

import asyncio
import logging
import os
import time

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig, MediaStream

import config
import saavn
import youtube
from queuestate import ChatState, Track
from utils import format_duration

log = logging.getLogger("paattu.player")

# ffmpeg -af chains, handed to MediaStream(ffmpeg_parameters=...).
EFFECTS = {
    "nightcore": "-af asetrate=48000*1.25,aresample=48000",
    "slowed": "-af atempo=0.8,aecho=0.8:0.88:60:0.3",
    "bassboost": "-af bass=g=15",
    "chipmunk": "-af asetrate=48000*1.4,aresample=48000",
}

LOOP_LABEL = {"off": "Off", "track": "Song", "queue": "Queue"}


class PlayerManager:
    def __init__(self, bot, assistant, call: PyTgCalls, store):
        self.bot = bot                # pyrogram Client (bot token)
        self.assistant = assistant    # pyrogram Client (session string)
        self.call = call              # PyTgCalls bound to the assistant
        self.store = store
        self.states = {}              # chat_id -> ChatState
        self._locks = {}              # chat_id -> asyncio.Lock
        self._leave_tasks = {}        # chat_id -> asyncio.Task
        self._prefetch_tasks = {}     # chat_id -> asyncio.Task
        self._member_attempts = {}    # chat_id -> last auto-join attempt ts

    # ------------------------------------------------------------ state helpers

    def state(self, chat_id: int) -> ChatState:
        if chat_id not in self.states:
            self.states[chat_id] = ChatState(chat_id=chat_id)
        return self.states[chat_id]

    def lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    async def _run(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    # ------------------------------------------------------------ enqueue / play

    async def enqueue(self, chat_id: int, track: Track, hidden: bool = False):
        """Queue a track; starts playback immediately when idle.

        Returns (status, position): status is 'playing' or 'queued'.
        """
        async with self.lock(chat_id):
            state = self.state(chat_id)
            self._cancel_leave(chat_id)
            if state.current is None:
                state.start_track(track, hidden=hidden)
                await self._play_current(chat_id, state)
                return "playing", 0
            position = state.enqueue(track)
            return "queued", position

    async def insert_front_and_play(self, chat_id: int, track: Track):
        """Put a track at the front and jump to it (battle wins, polls)."""
        async with self.lock(chat_id):
            state = self.state(chat_id)
            self._cancel_leave(chat_id)
            state.insert_front(track)
            if state.current is not None:
                await self._skip_locked(chat_id, state)
            else:
                state.start_track(state.queue.pop(0))
                await self._play_current(chat_id, state)

    def _media_stream(self, state: ChatState, track: Track) -> MediaStream:
        path = track.file_path or track.stream_url
        kwargs = {}
        if state.effect and EFFECTS.get(state.effect):
            kwargs["ffmpeg_parameters"] = EFFECTS[state.effect]
        return MediaStream(path, video_flags=MediaStream.Flags.IGNORE, **kwargs)

    async def _ensure_assistant_member(self, chat_id: int):
        """The assistant can only stream into groups it belongs to. If it
        isn't a member, invite it with a bot-exported link and join —
        no manual adding by the owner. Throttled to one attempt per
        chat per 2 minutes; needs the bot to be admin with invite
        rights, which is the setup the owner already runs."""
        now = time.time()
        if now - self._member_attempts.get(chat_id, 0) < 120:
            return
        self._member_attempts[chat_id] = now
        try:
            await self.assistant.get_chat_member(chat_id, "me")
            return  # already a member
        except Exception:
            pass
        try:
            link = await self.bot.export_chat_invite_link(chat_id)
        except Exception:
            log.warning("auto-join: bot cannot export an invite link for "
                        "%s (bot needs to be admin with invite rights)",
                        chat_id)
            return
        try:
            await self.assistant.join_chat(link)
            log.info("auto-join: assistant joined chat %s", chat_id)
        except Exception:
            log.warning("auto-join: assistant failed to join %s",
                        chat_id, exc_info=True)

    async def _play_current(self, chat_id: int, state: ChatState):
        """Resolve + stream state.current. Caller must hold the chat lock."""
        track = state.current
        if track is None:
            return
        await self._ensure_assistant_member(chat_id)
        if track.source in ("youtube", "soundcloud") \
                and not track.stream_fresh():
            await self._run(youtube.resolve_stream, track)
        elif track.source == "saavn" and not track.file_path:
            # Best-effort: a local file streams far more smoothly than a
            # remote URL on the free tier. Falls back to the URL.
            await self._run(saavn.download_track, track)
        try:
            await self.call.play(
                chat_id,
                self._media_stream(state, track),
                config=GroupCallConfig(auto_start=True),
            )
            state.in_call = True
        except Exception:
            log.exception("play failed in chat %s", chat_id)
            raise
        self.store.record_action(chat_id, track, "play")
        if not state.hidden_current:
            await self._send_card(chat_id, state)
        self._schedule_prefetch(chat_id, state)

    # ------------------------------------------------------------ transport

    async def skip(self, chat_id: int):
        async with self.lock(chat_id):
            state = self.state(chat_id)
            if state.current is None:
                return None
            return await self._skip_locked(chat_id, state)

    async def _skip_locked(self, chat_id: int, state: ChatState):
        finished = state.current
        self.store.record_action(chat_id, finished, "skip")
        state.suppress_stream_end = True
        nxt = state.advance(skipped=True)
        if nxt is None:
            await self._leave_locked(chat_id, state)
            return None
        await self._play_current(chat_id, state)
        return nxt

    async def pause(self, chat_id: int) -> bool:
        state = self.state(chat_id)
        if state.current is None or state.paused:
            return False
        try:
            ok = await self.call.pause(chat_id)
        except Exception:
            ok = False
        if ok:
            state.paused = True
            await self._refresh_card(chat_id, state)
        return bool(ok)

    async def resume(self, chat_id: int) -> bool:
        state = self.state(chat_id)
        if state.current is None or not state.paused:
            return False
        try:
            ok = await self.call.resume(chat_id)
        except Exception:
            ok = False
        if ok:
            state.paused = False
            await self._refresh_card(chat_id, state)
        return bool(ok)

    async def stop(self, chat_id: int):
        async with self.lock(chat_id):
            state = self.state(chat_id)
            state.suppress_stream_end = True
            await self._delete_card(chat_id, state)
            state.clear()
            await self._leave_locked(chat_id, state)

    async def set_effect(self, chat_id: int, effect):
        """Re-stream the current track through an ffmpeg effect (restarts it)."""
        async with self.lock(chat_id):
            state = self.state(chat_id)
            if state.current is None:
                return False
            state.effect = effect
            state.suppress_stream_end = True
            await self._play_current(chat_id, state)
            return True

    async def cycle_loop(self, chat_id: int) -> str:
        state = self.state(chat_id)
        mode = state.cycle_loop()
        await self._refresh_card(chat_id, state)
        return mode

    # ------------------------------------------------------------ pytgcalls events

    async def handle_stream_end(self, chat_id: int):
        async with self.lock(chat_id):
            state = self.state(chat_id)
            if state.suppress_stream_end:
                state.suppress_stream_end = False
                return
            if state.current is None:
                return
            finished = state.current
            self.store.record_action(chat_id, finished, "finish")
            nxt = state.advance(skipped=False)
            if nxt is None:
                nxt = await self._autoplay_next(chat_id, state, finished)
            # Play the next track; if its stream is broken, skip past it.
            while nxt is not None:
                try:
                    await self._play_current(chat_id, state)
                    return
                except Exception:
                    log.warning("stream failed in %s, skipping ahead",
                                chat_id, exc_info=True)
                    nxt = state.advance(skipped=True)
            await self._delete_card(chat_id, state)
            self._schedule_leave(chat_id, state)

    async def _autoplay_next(self, chat_id: int, state: ChatState, finished):
        """Autoplay mode: queue a related track when the queue runs dry."""
        if finished is None or finished.source != "youtube":
            return None
        if self.store.get_setting(chat_id, "autoplay", "off") != "on":
            return None
        try:
            related = await self._run(youtube.related_track, finished)
        except Exception:
            log.debug("autoplay lookup failed in %s", chat_id, exc_info=True)
            return None
        if related is None:
            return None
        related.requester_id = finished.requester_id
        related.requester_name = "🤖 Autoplay"
        state.start_track(related)
        try:
            await self.bot.send_message(
                chat_id,
                f"🤖 Autoplay: **{related.title}** — queue theerthu, "
                "paattu continues 🎶")
        except Exception:
            pass
        return related

    async def handle_call_closed(self, chat_id: int):
        """Voice chat ended / assistant removed — reset local state."""
        async with self.lock(chat_id):
            state = self.state(chat_id)
            self._cancel_leave(chat_id)
            await self._delete_card(chat_id, state)
            state.clear()
            state.in_call = False

    # ------------------------------------------------------------ leave / prefetch

    def _cancel_leave(self, chat_id: int):
        task = self._leave_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    def _schedule_leave(self, chat_id: int, state: ChatState):
        self._cancel_leave(chat_id)

        async def _leave_later():
            try:
                await asyncio.sleep(config.LEAVE_GRACE_SECONDS)
                async with self.lock(chat_id):
                    if state.current is None and state.in_call:
                        await self._leave_locked(chat_id, state)
            except asyncio.CancelledError:
                pass

        self._leave_tasks[chat_id] = asyncio.create_task(_leave_later())

    async def _leave_locked(self, chat_id: int, state: ChatState):
        self._cancel_leave(chat_id)
        if state.in_call:
            try:
                await self.call.leave_call(chat_id)
            except Exception:
                log.debug("leave_call failed in %s", chat_id, exc_info=True)
            state.in_call = False

    def _schedule_prefetch(self, chat_id: int, state: ChatState):
        old = self._prefetch_tasks.pop(chat_id, None)
        if old and not old.done():
            old.cancel()
        if not state.queue:
            return
        nxt = state.queue[0]
        if nxt.source not in ("youtube", "soundcloud") or nxt.stream_fresh():
            return

        async def _prefetch():
            try:
                await self._run(youtube.resolve_stream, nxt)
            except Exception:
                log.debug("prefetch failed in %s", chat_id, exc_info=True)

        self._prefetch_tasks[chat_id] = asyncio.create_task(_prefetch())

    async def participant_count(self, chat_id: int):
        try:
            participants = await self.call.get_participants(chat_id)
            return len(participants) if participants is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------ Now Playing card

    def card_caption(self, state: ChatState) -> str:
        track = state.current
        lines = [f"🎶 {track.title}"]
        meta = [f"⏱ {format_duration(track.duration)}",
                f"👤 {track.requester_name}"]
        if track.uploader:
            meta.append(f"🎤 {track.uploader}")
        lines.append(" • ".join(meta))
        if track.dedication:
            lines.append(f"💌 Dedicated to {track.dedication}")
        badges = []
        if state.paused:
            badges.append("⏸ Paused")
        if state.loop_mode != "off":
            badges.append(f"🔁 Loop: {LOOP_LABEL[state.loop_mode]}")
        if state.effect:
            badges.append(f"🎛 {state.effect}")
        if state.queue:
            badges.append(f"📃 {len(state.queue)} in queue")
        if badges:
            lines.append(" • ".join(badges))
        return "\n".join(lines)

    def card_markup(self, state: ChatState) -> InlineKeyboardMarkup:
        fire, poop = state.rating_counts()
        toggle = "▶️" if state.paused else "⏸"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle, callback_data="np:toggle"),
             InlineKeyboardButton("⏭", callback_data="np:skip"),
             InlineKeyboardButton("⏹", callback_data="np:stop")],
            [InlineKeyboardButton(f"🔁 {LOOP_LABEL[state.loop_mode]}",
                                  callback_data="np:loop"),
             InlineKeyboardButton("🔀", callback_data="np:shuffle")],
            [InlineKeyboardButton(f"🔥 {fire}" if fire else "🔥",
                                  callback_data="rate:fire"),
             InlineKeyboardButton(f"💩 {poop}" if poop else "💩",
                                  callback_data="rate:poop")],
        ])

    async def _send_card(self, chat_id: int, state: ChatState):
        await self._delete_card(chat_id, state)
        caption = self.card_caption(state)
        markup = self.card_markup(state)
        try:
            if state.current.thumbnail:
                msg = await self.bot.send_photo(
                    chat_id, state.current.thumbnail,
                    caption=caption, reply_markup=markup)
            else:
                msg = await self.bot.send_message(
                    chat_id, caption, reply_markup=markup)
            state.card_message_id = msg.id
        except Exception:
            log.debug("card send failed in %s", chat_id, exc_info=True)
            state.card_message_id = None

    async def _refresh_card(self, chat_id: int, state: ChatState):
        if state.card_message_id is None or state.current is None:
            return
        try:
            await self.bot.edit_message_caption(
                chat_id, state.card_message_id,
                caption=self.card_caption(state),
                reply_markup=self.card_markup(state))
        except Exception:
            log.debug("card refresh failed in %s", chat_id, exc_info=True)

    async def _delete_card(self, chat_id: int, state: ChatState):
        if state.card_message_id is None:
            return
        try:
            await self.bot.delete_messages(chat_id, state.card_message_id)
        except Exception:
            log.debug("card delete failed in %s", chat_id, exc_info=True)
        state.card_message_id = None

    # ------------------------------------------------------------ misc

    def cleanup_file(self, track: Track):
        """Remove a downloaded reply-audio file once it is done playing."""
        if track and track.source == "file" and track.file_path:
            try:
                os.remove(track.file_path)
            except OSError:
                pass
