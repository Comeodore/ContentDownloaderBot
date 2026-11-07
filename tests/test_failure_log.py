"""The failure log line: its cause and the format the Grafana alert parses."""
import logging
import re
from types import SimpleNamespace

import yt_dlp

import bot
import downloader

# Same pattern as the regexp in the Grafana alert contentdl_download_failed
# (/opt/monitoring/grafana/provisioning/alerting/rules.yml). Keep them in sync.
ALERT_RE = re.compile(
    r"Download failed for (?P<user>\S+): (?P<url>\S+) — (?P<error>[^|]*[^| ])(?: \| cause: (?P<cause>.+))?")

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
        rec = _logged(caplog, bot._SOMETHING_WRONG, downloader.error_detail(e), exc_info=True)
    assert rec.levelno == logging.ERROR and rec.exc_info
    assert ALERT_RE.search(rec.getMessage()).group("cause") == "RuntimeError: kaput"


def test_percent_signs_are_kept(caplog):
    rec = _logged(caplog, "50% done %s")
    assert rec.getMessage().endswith("— 50% done %s")
