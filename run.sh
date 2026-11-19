#!/usr/bin/env bash
# Paattu Petti entrypoint.
# - The bgutil PO-token server is intentionally NOT started: YouTube
#   bot-walls this host's IP regardless of tokens (storyboard-only),
#   JioSaavn is the primary source, and the Node server cost ~200 MB
#   of the free tier's 512 MB.
# - Optional WARP egress (YT_VIA_WARP=1): registers a free Cloudflare
#   WARP account and starts wireproxy (userspace WireGuard -> SOCKS5
#   on 127.0.0.1:40001) so yt-dlp's YouTube requests leave via
#   Cloudflare instead of this host's burned IP. Any failure here is
#   logged and the bot continues without the tunnel.
cd /app

if [ "${YT_VIA_WARP:-}" = "1" ] || [ "${YT_VIA_WARP:-}" = "true" ]; then
  echo "[run.sh] WARP: registering account + starting wireproxy..."
  mkdir -p /tmp/warp && cd /tmp/warp
  if wgcf register --accept-tos >register.log 2>&1 \
     && wgcf generate >>register.log 2>&1; then
    printf '\n[Socks5]\nBindAddress = 127.0.0.1:40001\n' >> wgcf-profile.conf
    wireproxy -c wgcf-profile.conf >wireproxy.log 2>&1 &
    for i in $(seq 1 20); do
      (echo > /dev/tcp/127.0.0.1/40001) 2>/dev/null && break
      sleep 1
    done
    echo "[run.sh] WARP: wireproxy up on socks5://127.0.0.1:40001"
  else
    echo "[run.sh] WARP: registration failed - continuing without the tunnel (see /tmp/warp/register.log)"
  fi
  cd /app
fi

exec python bot.py
