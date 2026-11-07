"""yt-dlp wrapper: videos and photos from TikTok, Instagram, YouTube and Threads.

Handles single videos, photos and multi-item posts (carousels, TikTok slideshows
with music). Instagram photos and stories, Threads and TikTok photo posts come
from our plugins in yt_dlp_plugins: yt-dlp itself only takes videos there.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

import yt_dlp

from config import Config

logger = logging.getLogger("videobot.downloader")

# Telegram's player (iOS especially) reliably plays only 8-bit yuv420p H.264:
# HEVC stutters, VP9/AV1 freeze the picture. Everything else is re-encoded.
_TELEGRAM_VCODEC = "h264"
_TELEGRAM_PIX = {"yuv420p", "yuvj420p"}
# Opus/Vorbis in mp4 (YouTube) doesn't play in every client: the audio becomes AAC.
_TELEGRAM_ACODECS = {"aac", "mp3"}

# CRF 18 is visually lossless; veryfast spares the mini-PC's CPU.
_H264_ARGS = [
    "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
    "-preset", "veryfast", "-crf", "18",
]
_AAC_ARGS = ["-c:a", "aac", "-b:a", "160k"]

# The link's parsed host must be one of these domains or a subdomain. A substring
# match once let "tiktok.com" anywhere in a URL through to the generic extractor,
# which fetched any address, LAN ones included.
_SITES = {
    "tiktok": ("tiktok.com",),
    "instagram": ("instagram.com", "instagr.am"),
    # youtube-nocookie.com: the embedded player (/embed/).
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "threads": ("threads.com", "threads.net"),
}

# Post extractors of our sites only (names match in full). Otherwise generic takes
# unrecognised links and follows any address or redirect. Profiles, tags and feeds
# (tiktok:user, …) are left out: a profile link would pull a whole account.
_ALLOWED_EXTRACTORS = [
    r"tiktok", r"vm\.tiktok",                # videos and short vm./t/ links
    r"tiktok:photo",                         # our plugin: photo posts
    # "+videobot": our overrides of yt-dlp's extractors carry this suffix.
    r"instagram(?:\+videobot)?", r"instagram:story(?:\+videobot)?",
    r"youtube", r"youtube:tab",              # tab: a video opened from a playlist
    r"youtube:clip",
    r"threads",                              # our plugin
]

# YouTube videos besides /watch?v=: Shorts, embeds and clips. The 11-character
# embeds videoseries and live_stream are a playlist and a channel's live.
_YOUTUBE_VIDEO_PATH_RE = re.compile(
    r"/(?:shorts/[\w-]+|embed/(?!videoseries|live_stream)[\w-]{11}|clip/[\w-]+)/?")

# Lives and upcoming premieres aren't downloaded: the recording would hold a slot
# until the stream ends, for hours or forever.
_LIVE_STATUSES = ("is_live", "is_upcoming")
_LIVE_MESSAGE = (
    "This is a live stream or a premiere that hasn't happened yet — I don't "
    "download those. Send the link after it's over."
)

# A TikTok photo post has up to 35 pictures plus music, an Instagram carousel 20.
MAX_POST_ITEMS = 36

_URL_RE = re.compile(r"https?://\S+")
# Trailing characters that belong to the surrounding text, not the link.
_URL_TAIL = ".,;:!?…»\"'>]"

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# TikTok slideshow music comes as a separate track.
AUDIO_EXTS = {".mp3", ".m4a"}
_SKIP_EXTS = {".part", ".ytdl", ".temp", ".f4v"}
# yt-dlp's tracks before merging: "001-id.f140.m4a".
_INTERMEDIATE_RE = re.compile(r"\.f[^.]+\.[^.]+$")


@dataclass
class MediaItem:
    path: str
    is_video: bool
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    # Slideshow music, sent as a separate track with these tags.
    is_audio: bool = False
    title: str | None = None
    performer: str | None = None


@dataclass
class DownloadResult:
    items: list[MediaItem] = field(default_factory=list)
    title: str = "video"
    description: str = ""
    total_size: int = 0


class DownloadError(Exception):
    """Download error; the message is shown to the user.

    auth: the site's saved cookies didn't work, so the owner gets a reminder.
        Not set when the bot has no cookies for the site at all.
    no_post: the post page wasn't served (redirect home, empty shell, 404).
    no_media: the post came without media.
        Both are worth retrying under the session (see _extract_info).
    waf: the site's WAF rejected the TLS fingerprint (TikTok); retry with
        another impersonate target.
    """

    def __init__(self, message: str, auth: bool = False, no_post: bool = False,
                 no_media: bool = False, waf: bool = False) -> None:
        super().__init__(message)
        self.auth = auth
        self.no_post = no_post
        self.no_media = no_media
        self.waf = waf


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0)
    # "(https://youtu.be/abc)." — a closing parenthesis goes only if unpaired.
    while url:
        if url[-1] in _URL_TAIL:
            url = url[:-1]
        elif url[-1] == ")" and url.count("(") < url.count(")"):
            url = url[:-1]
        else:
            break
    return url or None


def site_of(url: str) -> str | None:
    """The link's site ("tiktok", "instagram", "youtube", "threads") or None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    # userinfo ("tiktok.com@192.168.0.1") and backslashes make URL parsers
    # disagree on the host.
    if (parts.scheme not in ("http", "https") or "@" in parts.netloc
            or "\\" in url or port not in (None, 80, 443)):
        return None
    host = (parts.hostname or "").rstrip(".")
    for site, domains in _SITES.items():
        for domain in domains:
            if host != domain and not host.endswith("." + domain):
                continue
            # Only videos: a channel or playlist would download whole.
            # Live recordings (/live/<id>) are excluded too.
            if domain == "youtube.com" and not (
                    _YOUTUBE_VIDEO_PATH_RE.fullmatch(parts.path)
                    or (parts.path == "/watch" and parse_qs(parts.query).get("v"))):
                return None
            if domain == "youtube-nocookie.com" and not (
                    parts.path.startswith("/embed/")
                    and _YOUTUBE_VIDEO_PATH_RE.fullmatch(parts.path)):
                return None
            return site
    return None


def is_supported(url: str) -> bool:
    return site_of(url) is not None


def _normalize_url(url: str) -> str:
    """TikTok links in the form yt-dlp recognises; others unchanged.

    The extractor needs www.tiktok.com: links without www and old mobile
    m.tiktok.com/v/<id>.html are "Unsupported URL". vm./vt. have their own.
    """
    if site_of(url) != "tiktok":
        return url
    parts = urlsplit(url)
    if parts.hostname in ("www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"):
        return url
    path = parts.path
    if old_mobile := re.fullmatch(r"/v/(\d+)\.html", path):
        path = f"/share/video/{old_mobile.group(1)}"
    return urlunsplit(("https", "www.tiktok.com", path, parts.query, ""))


def is_threads(url: str) -> bool:
    return site_of(url) == "threads"


class _SameSiteRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects only within the original link's site."""

    max_redirections = 5

    def __init__(self, site: str):
        super().__init__()
        self.site = site

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if site_of(newurl) != self.site:
            raise urllib.error.HTTPError(newurl, code, f"redirect off {self.site}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _resolve_share_link(url: str) -> str:
    """Resolves instagram.com/share/… and threads.com/share/… to the post link.

    Neither extractor takes share links, and generic is disallowed, so the
    redirects are followed here, checking every hop stays on the site.
    """
    site = site_of(url)
    if site not in ("instagram", "threads") or not urlsplit(url).path.startswith("/share/"):
        return url
    opener = urllib.request.build_opener(_SameSiteRedirects(site))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(request, timeout=15) as resp:
            final = resp.geturl()
    except (OSError, ValueError) as e:
        logger.info("Couldn't resolve share link (%s): %s", url, e)
        return url
    return final if site_of(final) == site else url


# ── Pinning the curl_cffi impersonate target ────────────────────────────────
# The TikTok extractor asks for any target and yt-dlp picks the newest Chrome.
# Sites periodically ban a TLS fingerprint and answer with a WAF stub
# ("Unexpected response from webpage request"), so we cycle browsers ourselves.
_IMPERSONATE_CLIENTS = ("firefox", "safari", "chrome")
# Downloads run in parallel threads, so the target is thread-local.
_pinned = threading.local()


def _install_impersonate_pinning() -> bool:
    """Makes yt-dlp resolve the pinned target instead of the first available.

    No public API: the extractor overrides the impersonate option with a
    wildcard, so this patches the internal _resolve_target. yt-dlp updates
    nightly; on any surprise the default behaviour stays.
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateRequestHandler
    except ImportError:
        return False  # no curl_cffi, no impersonation

    original = ImpersonateRequestHandler._resolve_target

    def _resolve_target(self, target):
        pinned = getattr(_pinned, "target", None)
        # Only the wildcard (client=None) is replaced, never an explicit target.
        if pinned is not None and target is not None and target.client is None:
            return original(self, pinned) or original(self, target)
        return original(self, target)

    try:
        ImpersonateRequestHandler._resolve_target = _resolve_target
    except Exception:  # noqa: BLE001
        return False
    return True


_IMPERSONATE_PINNING = _install_impersonate_pinning()


def _impersonate_candidates() -> list:
    """Targets to try: the newest per browser, then yt-dlp's default."""
    if not _IMPERSONATE_PINNING:
        return [None]
    try:
        from yt_dlp.networking._curlcffi import CurlCFFIRH
        from yt_dlp.networking.impersonate import ImpersonateTarget
        available = {t.client for t in CurlCFFIRH.supported_targets}
        return [ImpersonateTarget(c) for c in _IMPERSONATE_CLIENTS if c in available] + [None]
    except Exception:  # noqa: BLE001
        return [None]


# ── Errors: yt-dlp's wording to the user's ──────────────────────────────────
# yt-dlp writes for someone running it by hand. The "ERROR: [Extractor] <id>:"
# prefix is stripped and known cases get short messages.
_YTDLP_PREFIX_RE = re.compile(r"^ERROR:\s*(?:\[[^\]]+\]\s*)?(?:[\w.-]+:\s+)?")

# A login or fresh cookies are needed.
_AUTH_MARKERS = (
    "login", "log in", "sign in", "rate-limit", "rate limit", "private",
    "empty media response", "requires authentication", "not a bot",
    # yt-dlp's "Use --cookies… for the authentication": Instagram stories for guests.
    "for the authentication",
    # Audience-restricted reels: refused to guests, served under the session.
    "available to everyone",
)

# Our Instagram plugin's error for an API redirect to login under the session.
_SESSION_EXPIRED_MARKERS = ("session expired",)

# The post page wasn't served. Looks the same for a hidden post and a bad
# session (Threads answers a bad session so even for public posts).
_NO_POST_MARKERS = (
    "hidden from logged-out", "could not find post data", "http error 404",
)

# The Threads plugin's "post without media".
_NO_MEDIA_MARKERS = ("has no photo or video",)

# The TikTok extractor's failure on a WAF stub or JS challenge.
_WAF_MARKERS = ("unexpected response from webpage request", "challenge")

# (markers in yt-dlp's text) → message. First match wins: specific cases first.
_ERROR_MESSAGES = (
    # The Threads plugin's own messages.
    (("quoted post holding the media",),
     "This post quotes another one, and that post is unavailable — "
     "deleted, or not shown to logged-out visitors."),
    (("hidden from logged-out", "could not find post data"),
     "The site didn't hand over this post — it's deleted, "
     "or visible only to logged-in users."),
    (("has no photo or video",),
     "This post is text only — no photo or video in it."),
    # Our Instagram plugin: the story expired (24 h). Not a login error.
    (("instagram story is gone",),
     "This story is no longer available — stories disappear after 24 hours, "
     "and deleted ones or ones from private accounts can't be downloaded."),
    # Lives YouTube won't serve: no recording yet, upcoming premiere.
    (("live stream recording is not available", "live event will begin", "premieres in"),
     _LIVE_MESSAGE),
    (("unsupported url", "no suitable extractor"),
     "This link doesn't point to a video or a post."),
    (("blocked from accessing", "ip address is blocked"),
     "The site is blocking this server's IP address."),
    (("not available in your country", "geo restriction", "geo-restricted"),
     "This video isn't available in the country where the bot runs."),
    (("is unavailable", "has been removed", "no longer available",
      "video not found", "does not exist"),
     "This video is no longer available — deleted or private."),
    (("requested format", "no video formats", "no formats found"),
     "No downloadable video or photo in this post."),
    (("http error 4", "http error 5", "unable to download video data",
      "forbidden"),
     "The site refused to hand over the file."),
    (("timed out", "timeout", "unable to connect", "connection reset",
      "temporary failure in name resolution"),
     "The site didn't respond in time."),
)


def _raw_error(e: Exception) -> str:
    # The last line holds the point; above it is the traceback.
    lines = str(e).splitlines()
    return lines[-1].strip() if lines else ""


def error_detail(e: BaseException | None) -> str | None:
    """The cause behind an error: yt-dlp's raw text, or the exception's type and text."""
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, yt_dlp.utils.DownloadError):
            return _raw_error(e).removeprefix("ERROR: ")
        if not isinstance(e, DownloadError):
            return f"{type(e).__name__}: {_raw_error(e)}"
        e = e.__cause__
    return None


def _cookies_name(site: str | None) -> str:
    """How errors name a site's cookies: "instagram.com cookies"."""
    return f"{_SITES[site][0]} cookies" if site in _SITES else "cookies for this site"


def _as_download_error(e: Exception, site: str | None = None) -> DownloadError:
    clean = _YTDLP_PREFIX_RE.sub("", _raw_error(e))
    low = clean.lower()

    # Flags come from yt-dlp's text: the translation may lose the words.
    no_post = any(marker in low for marker in _NO_POST_MARKERS)
    no_media = any(marker in low for marker in _NO_MEDIA_MARKERS)
    waf = any(marker in low for marker in _WAF_MARKERS)

    # Access errors are cookie problems. Without the site's cookies in the
    # request, _extract_info replaces this with "the bot has no cookies".
    cookies = _cookies_name(site)
    if any(marker in low for marker in _SESSION_EXPIRED_MARKERS):
        return DownloadError(
            f"Cookie problem: the saved {cookies} have expired — the site sends "
            "the bot to its login page.",
            auth=True, no_post=no_post,
        )
    if any(marker in low for marker in _AUTH_MARKERS):
        return DownloadError(
            f"Cookie problem: this needs a login, and the saved {cookies} didn't "
            "get through — they've expired, or their account can't see this.",
            auth=True, no_post=no_post,
        )
    for markers, text in _ERROR_MESSAGES:
        if any(marker in low for marker in markers):
            return DownloadError(text, no_post=no_post, no_media=no_media, waf=waf)

    # Unknown case: yt-dlp's own text, capped.
    if len(clean) > 200:
        clean = clean[:199].rstrip() + "…"
    return DownloadError(f"Couldn't download it: {clean}", no_post=no_post, waf=waf)


# ── cookies.txt: per-site replacement ───────────────────────────────────────
# One file serves all sites, so an upload replaces only the sites it contains.
_COOKIE_HEADER = "# Netscape HTTP Cookie File\n"
_HTTPONLY_PREFIX = "#HttpOnly_"


def _cookie_key(line: str) -> tuple[str, str, str] | None:
    """(domain, path, name) of a Netscape-format record, or None."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 7:
        return None
    domain = parts[0]
    if domain.startswith(_HTTPONLY_PREFIX):
        domain = domain[len(_HTTPONLY_PREFIX):]
    if not domain or domain.startswith("#"):
        return None
    return domain.lower(), parts[2], parts[5]


def _site_domain(domain: str) -> str:
    """Domain without the leading dot and www.: ".www.threads.com" → "threads.com"."""
    domain = domain.lstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def merge_cookies_file(upload_path: str, target_path: str) -> list[str]:
    """Replaces the cookies of upload_path's sites in target_path; returns the sites.

    An export is a full snapshot, so old records of its sites are dropped, not
    merged by name: a logged-out export must remove an old sessionid.
    An empty list means no valid records and target_path is untouched.
    """
    with open(upload_path, encoding="utf-8", errors="replace") as f:
        incoming = [(key, line) for line in f if (key := _cookie_key(line))]
    if not incoming:
        return []

    sites = {_site_domain(key[0]) for key, _ in incoming}

    def replaced(domain: str) -> bool:
        d = _site_domain(domain)
        return any(d == s or d.endswith("." + s) for s in sites)

    lines: list[str] = []
    if os.path.isfile(target_path):
        with open(target_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                key = _cookie_key(line)
                # The header is rewritten; the upload's sites are replaced.
                if key is None or replaced(key[0]):
                    continue
                lines.append(line)
    lines.extend(line for _, line in incoming)

    # In place, not via rename: on the volume shared with the Bot API server a
    # new inode would change the owner and lock the bot out.
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(_COOKIE_HEADER)
        for line in lines:
            f.write(line if line.endswith("\n") else line + "\n")
    return sorted(sites)


# Instagram and Threads go as a guest first; the cookies.txt session is only for
# what guests don't get (stories, private accounts, hidden posts). Every session
# request counts against a real account: with all links of this public bot going
# through it, Instagram locked the owner's account (2026-11-06).
_GUEST_FIRST_SITES = ("instagram", "threads")
# Both sites' session cookie; without it a request is anonymous anyway.
_SESSION_COOKIE = "sessionid"


def _has_session(cookiefile: str | None, site: str | None) -> bool:
    """Whether cookies.txt holds a login for the site.

    Instagram and Threads: a sessionid on the site's domain. Others: any of
    their cookies, since session cookie names differ.
    """
    if not cookiefile or site not in _SITES:
        return False
    domains = _SITES[site]
    try:
        with open(cookiefile, encoding="utf-8", errors="replace") as f:
            for line in f:
                key = _cookie_key(line)
                if not key:
                    continue
                if site in _GUEST_FIRST_SITES and key[2] != _SESSION_COOKIE:
                    continue
                d = _site_domain(key[0])
                if any(d == s or d.endswith("." + s) for s in domains):
                    return True
    except OSError:
        logger.exception("Couldn't read cookies")
    return False


def _no_cookies_error(site: str | None, cause: DownloadError) -> DownloadError:
    """The site wants a login and the bot has no cookies for it.

    auth stays False: there's nothing for the owner to renew.
    """
    what = (" (a story, a private account, or a post hidden from logged-out visitors)"
            if site in _GUEST_FIRST_SITES else "")
    return DownloadError(
        f"Cookie problem: this needs a login{what}, and the bot has no "
        f"{_cookies_name(site)}.",
        no_post=cause.no_post, no_media=cause.no_media,
    )


def _extract_info(opts: dict, url: str) -> dict:
    """extract_info; Instagram and Threads as a guest first, then under the session."""
    site = site_of(url)
    cookiefile = opts.get("cookiefile")
    if site not in _GUEST_FIRST_SITES:
        try:
            return _extract_info_attempts(opts, url)
        except DownloadError as e:
            if not e.auth or _has_session(cookiefile, site):
                raise
            raise _no_cookies_error(site, e) from e
    try:
        return _extract_info_attempts({**opts, "cookiefile": None}, url)
    except DownloadError as guest:
        # The session only helps with access, hidden or stripped posts.
        if not (guest.auth or guest.no_post or guest.no_media):
            raise
        if not _has_session(cookiefile, site):
            # No session: no reminder for the owner; the user is told it's cookies.
            if guest.auth:
                raise _no_cookies_error(site, guest) from guest
            if guest.no_post:
                raise DownloadError(
                    "The site didn't hand over this post: it's deleted, or it's a "
                    "cookie problem — the post is for logged-in users only, and the "
                    f"bot has no {_cookies_name(site)}.",
                    no_post=True,
                ) from guest
            raise
        logger.info("%s: guest attempt failed (%s), trying the session", site, guest)
        try:
            return _extract_info_attempts(opts, url)
        except DownloadError as e:
            if e.auth or not e.no_post:
                raise
            # Not served under the session either: deleted, or a bad session
            # (Threads answers so), which the owner should check.
            raise DownloadError(
                "The site didn't hand over this post, as a guest or with the saved "
                f"{_cookies_name(site)}: it's deleted, or it's a cookie problem — "
                "the cookies have expired, or their account can't see this post.",
                auth=True, no_post=True,
            ) from e


def _extract_info_attempts(opts: dict, url: str) -> dict:
    """extract_info; for TikTok, cycling impersonate targets on WAF errors.

    Elsewhere retries only add requests: Instagram rate-limits, Threads serves
    empty pages.
    """
    site = site_of(url)
    candidates = _impersonate_candidates() if site == "tiktok" else [None]
    for attempt, target in enumerate(candidates, start=1):
        _pinned.target = target
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            err = _as_download_error(e, site)
            # The raw text goes to the log: the user's message loses the cause.
            logger.warning(
                "yt-dlp failed %s (attempt %d/%d, target %s): %s",
                url, attempt, len(candidates), target or "default", _raw_error(e))
            if not err.waf or attempt == len(candidates):
                raise err from e
        finally:
            _pinned.target = None
    raise DownloadError("Couldn't download it.")  # unreachable


def _to_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _cookies_for_run(config: Config, work_dir: str) -> str | None:
    """A one-off copy of cookies.txt for this download.

    yt-dlp writes the cookie jar back to the file, and Threads answers a refused
    request with a logout that would erase sessionid from the bot's file.
    """
    src = config.cookies_file
    if not src or not os.path.isfile(src):
        return None
    dst = os.path.join(work_dir, "cookies.txt")
    try:
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
    except OSError:
        logger.exception("Couldn't copy cookies, downloading without them")
        return None
    return dst


def _is_live(info: dict) -> bool:
    return bool(info.get("is_live")) or info.get("live_status") in _LIVE_STATUSES


def _skip_live(info: dict, *, incomplete: bool = False) -> str | None:
    """yt-dlp match_filter: skip lives (_reject_live explains why)."""
    return "live stream" if _is_live(info) else None


def _reject_live(info: dict) -> None:
    if _is_live(info):
        raise DownloadError(_LIVE_MESSAGE)


def _build_opts(config: Config, out_template: str, url: str, work_dir: str) -> dict:
    opts: dict = {
        # Vertical 1080p is 1080x1920, so the longer side is capped.
        "format": "bv*[height<=1920][width<=1920]+ba/b[height<=1920][width<=1920]/b",
        # H.264 and AAC first: Telegram needs them, and native ones skip re-encoding.
        "format_sort": ["vcodec:h264", "res:1080", "acodec:aac"],
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "allowed_extractors": _ALLOWED_EXTRACTORS,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "concurrent_fragment_downloads": 4,
        "max_filesize": config.max_file_size_bytes,
        # Also caps any unexpected playlist.
        "playlistend": MAX_POST_ITEMS,
        "match_filter": _skip_live,
    }
    # Instagram/Threads carousels are playlists.
    if site_of(url) in ("instagram", "threads"):
        opts["noplaylist"] = False
    if cookies := _cookies_for_run(config, work_dir):
        opts["cookiefile"] = cookies
    return opts


def _collect_media(work_dir: str) -> list[str]:
    """Final media files in the request folder."""
    files = []
    for path in sorted(glob.glob(os.path.join(work_dir, "*"))):
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext in _SKIP_EXTS or _INTERMEDIATE_RE.search(os.path.basename(path)):
            continue
        if ext in VIDEO_EXTS or ext in PHOTO_EXTS or ext in AUDIO_EXTS:
            files.append(path)
    return files


def _probe_video(path: str) -> dict:
    """Video codec, pix_fmt, size and duration, plus the audio codec (ffprobe)."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type,codec_name,pix_fmt,width,height,duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=30,
        ).stdout
        streams = json.loads(out)["streams"]
        s = next(st for st in streams if st.get("codec_type") == "video")
        audio = next((st for st in streams if st.get("codec_type") == "audio"), {})
        dur = s.get("duration")
        return {
            "codec": s.get("codec_name"),
            "pix": s.get("pix_fmt"),
            "acodec": audio.get("codec_name"),
            "w": _to_int(s.get("width")),
            "h": _to_int(s.get("height")),
            "dur": int(float(dur)) if dur else None,
        }
    except Exception:  # noqa: BLE001
        return {}


def _ensure_telegram_playable(path: str) -> str:
    """Converts a video to what Telegram plays everywhere: 8-bit H.264 + AAC/MP3 in MP4.

    Fit files are left alone; only the failing stream is re-encoded.
    Returns the final .mp4 path.
    """
    meta = _probe_video(path)
    video_ok = meta.get("codec") == _TELEGRAM_VCODEC and meta.get("pix") in _TELEGRAM_PIX
    audio_ok = meta.get("acodec") in (None, *_TELEGRAM_ACODECS)  # None: no audio
    base, ext = os.path.splitext(path)
    if video_ok and audio_ok and ext.lower() == ".mp4":
        return path

    out = base + ".tg.mp4"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
        *(["-c:v", "copy"] if video_ok else _H264_ARGS),
        *(["-c:a", "copy"] if audio_ok else _AAC_ARGS),
        "-movflags", "+faststart",
        out,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True)
    except Exception:  # noqa: BLE001
        logger.warning("Couldn't convert %s to H.264/AAC, sending as is",
                       os.path.basename(path), exc_info=True)
        if os.path.exists(out):
            os.remove(out)
        return path
    final = base + ".mp4"
    os.replace(out, final)
    if final != path:
        os.remove(path)
    return final


def _download_blocking(url: str, config: Config, work_dir: str) -> DownloadResult:
    url = _resolve_share_link(_normalize_url(url))
    # autonumber keeps the carousel order
    out_template = os.path.join(work_dir, "%(autonumber)03d-%(id)s.%(ext)s")
    opts = _build_opts(config, out_template, url, work_dir)

    info = _extract_info(opts, url)
    _reject_live(info)

    files = _collect_media(work_dir)
    if not files:
        raise DownloadError(
            f"Nothing came through — the video is probably over the "
            f"{config.max_file_size_mb} MB limit."
        )

    # Item metadata by id, for track tags (file name: NNN-<id>.<ext>).
    entries = {e["id"]: e for e in info.get("entries") or [info] if e and e.get("id")}
    items = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        is_video = ext in VIDEO_EXTS
        item = MediaItem(path=f, is_video=is_video, is_audio=ext in AUDIO_EXTS)
        if item.is_audio:
            entry = entries.get(os.path.splitext(os.path.basename(f))[0].split("-", 1)[-1]) or {}
            item.title = entry.get("track") or entry.get("title")
            item.performer = entry.get("artist")
            item.duration = _to_int(entry.get("duration"))
        if is_video:
            item.path = _ensure_telegram_playable(f)
            meta = _probe_video(item.path)
            item.width = meta.get("w")
            item.height = meta.get("h")
            item.duration = meta.get("dur")
        items.append(item)

    total = sum(os.path.getsize(i.path) for i in items)
    if total > config.max_file_size_bytes:
        for i in items:
            try:
                os.remove(i.path)
            except OSError:
                pass
        raise DownloadError(
            f"This is {total / 1024 / 1024:.0f} MB — over the "
            f"{config.max_file_size_mb} MB limit."
        )

    # Carousels may keep the caption only in their items.
    description = info.get("description") or ""
    if not description:
        for entry in info.get("entries") or []:
            if entry and entry.get("description"):
                description = entry["description"]
                break

    return DownloadResult(
        items=items,
        title=info.get("title") or "video",
        description=description,
        total_size=total,
    )


async def download(url: str, config: Config, work_dir: str) -> DownloadResult:
    """yt-dlp blocks, so it runs in a thread."""
    return await asyncio.to_thread(_download_blocking, url, config, work_dir)


# ── Cutting: only the requested pieces are downloaded ───────────────────────


@dataclass
class ClipsResult:
    duration: int | None = None
    # Range index → clip or error. Ranges starting past the end are in neither.
    clips: dict[int, MediaItem] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)


def _clip_sections(ranges: list[tuple[int, int]]) -> Callable:
    """yt-dlp download_ranges without ranges starting past the end (they break ffmpeg)."""
    def sections(info: dict, ydl: yt_dlp.YoutubeDL) -> list[dict]:
        dur = info.get("duration")
        return [
            {"start_time": start, "end_time": end, "index": i}
            for i, (start, end) in enumerate(ranges)
            if not (dur and start >= dur)
        ]
    return sections


def _download_clips_blocking(
    url: str,
    config: Config,
    work_dir: str,
    ranges: list[tuple[int, int]],
) -> ClipsResult:
    """Downloads only the requested pieces, not the whole video.

    ffmpeg reads just each span; force_keyframes_at_cuts disables stream copy, so
    our output args encode a frame-accurate H.264/AAC clip directly.
    """
    url = _resolve_share_link(_normalize_url(url))
    out_template = os.path.join(work_dir, "clip-%(section_number)03d.%(ext)s")
    opts = _build_opts(config, out_template, url, work_dir)
    opts.pop("max_filesize", None)  # checked per piece below
    opts.update({
        "noplaylist": True,
        "download_ranges": _clip_sections(ranges),
        "force_keyframes_at_cuts": True,
        "external_downloader_args": {
            # -rw_timeout (µs): a stalled connection fails ffmpeg instead of hanging.
            "ffmpeg_i": ["-rw_timeout", "30000000"],
            "ffmpeg_o": [*_H264_ARGS, *_AAC_ARGS, "-movflags", "+faststart"],
        },
        # One failed piece doesn't stop the others.
        "ignoreerrors": "only_download",
    })

    info = _extract_info(opts, url)
    _reject_live(info)

    result = ClipsResult(duration=_to_int(info.get("duration")))
    for i, (start, _end) in enumerate(ranges):
        if result.duration and start >= result.duration:
            continue
        path = next((p for p in _collect_media(work_dir)
                     if os.path.basename(p).startswith(f"clip-{i:03d}.")), None)
        if not path:
            result.errors[i] = "couldn't cut this piece"
            continue
        if os.path.getsize(path) == 0:
            os.remove(path)
            result.errors[i] = "came out empty, the timecodes are outside the video"
            continue
        # Cheap guard for odd sources (WebM without mp4 formats).
        path = _ensure_telegram_playable(path)
        if os.path.getsize(path) > config.max_file_size_bytes:
            os.remove(path)
            result.errors[i] = f"over the {config.max_file_size_mb} MB limit"
            continue
        meta = _probe_video(path)
        result.clips[i] = MediaItem(path=path, is_video=True, width=meta.get("w"),
                                    height=meta.get("h"), duration=meta.get("dur"))
    return result


async def download_clips(
    url: str, config: Config, work_dir: str, ranges: list[tuple[int, int]],
) -> ClipsResult:
    return await asyncio.to_thread(_download_clips_blocking, url, config, work_dir, ranges)
