"""Pure helpers in bot.py: timecodes, captions, cookie sites."""
import pytest

import bot
import downloader

URL = "https://youtu.be/dQw4w9WgXcQ"


@pytest.mark.parametrize("text, ranges, rejected", [
    (f"{URL} 1:00-1:10, 2:10-2:55", [(60, 70), (130, 175)], []),
    (f"{URL} 5-30", [(5, 30)], []),
    (f"{URL} 1:02:03-1:02:30", [(3723, 3750)], []),
    (f"{URL} 1:00 – 1:10", [(60, 70)], []),
    (f"{URL} 1:00—1:10", [(60, 70)], []),
    (f"{URL} 1:10-1:00 2:00-2:00", [], ["1:10-1:00", "2:00-2:00"]),
    (f"{URL} 0:10-0:20 1:10-1:00", [(10, 20)], ["1:10-1:00"]),
    # Years aren't a clip.
    (f"{URL} the 2024-2025 season", [], []),
    (URL, [], []),
])
def test_parse_ranges(text, ranges, rejected):
    assert bot._parse_ranges(text, URL) == (ranges, rejected)


def test_parse_ranges_ignores_digits_in_the_url():
    url = "https://www.youtube.com/watch?v=a1-23bcdefg"
    assert bot._parse_ranges(url, url) == ([], [])


@pytest.mark.parametrize("text, expected", [
    ("cut 1:00 to 2", True),
    ("10:15", True),
    ("hello", False),
    ("", False),
    (None, False),
])
def test_looks_like_timecodes(text, expected):
    assert bot._looks_like_timecodes(text) is expected


@pytest.mark.parametrize("ts, sec", [("5", 5), ("1:05", 65), ("10:00", 600), ("1:02:03", 3723)])
def test_timestamps_round_trip(ts, sec):
    assert bot._ts_to_sec(ts) == sec


@pytest.mark.parametrize("sec, ts", [(0, "0:00"), (59, "0:59"), (600, "10:00"), (3723, "1:02:03")])
def test_fmt_ts(sec, ts):
    assert bot._fmt_ts(sec) == ts


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def test_truncate_keeps_short_text():
    text = "a" * (bot.CAPTION_LIMIT - 1)
    assert bot._truncate_utf16(text) == text


@pytest.mark.parametrize("text", ["a" * 5000, "😀" * 600, "a😀" * 400])
def test_truncate_fits_the_utf16_limit(text):
    out = bot._truncate_utf16(text)
    assert out.endswith("…")
    assert utf16_len(out) <= bot.CAPTION_LIMIT


def result(description="", title="video"):
    return downloader.DownloadResult(description=description, title=title)


def test_caption_prefers_the_description_and_escapes_html():
    caption = bot._caption_for(result("  <b>hi</b> & bye  ", "Title"))
    assert caption == "&lt;b&gt;hi&lt;/b&gt; &amp; bye"


def test_caption_falls_back_to_the_title():
    assert bot._caption_for(result("", "My title")) == "My title"


def test_no_caption_for_the_default_title():
    assert bot._caption_for(result("", "video")) is None
    assert bot._caption_for(result("   ", "")) is None


def test_long_captions_are_cut():
    caption = bot._caption_for(result("x" * 3000))
    assert caption.endswith("…") and utf16_len(caption) <= bot.CAPTION_LIMIT


class _FakeChat:
    """A Message stand-in that records what's sent into its chat."""

    def __init__(self):
        self.sent = []

    def __getattr__(self, name):
        async def answer(**kwargs):
            if name == "answer_media_group":
                msgs = [f"{name}#{i}" for i in range(len(kwargs["media"]))]
            else:
                msgs = f"{name}:{kwargs.get('caption')}"
            self.sent.append(msgs)
            return msgs
        return answer


def _item(path, **kw):
    return downloader.MediaItem(path=path, is_video=kw.pop("is_video", True), **kw)


def test_send_media_returns_every_message_and_takes_a_caption():
    import asyncio
    result = downloader.DownloadResult(
        items=[_item("a.mp4"), _item("b.jpg", is_video=False),
               _item("m.mp3", is_video=False, is_audio=True)],
        description="threads text")
    chat = _FakeChat()
    sent = asyncio.run(bot._send_media(chat, result, "https://www.instagram.com/p/x/", "1/5"))
    assert sent == ["answer_media_group#0", "answer_media_group#1", "answer_audio:None"]

    single = downloader.DownloadResult(items=[_item("a.mp4")], description="threads text")
    assert asyncio.run(bot._send_media(_FakeChat(), single, "https://www.threads.com/@a/post/x")) \
        == ["answer_video:threads text"]
    assert asyncio.run(bot._send_media(_FakeChat(), single, "https://www.instagram.com/p/x/")) \
        == ["answer_video:None"]
