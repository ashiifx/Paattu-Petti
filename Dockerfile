FROM python:3.12-slim

# ffmpeg: required by py-tgcalls for streaming and by the audio effects.
# Node 20 + git: the bgutil PO-token provider server (see below).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git curl ca-certificates \
    && curl -fsSL https://nodejs.org/dist/v20.18.1/node-v20.18.1-linux-x64.tar.gz -o /tmp/node.tar.gz \
    && tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 \
    && rm /tmp/node.tar.gz \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# bgutil PO-token provider server, version-matched with the pip plugin in
# requirements.txt (2.0.2). YouTube demands a PO token for playback from
# datacenter IPs; this server generates them locally on port 4416 and the
# yt-dlp plugin picks them up automatically.
RUN git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm install \
    && npx tsc

COPY . .

CMD ["bash", "run.sh"]
