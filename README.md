# 📥 Content Downloader

Telegram bot: send a link, get the video or photos back.

- **TikTok**: videos without watermark, photo slideshows with their music
- **Instagram**: public reels, posts, carousels
- **YouTube**: Shorts, videos, clips
- **Threads**: public videos, photos, carousels

Timecodes after a link (`<link> 1:00-1:10, 2:10-2:55`) cut clips.

When Telegram fails an upload on its own side (`Internal Server Error`), the status tells the user the upload will take a bit longer, and the bot sends again after 3, 10 and 30 s.

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
| `COOKIES_FILE` | path to `cookies.txt` (TikTok/YouTube sessions; Instagram and Threads never get cookies, see below) |
| `OWNER_ID` | owner's Telegram id: uploads cookies, gets failure reports |
| `YTDLP_AUTO_UPDATE_HOURS` | restart period to update yt-dlp (`0` = off) |
| `TELEGRAM_API_BASE` | local Bot API server URL, for files up to 2 GB |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | from my.telegram.org, local server only |
| `WATCH_CHANNEL_ID` | channel for the owner's watch queue (below); empty = off |
| `WATCH_DAILY_AT` / `WATCH_TZ` | time the next batch goes out by itself every day (`12:00`, `Europe/Kyiv`); empty = only on request |

## Watch queue (owner only)

For one channel (`WATCH_CHANNEL_ID`, the bot is an admin there) where links to videos are posted:

- The owner sends the bot a link to the first unwatched post (`t.me/c/…/750`). Older posts can't be read through the Bot API, so the bot forwards each one silently into the owner's DM and deletes the copy at once, a batch at a time when Next is pressed. New posts arrive by themselves.
- **The player:** the next five posts come one at a time: the video (captioned with a link to its post and the post's text), and under it a message with **◀️ 2/5 ▶️**. A button replaces both, so the current video is always at the bottom, ready for a reaction or a reply. Videos already sent come back from Telegram's file ids, nothing is re-uploaded. Text-only posts right before a link (same author, within 10 minutes) count as its comment and go into the caption too. With fewer left the batch is shorter (`1/3`…). A post whose video can't be had (deleted, hidden, not a video) doesn't take a place: the next one fills in, and the bot replies "Пост недоступен" under it in the channel. After the last one, **▶️ Next 5** takes the next batch; with `WATCH_DAILY_AT` a batch also comes by itself every day, and with nothing new it sends nothing. The next batch is downloaded ahead. The same video posted twice comes once. `/watch` brings the post on screen back to the bottom.
- A reply to one of those videos (text, voice, sticker…) is copied into the channel as a reply to the original post. A reaction on it is posted there the same way, edited when changed, deleted when removed — one per post, even if the video was shown again. While the owner has replies under the post on screen, **🗑 Remove my replies** deletes them from the channel (and the comments from the DM).

State: `watch.db` next to the downloads dir (`watch.py`).

## Failure reports

When a download fails for anyone, the owner gets a report: the user (clickable name, @username, id), the link, the bot's diagnosis and the raw cause. The user sees the reason only when it's theirs to deal with (deleted video, wrong link, size limit); otherwise a standard reply says the developer has been notified.

Every report also goes straight to `claude-inbox/` on the data volume, where a Claude Code worker picks it up, fixes what it can and answers under the report (see `inbox.py`). If queueing fails, the report gets a **🛠 Send to Claude** button instead (owner only).

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

## Cookies (TikTok, YouTube)

1. Install the **Get cookies.txt LOCALLY** browser extension.
2. Log in to the site and export **only the current site**, not all cookies: the bot replaces every site found in the file.
3. Send the file to the bot as a document (owner only); it's merged into the existing one.

## Instagram and Threads: guest only

Instagram and Threads are fetched **as a guest only**: the bot never sends them cookies, even if `cookies.txt` has some (an upload of their cookies is saved but reported as not used). Public posts, reels and carousels need no login.

Guests don't get stories, private accounts, or age- and audience-restricted posts. For those the user sees "Instagram/Threads shows this post only to signed-in users … I download without an account, so this one is out of my reach." Such a failure is only logged (`Download failed for …`): the owner gets no report, and Claude's worker gets nothing. A deleted Instagram post gets the same message, since to a guest it looks the same.

> ⚠️ Why no account: the bot is public, and Instagram most likely takes a browser session replayed from a server for a stolen or automated one. The owner's account was locked twice in 2026-11: once when every link went through it, and again when it was only a fallback (27 requests in 4 days). Don't bring a session back without the owner's OK.

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
