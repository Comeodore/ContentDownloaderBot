"""Telegram bot: send a link, get the video back.

With the local Bot API server it runs in path mode: files land in a directory
shared with the server, which gets a path instead of bytes (up to 2 GB).
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import random
import re
import shutil
import signal
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
)

import downloader
import inbox
from config import Config, load_config
from heartbeat import beat

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("videobot")

HEARTBEAT_FILE = "/tmp/heartbeat"

dp = Dispatcher()

WELCOME = (
    "👋 Hi! Send me a link and I'll send back the video or photos:\n\n"
    "• TikTok — videos without watermark, photo slideshows + their music\n"
    "• Instagram — Reels, posts, carousels, stories\n"
    "• YouTube — Shorts, videos and clips\n"
    "• Threads — videos, photos, carousels\n\n"
    "✂️ To cut clips, add timecodes after the link:\n"
    "<code>&lt;link&gt; 1:00-1:10, 2:10-2:55</code>\n"
    # "30 min" is MAX_CUT_SECONDS.
    "Each range comes back as a separate video (up to 30 min in total)."
)

# The bot's Telegram profile, synced on start by _sync_profile.
BOT_NAME = "Content Downloader"
BOT_SHORT_DESCRIPTION = (
    "Send a link — get the video, photos or slideshow from TikTok, Instagram, "
    "YouTube and Threads. No watermark."
)
BOT_DESCRIPTION = (
    "Downloads videos and posts from TikTok, Instagram, YouTube and Threads — "
    "just send a link.\n\n"
    "• TikTok — videos without watermark, photo slideshows with their music\n"
    "• Instagram — Reels, posts, carousels, stories\n"
    "• YouTube — Shorts, videos and clips\n"
    "• Threads — videos, photos, carousels\n\n"
    "✂️ Cut clips: add timecodes after the link, e.g. 1:00-1:10, 2:10-2:55"
)

# Cookie reminders at most every 6 h. -inf, not 0: monotonic() counts from host
# boot, so right after a reboot 0 would mute the first reminder.
_last_cookie_notify = {"t": float("-inf")}


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME)


@dp.message(F.document)
async def handle_cookies_upload(message: Message, config: Config) -> None:
    """The owner sends cookies.txt as a document; it's merged in live."""
    if not config.owner_id or not message.from_user or message.from_user.id != config.owner_id:
        return  # others' documents are ignored
    doc = message.document
    name = (doc.file_name or "").lower()
    if not (name.endswith(".txt") or "cookie" in name):
        await message.answer("That doesn't look like cookies.txt — send a .txt cookies file.")
        return
    if not config.cookies_file:
        await message.answer("COOKIES_FILE isn't set in .env — nowhere to save it.")
        return
    if (doc.file_size or 0) > 1_000_000:
        await message.answer("That file is too big for a cookies.txt (limit 1 MB).")
        return
    upload = config.cookies_file + ".upload"
    try:
        await message.bot.download(doc, destination=upload)
        os.chmod(upload, 0o600)
        domains = downloader.merge_cookies_file(upload, config.cookies_file)
        if not domains:
            await message.answer(
                "No cookie lines found in that file — nothing changed. "
                "Export in <b>Netscape</b> (cookies.txt) format, not JSON."
            )
            return
        os.chmod(config.cookies_file, 0o600)
        await message.answer("✅ Cookies updated for: " + html.quote(", ".join(domains)))
        logger.info("Cookies updated by owner %s: %s",
                    message.from_user.id, ", ".join(domains))
    except Exception:  # noqa: BLE001
        logger.exception("Couldn't save cookies")
        await message.answer("❌ Couldn't save the cookies — check the bot logs.")
    finally:
        try:
            os.remove(upload)
        except OSError:
            pass


# Timecode ranges: "1:00-1:10", "1:02:03-1:02:30", "5-30". The edge guards keep
# "2024-2025" from reading as a 0:24–3:22 clip.
_RANGE_RE = re.compile(
    r"(?<![\d:])(\d{1,3}(?::\d{2}){0,2})\s*[-–—]\s*(\d{1,3}(?::\d{2}){0,2})(?![\d:])")
MAX_CLIPS = 20
# Clip seconds per message: pieces are encoded on the mini-PC's CPU, and the bot
# is public.
MAX_CUT_SECONDS = 30 * 60


def _ts_to_sec(ts: str) -> int:
    sec = 0
    for part in ts.split(":"):
        sec = sec * 60 + int(part)
    return sec


def _fmt_ts(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_ranges(text: str, url: str) -> tuple[list[tuple[int, int]], list[str]]:
    """(valid ranges, rejected strings with start >= end)."""
    # Without the URL, so digits in it (a dash in a YouTube ID) aren't ranges.
    cleaned = (text or "").replace(url, " ") if url else (text or "")
    ranges, rejected = [], []
    for m in _RANGE_RE.finditer(cleaned):
        start, end = _ts_to_sec(m.group(1)), _ts_to_sec(m.group(2))
        if end > start:
            ranges.append((start, end))
        else:
            rejected.append(m.group(0).strip())
    return ranges, rejected


def _looks_like_timecodes(text: str) -> bool:
    """Whether the text has mm:ss, i.e. the user meant timecodes."""
    return bool(re.search(r"\d{1,2}:\d{2}", text or ""))


@dp.message(F.text)
async def handle_link(message: Message, config: Config) -> None:
    url = downloader.extract_url(message.text)
    if not url:
        await message.answer("That doesn't look like a link 🤔 Send me a video link.")
        return

    if not downloader.is_supported(url):
        await message.answer(
            "This link isn't supported. Send a link to a single video or post "
            "from TikTok, YouTube, Instagram or Threads."
        )
        return

    ranges, rejected = _parse_ranges(message.text, url)
    cleaned = (message.text or "").replace(url, " ")
    if ranges:
        await _handle_cut(message, config, url, ranges, rejected)
    elif rejected or _looks_like_timecodes(cleaned):
        # Timecodes were meant but none parsed: don't download the whole video.
        await message.answer(
            "⚠️ I couldn't read those timecodes. Use e.g. "
            "<code>1:00-1:10, 2:10-2:55</code> — format mm:ss, start before end."
        )
    else:
        await _handle_full(message, config, url)


# Request directories left behind by a killed process (restart, OOM) are swept
# on start: they can hold gigabytes.
STALE_WORK_DIR_AGE = 6 * 3600


def _sweep_stale_work_dirs(config: Config) -> None:
    now = time.time()
    removed = 0
    for prefix in ("vdl_", "cut_"):
        for path in glob.glob(os.path.join(config.download_dir, prefix + "*")):
            if not os.path.isdir(path):
                continue
            try:
                # Fresh ones may belong to a second running instance.
                if now - os.path.getmtime(path) < STALE_WORK_DIR_AGE:
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Removed stale download dirs: %d", removed)


# Concurrent requests: encoding loads the mini-PC's 4 cores; the rest queue.
MAX_PARALLEL_JOBS = 3
_job_slots = asyncio.Semaphore(MAX_PARALLEL_JOBS)
# Requests in progress or queued; the scheduled restart waits for zero.
_jobs_in_flight = 0


# Animated custom emoji at the end of a status; they work while the bot's owner
# has Telegram Premium. They must come from the bot's pack
# contentdl_loading_by_comeodore_download_bot, which has needs_repainting:
# non-repainted white dots (5981246029926764162) vanish in the light theme.
# The pack also holds an arc spinner (5424626441687968723).
_SPINNER_IDS = (
    "5438353462108662341",  # dots
)


def _spinner() -> str:
    # ⏳ shows in notifications and the chat list.
    return f'<tg-emoji emoji-id="{random.choice(_SPINNER_IDS)}">⏳</tg-emoji>'


async def _send_status(message: Message, text: str) -> Message:
    """Sends a status, without custom emoji if Telegram rejects them (no Premium)."""
    try:
        return await message.answer(text)
    except TelegramBadRequest:
        logger.warning("Telegram rejected the status's custom emoji, sending without")
        return await message.answer(re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", text))


@contextlib.asynccontextmanager
async def _job(status: Message, working_text: str):
    """A request slot; the status says so while waiting."""
    global _jobs_in_flight
    _jobs_in_flight += 1
    try:
        queued = _job_slots.locked()
        if queued:
            await _safe_edit(status, f"Waiting in line — other downloads are running {_spinner()}")
        async with _job_slots:
            if queued:
                await _safe_edit(status, working_text)
            yield
    finally:
        _jobs_in_flight -= 1


def _who(message: Message) -> str:
    return str(message.from_user.id) if message.from_user else "?"


# For failures the developer has to fix: every failure is reported to the owner.
_NOTIFIED = "The developer has already been notified."
_STANDARD_ERROR = "Couldn't download this — something went wrong on my side. " + _NOTIFIED


def _user_text(e: downloader.DownloadError) -> str:
    """The reason if it's the user's to deal with, else the standard reply."""
    return str(e) if e.user_side else _STANDARD_ERROR


def _log_failure(message: Message, url: str, error: str, cause: str | None = None,
                 exc_info: bool = False) -> None:
    """One line per failure the user saw; the dashboard and the alert parse it.

    Format: "Download failed for <user>: <url> — <error> | cause: <cause>", where error
    is the bot's diagnosis and cause the raw yt-dlp text or exception.
    """
    line = f"Download failed for {_who(message)}: {url} — {error}"
    if cause and cause not in error:
        line += " | cause: " + " ".join(cause.split())
    logger.log(logging.ERROR if exc_info else logging.INFO, line, exc_info=exc_info)


# Failure reports to the owner: the same failure at most every 10 min, and at most
# 20 reports an hour, so a stream of broken links from strangers can't flood them.
_REPORT_REPEAT_AFTER = 600
_REPORTS_PER_HOUR = 20
_reported: dict[tuple[str, str], float] = {}
_report_times: list[float] = []
# callback_data of the button under a report.
_CLAUDE_BUTTON = "claude"
# Technical failures go to Claude's queue straight away. The bot is public, so anyone
# can trigger that with broken links: one session per problem every 6 h, at most 10 a
# day; beyond that the report keeps the button.
_AUTO_SAME_PROBLEM_AFTER = 6 * 3600
_AUTO_PER_DAY = 10
_auto_sent: dict[str, float] = {}
_auto_times: list[float] = []


def _should_report(url: str, error: str, now: float) -> bool:
    for key, t in list(_reported.items()):
        if now - t >= _REPORT_REPEAT_AFTER:
            del _reported[key]
    _report_times[:] = [t for t in _report_times if now - t < 3600]
    if (url, error) in _reported or len(_report_times) >= _REPORTS_PER_HOUR:
        return False
    _reported[(url, error)] = now
    _report_times.append(now)
    return True


def _problem_key(url: str, error: str) -> str:
    """The site and the error without ids and numbers: one session per problem."""
    text = re.sub(r"https?://\S+|\b\w*\d\w*\b", "", error.lower())
    return f"{downloader.site_of(url)}: {' '.join(text.split())[:120]}"


def _should_auto_send(url: str, error: str, now: float) -> bool:
    for key, t in list(_auto_sent.items()):
        if now - t >= _AUTO_SAME_PROBLEM_AFTER:
            del _auto_sent[key]
    _auto_times[:] = [t for t in _auto_times if now - t < 86400]
    key = _problem_key(url, error)
    if key in _auto_sent or len(_auto_times) >= _AUTO_PER_DAY:
        return False
    _auto_sent[key] = now
    _auto_times.append(now)
    return True


def _failure_report(message: Message, url: str, error: str, cause: str | None) -> str:
    user = message.from_user
    who = "?"
    if user:
        who = f"{user.id} (@{user.username})" if user.username else str(user.id)
    lines = ["❌ <b>Download failed</b>",
             f"<b>User:</b> {html.quote(who)}",
             f"<b>Link:</b> {html.quote(url)}",
             f"<b>Error:</b> {html.quote(error)}"]
    if cause and cause not in error:
        lines.append(f"<b>Cause:</b> <code>{html.quote(' '.join(cause.split())[:1500])}</code>")
    return "\n".join(lines)


async def _failure(message: Message, config: Config, url: str, error: str,
                   cause: str | None = None, exc_info: bool = False,
                   user_side: bool = False) -> None:
    """Logs a failure the user saw and reports it to the owner.

    A technical failure also goes to Claude's queue at once (within the limits above),
    and Claude answers under the report; the rest get a Send to Claude button.
    """
    _log_failure(message, url, error, cause, exc_info=exc_info)
    now = time.monotonic()
    if not config.owner_id or not _should_report(url, error, now):
        return
    auto = not user_side and _should_auto_send(url, error, now)
    text = _failure_report(message, url, error, cause)
    if auto:
        text += "\n\n🤖 <i>Sent to Claude automatically</i>"
    elif user_side:
        text += "\n\nℹ️ <i>The user's side, nothing to fix</i>"
    else:
        text += "\n\n⏸ <i>Auto-send limit reached</i>"
    button = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛠 Send to Claude", callback_data=_CLAUDE_BUTTON)]])
    try:
        sent = await message.bot.send_message(
            config.owner_id, text, link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=None if auto else button)
    except Exception:  # noqa: BLE001
        logger.exception("Couldn't send the failure report")
        return
    if auto:
        try:
            logger.info("Report queued for Claude: %s",
                        inbox.add(config, sent.chat.id, sent.message_id, sent.text or text))
        except OSError:
            logger.exception("Couldn't queue the report for Claude")


@dp.callback_query(F.data == _CLAUDE_BUTTON)
async def handle_send_to_claude(callback: CallbackQuery, config: Config) -> None:
    """The owner queues a failure report for Claude; the button works only for them."""
    msg = callback.message
    if (not config.owner_id or callback.from_user.id != config.owner_id
            or not isinstance(msg, Message) or not msg.text):
        await callback.answer()
        return
    try:
        rid = inbox.add(config, msg.chat.id, msg.message_id, msg.text)
    except OSError:
        logger.exception("Couldn't queue the report for Claude")
        await callback.answer("Couldn't queue it, see the bot logs.", show_alert=True)
        return
    logger.info("Report queued for Claude: %s", rid)
    await msg.edit_text(msg.html_text + "\n\n📨 <i>Sent to Claude</i>",
                        link_preview_options=LinkPreviewOptions(is_disabled=True))
    await callback.answer("Sent to Claude")


async def _report_download_error(
    message: Message, status: Message, config: Config, url: str,
    e: downloader.DownloadError,
) -> None:
    """Logs a download error, reports it to the owner and shows it to the user."""
    await _failure(message, config, url, str(e), downloader.error_detail(e),
                   user_side=e.user_side)
    await status.edit_text(f"❌ {html.quote(_user_text(e))}")
    if e.auth and _needs_cookies(url):
        await _notify_owner_cookies(message.bot, config, url)


async def _handle_full(message: Message, config: Config, url: str) -> None:
    # One status for download and upload, no percentages: small files finished
    # before the second edit, and in path mode the upload isn't visible to the bot.
    working_text = f"Downloading {_spinner()}"
    status = await _send_status(message, working_text)

    async with _job(status, working_text):
        os.makedirs(config.download_dir, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="vdl_", dir=config.download_dir)
        try:
            try:
                result = await downloader.download(url, config, work_dir)
            except downloader.DownloadError as e:
                await _report_download_error(message, status, config, url, e)
                return
            except Exception as e:  # noqa: BLE001
                await _failure(message, config, url, "Unexpected error",
                               downloader.error_detail(e), exc_info=True)
                await status.edit_text("❌ " + _STANDARD_ERROR)
                return
            logger.info("Downloaded for %s: %s — %d file(s), %.1f MB", _who(message), url,
                        len(result.items), result.total_size / 1024 / 1024)

            try:
                await _send_media(message, result, url)
                await status.delete()
            except Exception as e:  # noqa: BLE001
                await _failure(message, config, url, "Telegram didn't accept the file",
                               downloader.error_detail(e), exc_info=True)
                await status.edit_text("❌ " + _STANDARD_ERROR)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def _handle_cut(
    message: Message, config: Config, url: str,
    ranges: list[tuple[int, int]], rejected: list[str] | None = None,
) -> None:
    ranges = ranges[:MAX_CLIPS]
    if rejected:
        await message.answer(
            "⚠️ Skipped these — the start isn't before the end: " + ", ".join(rejected)
        )
    total = sum(end - start for start, end in ranges)
    if total > MAX_CUT_SECONDS:
        await message.answer(
            f"⚠️ That's {_fmt_ts(total)} of clips in total — I cut up to "
            f"{_fmt_ts(MAX_CUT_SECONDS)} per message."
        )
        return
    working_text = f"Cutting {len(ranges)} clip(s) {_spinner()}"
    status = await _send_status(message, working_text)

    async with _job(status, working_text):
        os.makedirs(config.download_dir, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="cut_", dir=config.download_dir)
        try:
            try:
                result = await downloader.download_clips(url, config, work_dir, ranges)
            except downloader.DownloadError as e:
                await _report_download_error(message, status, config, url, e)
                return
            except Exception as e:  # noqa: BLE001
                await _failure(message, config, url, "Unexpected error",
                               downloader.error_detail(e), exc_info=True)
                await status.edit_text("❌ " + _STANDARD_ERROR)
                return
            logger.info("Cut for %s: %s — %d of %d clip(s)", _who(message), url,
                        len(result.clips), len(ranges))

            vdur = result.duration
            sent = 0
            for i, (start, end) in enumerate(ranges):  # sent in order
                label = f"{_fmt_ts(start)}–{_fmt_ts(end)}"
                if vdur and start >= vdur:
                    await message.answer(
                        f"⏭ {label}: skipped, the video is only {_fmt_ts(vdur)} long"
                    )
                    continue
                clip = result.clips.get(i)
                if clip is None:
                    error = (result.errors.get(i)
                             or downloader.DownloadError("couldn't process this piece"))
                    await _failure(message, config, url, f"{label}: {error}",
                                   user_side=error.user_side)
                    text = str(error) if error.user_side else f"{error}. {_NOTIFIED}"
                    await message.answer(f"❌ {label}: {html.quote(text)}")
                    continue
                try:
                    await message.answer_video(
                        video=FSInputFile(clip.path, filename="clip.mp4"),
                        caption=label,
                        duration=clip.duration,
                        width=clip.width,
                        height=clip.height,
                        supports_streaming=True,
                    )
                    sent += 1
                except Exception as e:  # noqa: BLE001
                    await _failure(message, config, url,
                                   f"{label}: Telegram didn't accept this clip",
                                   downloader.error_detail(e), exc_info=True)
                    await message.answer(
                        f"❌ {label}: Telegram didn't accept this clip. {_NOTIFIED}")

            if sent:
                await status.delete()
            else:
                await _safe_edit(status, "❌ Couldn't produce any clips.")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


# Telegram counts the caption limit in UTF-16 code units, not characters.
CAPTION_LIMIT = 1024


def _truncate_utf16(text: str, limit: int = CAPTION_LIMIT) -> str:
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit - 1:  # room for "…"
            return text[:i] + "…"
    return text


def _caption_for(result: downloader.DownloadResult) -> str | None:
    """The post text for the caption: description, else title."""
    text = (result.description or "").strip() or (result.title or "").strip()
    if not text or text == "video":
        return None
    return html.quote(_truncate_utf16(text))


async def _send_media(message: Message, result: downloader.DownloadResult, url: str) -> None:
    # Captions only for Threads: elsewhere descriptions are mostly hashtags.
    caption = _caption_for(result) if downloader.is_threads(url) else None
    visual = [it for it in result.items if not it.is_audio]

    # Albums of up to 10; the caption on the first item shows under the album.
    for chunk in _chunks(visual, 10):
        if len(chunk) == 1:
            await _send_single(message, chunk[0], caption)
            caption = None
            continue
        media = []
        for it in chunk:
            f = FSInputFile(it.path)
            media.append(
                InputMediaVideo(media=f, caption=caption, supports_streaming=True)
                if it.is_video
                else InputMediaPhoto(media=f, caption=caption)
            )
            caption = None
        await message.answer_media_group(media=media)

    # TikTok slideshow music: audio can't join a photo album, so it follows alone.
    for it in result.items:
        if it.is_audio:
            await message.answer_audio(
                audio=FSInputFile(it.path, filename="music" + os.path.splitext(it.path)[1]),
                caption=caption,
                title=it.title,
                performer=it.performer,
                duration=it.duration,
            )
            caption = None


async def _send_single(
    message: Message, it: downloader.MediaItem, caption: str | None = None
) -> None:
    if it.is_video:
        await message.answer_video(
            video=FSInputFile(it.path, filename="video.mp4"),
            caption=caption,
            duration=it.duration,
            width=it.width,
            height=it.height,
            supports_streaming=True,
        )
    else:
        await message.answer_photo(photo=FSInputFile(it.path), caption=caption)


async def _safe_edit(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except Exception:  # noqa: BLE001  (not modified / rate limit)
        pass


# Sites with a session in cookies.txt, and the domain named in reminders.
_COOKIE_SITES = {"instagram": "instagram.com", "threads": "threads.com"}


def _needs_cookies(url: str) -> bool:
    """Whether a login error on this link means the saved cookies failed."""
    return downloader.site_of(url) in _COOKIE_SITES


async def _notify_owner_cookies(bot: Bot, config: Config, url: str) -> None:
    """Tells the owner the saved session failed, at most every 6 h."""
    if not config.owner_id:
        return
    now = time.monotonic()
    if now - _last_cookie_notify["t"] < 6 * 3600:
        return
    _last_cookie_notify["t"] = now
    domain = _COOKIE_SITES.get(downloader.site_of(url), "the site")
    text = (
        f"⚠️ A <b>{domain}</b> link needed the saved session, and it didn't work "
        "(login required / rate limit / expired session).\n"
        f"Export cookies.txt for <b>{domain}</b> — from a secondary account, "
        "not your main one — and send it here as a document. I merge it into "
        "the existing file, so the other sites keep working."
    )
    try:
        await bot.send_message(config.owner_id, text)
    except Exception:  # noqa: BLE001
        logger.exception("Couldn't send the cookie reminder")


# Set by _heartbeat each turn, checked by the watchdog thread.
_loop_tick = {"t": time.monotonic()}
# Seconds without a tick that mean the loop hung (heartbeat runs every 30 s).
LOOP_STALL_LIMIT = 180
# Own thread for monitor pushes: the default pool is busy with downloads.
_beat_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="heartbeat")


async def _heartbeat() -> None:
    """Every 30 s: watchdog tick, HEALTHCHECK file, monitor push.

    All from the event loop, so a hung loop stops them all.
    """
    loop = asyncio.get_running_loop()
    while True:
        _loop_tick["t"] = time.monotonic()
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
        # Not awaited: a slow monitor mustn't stall the loop; beat() swallows errors.
        loop.run_in_executor(_beat_pool, lambda: beat("polling alive", service_id="contentdl-bot"))
        await asyncio.sleep(30)


def _start_watchdog() -> None:
    """Exits the process if the event loop hangs.

    Docker doesn't restart unhealthy containers, only exited ones.
    """
    def watch() -> None:
        while True:
            time.sleep(30)
            stalled = time.monotonic() - _loop_tick["t"]
            if stalled > LOOP_STALL_LIMIT:
                logger.critical("Event loop stalled for %.0f s, restarting the process", stalled)
                os._exit(1)

    threading.Thread(target=watch, name="watchdog", daemon=True).start()


async def _sync_profile(bot: Bot) -> None:
    """Syncs the bot's name and descriptions with BOT_NAME/BOT_*DESCRIPTION.

    Only what differs is set: setMyName is tightly rate-limited and the bot
    restarts daily. Failures don't stop the start.
    """
    try:
        if (await bot.get_my_name()).name != BOT_NAME:
            await bot.set_my_name(name=BOT_NAME)
            logger.info("Bot name updated: %s", BOT_NAME)
        if (await bot.get_my_description()).description != BOT_DESCRIPTION:
            await bot.set_my_description(description=BOT_DESCRIPTION)
            logger.info("Bot description updated")
        if (await bot.get_my_short_description()).short_description != BOT_SHORT_DESCRIPTION:
            await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)
            logger.info("Bot short description updated")
    except Exception:  # noqa: BLE001
        logger.warning("Couldn't update the bot profile", exc_info=True)


# How long the scheduled restart waits for idle before restarting anyway.
RESTART_IDLE_WAIT = 3600


async def _scheduled_restart(hours: int) -> None:
    """Every `hours` hours, SIGTERMs the process once idle, so the entrypoint
    updates yt-dlp on restart. A restart mid-download would leave the user with
    an endless "Downloading", hence the wait (up to RESTART_IDLE_WAIT).
    """
    if hours <= 0:
        return
    await asyncio.sleep(hours * 3600)
    deadline = time.monotonic() + RESTART_IDLE_WAIT
    while _jobs_in_flight and time.monotonic() < deadline:
        await asyncio.sleep(10)
    if _jobs_in_flight:
        logger.warning("Scheduled restart: never idle, interrupting %d request(s)",
                       _jobs_in_flight)
    logger.info("Scheduled restart (%d h) to update yt-dlp, sending SIGTERM", hours)
    os.kill(os.getpid(), signal.SIGTERM)


async def main() -> None:
    config = load_config()

    session = None
    if config.is_local_server:
        # is_local: aiogram sends the server a file path instead of uploading.
        api_server = TelegramAPIServer.from_base(config.telegram_api_base, is_local=True)
        session = AiohttpSession(api=api_server)
        logger.info("Local Bot API server (path mode): %s", config.telegram_api_base)

    bot = Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp["config"] = config

    os.makedirs(config.download_dir, exist_ok=True)
    _sweep_stale_work_dirs(config)

    logger.info("Bot started. Downloads: %s | owner: %s", config.download_dir, config.owner_id)

    bg = [
        asyncio.create_task(_heartbeat()),
        asyncio.create_task(_scheduled_restart(config.auto_update_hours)),
    ]
    _start_watchdog()
    # Keep updates sent while the bot was down (restart, deploy).
    await bot.delete_webhook(drop_pending_updates=False)
    await _sync_profile(bot)
    try:
        await dp.start_polling(bot)
    finally:
        for t in bg:
            t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped.")
