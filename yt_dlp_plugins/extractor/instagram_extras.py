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
"""
from yt_dlp.extractor.instagram import InstagramIE, InstagramStoryIE
from yt_dlp.utils import ExtractorError, determine_ext, traverse_obj, url_or_none

from yt_dlp_plugins.extractor._images import best_image


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
        return _with_photo_format(super()._extract_product_media(product_media), product_media)

    def _request_webpage(self, url_or_request, *args, **kwargs):
        # Read before the request: Set-Cookie on the way to login may drop sessionid.
        logged_in = self._is_logged_in
        return _refuse_login_redirect(
            self, url_or_request, super()._request_webpage(url_or_request, *args, **kwargs),
            logged_in)


class _InstagramStoryIE(InstagramStoryIE, plugin_name='videobot'):
    def _extract_product_media(self, product_media):
        return _with_photo_format(super()._extract_product_media(product_media), product_media)

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
