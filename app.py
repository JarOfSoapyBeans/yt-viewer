"""Flask web interface for preparing and playing cached YouTube videos."""

import hashlib
import logging
import re
import threading
import time
from collections import defaultdict, deque

from flask import Flask, jsonify, render_template, request, send_file, url_for
from waitress import serve

from player import (
    PlayerError,
    PreparationCancelled,
    QUALITIES,
    cached_video_info,
    cache_file_is_active,
    cache_path,
    clean_cache,
    download_video,
    estimated_size_label,
    info_timing,
    ffmpeg_path,
    get_info,
    normalize_quality,
    retain_cache_file,
    try_retain_cache_file,
    search_videos,
    validate_url,
    video_id_from_url,
)


app = Flask(__name__)
app.config["DEBUG"] = False
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_jobs = {}
_jobs_lock = threading.Lock()
_rate_windows = defaultdict(deque)
_rate_lock = threading.Lock()
RATE_LIMIT_REQUESTS = 10
SEARCH_RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_SECONDS = 60
clean_cache()
FFMPEG_AVAILABLE = bool(ffmpeg_path())
FFMPEG_WARNING = None if FFMPEG_AVAILABLE else (
    "ffmpeg is not installed. Install ffmpeg and restart the viewer to merge video and audio."
)
if FFMPEG_WARNING:
    app.logger.error(FFMPEG_WARNING)


@app.before_request
def enforce_rate_limit():
    if request.endpoint in {"stream", "static"}:
        return None

    client_ip = request.headers.get("CF-Connecting-IP", "").strip() or request.remote_addr or "unknown"
    is_search = request.endpoint == "search"
    rate_limit_requests = SEARCH_RATE_LIMIT_REQUESTS if is_search else RATE_LIMIT_REQUESTS
    now = time.monotonic()
    with _rate_lock:
        requests = _rate_windows[(client_ip, is_search)]
        while requests and now - requests[0] >= RATE_LIMIT_SECONDS:
            requests.popleft()
        if len(requests) >= rate_limit_requests:
            retry_after = max(1, int(RATE_LIMIT_SECONDS - (now - requests[0])))
            return jsonify(error="Rate limit exceeded. Try again shortly."), 429, {
                "Retry-After": str(retry_after)
            }
        requests.append(now)
    return None


def _render_index(**context):
    values = {
        "ffmpeg_warning": FFMPEG_WARNING,
        "qualities": QUALITIES,
        "quality": 480,
        "title": None,
        "video_url": "",
        "watching": False,
        "status_url": None,
        "error": None,
    }
    values.update(context)
    return render_template("index.html", **values)


def _job_key(video_url, quality):
    try:
        video_id = video_id_from_url(video_url)
    except PlayerError:
        video_id = video_url
    value = f"{video_id}\0{normalize_quality(quality)}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def clean_download_filename(title):
    """Return a Windows-safe MP4 filename based on a video title."""
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(title or ""))
    filename = filename.strip().rstrip(".")[:100].strip().rstrip(".")
    if not filename:
        filename = "video"
    if filename.upper().split(".", 1)[0] in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        filename = f"_{filename}"
    return f"{filename}.mp4"


def _update_progress(key, data):
    with _jobs_lock:
        job = _jobs.get(key)
        if job and job["status"] == "preparing":
            update = dict(data)
            timings = update.pop("timings", None)
            job.update(update)
            if timings:
                job.setdefault("timings", {}).update(timings)


def _prepare_video(key, video_url, quality):
    with _jobs_lock:
        job = _jobs.get(key)
        if not job:
            return
        cancel_event = job["cancel_event"]
    try:
        result = download_video(
            video_url,
            quality,
            lambda data: _update_progress(key, data),
            cancel_event,
        )
    except PreparationCancelled:
        with _jobs_lock:
            if key in _jobs:
                _jobs[key].update(status="cancelled", message="Preparation cancelled.")
        app.logger.info("Video preparation cancelled for %r", video_url)
        return
    except PlayerError as error:
        app.logger.exception("Video preparation failed for %r", video_url)
        with _jobs_lock:
            _jobs[key].update(status="error", error=str(error), message=str(error))
        return
    except Exception as error:
        app.logger.exception("Unexpected video preparation failure for %r", video_url)
        with _jobs_lock:
            _jobs[key].update(status="error", error=str(error), message=str(error))
        return

    with _jobs_lock:
        _jobs[key].update(
            status="ready",
            progress=100,
            message="Video ready.",
            video_id=result["id"],
            title=result["title"],
            timings=result.get("timings", _jobs[key].get("timings", {})),
        )


def _start_job(video_url, quality, retry=False):
    key = _job_key(video_url, quality)
    with _jobs_lock:
        job = _jobs.get(key)
        if job and job["status"] in {"preparing", "ready", "cancelling"}:
            return key
        if job and job["status"] in {"error", "cancelled"} and not retry:
            return key
        _jobs[key] = {
            "status": "preparing",
            "progress": 0,
            "message": "Preparing video...",
            "timings": {},
            "started_at": time.monotonic(),
            "cancel_event": threading.Event(),
        }
        worker = threading.Thread(target=_prepare_video, args=(key, video_url, quality), daemon=True)
        worker.start()
    return key


@app.get("/")
def index():
    return _render_index()


@app.post("/watch")
def watch():
    video_url = request.form.get("url", "").strip()
    if not video_url:
        return _render_index(error="YouTube URL is required."), 400
    try:
        video_url = validate_url(video_url)
        quality = normalize_quality(request.form.get("quality", "480"))
    except (PlayerError, ValueError) as error:
        return _render_index(error=str(error), video_url=video_url), 400

    _start_job(video_url, quality, retry=True)
    return _render_index(
        video_url=video_url,
        quality=quality,
        watching=True,
        status_url=url_for("video_status", url=video_url, quality=quality),
    )


@app.get("/api/status")
def video_status():
    video_url = request.args.get("url", "").strip()
    try:
        video_url = validate_url(video_url)
        quality = normalize_quality(request.args.get("quality", "480"))
    except (PlayerError, ValueError) as error:
        return jsonify(status="error", error=str(error)), 400

    key = _start_job(video_url, quality)
    with _jobs_lock:
        state = dict(_jobs[key])
        state.pop("cancel_event", None)
        state.pop("started_at", None)
        state["timings"] = dict(state.get("timings", {}))
        state["timings"].setdefault("time_to_first_byte_seconds", None)
    if state["status"] == "ready":
        state["stream_url"] = url_for("stream", id=state["video_id"], quality=quality)
    return jsonify(state)


@app.get("/api/info")
def video_info():
    video_url = request.args.get("url", request.args.get("id", "")).strip()
    try:
        video_url = validate_url(video_url)
        info = get_info(video_url)
        info["quality_sizes"] = {
            str(quality): estimated_size_label(info.get("duration"), quality)
            for quality in QUALITIES
        }
        info["timings"] = {"metadata_extraction_seconds": info_timing(info["id"])}
    except (PlayerError, ValueError) as error:
        return jsonify(error=str(error)), 400
    return jsonify(info)


@app.post("/api/cancel")
def cancel_preparation():
    video_url = request.form.get("url", request.args.get("url", "")).strip()
    try:
        video_url = validate_url(video_url)
        quality = normalize_quality(request.form.get("quality", request.args.get("quality", "480")))
    except (PlayerError, ValueError) as error:
        return jsonify(status="error", error=str(error)), 400

    key = _job_key(video_url, quality)
    with _jobs_lock:
        job = _jobs.get(key)
        if not job or job["status"] != "preparing":
            return jsonify(status="not_preparing")
        job["cancel_event"].set()
        job["status"] = "cancelling"
    app.logger.info("Cancellation requested for video preparation %s at %sp", video_id_from_url(video_url), quality)
    return jsonify(status="cancelling")


@app.get("/api/search")
def search():
    query = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", "12"))
        if limit < 1:
            raise ValueError("Search limit must be a positive integer.")
        if limit > 96:
            raise ValueError("Search limit must be 96 or fewer.")
        skip = int(request.args.get("skip", "0"))
        if skip < 0 or skip > limit:
            raise ValueError("Search offset must be between zero and the search limit.")
        videos = search_videos(query, limit, skip)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except PlayerError as error:
        return jsonify(error=str(error)), 502
    return jsonify(videos)


@app.get("/stream")
def stream():
    video_url = request.args.get("id", request.args.get("url", "")).strip()
    if "://" in video_url:
        try:
            video_url = validate_url(video_url)
        except PlayerError as error:
            return str(error), 400
        video_id = video_id_from_url(video_url)
    else:
        video_id = video_url
    if not video_id:
        return "Video ID is required.", 400
    try:
        quality = normalize_quality(request.args.get("quality", "480"))
    except ValueError as error:
        return str(error), 400

    path = cache_path(video_id, quality)
    release = try_retain_cache_file(path)
    if release is None:
        if path.is_file() and cache_file_is_active(path):
            return "Video is still preparing.", 404
        return "Prepared video is not in the cache.", 404
    return _send_cached_file(path, video_id, quality, conditional=True, release=release)


def _send_cached_file(
    path,
    video_id,
    quality,
    conditional=False,
    as_attachment=False,
    download_name=None,
    release=None,
):
    request_started = time.monotonic()
    key = _job_key(video_id, quality)
    if release is None:
        release = retain_cache_file(path)
    try:
        response = send_file(
            path,
            mimetype="video/mp4",
            conditional=conditional,
            etag=True,
            max_age=0,
            as_attachment=as_attachment,
            download_name=download_name,
        )
    except OSError:
        release()
        app.logger.exception("Could not open cached video %s at quality %sp", video_id, quality)
        return "Prepared video could not be read.", 500

    original_iterator = response.response
    first_chunk = True

    def timed_response():
        nonlocal first_chunk
        try:
            for chunk in original_iterator:
                if first_chunk:
                    first_chunk = False
                    request_seconds = time.monotonic() - request_started
                    preparation_seconds = None
                    with _jobs_lock:
                        job = _jobs.get(key)
                        started_at = job.get("started_at") if job else None
                        if started_at is not None:
                            preparation_seconds = time.monotonic() - started_at
                            job.setdefault("timings", {})["time_to_first_byte_seconds"] = preparation_seconds
                    app.logger.info(
                        "Video timing id=%s quality=%sp stage=first_byte request_seconds=%.3f preparation_seconds=%s",
                        video_id,
                        quality,
                        request_seconds,
                        f"{preparation_seconds:.3f}" if preparation_seconds is not None else "n/a",
                    )
                yield chunk
        finally:
            close = getattr(original_iterator, "close", None)
            if close:
                close()
            release()

    response.response = timed_response()
    response.call_on_close(release)
    return response


@app.get("/download")
def download():
    video_id = request.args.get("id", "")
    requested_quality = request.args.get("quality", "480")
    if requested_quality not in {str(option) for option in QUALITIES}:
        error = "Choose 360p, 480p, or 720p."
        app.logger.warning("Rejected download quality %r: %s", requested_quality, error)
        return error, 400
    try:
        quality = normalize_quality(requested_quality)
    except ValueError as error:
        app.logger.warning("Rejected download quality %r: %s", request.args.get("quality"), error)
        return str(error), 400

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        app.logger.warning("Rejected invalid download video ID %r", video_id)
        return "Video ID must be exactly 11 letters, numbers, hyphens, or underscores.", 400

    try:
        path = cache_path(video_id, quality)
        release = try_retain_cache_file(path)
        if release is None:
            app.logger.info("Download requested before video %s at quality %sp is ready", video_id, quality)
            return "Video is still preparing.", 409
        metadata = cached_video_info(path, video_id)
        return _send_cached_file(
            path,
            video_id=video_id,
            quality=quality,
            as_attachment=True,
            download_name=clean_download_filename(metadata.get("title")),
            release=release,
        )
    except OSError:
        app.logger.exception("Could not serve download for video %s at quality %sp", video_id, quality)
        return "Could not prepare the video download.", 500


def run():
    serve(app, host="127.0.0.1", port=5000, threads=8)


if __name__ == "__main__":
    run()