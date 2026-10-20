"""YouTube metadata and cached MP4 download helpers."""

import json
import logging
import re
import shutil
import threading
import time
from copy import deepcopy
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
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
INFO_CACHE_TTL_SECONDS = 30 * 60
ESTIMATED_BITRATES = {360: 600_000, 480: 1_100_000, 720: 2_400_000}
ARIA2C_PATH = shutil.which("aria2c")
_search_cache = {}
_search_cache_lock = threading.Lock()
_info_cache = {}
_info_cache_lock = threading.Lock()
_info_locks = {}
_info_locks_lock = threading.Lock()
_video_locks = {}
_video_locks_lock = threading.Lock()
_active_cache_paths = {}
_preparing_cache_paths = {}
_cache_activity_lock = threading.Lock()
logger = logging.getLogger(__name__)


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


class YouTubeHTTPError(PlayerError):
    """YouTube refused a request with an HTTP status that may be retried."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def _http_status_code(message):
    match = re.search(r"(?:HTTP\s+Error\s+|HTTP\s+|status(?:\s+code)?\s*[:= ]\s*)(403|429)\b", str(message), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _raise_for_youtube_http_error(error):
    message = str(error).strip() or error.__class__.__name__
    status_code = _http_status_code(message) or getattr(error, "code", None)
    if status_code in {403, 429}:
        raise YouTubeHTTPError(f"YouTube is refusing the request (HTTP {status_code}).", status_code) from error
    return message


def _clear_info_cache(video_id):
    with _info_cache_lock:
        _info_cache.pop(str(video_id), None)


def _remove_incomplete_video_files(video_id, quality):
    target = cache_path(video_id, quality)
    if not CACHE_DIR.exists():
        return
    for path in CACHE_DIR.glob(f"{target.stem}*"):
        with _cache_activity_lock:
            if path.is_file() and not _cache_path_is_protected(path):
                try:
                    path.unlink()
                except OSError:
                    logger.warning("Could not remove an incomplete video file for %s", video_id)


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


def format_selector(quality=480):
    height = normalize_quality(quality)
    selector = FORMAT_SELECTOR.replace("height<=720", f"height<={height}")
    if height < 720:
        selector = selector.replace(
            "/b[ext=mp4]/b",
            f"/b[ext=mp4][height<={height}]/b[height<={height}]",
        )
    return selector


def cache_key(video_id, quality):
    """Return the canonical per-video, per-quality cache key."""
    return str(video_id), normalize_quality(quality)


def estimated_size_bytes(duration, quality):
    """Estimate MP4 size from duration and a representative total bitrate."""
    height = normalize_quality(quality)
    seconds = max(0, float(duration or 0))
    return int(seconds * ESTIMATED_BITRATES[height] / 8)


def estimated_size_label(duration, quality):
    size_mb = estimated_size_bytes(duration, quality) / (1024 * 1024)
    if size_mb < 1:
        return f"~{max(1, round(size_mb * 1024))} KB"
    return f"~{round(size_mb)} MB"


def _lock_for_video(video_id):
    with _video_locks_lock:
        return _video_locks.setdefault(str(video_id), threading.Lock())


def _lock_for_info(video_id):
    with _info_locks_lock:
        return _info_locks.setdefault(str(video_id), threading.Lock())


@contextmanager
def protect_cache_file(path):
    cache_file = Path(path)
    with _cache_activity_lock:
        _active_cache_paths[cache_file] = _active_cache_paths.get(cache_file, 0) + 1
        _preparing_cache_paths[cache_file] = _preparing_cache_paths.get(cache_file, 0) + 1
    try:
        yield
    finally:
        with _cache_activity_lock:
            active_count = _active_cache_paths.get(cache_file, 0) - 1
            if active_count > 0:
                _active_cache_paths[cache_file] = active_count
            else:
                _active_cache_paths.pop(cache_file, None)
            preparing_count = _preparing_cache_paths.get(cache_file, 0) - 1
            if preparing_count > 0:
                _preparing_cache_paths[cache_file] = preparing_count
            else:
                _preparing_cache_paths.pop(cache_file, None)


def _is_preparing_path(path):
    return any(
        active_path == path or path.name.startswith(active_path.stem)
        for active_path in _preparing_cache_paths
    )


def try_retain_cache_file(path):
    """Atomically retain a ready cached file, or return None if it is unavailable."""
    cache_file = Path(path)
    with _cache_activity_lock:
        if _is_preparing_path(cache_file) or not cache_file.is_file():
            return None
        _active_cache_paths[cache_file] = _active_cache_paths.get(cache_file, 0) + 1
    released = False
    release_lock = threading.Lock()

    def release():
        nonlocal released
        with release_lock:
            if released:
                return
            released = True
        with _cache_activity_lock:
            active_count = _active_cache_paths.get(cache_file, 0) - 1
            if active_count > 0:
                _active_cache_paths[cache_file] = active_count
            else:
                _active_cache_paths.pop(cache_file, None)

    return release


def retain_cache_file(path):
    """Protect a cached file until its response has been closed."""
    release = try_retain_cache_file(path)
    if release is None:
        raise FileNotFoundError(f"Cached video is unavailable: {path}")
    return release


def cache_file_is_active(path):
    with _cache_activity_lock:
        cache_file = Path(path)
        return _active_cache_paths.get(cache_file, 0) > 0 or _is_preparing_path(cache_file)


def _cache_path_is_protected(path):
    return any(
        active_path == path or path.name.startswith(active_path.stem)
        for active_path in (*_active_cache_paths, *_preparing_cache_paths)
    )


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
        with _cache_activity_lock:
            if _cache_path_is_protected(path) or not path.is_file():
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
    return removed


def cache_stats():
    """Return the total cache bytes and number of files without exposing paths."""
    if not CACHE_DIR.exists():
        return 0, 0
    total_bytes = 0
    file_count = 0
    for path in CACHE_DIR.iterdir():
        try:
            if path.is_file():
                total_bytes += path.stat().st_size
                file_count += 1
        except OSError:
            continue
    return total_bytes, file_count


def clear_cache():
    """Delete cache files except files currently being prepared or served."""
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for path in CACHE_DIR.iterdir():
        with _cache_activity_lock:
            if _cache_path_is_protected(path) or not path.is_file():
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
    return removed


def cache_path(video_id, quality=480):
    video_id, height = cache_key(video_id, quality)
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
    video_id = video_id_from_url(validated_url)
    now = time.monotonic()
    with _info_cache_lock:
        cached = _info_cache.get(video_id)
        if cached and now - cached["cached_at"] < INFO_CACHE_TTL_SECONDS:
            return cached["info"]
        if cached:
            _info_cache.pop(video_id, None)

    with _lock_for_info(video_id):
        now = time.monotonic()
        with _info_cache_lock:
            cached = _info_cache.get(video_id)
            if cached and now - cached["cached_at"] < INFO_CACHE_TTL_SECONDS:
                return cached["info"]
            if cached:
                _info_cache.pop(video_id, None)

        started_at = time.perf_counter()
        info = _extract_info_uncached(validated_url)
        elapsed = time.perf_counter() - started_at
        with _info_cache_lock:
            _info_cache[video_id] = {
                "info": info,
                "cached_at": time.monotonic(),
                "metadata_seconds": elapsed,
            }
        logger.info("Video timing id=%s stage=metadata_extraction seconds=%.3f", video_id, elapsed)
        return info


def info_timing(video_id):
    with _info_cache_lock:
        cached = _info_cache.get(str(video_id))
        if cached and time.monotonic() - cached["cached_at"] < INFO_CACHE_TTL_SECONDS:
            return cached["metadata_seconds"]
    return None


def _extract_info_uncached(url):
    validated_url = validate_url(url)
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "cachedir": str(CACHE_DIR / "yt-dlp"),
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(validated_url, download=False)
    except DownloadError as error:
        message = _raise_for_youtube_http_error(error)
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


def get_info_quality_sizes(url):
    info = _extract_info(url)
    duration = info.get("duration")
    return {
        str(quality): estimated_size_label(duration, quality)
        for quality in QUALITIES
    }


def _format_rate(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    rate = float(value)
    unit = 0
    while rate >= 1024 and unit < len(units) - 1:
        rate /= 1024
        unit += 1
    return f"{rate:.1f} {units[unit]}"


def _format_eta(value):
    if not isinstance(value, (int, float)) or value < 0:
        return None
    seconds = int(value)
    return f"{seconds // 60}:{seconds % 60:02d} remaining"


@contextmanager
def _parallelize_format_downloads(downloader, requested_formats):
    format_keys = {
        (str(fmt.get("format_id")), fmt.get("url"))
        for fmt in requested_formats
        if fmt.get("format_id") and fmt.get("url")
    }
    if len(format_keys) < 2:
        yield
        return

    original_download = downloader.dl
    executor = ThreadPoolExecutor(max_workers=len(format_keys))
    futures = []

    def parallel_download(name, info, *args, **kwargs):
        key = (str(info.get("format_id")), info.get("url"))
        if key not in format_keys:
            return original_download(name, info, *args, **kwargs)

        futures.append(executor.submit(original_download, name, info, *args, **kwargs))
        if len(futures) < len(format_keys):
            return True, True
        results = [future.result() for future in futures]
        return all(result[0] for result in results), any(result[1] for result in results)

    downloader.dl = parallel_download
    try:
        yield
    finally:
        downloader.dl = original_download
        executor.shutdown(wait=True, cancel_futures=True)


class PreparationCancelled(PlayerError):
    """The user cancelled a background preparation."""


def _download_video_once(url, quality=480, progress_callback=None, cancel_event=None):
    """Download and merge a video into the quality-specific MP4 cache file."""
    validated_url = validate_url(url)
    height = normalize_quality(quality)
    video_id = video_id_from_url(validated_url)
    if not video_id:
        raise VideoUnavailableError("Could not determine the video ID.")
    with _lock_for_video(video_id):
        clean_cache()
        output_path = cache_path(video_id, height)
        if output_path.is_file():
            return {**cached_video_info(output_path, video_id), "path": output_path, "cached": True}

        require_ffmpeg()
        info = _extract_info(validated_url)
        video_id = info.get("id")
        if not video_id:
            raise VideoUnavailableError("yt-dlp did not return a video ID for this URL.")
        metadata_seconds = info_timing(video_id)
        if progress_callback:
            progress_callback({
                "stage": "metadata",
                "timings": {"metadata_extraction_seconds": metadata_seconds},
                "message": "Downloading video and audio...",
            })

        output_path = cache_path(video_id, height)
        if output_path.is_file():
            return {**cached_video_info(output_path, video_id), "path": output_path, "cached": True}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        started_at = time.perf_counter()
        merge_started_at = None
        timings = {"metadata_extraction_seconds": metadata_seconds}
        downloaded_formats = {}
        downloaded_formats_lock = threading.Lock()

        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise PreparationCancelled("Video preparation was cancelled.")

        def report(data):
            check_cancelled()
            if not progress_callback or data.get("status") != "downloading":
                return
            format_id = str(data.get("format_id") or data.get("filename") or "video")
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            with downloaded_formats_lock:
                downloaded_formats[format_id] = (downloaded, total)
                total_downloaded = sum(value[0] for value in downloaded_formats.values())
                total_size = sum(value[1] for value in downloaded_formats.values() if value[1])
            percent = min(95, int(total_downloaded * 95 / total_size)) if total_size else 0
            details = [f"Downloading {percent}%"]
            speed = _format_rate(data.get("speed"))
            eta = _format_eta(data.get("eta"))
            if speed:
                details.append(speed)
            if eta:
                details.append(eta)
            progress_callback({
                "stage": "download",
                "progress": percent,
                "speed": data.get("speed"),
                "eta": data.get("eta"),
                "message": " · ".join(details),
            })

        def report_postprocessing(data):
            nonlocal merge_started_at
            postprocessor = str(data.get("postprocessor", "")).lower()
            if "merger" in postprocessor and data.get("status") == "started":
                merge_started_at = time.perf_counter()
                download_seconds = merge_started_at - started_at
                timings["download_seconds"] = download_seconds
                logger.info("Video timing id=%s stage=download seconds=%.3f", video_id, download_seconds)
                if progress_callback:
                    progress_callback({
                        "stage": "merge",
                        "progress": 97,
                        "message": "Merging video and audio...",
                        "timings": dict(timings),
                    })
            elif "merger" in postprocessor and data.get("status") == "finished" and merge_started_at is not None:
                merge_seconds = time.perf_counter() - merge_started_at
                timings["merge_seconds"] = merge_seconds
                logger.info("Video timing id=%s stage=merge seconds=%.3f", video_id, merge_seconds)
            if "convertor" in postprocessor or "re-encode" in postprocessor:
                logger.warning("Video timing id=%s warning=ffmpeg_re-encode", video_id)

        options = {
            "format": format_selector(height),
            "merge_output_format": "mp4",
            "outtmpl": str(CACHE_DIR / f"{output_path.stem}.%(ext)s"),
            "progress_hooks": [report],
            "postprocessor_hooks": [report_postprocessing],
            "concurrent_fragment_downloads": 4,
            "http_chunk_size": 10_485_760,
            "retries": 5,
            "fragment_retries": 5,
            "sleep_interval_requests": 0.5,
            "cachedir": str(CACHE_DIR / "yt-dlp"),
            "postprocessor_args": {
                "merger+ffmpeg_o": ["-c", "copy", "-movflags", "+faststart"],
            },
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        if ARIA2C_PATH:
            options["external_downloader"] = {"https": ARIA2C_PATH}
            options["external_downloader_args"] = {
                "aria2c": ["-x4", "-s4", "-k", "1M"],
            }

        with protect_cache_file(output_path):
            clean_cache()
            try:
                check_cancelled()
                with yt_dlp.YoutubeDL(options) as downloader:
                    with _parallelize_format_downloads(downloader, info.get("requested_formats", [])):
                        downloader.process_ie_result(deepcopy(info), download=True)
                check_cancelled()
            except PreparationCancelled:
                raise
            except DownloadError as error:
                message = _raise_for_youtube_http_error(error)
                lowered = message.lower()
                if any(word in lowered for word in ("network", "connection", "timed out", "unable to download")):
                    raise NetworkError(message) from error
                raise VideoUnavailableError(message) from error
            except (OSError, TimeoutError) as error:
                message = str(error).strip() or error.__class__.__name__
                raise NetworkError(message) from error

            if not output_path.is_file():
                raise VideoUnavailableError(f"yt-dlp completed but did not create the expected MP4: {output_path.name}")
            if merge_started_at is None:
                timings["download_seconds"] = time.perf_counter() - started_at
                logger.info(
                    "Video timing id=%s stage=download seconds=%.3f",
                    video_id,
                    timings["download_seconds"],
                )
                if len(info.get("requested_formats", [])) > 1:
                    timings["merge_seconds"] = 0.0
                    logger.info(
                        "Video timing id=%s stage=merge seconds=0.000 mode=parallel_mux",
                        video_id,
                    )
            metadata_path = output_path.with_suffix(".json")
            metadata = {
                key: info.get(key)
                for key in ("id", "title", "duration", "thumbnail", "channel", "view_count", "upload_date")
            }
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            timings["total_preparation_seconds"] = time.perf_counter() - started_at
            if progress_callback:
                progress_callback({
                    "stage": "ready",
                    "progress": 100,
                    "message": "Video ready.",
                    "timings": dict(timings),
                })
            return {**info, "path": output_path, "cached": False, "timings": timings}


def download_video(url, quality=480, progress_callback=None, cancel_event=None):
    """Prepare a video, refreshing expired URLs after YouTube 403/429 errors."""
    validated_url = validate_url(url)
    video_id = video_id_from_url(validated_url)
    height = normalize_quality(quality)
    delays = (2, 5, 10)
    for attempt in range(len(delays) + 1):
        try:
            return _download_video_once(validated_url, height, progress_callback, cancel_event)
        except YouTubeHTTPError:
            if attempt >= len(delays):
                raise
            _remove_incomplete_video_files(video_id, height)
            _clear_info_cache(video_id)
            delay = delays[attempt]
            if cancel_event is not None:
                if cancel_event.wait(delay):
                    raise PreparationCancelled("Video preparation was cancelled.")
            else:
                time.sleep(delay)