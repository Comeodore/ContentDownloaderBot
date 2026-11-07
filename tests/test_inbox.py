"""Failure reports to the owner and the queue they go to for Claude."""
import asyncio
import dataclasses
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

import bot
import inbox


def test_queue_lifecycle(config):
    cfg = config()
    rid = inbox.add(cfg, chat_id=1, message_id=77, report="❌ Download failed\nLink: x")
    assert rid.endswith("-77")
    assert os.path.dirname(inbox.inbox_dir(cfg)) == os.path.dirname(cfg.download_dir)
    assert [r["id"] for r in inbox.pending(cfg)] == [rid]
    assert inbox.load(cfg, rid)["report"].startswith("❌ Download failed")
    inbox.move(cfg, rid, "taken")
    assert inbox.pending(cfg) == []
    assert inbox.load(cfg, rid)["message_id"] == 77
    inbox.move(cfg, rid, "done")
    assert os.path.isfile(os.path.join(inbox.inbox_dir(cfg), "done", rid + ".json"))
    with pytest.raises(FileNotFoundError):
        inbox.move(cfg, "nope", "done")


def test_no_partial_files_in_the_inbox(config):
    cfg = config()
    inbox.add(cfg, 1, 2, "r")
    assert not [f for f in os.listdir(inbox.inbox_dir(cfg)) if f.endswith(".tmp")]


def test_user_ref_links_the_name_and_shows_tag_and_id():
    user = SimpleNamespace(id=5, first_name="Bob <B>", last_name="Smith", username="bob")
    assert bot._user_ref(user) == '<a href="tg://user?id=5">Bob &lt;B&gt; Smith</a> @bob (5)'
    assert bot._user_ref(SimpleNamespace(id=7, first_name=None, username=None)) == \
        '<a href="tg://user?id=7">7</a> (7)'
    assert bot._user_ref(SimpleNamespace(id="TEST", first_name="T", username=None)) == "T (TEST)"
    assert bot._user_ref(None) == "?"


def test_report_escapes_everything():
    msg = SimpleNamespace(from_user=SimpleNamespace(id=5, first_name="Bob", username="bob"))
    text = bot._failure_report(msg, "https://x.com/?a=1&b=<2>", "Couldn't <do> it",
                               "[X] y: bad &\n  thing")
    assert '<b>User:</b> <a href="tg://user?id=5">Bob</a> @bob (5)' in text
    assert "a=1&amp;b=&lt;2&gt;" in text and "Couldn't &lt;do&gt; it" in text
    assert "<code>[X] y: bad &amp; thing</code>" in text


def test_report_skips_a_cause_already_in_the_error():
    msg = SimpleNamespace(from_user=None)
    text = bot._failure_report(msg, "u", "Couldn't download it: boom", "boom")
    assert "Cause" not in text and "<b>User:</b> ?" in text


def _callback(user_id, text="❌ Download failed"):
    msg = MagicMock(spec=Message)
    msg.text, msg.html_text = text, text
    msg.chat, msg.message_id = SimpleNamespace(id=1), 99
    msg.edit_text = AsyncMock()
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id), message=msg,
                           answer=AsyncMock())


def test_button_queues_the_report_for_the_owner(config):
    cfg = config()  # owner_id=1
    cb = _callback(user_id=1)
    asyncio.run(bot.handle_send_to_claude(cb, cfg))
    [req] = inbox.pending(cfg)
    assert req["message_id"] == 99 and req["report"] == "❌ Download failed"
    cb.message.edit_text.assert_awaited_once()
    assert "Sent to Claude" in cb.message.edit_text.await_args.args[0]


def test_button_ignores_everyone_else(config):
    cfg = config()
    cb = _callback(user_id=2)
    asyncio.run(bot.handle_send_to_claude(cb, cfg))
    assert inbox.pending(cfg) == []
    cb.message.edit_text.assert_not_awaited()


IG = "https://www.instagram.com/p/DfR0xxWuoFm/"
JSON_ERR = "Couldn't download it: [Instagram] DfR0xxWuoFm: Failed to parse JSON"


def _user_message():
    """A user's message whose bot returns a new Telegram message id for every report."""
    sent = []

    async def send_message(*_args, **_kwargs):
        sent.append(SimpleNamespace(chat=SimpleNamespace(id=1), message_id=5 + len(sent),
                                    text="❌ Download failed …", edit_reply_markup=AsyncMock()))
        return sent[-1]
    tg = SimpleNamespace(send_message=AsyncMock(side_effect=send_message), sent=sent)
    return SimpleNamespace(from_user=SimpleNamespace(id=42, first_name="Ann", username=None),
                           bot=tg)


def test_every_failure_is_reported_and_queued(config):
    cfg, msg = config(), _user_message()
    asyncio.run(bot._failure(msg, cfg, IG, JSON_ERR, "raw cause"))
    asyncio.run(bot._failure(msg, cfg, IG, JSON_ERR, "raw cause"))  # the same again: no limits
    assert msg.bot.send_message.await_count == 2
    args, kwargs = msg.bot.send_message.await_args
    assert args[0] == cfg.owner_id and "Sent to Claude automatically" in args[1]
    assert "reply_markup" not in kwargs
    assert len(inbox.pending(cfg)) == 2


def test_user_side_failure_goes_to_claude_too(config):
    cfg, msg = config(), _user_message()
    asyncio.run(bot._failure(msg, cfg, IG, "This video is no longer available — deleted or private.",
                             user_side=True))
    assert "looks like the user's side" in msg.bot.send_message.await_args.args[1]
    assert len(inbox.pending(cfg)) == 1


def test_unqueued_report_gets_the_button(config, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(inbox, "add", broken)
    cfg, msg = config(), _user_message()
    asyncio.run(bot._failure(msg, cfg, IG, JSON_ERR))
    msg.bot.sent[-1].edit_reply_markup.assert_awaited_once()


def test_no_owner_no_report(config):
    cfg, msg = dataclasses.replace(config(), owner_id=None), _user_message()
    asyncio.run(bot._failure(msg, cfg, IG, JSON_ERR))
    msg.bot.send_message.assert_not_awaited()
    assert inbox.pending(cfg) == []
