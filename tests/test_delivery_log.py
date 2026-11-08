"""The delivery timing line and the conversion time behind it."""
import logging
import re
from types import SimpleNamespace

import bot
import downloader

# The line as the Grafana dashboard parses it (the "Request time by site" and "Where the
# time goes" regexps in /opt/monitoring/grafana/dashboards/services/contentdl-bot.json).
# Keep them in sync.
SITE_RE = re.compile(r": https?://(?:[^/ ]*\.)?(?P<site>tiktok|instagram|threads|youtube|youtu)\.")
TIMES_RE = re.compile(
    r"— queue (?P<queue>[0-9.]+) s, fetch (?P<fetch>[0-9.]+) s, convert (?P<convert>[0-9.]+) s,"
    r" upload (?P<upload>[0-9.]+) s, total (?P<total>[0-9.]+) s$")

URL = "https://www.instagram.com/reel/abc/?a=1&b=2"


def test_delivery_line_matches_the_dashboard(caplog):
    msg = SimpleNamespace(from_user=SimpleNamespace(id=42, username="bob"))
    with caplog.at_level(logging.INFO, logger="videobot"):
        bot._log_delivery(msg, URL, queue=0.04, fetch=1.62, convert=0.0, upload=2.13, total=3.81)
    line = caplog.records[-1].getMessage()
    assert line.startswith("Delivered to 42 @bob: " + URL)
    assert SITE_RE.search(line)["site"] == "instagram"
    assert TIMES_RE.search(line).groupdict() == {
        "queue": "0.0", "fetch": "1.6", "convert": "0.0", "upload": "2.1", "total": "3.8"}


def test_download_counts_the_conversion_time(monkeypatch, config, tmp_path):
    video = tmp_path / "001-x.mp4"
    video.write_bytes(b"0" * 10)
    clock = [100.0]

    def convert(path):
        clock[0] += 2.5
        return path

    monkeypatch.setattr(downloader.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(downloader, "_resolve_share_link", lambda url: url)
    monkeypatch.setattr(downloader, "_extract_info", lambda opts, url: {"id": "x"})
    monkeypatch.setattr(downloader, "_collect_media", lambda work_dir: [str(video)])
    monkeypatch.setattr(downloader, "_ensure_telegram_playable", convert)
    monkeypatch.setattr(downloader, "_probe_video", lambda path: {})
    result = downloader._download_blocking(
        "https://www.tiktok.com/@u/video/1", config(), str(tmp_path))
    assert result.convert_seconds == 2.5
