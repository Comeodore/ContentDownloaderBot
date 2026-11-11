"""The owner's watch queue: post parsing, the SQLite queue and the handlers
that mirror replies and reactions into the channel (with a fake bot)."""
import asyncio
import contextlib
import dataclasses
import logging
import re
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.handler import FilterObject
from aiogram.types import ReactionTypeCustomEmoji, ReactionTypeEmoji, ReactionTypePaid

import config as config_module
import watch

CH = -1001234567890
OWNER = 1
REEL = "https://www.instagram.com/reel/ABCdef12345/?igsh=dGVzdA=="


# ── Pure helpers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("https://t.me/c/1234567890/750", 750),
    ("t.me/c/1234567890/751?single", 751),
    ("from here: https://t.me/c/1234567890/12/752", 752),  # topic link
    ("https://t.me/c/1111111111/750", None),  # another channel
    ("https://t.me/somechannel/750", None),
    ("", None),
    (None, None),
])
def test_parse_post_link(text, expected):
    assert watch.parse_post_link(text, CH) == expected


def test_post_link():
    assert watch.post_link(CH, 750) == "https://t.me/c/1234567890/750"


def _post(text=None, caption=None, entities=None, **media):
    return SimpleNamespace(text=text, caption=caption, entities=entities,
                           caption_entities=None, **media)


def test_read_post_link_only():
    assert watch.read_post(_post(REEL)) == ("link", REEL, "")


def test_read_post_keeps_the_note():
    assert watch.read_post(_post(f"look at this 😂\n{REEL}")) == ("link", REEL, "look at this 😂")


def test_read_post_skips_unsupported_links():
    kind, url, _ = watch.read_post(_post(f"https://example.com/x {REEL}"))
    assert (kind, url) == ("link", REEL)


def test_read_post_hidden_link():
    entity = SimpleNamespace(type="text_link", url=REEL)
    assert watch.read_post(_post("this one", entities=[entity])) == ("link", REEL, "this one")


def test_read_post_media_and_nothing():
    assert watch.read_post(_post(caption="cat", video=object())) == ("media", None, "cat")
    assert watch.read_post(_post("just text")) == ("none", None, "just text")


def test_dedup_key_ignores_query_and_url_form():
    keys = {watch.dedup_key(u) for u in (
        REEL,
        "https://instagram.com/reel/ABCdef12345",
        "https://www.instagram.com/p/ABCdef12345/",
        "https://www.instagram.com/someone/reel/ABCdef12345/?utm_source=ig",
    )}
    assert keys == {"instagram:ABCdef12345"}
    assert (watch.dedup_key("https://www.youtube.com/watch?v=a")
            != watch.dedup_key("https://www.youtube.com/watch?v=b"))


def test_reaction_html():
    assert watch.reaction_html([ReactionTypeEmoji(emoji="❤"), ReactionTypePaid()]) == "❤"
    assert watch.reaction_html([ReactionTypeCustomEmoji(custom_emoji_id="42")]) == \
        '<tg-emoji emoji-id="42">⭐</tg-emoji>'
    assert watch.reaction_html([]) == ""


def test_caption():
    assert watch.caption(CH, 750, 2, 5, "") == \
        '2/5 · <a href="https://t.me/c/1234567890/750">post</a>'
    long = watch.caption(CH, 750, 1, 5, "<b>" + "x" * 1000)
    assert "&lt;b&gt;" in long and long.endswith("…")
    assert len(long) < 1024 // 2


# ── The queue ───────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return watch.Store(str(tmp_path / "watch.db"))


def test_queue_order_and_repeats(store):
    assert store.add(12, "link", REEL, "")
    assert store.add(10, "link", "https://www.tiktok.com/@a/video/1", "first")
    assert not store.add(13, "link", REEL.split("?")[0], "")  # the same reel again
    assert not store.add(14, "none", None, "hello")
    assert store.add(15, "media", None, "")
    assert [p.post_id for p in store.queued()] == [10, 12, 15]
    assert [p.post_id for p in store.queued(2)] == [10, 12]
    assert store.count_queued() == 3
    assert store.known(13) and not store.known(11)


def test_sent_posts_and_mirrors(store):
    store.add(10, "link", REEL, "")
    store.mark_sent(10, [100, 101])  # an album: both messages lead to the post
    assert store.count_queued() == 0
    assert store.post_of(100) == store.post_of(101) == 10
    assert store.post_of(102) is None
    store.set_mirror(10, 555)
    assert store.mirror_of(10) == 555
    store.set_mirror(10, None)
    assert store.mirror_of(10) is None
    assert not store.add(11, "link", REEL, "")  # already watched


def test_start_from(store):
    for pid in (10, 11, 12, 13):
        store.add(pid, "link", f"https://www.tiktok.com/@a/video/{pid}", "")
    store.mark_sent(12, [100])
    store.set("caught_up", 1)
    store.start_from(11)
    assert store.get("start") == 11
    assert store.get("cursor") == 11 - watch.COMMENT_LOOKBACK  # for the first link's comment
    assert store.more_to_read()
    assert store.queued() == []  # 10 skipped; 11 and 13 forgotten, to be imported again
    assert store.known(10) and store.known(12) and not store.known(11)
    assert not store.add(9, "link", "https://www.tiktok.com/@a/video/9", "")  # before start
    assert store.add(11, "link", "https://www.tiktok.com/@a/video/11", "")


def test_own_posts_are_known(store):
    store.add_own(20)
    assert store.known(20)


def test_config_reads_watch_ids(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "0:test")
    monkeypatch.setenv("WATCH_CHANNEL_ID", str(CH))
    assert config_module.load_config().watch_channel_id == CH
    monkeypatch.setenv("WATCH_CHANNEL_ID", "")
    assert config_module.load_config().watch_channel_id is None
    monkeypatch.setenv("WATCH_CHANNEL_ID", "nope")
    with pytest.raises(RuntimeError):
        config_module.load_config()


# ── Handlers, with a fake bot ───────────────────────────────────────────────

class FakeBot:
    def __init__(self):
        self.calls = []
        self.next_id = 900

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "send_media_group":
                out = []
                for _ in args[1]:
                    self.next_id += 1
                    out.append(SimpleNamespace(message_id=self.next_id))
                return out
            self.next_id += 1
            return SimpleNamespace(message_id=self.next_id)
        return call


@pytest.fixture
def cfg(config, store, monkeypatch):
    monkeypatch.setattr(watch, "_store", store)
    monkeypatch.setattr(watch, "_kick_prefetch", lambda config: None)
    return dataclasses.replace(config(), owner_id=OWNER, watch_channel_id=CH)


def _reaction(*emoji, message_id=100, user_id=OWNER):
    return SimpleNamespace(chat=SimpleNamespace(id=OWNER), message_id=message_id,
                           user=SimpleNamespace(id=user_id),
                           new_reaction=[ReactionTypeEmoji(emoji=e) for e in emoji])


def test_reaction_is_mirrored_changed_and_removed(cfg, store):
    store.add(750, "link", REEL, "")
    store.mark_sent(750, [100])
    bot = FakeBot()

    asyncio.run(watch._on_reaction(_reaction("❤"), bot, cfg))
    name, args, kwargs = bot.calls[-1]
    assert name == "send_message" and args == (CH, "❤")
    assert kwargs["reply_parameters"].message_id == 750
    mirror = store.mirror_of(750)
    assert mirror and store.known(mirror)  # its channel_post won't be queued

    asyncio.run(watch._on_reaction(_reaction("😂"), bot, cfg))
    assert bot.calls[-1][0] == "edit_message_text"
    assert bot.calls[-1][2]["message_id"] == mirror

    # The same post shown again (◀️) is a new message: its reaction edits the same reply.
    store.mark_sent(750, [200])
    asyncio.run(watch._on_reaction(_reaction("🔥", message_id=200), bot, cfg))
    assert bot.calls[-1][0] == "edit_message_text" and bot.calls[-1][1][0] == "🔥"
    assert store.mirror_of(750) == mirror

    asyncio.run(watch._on_reaction(_reaction(message_id=200), bot, cfg))
    assert bot.calls[-1][:2] == ("delete_message", (CH, mirror))
    assert store.mirror_of(750) is None


def test_reactions_elsewhere_are_ignored(cfg, store):
    store.add(750, "link", REEL, "")
    store.mark_sent(750, [100])
    bot = FakeBot()
    asyncio.run(watch._on_reaction(_reaction("❤", message_id=5), bot, cfg))  # not a watch video
    asyncio.run(watch._on_reaction(_reaction("❤", user_id=2), bot, cfg))  # not the owner
    assert bot.calls == []


def _filter(callback, message, config):
    """Runs a filter as aiogram does: a sync one goes to a worker thread, where
    the store's SQLite connection can't be used."""
    return asyncio.run(FilterObject(callback).call(message, config=config))


def test_reply_goes_under_the_post(cfg, store):
    store.add(750, "link", REEL, "")
    store.mark_sent(750, [100])
    msg = SimpleNamespace(chat=SimpleNamespace(id=OWNER), from_user=SimpleNamespace(id=OWNER),
                          message_id=300, reply_to_message=SimpleNamespace(message_id=100))
    assert _filter(watch._is_watch_reply, msg, cfg)
    assert not _filter(watch._is_watch_reply, SimpleNamespace(
        **{**vars(msg), "reply_to_message": SimpleNamespace(message_id=7)}), cfg)
    bot = FakeBot()
    store.set_screen([100])  # the video replied to is on screen
    asyncio.run(watch._on_reply(msg, bot, cfg))
    name, args, kwargs = bot.calls[0]
    assert name == "copy_message" and args == (CH, OWNER, 300)
    assert kwargs["reply_parameters"].message_id == 750
    assert bot.calls[1][0] == "set_message_reaction"
    assert store.screen() == [100, 300]  # the reply goes when the post is switched

    store.set_screen([])  # a reply to a video no longer on screen stays in the chat
    asyncio.run(watch._on_reply(SimpleNamespace(**{**vars(msg), "message_id": 301}), bot, cfg))
    assert store.screen() == []


def test_channel_post_is_queued_once(cfg, store):
    bot = FakeBot()
    post = SimpleNamespace(chat=SimpleNamespace(id=CH), message_id=760, text=REEL, caption=None,
                           entities=None, caption_entities=None)
    asyncio.run(watch._on_channel_post(post, bot, cfg))
    asyncio.run(watch._on_channel_post(post, bot, cfg))
    assert [p.post_id for p in store.queued()] == [760]
    assert store.get("cursor") == 761 and store.get("latest") == 760
    assert not store.more_to_read()  # no start point: only new posts, nothing to read
    store.add_own(761)
    asyncio.run(watch._on_channel_post(
        SimpleNamespace(**{**vars(post), "message_id": 761}), bot, cfg))
    assert store.count_queued() == 1


def _player(monkeypatch, tmp_path, *, gone=(), broken=()):
    """Fakes for downloading and sending; returns (sent_media, failures)."""
    import downloader
    sent_media, failures = [], []

    async def fake_download(p, config):
        if p.url in gone:
            raise downloader.DownloadError("This video is no longer available", user_side=True)
        if p.url in broken:
            raise downloader.DownloadError("The site refused to hand over the file.")
        return str(tmp_path / "w"), downloader.DownloadResult()

    async def fake_send_media(anchor, result, url, caption, disable_notification=None):
        assert anchor.chat.id == OWNER
        sent_media.append((url, caption, disable_notification))
        n = len(sent_media)
        return [SimpleNamespace(message_id=500 + n, video=SimpleNamespace(file_id=f"F{n}"))]

    async def fake_failure(message, config, url, error, cause=None, user_side=False, report=True):
        assert report  # watch reports only bot failures; guest walls are skipped as gone
        failures.append((message.from_user.id, url, error, user_side))

    monkeypatch.setattr(watch, "_download", fake_download)
    monkeypatch.setattr(watch, "_deps", SimpleNamespace(
        failure=fake_failure, send_media=fake_send_media, slot=None))
    return sent_media, failures


def _press(data, bot, cfg):
    async def answer(*args, **kwargs):
        pass
    callback = SimpleNamespace(from_user=SimpleNamespace(id=OWNER), data=data, answer=answer)
    asyncio.run(watch._on_button(callback, bot, cfg))


def _text(call):
    """The text of a send_message(chat, text) or edit_message_text(text, ...) call."""
    return call[1][0] if call[0] == "edit_message_text" else call[1][1]


def _buttons(call):
    markup = call[2].get("reply_markup")
    return [[b.text for b in row] for row in markup.inline_keyboard] if markup else []


def test_batch_shows_one_post_at_a_time(cfg, store, monkeypatch, tmp_path):
    store.add(10, "link", REEL, "look")
    store.add(11, "link", _tiktok(1), "")   # deleted: skipped, the channel is told
    store.add(12, "link", _tiktok(2), "")   # a bot failure: ❌ in its place
    store.add(13, "media", None, "")
    for pid in (14, 15, 16):
        store.add(pid, "link", _tiktok(pid), "")
    store.set("cursor", 17)
    store.set("caught_up", 1)
    sent_media, failures = _player(monkeypatch, tmp_path, gone={_tiktok(1)}, broken={_tiktok(2)})
    bot = ChannelBot({})

    assert asyncio.run(watch._new_batch(bot, cfg))
    # Five places: 10, ❌12, 13, 14, 15 — the deleted 11 doesn't take one.
    assert [b[0] for b in store.batch()] == [10, 12, 13, 14, 15]
    assert bot.calls[0][1] == (CH, "Пост недоступен")
    assert bot.calls[0][2]["reply_parameters"].message_id == 11
    assert failures == [("watch channel", _tiktok(2), "The site refused to hand over the file.",
                         False)]
    # Only the first post is on screen, and it notifies.
    assert sent_media == [(REEL, watch.caption(CH, 10, 1, 5, "look"), False)]
    control = [c for c in bot.calls if c[0] == "send_message" and c[1][0] == OWNER][-1]
    assert control[1][1] == "📺 1 of 5" and _buttons(control) == [["1/5", "▶️"]]
    assert store.screen() == [501] and store.post_of(501) == 10
    assert [p.post_id for p in store.queued()] == [12, 13, 14, 15, 16]  # not shown yet

    bot.calls.clear()
    old_control = store.get("control")
    _press(watch.NEXT, bot, cfg)  # → the ❌ in place 2: the video and its buttons go
    assert bot.calls[0][:2] == ("delete_messages", (OWNER, [501, old_control]))
    failed = bot.calls[1]
    assert failed[1][1].startswith("❌ 2/5") and failed[2]["disable_notification"] is True
    assert _buttons(bot.calls[-1]) == [["◀️", "2/5", "▶️"]]

    _press(watch.NEXT, bot, cfg)  # → the media post, copied from the channel
    copied = [c for c in bot.calls if c[0] == "copy_message"][-1]
    assert copied[1] == (OWNER, CH, 13) and copied[2]["disable_notification"] is True

    for _ in range(2):
        _press(watch.NEXT, bot, cfg)
    assert [u for u, _, _ in sent_media] == [REEL, _tiktok(14), _tiktok(15)]
    assert sent_media[-1][1].startswith("5/5") and sent_media[-1][2] is True
    last = bot.calls[-1]
    assert _text(last) == "📺 1 more to watch"
    assert _buttons(last) == [["◀️", "5/5"], ["▶️ Next 1"]]

    # Back to the first: sent again from its file id, nothing downloaded or uploaded.
    for _ in range(4):
        _press(watch.PREV, bot, cfg)
    again = [c for c in bot.calls if c[0] == "send_video"][-1]
    assert again[1] == (OWNER, "F1") and again[2]["caption"].startswith("1/5")
    assert len(sent_media) == 3
    assert store.post_of(store.screen()[0]) == 10
    assert watch._fetches == {} or set(watch._fetches) <= {16}

    # Next 1 → a batch of one; then nothing new.
    for _ in range(4):
        _press(watch.NEXT, bot, cfg)
    _press(watch.MORE, bot, cfg)
    assert [b[0] for b in store.batch()] == [16]
    assert sent_media[-1][1].startswith("1/1")
    assert _buttons(bot.calls[-1]) == [["1/1"]] and _text(bot.calls[-1]) == "✅ All watched"
    _press(watch.MORE, bot, cfg)  # a stale Next: still all watched
    assert [b[0] for b in store.batch()] == [16]


def test_control_view():
    assert watch.control_view(0, 5, 3, False) == ("📺 1 of 5", [[("1/5", watch.NOOP),
                                                                 ("▶️", watch.NEXT)]])
    text, rows = watch.control_view(4, 5, 7, True)
    assert text == "📺 7+ more to watch" and rows[1] == [("▶️ Next 5", watch.MORE)]
    assert watch.control_view(4, 5, 0, True)[0] == "📺 More to watch"
    assert watch.control_view(None, 0, 2, False) == ("📺 2 more to watch",
                                                     [[("▶️ Next 2", watch.MORE)]])
    assert watch.control_view(None, 0, 0, False) == ("✅ All watched", [])
    assert watch.control_view(1, 5, 0, False, loading=True) == ("⏳ Loading…", [])


def test_file_ids_and_album_resend():
    msgs = [SimpleNamespace(video=SimpleNamespace(file_id="V"), photo=None),
            SimpleNamespace(video=None, animation=None, photo=[SimpleNamespace(file_id="small"),
                                                              SimpleNamespace(file_id="P")]),
            SimpleNamespace(video=None, animation=None, photo=None,
                            audio=SimpleNamespace(file_id="A"))]
    files = watch.file_ids(msgs)
    assert files == [["video", "V"], ["photo", "P"], ["audio", "A"]]
    bot = FakeBot()
    ids = asyncio.run(watch._resend(bot, OWNER, files, "1/5", True))
    assert [c[0] for c in bot.calls] == ["send_media_group", "send_audio"]
    media = bot.calls[0][1][1]
    assert [m.media for m in media] == ["V", "P"] and media[0].caption == "1/5"
    assert bot.calls[1][2]["caption"] is None and len(ids) == 3


# The download and failure lines as the Grafana dashboard parses them (the user regexps in
# /opt/monitoring/grafana/dashboards/services/contentdl-bot.json). Keep them in sync.
DASHBOARD_RE = re.compile(r"(?:for|для) (?P<user>\d+|watch channel)(?: @\w+)?: (?P<link>https?://[^ ?#]+)")


def test_downloads_and_failures_are_logged_for_the_channel(cfg, monkeypatch, tmp_path, caplog):
    """Not for the owner: the dashboard's Users table shows the channel as its own row."""
    import bot as bot_module
    import downloader

    async def fake_download(url, config, work_dir):
        return downloader.DownloadResult()

    @contextlib.asynccontextmanager
    async def slot():
        yield

    monkeypatch.setattr(downloader, "download", fake_download)
    monkeypatch.setattr(watch, "_deps", SimpleNamespace(slot=slot, failure=bot_module._failure))
    cfg = dataclasses.replace(cfg, download_dir=str(tmp_path), owner_id=0)
    post = watch.Post(post_id=10, kind="link", url=REEL, note="")
    with caplog.at_level(logging.INFO, logger="videobot"):
        asyncio.run(watch._download(post, cfg))
        asyncio.run(watch._report(FakeBot(), cfg, post, downloader.DownloadError("boom")))
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith(("Downloaded for", "Download failed for"))]
    assert [line.split(":")[0] for line in lines] == [
        "Downloaded for watch channel", "Download failed for watch channel"]
    for line in lines:
        m = DASHBOARD_RE.search(line)
        assert (m["user"], m["link"]) == ("watch channel", REEL.split("?")[0])
    assert bot_module._user_ref(watch.CHANNEL) == "Watch queue (watch channel)"



class ChannelBot(FakeBot):
    """Forwards the given channel posts into the DM; other ids are missing."""

    def __init__(self, posts):
        super().__init__()
        self.posts = posts

    async def forward_message(self, chat_id, from_chat_id, message_id, **kwargs):
        from aiogram.exceptions import TelegramBadRequest
        self.calls.append(("forward_message", (chat_id, from_chat_id, message_id), kwargs))
        if message_id not in self.posts:
            raise TelegramBadRequest(method=None,
                                     message="Bad Request: message to forward not found")
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id, text=self.posts[message_id],
                               caption=None, entities=None, caption_entities=None)


def _tiktok(n):
    return f"https://www.tiktok.com/@a/video/{n}"


def test_read_goes_a_batch_at_a_time_and_cleans_up(cfg, store):
    posts = {750: REEL, 751: "hi", 753: _tiktok(1), 754: _tiktok(2), 755: _tiktok(3),
             756: _tiktok(4), 757: _tiktok(5), 758: _tiktok(6)}
    bot = ChannelBot(posts)
    store.start_from(750)

    asyncio.run(watch._read(bot, cfg))
    assert [p.post_id for p in store.queued()] == [750, 753, 754, 755, 756]
    assert store.get("cursor") == 757 and store.more_to_read()
    forwards = [c for c in bot.calls if c[0] == "forward_message"]
    assert all(c[1][0] == OWNER and c[2]["disable_notification"] for c in forwards)
    # Every copy in the DM is deleted right away.
    assert len([c for c in bot.calls if c[0] == "delete_message"]) == len(posts) - 2

    for p in store.queued():
        store.mark_sent(p.post_id, [])
    asyncio.run(watch._read(bot, cfg))
    assert [p.post_id for p in store.queued()] == [757, 758]
    assert store.get("caught_up") == 1 and store.get("cursor") == 759
    assert not store.more_to_read()
    tried = [c[1][2] for c in bot.calls if c[0] == "forward_message"]
    assert tried[-1] == 758 + watch.END_MISSES  # probed past the end, then stopped

    calls = len(bot.calls)
    asyncio.run(watch._read(bot, cfg))  # caught up: no probing again
    assert len(bot.calls) == calls


def test_read_fills_a_gap_after_catching_up(cfg, store):
    bot = ChannelBot({800: _tiktok(1), 802: _tiktok(2)})
    store.start_from(800)
    store.set("caught_up", 1)
    store.set("latest", 803)  # post 803 came as an update; 800-802 weren't read
    store.add(803, "link", _tiktok(3), "")
    asyncio.run(watch._read(bot, cfg))
    assert [p.post_id for p in store.queued()] == [800, 802, 803]
    assert [c[1][2] for c in bot.calls if c[0] == "forward_message"] == [797, 798, 799, 800,
                                                                        801, 802]


# ── A comment posted right before the link ──────────────────────────────────

T = 1700000000
AUTHOR = "Author One"


def test_comment_before_the_link_goes_with_it(store):
    store.add(759, "link", _tiktok(1), "", T - 100, AUTHOR)
    store.add(760, "none", None, "Я", T, AUTHOR)
    store.add(761, "none", None, "смотри", T + 30, AUTHOR)
    assert store.add(762, "link", REEL, "", T + 60, AUTHOR)
    assert store.queued()[-1].note == "Я\nсмотри"


def test_comment_and_own_text_both_go(store):
    store.add(760, "none", None, "Я", T, AUTHOR)
    store.add(761, "link", REEL, "this one", T + 5, AUTHOR)
    assert store.queued()[0].note == "Я\nthis one"


@pytest.mark.parametrize("prev", [
    (760, "none", None, "old", T - watch.COMMENT_WINDOW - 1, AUTHOR),  # too long before
    (760, "none", None, "Я", T, "Someone Else"),  # another author
    (759, "none", None, "Я", T, AUTHOR),  # not right before: 760 is unknown
    (760, "link", _tiktok(1), "", T, AUTHOR),  # a link isn't a comment
])
def test_not_a_comment(store, prev):
    store.add(*prev)
    store.add(761, "link", REEL, "", T + 5, AUTHOR)
    assert store.queued()[-1].note == ""


def test_post_meta_reads_the_forwarded_original():
    from datetime import datetime, timezone
    d = datetime.fromtimestamp(T, timezone.utc)
    fw = SimpleNamespace(forward_origin=SimpleNamespace(date=d, author_signature=AUTHOR),
                         date=datetime.now(timezone.utc), author_signature=None)
    assert watch.post_meta(fw) == (T, AUTHOR)
    post = SimpleNamespace(date=d, author_signature=None)
    assert watch.post_meta(post) == (T, None)


def test_old_database_gets_the_new_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE posts (post_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, url TEXT,"
               " key TEXT, note TEXT NOT NULL DEFAULT '', state TEXT NOT NULL)")
    db.execute("INSERT INTO posts VALUES (5, 'link', 'u', 'k', '', 'sent')")
    db.commit()
    db.close()
    store = watch.Store(path)
    assert store.add(760, "none", None, "Я", T, AUTHOR) is False
    assert store.add(761, "link", REEL, "", T + 5, AUTHOR)
    assert store.queued()[0].note == "Я"


# ── The daily batch ─────────────────────────────────────────────────────────

def test_parse_daily():
    from datetime import time
    assert watch.parse_daily("12:00") == time(12, 0)
    assert watch.parse_daily("9:05") == time(9, 5)
    for bad in ("12", "12:0", "noon", "25:00", "12:60"):
        with pytest.raises(ValueError):
            watch.parse_daily(bad)


def _kyiv(*args):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(*args, tzinfo=ZoneInfo("Europe/Kyiv"))


def test_daily_due():
    from datetime import time
    noon = time(12, 0)
    today = _kyiv(2026, 11, 10, 0, 0).date().toordinal()
    assert not watch.daily_due(_kyiv(2026, 11, 10, 11, 59), noon, None)
    assert watch.daily_due(_kyiv(2026, 11, 10, 12, 0), noon, None)
    assert watch.daily_due(_kyiv(2026, 11, 10, 14, 0), noon, today - 1)  # caught up after a restart
    assert not watch.daily_due(_kyiv(2026, 11, 10, 12, 30), noon, today)  # already sent today
    assert not watch.daily_due(_kyiv(2026, 11, 10, 15, 30), noon, None)  # too late, wait for tomorrow


def test_next_daily_and_dst():
    from datetime import time
    noon = time(12, 0)
    assert watch.next_daily(_kyiv(2026, 11, 10, 2, 50), noon) == _kyiv(2026, 11, 10, 12, 0)
    assert watch.next_daily(_kyiv(2026, 11, 10, 12, 0), noon) == _kyiv(2026, 11, 11, 12, 0)
    # The night clocks go back (25 Oct 2026): still noon local time.
    nxt = watch.next_daily(_kyiv(2026, 10, 24, 13, 0), noon)
    assert nxt == _kyiv(2026, 10, 25, 12, 0) and nxt.utcoffset().total_seconds() == 7200


def test_quiet_batch_with_nothing_new_sends_nothing(cfg, store):
    bot = ChannelBot({})
    store.set("cursor", 900)
    store.set("caught_up", 1)
    assert asyncio.run(watch._new_batch(bot, cfg, quiet=True)) is False
    assert bot.calls == []
    store.set("control", 777)
    assert asyncio.run(watch._new_batch(bot, cfg)) is False  # a pressed Next
    assert bot.calls[-1][0] == "edit_message_text" and bot.calls[-1][1][0] == "✅ All watched"


def test_short_batch_is_numbered_by_what_there_is(cfg, store, monkeypatch, tmp_path):
    for pid in (10, 11):
        store.add(pid, "link", _tiktok(pid), "")
    store.set("cursor", 12)
    store.set("caught_up", 1)
    sent_media, _ = _player(monkeypatch, tmp_path)
    bot = ChannelBot({})
    assert asyncio.run(watch._new_batch(bot, cfg, quiet=True))
    _press(watch.NEXT, bot, cfg)
    assert [c.split(" ·")[0] for _, c, _ in sent_media] == ["1/2", "2/2"]
    assert store.count_queued() == 0


# ── 🗑 Remove my replies ─────────────────────────────────────────────────────

def test_control_offers_to_remove_replies():
    text, rows = watch.control_view(1, 5, 3, False, answers=2)
    assert rows == [[("◀️", watch.PREV), ("2/5", watch.NOOP), ("▶️", watch.NEXT)],
                    [("🗑 Remove my replies (2)", watch.UNDO)]]
    _, rows = watch.control_view(4, 5, 1, False, answers=1)
    assert rows[1:] == [[("🗑 Remove my replies (1)", watch.UNDO)], [("▶️ Next 1", watch.MORE)]]


def test_remove_my_replies(cfg, store):
    store.add(750, "link", REEL, "")
    store.set_batch([(750, None)])
    store.mark_sent(750, [100])
    store.set_screen([100])
    store.set("control", 101)
    bot = FakeBot()
    reply = SimpleNamespace(chat=SimpleNamespace(id=OWNER), from_user=SimpleNamespace(id=OWNER),
                            message_id=300, reply_to_message=SimpleNamespace(message_id=100))
    asyncio.run(watch._on_reply(reply, bot, cfg))
    copy_id = bot.next_id - 2  # copy_message, then the 👌, then the control edit
    asyncio.run(watch._on_reaction(_reaction("❤"), bot, cfg))
    mirror = store.mirror_of(750)
    assert store.answers_count(750) == 2
    assert _buttons(bot.calls[-1])[-1] == ["🗑 Remove my replies (2)"]

    bot.calls.clear()
    _press(watch.UNDO, bot, cfg)
    assert bot.calls[0][:2] == ("delete_messages", (CH, [copy_id, mirror]))
    assert bot.calls[1][:2] == ("delete_messages", (OWNER, [300]))
    assert store.answers_count(750) == 0 and store.screen() == [100]
    assert _buttons(bot.calls[-1]) == [["1/1"]]  # the button is gone
