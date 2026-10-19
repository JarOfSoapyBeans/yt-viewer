import pytest
from werkzeug.datastructures import Headers

import app as app_module
from app import app


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


def test_watch_rejects_non_youtube_hosts(monkeypatch):
    monkeypatch.setattr("app._start_job", lambda *args, **kwargs: pytest.fail("invalid host reached downloader"))

    response = app.test_client().post(
        "/watch",
        data={"url": "https://youtube.com.example/watch?v=abc123"},
    )

    assert response.status_code == 400
    assert b"valid YouTube video URL" in response.data


def test_rate_limit_caps_watch_requests_per_ip_and_isolates_clients(monkeypatch):
    monkeypatch.setattr("app._start_job", lambda *args, **kwargs: None)
    client = app.test_client()
    form = {"url": "https://youtu.be/abc123", "quality": "720"}

    for _ in range(10):
        response = client.post("/watch", data=form, environ_base={"REMOTE_ADDR": "192.0.2.10"})
        assert response.status_code == 200

    limited = client.post("/watch", data=form, environ_base={"REMOTE_ADDR": "192.0.2.10"})
    different_ip = client.post("/watch", data=form, environ_base={"REMOTE_ADDR": "192.0.2.11"})

    assert limited.status_code == 429
    assert limited.headers["Retry-After"]
    assert b"Rate limit exceeded" in limited.data
    assert different_ip.status_code == 200


def test_rate_limit_uses_cloudflare_ip_when_present(monkeypatch):
    monkeypatch.setattr("app._start_job", lambda *args, **kwargs: None)
    client = app.test_client()
    form = {"url": "https://youtu.be/abc123", "quality": "720"}

    for _ in range(app_module.RATE_LIMIT_REQUESTS):
        response = client.post(
            "/watch",
            data=form,
            environ_base={"REMOTE_ADDR": "192.0.2.12"},
            headers={"CF-Connecting-IP": "192.0.2.13"},
        )
        assert response.status_code == 200

    limited = client.post(
        "/watch",
        data=form,
        environ_base={"REMOTE_ADDR": "192.0.2.14"},
        headers={"CF-Connecting-IP": "192.0.2.13"},
    )
    different_cf_ip = client.post(
        "/watch",
        data=form,
        environ_base={"REMOTE_ADDR": "192.0.2.12"},
        headers={"CF-Connecting-IP": "192.0.2.15"},
    )

    assert limited.status_code == 429
    assert different_cf_ip.status_code == 200


def test_run_uses_waitress_on_loopback_with_debug_disabled(monkeypatch):
    captured = {}

    def fake_serve(flask_app, **options):
        captured.update(app=flask_app, options=options)

    monkeypatch.setattr(app_module, "serve", fake_serve)

    app_module.run()

    assert app.debug is False
    assert captured["app"] is app
    assert captured["options"] == {"host": "127.0.0.1", "port": 5000, "threads": 8}


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


@pytest.mark.parametrize(
    ("query", "expected_status"),
    [("", 400), ("   ", 400), ("x" * 201, 400)],
)
def test_search_rejects_invalid_queries(query, expected_status):
    response = app.test_client().get(
        "/api/search",
        query_string={"q": query},
        environ_base={"REMOTE_ADDR": "192.0.2.80"},
    )

    assert response.status_code == expected_status
    assert response.get_json()["error"]


def test_search_accepts_maximum_limit_and_returns_video_list(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        app_module,
        "search_videos",
        lambda query, limit, skip: captured.update(query=query, limit=limit, skip=skip)
        or [{"id": "abcdefghijk"}],
    )

    response = app.test_client().get(
        "/api/search",
        query_string={"q": "cats", "limit": 96, "skip": 12},
        environ_base={"REMOTE_ADDR": "192.0.2.81"},
    )

    assert response.status_code == 200
    assert response.get_json() == [{"id": "abcdefghijk"}]
    assert captured == {"query": "cats", "limit": 96, "skip": 12}


def test_search_rejects_limit_above_96():
    response = app.test_client().get(
        "/api/search",
        query_string={"q": "cats", "limit": 97},
        environ_base={"REMOTE_ADDR": "192.0.2.86"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Search limit must be 96 or fewer."


def test_search_returns_only_results_after_skip(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "search_videos",
        lambda query, limit, skip: [{"id": str(index)} for index in range(skip, limit)],
    )

    response = app.test_client().get(
        "/api/search",
        query_string={"q": "cats", "limit": 24, "skip": 12},
        environ_base={"REMOTE_ADDR": "192.0.2.87"},
    )

    assert response.status_code == 200
    assert response.get_json() == [{"id": str(index)} for index in range(12, 24)]


def test_search_returns_original_ytdlp_error(monkeypatch):
    def fail_search(*args):
        raise app_module.PlayerError("ERROR: blocked by test")

    monkeypatch.setattr(app_module, "search_videos", fail_search)

    response = app.test_client().get(
        "/api/search",
        query_string={"q": "cats"},
        environ_base={"REMOTE_ADDR": "192.0.2.82"},
    )

    assert response.status_code == 502
    assert response.get_json()["error"] == "ERROR: blocked by test"


def test_search_allows_30_requests_per_minute_per_ip(monkeypatch):
    monkeypatch.setattr(app_module, "search_videos", lambda *args: [])
    client = app.test_client()
    client_ip = "192.0.2.84"

    for _ in range(app_module.SEARCH_RATE_LIMIT_REQUESTS):
        response = client.get(
            "/api/search",
            query_string={"q": "cats"},
            environ_base={"REMOTE_ADDR": client_ip},
        )
        assert response.status_code == 200

    limited = client.get(
        "/api/search",
        query_string={"q": "cats"},
        environ_base={"REMOTE_ADDR": client_ip},
    )

    assert limited.status_code == 429
    assert limited.headers["Retry-After"]


def test_search_rate_limit_uses_cloudflare_ip(monkeypatch):
    monkeypatch.setattr(app_module, "search_videos", lambda *args: [])
    client = app.test_client()
    headers = {"CF-Connecting-IP": "192.0.2.88"}

    for _ in range(app_module.SEARCH_RATE_LIMIT_REQUESTS):
        response = client.get(
            "/api/search",
            query_string={"q": "cats"},
            environ_base={"REMOTE_ADDR": "192.0.2.89"},
            headers=headers,
        )
        assert response.status_code == 200

    limited = client.get(
        "/api/search",
        query_string={"q": "cats"},
        environ_base={"REMOTE_ADDR": "192.0.2.90"},
        headers=headers,
    )

    assert limited.status_code == 429


def test_info_route_normalizes_plain_video_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        app_module,
        "get_info",
        lambda url: captured.update(url=url) or {"id": "abcdefghijk", "title": "Example"},
    )

    response = app.test_client().get(
        "/api/info",
        query_string={"id": "abcdefghijk"},
        environ_base={"REMOTE_ADDR": "192.0.2.83"},
    )

    assert response.status_code == 200
    assert response.get_json()["title"] == "Example"
    assert captured["url"] == "https://www.youtube.com/watch?v=abcdefghijk"


def test_status_route_normalizes_plain_video_id(monkeypatch):
    captured = {}
    job_key = "normalized-video-job"
    app_module._jobs[job_key] = {
        "status": "preparing",
        "progress": 0,
        "message": "Preparing video...",
    }

    def capture_job(url, quality):
        captured.update(url=url, quality=quality)
        return job_key

    monkeypatch.setattr(app_module, "_start_job", capture_job)
    response = app.test_client().get(
        "/api/status",
        query_string={"url": "abcdefghijk", "quality": "720"},
        environ_base={"REMOTE_ADDR": "192.0.2.85"},
    )

    assert response.status_code == 200
    assert captured == {
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "quality": 720,
    }


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


def test_stream_accepts_full_youtube_url(tmp_path, monkeypatch):
    video_file = tmp_path / "abcdefghijk_720p.mp4"
    video_file.write_bytes(b"video")
    requested = {}

    def fake_cache_path(video_id, quality):
        requested.update(video_id=video_id, quality=quality)
        return video_file

    monkeypatch.setattr(app_module, "cache_path", fake_cache_path)

    response = app.test_client().get(
        "/stream",
        query_string={"id": "https://www.youtube.com/watch?v=abcdefghijk"},
    )

    assert response.status_code == 200
    assert requested == {"video_id": "abcdefghijk", "quality": 720}


def test_stream_returns_not_found_before_video_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "cache_path", lambda video_id, quality: tmp_path / "missing.mp4")

    response = app.test_client().get("/stream", query_string={"id": "abc123", "quality": "720"})

    assert response.status_code == 404