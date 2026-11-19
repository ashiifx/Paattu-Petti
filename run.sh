#!/usr/bin/env bash
# Paattu Petti entrypoint: PO-token provider first, then the bot.
# The provider mints the PO tokens yt-dlp needs to get past YouTube's
# datacenter-IP bot check. It stays in a restart loop; the bot is the
# main process (exec) so Render health checks and signals behave.

cd /opt/bgutil/server
while true; do
    node build/main.js --port 4416
    echo "bgutil PO server exited — restarting in 3s" >&2
    sleep 3
done &

cd /app
sleep 3  # give the provider a moment to start listening
exec python bot.py
