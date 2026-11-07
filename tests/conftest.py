"""Shared fixtures: Netscape cookie files and a Config."""
import pytest

from config import Config

HEADER = "# Netscape HTTP Cookie File\n"


def cookie(domain, name, value="v", path="/"):
    """One Netscape-format record."""
    return f"{domain}\tTRUE\t{path}\tTRUE\t2000000000\t{name}\t{value}\n"


@pytest.fixture
def cookie_file(tmp_path):
    """Writes the given records into a cookies.txt and returns its path."""
    def make(*records, name="cookies.txt"):
        path = tmp_path / name
        path.write_text(HEADER + "".join(records), encoding="utf-8")
        return str(path)
    return make


@pytest.fixture
def config(tmp_path):
    def make(cookies_file=None):
        return Config(
            bot_token="0:test", max_file_size_mb=49, cookies_file=cookies_file,
            telegram_api_base=None, auto_update_hours=0,
            download_dir=str(tmp_path / "downloads"), owner_id=1,
        )
    return make
