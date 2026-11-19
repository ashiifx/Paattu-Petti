# Paattu Petti 🎶 — Telegram Voice-Chat Music Bot

A music bot for group chats: someone types `/play <song name>`, the bot finds
the song on YouTube and plays it **inside the group's voice chat**, with a
Now Playing card (thumbnail + pause/skip/stop/loop buttons), a queue,
vote-skip, Guess the Song, song battles, next-song polls, live audio
effects, lyrics, and per-group stats/leaderboards.

## Architecture — why there are TWO accounts

A normal Telegram **bot cannot join a voice chat** (the Bot API doesn't
allow it). Every working VC music bot is two clients in one process:

| Client | Credential | Job |
|---|---|---|
| Bot (Pyrogram, bot token) | `BOT_TOKEN` | Commands, queue, Now Playing cards, buttons |
| Assistant userbot (Pyrogram, session string) | `SESSION_STRING` | Joins the voice chat and streams audio via **py-tgcalls** |

Songs are resolved with **yt-dlp** (search + direct audio stream URL —
nothing is downloaded for YouTube tracks). Stats, ratings, game scores and
per-group settings live in **SQLite**.

```
bot.py        entrypoint: clients, command/callback handlers, main()
player.py     streaming layer: per-chat playback, cards, stream-end flow
queuestate.py pure queue/state logic (unit-tested, no Telegram imports)
youtube.py    yt-dlp wrappers (search, playlists, stream URLs, related)
storage.py    SQLite: actions, ratings, guess scores, settings
games.py      Guess the Song, Song Battle, Next-Song Poll
utils.py      pure helpers: durations, fuzzy title match, vote math
config.py     environment-variable config + limits
gen_session.py  one-time session-string generator for the assistant account
```

## Setup (owner)

1. **Bot token** — create the bot with BotFather **on your main account**
   (you own/control it) and copy the token.
2. **API ID + API Hash** — log in to <https://my.telegram.org> with the
   **assistant (alt) account** → API development tools → create an app.
   (The bot does *not* need to be created from the same account.)
3. **Session string** — on the alt account, run the generator yourself:
   ```
   pip install pyrogram
   python gen_session.py
   ```
   It asks for the API ID/Hash, the alt account's phone number, the login
   code Telegram sends, and its 2FA password if any — all locally on your
   device. Copy **only the session string** it prints. Treat it like a
   password: anyone with it controls the alt account.
4. **Install** (Python 3.10+, plus the `ffmpeg` system binary):
   ```
   pip install -r requirements.txt
   ```
   Pinned combo, verified together: `pyrogram==2.0.106`,
   `py-tgcalls==3.0.1`, `ntgcalls==3.0.7`, `yt-dlp==2026.10.26`.
5. **Group prep** — add **both** the bot and the assistant account to the
   group. Making the bot an admin is recommended (so it can manage its
   Now Playing cards). The assistant just needs to be a member.
6. **Run** — set the four env vars (see `.env.example`) and launch:
   ```
   env -u no_proxy -u NO_PROXY python bot.py
   ```
7. **Play** — start a voice chat in the group (if none is active the
   assistant starts one automatically), then `/play <song name>`.

## Commands

Playback: `/play` (name, YouTube link, playlist link ≤ 20 tracks,
`for @user` dedication, or reply to an audio file), `/queue`, `/remove <n>`,
`/shuffle`, `/song`, `/skip`, `/pause`, `/resume`, `/stop`,
`/loop` (off → song → queue), `/voteskip` (majority of VC participants).

Fun: `/guess` (+ `/guess top`), `/battle <song>`, `/pollnext`,
`/nightcore`, `/slowed`, `/bassboost`, `/chipmunk`, `/normal`, `/lyrics`,
`/autoplay on|off`, `/top` (top requesters, most-skipped shame board,
guess champions).

Admins: `/controls everyone|admins` — in *admins* mode, transport controls
and card buttons work only for group admins and the current track's
requester.

Limits: tracks longer than **30 minutes** are rejected; max **5 queued
tracks per user** per group. Ratings (🔥/💩 on the card) are one per user
per track and stored. When the queue runs dry the assistant leaves the VC
after a 30-second grace period (unless autoplay is on).

## ⚠️ Hosting caveat — read this

This bot runs as a plain process on this VM. **If the VM is replaced or
restarts, the process dies** (same thing that has stopped the other bots).
Code and the SQLite database survive on disk; the bot token and session
string **do not** — they are only ever supplied as environment variables,
never written into the code — so a relaunch means re-supplying them and
running the command above again (≈2 minutes). For a permanent home, deploy
the same folder to a host like Render with the four env vars set there.

Effects note: `/nightcore` etc. re-stream the current track through ffmpeg
filters (the song restarts with the effect applied). They use py-tgcalls'
`MediaStream(ffmpeg_parameters=…)` hook; `/normal` always restores plain
playback.

## Tests

Pure-logic unit tests (queue ops, duration formatting, fuzzy title
matching, vote counting, storage, yt-dlp info parsing) — no credentials
needed:

```
python -m unittest discover -s tests -v
```
