"""Pure helpers of our yt-dlp plugins."""
import re

import pytest
import yt_dlp
from yt_dlp.utils import ExtractorError

import downloader
import yt_dlp_plugins.extractor.instagram_extras as ig
from yt_dlp_plugins.extractor._images import best_image
from yt_dlp_plugins.extractor.threads import ThreadsIE
from yt_dlp_plugins.extractor.tiktok_photos import TikTokPhotoIE, _printable


def img(url, w=None, h=None):
    return {"url": url, "width": w, "height": h}


# ── _images.best_image ──────────────────────────────────────────────────────

def test_best_image_prefers_the_original_ratio_over_a_bigger_crop():
    media = {"original_width": 972, "original_height": 1296}
    images = [img("square", 1080, 1080), img("orig", 972, 1296), img("small", 486, 648)]
    assert best_image(media, images)["url"] == "orig"


def test_best_image_without_original_size_takes_the_largest():
    images = [img("a", 100, 100), img("b", 200, 300), img("c", 150, 150)]
    assert best_image({}, images)["url"] == "b"


def test_best_image_keeps_the_first_among_equals():
    # Guest candidates come without sizes; the first one is the original.
    assert best_image({}, [img("first"), img("second")])["url"] == "first"


# ── instagram_extras ────────────────────────────────────────────────────────

def test_photo_format_added_to_media_without_video():
    media = {"original_width": 1080, "original_height": 1350, "image_versions2": {"candidates": [
        img("https://cdn/sq.jpg?a=1", 1080, 1080), img("https://cdn/orig.webp?a=1", 1080, 1350)]}}
    info = ig._with_photo_format({"id": "x"}, media)
    assert info["formats"] == [{"format_id": "photo", "url": "https://cdn/orig.webp?a=1",
                                "ext": "webp", "width": 1080, "height": 1350}]


def test_photo_format_leaves_videos_and_empty_media_alone():
    video = {"id": "x", "formats": [{"url": "v.mp4"}]}
    assert ig._with_photo_format(dict(video), {"image_versions2": {"candidates": [img("i.jpg")]}}) == video
    assert ig._with_photo_format({"id": "x"}, {}) == {"id": "x"}


class FakeIE:
    @staticmethod
    def _is_login_redirect(url):
        return "/accounts/login" in url


class FakeResponse:
    def __init__(self, url):
        self.url = url


class FakeRequest:
    def __init__(self, url):
        self.url = url


LOGIN_PAGE = FakeResponse("https://www.instagram.com/accounts/login/?next=x")
API = "https://i.instagram.com/api/v1/media/1/info/"


def test_dead_session_on_the_api_is_an_error():
    with pytest.raises(ExtractorError, match="session expired"):
        ig._refuse_login_redirect(FakeIE(), API, LOGIN_PAGE, logged_in=True)
    with pytest.raises(ExtractorError, match="session expired"):
        ig._refuse_login_redirect(FakeIE(), FakeRequest(API), LOGIN_PAGE, logged_in=True)


@pytest.mark.parametrize("url, response, logged_in", [
    (API, LOGIN_PAGE, False),                                  # guests are yt-dlp's business
    ("https://www.instagram.com/p/abc/", LOGIN_PAGE, True),    # not the API
    (API, FakeResponse(API), True),                            # a normal answer
    (API, None, True),                                         # non-fatal request failed
])
def test_other_responses_pass_through(url, response, logged_in):
    assert ig._refuse_login_redirect(FakeIE(), url, response, logged_in) is response


PROGRESSIVE = "https://scontent-iev1-1.cdninstagram.com/v/reel.mp4?sig=1"


def guest_reel(native_url=PROGRESSIVE):
    """Formats of a guest reel as yt-dlp builds them (2026-11-08)."""
    return {"id": "r", "formats": [
        {"url": native_url, "ext": "mp4"},
        {"url": native_url, "ext": "mp4"},
        {"format_id": "dash-a", "url": "https://cdn/a.m4a", "ext": "m4a",
         "vcodec": "none", "acodec": "mp4a.40.5"},
        {"format_id": "dash-720v", "url": "https://cdn/720.mp4", "ext": "mp4",
         "vcodec": "vp09.00.31.08", "acodec": "none", "width": 720, "height": 1280},
        {"format_id": "dash-1080v", "url": "https://cdn/1080.mp4", "ext": "mp4",
         "vcodec": "vp09.00.40.08", "acodec": "none", "width": 1080, "height": 1920},
    ]}


def fake_probe(meta):
    calls = []

    def probe(url):
        calls.append(url)
        return meta
    probe.calls = calls
    return probe


H264_720 = {"vcodec": "h264", "acodec": "aac", "width": 720, "height": 1280}


def test_native_h264_is_labelled_when_dash_is_vp9_only():
    probe = fake_probe(H264_720)
    info = ig._label_native_h264(guest_reel(), probe)
    assert info["formats"][0] == {"url": PROGRESSIVE, "ext": "mp4", **H264_720}
    assert info["formats"][1] == info["formats"][0]
    assert probe.calls == [PROGRESSIVE]  # one probe per file


def test_low_res_native_stays_unlabelled():
    meta = {"vcodec": "h264", "acodec": "aac", "width": 480, "height": 854}
    info = ig._label_native_h264(guest_reel(), fake_probe(meta))
    assert "vcodec" not in info["formats"][0]


@pytest.mark.parametrize("meta", [None, {**H264_720, "vcodec": "hevc"}])
def test_unprobed_or_other_codec_stays_unlabelled(meta):
    info = ig._label_native_h264(guest_reel(), fake_probe(meta))
    assert "vcodec" not in info["formats"][0]


def test_no_probe_when_dash_has_h264():
    info = guest_reel()
    info["formats"][3]["vcodec"] = "avc1.64001f"
    probe = fake_probe(H264_720)
    ig._label_native_h264(info, probe)
    assert probe.calls == []


def test_no_probe_without_dash_video():
    probe = fake_probe(H264_720)
    info = {"id": "r", "formats": [{"url": PROGRESSIVE}]}
    assert ig._label_native_h264(info, probe) == {"id": "r", "formats": [{"url": PROGRESSIVE}]}
    assert probe.calls == []


@pytest.mark.parametrize("url", [
    "http://scontent.cdninstagram.com/v.mp4",       # not https
    "https://192.168.0.1/v.mp4",                     # not Instagram's CDN
    "https://cdninstagram.com.evil.example/v.mp4",
    "file:///etc/passwd",
])
def test_probe_runs_only_on_instagram_cdn_over_https(monkeypatch, url):
    def no_run(*a, **k):
        raise AssertionError("ffprobe must not run")
    monkeypatch.setattr(ig.subprocess, "run", no_run)
    assert ig._probe_stream(url) is None


def test_bot_format_choice_takes_labelled_native_h264(config, tmp_path):
    """With the labels, the bot's own format/format_sort pick the native file alone."""
    opts = downloader._build_opts(config(), "%(id)s.%(ext)s",
                                  "https://www.instagram.com/reel/r/", str(tmp_path))
    info = ig._label_native_h264(guest_reel(), fake_probe(H264_720))
    info.update(title="t", extractor="instagram", extractor_key="Instagram",
                webpage_url="https://www.instagram.com/reel/r/")
    ydl_opts = {"format": opts["format"], "format_sort": opts["format_sort"], "quiet": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        chosen = ydl.process_ie_result(info, download=False)
    assert chosen["url"] == PROGRESSIVE
    assert "requested_formats" not in chosen  # no merge with the DASH audio


# ── threads ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url, ok", [
    ("https://www.threads.com/@u/post/C6cqebdCfBi", True),
    ("https://threads.net/@u/post/C6cqebdCfBi?xmt=1", True),
    ("https://www.threads.com/t/C6cqebdCfBi", True),
    ("https://www.threads.com/@u", False),
])
def test_threads_url_match(url, ok):
    assert ThreadsIE.suitable(url) is ok


@pytest.mark.parametrize("code, pk", [("B", 1), ("BA", 64), ("C6cqebdCfBi", 3358746231127535714)])
def test_threads_code_to_pk(code, pk):
    assert ThreadsIE._pk_from_code(code) == pk


def test_threads_code_with_foreign_characters():
    assert ThreadsIE._pk_from_code("abc.def") is None


def test_threads_video_entry():
    media = {"pk": 1, "original_width": 720, "original_height": 1280,
             "video_versions": [{"type": 101, "url": "https://v/1.mp4"}],
             "image_versions2": {"candidates": [img("https://i/1.jpg", 720, 1280)]}}
    entry = ThreadsIE()._media_entry(media, "CODE")
    assert entry["id"] == "1"
    assert entry["formats"] == [{"format_id": "1-101", "url": "https://v/1.mp4",
                                 "width": 720, "height": 1280}]


def test_threads_photo_entry_and_text_post():
    media = {"pk": 2, "original_width": 1080, "original_height": 1350, "image_versions2": {
        "candidates": [img("https://i/sq.jpg", 1080, 1080), img("https://i/orig.jpg", 1080, 1350)]}}
    entry = ThreadsIE()._media_entry(media, "CODE")
    assert entry["formats"][0]["url"] == "https://i/orig.jpg"
    assert ThreadsIE()._media_entry({"pk": 3}, "CODE") is None


def test_threads_carousel_skips_text_items():
    photo = {"pk": 1, "image_versions2": {"candidates": [img("https://i/1.jpg", 10, 10)]}}
    post = {"carousel_media": [photo, {"pk": 2}, dict(photo, pk=3)]}
    assert [e["id"] for e in ThreadsIE()._entries(post, "CODE")] == ["1", "3"]


@pytest.mark.parametrize("share_info, shared, unavailable", [
    ({"quoted_attachment_post": {"code": "Q"}}, {"code": "Q"}, False),
    ({"reposted_post": {"code": "R"}}, {"code": "R"}, False),
    ({"quoted_post_unavailable": True}, None, True),
    ({}, None, False),
])
def test_threads_shared_post(share_info, shared, unavailable):
    post = {"text_post_app_info": {"share_info": share_info}}
    assert ThreadsIE._shared_post(post) == (shared, unavailable)


# ── tiktok_photos ───────────────────────────────────────────────────────────

def test_tiktok_photo_url_match():
    assert TikTokPhotoIE.suitable("https://www.tiktok.com/@u.name/photo/7553302113757990166")
    assert not TikTokPhotoIE.suitable("https://www.tiktok.com/@u/video/7553302113757990166")


@pytest.mark.parametrize("text, clean", [
    ("ab￴c\x00 ", "abc"),
    ("Zoë 😀", "Zoë 😀"),
    ("￴", None),
    (None, None),
])
def test_tiktok_printable(text, clean):
    assert _printable(text) == clean


# ── names vs the bot's extractor allowlist ──────────────────────────────────

@pytest.mark.parametrize("name", [
    ig._InstagramIE.IE_NAME, ig._InstagramStoryIE.IE_NAME, ThreadsIE.IE_NAME, TikTokPhotoIE.IE_NAME,
])
def test_plugins_are_allowed(name):
    assert any(re.fullmatch(p, name, re.IGNORECASE) for p in downloader._ALLOWED_EXTRACTORS)


def test_generic_is_not_allowed():
    assert not any(re.fullmatch(p, "generic", re.IGNORECASE) for p in downloader._ALLOWED_EXTRACTORS)
