"""Guest-first / session-fallback logic in _extract_info, with yt-dlp stubbed out."""
import pytest

import downloader as d
from conftest import cookie

IG = "https://www.instagram.com/p/abc/"
THREADS = "https://www.threads.com/@u/post/abc"
YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

LOGIN = "ERROR: [x] abc: This content isn't available to everyone"
HIDDEN = "ERROR: [Threads] abc: This post is unavailable — it may have been deleted, " \
         "or it is hidden from logged-out visitors"
NO_MEDIA = "ERROR: [Threads] abc: This Threads post has no photo or video"
REMOVED = "ERROR: [x] abc: Video unavailable. This video has been removed"


@pytest.fixture
def attempts(monkeypatch):
    """Stubs _extract_info_attempts: each call takes the next outcome.

    An outcome is an info dict to return or a raw yt-dlp error to raise.
    Records the cookiefile of every call.
    """
    calls = []

    def setup(*outcomes):
        queue = list(outcomes)

        def fake(opts, url):
            calls.append(opts.get("cookiefile"))
            outcome = queue.pop(0)
            if isinstance(outcome, dict):
                return outcome
            raise d._as_download_error(Exception(outcome), d.site_of(url))

        monkeypatch.setattr(d, "_extract_info_attempts", fake)
        return calls
    return setup


@pytest.fixture
def ig_session(cookie_file):
    return cookie_file(cookie(".instagram.com", "sessionid"), cookie(".threads.com", "sessionid"))


@pytest.fixture
def no_session(cookie_file):
    return cookie_file(cookie(".instagram.com", "csrftoken"), cookie(".tiktok.com", "sid_tt"))


def test_guest_success_skips_the_session(attempts, ig_session):
    calls = attempts({"id": "ok"})
    assert d._extract_info({"cookiefile": ig_session}, IG) == {"id": "ok"}
    assert calls == [None]


def test_guest_login_error_without_session(attempts, no_session):
    calls = attempts(LOGIN)
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": no_session}, IG)
    assert str(e.value).startswith("Cookie problem: this needs a login (a story")
    assert "the bot has no instagram.com cookies" in str(e.value)
    assert not e.value.auth  # nothing for the owner to renew
    assert calls == [None]


def test_guest_hidden_post_without_session(attempts, no_session):
    calls = attempts(HIDDEN)
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": no_session}, THREADS)
    assert "it's a cookie problem" in str(e.value)
    assert "the bot has no threads.com cookies" in str(e.value)
    assert e.value.no_post and not e.value.auth
    assert calls == [None]


def test_guest_text_only_without_session_is_kept(attempts, no_session):
    attempts(NO_MEDIA)
    with pytest.raises(d.DownloadError, match="This post is text only"):
        d._extract_info({"cookiefile": no_session}, THREADS)


def test_guest_login_error_falls_back_to_the_session(attempts, ig_session):
    calls = attempts(LOGIN, {"id": "via session"})
    assert d._extract_info({"cookiefile": ig_session}, IG) == {"id": "via session"}
    assert calls == [None, ig_session]


@pytest.mark.parametrize("guest", [HIDDEN, NO_MEDIA])
def test_hidden_or_stripped_posts_fall_back_too(attempts, ig_session, guest):
    calls = attempts(guest, {"id": "ok"})
    assert d._extract_info({"cookiefile": ig_session}, THREADS) == {"id": "ok"}
    assert calls == [None, ig_session]


def test_hidden_under_the_session_too_alerts_the_owner(attempts, ig_session):
    attempts(HIDDEN, HIDDEN)
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": ig_session}, THREADS)
    assert "as a guest or with the saved threads.com cookies" in str(e.value)
    assert e.value.auth and e.value.no_post


def test_session_login_error_is_raised_as_is(attempts, ig_session):
    attempts(LOGIN, "ERROR: [x] abc: Instagram session expired: the API sent it to the login page")
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": ig_session}, IG)
    assert "saved instagram.com cookies have expired" in str(e.value)
    assert e.value.auth


def test_session_other_error_is_raised_as_is(attempts, ig_session):
    attempts(LOGIN, REMOVED)
    with pytest.raises(d.DownloadError, match="no longer available") as e:
        d._extract_info({"cookiefile": ig_session}, IG)
    assert not e.value.auth


def test_errors_a_session_cant_fix_skip_it(attempts, ig_session):
    calls = attempts(REMOVED)
    with pytest.raises(d.DownloadError, match="no longer available"):
        d._extract_info({"cookiefile": ig_session}, IG)
    assert calls == [None]


def test_no_cookie_file_at_all(attempts):
    calls = attempts(LOGIN)
    with pytest.raises(d.DownloadError, match="has no instagram.com cookies"):
        d._extract_info({}, IG)
    assert calls == [None]


def test_other_sites_use_cookies_directly(attempts, cookie_file):
    yt = cookie_file(cookie(".youtube.com", "SID"))
    calls = attempts({"id": "yt"})
    assert d._extract_info({"cookiefile": yt}, YT) == {"id": "yt"}
    assert calls == [yt]


def test_other_sites_login_error_without_cookies(attempts, no_session):
    calls = attempts("ERROR: [youtube] x: Sign in to confirm you're not a bot")
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": no_session}, YT)
    assert str(e.value) == "Cookie problem: this needs a login, and the bot has no youtube.com cookies."
    assert not e.value.auth
    assert calls == [no_session]


def test_other_sites_login_error_with_cookies(attempts, cookie_file):
    yt = cookie_file(cookie(".youtube.com", "SID"))
    attempts("ERROR: [youtube] x: Sign in to confirm you're not a bot")
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": yt}, YT)
    assert "saved youtube.com cookies didn't get through" in str(e.value)
    assert e.value.auth


def test_other_sites_plain_errors_pass_through(attempts, no_session):
    attempts(REMOVED)
    with pytest.raises(d.DownloadError, match="no longer available"):
        d._extract_info({"cookiefile": no_session}, YT)
