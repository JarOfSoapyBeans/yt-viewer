import pytest
from werkzeug.datastructures import Headers

import app as app_module
from app import app
from player import InvalidURLError, PlayerError, VideoUnavailableError


def test_index_shows_quality_options_and_ffmpeg_warning(monkeypatch):
    monkeypatch.setattr(app_module, "FFMPEG_WARNING", "Install ffmpeg to prepare videos.")

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"360p" in response.data
    assert b"480p" in response.data
    assert b"720p" in response.data
    assert b"Install ffmpeg" in response.data


def test_watch_starts_background_preparation(monkeypatch):
    started = {}

    def capture_job(url, quality, retry=False):
        started.update(url=url, quality=quality, retry=retry)

    monkeypatch.setattr(app_module, "_start_job", capture_job)

    response = app.test_client().post(
        "/watch",
        data={"url": "https://youtu.be/abc123", "quality": "480"},
    )

    assert response.status_code == 200
    assert b"Preparing video..." in response.data
    assert b"api/status" in response.data
    assert started == {"url": "https://youtu.be/abc123", "quality": 480, "retry": True}


def test_watch_rejects_missing_url_and_unsupported_quality(monkeypatch):
    client = app.test_client()
    missing = client.post("/watch", data={})
    invalid_quality = client.post("/watch", data={"url": "https://youtu.be/abc123", "quality": "1080"})

    assert missing.status_code == 400
    assert b"YouTube URL is required" in missing.data
    assert invalid_quality.status_code == 400
    assert b"Choose 360p, 480p, or 720p" in invalid_quality.data


def test_status_returns_progress_and_does_not_restart_failed_job(monkeypatch):
    video_url = "https://youtu.be/abc123"
    quality = 720
    key = app_module._job_key(video_url, quality)
    app_module._jobs[key] = {
        "status": "error",
        "progress": 23,
        "message": "ERROR: original yt-dlp message",
        "error": "ERROR: original yt-dlp message",
    }
    monkeypatch.setattr(app_module, "_start_job", lambda *args, **kwargs: key)

    response = app.test_client().get("/api/status", query_string={"url": video_url, "quality": quality})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "error"
    assert payload["progress"] == 23
    assert payload["error"] == "ERROR: original yt-dlp message"


def test_preparation_progress_transitions_to_ready_status(monkeypatch, tmp_path):
    video_url = "https://youtu.be/abc123"
    quality = 480
    key = app_module._job_key(video_url, quality)
    app_module._jobs[key] = {"status": "preparing", "progress": 0, "message": "Preparing video..."}

    def fake_download(url, selected_quality, progress_callback):
        assert selected_quality == quality
        progress_callback({"progress": 54, "message": "Merging streams into MP4..."})
        return {"id": "abc123", "title": "Example title", "path": tmp_path / "abc123_480p.mp4"}

    monkeypatch.setattr(app_module, "download_video", fake_download)
    monkeypatch.setattr(app_module, "_start_job", lambda *args, **kwargs: key)

    app_module._prepare_video(key, video_url, quality)
    payload = app.test_client().get(
        "/api/status",
        query_string={"url": video_url, "quality": quality},
    ).get_json()

    assert payload["status"] == "ready"
    assert payload["progress"] == 100
    assert payload["title"] == "Example title"
    assert payload["stream_url"] == "/stream?id=abc123&quality=480"


def test_status_rejects_invalid_url():
    response = app.test_client().get("/api/status", query_string={"url": "https://example.com/video"})

    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


def test_stream_serves_cached_mp4_with_conditional_range(tmp_path, monkeypatch):
    video_file = tmp_path / "abc123_480p.mp4"
    video_file.write_bytes(b"0123456789")
    monkeypatch.setattr(app_module, "cache_path", lambda video_id, quality: video_file)

    response = app.test_client().get(
        "/stream",
        query_string={"id": "abc123", "quality": "480"},
        headers=Headers({"Range": "bytes=2-5"}),
    )

    assert response.status_code == 206
    assert response.data == b"2345"
    assert response.headers["Content-Type"].startswith("video/mp4")
    assert response.headers["Content-Range"] == "bytes 2-5/10"
    assert response.headers["Content-Length"] == "4"


def test_stream_returns_not_found_before_video_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "cache_path", lambda video_id, quality: tmp_path / "missing.mp4")

    response = app.test_client().get("/stream", query_string={"id": "abc123", "quality": "720"})

    assert response.status_code == 404