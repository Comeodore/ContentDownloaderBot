"""The failure log line: its cause and the format the Grafana alert parses."""
import logging
import re
from types import SimpleNamespace

import pytest
import yt_dlp

import bot
import downloader

# The line as the Grafana dashboard parses it (the "Users" and "Failure reasons" regexps in
# /opt/monitoring/grafana/dashboards/services/contentdl-bot.json). Keep them in sync.
ALERT_RE = re.compile(
    r"Download failed for (?P<user>\d+|\?)(?: @(?P<tag>\w+))?: (?P<url>\S+)"
    r" — (?P<error>[^|]*[^| ])(?: \| cause: (?P<cause>.+))?")

URL = "https://www.instagram.com/p/abc/?a=1&b=2"
MSG = SimpleNamespace(from_user=SimpleNamespace(id=42))


def _ytdlp_error(text):
    return yt_dlp.utils.DownloadError(text)


def _chained(outer, inner):
    try:
        try:
            raise inner
        except BaseException as e:
            raise outer from e
    except BaseException as e:
        return e


def test_detail_is_ytdlp_raw_text():
    raw = "ERROR: [Instagram] abc: Failed to parse JSON"
    err = _chained(downloader._as_download_error(_ytdlp_error(raw), "instagram"), _ytdlp_error(raw))
    assert downloader.error_detail(err) == "[Instagram] abc: Failed to parse JSON"


def test_detail_walks_wrapped_errors():
    raw = "ERROR: [Threads] x: Could not find post data"
    guest = _chained(downloader.DownloadError("guest"), _ytdlp_error(raw))
    final = _chained(downloader.DownloadError("final"), guest)
    assert downloader.error_detail(final) == "[Threads] x: Could not find post data"


def test_detail_of_own_error_without_cause_is_none():
    assert downloader.error_detail(downloader.DownloadError("over the limit")) is None


def test_detail_of_other_exception():
    assert downloader.error_detail(ValueError("bad\nlast line")) == "ValueError: last line"


def _logged(caplog, *args, **kwargs):
    with caplog.at_level(logging.INFO, logger="videobot"):
        bot._log_failure(MSG, URL, *args, **kwargs)
    return caplog.records[-1]


def test_line_with_cause_parses(caplog):
    rec = _logged(caplog, "Couldn't download it: boom.", "[Instagram] abc: Failed\n  to parse JSON")
    m = ALERT_RE.search(rec.getMessage())
    assert m.group("user") == "42"
    assert m.group("url") == URL
    assert m.group("error") == "Couldn't download it: boom."
    assert m.group("cause") == "[Instagram] abc: Failed to parse JSON"  # one line
    assert rec.levelno == logging.INFO


def test_line_without_cause_parses(caplog):
    rec = _logged(caplog, "This video is no longer available — deleted or private.")
    m = ALERT_RE.search(rec.getMessage())
    assert m.group("error") == "This video is no longer available — deleted or private."
    assert m.group("cause") is None


def test_cause_already_in_message_is_not_repeated(caplog):
    rec = _logged(caplog, "Couldn't download it: [X] y: boom", "[X] y: boom")
    assert "| cause:" not in rec.getMessage()


def test_unexpected_failure_is_an_error_with_traceback(caplog):
    try:
        raise RuntimeError("kaput")
    except RuntimeError as e:
        rec = _logged(caplog, "Unexpected error", downloader.error_detail(e), exc_info=True)
    assert rec.levelno == logging.ERROR and rec.exc_info
    assert ALERT_RE.search(rec.getMessage()).group("cause") == "RuntimeError: kaput"


def test_percent_signs_are_kept(caplog):
    rec = _logged(caplog, "50% done %s")
    assert rec.getMessage().endswith("— 50% done %s")


# What the user sees: the reason when it's theirs to deal with, else the standard reply.
@pytest.mark.parametrize("raw, user_side", [
    ("ERROR: [youtube] x: Video unavailable. This video has been removed", True),
    ("ERROR: [generic] Unsupported URL: https://example.com", True),
    ("ERROR: [Threads] x: This Threads post has no photo or video", True),
    ("ERROR: [instagram:story+videobot] 1: Instagram story is gone: expired", True),
    ("ERROR: [youtube] x: This live event will begin in 3 hours", True),
    ("ERROR: [youtube] x: The uploader has not made this video available in your country", True),
    ("ERROR: [Instagram] x: Failed to parse JSON", False),
    ("ERROR: [Instagram] x: Instagram session expired: the API sent it to the login page", False),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", False),
    ("ERROR: [TikTok] x: Your IP address is blocked from accessing this post", False),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", False),
    ("ERROR: [Instagram] x: Read timed out", False),
])
def test_user_side_errors(raw, user_side):
    err = downloader._as_download_error(_ytdlp_error(raw), "youtube")
    assert err.user_side is user_side
    assert bot._user_text(err) == (str(err) if user_side else bot._STANDARD_ERROR)


def test_standard_reply_says_the_developer_knows():
    assert "developer has already been notified" in bot._STANDARD_ERROR
    assert bot._user_text(downloader.DownloadError("Cookie problem: x")) == bot._STANDARD_ERROR


def test_username_follows_the_id(caplog):
    msg = SimpleNamespace(from_user=SimpleNamespace(id=42, username="bob_1"))
    with caplog.at_level(logging.INFO, logger="videobot"):
        bot._log_failure(msg, URL, "boom")
    m = ALERT_RE.search(caplog.records[-1].getMessage())
    assert (m.group("user"), m.group("tag"), m.group("url")) == ("42", "bob_1", URL)


def test_who_without_username_or_user():
    assert bot._who(SimpleNamespace(from_user=SimpleNamespace(id=42, username=None))) == "42"
    assert bot._who(SimpleNamespace(from_user=None)) == "?"
