"""yt-dlp error text → user message and flags (_as_download_error)."""
import pytest

import downloader as d


def err(raw, site=None):
    return d._as_download_error(Exception(raw), site)


def test_session_expired_names_the_sites_cookies():
    e = err("ERROR: [x] 123: session expired: the API sent it to the login page", "tiktok")
    assert str(e).startswith("Cookie problem: the saved tiktok.com cookies have expired")
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
    e = err(raw, "youtube")
    assert e.auth and not e.guest_wall
    assert str(e).startswith("Cookie problem: this needs a login, and the saved youtube.com cookies")


@pytest.mark.parametrize("site", ["instagram", "threads"])
@pytest.mark.parametrize("raw", [
    "ERROR: [Instagram] x: Requested content is not available, rate-limit reached or login required",
    "ERROR: [Instagram] x: This content isn't available to everyone: "
    "It can't be seen by certain audiences.",
    "ERROR: [Instagram] x: Instagram sent an empty media response.",
    "ERROR: [Instagram] x: Instagram session expired: the API sent it to the login page",
    "ERROR: [Threads] x: This post is unavailable — it may have been deleted, "
    "or it is hidden from logged-out visitors",
])
def test_guest_only_sites_name_no_cookies(raw, site):
    e = err(raw, site)
    assert e.guest_wall and e.user_side and not e.auth
    assert "cookie" not in str(e).lower()
    assert str(e).endswith("I download without an account, so this one is out of my reach.")


def test_cookie_message_without_a_known_site():
    assert "saved cookies for this site" in str(err("ERROR: login required"))


def test_session_expired_wins_over_plain_login():
    e = err("session expired, please log in", "tiktok")
    assert "have expired" in str(e) and "tiktok.com cookies" in str(e)


@pytest.mark.parametrize("raw, waf", [
    ("ERROR: [TikTok] x: Unexpected response from webpage request", True),
    ("ERROR: [TikTok] x: HTTP Error 404: Not Found", False),
])
def test_waf_flag(raw, waf):
    e = err(raw, "tiktok")
    assert e.waf is waf
    assert not e.auth and not e.guest_wall


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
