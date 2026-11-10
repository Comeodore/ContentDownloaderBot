"""The owner's watch queue: post parsing, the SQLite queue and the handlers
that mirror replies and reactions into the channel (with a fake bot)."""
import asyncio
import dataclasses
from types import SimpleNamespace

import pytest
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


def test_sent_posts_and_reactions(store):
    store.add(10, "link", REEL, "")
    store.mark_sent(10, [100, 101])  # an album: both messages lead to the post
    assert store.count_queued() == 0
    assert store.post_of(100) == store.post_of(101) == 10
    assert store.post_of(102) is None
    store.set_reaction(100, 555)
    assert store.reaction_of(100) == 555
    store.set_reaction(100, None)
    assert store.reaction_of(100) is None
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
    mirror = store.reaction_of(100)
    assert mirror and store.known(mirror)  # its channel_post won't be queued

    asyncio.run(watch._on_reaction(_reaction("😂"), bot, cfg))
    assert bot.calls[-1][0] == "edit_message_text"
    assert bot.calls[-1][2]["message_id"] == mirror

    asyncio.run(watch._on_reaction(_reaction(), bot, cfg))
    assert bot.calls[-1][:2] == ("delete_message", (CH, mirror))
    assert store.reaction_of(100) is None


def test_reactions_elsewhere_are_ignored(cfg, store):
    store.add(750, "link", REEL, "")
    store.mark_sent(750, [100])
    bot = FakeBot()
    asyncio.run(watch._on_reaction(_reaction("❤", message_id=5), bot, cfg))  # not a watch video
    asyncio.run(watch._on_reaction(_reaction("❤", user_id=2), bot, cfg))  # not the owner
    assert bot.calls == []


def test_reply_goes_under_the_post(cfg, store):
    store.add(750, "link", REEL, "")
    store.mark_sent(750, [100])
    msg = SimpleNamespace(chat=SimpleNamespace(id=OWNER), from_user=SimpleNamespace(id=OWNER),
                          message_id=300, reply_to_message=SimpleNamespace(message_id=100))
    assert watch._is_watch_reply(msg, cfg)
    assert not watch._is_watch_reply(
        SimpleNamespace(**{**vars(msg), "reply_to_message": SimpleNamespace(message_id=7)}), cfg)
    bot = FakeBot()
    asyncio.run(watch._on_reply(msg, bot, cfg))
    name, args, kwargs = bot.calls[0]
    assert name == "copy_message" and args == (CH, OWNER, 300)
    assert kwargs["reply_parameters"].message_id == 750
    assert bot.calls[1][0] == "set_message_reaction"


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


def test_batch_sends_in_order_and_maps_messages(cfg, store, monkeypatch, tmp_path):
    import downloader
    store.add(10, "link", REEL, "look")
    store.add(11, "link", "https://www.tiktok.com/@a/video/1", "")
    store.add(12, "media", None, "")
    sent_media, failures = [], []

    async def fake_download(p, config):
        if "tiktok" in p.url:
            raise downloader.DownloadError("This video is no longer available", user_side=True)
        return str(tmp_path / "w"), downloader.DownloadResult()

    async def fake_send_media(anchor, result, url, caption):
        assert anchor.chat.id == OWNER
        sent_media.append((url, caption))
        return [SimpleNamespace(message_id=500), SimpleNamespace(message_id=501)]

    async def fake_failure(message, config, url, error, cause=None, user_side=False):
        failures.append((message.from_user.id, url, error, user_side))

    monkeypatch.setattr(watch, "_download", fake_download)
    monkeypatch.setattr(watch, "_deps", SimpleNamespace(
        failure=fake_failure, send_media=fake_send_media, slot=None))
    bot = FakeBot()
    asyncio.run(watch._send_batch(bot, cfg, SimpleNamespace(id=OWNER)))

    assert sent_media == [(REEL, watch.caption(CH, 10, 1, 3, "look"))]
    assert failures == [(OWNER, "https://www.tiktok.com/@a/video/1",
                         "This video is no longer available", True)]
    names = [c[0] for c in bot.calls]
    assert names == ["send_message", "copy_message", "send_message"]  # ❌ item, media, Next
    assert bot.calls[0][1][1].startswith("❌ 2/3")
    assert bot.calls[1][1] == (OWNER, CH, 12)
    assert store.count_queued() == 0
    assert store.post_of(500) == store.post_of(501) == 10
    assert store.get("control") == bot.next_id
    assert watch._fetches == {}


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
