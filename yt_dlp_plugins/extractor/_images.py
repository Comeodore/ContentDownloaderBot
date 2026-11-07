"""Image picking shared by the Instagram and Threads plugins.

The leading "_" makes yt-dlp's plugin loader skip this module.
"""


def best_image(media, images):
    """Largest candidate in the original aspect ratio.

    Candidates come in pairs, the original and a square crop (972x1296 and
    972x972); yt-dlp's res sort would pick the crop.
    """
    def area(t):
        return (t.get('width') or 0) * (t.get('height') or 0)

    width, height = media.get('original_width'), media.get('original_height')
    if width and height:
        ratio = width / height
        original = [
            t for t in images if t.get('width') and t.get('height')
            and abs(t['width'] / t['height'] - ratio) < 0.01
        ]
        if original:
            return max(original, key=area)
    return max(images, key=area)
