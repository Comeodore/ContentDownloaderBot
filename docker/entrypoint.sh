#!/usr/bin/env sh
set -e

mkdir -p "${DOWNLOAD_DIR:-/var/lib/telegram-bot-api/downloads}" 2>/dev/null || true

# Upgrade yt-dlp on every start; nightly keeps up with site changes.
echo "[entrypoint] Updating yt-dlp to the latest nightly..."
pip install --no-cache-dir -U --pre "yt-dlp[default]" || \
    echo "[entrypoint] Update failed (no network?), starting with the current version."

echo "[entrypoint] yt-dlp: $(yt-dlp --version 2>/dev/null || echo '?') | ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 || echo '?')"

exec python bot.py
