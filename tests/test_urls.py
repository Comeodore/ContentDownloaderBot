"""Link detection: extract_url, site_of, _normalize_url."""
import pytest

import downloader as d


@pytest.mark.parametrize("text, url", [
    ("look https://youtu.be/abc.", "https://youtu.be/abc"),
    ("(https://youtu.be/abc).", "https://youtu.be/abc"),
    ("https://youtu.be/abc!?…»", "https://youtu.be/abc"),
    ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"),
    ("first https://a.com/1 then https://b.com/2", "https://a.com/1"),
    ("no link here", None),
    ("", None),
    (None, None),
])
def test_extract_url(text, url):
    assert d.extract_url(text) == url


@pytest.mark.parametrize("url, site", [
    ("https://www.tiktok.com/@u/video/1", "tiktok"),
    ("https://vm.tiktok.com/ZSabc/", "tiktok"),
    ("https://tiktok.com/@u/photo/1", "tiktok"),
    ("https://www.tiktok.com./@u/video/1", "tiktok"),
    ("https://www.tiktok.com:443/@u/video/1", "tiktok"),
    ("http://www.tiktok.com:80/@u/video/1", "tiktok"),
    ("https://www.instagram.com/p/abc/", "instagram"),
    ("https://instagram.com/reel/abc", "instagram"),
    ("https://instagr.am/p/abc", "instagram"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
    ("https://youtube.com/shorts/abc?si=x", "youtube"),
    ("https://m.youtube.com/shorts/abc", "youtube"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/embed/dQw4w9WgXcQ", "youtube"),
    ("https://www.youtube.com/clip/UgkxAbc", "youtube"),
    ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", "youtube"),
    ("https://www.threads.com/@u/post/abc", "threads"),
    ("https://threads.net/@u/post/abc", "threads"),
])
def test_site_of_accepts(url, site):
    assert d.site_of(url) == site
    assert d.is_supported(url)


@pytest.mark.parametrize("url", [
    # Parser-confusion tricks that could reach the LAN.
    "https://tiktok.com@192.168.0.1/",
    "https://www.tiktok.com\\@192.168.0.1/",
    "https://www.tiktok.com:8080/@u/video/1",
    "ftp://www.tiktok.com/@u/video/1",
    "file:///etc/passwd",
    # Site name outside the host.
    "https://example.com/?u=tiktok.com",
    "https://nottiktok.com/@u/video/1",
    "https://tiktok.com.evil.com/@u/video/1",
    "https://192.168.0.1/",
    # YouTube pages that aren't a single video.
    "https://www.youtube.com/@channel",
    "https://www.youtube.com/playlist?list=PL1",
    "https://www.youtube.com/watch?list=PL1",
    "https://www.youtube.com/live/abc",
    "https://www.youtube.com/embed/videoseries?list=PL1",
    "https://www.youtube.com/embed/live_stream?channel=x",
    "https://www.youtube-nocookie.com/watch?v=dQw4w9WgXcQ",
    "not a url",
    "https://[broken/",
])
def test_site_of_rejects(url):
    assert d.site_of(url) is None
    assert not d.is_supported(url)


def test_is_threads():
    assert d.is_threads("https://www.threads.com/@u/post/abc")
    assert not d.is_threads("https://www.instagram.com/p/abc/")


@pytest.mark.parametrize("url, normalized", [
    ("https://tiktok.com/@u/video/1?lang=en", "https://www.tiktok.com/@u/video/1?lang=en"),
    ("https://m.tiktok.com/v/123.html", "https://www.tiktok.com/share/video/123"),
    ("https://www.tiktok.com/@u/video/1", "https://www.tiktok.com/@u/video/1"),
    ("https://vm.tiktok.com/ZSabc/", "https://vm.tiktok.com/ZSabc/"),
    ("https://vt.tiktok.com/ZSabc/", "https://vt.tiktok.com/ZSabc/"),
    ("https://www.instagram.com/p/abc/", "https://www.instagram.com/p/abc/"),
])
def test_normalize_url(url, normalized):
    assert d._normalize_url(url) == normalized
