#!/usr/bin/env bash
# Paattu Petti entrypoint. The bgutil PO-token server is intentionally
# NOT started anymore: YouTube bot-walls this host's IP regardless
# (storyboard-only responses), JioSaavn is the primary source now, and
# the Node server cost ~200 MB of the free tier's 512 MB — memory the
# bot + ffmpeg need while streaming.
cd /app
exec python bot.py
