# 📥 Content Downloader

Telegram bot: send a link, get the video or photos back.

- **TikTok**: videos without watermark, photo slideshows with their music
- **Instagram**: reels, posts, carousels, stories
- **YouTube**: Shorts, videos, clips
- **Threads**: videos, photos, carousels

Timecodes after a link (`<link> 1:00-1:10, 2:10-2:55`) cut clips.

Stack: [aiogram 3](https://aiogram.dev/), [yt-dlp](https://github.com/yt-dlp/yt-dlp), ffmpeg. TikTok comes without a watermark by default: yt-dlp takes the stream before the watermark is applied.

## Running locally

```bash
brew install ffmpeg            # or: sudo apt install ffmpeg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # set BOT_TOKEN from @BotFather
python bot.py
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Offline: no network, Telegram or real downloads.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | token from [@BotFather](https://t.me/BotFather) (required) |
| `MAX_FILE_SIZE_MB` | file size limit: `49` without a local server, up to `1900` with one |
| `COOKIES_FILE` | path to `cookies.txt` (logged-in sessions, see below) |
| `OWNER_ID` | owner's Telegram id: uploads cookies, gets failure reports and cookie reminders |
| `YTDLP_AUTO_UPDATE_HOURS` | restart period to update yt-dlp (`0` = off) |
| `TELEGRAM_API_BASE` | local Bot API server URL, for files up to 2 GB |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | from my.telegram.org, local server only |

## Failure reports

When a download fails for anyone, the owner gets a report: user, link, the bot's diagnosis and the raw cause. The user sees the reason only when it's theirs to deal with (deleted video, wrong link, size limit); otherwise a standard reply says the developer has been notified.

The report's **🛠 Send to Claude** button (owner only) queues it in `claude-inbox/` on the data volume, where a Claude Code session picks it up; see `inbox.py`.

## 🐳 Deploy

Two services: the bot and a local Bot API server. The official Bot API caps bot uploads at 50 MB; the local server ([`aiogram/telegram-bot-api`](https://github.com/aiogram/telegram-bot-api) in `--local` mode) raises it to 2 GB and takes files by path.

Currently on host `ubuntu` (192.168.0.185): code in `/root/contentdl-bot`, data in `/root/contentdl-data`.

`CONTENTDL_DATA_DIR` is mounted into **both** containers at `/var/lib/telegram-bot-api`, or path mode can't find the files. It holds `cookies.txt` and server state (~100 KB) plus downloads while they're sent (up to ~10 GB at peak). Keep it **outside** the code dir: `COPY . .` would put `data/cookies.txt` into the image.

First deploy:

```bash
# data dir owned by uid 101 (the telegram-bot-api user)
ssh root@YOUR_SERVER 'mkdir -p /path/to/contentdl-data/downloads && chown -R 101:101 /path/to/contentdl-data'
# .env on the host: BOT_TOKEN, TELEGRAM_API_ID/HASH, MAX_FILE_SIZE_MB=1900, CONTENTDL_DATA_DIR
COPYFILE_DISABLE=1 tar czf - --exclude=.git --exclude=.venv --exclude=.env . | ssh root@YOUR_SERVER 'tar xzf - -C /path/to/contentdl-bot'
ssh root@YOUR_SERVER 'cd /path/to/contentdl-bot && docker compose up -d --build'
```

After code changes:

```bash
scp -r bot.py downloader.py config.py yt_dlp_plugins root@YOUR_SERVER:/path/to/contentdl-bot/
ssh root@YOUR_SERVER 'cd /path/to/contentdl-bot && docker compose up -d --build bot'
```

Logs: `docker compose logs -f bot`.

### Updates

- **yt-dlp**: `entrypoint.sh` installs the latest nightly on every start, and the bot restarts itself every `YTDLP_AUTO_UPDATE_HOURS` (24 h by default) once idle.
- **Bot API server**: the host's shared Watchtower pulls new images. The stack has no Watchtower of its own: a second unscoped instance stops and removes the first. On a host without one, add it.
- **Bot image**: built locally, updated by rebuilding.

## Cookies for Instagram and Threads

Instagram and Threads are fetched **as a guest first**: public posts, reels and carousels need no login. The session from `cookies.txt` is used only for what guests don't get: stories, private accounts, posts hidden from logged-out visitors. Without it those links fail with "Cookie problem: …", and the owner isn't notified.

To add a session:

1. Install the **Get cookies.txt LOCALLY** browser extension.
2. Log in to Instagram, or to threads.com for Threads (Instagram cookies don't work there).
3. Export **only the current site**, not all cookies: the bot replaces every site found in the file.
4. Send the file to the bot as a document (owner only); it's merged into the existing one.

If a link fails under the session too, the user sees "Cookie problem: the saved … cookies …" and the owner is asked for fresh cookies.

> ⚠️ Use a secondary account only. The bot is public and Instagram counts every session request against the account: in 2026-11 the account all links went through was locked for automated activity.

## Layout

```
bot.py                — aiogram handlers, polling, scheduled restart
downloader.py         — yt-dlp wrapper: link checks, download, limits
config.py             — .env loading
yt_dlp_plugins/       — yt-dlp plugins: Threads, Instagram photos/stories, TikTok photos
Dockerfile            — bot image (python + ffmpeg)
docker/entrypoint.sh  — yt-dlp update on start, then the bot
docker-compose.yml    — bot + local Bot API server
```
