"""yt-dlp error text → user message and flags (_as_download_error)."""
import pytest

import downloader as d


def err(raw, site=None):
    return d._as_download_error(Exception(raw), site)


def test_session_expired_names_the_sites_cookies():
    e = err("ERROR: [Instagram+videobot] 123: Instagram session expired: "
            "the API sent it to the login page", "instagram")
    assert str(e).startswith("Cookie problem: the saved instagram.com cookies have expired")
    assert e.auth


@pytest.mark.parametrize("raw", [
    "ERROR: [Instagram] x: Requested content is not available, rate-limit reached or login required",
    "ERROR: [instagram:story] 1: This content is unreachable. Use --cookies-from-browser "
    "or --cookies for the authentication. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ",
    "ERROR: [Instagram] x: This content isn't available to everyone: "
    "It can't be seen by certain audiences.",
    "ERROR: [Instagram] x: Instagram sent an empty media response.",
    "ERROR: [youtube] x: Sign in to confirm you're not a bot",
    "ERROR: [youtube] x: Private video. Sign in if you've been granted access",
    "ERROR: [TikTok] x: This video is private",
])
def test_auth_errors_are_cookie_problems(raw):
    e = err(raw, "instagram")
    assert e.auth
    assert str(e).startswith("Cookie problem: this needs a login, and the saved instagram.com cookies")


def test_cookie_message_without_a_known_site():
    assert "saved cookies for this site" in str(err("ERROR: login required"))


def test_session_expired_wins_over_plain_login():
    e = err("session expired, please log in", "threads")
    assert "have expired" in str(e) and "threads.com cookies" in str(e)


@pytest.mark.parametrize("raw, flag", [
    ("ERROR: [Threads] x: This post is unavailable — it may have been deleted, "
     "or it is hidden from logged-out visitors", "no_post"),
    ("ERROR: [Threads] x: Could not find post data", "no_post"),
    ("ERROR: [Instagram] x: HTTP Error 404: Not Found", "no_post"),
    ("ERROR: [Threads] x: This Threads post has no photo or video", "no_media"),
    ("ERROR: [TikTok] x: Unexpected response from webpage request", "waf"),
])
def test_flags(raw, flag):
    e = err(raw)
    assert getattr(e, flag)
    assert not e.auth
    for other in {"no_post", "no_media", "waf"} - {flag}:
        assert not getattr(e, other)


@pytest.mark.parametrize("raw, message", [
    # The quote message must win over the generic "is unavailable".
    ("The quoted post holding the media is unavailable — it may have been deleted, "
     "or it is not shown to logged-out visitors", "This post quotes another one"),
    ("This post is unavailable — it may have been deleted, or it is hidden from "
     "logged-out visitors", "The site didn't hand over this post"),
    ("This Threads post has no photo or video", "This post is text only"),
    ("Instagram story is gone: expired, deleted or hidden", "This story is no longer available"),
    ("This live event will begin in 5 hours", "This is a live stream"),
    ("Unsupported URL: https://x", "This link doesn't point to a video"),
    ("Your IP address is blocked from accessing this post", "blocking this server's IP"),
    ("This video is not available in your country", "isn't available in the country"),
    ("Video unavailable. This video has been removed by the uploader", "no longer available"),
    ("Requested format is not available", "No downloadable video or photo"),
    ("HTTP Error 403: Forbidden", "refused to hand over the file"),
    ("Read timed out", "didn't respond in time"),
])
def test_known_messages(raw, message):
    e = err(f"ERROR: [x] id: {raw}")
    assert message in str(e)
    assert not e.auth


def test_prefix_is_stripped_from_unknown_errors():
    assert str(err("ERROR: [youtube] abc123: Something new broke")) == \
        "Couldn't download it: Something new broke"


def test_unknown_error_is_capped():
    msg = str(err("ERROR: " + "x" * 500))
    assert msg == "Couldn't download it: " + "x" * 199 + "…"


def test_only_the_last_line_counts():
    raw = "Traceback (most recent call last):\n  File ...\nERROR: [x] y: Read timed out"
    assert str(err(raw)) == "The site didn't respond in time."


def test_waf_kept_on_unknown_errors():
    e = err("ERROR: [TikTok] 1: Unexpected response from webpage request")
    assert e.waf and str(e).startswith("Couldn't download it:")
