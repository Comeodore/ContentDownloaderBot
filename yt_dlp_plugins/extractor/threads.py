"""yt-dlp extractor plugin for Threads (threads.com / threads.net).

yt-dlp has no Threads support yet (PR yt-dlp/yt-dlp#9852 is unmerged). This is
that PR adapted to the plugin system, plus photos, media from quoted/reposted
posts and inline Instagram reels, and unavailable-post detection. If upstream
gains an extractor, drop this file, but check it handles photos first.

yt-dlp finds it because the bot runs from the project root, which holds the
`yt_dlp_plugins` package.

The bot runs it as a guest only (downloader._GUEST_ONLY_SITES); the session
paths below serve manual runs with cookies.
"""
import json
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    decode_base_n,
    determine_ext,
    parse_qs,
    remove_end,
    strip_or_none,
    traverse_obj,
)

from yt_dlp_plugins.extractor._images import best_image

_BASE_URL = 'https://www.threads.com/'
# Threads web client's X-IG-App-ID; without it the private API redirects to login.
# Found on any threads.com page (RelayAPIConfigDefaults.customHeaders).
_WEB_APP_ID = '238260118697367'
# Shortcode alphabet: a post code is its numeric pk in base64url.
_CODE_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'


# A quote or repost has no media of its own (media_type 19, text); the video is
# in the source post, under a different key for each kind of share.
_SHARED_POST_KEYS = ('quoted_attachment_post', 'quoted_post', 'reposted_post')

# A post Threads won't show this visitor comes as an error component in place of
# the post data, with the reason as its props (verified 2026-11-11 on an
# audience-restricted post: "This content isn't available to everyone").
_BLOCKED_RE = re.compile(
    r'GeoBlockedErrorRoot\.react"\},"props":\{"title":("(?:[^"\\]|\\.)*"),'
    r'"description":("(?:[^"\\]|\\.)*")')


class ThreadsIE(InfoExtractor):
    # /t/<code> is the old short form; Threads 301s it to /@user/post/<code>.
    _VALID_URL = r'https?://(?:www\.)?threads\.(?:com|net)/(?:(?P<uploader>[^/?#]+)/post|t)/(?P<id>[^/?#&]+)'

    _TESTS = [{
        'url': 'https://www.threads.com/@tntsportsbr/post/C6cqebdCfBi',
        'info_dict': {
            'id': 'C6cqebdCfBi',
            'ext': 'mp4',
            'title': 'md5:062673d04195aa2d99b8d7a11798cb9d',
            'uploader_id': 'tntsportsbr',
        },
    }, {
        # Text plus an Instagram reel link: the video is in linked_inline_media.
        'url': 'https://www.threads.com/@zhanginsergei/post/DfNaDpOgK4s',
        'info_dict': {
            'id': 'DfNaDpOgK4s',
            'ext': 'mp4',
            'uploader_id': 'zhanginsergei',
        },
    }]

    def _media_entry(self, media, post_code):
        """Entry for one media item: video, or the largest photo."""
        images = [
            t for t in traverse_obj(media, ('image_versions2', 'candidates')) or []
            if t.get('url')
        ]
        formats = [{
            'format_id': '{}-{}'.format(media.get('pk'), video.get('type')),
            'url': video['url'],
            'width': media.get('original_width'),
            'height': media.get('original_height'),
        } for video in media.get('video_versions') or []]

        if formats:
            thumbnails = [
                {'url': t['url'], 'width': t.get('width'), 'height': t.get('height')}
                for t in images
            ]
            return {
                'id': str(media.get('pk') or post_code),
                'formats': formats,
                'thumbnails': thumbnails,
            }

        if not images:
            return None  # text post (media_type 19)
        best = best_image(media, images)
        return {
            'id': str(media.get('pk') or post_code),
            'formats': [{
                'format_id': 'photo',
                'url': best['url'],
                'ext': determine_ext(best['url'], 'jpg'),
                'width': best.get('width'),
                'height': best.get('height'),
            }],
        }

    def _entries(self, post, post_code):
        return [
            entry for media in post.get('carousel_media') or [post]
            if (entry := self._media_entry(media, post_code))
        ]

    @staticmethod
    def _shared_post(post):
        """The quoted/reposted source post, and whether it is unavailable.

        Logged out, Threads sets *_unavailable and strips the media, so the post
        looks like plain text. Verified: the same URL with a valid session
        returns the full quoted_attachment_post with video_versions.
        """
        share_info = traverse_obj(post, ('text_post_app_info', 'share_info')) or {}
        for key in _SHARED_POST_KEYS:
            if shared := share_info.get(key):
                return shared, False
        return None, any(share_info.get(f'{key}_unavailable') for key in _SHARED_POST_KEYS)

    def _find_post(self, webpage, video_id):
        """Find the post object in the page's <script> JSON.

        It sits either in result.data.media or in
        result.data.data.edges[].node.thread_items[].post. "<" is escaped
        inside the JSON, so [^<]* doesn't cut it short.
        """
        for mobj in re.finditer(
                rf'<script[^>]+>([^<]*"code":"{re.escape(video_id)}"[^<]*)</script>', webpage):
            result = self._search_json(
                r'"result":', mobj.group(1), 'result data', video_id, default=None)
            if not result:
                continue
            candidates = [traverse_obj(result, ('data', 'media'))]
            for node in traverse_obj(result, ('data', 'data', 'edges')) or []:
                for item in traverse_obj(node, ('node', 'thread_items')) or []:
                    candidates.append(item.get('post'))
            post = next((c for c in candidates if c and c.get('code') == video_id), None)
            if post:
                return post
        return None

    @staticmethod
    def _blocked_reason(webpage):
        """Threads' reason for serving an error page instead of the post, or None."""
        mobj = _BLOCKED_RE.search(webpage)
        if mobj:
            return ': '.join(filter(None, (json.loads(g).strip() for g in mobj.groups())))
        if 'GeoBlockedErrorRoot' in webpage:
            # A new layout of the error page: worth a look, so no access wording.
            return 'Threads served an error page instead of the post'
        return None

    def _has_session(self):
        return bool(self._get_cookies(_BASE_URL).get('sessionid'))

    @staticmethod
    def _pk_from_code(code):
        try:
            return decode_base_n(code, table=_CODE_CHARS)
        except (ValueError, KeyError):  # KeyError: a character outside the alphabet
            return None

    def _api_post(self, video_id, pk=None):
        """Post object from the private API (needs a session; same shape as the HTML one)."""
        pk = pk or self._pk_from_code(video_id)
        if not pk:
            return None
        data = self._download_json(
            f'{_BASE_URL}api/v1/media/{pk}/info/', video_id,
            note='Downloading post from API', errnote='Post API request failed',
            fatal=False, headers={'X-IG-App-ID': _WEB_APP_ID, 'Referer': _BASE_URL})
        post = traverse_obj(data, ('items', 0))
        # A wrong pk (e.g. from a junk redirect) would silently return another post.
        return post if post and post.get('code') == video_id else None

    def _webpage_post(self, url, video_id):
        """Post object from the page HTML, plus the page for metadata."""
        # Threads sometimes serves a page without post data; one retry usually fixes it.
        for attempt in range(2):
            webpage, urlh = self._download_webpage_handle(
                url, video_id,
                note='Downloading webpage' if not attempt else 'Retrying webpage (no post data)')
            # An unavailable post (deleted / hidden from logged-out visitors) is
            # not a 404: Threads redirects to /?error=invalid_post with a 200 feed.
            if video_id not in urlh.url:
                # Logged in, a redirect to the feed is normal: the media pk is in
                # injected_media_ids.
                injected = traverse_obj(parse_qs(urlh.url), ('injected_media_ids', 0)) or ''
                pk = self._search_regex(r'\d{6,}', injected, 'media id', default=None)
                post = self._api_post(video_id, pk) if pk else None
                if post:
                    return post, None
                raise ExtractorError(
                    'This post is unavailable — it may have been deleted, '
                    'or it is hidden from logged-out visitors', expected=True)
            post = self._find_post(webpage, video_id)
            if post:
                return post, webpage
            if reason := self._blocked_reason(webpage):
                raise ExtractorError(reason, expected=True)
        return None, webpage

    def _real_extract(self, url):
        video_id = self._match_id(url)

        # Logged in, Threads doesn't render the post page: it 302s to the feed
        # and serves media only via the private API. So with a session, go to
        # the API first.
        webpage = None
        post = self._api_post(video_id) if self._has_session() else None
        if not post:
            post, webpage = self._webpage_post(url, video_id)
        if not post:
            raise ExtractorError('Could not find post data', expected=True)

        username = traverse_obj(post, ('user', 'username'))
        # caption is complete and present on both paths; <title> only on the page.
        caption = strip_or_none(traverse_obj(post, ('caption', 'text')))
        page_title = strip_or_none(
            remove_end(self._html_extract_title(webpage), '• Threads')) if webpage else None
        metadata = {
            'title': caption or page_title,
            'description': caption or (
                self._og_search_description(webpage, default=None) if webpage else None),
            'uploader': traverse_obj(post, ('user', 'full_name')) or username,
            'uploader_id': username,
            'uploader_url': f'https://www.threads.com/@{username}' if username else None,
            'channel': username,
            'channel_is_verified': traverse_obj(post, ('user', 'is_verified')),
            'timestamp': post.get('taken_at'),
            'like_count': post.get('like_count'),
        }

        entries = self._entries(post, video_id)
        if not entries:
            # No own media: a text post, a quote/repost holding the video, or a
            # linked Instagram reel that Threads embeds as linked_inline_media.
            shared, unavailable = self._shared_post(post)
            linked = traverse_obj(post, ('text_post_app_info', 'linked_inline_media'))
            if shared:
                entries = self._entries(shared, video_id)
            if not entries and linked:
                entries = self._entries(linked, video_id)
            if not entries and unavailable:
                raise ExtractorError(
                    'The quoted post holding the media is unavailable — it may have '
                    'been deleted, or it is not shown to logged-out visitors',
                    expected=True)
        if not entries:
            raise ExtractorError('This Threads post has no photo or video', expected=True)

        if len(entries) == 1:
            return {**metadata, **entries[0], 'id': video_id}
        return self.playlist_result(
            [{**metadata, **e} for e in entries], playlist_id=video_id,
            playlist_title=metadata['title'])
