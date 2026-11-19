"""Paattu Petti 🎶 — Telegram group voice-chat music bot.

Two clients in one process:
  * bot client (BotFather token) — commands, queue, Now Playing cards.
  * assistant userbot client (session string) — joins the voice chat and
    streams audio through py-tgcalls. A normal bot cannot join a VC, which
    is why the assistant account exists.

Config comes ONLY from environment variables — see config.py / .env.example.
Run:  env -u no_proxy -u NO_PROXY python bot.py   (with the env vars set)
"""

import asyncio
import json
import logging
import os
import random
import urllib.parse
import urllib.request

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import CallbackQuery, Message
from pytgcalls import PyTgCalls
from pytgcalls.types import ChatUpdate, StreamEnded

import config
import youtube
from games import BattleManager, GuessManager, PollManager
from player import EFFECTS, PlayerManager
from storage import Storage
from utils import (find_youtube_url, format_duration, is_playlist_url,
                   parse_dedication, truncate, votes_needed)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")
log = logging.getLogger("paattu.bot")

PLAY_LINES = [
    "Pettu thurannu! 🎶", "Ganamela thudangi! 🎤", "Sound on aakikko! 🔊",
]
SKIP_LINES = [
    "Skipped! Aa song poyi 🏃", "Next! Janangalkku ishtapettilla 😅",
    "Skip adichu kalanju! ⏭",
]

HELP_TEXT = """🎶 **Paattu Petti — commands**

**Playing**
/play <song name> — search & play in the voice chat
/play <YouTube link> — play a video / queue a playlist (up to 20)
/play <song> for @user — dedicate a song
/play (reply to an audio file) — play that file
/queue — what's lined up • /remove <n> • /shuffle
/song — now playing • /skip • /pause • /resume • /stop
/loop — cycle loop off → song → queue
/voteskip — democratic skip 🗳

**Fun**
/guess — Guess the Song (points!) • /battle <song> — song vs song
/pollnext — vote for the next song
/nightcore /slowed /bassboost /chipmunk — effects • /normal — undo
/lyrics — lyrics of the current song
/autoplay on|off — keep the music going automatically
/top — top requesters, shame board & guess champions

**Admins**
/controls — toggle who can use controls (everyone / admins + requester)

Start a voice chat in the group, then /play something!"""


# ------------------------------------------------------------------ helpers

def cmd_args(message: Message) -> str:
    parts = (message.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


async def is_admin(bot: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER,
                                 ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


async def can_control(bot, store, chat_id, user_id, state) -> bool:
    mode = store.get_setting(chat_id, "controls", "everyone")
    if mode != "admins":
        return True
    if await is_admin(bot, chat_id, user_id):
        return True
    return bool(state.current and state.current.requester_id == user_id)


async def _run(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


def _fetch_lyrics(artist: str, title: str):
    url = ("https://api.lyrics.ovh/v1/"
           f"{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        return data.get("lyrics")
    except Exception:
        return None


# ------------------------------------------------------------------ handlers

def register_bot_handlers(bot, player, guess, battle, poll, store):
    state = player.state  # shortcut

    # ---------------------------------------------------------- basics

    @bot.on_message(filters.command("start") & filters.private)
    async def start_private(_, message: Message):
        await message.reply(
            "🎶 **Paattu Petti** — I play songs in group voice chats.\n"
            "Add me (and the assistant account) to your group, start a "
            "voice chat there, then send /play <song name>.")

    @bot.on_message(filters.command(["start", "help"]) & filters.group)
    async def help_cmd(_, message: Message):
        await message.reply(HELP_TEXT)

    # ---------------------------------------------------------- /play

    @bot.on_message(filters.command("play") & filters.group)
    async def play_cmd(_, message: Message):
        chat_id = message.chat.id
        user = message.from_user
        name = user.first_name or "Someone"
        reply = message.reply_to_message

        # Case 1: reply to an audio / voice file in the chat.
        media = None
        if reply is not None:
            media = reply.audio or reply.voice or (
                reply.document if reply.document and
                (reply.document.mime_type or "").startswith("audio") else None)
        if media is not None:
            ext = os.path.splitext(media.file_name or "")[1] or ".mp3"
            os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
            path = os.path.join(config.DOWNLOAD_DIR,
                                f"{chat_id}_{reply.id}{ext}")
            if not await _download_media(reply, player, path):
                await message.reply(
                    "That file download failed 😅 If this group has "
                    "**Restrict saving content** on, I can't download "
                    "files at all — a group admin can turn that off in "
                    "group settings.")
                return
            from queuestate import Track
            track = Track(
                title=(getattr(media, "title", None) or media.file_name
                       or "Audio file"),
                duration=int(getattr(media, "duration", 0) or 0),
                requester_id=user.id, requester_name=name,
                source="file", file_path=path)
            await _enqueue_and_reply(message, player, track)
            return

        # Case 2: text query / URL.
        raw = cmd_args(message)
        if not raw:
            await message.reply(
                "Usage: /play <song name> — or /play <YouTube link>, "
                "or reply to an audio file with /play 🎶")
            return
        query, dedication = parse_dedication(raw)
        url = find_youtube_url(query)
        status_msg = await message.reply("🔎 Searching…")
        try:
            if url and is_playlist_url(url):
                tracks = await _run(youtube.playlist_tracks, url,
                                    config.PLAYLIST_MAX_TRACKS, user.id, name)
                if not tracks:
                    await status_msg.edit("That playlist looks empty 😅")
                    return
                added = 0
                first_status = None
                for t in tracks:
                    s, _ = await player.enqueue(chat_id, t)
                    if first_status is None:
                        first_status = s
                    added += 1
                await status_msg.edit(
                    f"📃 Queued **{added} songs** from the playlist!"
                    + (" First one is playing 🎶" if first_status == "playing"
                       else ""))
                return
            if url:
                track = await _run(youtube.track_from_url, url, user.id,
                                   name, dedication)
            else:
                results = await _run(youtube.search_tracks, query, 1,
                                     user.id, name, dedication)
                if not results:
                    await status_msg.edit(
                        f"No results for **{truncate(query, 50)}** 😕")
                    return
                track = results[0]
        except youtube.YoutubeError as exc:
            await status_msg.edit(f"⚠️ {exc}")
            return
        await status_msg.delete()
        await _enqueue_and_reply(message, player, track)

    async def _download_media(reply, player_, path) -> bool:
        """Download a replied audio file, with retries and an assistant
        fallback. Every failure is logged with its real exception —
        a silent 'try again' hid the cause for too long."""
        def _ok():
            return os.path.exists(path) and os.path.getsize(path) > 0

        for attempt in range(2):
            try:
                await reply.download(file_name=path)
                if _ok():
                    return True
            except Exception:
                log.warning("media download attempt %s failed (bot)",
                            attempt + 1, exc_info=True)
        try:
            msg = await player_.assistant.get_messages(reply.chat.id,
                                                       reply.id)
            await player_.assistant.download_media(msg, file_name=path)
            if _ok():
                return True
        except Exception:
            log.warning("media download failed (assistant too)",
                        exc_info=True)
        return False

    async def _enqueue_and_reply(message, player_, track):
        chat_id = message.chat.id
        user = message.from_user
        st = state(chat_id)
        if track.duration and track.duration > config.MAX_TRACK_SECONDS:
            await message.reply(
                f"🚫 **{truncate(track.title, 50)}** is "
                f"{format_duration(track.duration)} — 30 minute-il kooduthal "
                "ulla songs venda, queue block aakum 😅")
            return
        if st.current is not None and \
                st.queued_by(user.id) >= config.MAX_QUEUE_PER_USER:
            await message.reply(
                f"✋ You already have {config.MAX_QUEUE_PER_USER} songs in "
                "the queue — wait for yours to play, bro 😄")
            return
        try:
            status, position = await player_.enqueue(chat_id, track)
        except youtube.YoutubeError as exc:
            await message.reply(f"⚠️ {exc}")
            return
        except Exception:
            log.exception("enqueue failed in %s", chat_id)
            await message.reply("Song play cheyyan pattiyilla 😕 try again")
            return
        if status == "playing":
            await message.reply(
                f"{random.choice(PLAY_LINES)} **{truncate(track.title, 60)}**")
        else:
            await message.reply(
                f"📃 Added at **#{position}**: {truncate(track.title, 60)} "
                f"({format_duration(track.duration)})")

    # ---------------------------------------------------------- queue management

    @bot.on_message(filters.command("queue") & filters.group)
    async def queue_cmd(_, message: Message):
        st = state(message.chat.id)
        if st.current is None and not st.queue:
            await message.reply("Queue empty aanu — /play something! 🎶")
            return
        lines = []
        if st.current is not None:
            lines.append(f"🎶 Now: **{truncate(st.current.title, 50)}** "
                         f"({format_duration(st.current.duration)}) — "
                         f"{st.current.requester_name}")
        for i, t in enumerate(st.queue, 1):
            lines.append(f"{i}. {truncate(t.title, 50)} "
                         f"({format_duration(t.duration)}) — {t.requester_name}")
        await message.reply("\n".join(lines))

    @bot.on_message(filters.command("remove") & filters.group)
    async def remove_cmd(_, message: Message):
        st = state(message.chat.id)
        try:
            position = int(cmd_args(message))
        except ValueError:
            await message.reply("Usage: /remove <number from /queue>")
            return
        track = st.queue[position - 1] if 1 <= position <= len(st.queue) else None
        if track is None:
            await message.reply("That number isn't in the queue 😅")
            return
        mode = store.get_setting(message.chat.id, "controls", "everyone")
        if mode == "admins" and track.requester_id != message.from_user.id \
                and not await is_admin(bot, message.chat.id,
                                       message.from_user.id):
            await message.reply("Admins mode is on — only the requester or "
                                "an admin can remove that one.")
            return
        st.remove(position)
        await message.reply(f"🗑 Removed **{truncate(track.title, 50)}**")

    @bot.on_message(filters.command("shuffle") & filters.group)
    async def shuffle_cmd(_, message: Message):
        st = state(message.chat.id)
        if len(st.queue) < 2:
            await message.reply("Shuffle cheyyan queue-il onnum illa 😅")
            return
        st.shuffle_queue()
        await message.reply("🔀 Queue shuffled!")

    # ---------------------------------------------------------- transport

    @bot.on_message(filters.command("song") & filters.group)
    async def song_cmd(_, message: Message):
        st = state(message.chat.id)
        if st.current is None:
            await message.reply("Onnum play cheyyunilla ippol 🤫 /play adikku!")
            return
        t = st.current
        text = (f"🎶 **{t.title}**\n⏱ {format_duration(t.duration)} • "
                f"👤 {t.requester_name}")
        if t.dedication:
            text += f"\n💌 Dedicated to {t.dedication}"
        if st.queue:
            text += f"\n📃 {len(st.queue)} more in queue"
        await message.reply(text)

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_cmd(_, message: Message):
        st = state(message.chat.id)
        if st.current is None:
            await message.reply("Skip cheyyan onnum illa 😅")
            return
        if not await can_control(bot, store, message.chat.id,
                                 message.from_user.id, st):
            await message.reply("🔒 Controls are admins + requester only.")
            return
        nxt = await player.skip(message.chat.id)
        if nxt is None:
            await message.reply("⏭ Skipped — queue theerthu, VC vidunnu 👋")
        else:
            await message.reply(random.choice(SKIP_LINES))

    @bot.on_message(filters.command("pause") & filters.group)
    async def pause_cmd(_, message: Message):
        st = state(message.chat.id)
        if not await can_control(bot, store, message.chat.id,
                                 message.from_user.id, st):
            await message.reply("🔒 Controls are admins + requester only.")
            return
        await message.reply("⏸ Paused" if await player.pause(message.chat.id)
                            else "Pause cheyyan pattiyilla 😅")

    @bot.on_message(filters.command("resume") & filters.group)
    async def resume_cmd(_, message: Message):
        st = state(message.chat.id)
        if not await can_control(bot, store, message.chat.id,
                                 message.from_user.id, st):
            await message.reply("🔒 Controls are admins + requester only.")
            return
        await message.reply("▶️ Resumed" if await player.resume(message.chat.id)
                            else "Resume cheyyan pattiyilla 😅")

    @bot.on_message(filters.command("stop") & filters.group)
    async def stop_cmd(_, message: Message):
        st = state(message.chat.id)
        if not await can_control(bot, store, message.chat.id,
                                 message.from_user.id, st):
            await message.reply("🔒 Controls are admins + requester only.")
            return
        guess.cancel(message.chat.id)
        await player.stop(message.chat.id)
        await message.reply("⏹ Stopped. Paattu Petti box അടച്ചു 👋")

    @bot.on_message(filters.command("loop") & filters.group)
    async def loop_cmd(_, message: Message):
        st = state(message.chat.id)
        arg = cmd_args(message).lower()
        if arg in ("off", "track", "song", "queue"):
            st.loop_mode = "track" if arg == "song" else arg
            mode = st.loop_mode
        else:
            mode = await player.cycle_loop(message.chat.id)
        await message.reply(f"🔁 Loop mode: **{mode}**")

    @bot.on_message(filters.command("voteskip") & filters.group)
    async def voteskip_cmd(_, message: Message):
        chat_id = message.chat.id
        st = state(chat_id)
        if st.current is None:
            await message.reply("Vote cheyyan song onnum illa 😅")
            return
        if not st.add_voteskip(message.from_user.id):
            needed = votes_needed(await player.participant_count(chat_id))
            await message.reply(
                f"Nee already vote cheythu 😄 ({len(st.voteskip_voters)}/"
                f"{needed})")
            return
        needed = votes_needed(await player.participant_count(chat_id))
        have = len(st.voteskip_voters)
        if have >= needed:
            await player.skip(chat_id)
            await message.reply(
                f"🗳 Vote pass aayi ({have}/{needed}) — song skipped! "
                "Democracy wins 😄")
        else:
            await message.reply(f"🗳 Skip vote: **{have}/{needed}**")

    # ---------------------------------------------------------- effects

    async def _effect(message: Message, effect):
        st = state(message.chat.id)
        if st.current is None:
            await message.reply("Effect idan song play cheyyanam first 😅")
            return
        if not await can_control(bot, store, message.chat.id,
                                 message.from_user.id, st):
            await message.reply("🔒 Controls are admins + requester only.")
            return
        ok = await player.set_effect(message.chat.id, effect)
        if ok:
            label = effect or "normal"
            await message.reply(f"🎛 Effect: **{label}** (song restarts)")

    for cmd_name in EFFECTS:
        bot.on_message(filters.command(cmd_name) & filters.group)(
            lambda _, m, _e=cmd_name: _effect(m, _e))

    @bot.on_message(filters.command("normal") & filters.group)
    async def normal_cmd(_, message: Message):
        await _effect(message, None)

    # ---------------------------------------------------------- fun

    @bot.on_message(filters.command("guess") & filters.group)
    async def guess_cmd(_, message: Message):
        if cmd_args(message).lower() == "top":
            board = store.guess_leaderboard(message.chat.id)
            if not board:
                await message.reply("Guess scores onnum illa — /guess "
                                    "start cheyyu! 🎲")
            else:
                lines = ["🏆 **Guess the Song — champions**"] + [
                    f"{i}. {n} — {p} pts" for i, (n, p) in
                    enumerate(board, 1)]
                await message.reply("\n".join(lines))
            return
        result = await guess.start(message.chat.id)
        if result == "started":
            await message.reply(
                "🎲 **Guess the Song!** A song is playing in the VC — "
                "first to type its name in chat wins a point! "
                f"You have 60 seconds…")
        elif result == "already":
            await message.reply("A round is already running! Guess adikku 🎧")
        elif result == "busy":
            await message.reply("Oru song ippol play cheyyunnundu — athu "
                                "kazhinjaalo /stop cheythittu /guess idu 😄")
        else:
            await message.reply("Song kandupidikkaan pattiyilla 😕 try again")

    @bot.on_message(filters.command("battle") & filters.group)
    async def battle_cmd(_, message: Message):
        query = cmd_args(message)
        if not query:
            await message.reply("Usage: /battle <song name>")
            return
        user = message.from_user
        try:
            results = await _run(youtube.search_tracks, query, 1, user.id,
                                 user.first_name or "Someone")
        except youtube.YoutubeError as exc:
            await message.reply(f"⚠️ {exc}")
            return
        if not results:
            await message.reply("Challenger song kandilla 😕")
            return
        result = await battle.start(message.chat.id, results[0])
        if result == "already":
            await message.reply("Oru battle already nadakkunnundu ⚔️")
        elif result == "idle":
            await message.reply("Battle cheyyan aadyam /play cheyyanam ⚔️")

    @bot.on_message(filters.command("pollnext") & filters.group)
    async def poll_cmd(_, message: Message):
        result = await poll.start(message.chat.id)
        if result == "already":
            await message.reply("Oru poll already nadakkunnundu 🗳")
        elif result == "error":
            await message.reply("Poll options undaakaan pattiyilla 😕")

    @bot.on_message(filters.command("lyrics") & filters.group)
    async def lyrics_cmd(_, message: Message):
        st = state(message.chat.id)
        if st.current is None:
            await message.reply("Lyrics kaanan song play cheyyanam 🎶")
            return
        track = st.current
        title = track.title
        artist = track.uploader or ""
        if " - " in title:
            artist, title = title.split(" - ", 1)
        title = title.split("(")[0].split("[")[0].strip()
        lyrics = await _run(_fetch_lyrics, artist, title)
        if not lyrics:
            await message.reply("Lyrics kandilla for this one 😕")
            return
        header = f"🎤 **{track.title}**\n\n"
        body = header + lyrics.strip()
        for i in range(0, len(body), 3900):
            await message.reply(body[i:i + 3900])

    @bot.on_message(filters.command("autoplay") & filters.group)
    async def autoplay_cmd(_, message: Message):
        chat_id = message.chat.id
        arg = cmd_args(message).lower()
        if arg in ("on", "off"):
            store.set_setting(chat_id, "autoplay", arg)
            await message.reply(f"🤖 Autoplay **{arg}**")
        else:
            current = store.get_setting(chat_id, "autoplay", "off")
            await message.reply(
                f"🤖 Autoplay is **{current}** — /autoplay on|off")

    @bot.on_message(filters.command("top") & filters.group)
    async def top_cmd(_, message: Message):
        chat_id = message.chat.id
        sections = []
        req = store.top_requesters(chat_id)
        if req:
            sections.append("🎧 **Top requesters**\n" + "\n".join(
                f"{i}. {n} — {c} songs" for i, (n, c) in enumerate(req, 1)))
        skipped = store.most_skipped(chat_id)
        if skipped:
            sections.append("💩 **Most skipped (shame board)**\n" + "\n".join(
                f"{i}. {n} — {c} skips" for i, (n, c) in enumerate(skipped, 1)))
        board = store.guess_leaderboard(chat_id)
        if board:
            sections.append("🏆 **Guess champions**\n" + "\n".join(
                f"{i}. {n} — {p} pts" for i, (n, p) in enumerate(board, 1)))
        await message.reply("\n\n".join(sections) if sections
                            else "Stats onnum illa yet — play something! 🎶")

    @bot.on_message(filters.command("controls") & filters.group)
    async def controls_cmd(_, message: Message):
        chat_id = message.chat.id
        if not await is_admin(bot, chat_id, message.from_user.id):
            await message.reply("Admins-inu mathram /controls change cheyyam")
            return
        arg = cmd_args(message).lower()
        if arg in ("admins", "admin"):
            store.set_setting(chat_id, "controls", "admins")
            await message.reply("🔒 Controls: **admins + requester** only")
        elif arg in ("everyone", "all"):
            store.set_setting(chat_id, "controls", "everyone")
            await message.reply("🔓 Controls: **everyone**")
        else:
            current = store.get_setting(chat_id, "controls", "everyone")
            await message.reply(
                f"Controls mode: **{current}** — /controls everyone|admins")

    # ---------------------------------------------------------- guess answers

    @bot.on_message(filters.group & filters.text
                    & ~filters.regex(r"^/"))
    async def guess_listener(_, message: Message):
        if guess.active(message.chat.id):
            await guess.check_message(message.chat.id, message.from_user,
                                      message.text)

    # ---------------------------------------------------------- callbacks

    @bot.on_callback_query()
    async def callbacks(_, cq: CallbackQuery):
        chat_id = cq.message.chat.id if cq.message else None
        if chat_id is None:
            await cq.answer()
            return
        data = cq.data or ""
        st = state(chat_id)

        if data.startswith("rate:"):
            rating = "fire" if data == "rate:fire" else "poop"
            if st.current is None:
                await cq.answer("Nothing playing")
                return
            if st.add_rating(cq.from_user.id, rating):
                store.record_rating(chat_id, st.current, cq.from_user.id,
                                    rating)
                await player._refresh_card(chat_id, st)
                await cq.answer("🔥" if rating == "fire" else "💩")
            else:
                await cq.answer("Already rated this one 😄")
            return

        if data.startswith("battle:"):
            side = "cur" if data == "battle:cur" else "chal"
            result = await battle.vote(chat_id, cq.from_user.id, side)
            await cq.answer("Vote counted 🗳" if result else "Battle over")
            return

        if data.startswith("poll:"):
            try:
                index = int(data.split(":", 1)[1])
            except ValueError:
                await cq.answer()
                return
            result = await poll.vote(chat_id, cq.from_user.id, index)
            await cq.answer("Vote counted 🗳" if result is not None
                            else "Poll over")
            return

        if data.startswith("np:"):
            action = data.split(":", 1)[1]
            if not await can_control(bot, store, chat_id, cq.from_user.id,
                                     st):
                await cq.answer("🔒 Admins + requester only")
                return
            if action == "toggle":
                if st.paused:
                    await player.resume(chat_id)
                    await cq.answer("▶️")
                else:
                    await player.pause(chat_id)
                    await cq.answer("⏸")
            elif action == "skip":
                await player.skip(chat_id)
                await cq.answer("⏭")
            elif action == "stop":
                guess.cancel(chat_id)
                await player.stop(chat_id)
                await cq.answer("⏹")
            elif action == "loop":
                mode = await player.cycle_loop(chat_id)
                await cq.answer(f"🔁 Loop: {mode}")
            elif action == "shuffle":
                st.shuffle_queue()
                await cq.answer("🔀 Shuffled")
            else:
                await cq.answer()
            return

        await cq.answer()


def register_call_handlers(call: PyTgCalls, player: PlayerManager, guess):
    @call.on_update()
    async def on_call_update(_, update):
        if isinstance(update, StreamEnded):
            await player.handle_stream_end(update.chat_id)
        elif isinstance(update, ChatUpdate) and update.status in (
                ChatUpdate.Status.CLOSED_VOICE_CHAT,
                ChatUpdate.Status.KICKED):
            guess.cancel(update.chat_id)
            await player.handle_call_closed(update.chat_id)


# ------------------------------------------------------------------ main

def _start_health_server():
    """Render free web services must answer HTTP on $PORT or they spin down.

    Serves a tiny OK page in a daemon thread. No-op when PORT is unset
    (e.g. running locally or on the VM).
    """
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Paattu Petti is live")

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)


async def _self_test():
    """Run the YouTube extraction probe off the event loop and print the
    verdict where the deploy log can't miss it."""
    loop = asyncio.get_running_loop()
    print(await loop.run_in_executor(None, youtube.self_test), flush=True)


async def main():
    missing = config.missing_vars()
    if missing:
        print("Missing environment variables:", ", ".join(missing))
        print("Set them and re-run. See .env.example / README.md.")
        return

    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)

    youtube._cookiefile()  # eager cookie check — status lands in the deploy log

    bot = Client("paattu_petti_bot", api_id=config.API_ID,
                 api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)
    assistant = Client("paattu_petti_assistant", api_id=config.API_ID,
                       api_hash=config.API_HASH,
                       session_string=config.SESSION_STRING)
    call = PyTgCalls(assistant)

    store = Storage(config.DB_PATH)
    player = PlayerManager(bot, assistant, call, store)
    guess = GuessManager(bot, player, store)
    battle = BattleManager(bot, player, store)
    poll = PollManager(bot, player, store)

    register_bot_handlers(bot, player, guess, battle, poll, store)
    register_call_handlers(call, player, guess)

    await bot.start()
    await assistant.start()
    await call.start()
    me = await assistant.get_me()
    log.info("Paattu Petti live. Assistant: %s (@%s)",
             me.first_name, me.username)
    print("🎶 Paattu Petti is live — /play something in your group!")
    _start_health_server()
    asyncio.ensure_future(_self_test())
    await idle()
    await bot.stop()
    await assistant.stop()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
