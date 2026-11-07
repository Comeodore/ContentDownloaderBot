"""The Send to Claude queue and the failure report the button sits under."""
import asyncio
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


@pytest.fixture
def fresh_limits(monkeypatch):
    monkeypatch.setattr(bot, "_reported", {})
    monkeypatch.setattr(bot, "_report_times", [])
    monkeypatch.setattr(bot, "_auto_sent", {})
    monkeypatch.setattr(bot, "_auto_times", [])


def test_same_failure_is_reported_once_per_window(fresh_limits):
    assert bot._should_report("u", "e", 1000.0)
    assert not bot._should_report("u", "e", 1000.0 + 599)
    assert bot._should_report("u", "other", 1000.0 + 599)
    assert bot._should_report("u", "e", 1000.0 + 601)


def test_reports_per_hour_are_capped(fresh_limits):
    sent = [bot._should_report(f"u{i}", "e", 10.0 + i) for i in range(25)]
    assert sent.count(True) == bot._REPORTS_PER_HOUR
    assert bot._should_report("late", "e", 10.0 + 3601)


def test_report_escapes_and_names_the_user():
    msg = SimpleNamespace(from_user=SimpleNamespace(id=5, username="bob"))
    text = bot._failure_report(msg, "https://x.com/?a=1&b=<2>", "Couldn't <do> it",
                               "[X] y: bad &\n  thing")
    assert "5 (@bob)" in text
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


def test_problem_key_ignores_ids_and_links():
    other = bot._problem_key("https://www.instagram.com/p/Zz9/", JSON_ERR.replace("DfR0xxWuoFm", "Zz9"))
    assert bot._problem_key(IG, JSON_ERR) == other
    assert bot._problem_key(IG, JSON_ERR) != bot._problem_key("https://youtu.be/abcdefghijk", JSON_ERR)


def test_same_problem_auto_sent_once_per_window(fresh_limits):
    assert bot._should_auto_send(IG, JSON_ERR, 0.0)
    assert not bot._should_auto_send(IG, JSON_ERR.replace("DfR0xxWuoFm", "Zz9"), 60.0)
    assert bot._should_auto_send(IG, "Couldn't download it: something else", 60.0)
    assert bot._should_auto_send(IG, JSON_ERR, bot._AUTO_SAME_PROBLEM_AFTER + 1)


def test_auto_sessions_per_day_are_capped(fresh_limits):
    sent = [bot._should_auto_send(IG, f"error kind {chr(97 + i)}", 10.0 + i) for i in range(15)]
    assert sent.count(True) == bot._AUTO_PER_DAY


def _user_message():
    sent = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=5, text="❌ Download failed …")
    tg = SimpleNamespace(send_message=AsyncMock(return_value=sent))
    return SimpleNamespace(from_user=SimpleNamespace(id=42, username=None), bot=tg)


def test_technical_failure_goes_to_claude_without_a_button(config, fresh_limits):
    cfg, msg = config(), _user_message()
    asyncio.run(bot._failure(msg, cfg, IG, JSON_ERR, "raw cause"))
    args, kwargs = msg.bot.send_message.await_args
    assert args[0] == cfg.owner_id and "Sent to Claude automatically" in args[1]
    assert kwargs["reply_markup"] is None
    [req] = inbox.pending(cfg)
    assert (req["chat_id"], req["message_id"]) == (1, 5)


def test_user_side_failure_is_only_reported(config, fresh_limits):
    cfg, msg = config(), _user_message()
    asyncio.run(bot._failure(msg, cfg, IG, "This video is no longer available — deleted or private.",
                             user_side=True))
    args, kwargs = msg.bot.send_message.await_args
    assert "user's side" in args[1] and kwargs["reply_markup"] is not None
    assert inbox.pending(cfg) == []


def test_over_the_limit_the_report_keeps_the_button(config, fresh_limits, monkeypatch):
    monkeypatch.setattr(bot, "_AUTO_PER_DAY", 0)
    cfg, msg = config(), _user_message()
    asyncio.run(bot._failure(msg, cfg, IG, JSON_ERR))
    args, kwargs = msg.bot.send_message.await_args
    assert "limit reached" in args[1] and kwargs["reply_markup"] is not None
    assert inbox.pending(cfg) == []
