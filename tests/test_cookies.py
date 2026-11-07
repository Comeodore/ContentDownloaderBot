"""cookies.txt handling: session detection, per-site merge, one-off copies."""
import os
import stat

import pytest

import downloader as d
from conftest import HEADER, cookie


@pytest.mark.parametrize("records, site, expected", [
    ([cookie(".instagram.com", "sessionid")], "instagram", True),
    ([cookie(".instagram.com", "csrftoken"), cookie(".instagram.com", "ds_user_id")],
     "instagram", False),
    ([cookie("#HttpOnly_.www.threads.com", "sessionid")], "threads", True),
    ([cookie("i.instagram.com", "sessionid")], "instagram", True),
    # An Instagram session isn't a Threads one.
    ([cookie(".instagram.com", "sessionid")], "threads", False),
    # Other sites: any cookie counts.
    ([cookie(".youtube.com", "VISITOR_INFO1_LIVE")], "youtube", True),
    ([cookie(".tiktok.com", "ttwid")], "tiktok", True),
    ([cookie(".youtube.com", "SID")], "tiktok", False),
    ([cookie(".notinstagram.com", "sessionid")], "instagram", False),
])
def test_has_session(cookie_file, records, site, expected):
    assert d._has_session(cookie_file(*records), site) is expected


def test_has_session_without_file_or_site(cookie_file, tmp_path):
    path = cookie_file(cookie(".instagram.com", "sessionid"))
    assert not d._has_session(None, "instagram")
    assert not d._has_session(path, None)
    assert not d._has_session(path, "example")
    assert not d._has_session(str(tmp_path / "missing.txt"), "instagram")


def records(path):
    """(domain, name, value) of every record in a cookies file."""
    out = []
    for line in open(path, encoding="utf-8"):
        parts = line.rstrip("\n").split("\t")
        if len(parts) == 7:
            out.append((parts[0], parts[5], parts[6]))
    return out


def test_merge_replaces_only_uploaded_sites(cookie_file):
    target = cookie_file(
        cookie(".tiktok.com", "sid_tt", "keep"),
        cookie(".youtube.com", "SID", "keep"),
        cookie(".instagram.com", "sessionid", "old"),
        cookie("#HttpOnly_i.instagram.com", "rur", "old"),
        cookie(".instagram.com", "csrftoken", "old"),
        name="target.txt",
    )
    upload = cookie_file(
        cookie(".instagram.com", "sessionid", "new"),
        cookie("www.instagram.com", "csrftoken", "new"),
        name="upload.txt",
    )
    assert d.merge_cookies_file(upload, target) == ["instagram.com"]
    got = records(target)
    assert (".tiktok.com", "sid_tt", "keep") in got
    assert (".youtube.com", "SID", "keep") in got
    assert not [r for r in got if r[2] == "old"]  # subdomain records go too
    assert sorted(r[2] for r in got if "instagram" in r[0]) == ["new", "new"]
    assert open(target, encoding="utf-8").readline() == HEADER


def test_merge_several_sites(cookie_file):
    target = cookie_file(cookie(".tiktok.com", "sid_tt"), name="target.txt")
    upload = cookie_file(cookie(".threads.com", "sessionid"), cookie(".youtube.com", "SID"),
                         name="upload.txt")
    assert d.merge_cookies_file(upload, target) == ["threads.com", "youtube.com"]
    assert len(records(target)) == 3


@pytest.mark.parametrize("content", [
    "",
    '[{"domain": ".instagram.com", "name": "sessionid", "value": "x"}]',
    "# Netscape HTTP Cookie File\n# only comments\n",
])
def test_merge_ignores_uploads_without_records(cookie_file, tmp_path, content):
    target = cookie_file(cookie(".tiktok.com", "sid_tt"), name="target.txt")
    before = open(target, "rb").read()
    upload = tmp_path / "upload.txt"
    upload.write_text(content, encoding="utf-8")
    assert d.merge_cookies_file(str(upload), target) == []
    assert open(target, "rb").read() == before


def test_merge_writes_in_place(cookie_file):
    target = cookie_file(cookie(".tiktok.com", "sid_tt"), name="target.txt")
    inode = os.stat(target).st_ino
    d.merge_cookies_file(cookie_file(cookie(".instagram.com", "sessionid"), name="up.txt"), target)
    assert os.stat(target).st_ino == inode


def test_merge_creates_a_missing_target(cookie_file, tmp_path):
    target = str(tmp_path / "new.txt")
    assert d.merge_cookies_file(cookie_file(cookie(".instagram.com", "sessionid")), target) == \
        ["instagram.com"]
    assert records(target) == [(".instagram.com", "sessionid", "v")]


def test_cookies_for_run_is_a_private_copy(cookie_file, config, tmp_path):
    src = cookie_file(cookie(".instagram.com", "sessionid"))
    work = tmp_path / "work"
    work.mkdir()
    copy = d._cookies_for_run(config(src), str(work))
    assert copy == str(work / "cookies.txt") and copy != src
    assert open(copy).read() == open(src).read()
    assert stat.S_IMODE(os.stat(copy).st_mode) == 0o600
    # yt-dlp writes the jar back into the copy; the original must not change.
    with open(copy, "a") as f:
        f.write(cookie(".instagram.com", "extra"))
    assert "extra" not in open(src).read()


def test_cookies_for_run_without_a_file(config, tmp_path):
    assert d._cookies_for_run(config(None), str(tmp_path)) is None
    assert d._cookies_for_run(config(str(tmp_path / "missing.txt")), str(tmp_path)) is None
