"""Owner-only watch queue for one channel (WATCH_CHANNEL_ID).

Links posted in the channel are queued, downloaded ahead and sent to the
owner's DM a batch at a time ("Next 5"). The owner's replies to those videos,
and reactions on them, go back to the channel as replies to the original post.

The Bot API can't read a channel's history: an older post is read by
forwarding it silently into the owner's DM and deleting the copy at once. That
happens a batch at a time, right when the owner presses Next (they're in the
chat then, so nothing notifies). The owner starts the queue by sending the bot
a link to the first unwatched post; new posts arrive as channel_post.

With WATCH_DAILY_AT set, the next batch also goes out by itself every day at
that time (WATCH_TZ); with nothing new it sends nothing.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher, F, html
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageReactionUpdated,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
    ReplyParameters,
)

import downloader
from config import Config

logger = logging.getLogger("videobot.watch")

BATCH = 5
# Reading stops after this many ids in a row that can't be forwarded: past the
# newest post every id fails, and gaps (deleted posts, service messages) are
# much shorter.
END_MISSES = 30
# Post text shown under a video; short enough for the 1024-unit caption limit.
NOTE_LIMIT = 400
# Text-only posts right before a link, by the same author and at most this
# long before it, are its comment (people send "look 😂" and then the link).
COMMENT_WINDOW = 10 * 60
# Posts read before the start point, for the first link's comment.
COMMENT_LOOKBACK = 3
# A daily batch missed while the bot was down (a restart) goes out late, up to this.
DAILY_CATCH_UP = timedelta(hours=3)
NEXT_BUTTON = "watch:next"
# The bot's reply in the channel under a post whose video can't be had
# (deleted, hidden, not a video). In the channel's language, at the owner's word.
GONE_REPLY = "Пост недоступен"
# Shown in place of a custom emoji where custom emoji can't be rendered.
CUSTOM_EMOJI_FALLBACK = "⭐"

_POST_LINK_RE = re.compile(r"(?:https?://)?t\.me/c/(\d+)/(?:\d+/)?(\d+)")
_INSTAGRAM_CODE_RE = re.compile(r"/(?:p|reels?|tv)/([^/]+)")


# ── Pure helpers ────────────────────────────────────────────────────────────

def _internal_id(channel_id: int) -> str:
    """The id in t.me/c/<id>/... links: the chat id without its -100 prefix."""
    return str(channel_id).removeprefix("-100")


def post_link(channel_id: int, post_id: int) -> str:
    return f"https://t.me/c/{_internal_id(channel_id)}/{post_id}"


def parse_post_link(text: str | None, channel_id: int) -> int | None:
    """The post id from a t.me/c/ link to this channel, else None."""
    for m in _POST_LINK_RE.finditer(text or ""):
        if m.group(1) == _internal_id(channel_id):
            return int(m.group(2))
    return None


def read_post(msg) -> tuple[str, str | None, str]:
    """(kind, url, note) of a channel post.

    kind is "link" (a supported link to download), "media" (a video or photo
    posted as is, delivered by copying) or "none". note is the post text
    without the link.
    """
    text = getattr(msg, "text", None) or getattr(msg, "caption", None) or ""
    entities = getattr(msg, "entities", None) or getattr(msg, "caption_entities", None) or []
    candidates = [e.url for e in entities if getattr(e, "type", None) == "text_link" and e.url]
    rest = text
    for _ in range(20):
        if not (url := downloader.extract_url(rest)):
            break
        candidates.append(url)
        rest = rest.replace(url, " ", 1)
    url = next((u for u in candidates if downloader.is_supported(u)), None)
    note = " ".join((text.replace(url, " ") if url else text).split())
    if url:
        return "link", url, note
    if any(getattr(msg, k, None) for k in ("video", "animation", "photo", "video_note")):
        return "media", None, note
    return "none", None, note


def post_meta(msg) -> tuple[int | None, str | None]:
    """(unix time, author signature) of a channel post, or of the original of
    a forwarded one."""
    origin = getattr(msg, "forward_origin", None)
    src = origin if origin is not None else msg
    date = getattr(src, "date", None)
    return (int(date.timestamp()) if date else None), getattr(src, "author_signature", None)


def dedup_key(url: str) -> str:
    """Same video, same key: Instagram /p/, /reel/, /reels/ and query strings ignored."""
    site = downloader.site_of(url) or "?"
    parts = urlsplit(url)
    if site == "instagram" and (m := _INSTAGRAM_CODE_RE.search(parts.path)):
        return f"instagram:{m.group(1)}"
    return f"{site}:{(parts.hostname or '').lower()}{parts.path.rstrip('/')}?{parts.query}"


def reaction_html(reactions) -> str:
    """The owner's reactions as message text; paid reactions are left out."""
    parts = []
    for r in reactions or []:
        if isinstance(r, ReactionTypeEmoji):
            parts.append(r.emoji)
        elif isinstance(r, ReactionTypeCustomEmoji):
            parts.append(f'<tg-emoji emoji-id="{html.quote(r.custom_emoji_id)}">'
                         f"{CUSTOM_EMOJI_FALLBACK}</tg-emoji>")
    return " ".join(parts)


def caption(channel_id: int, post_id: int, i: int, n: int, note: str) -> str:
    text = f'{i}/{n} · <a href="{post_link(channel_id, post_id)}">post</a>'
    if note:
        if len(note) > NOTE_LIMIT:
            note = note[:NOTE_LIMIT - 1] + "…"
        text += "\n" + html.quote(note)
    return text


# ── State ───────────────────────────────────────────────────────────────────

@dataclass
class Post:
    post_id: int
    kind: str
    url: str | None
    note: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id INTEGER PRIMARY KEY,  -- message id in the channel
    kind TEXT NOT NULL,           -- link | media | none
    url TEXT,
    key TEXT,                     -- dedup_key(url)
    note TEXT NOT NULL DEFAULT '',  -- the post's text, with its comment for link/media
    state TEXT NOT NULL,          -- queued | sent | skipped | gone (deleted video)
    date INTEGER,                 -- unix time
    author TEXT,                  -- author signature
    text TEXT NOT NULL DEFAULT '' -- the post's own text without the link
);
CREATE INDEX IF NOT EXISTS posts_key ON posts(key);
CREATE TABLE IF NOT EXISTS sent (
    dm_id INTEGER PRIMARY KEY,    -- message in the owner's DM
    post_id INTEGER NOT NULL,
    reaction_id INTEGER           -- channel message mirroring the owner's reaction
);
-- Channel messages the bot posted itself: never queued.
CREATE TABLE IF NOT EXISTS own (post_id INTEGER PRIMARY KEY);
-- start: first post to watch; cursor: next post id to read; latest: newest
-- post id seen; caught_up: reading reached the end (new posts come as updates);
-- control: the "Next" message in the DM; daily_day: date.toordinal() of the
-- last daily batch.
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER);
"""


class Store:
    """The queue in SQLite. Used from the event loop thread only."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(_SCHEMA)
        # Columns added after the first release.
        have = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
        for column in ("date INTEGER", "author TEXT", "text TEXT NOT NULL DEFAULT ''"):
            if column.split()[0] not in have:
                self.db.execute(f"ALTER TABLE posts ADD COLUMN {column}")

    def get(self, key: str) -> int | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: int | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def known(self, post_id: int) -> bool:
        return bool(self.db.execute(
            "SELECT 1 FROM posts WHERE post_id = ? UNION SELECT 1 FROM own WHERE post_id = ?",
            (post_id, post_id)).fetchone())

    def add(self, post_id: int, kind: str, url: str | None, text: str,
            date: int | None = None, author: str | None = None) -> bool:
        """Records a post; True if it was queued (not a repeat, not before start).
        A link or media post gets the comment posted right before it."""
        key = dedup_key(url) if url else None
        start = self.get("start")
        queued = (kind != "none" and (start is None or post_id >= start)
                  and not (key and self.db.execute(
                      "SELECT 1 FROM posts WHERE key = ? AND state != 'skipped'",
                      (key,)).fetchone()))
        note = text
        if kind != "none":
            note = "\n".join(filter(None, (self.comment_for(post_id, date, author), text)))
        cur = self.db.execute(
            "INSERT OR IGNORE INTO posts (post_id, kind, url, key, note, state, date, author, text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (post_id, kind, url, key, note, "queued" if queued else "skipped", date, author, text))
        return queued and cur.rowcount == 1

    def comment_for(self, post_id: int, date: int | None, author: str | None) -> str:
        """The text-only posts right before post_id: same author, within
        COMMENT_WINDOW. Stops at a missing post, a link or media."""
        parts = []
        for prev in range(post_id - 1, max(post_id - 20, 0), -1):
            row = self.db.execute("SELECT kind, text, date, author FROM posts WHERE post_id = ?",
                                  (prev,)).fetchone()
            if not row or row[0] != "none" or not row[1]:
                break
            if date is not None and (row[2] is None or not 0 <= date - row[2] <= COMMENT_WINDOW):
                break
            if author and row[3] and author != row[3]:
                break
            parts.append(row[1])
        return "\n".join(reversed(parts))

    def queued(self, limit: int | None = None, after: int = 0) -> list[Post]:
        rows = self.db.execute(
            "SELECT post_id, kind, url, note FROM posts WHERE state = 'queued' AND post_id > ? "
            "ORDER BY post_id LIMIT ?", (after, -1 if limit is None else limit)).fetchall()
        return [Post(*r) for r in rows]

    def mark_gone(self, post_id: int) -> None:
        self.db.execute("UPDATE posts SET state = 'gone' WHERE post_id = ?", (post_id,))

    def count_queued(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM posts WHERE state = 'queued'").fetchone()[0]

    def mark_sent(self, post_id: int, dm_ids: list[int]) -> None:
        self.db.execute("UPDATE posts SET state = 'sent' WHERE post_id = ?", (post_id,))
        self.db.executemany("INSERT OR REPLACE INTO sent (dm_id, post_id) VALUES (?, ?)",
                            [(dm_id, post_id) for dm_id in dm_ids])

    def post_of(self, dm_id: int) -> int | None:
        row = self.db.execute("SELECT post_id FROM sent WHERE dm_id = ?", (dm_id,)).fetchone()
        return row[0] if row else None

    def reaction_of(self, dm_id: int) -> int | None:
        row = self.db.execute("SELECT reaction_id FROM sent WHERE dm_id = ?", (dm_id,)).fetchone()
        return row[0] if row else None

    def set_reaction(self, dm_id: int, channel_msg_id: int | None) -> None:
        self.db.execute("UPDATE sent SET reaction_id = ? WHERE dm_id = ?", (channel_msg_id, dm_id))

    def add_own(self, post_id: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO own VALUES (?)", (post_id,))

    def start_from(self, post_id: int) -> None:
        """Everything before post_id counts as watched; from it on, posts not
        yet sent are forgotten and read again. Reading starts a few posts
        earlier, for the first link's comment."""
        self.db.execute("UPDATE posts SET state = 'skipped' WHERE post_id < ? AND state = 'queued'",
                        (post_id,))
        self.db.execute("DELETE FROM posts WHERE post_id >= ? AND state != 'sent'", (post_id,))
        self.set("start", post_id)
        self.set("cursor", max(1, post_id - COMMENT_LOOKBACK))  # known ones aren't re-read
        self.set("caught_up", 0)

    def more_to_read(self) -> bool:
        """Whether unread posts may be ahead of the cursor."""
        cursor = self.get("cursor")
        if cursor is None:
            return False
        return not self.get("caught_up") or (self.get("latest") or 0) >= cursor


# ── Telegram side ───────────────────────────────────────────────────────────

_store: Store | None = None
# Set by setup(): bot.py's helpers (bot.py can't be imported from here).
_deps = SimpleNamespace(failure=None, send_media=None, slot=None)

# Who the queue's downloads and failures are for in the logs, the Grafana dashboard
# and failure reports: the channel, not the owner who reads it.
CHANNEL = SimpleNamespace(id="watch channel", first_name="Watch queue", username=None)

# Downloads per post id: a task returning (work_dir, DownloadResult).
_fetches: dict[int, asyncio.Task] = {}
_batch_lock = asyncio.Lock()
_prefetch_task: asyncio.Task | None = None
_daily_task: asyncio.Task | None = None


def enabled(config: Config) -> bool:
    return _store is not None and bool(config.owner_id and config.watch_channel_id)


def _owner(user, config: Config) -> bool:
    return bool(user and config.owner_id and user.id == config.owner_id)


def _in_owner_dm(message: Message, config: Config) -> bool:
    return enabled(config) and message.chat.id == config.owner_id and _owner(message.from_user, config)


def setup(bot: Bot, config: Config, *, failure, send_media, slot) -> None:
    """Opens the queue and resumes downloading ahead; no-op unless configured."""
    global _store
    if not (config.owner_id and config.watch_channel_id):
        return
    db_path = os.path.join(os.path.dirname(os.path.abspath(config.download_dir)), "watch.db")
    _store = Store(db_path)
    _deps.failure, _deps.send_media, _deps.slot = failure, send_media, slot
    logger.info("Watch queue on for channel %s: %d queued", config.watch_channel_id,
                _store.count_queued())
    _kick_prefetch(config)
    if config.watch_daily_at:
        try:
            at, tz = parse_daily(config.watch_daily_at), ZoneInfo(config.watch_tz)
        except (ValueError, KeyError) as e:
            logger.error("Watch: bad WATCH_DAILY_AT/WATCH_TZ (%s), no daily batch", e)
            return
        global _daily_task
        _daily_task = asyncio.create_task(_daily(bot, config, at, tz))
        logger.info("Watch: daily batch at %s %s", at.strftime("%H:%M"), config.watch_tz)


def register(dp: Dispatcher) -> None:
    """Called before the bot's own handlers: replies to watch videos aren't links."""
    dp.message.register(_on_reply, F.reply_to_message, _is_watch_reply)
    dp.message.register(_on_watch_command, Command("watch"), _in_owner_dm)
    dp.message.register(_on_start_link, F.text, _is_start_link)
    dp.callback_query.register(_on_next, F.data == NEXT_BUTTON)
    dp.message_reaction.register(_on_reaction)
    dp.channel_post.register(_on_channel_post)


async def _is_watch_reply(message: Message, config: Config) -> bool:
    # async: aiogram runs sync filters in a worker thread, and the store is loop-thread only.
    return (_in_owner_dm(message, config)
            and _store.post_of(message.reply_to_message.message_id) is not None)


def _is_start_link(message: Message, config: Config) -> bool:
    return (_in_owner_dm(message, config)
            and parse_post_link(message.text, config.watch_channel_id) is not None)


async def _on_reply(message: Message, bot: Bot, config: Config) -> None:
    """The owner's reply to a watch video goes to the channel under the post."""
    post_id = _store.post_of(message.reply_to_message.message_id)
    try:
        copied = await bot.copy_message(
            config.watch_channel_id, message.chat.id, message.message_id,
            reply_parameters=ReplyParameters(message_id=post_id, allow_sending_without_reply=True))
    except Exception as e:  # noqa: BLE001
        logger.warning("Watch: couldn't post the owner's reply to post %d", post_id, exc_info=True)
        await message.reply(f"❌ Couldn't post it to the channel: {html.quote(str(e))}")
        return
    _store.add_own(copied.message_id)
    logger.info("Watch: the owner's reply to post %d went to the channel", post_id)
    with contextlib.suppress(Exception):
        await bot.set_message_reaction(message.chat.id, message.message_id,
                                       [ReactionTypeEmoji(emoji="👌")])


async def _on_reaction(event: MessageReactionUpdated, bot: Bot, config: Config) -> None:
    """The owner's reaction on a watch video is mirrored as a reply under the post:
    sent, edited when changed, deleted when removed."""
    if not enabled(config) or event.chat.id != config.owner_id or not _owner(event.user, config):
        return
    post_id = _store.post_of(event.message_id)
    if post_id is None:
        return
    text = reaction_html(event.new_reaction)
    mirror = _store.reaction_of(event.message_id)
    channel = config.watch_channel_id
    try:
        if mirror and not text:
            await bot.delete_message(channel, mirror)
            _store.set_reaction(event.message_id, None)
        elif mirror:
            await _send_or_edit(bot, channel, text, edit_id=mirror)
        elif text:
            sent = await _send_or_edit(bot, channel, text, reply_to=post_id)
            _store.add_own(sent.message_id)
            _store.set_reaction(event.message_id, sent.message_id)
        logger.info("Watch: the owner's reaction on post %d mirrored to the channel", post_id)
    except Exception:  # noqa: BLE001
        logger.warning("Watch: couldn't mirror the reaction on post %d", post_id, exc_info=True)


async def _send_or_edit(bot: Bot, chat_id: int, text: str, *, reply_to: int | None = None,
                        edit_id: int | None = None):
    """Sends or edits a text; without custom emoji if Telegram rejects them."""
    async def go(t: str):
        if edit_id:
            return await bot.edit_message_text(t, chat_id=chat_id, message_id=edit_id)
        return await bot.send_message(chat_id, t, reply_parameters=ReplyParameters(
            message_id=reply_to, allow_sending_without_reply=True))
    try:
        return await go(text)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return None
        plain = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", text)
        if plain == text:
            raise
        return await go(plain)


async def _on_channel_post(message: Message, bot: Bot, config: Config) -> None:
    if not enabled(config) or message.chat.id != config.watch_channel_id:
        return
    post_id = message.message_id
    if _store.known(post_id):
        return
    kind, url, note = read_post(message)
    queued = _store.add(post_id, kind, url, note, *post_meta(message))
    logger.info("Watch: new post %d (%s)%s", post_id, kind, "" if queued else ", not queued")
    _store.set("latest", max(post_id, _store.get("latest") or 0))
    cursor = _store.get("cursor")
    if cursor is None:  # no start point: only new posts are watched
        _store.set("caught_up", 1)
    if cursor is None or post_id == cursor:
        _store.set("cursor", post_id + 1)
    # A post past the cursor leaves a gap; latest makes the next read cover it.
    if queued:
        _kick_prefetch(config)
        await _control(bot, config)


async def _on_watch_command(message: Message, bot: Bot, config: Config) -> None:
    await _control(bot, config, new=True)


async def _on_start_link(message: Message, bot: Bot, config: Config) -> None:
    """A link to a channel post: watch from that post on."""
    post_id = parse_post_link(message.text, config.watch_channel_id)
    async with _batch_lock:
        _store.start_from(post_id)
        logger.info("Watch: starting from post %d", post_id)
        await _read(bot, config)
    await _control(bot, config, new=True)
    _kick_prefetch(config)


async def _on_next(callback: CallbackQuery, bot: Bot, config: Config) -> None:
    if not enabled(config) or not _owner(callback.from_user, config):
        await callback.answer()
        return
    if _batch_lock.locked():
        await callback.answer("Already sending")
        return
    async with _batch_lock:
        await callback.answer()
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)
        await _batch(bot, config)
    _kick_prefetch(config)


async def _batch(bot: Bot, config: Config, quiet: bool = False) -> bool:
    """Reads and sends the next batch (fewer if that's all there is); False if
    there was nothing to send. With nothing to send, a pressed button gets a
    fresh "All watched"; a quiet (daily) run sends nothing. The caller holds
    _batch_lock."""
    await _read(bot, config)  # this batch, if not read yet
    if not _store.count_queued():
        await _control(bot, config, new=not quiet)
        return False
    sent = await _send_batch(bot, config)
    await _read(bot, config)  # the next one, so it downloads ahead
    await _control(bot, config)
    return sent


async def _send_batch(bot: Bot, config: Config) -> bool:
    """Sends the next BATCH posts in order (fewer if that's all), then a fresh
    "Next" message. A post whose video can't be had (deleted, hidden, not a
    video) doesn't take a place: the next posts fill in, and the channel gets
    GONE_REPLY under it. False if nothing was sent."""
    ready: list[tuple[Post, object]] = []  # (post, (work_dir, result) or the error)
    last = 0
    while len(ready) < BATCH:
        posts = _store.queued(BATCH - len(ready), after=last)
        if not posts:
            await _read(bot, config)
            posts = _store.queued(BATCH - len(ready), after=last)
            if not posts:
                break
        for p in posts:
            if p.kind == "link":
                _fetch(p, config)  # all at once; the job slots limit the parallelism
        for p in posts:
            last = p.post_id
            if p.kind != "link":
                ready.append((p, None))
                continue
            try:
                ready.append((p, await _fetches[p.post_id]))
            except downloader.DownloadError as e:
                if e.user_side:
                    _release(p.post_id)
                    _store.mark_gone(p.post_id)
                    logger.info("Watch: post %d skipped: %s", p.post_id, e)
                    await _reply_gone(bot, config, p.post_id)
                else:
                    ready.append((p, e))
            except Exception as e:  # noqa: BLE001
                ready.append((p, e))

    # bot.py's sender answers a Message. The pressed one can be inaccessible
    # (too old), so a stand-in for the owner's DM is used.
    anchor = Message(message_id=0, date=datetime.now(timezone.utc),
                     chat=Chat(id=config.owner_id, type="private")).as_(bot)
    for i, (p, got) in enumerate(ready, 1):
        cap = caption(config.watch_channel_id, p.post_id, i, len(ready), p.note)
        if p.kind == "media":
            dm_ids = await _deliver_media(bot, config, p, cap)
        elif isinstance(got, BaseException):
            dm_ids = await _deliver_failure(bot, config, p, cap, got)
        else:
            dm_ids = await _deliver_download(bot, config, p, cap, got[1], anchor)
        _store.mark_sent(p.post_id, dm_ids)
    await _control(bot, config, new=bool(ready))
    return bool(ready)


async def _reply_gone(bot: Bot, config: Config, post_id: int) -> None:
    try:
        sent = await bot.send_message(config.watch_channel_id, GONE_REPLY,
                                      reply_parameters=ReplyParameters(message_id=post_id))
        _store.add_own(sent.message_id)
    except Exception:  # noqa: BLE001
        logger.warning("Watch: couldn't reply under post %d", post_id, exc_info=True)


async def _deliver_download(bot: Bot, config: Config, p: Post, cap: str, result,
                            anchor: Message) -> list[int]:
    try:
        msgs = await _deps.send_media(anchor, result, p.url, cap)
        return [m.message_id for m in msgs]
    except Exception as e:  # noqa: BLE001
        failed = e
    finally:
        _release(p.post_id)
    return await _deliver_failure(bot, config, p, cap, failed,
                                  what="Telegram didn't accept the file")


async def _deliver_failure(bot: Bot, config: Config, p: Post, cap: str, e: BaseException,
                           what: str | None = None) -> list[int]:
    """A ❌ in the batch's place; reported like any failure the owner saw."""
    _fetches.pop(p.post_id, None)
    if what is None:
        what = str(e) if isinstance(e, downloader.DownloadError) else "Unexpected error"
    await _deps.failure(SimpleNamespace(from_user=CHANNEL, bot=bot), config, p.url, what,
                        downloader.error_detail(e), user_side=False)
    sent = await bot.send_message(
        config.owner_id, f"❌ {cap}\n{html.quote(what)}\n{html.quote(p.url)}",
        link_preview_options=LinkPreviewOptions(is_disabled=True))
    return [sent.message_id]


async def _deliver_media(bot: Bot, config: Config, p: Post, cap: str) -> list[int]:
    try:
        copied = await bot.copy_message(config.owner_id, config.watch_channel_id, p.post_id,
                                        caption=cap)
        return [copied.message_id]
    except Exception as e:  # noqa: BLE001
        logger.warning("Watch: couldn't copy post %d", p.post_id, exc_info=True)
        sent = await bot.send_message(config.owner_id,
                                      f"❌ {cap}\nCouldn't copy it: {html.quote(str(e))}")
        return [sent.message_id]


# ── The daily batch ─────────────────────────────────────────────────────────

def parse_daily(value: str) -> dtime:
    """ "12:00" → time(12, 0); ValueError if it isn't HH:MM."""
    hours, sep, minutes = value.partition(":")
    if not sep or not hours.isdigit() or len(minutes) != 2 or not minutes.isdigit():
        raise ValueError(f"WATCH_DAILY_AT={value!r} isn't HH:MM")
    return dtime(int(hours), int(minutes))


def daily_due(now: datetime, at: dtime, last_day: int | None) -> bool:
    """Whether today's batch should go out now: its time has come, it hasn't
    gone out today, and it's not too late to catch up."""
    run = datetime.combine(now.date(), at, tzinfo=now.tzinfo)
    return run <= now < run + DAILY_CATCH_UP and last_day != now.date().toordinal()


def next_daily(now: datetime, at: dtime) -> datetime:
    day = now.date() if now.time() < at else now.date() + timedelta(days=1)
    return datetime.combine(day, at, tzinfo=now.tzinfo)


async def _daily(bot: Bot, config: Config, at: dtime, tz: ZoneInfo) -> None:
    while True:
        now = datetime.now(tz)
        if daily_due(now, at, _store.get("daily_day")):
            # Marked first: a crash mid-batch mustn't send a second batch on restart.
            _store.set("daily_day", now.date().toordinal())
            try:
                async with _batch_lock:
                    sent = await _batch(bot, config, quiet=True)
                logger.info("Watch: daily batch %s", "sent" if sent else "skipped, nothing new")
            except Exception:  # noqa: BLE001
                logger.exception("Watch: the daily batch failed")
            _kick_prefetch(config)
        target = next_daily(datetime.now(tz), at)
        # Short sleeps: the wall clock may jump (NTP, DST) while asleep.
        while (left := (target - datetime.now(tz)).total_seconds()) > 0:
            await asyncio.sleep(min(left, 600))


# ── Downloads ahead ─────────────────────────────────────────────────────────

def _fetch(p: Post, config: Config) -> asyncio.Task:
    """The post's download, started if absent or failed before (a retry,
    unless the video is gone: that's final)."""
    task = _fetches.get(p.post_id)
    if task is None or (task.done() and (task.cancelled() or (
            task.exception() and not getattr(task.exception(), "user_side", False)))):
        task = asyncio.create_task(_download(p, config))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # mark retrieved
        _fetches[p.post_id] = task
    return task


async def _download(p: Post, config: Config):
    os.makedirs(config.download_dir, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="watch_", dir=config.download_dir)
    try:
        async with _deps.slot():
            result = await downloader.download(p.url, config, work_dir)
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    logger.info("Downloaded for %s: %s — %d file(s), %.1f MB", CHANNEL.id, p.url,
                len(result.items), result.total_size / 1024 / 1024)
    return work_dir, result


def _release(post_id: int) -> None:
    """Forgets a download and deletes its files."""
    task = _fetches.pop(post_id, None)
    if task and task.done() and not task.cancelled() and not task.exception():
        shutil.rmtree(task.result()[0], ignore_errors=True)


def _kick_prefetch(config: Config) -> None:
    global _prefetch_task
    if enabled(config) and (_prefetch_task is None or _prefetch_task.done()):
        _prefetch_task = asyncio.create_task(_prefetch(config))


async def _prefetch(config: Config) -> None:
    """Keeps the next batch downloaded, one at a time so users aren't kept waiting.
    A failure isn't retried here: delivery retries it live."""
    while True:
        window = [p for p in _store.queued(BATCH) if p.kind == "link"]
        ids = {p.post_id for p in window}
        if not _batch_lock.locked():
            for post_id in [k for k, t in _fetches.items() if k not in ids and t.done()]:
                _release(post_id)  # no longer queued (a new start point)
        todo = [p for p in window if p.post_id not in _fetches]
        if not todo:
            return
        with contextlib.suppress(Exception):
            await _fetch(todo[0], config)


# ── Reading the channel ─────────────────────────────────────────────────────

async def _read(bot: Bot, config: Config, want: int = BATCH) -> None:
    """Reads posts from the cursor on until `want` are queued or the channel ends.

    Each post is forwarded silently into the owner's DM, read and deleted at
    once: the Bot API has no other way to see an older post.
    """
    post_id = _store.get("cursor")
    misses = errors = 0
    try:
        while post_id is not None and _store.count_queued() < want:
            if post_id > (_store.get("latest") or 0) and (
                    _store.get("caught_up") or misses >= END_MISSES):
                if not _store.get("caught_up"):
                    _store.set("caught_up", 1)
                    logger.info("Watch: read the channel up to post %d", _store.get("cursor") - 1)
                return
            if _store.known(post_id):
                post_id += 1
                misses = 0
                _store.set("cursor", max(post_id, _store.get("cursor")))
                continue
            try:
                fw = await bot.forward_message(config.owner_id, config.watch_channel_id, post_id,
                                               disable_notification=True)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except TelegramBadRequest as e:
                # "message to forward not found": deleted, or past the newest post.
                if "not found" not in str(e):
                    logger.warning("Watch: post %d can't be forwarded: %s", post_id, e)
                misses += 1
                post_id += 1
                continue
            except Exception:  # noqa: BLE001
                errors += 1
                if errors > 3:
                    raise
                await asyncio.sleep(2 * errors)
                continue
            with contextlib.suppress(Exception):
                await bot.delete_message(config.owner_id, fw.message_id)
            errors = misses = 0
            kind, url, note = read_post(fw)
            _store.add(post_id, kind, url, note, *post_meta(fw))
            post_id += 1
            _store.set("cursor", post_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Watch: reading stopped at post %s", post_id)
        with contextlib.suppress(Exception):
            await bot.send_message(config.owner_id, f"⚠️ Couldn't read post {post_id} of the "
                                   f"channel: {html.quote(str(e))}")


# ── The "Next" message ──────────────────────────────────────────────────────

async def _control(bot: Bot, config: Config, new: bool = False) -> None:
    """The queue size and the Next button: edited in place, or sent anew under
    the latest batch (the old one loses its button)."""
    if not enabled(config):
        return
    n = _store.count_queued()
    more = _store.more_to_read()
    if n or more:
        text = f"📺 {n}+ to watch" if more and n else ("📺 More to watch" if more
                                                       else f"📺 {n} to watch")
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"▶️ Next {BATCH if more else min(n, BATCH)}", callback_data=NEXT_BUTTON)]])
    else:
        text, markup = "✅ All watched", None
    old = _store.get("control")
    try:
        if not new and old:
            with contextlib.suppress(TelegramBadRequest):  # not modified / deleted
                await bot.edit_message_text(text, chat_id=config.owner_id, message_id=old,
                                            reply_markup=markup)
            return
        if not new:
            return
        if old:
            with contextlib.suppress(TelegramBadRequest):
                await bot.edit_message_reply_markup(chat_id=config.owner_id, message_id=old,
                                                    reply_markup=None)
        sent = await bot.send_message(config.owner_id, text, reply_markup=markup,
                                      disable_notification=True)
        _store.set("control", sent.message_id)
    except Exception:  # noqa: BLE001
        logger.warning("Watch: couldn't update the Next message", exc_info=True)
