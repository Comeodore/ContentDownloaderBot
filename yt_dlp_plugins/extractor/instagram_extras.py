"""Overrides of yt-dlp's Instagram extractors: photos, stories, dead sessions.

They replace InstagramIE and InstagramStoryIE (named "+videobot"); everything
else stays yt-dlp's.

Photos: yt-dlp takes only video, so photo posts fail and carousel/story photos
drop out. Every path builds media via _extract_product_media, where media
without video gets a photo format.

Stories: yt-dlp fetches all of the author's stories (feed/reels_media), which
answers HTTP 500 under a live session (verified 2026-10-22). A shared link's
number is the media pk, and media/<pk>/info/ returns that one story. An expired,
deleted or hidden one answers 400 "Media not found or unavailable" (verified
2026-11-05); falling back would end in "You need to log in" and a false cookie
alarm, so it gets its own "story is gone" error.

Dead session: the API redirects to /accounts/login/ with a 200 HTML page
(verified 2026-11-06), which yt-dlp misreads as "Failed to parse JSON". We raise
"session expired" instead; no guest fallback, since the bot only uses the
session after the guest attempt failed (downloader._extract_info).

Native H.264: a guest gets 1080p only as VP9 DASH, and the progressive mp4s
(H.264 720p) come without codec or size (verified 2026-11-08). The bot then took
the VP9 and re-encoded it for Telegram: 10-55 s per reel and a file 4x the
source. _label_native_h264 probes the progressive file, so the bot's H.264-first
format_sort picks it and nothing is re-encoded.
"""
import json
import re
import subprocess
from urllib.parse import urlsplit

from yt_dlp.extractor.instagram import InstagramIE, InstagramStoryIE
from yt_dlp.utils import ExtractorError, determine_ext, int_or_none, traverse_obj, url_or_none

from yt_dlp_plugins.extractor._images import best_image

# The native H.264 must be at least this share of the best VP9's short side.
# Instagram's ladder goes 1080 -> 720 (0.67); a smaller one isn't worth the
# lost sharpness, and the VP9 is re-encoded as before.
_MIN_NATIVE_SCALE = 0.6
_CDN_HOSTS = ('.cdninstagram.com', '.fbcdn.net')
_H264_RE = re.compile(r'(?:avc|h264)', re.IGNORECASE)


def _probe_stream(url):
    """Codecs and size of a progressive file (ffprobe), or None."""
    parts = urlsplit(url)
    # The URL comes from the site's JSON, and ffprobe also reads file:, concat: etc.
    if parts.scheme != 'https' or not (parts.hostname or '').endswith(_CDN_HOSTS):
        return None
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-protocol_whitelist', 'https,tls,tcp',
             '-show_entries', 'stream=codec_type,codec_name,width,height',
             '-of', 'json', url],
            capture_output=True, text=True, timeout=20, check=True).stdout
        streams = json.loads(out)['streams']
    except Exception:  # noqa: BLE001
        return None
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if not video or not video.get('codec_name'):
        return None
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    return {
        'vcodec': video['codec_name'],
        'acodec': (audio or {}).get('codec_name') or 'none',
        'width': int_or_none(video.get('width')),
        'height': int_or_none(video.get('height')),
    }


def _short_side(fmt):
    return min(fmt.get('width') or 0, fmt.get('height') or 0)


def _label_native_h264(info, probe=_probe_stream):
    """Fills codecs and size of unlabelled progressive formats that are usable H.264.

    Only when no DASH video is H.264 already: otherwise there's nothing to gain.
    """
    formats = info.get('formats') or []
    videos = [f for f in formats if f.get('vcodec') not in (None, 'none')]
    if not videos or any(_H264_RE.match(f['vcodec']) for f in videos):
        return info
    best = max(_short_side(f) for f in videos)
    probed = {}
    for f in formats:
        if f.get('vcodec') is not None or not f.get('url'):
            continue
        if f['url'] not in probed:
            probed[f['url']] = probe(f['url'])
        meta = probed[f['url']]
        if (meta and _H264_RE.match(meta['vcodec'])
                and _short_side(meta) >= best * _MIN_NATIVE_SCALE):
            f.update(meta)
    return info


def _refuse_login_redirect(ie, url_or_request, urlh, logged_in):
    """A logged-in API request that lands on the login page means the session is dead."""
    url = getattr(url_or_request, 'url', url_or_request)
    if logged_in and urlh and '/api/v1/' in str(url) and ie._is_login_redirect(urlh.url):
        raise ExtractorError(
            'Instagram session expired: the API sent it to the login page', expected=True)
    return urlh


def _with_photo_format(info, product_media):
    """Give media without video a photo format."""
    if info.get('formats'):
        return info
    images = traverse_obj(product_media, (
        'image_versions2', 'candidates', lambda _, v: url_or_none(v['url'])))
    if not images:
        return info
    # Guest candidates have no sizes, but the original comes first and
    # best_image keeps the first among equals.
    best = best_image(product_media, images)
    info['formats'] = [{
        'format_id': 'photo',
        'url': best['url'],
        'ext': determine_ext(best['url'], 'jpg'),
        'width': best.get('width'),
        'height': best.get('height'),
    }]
    return info


class _InstagramIE(InstagramIE, plugin_name='videobot'):
    def _extract_product_media(self, product_media):
        return _with_photo_format(
            _label_native_h264(super()._extract_product_media(product_media)), product_media)

    def _request_webpage(self, url_or_request, *args, **kwargs):
        # Read before the request: Set-Cookie on the way to login may drop sessionid.
        logged_in = self._is_logged_in
        return _refuse_login_redirect(
            self, url_or_request, super()._request_webpage(url_or_request, *args, **kwargs),
            logged_in)


class _InstagramStoryIE(InstagramStoryIE, plugin_name='videobot'):
    def _extract_product_media(self, product_media):
        return _with_photo_format(
            _label_native_h264(super()._extract_product_media(product_media)), product_media)

    def _request_webpage(self, url_or_request, *args, **kwargs):
        logged_in = self._is_logged_in
        return _refuse_login_redirect(
            self, url_or_request, super()._request_webpage(url_or_request, *args, **kwargs),
            logged_in)

    def _real_extract(self, url):
        username, story_id = self._match_valid_url(url).group('user', 'id')
        # Without a session stories aren't served at all; let yt-dlp say so.
        if story_id and username != 'highlights' and self._is_logged_in:
            info = self._download_json(
                f'{self._API_BASE_URL}/media/{story_id}/info/', story_id,
                'Downloading story info', 'Story info request failed', fatal=False,
                expected_status=400, headers=self._api_headers,
                impersonate=self._can_impersonate and self._is_web_app)
            item = traverse_obj(info, ('items', 0, {dict}))
            if item:
                return {
                    **self._extract_product(item, get_comments=False),
                    'title': f'Story by {username}',
                }
            if 'not found or unavailable' in str(traverse_obj(info, 'message') or '').lower():
                # No "login"/"private" in the text, or the bot reads it as a cookie problem.
                raise ExtractorError(
                    'Instagram story is gone: expired, deleted or hidden', expected=True,
                    video_id=story_id)
        return super()._real_extract(url)
