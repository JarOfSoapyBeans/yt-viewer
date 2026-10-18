"""Flask web interface for preparing and playing cached YouTube videos."""

import hashlib
import threading

from flask import Flask, jsonify, render_template, request, send_file, url_for

from player import (
    PlayerError,
    QUALITIES,
    cache_path,
    clean_cache,
    download_video,
    ffmpeg_path,
    normalize_quality,
    validate_url,
)


app = Flask(__name__)

_jobs = {}
_jobs_lock = threading.Lock()
clean_cache()
FFMPEG_AVAILABLE = bool(ffmpeg_path())
FFMPEG_WARNING = None if FFMPEG_AVAILABLE else (
    "ffmpeg is not installed. Install ffmpeg and restart the viewer to merge video and audio."
)
if FFMPEG_WARNING:
  app.logger.error(FFMPEG_WARNING)

def _render_index(**context):
    values = {
        "ffmpeg_warning": FFMPEG_WARNING,
        "qualities": QUALITIES,
        "quality": 720,
        "title": None,
        "video_url": "",
        "watching": False,
        "status_url": None,
        "error": None,
    }
    values.update(context)
    return render_template("index.html", **values)


def _job_key(video_url, quality):
    value = f"{video_url}\0{quality}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _update_progress(key, data):
    with _jobs_lock:
        job = _jobs.get(key)
        if job and job["status"] == "preparing":
            job.update(data)


def _prepare_video(key, video_url, quality):
    try:
        result = download_video(video_url, quality, lambda data: _update_progress(key, data))
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
        )


def _start_job(video_url, quality, retry=False):
    key = _job_key(video_url, quality)
    with _jobs_lock:
        job = _jobs.get(key)
        if job and job["status"] in {"preparing", "ready"}:
            return key
        if job and job["status"] == "error" and not retry:
            return key
        _jobs[key] = {"status": "preparing", "progress": 0, "message": "Preparing video..."}
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
        quality = normalize_quality(request.form.get("quality", "720"))
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
        quality = normalize_quality(request.args.get("quality", "720"))
    except (PlayerError, ValueError) as error:
        return jsonify(status="error", error=str(error)), 400

    key = _start_job(video_url, quality)
    with _jobs_lock:
        state = dict(_jobs[key])
    if state["status"] == "ready":
        state["stream_url"] = url_for("stream", id=state["video_id"], quality=quality)
    return jsonify(state)


@app.get("/stream")
def stream():
    video_id = request.args.get("id", "")
    try:
        quality = normalize_quality(request.args.get("quality", "720"))
    except ValueError as error:
        return str(error), 400
    if not video_id:
        return "Video ID is required.", 400

    path = cache_path(video_id, quality)
    if not path.is_file():
        return "Prepared video is not in the cache.", 404
    return send_file(path, mimetype="video/mp4", conditional=True, etag=True, max_age=0)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)