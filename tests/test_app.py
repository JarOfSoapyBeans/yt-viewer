import pytest

from app import app
from player import NetworkError, VideoUnavailableError


def test_stream_forwards_range_and_relays_partial_response(monkeypatch):
    captured = {}

    class UpstreamResponse:
        status_code = 206
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": "6",
            "Content-Range": "bytes 5-10/100",
            "Accept-Ranges": "bytes",
        }

        def iter_content(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"abc"
            yield b"123"

        def close(self):
            captured["closed"] = True

    def fake_get(media_url, headers, stream, timeout):
        captured.update(url=media_url, headers=headers, stream=stream, timeout=timeout)
        return UpstreamResponse()

    monkeypatch.setattr("app.get_stream_url", lambda url, quality: ("https://media/video", {"User-Agent": "viewer"}))
    monkeypatch.setattr("app.requests.get", fake_get)

    response = app.test_client().get(
        "/stream",
        query_string={"url": "https://youtu.be/abc123", "quality": "low"},
        headers={"Range": "bytes=5-10"},
    )

    assert response.status_code == 206
    assert response.data == b"abc123"
    assert response.headers["Content-Type"] == "video/mp4"
    assert response.headers["Content-Length"] == "6"
    assert response.headers["Content-Range"] == "bytes 5-10/100"
    assert captured["headers"]["Range"] == "bytes=5-10"
    assert captured["headers"]["Accept-Encoding"] == "identity"
    assert captured["stream"] is True
    assert captured["closed"] is True


def test_stream_rejects_malformed_range_before_upstream_request(monkeypatch):
    monkeypatch.setattr("app.get_stream_url", lambda url, quality: ("https://media/video", {}))
    monkeypatch.setattr("app.requests.get", lambda *args, **kwargs: pytest.fail("unexpected upstream request"))

    response = app.test_client().get(
        "/stream",
        query_string={"url": "https://youtu.be/abc123"},
        headers={"Range": "bytes=invalid"},
    )

    assert response.status_code == 400
    assert b"Malformed byte range" in response.data


def test_watch_reports_network_failures_as_bad_gateway(monkeypatch):
    def fail_info(url):
        raise NetworkError("Could not reach YouTube.")

    monkeypatch.setattr("app.get_info", fail_info)

    response = app.test_client().post("/watch", data={"url": "https://youtu.be/abc123"})

    assert response.status_code == 502
    assert b"Could not reach YouTube" in response.data


def test_watch_reports_missing_url_with_clear_400():
    client = app.test_client()

    assert b'name="url"' in client.get("/").data
    response = client.post("/watch", data={})

    assert response.status_code == 400
    assert b"YouTube URL is required." in response.data


def test_watch_displays_original_error_and_logs_traceback(monkeypatch, caplog):
    detail = "ERROR: [youtube] example: Sign in to confirm your age"

    def fail_info(url):
        raise VideoUnavailableError(detail)

    monkeypatch.setattr("app.get_info", fail_info)

    response = app.test_client().post("/watch", data={"url": "https://youtu.be/abc123"})

    assert response.status_code == 400
    assert detail.encode() in response.data
    assert "YouTube metadata lookup failed" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text