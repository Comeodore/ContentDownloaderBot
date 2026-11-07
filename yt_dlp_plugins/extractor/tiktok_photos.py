"""yt-dlp extractor plugin for TikTok photo posts (slideshows).

yt-dlp's TikTok extractor doesn't match /photo/<id>, and the /photo/ page has no
post data (JS renders it). The same id opened as /video/<id> returns the whole
post with imagePost (verified on 1, 2 and 16 images), so we fetch it through
TikTokIE's own WAF handling. Images become a playlist (sent as an album), and
the slideshow music its last entry. A /photo/ link that is really a video goes
to the stock extractor.
"""
import unicodedata

from yt_dlp.extractor.tiktok import TikTokIE
from yt_dlp.utils import (
    determine_ext,
    mimetype2ext,
    parse_qs,
    strip_or_none,
    traverse_obj,
    url_or_none,
)


def _printable(text):
    """Text without unassigned/control characters, or None.

    TikTok author names contain characters like U+FFF4 that Telegram draws as boxes.
    """
    text = ''.join(ch for ch in text or '' if unicodedata.category(ch) not in ('Cn', 'Co', 'Cs', 'Cc'))
    return text.strip() or None


class TikTokPhotoIE(TikTokIE):
    IE_NAME = 'tiktok:photo'
    _VALID_URL = r'https?://(?:www\.)?tiktok\.com/@(?P<user_id>[\w.-]*)/photo/(?P<id>\d+)'

    _TESTS = [{
        'url': 'https://www.tiktok.com/@hullcity/photo/7553302113757990166',
        'info_dict': {'id': '7553302113757990166'},
        'playlist_count': 2,
    }, {
        # A video served under a /photo/ link
        'url': 'https://www.tiktok.com/@memezar/photo/7449708266168274208',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        video_id, user_id = self._match_valid_url(url).group('id', 'user_id')
        post_url = self._create_url(user_id, video_id)
        data, _ = self._extract_web_data_and_status(post_url, video_id)

        images = traverse_obj(data, (
            'imagePost', 'images', lambda _, v: url_or_none(v['imageURL']['urlList'][0])))
        if not images:
            return self.url_result(post_url, TikTokIE, video_id)

        description = strip_or_none(data.get('desc'))
        title = strip_or_none(traverse_obj(data, ('imagePost', 'title'))) or description
        metadata = traverse_obj(data, {
            'uploader': ('author', 'uniqueId', {str}),
            'uploader_id': ('author', 'id', {str}),
            'channel': ('author', 'nickname', {str}),
            'timestamp': ('createTime', {int}),
        })
        entries = []
        for index, image in enumerate(images, start=1):
            # urlList holds mirrors of the same JPEG.
            image_url = image['imageURL']['urlList'][0]
            entries.append({
                **metadata,
                'id': f'{video_id}-{index}',
                'title': title,
                'description': description,
                'formats': [{
                    'format_id': 'photo',
                    'url': image_url,
                    'ext': determine_ext(image_url, 'jpg'),
                    'width': image.get('imageWidth'),
                    'height': image.get('imageHeight'),
                }],
                'http_headers': {'Referer': 'https://www.tiktok.com/'},
            })

        if music_url := traverse_obj(data, ('music', 'playUrl', {url_or_none})):
            # The type comes from mime_type in the URL: without it the CDN serves
            # MP3, with mime_type=video_mp4 an audio-only MP4, i.e. M4A (verified).
            # No codecs set: an audio-only format would fail the bot's format filter.
            ext = traverse_obj(parse_qs(music_url), (
                'mime_type', -1, {lambda x: x.replace('_', '/')}, {mimetype2ext})) or 'mp3'
            ext = 'm4a' if ext == 'mp4' else ext
            track = _printable(traverse_obj(data, ('music', 'title', {str})))
            entries.append({
                **metadata,
                'id': f'{video_id}-music',
                'title': track or title,
                'track': track,
                'artist': _printable(traverse_obj(data, ('music', 'authorName', {str}))),
                'duration': traverse_obj(data, ('music', 'duration', {int})),
                'formats': [{'format_id': 'music', 'url': music_url, 'ext': ext}],
                'http_headers': {'Referer': 'https://www.tiktok.com/'},
            })
        return self.playlist_result(
            entries, video_id, title, description, **metadata)
