FROM python:3.12-slim

# ffmpeg: required by py-tgcalls for streaming and by the audio effects.
# Node and the bgutil PO-token server used to be built here too, but
# run.sh no longer starts that server (YouTube IP-walls this host
# regardless), and building it (npm install + tsc) was the slowest
# part of every deploy — minutes of bot downtime per upload, for a
# component nothing runs. The yt-dlp plugin stays in requirements.txt
# (harmless without a server; the laptop setup can run one manually).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# WARP egress tools (used only when YT_VIA_WARP=1; see run.sh): wgcf
# registers a free Cloudflare WARP account at boot, wireproxy turns it
# into a local SOCKS5 proxy in userspace — no root or TUN device.
RUN curl -fsSL https://github.com/ViRb3/wgcf/releases/download/v2.3.1/wgcf_2.3.1_linux_amd64 -o /usr/local/bin/wgcf \
    && curl -fsSL https://github.com/pufferffish/wireproxy/releases/download/v1.0.9/wireproxy_linux_amd64.tar.gz -o /tmp/wireproxy.tar.gz \
    && tar -xzf /tmp/wireproxy.tar.gz -C /usr/local/bin \
    && rm /tmp/wireproxy.tar.gz \
    && chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "run.sh"]
