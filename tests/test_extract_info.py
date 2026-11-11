"""_extract_info: Instagram and Threads as a guest only, other sites with cookies."""
import pytest

import downloader as d
from conftest import cookie

IG = "https://www.instagram.com/p/abc/"
STORY = "https://www.instagram.com/stories/someone/3700000000000000000/"
THREADS = "https://www.threads.com/@u/post/abc"
YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Real guest answers (2026-11-10/11).
RESTRICTED = ("ERROR: [Instagram+videobot] abc: This content isn't available to everyone: "
              "It can't be seen by certain audiences.")
EMPTY = ("ERROR: [Instagram+videobot] abc: Instagram sent an empty media response. Check if "
         "this post is accessible in your browser without being logged-in. If it is not, then "
         "use --cookies-from-browser or --cookies for the authentication.")
STORY_ERR = ("ERROR: [instagram:story+videobot] 3700000000000000000: This content is "
             "unreachable. Use --cookies-from-browser or --cookies for the authentication.")
THREADS_RESTRICTED = ("ERROR: [Threads] abc: This content isn't available to everyone: "
                      "It can't be seen by certain audiences.")
HIDDEN = ("ERROR: [Threads] abc: This post is unavailable — it may have been deleted, "
          "or it is hidden from logged-out visitors")
QUOTE = ("ERROR: [Threads] abc: The quoted post holding the media is unavailable — it may "
         "have been deleted, or it is not shown to logged-out visitors")
NO_MEDIA = "ERROR: [Threads] abc: This Threads post has no photo or video"
NO_DATA = "ERROR: [Threads] abc: Could not find post data"
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
def sessions(cookie_file):
    return cookie_file(cookie(".instagram.com", "sessionid"), cookie(".threads.com", "sessionid"))


@pytest.mark.parametrize("url", [IG, THREADS])
def test_guest_only_sites_never_get_cookies(attempts, sessions, url):
    calls = attempts({"id": "ok"})
    assert d._extract_info({"cookiefile": sessions}, url) == {"id": "ok"}
    assert calls == [None]


@pytest.mark.parametrize("url, raw, start", [
    (IG, RESTRICTED, "Instagram shows this post only to signed-in users"),
    (IG, EMPTY, "Instagram shows this post only to signed-in users"),
    (STORY, STORY_ERR, "Instagram shows stories only to signed-in users"),
    (THREADS, THREADS_RESTRICTED, "Threads shows this post only to signed-in users"),
    (THREADS, HIDDEN, "Threads shows this post only to signed-in users"),
    (THREADS, QUOTE, "This post quotes another one, and Threads shows that one only"),
])
def test_closed_to_guests_is_one_try_and_no_report(attempts, sessions, url, raw, start):
    calls = attempts(raw, {"id": "a second try must not happen"})
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": sessions}, url)
    assert str(e.value).startswith(start)
    assert "without an account" in str(e.value)
    assert e.value.guest_wall and e.value.user_side and not e.value.auth
    assert calls == [None]


def test_text_only_threads_post_keeps_its_message(attempts):
    attempts(NO_MEDIA)
    with pytest.raises(d.DownloadError, match="This post is text only") as e:
        d._extract_info({}, THREADS)
    assert not e.value.guest_wall


def test_page_without_post_data_stays_a_bot_failure(attempts):
    # Not an access wall Threads named: maybe its page changed, worth a report.
    attempts(NO_DATA)
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({}, THREADS)
    assert not e.value.guest_wall and not e.value.user_side


def test_other_errors_of_guest_only_sites_pass_through(attempts):
    attempts(REMOVED)
    with pytest.raises(d.DownloadError, match="no longer available") as e:
        d._extract_info({}, IG)
    assert not e.value.guest_wall


def test_other_sites_use_cookies_directly(attempts, cookie_file):
    yt = cookie_file(cookie(".youtube.com", "SID"))
    calls = attempts({"id": "yt"})
    assert d._extract_info({"cookiefile": yt}, YT) == {"id": "yt"}
    assert calls == [yt]


def test_other_sites_login_error_without_cookies(attempts, sessions):
    calls = attempts("ERROR: [youtube] x: Sign in to confirm you're not a bot")
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": sessions}, YT)
    assert str(e.value) == "Cookie problem: this needs a login, and the bot has no youtube.com cookies."
    assert not e.value.auth and not e.value.guest_wall
    assert calls == [sessions]


def test_other_sites_login_error_with_cookies(attempts, cookie_file):
    yt = cookie_file(cookie(".youtube.com", "SID"))
    attempts("ERROR: [youtube] x: Sign in to confirm you're not a bot")
    with pytest.raises(d.DownloadError) as e:
        d._extract_info({"cookiefile": yt}, YT)
    assert "saved youtube.com cookies didn't get through" in str(e.value)
    assert e.value.auth


def test_other_sites_plain_errors_pass_through(attempts):
    attempts(REMOVED)
    with pytest.raises(d.DownloadError, match="no longer available"):
        d._extract_info({}, YT)


@pytest.mark.parametrize("url, gets_cookies", [(IG, False), (THREADS, False), (YT, True)])
def test_build_opts_copies_cookies_only_for_other_sites(config, sessions, tmp_path, url,
                                                        gets_cookies):
    work = tmp_path / "work"
    work.mkdir()
    opts = d._build_opts(config(sessions), str(work / "%(id)s.%(ext)s"), url, str(work))
    assert ("cookiefile" in opts) is gets_cookies
    assert (work / "cookies.txt").exists() is gets_cookies
