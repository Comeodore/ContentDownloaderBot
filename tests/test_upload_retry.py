"""Uploads Telegram fails on its own side are sent again, and the user is told."""
import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramServerError
from aiogram.methods import SendVideo

import bot
import downloader

URL = "https://www.tiktok.com/@u/video/1"


def glitch():
    # The error as it came on 2026-11-11 through the local Bot API server.
    return TelegramBadRequest(method=SendVideo(chat_id=1, video="x"),
                              message="Bad Request: internal Server Error during file upload")


def bad_request(text="Bad Request: file must be non-empty"):
    return TelegramBadRequest(method=SendVideo(chat_id=1, video="x"), message=text)


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr(bot, "UPLOAD_RETRY_DELAYS", (0, 0, 0))


def failing(*errors, result="sent"):
    """A send that raises the given errors in turn, then succeeds."""
    calls = []

    async def send():
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return result
    return send, calls


@pytest.mark.parametrize("e, expected", [
    (glitch(), True),
    (TelegramServerError(method=SendVideo(chat_id=1, video="x"),
                         message="Internal Server Error"), True),
    (bad_request(), False),
    (bad_request("Bad Request: chat not found"), False),
    (RuntimeError("internal server error"), False),
])
def test_is_telegram_glitch(e, expected):
    assert bot._is_telegram_glitch(e) is expected


def test_a_glitch_is_retried_and_the_user_told():
    send, calls = failing(glitch(), glitch())
    notices = []

    async def on_glitch():
        notices.append(1)

    assert asyncio.run(bot._upload(send, on_glitch)) == "sent"
    assert len(calls) == 3 and len(notices) == 2


def test_other_errors_are_not_retried():
    send, calls = failing(bad_request())
    with pytest.raises(TelegramBadRequest):
        asyncio.run(bot._upload(send))
    assert len(calls) == 1


def test_gives_up_after_the_last_retry():
    send, calls = failing(*[glitch()] * 10)
    with pytest.raises(TelegramBadRequest) as e:
        asyncio.run(bot._upload(send))
    assert bot._is_telegram_glitch(e.value)
    assert len(calls) == len(bot.UPLOAD_RETRY_DELAYS) + 1


class FakeStatus:
    def __init__(self, text):
        self.texts = [text]
        self.deleted = False

    async def edit_text(self, text, **kw):
        self.texts.append(text)

    async def delete(self):
        self.deleted = True


class FakeMessage:
    """A user's message: answer() gives the status; each send takes the next of
    `errors` and raises it (None: goes through), then all go through."""

    def __init__(self, *errors):
        self.from_user = SimpleNamespace(id=42, username="bob")
        self.errors = list(errors)
        self.sends = []
        self.answers = []
        self.status = None

    async def answer(self, text, **kw):
        if self.status is None:
            self.status = FakeStatus(text)
            return self.status
        self.answers.append(text)

    def __getattr__(self, name):
        if not name.startswith("answer_"):
            raise AttributeError(name)

        async def send(**kw):
            self.sends.append(name)
            if self.errors and (e := self.errors.pop(0)):
                raise e
            if name == "answer_media_group":
                return [f"{name}#{i}" for i in range(len(kw["media"]))]
            return name
        return send


def item(path, **kw):
    return downloader.MediaItem(path=path, is_video=kw.pop("is_video", True), **kw)


def test_a_failed_send_in_a_post_is_repeated_alone():
    """The album already sent isn't sent twice when the music after it fails."""
    result = downloader.DownloadResult(items=[
        item("a.jpg", is_video=False), item("b.jpg", is_video=False),
        item("m.mp3", is_video=False, is_audio=True)])
    msg = FakeMessage(None, glitch())  # the album goes through, the music fails once
    sent = asyncio.run(bot._send_media(msg, result, URL))
    assert msg.sends == ["answer_media_group", "answer_audio", "answer_audio"]
    assert sent == ["answer_media_group#0", "answer_media_group#1", "answer_audio"]


def run_full(monkeypatch, config, msg, items):
    failures = []

    async def download(url, cfg, work_dir):
        return downloader.DownloadResult(items=items, total_size=3_600_000)

    async def failure(message, cfg, url, error, cause=None, **kw):
        failures.append((error, cause))

    monkeypatch.setattr(downloader, "download", download)
    monkeypatch.setattr(bot, "_failure", failure)
    asyncio.run(bot._handle_full(msg, config(), URL))
    return failures


def test_the_status_says_telegram_is_slow_and_the_video_arrives(monkeypatch, config):
    msg = FakeMessage(glitch(), glitch())
    failures = run_full(monkeypatch, config, msg, [item("v.mp4")])
    assert msg.sends == ["answer_video"] * 3
    assert msg.status.texts[0].startswith("Downloading")
    slow = [t for t in msg.status.texts if t.startswith(bot._TELEGRAM_SLOW)]
    assert len(slow) == 1  # told once, not on every retry
    assert msg.status.deleted and not failures


def test_the_user_is_told_it_is_telegram_when_retries_run_out(monkeypatch, config):
    msg = FakeMessage(*[glitch()] * 10)
    failures = run_full(monkeypatch, config, msg, [item("v.mp4")])
    assert len(msg.sends) == len(bot.UPLOAD_RETRY_DELAYS) + 1
    assert msg.status.texts[-1] == "❌ " + bot._TELEGRAM_DOWN
    assert not msg.status.deleted
    assert [e for e, _ in failures] == ["Telegram didn't accept the file"]


def test_other_send_errors_keep_the_standard_reply(monkeypatch, config):
    msg = FakeMessage(bad_request())
    failures = run_full(monkeypatch, config, msg, [item("v.mp4")])
    assert msg.sends == ["answer_video"]
    assert msg.status.texts == [msg.status.texts[0], "❌ " + bot._STANDARD_ERROR]
    assert len(failures) == 1


def test_clips_are_retried_too(monkeypatch, config):
    async def download_clips(url, cfg, work_dir, ranges):
        return downloader.ClipsResult(duration=60, clips={
            0: item("c0.mp4", duration=5), 1: item("c1.mp4", duration=5)})

    failures = []

    async def failure(message, cfg, url, error, cause=None, **kw):
        failures.append(error)

    monkeypatch.setattr(downloader, "download_clips", download_clips)
    monkeypatch.setattr(bot, "_failure", failure)
    # Clip 1 goes through on the second try; clip 2 never does.
    msg = FakeMessage(glitch(), None, *[glitch()] * 10)
    asyncio.run(bot._handle_cut(msg, config(), URL, [(0, 5), (10, 15)]))
    assert msg.sends.count("answer_video") == 1 + 1 + len(bot.UPLOAD_RETRY_DELAYS) + 1
    assert msg.answers == ["❌ 0:10–0:15: " + bot._TELEGRAM_DOWN]
    assert failures == ["0:10–0:15: Telegram didn't accept this clip"]
    assert any(t.startswith(bot._TELEGRAM_SLOW) for t in msg.status.texts)
    assert msg.status.deleted  # one clip was sent
