# syntax=docker/dockerfile:1.7
# Static ffmpeg instead of apt's (~300 MB of dependencies on a small root disk).

# ──────────────────────────────────────────────────────────────────────────
# Stage 1: ffmpeg (static build)
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS ffmpeg
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# BtbN builds daily from master and ships arm64 (johnvansickle froze in 2024,
# amd64 only). The gpl variant has libx264 for re-encoding. Its binaries are
# ~140 MB each; strip doesn't help.
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) ff_arch=linux64 ;; \
      arm64) ff_arch=linuxarm64 ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-${ff_arch}-gpl.tar.xz" \
        -o /tmp/ff.tar.xz; \
    mkdir -p /ff; \
    tar -xJf /tmp/ff.tar.xz -C /ff --strip-components=1; \
    cp /ff/bin/ffmpeg /ff/bin/ffprobe /usr/local/bin/; \
    ffmpeg -version | head -1

# ──────────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/

# JS runtime for YouTube: since late 2025 yt-dlp solves its JS challenges with
# an external runtime; without one, formats go missing. deno is yt-dlp's default.
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno

# uid/gid 101 matches telegram-bot-api, so both containers share the data dir.
RUN groupadd --system --gid 101 app \
    && useradd --system --uid 101 --gid app --home-dir /srv/app --shell /usr/sbin/nologin app

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /srv/app
COPY requirements.txt .
# chown in the pip layer so it stays cached across code changes; app owns the
# venv so the entrypoint can upgrade yt-dlp.
RUN pip install --no-cache-dir -r requirements.txt && chown -R app:app "$VIRTUAL_ENV"

COPY --chown=app:app . .
RUN chmod +x docker/entrypoint.sh

USER app

# The bot touches /tmp/heartbeat every 30s. This only marks the container
# unhealthy; a hung event loop is restarted by the bot's own watchdog.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,time,sys; sys.exit(0 if os.path.exists('/tmp/heartbeat') and time.time()-os.path.getmtime('/tmp/heartbeat')<120 else 1)"

ENTRYPOINT ["docker/entrypoint.sh"]
