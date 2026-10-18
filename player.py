"""YouTube metadata and direct-stream helpers."""

from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError


FORMAT_SELECTOR = "best[ext=mp4]/best"
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


class PlayerError(Exception):
    """Base class for errors that can be shown to a viewer."""


class InvalidURLError(PlayerError):
    """The submitted URL is not a supported YouTube URL."""


class VideoUnavailableError(PlayerError):
    """The video is private, removed, or has no usable format."""


class NetworkError(PlayerError):
    """yt-dlp could not reach YouTube or retrieve video metadata."""


def validate_url(url):
    """Return a normalized URL if it points to a supported YouTube host."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidURLError("Enter a YouTube video URL.")

    value = url.strip()
    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as error:
        raise InvalidURLError("Enter a valid YouTube video URL.") from error
    if (
        parsed.scheme not in {"http", "https"}
        or hostname not in YOUTUBE_HOSTS
        or port not in (None, 80 if parsed.scheme == "http" else 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise InvalidURLError("Enter a valid YouTube video URL.")
    return value


def parse_range_header(value):
    """Parse one byte range as (start, end), allowing open and suffix ranges."""
    if value is None:
        return None

    if not isinstance(value, str) or not value.startswith("bytes=") or "," in value:
        raise ValueError("Only a single byte range is supported.")

    range_value = value[6:].strip()
    if "-" not in range_value:
        raise ValueError("Malformed byte range.")

    start_value, end_value = range_value.split("-", 1)
    try:
        start = int(start_value) if start_value else None
        end = int(end_value) if end_value else None
    except ValueError as error:
        raise ValueError("Malformed byte range.") from error

    if (start is None and (end is None or end <= 0)) or (start is not None and start < 0):
        raise ValueError("Malformed byte range.")
    if start is not None and end is not None and end < start:
        raise ValueError("Malformed byte range.")
    return start, end


def _extract_info(url, format_selector=FORMAT_SELECTOR):
    validated_url = validate_url(url)
    options = {
        "format": format_selector,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(validated_url, download=False)
    except DownloadError as error:
        message = str(error).strip() or error.__class__.__name__
        lowered = message.lower()
        if any(word in lowered for word in ("private", "unavailable", "removed", "not available")):
            raise VideoUnavailableError(message) from error
        if any(word in lowered for word in ("network", "connection", "timed out", "unable to download")):
            raise NetworkError(message) from error
        raise VideoUnavailableError(message) from error
    except (OSError, TimeoutError) as error:
        message = str(error).strip() or error.__class__.__name__
        raise NetworkError(message) from error

    if not info or info.get("_type") == "playlist":
        raise VideoUnavailableError("This URL did not resolve to an available video.")
    return info


def _progressive_mp4_formats(info):
    formats = []
    for item in info.get("formats", []):
        if (
            item.get("ext") == "mp4"
            and item.get("vcodec") not in (None, "none")
            and item.get("acodec") not in (None, "none")
            and item.get("url")
            and item.get("protocol") not in {"m3u8", "m3u8_native", "http_dash_segments"}
        ):
            formats.append(item)
    return sorted(
        formats,
        key=lambda item: (item.get("height") or 0, item.get("tbr") or 0),
    )


def get_info(url):
    """Return video details and available progressive MP4 quality choices."""
    info = _extract_info(url)
    formats = _progressive_mp4_formats(info)
    if not formats:
        raise VideoUnavailableError("This video has no progressive MP4 format that can be played here.")

    return {
        "title": info.get("title") or "Untitled video",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "formats": [
            {
                "format_id": item["format_id"],
                "height": item.get("height"),
                "format_note": item.get("format_note"),
                "filesize": item.get("filesize") or item.get("filesize_approx"),
            }
            for item in formats
        ],
    }


def get_stream_url(url, quality="best"):
    """Return a direct media URL and request headers without downloading it."""
    info = _extract_info(url)
    formats = _progressive_mp4_formats(info)
    if not formats:
        raise VideoUnavailableError("This video has no progressive MP4 format that can be played here.")

    if quality == "best":
        selected = formats[-1]
    else:
        selected = next((item for item in formats if item.get("format_id") == quality), None)
        if selected is None:
            raise VideoUnavailableError("The selected video quality is not available.")

    headers = {**(info.get("http_headers") or {}), **(selected.get("http_headers") or {})}
    return selected["url"], headers