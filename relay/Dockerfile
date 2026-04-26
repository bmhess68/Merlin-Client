FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ffmpeg static build from BtbN (ffmpeg 8.x, supports -tls_verify as a global option).
# Trixie's apt ffmpeg 7.1 does not expose -tls_verify at the top level; required for
# UniFi RTSPS sources whose self-signed cert is only valid for 127.0.0.1.
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz

RUN curl -fsSL "${FFMPEG_URL}" -o /tmp/ffmpeg.tar.xz \
    && cd /tmp && tar -xJf ffmpeg.tar.xz \
    && cp ffmpeg-*-linux64-gpl/bin/ffmpeg ffmpeg-*-linux64-gpl/bin/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg*

WORKDIR /app
COPY supervisor.py ./

CMD ["python", "-u", "supervisor.py"]
