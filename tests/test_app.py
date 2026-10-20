import threading

import pytest
from werkzeug.datastructures import Headers

import app as app_module
import player
from app import app


def test_index_shows_quality_options_and_ffmpeg_warning(monkeypatch):
    monkeypatch.setattr(app_module, "FFMPEG_WARNING", "Install ffmpeg to prepare videos.")

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"360p" in response.data
    assert b"480p" in response.data
    assert b"720p" in response.data
    assert b"Install ffmpeg" in response.data


def test_player_ui_assets_and_custom_video_markup_are_present():
    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"/static/player-ui.css" in response.data
    assert b"/static/player-ui.js" in response.data
    assert client.get("/static/player-ui.css").status_code == 200
    assert client.get("/static/player-ui.js").status_code == 200
    assert b'<video id="video-player" playsinline preload="metadata"></video>' in response.data
    assert b'<video id="video-player" controls' not in response.data
    assert b'value="480" selected' in response.data
    assert b'id="seek-slider"' in response.data
    assert b'id="player-speed"' in response.data


def test_pwa_manifest_icons_and_service_worker_are_served_and_registered():
    client = app.test_client()
    page = client.get("/", environ_base={"REMOTE_ADDR": "192.0.2.219"})

    assert b'rel="manifest"' in page.data
    assert b"serviceWorker.register('/service-worker.js')" in page.data
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/icons/icon-192.svg").status_code == 200
    assert client.get("/static/icons/icon-512.svg").status_code == 200
    worker = client.get("/service-worker.js", environ_base={"REMOTE_ADDR": "192.0.2.220"})
    assert worker.status_code == 200
    assert b"/stream" in worker.data and b"/download" in worker.data and b"/api/" in worker.data


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
    monkeypatch.setattr(app_module, "_start_ytdlp_update_if_enabled", lambda: None)

    app_module.run()

    assert app.debug is False
    assert captured["app"] is app
    assert captured["options"] == {"host": "127.0.0.1", "port": 5000, "threads": 8}


@pytest.mark.parametrize(
    ("installed", "latest", "expected"),
    [
        ("2025.01.01", "2025.02.01", True),
        ("2025.02.01", "2025.01.01", False),
        ("2025.01.01", "2025.01.01", False),
        ("2025.01.01", "not-a-version", False),
    ],
)
def test_ytdlp_version_comparison(installed, latest, expected):
    assert app_module._is_newer_version(installed, latest) is expected


def test_recent_error_buffer_keeps_only_five_sanitized_entries():
    errors = app_module.RecentErrors()
    for number in range(7):
        errors.add(f"failure {number}", timestamp=f"2026-10-20T00:00:0{number}+00:00")
    errors.add("failed https://example.test/private /usr/local/alice/file user=alice")
    errors.add("cookie=session=secret; other=secret")

    entries = errors.snapshot()
    assert len(entries) == 5
    assert entries[0]["message"] == "failure 4"
    assert entries[-2]["message"] == "failed [url] [path] user=[redacted]"
    assert entries[-1]["message"] == "cookie=[redacted]"


def test_ytdlp_update_checks_pypi_and_runs_pip_only_for_newer_version(monkeypatch):
    import io
    import json

    monkeypatch.setattr(app_module.yt_dlp.version, "__version__", "2025.01.01")
    monkeypatch.setattr(
        app_module,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps({"info": {"version": "2026.01.01"}}).encode()),
    )
    calls = []
    monkeypatch.setattr(app_module.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))

    app_module._check_for_ytdlp_update()

    assert calls[0][0][-5:] == ["-m", "pip", "install", "-U", "yt-dlp"]
    assert calls[0][1]["check"] is True


def test_ytdlp_update_failure_is_quiet(monkeypatch, caplog):
    monkeypatch.setattr(app_module, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))

    app_module._check_for_ytdlp_update()

    assert "yt-dlp auto-update skipped: OSError" in caplog.text


def test_health_limits_tunnel_response_to_versions_and_statuses(monkeypatch):
    monkeypatch.setattr(app_module, "_installed_ytdlp_version", lambda: "2026.01.01")
    client = app.test_client()

    local = client.get("/health", environ_base={"REMOTE_ADDR": "127.0.0.2"}).get_json()
    tunneled = client.get(
        "/health",
        environ_base={"REMOTE_ADDR": "127.0.0.2"},
        headers={"CF-Connecting-IP": "203.0.113.5"},
    ).get_json()

    assert "disk_free_bytes" in local
    assert set(tunneled) == {"yt_dlp", "ffmpeg", "deno", "aria2c"}
    assert tunneled["yt_dlp"] == {"version": "2026.01.01"}


def test_server_auto_update_setting_defaults_on_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "SETTINGS_PATH", tmp_path / "settings.json")
    client = app.test_client()

    environ = {"REMOTE_ADDR": "192.0.2.222"}
    assert client.get("/api/settings", environ_base=environ).get_json() == {"auto_update_ytdlp": True}
    saved = client.post("/api/settings", json={"auto_update_ytdlp": False}, environ_base=environ)

    assert saved.status_code == 200
    assert client.get("/api/settings", environ_base=environ).get_json() == {"auto_update_ytdlp": False}


def test_cache_clear_endpoint_skips_active_files(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    active_file = tmp_path / "active_480p.mp4"
    removable_file = tmp_path / "old_360p.mp4"
    active_file.write_bytes(b"active")
    removable_file.write_bytes(b"cached")

    with player.protect_cache_file(active_file):
        response = app.test_client().post(
            "/api/cache/clear",
            environ_base={"REMOTE_ADDR": "192.0.2.221"},
        )

    assert response.status_code == 200
    assert response.get_json() == {
        "removed_file_count": 1,
        "cache_size_bytes": len(b"active"),
        "cache_file_count": 1,
    }
    assert active_file.exists()
    assert not removable_file.exists()


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
    assert "time_to_first_byte_seconds" in payload["timings"]


def test_preparation_progress_transitions_to_ready_status(monkeypatch, tmp_path):
    video_url = "https://youtu.be/abc123"
    quality = 480
    key = app_module._job_key(video_url, quality)
    app_module._jobs[key] = {
        "status": "preparing",
        "progress": 0,
        "message": "Preparing video...",
        "timings": {},
        "started_at": 0.0,
        "cancel_event": threading.Event(),
    }

    def fake_download(url, selected_quality, progress_callback, cancel_event):
        assert selected_quality == quality
        assert not cancel_event.is_set()
        progress_callback({
            "progress": 54,
            "message": "Merging video and audio...",
            "timings": {"metadata_extraction_seconds": 0.3, "download_seconds": 2.0},
        })
        return {
            "id": "abc123",
            "title": "Example title",
            "path": tmp_path / "abc123_480p.mp4",
            "timings": {"metadata_extraction_seconds": 0.3, "download_seconds": 2.0, "merge_seconds": 0.2},
        }

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
    assert payload["timings"]["metadata_extraction_seconds"] == 0.3
    assert payload["timings"]["download_seconds"] == 2.0
    assert payload["timings"]["merge_seconds"] == 0.2


def test_background_preparation_can_be_cancelled():
    video_url = "https://youtu.be/abcdefghijk"
    quality = 480
    key = app_module._job_key(video_url, quality)
    cancel_event = threading.Event()
    app_module._jobs[key] = {
        "status": "preparing",
        "cancel_event": cancel_event,
    }

    response = app.test_client().post(
        "/api/cancel",
        data={"url": "abcdefghijk", "quality": "480"},
        environ_base={"REMOTE_ADDR": "192.0.2.104"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "cancelling"
    assert cancel_event.is_set()


def test_stream_records_time_until_first_byte_and_keeps_file_active(tmp_path, monkeypatch):
    import os
    import time

    video_file = tmp_path / "abcdefghijk_480p.mp4"
    video_file.write_bytes(b"stream bytes")
    monkeypatch.setattr(app_module, "cache_path", lambda video_id, quality: video_file)
    monkeypatch.setattr(app_module, "CACHE_DIR", tmp_path, raising=False)
    monkeypatch.setattr("player.CACHE_DIR", tmp_path)
    old_time = time.time() - player.CACHE_MAX_AGE_SECONDS - 5
    os.utime(video_file, (old_time, old_time))
    key = app_module._job_key("abcdefghijk", 480)
    app_module._jobs[key] = {
        "status": "ready",
        "started_at": time.monotonic() - 2,
        "timings": {},
        "video_id": "abcdefghijk",
        "cancel_event": threading.Event(),
    }
    monkeypatch.setattr(app_module, "_start_job", lambda *args, **kwargs: key)
    response = app.test_client().get(
        "/stream",
        query_string={"id": "abcdefghijk", "quality": "480"},
        buffered=False,
    )

    assert next(iter(response.response)) == b"stream bytes"
    assert app_module._jobs[key]["timings"]["time_to_first_byte_seconds"] >= 2
    assert player.cache_file_is_active(video_file)
    response.close()
    assert not player.cache_file_is_active(video_file)


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
    assert set(response.get_json()["quality_sizes"]) == {"360", "480", "720"}
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
    assert requested == {"video_id": "abcdefghijk", "quality": 480}


def test_stream_returns_not_found_before_video_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "cache_path", lambda video_id, quality: tmp_path / "missing.mp4")

    response = app.test_client().get("/stream", query_string={"id": "abc123", "quality": "720"})

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ('A <great>: "video"/name?*', "A great videoname.mp4"),
        ("CON", "_CON.mp4"),
        ("x" * 105, f'{"x" * 100}.mp4'),
        (' .<>:"/\\|?* ', "video.mp4"),
    ],
)
def test_download_filename_is_safe_for_windows(title, expected):
    assert app_module.clean_download_filename(title) == expected


@pytest.mark.parametrize(
    ("video_id", "quality"),
    [
        ("short", "720"),
        ("abcdefghij!", "720"),
        ("abcdefghijk", "1080"),
        ("abcdefghijk", "480p"),
    ],
)
def test_download_rejects_invalid_video_id_or_quality(video_id, quality):
    response = app.test_client().get(
        "/download",
        query_string={"id": video_id, "quality": quality},
        environ_base={"REMOTE_ADDR": "192.0.2.101"},
    )

    assert response.status_code == 400


def test_download_returns_conflict_while_video_is_preparing(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "cache_path", lambda video_id, quality: tmp_path / "missing.mp4")

    response = app.test_client().get(
        "/download",
        query_string={"id": "abcdefghijk", "quality": "720"},
        environ_base={"REMOTE_ADDR": "192.0.2.102"},
    )

    assert response.status_code == 409
    assert response.get_data(as_text=True) == "Video is still preparing."


def test_download_serves_cached_video_as_attachment_with_clean_title(tmp_path, monkeypatch):
    video_file = tmp_path / "abcdefghijk_480p.mp4"
    video_file.write_bytes(b"cached mp4")
    video_file.with_suffix(".json").write_text(
        '{"id":"abcdefghijk","title":"A <video>: file/name?"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(app_module, "cache_path", lambda video_id, quality: video_file)

    response = app.test_client().get(
        "/download",
        query_string={"id": "abcdefghijk", "quality": "480"},
        environ_base={"REMOTE_ADDR": "192.0.2.103"},
    )

    assert response.status_code == 200
    assert response.data == b"cached mp4"
    assert response.headers["Content-Type"].startswith("video/mp4")
    assert response.headers["Content-Disposition"] == 'attachment; filename="A video filename.mp4"'