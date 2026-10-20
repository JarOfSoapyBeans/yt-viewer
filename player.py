"""YouTube metadata and cached MP4 download helpers."""

import json
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import DownloadError


FORMAT_SELECTOR = "bv*[vcodec^=avc1][height<=720]+ba[ext=m4a]/b[ext=mp4]/b"
QUALITIES = (360, 480, 720)
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_MAX_AGE_SECONDS = 60 * 60
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
SEARCH_CACHE_TTL_SECONDS = 10 * 60
_search_cache = {}
_search_cache_lock = threading.Lock()


class PlayerError(Exception):
    """Base class for errors that can be shown to a viewer."""


class InvalidURLError(PlayerError):
    """The submitted URL is not a supported YouTube URL."""


class VideoUnavailableError(PlayerError):
    """yt-dlp could not retrieve a playable video."""


class NetworkError(PlayerError):
    """yt-dlp could not reach YouTube or retrieve video metadata."""


class FFmpegNotFoundError(PlayerError):
    """ffmpeg is required to merge video and audio streams."""


def validate_url(url):
    """Return a normalized URL if it points to a supported YouTube host."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidURLError("Enter a YouTube video ID or URL.")

    value = url.strip()
    if VIDEO_ID_PATTERN.fullmatch(value):
        return f"https://www.youtube.com/watch?v={value}"
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


def validate_search_query(query):
    """Validate and return a trimmed YouTube search query."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Enter a search query.")
    value = query.strip()
    if len(value) > 200:
        raise ValueError("Search queries must be 200 characters or fewer.")
    return value


def search_videos(query, limit=12, skip=0):
    """Search YouTube metadata without downloading media."""
    query = validate_search_query(query)
    try:
        limit = int(limit)
    except (TypeError, ValueError) as error:
        raise ValueError("Search limit must be a positive integer.") from error
    if limit < 1:
        raise ValueError("Search limit must be a positive integer.")
    if limit > 96:
        raise ValueError("Search limit must be 96 or fewer.")
    try:
        skip = int(skip)
    except (TypeError, ValueError) as error:
        raise ValueError("Search offset must be a non-negative integer.") from error
    if skip < 0 or skip > limit:
        raise ValueError("Search offset must be between zero and the search limit.")

    cache_key = (query, limit)
    now = time.monotonic()
    reusable_entries = []
    larger_entries = None
    with _search_cache_lock:
        for expired_key, (cached_at, _) in list(_search_cache.items()):
            if now - cached_at >= SEARCH_CACHE_TTL_SECONDS:
                del _search_cache[expired_key]
        cached = _search_cache.get(cache_key)
        if cached and now - cached[0] < SEARCH_CACHE_TTL_SECONDS:
            return [dict(video) for video in cached[1][skip:]]
        larger = [
            (cached_limit, entries)
            for (cached_query, cached_limit), (_, entries) in _search_cache.items()
            if cached_query == query and cached_limit > limit
        ]
        if larger:
            larger_entries = min(larger, key=lambda item: item[0])[1]
        reusable = [
            (cached_limit, entries)
            for (cached_query, cached_limit), (_, entries) in _search_cache.items()
            if cached_query == query and cached_limit < limit
        ]
        if reusable:
            reusable_entries = [dict(video) for video in max(reusable, key=lambda item: item[0])[1]]

    if larger_entries is not None:
        return [dict(video) for video in larger_entries[skip:]]

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            result = downloader.extract_info(f"ytsearch{limit}:{query}", download=False)
    except DownloadError as error:
        raise VideoUnavailableError(str(error).strip() or error.__class__.__name__) from error
    except (OSError, TimeoutError) as error:
        raise NetworkError(str(error).strip() or error.__class__.__name__) from error

    extracted_videos = []
    seen_ids = set()
    for entry in (result or {}).get("entries") or []:
        if not entry:
            continue
        video_id = entry.get("id")
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        extracted_videos.append(
            {
                "id": video_id,
                "title": entry.get("title") or "Untitled video",
                "channel": entry.get("channel") or entry.get("uploader") or "Unknown channel",
                "duration": entry.get("duration"),
                "view_count": entry.get("view_count"),
                "upload_date": entry.get("upload_date"),
                "thumbnail": entry.get("thumbnail")
                or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            }
        )

    videos = []
    seen_ids.clear()
    for video in (*reusable_entries, *extracted_videos):
        if video["id"] not in seen_ids:
            seen_ids.add(video["id"])
            videos.append(video)
        if len(videos) >= limit:
            break

    with _search_cache_lock:
        _search_cache[cache_key] = (time.monotonic(), videos)
    return [dict(video) for video in videos[skip:]]


def normalize_quality(quality):
    try:
        height = int(str(quality).lower().removesuffix("p"))
    except (TypeError, ValueError) as error:
        raise ValueError("Choose 360p, 480p, or 720p.") from error
    if height not in QUALITIES:
        raise ValueError("Choose 360p, 480p, or 720p.")
    return height


def format_selector(quality=720):
    height = normalize_quality(quality)
    selector = FORMAT_SELECTOR.replace("height<=720", f"height<={height}")
    if height < 720:
        selector = selector.replace(
            "/b[ext=mp4]/b",
            f"/b[ext=mp4][height<={height}]/b[height<={height}]",
        )
    return selector


def ffmpeg_path():
    return shutil.which("ffmpeg")


def require_ffmpeg():
    if not ffmpeg_path():
        raise FFmpegNotFoundError("ffmpeg is required to prepare video. Install ffmpeg and restart the viewer.")


def clean_cache(now=None):
    """Delete cached files and partial downloads older than one hour."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = (time.time() if now is None else now) - CACHE_MAX_AGE_SECONDS
    removed = 0
    for path in CACHE_DIR.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed


def cache_path(video_id, quality=720):
    height = normalize_quality(quality)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(video_id))
    return CACHE_DIR / f"{safe_id}_{height}p.mp4"


def video_id_from_url(url):
    parsed = urlparse(validate_url(url))
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0] or None

    query_id = parse_qs(parsed.query).get("v", [None])[0]
    if query_id:
        return query_id
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[0] in {"embed", "live", "shorts", "v"}:
        return path_parts[1]
    return None


def cached_video_info(output_path, video_id):
    fallback = {
        "id": video_id,
        "title": video_id,
        "duration": None,
        "thumbnail": None,
        "channel": None,
        "view_count": None,
        "upload_date": None,
    }
    metadata_path = output_path.with_suffix(".json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if not isinstance(metadata, dict) or metadata.get("id") != video_id:
        return fallback
    return {**fallback, **metadata}


def _extract_info(url):
    validated_url = validate_url(url)
    options = {"quiet": True, "no_warnings": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(validated_url, download=False)
    except DownloadError as error:
        message = str(error).strip() or error.__class__.__name__
        lowered = message.lower()
        if any(word in lowered for word in ("network", "connection", "timed out", "unable to download")):
            raise NetworkError(message) from error
        raise VideoUnavailableError(message) from error
    except (OSError, TimeoutError) as error:
        message = str(error).strip() or error.__class__.__name__
        raise NetworkError(message) from error

    if not info or info.get("_type") == "playlist":
        raise VideoUnavailableError("This URL did not resolve to an available video.")
    return info


def get_info(url):
    """Return video metadata without downloading media."""
    info = _extract_info(url)
    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled video",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "channel": info.get("channel") or info.get("uploader"),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
    }


def download_video(url, quality=720, progress_callback=None):
    """Download and merge a video into the quality-specific MP4 cache file."""
    validated_url = validate_url(url)
    height = normalize_quality(quality)
    clean_cache()
    video_id = video_id_from_url(validated_url)
    if video_id:
        cached_path = cache_path(video_id, height)
        if cached_path.is_file():
            return {**cached_video_info(cached_path, video_id), "path": cached_path, "cached": True}

    require_ffmpeg()
    info = get_info(validated_url)
    video_id = info.get("id")
    if not video_id:
        raise VideoUnavailableError("yt-dlp did not return a video ID for this URL.")

    output_path = cache_path(video_id, height)
    if output_path.is_file():
        return {**cached_video_info(output_path, video_id), "path": output_path, "cached": True}

    clean_cache()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def report(data):
        if not progress_callback:
            return
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            percent = min(95, int(downloaded * 95 / total)) if total else 0
            progress_callback({"progress": percent, "message": "Downloading video and audio..."})

    def report_postprocessing(data):
        if progress_callback and data.get("status") in {"started", "processing"}:
            progress_callback({"progress": 97, "message": "Merging streams into MP4..."})

    options = {
        "format": format_selector(height),
        "merge_output_format": "mp4",
        "outtmpl": str(CACHE_DIR / f"{output_path.stem}.%(ext)s"),
        "progress_hooks": [report],
        "postprocessor_hooks": [report_postprocessing],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.extract_info(validated_url, download=True)
    except DownloadError as error:
        message = str(error).strip() or error.__class__.__name__
        lowered = message.lower()
        if any(word in lowered for word in ("network", "connection", "timed out", "unable to download")):
            raise NetworkError(message) from error
        raise VideoUnavailableError(message) from error
    except (OSError, TimeoutError) as error:
        message = str(error).strip() or error.__class__.__name__
        raise NetworkError(message) from error

    if not output_path.is_file():
        raise VideoUnavailableError(f"yt-dlp completed but did not create the expected MP4: {output_path.name}")
    metadata_path = output_path.with_suffix(".json")
    metadata = {
        key: info.get(key)
        for key in ("id", "title", "duration", "thumbnail", "channel", "view_count", "upload_date")
    }
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    if progress_callback:
        progress_callback({"progress": 100, "message": "Video ready."})
    return {**info, "path": output_path, "cached": False}