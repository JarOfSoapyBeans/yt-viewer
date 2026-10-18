import pytest
from yt_dlp.utils import DownloadError

import player
from player import (
    FFmpegNotFoundError,
    InvalidURLError,
    NetworkError,
    VideoUnavailableError,
    cache_path,
    clean_cache,
    download_video,
    format_selector,
    validate_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "  https://m.youtube.com/watch?v=abc123  ",
    ],
)
def test_validate_url_accepts_youtube_urls(url):
    assert validate_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "ftp://youtube.com/watch?v=x",
        "https://youtube.com.example/watch?v=x",
        "https://example.com/watch?v=x",
        "https://[::1/watch?v=x",
        "https://youtube.com:444/watch?v=x",
    ],
)
def test_validate_url_rejects_unsupported_urls(url):
    with pytest.raises(InvalidURLError):
        validate_url(url)


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        (360, "bv*[vcodec^=avc1][height<=360]+ba[ext=m4a]/b[ext=mp4][height<=360]/b[height<=360]"),
        (480, "bv*[vcodec^=avc1][height<=480]+ba[ext=m4a]/b[ext=mp4][height<=480]/b[height<=480]"),
        (720, "bv*[vcodec^=avc1][height<=720]+ba[ext=m4a]/b[ext=mp4]/b"),
    ],
)
def test_format_selector_limits_h264_quality(quality, expected):
    assert format_selector(quality) == expected


def test_format_selector_rejects_other_qualities():
    with pytest.raises(ValueError, match="360p, 480p, or 720p"):
        format_selector(1080)


def test_clean_cache_removes_files_older_than_one_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    stale = tmp_path / "stale.mp4"
    fresh = tmp_path / "fresh.mp4"
    stale.touch()
    fresh.touch()
    now = 10_000
    stale_timestamp = now - player.CACHE_MAX_AGE_SECONDS - 1
    import os

    os.utime(stale, (stale_timestamp, stale_timestamp))
    os.utime(fresh, (now - 60, now - 60))

    assert clean_cache(now=now) == 1
    assert not stale.exists()
    assert fresh.exists()


def test_download_video_merges_to_mp4_and_reuses_cached_file(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(player, "get_info", lambda url: {"id": "abc123", "title": "Test"})
    monkeypatch.setattr(player, "require_ffmpeg", lambda: None)
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            assert download is True
            output = Path(captured["options"]["outtmpl"].replace("%(ext)s", "mp4"))
            output.touch()
            return {"id": "abc123"}

    from pathlib import Path

    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    callbacks = []
    result = download_video("https://youtu.be/abc123", 480, callbacks.append)

    assert result["path"] == cache_path("abc123", 480)
    assert result["cached"] is False
    assert captured["options"]["format"] == format_selector(480)
    assert captured["options"]["merge_output_format"] == "mp4"
    assert captured["options"]["outtmpl"].endswith("abc123_480p.%(ext)s")
    assert callbacks[-1] == {"progress": 100, "message": "Video ready."}

    cached_result = download_video("https://youtu.be/abc123", 480)
    assert cached_result["cached"] is True


def test_download_video_requires_ffmpeg_for_uncached_video(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(player, "get_info", lambda url: {"id": "abc123", "title": "Test"})
    monkeypatch.setattr(player, "ffmpeg_path", lambda: None)

    with pytest.raises(FFmpegNotFoundError, match="Install ffmpeg"):
        download_video("https://youtu.be/abc123", 720)


def test_download_video_uses_cached_url_without_network_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    cached_file = tmp_path / "abc123_480p.mp4"
    cached_file.write_bytes(b"cached mp4")
    cached_file.with_suffix(".json").write_text(
        '{"id": "abc123", "title": "Cached title"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(player, "get_info", lambda url: pytest.fail("metadata should not be fetched"))

    result = download_video("https://youtu.be/abc123", 480)

    assert result["cached"] is True
    assert result["path"] == cached_file
    assert result["title"] == "Cached title"


def test_download_video_preserves_ytdlp_error_text(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(player, "get_info", lambda url: {"id": "abc123", "title": "Test"})
    monkeypatch.setattr(player, "require_ffmpeg", lambda: None)

    class BrokenYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download):
            raise DownloadError("ERROR: requested test format is unavailable")

    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", BrokenYoutubeDL)

    with pytest.raises(VideoUnavailableError, match="requested test format is unavailable"):
        download_video("https://youtu.be/abc123", 720)