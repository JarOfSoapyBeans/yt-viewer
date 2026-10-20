import threading
import time

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
    get_info,
    search_videos,
    validate_url,
)


def test_get_info_includes_video_details_for_player_view(monkeypatch):
    monkeypatch.setattr(
        player,
        "_extract_info",
        lambda url: {
            "id": "abcdefghijk",
            "title": "Example video",
            "duration": 120,
            "thumbnail": "https://example.test/thumb.jpg",
            "uploader": "Example channel",
            "view_count": 1234,
            "upload_date": "20250102",
        },
    )

    info = get_info("abcdefghijk")

    assert info == {
        "id": "abcdefghijk",
        "title": "Example video",
        "duration": 120,
        "thumbnail": "https://example.test/thumb.jpg",
        "channel": "Example channel",
        "view_count": 1234,
        "upload_date": "20250102",
    }


def test_extract_info_is_cached_by_video_id_for_thirty_minutes(monkeypatch):
    extracted = []
    clock = [100.0]
    monkeypatch.setattr(player, "_info_cache", {})
    monkeypatch.setattr(player, "_info_locks", {})
    monkeypatch.setattr(player.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        player,
        "_extract_info_uncached",
        lambda url: extracted.append(url) or {"id": "abcdefghijk", "title": "Cached"},
    )

    first = player._extract_info("abcdefghijk")
    second = player._extract_info("https://youtu.be/abcdefghijk")
    assert first is second
    assert len(extracted) == 1

    clock[0] += player.INFO_CACHE_TTL_SECONDS + 1
    player._extract_info("abcdefghijk")
    assert len(extracted) == 2


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


def test_validate_url_converts_plain_video_id_to_watch_url():
    assert validate_url("abcdefghijk") == "https://www.youtube.com/watch?v=abcdefghijk"


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


def test_cache_key_and_video_locks_are_canonical():
    assert player.cache_key("abcdefghijk", "480p") == ("abcdefghijk", 480)
    assert player.cache_key("abcdefghijk", 480) == player.cache_key("abcdefghijk", "480")
    assert player._lock_for_video("abcdefghijk") is player._lock_for_video("abcdefghijk")
    assert player._lock_for_video("abcdefghijk") is not player._lock_for_video("lmnopqrstuv")


def test_separate_format_downloads_run_in_parallel():
    barrier = threading.Barrier(2)

    class FakeDownloader:
        def dl(self, name, info):
            barrier.wait(timeout=2)
            return True, True

    downloader = FakeDownloader()
    formats = [
        {"format_id": "video", "url": "https://example.test/video"},
        {"format_id": "audio", "url": "https://example.test/audio"},
    ]

    with player._parallelize_format_downloads(downloader, formats):
        assert downloader.dl("video.mp4", formats[0]) == (True, True)
        assert downloader.dl("audio.m4a", formats[1]) == (True, True)


def test_quality_size_labels_are_estimates_without_network():
    assert player.estimated_size_label(60, 480) == "~8 MB"
    assert player.estimated_size_label(0, 720) == "~1 KB"


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


def test_clean_cache_preserves_active_cache_files(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    active_file = tmp_path / "active.mp4"
    stale_file = tmp_path / "stale.mp4"
    active_file.touch()
    stale_file.touch()
    import os

    stale_timestamp = 10_000 - player.CACHE_MAX_AGE_SECONDS - 1
    os.utime(active_file, (stale_timestamp, stale_timestamp))
    os.utime(stale_file, (stale_timestamp, stale_timestamp))

    with player.protect_cache_file(active_file):
        assert clean_cache(now=10_000) == 1
        assert active_file.exists()
        assert not stale_file.exists()


def test_download_video_merges_to_mp4_and_reuses_cached_file(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(player, "_extract_info", lambda url: {"id": "abc123", "title": "Test"})
    monkeypatch.setattr(player, "info_timing", lambda video_id: 0.25)
    monkeypatch.setattr(player, "require_ffmpeg", lambda: None)
    monkeypatch.setattr(player, "ARIA2C_PATH", "/usr/bin/aria2c")
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def process_ie_result(self, info, download):
            assert download is True
            captured["info"] = info
            output = Path(captured["options"]["outtmpl"].replace("%(ext)s", "mp4"))
            output.touch()

    from pathlib import Path

    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    callbacks = []
    result = download_video("https://youtu.be/abc123", 480, callbacks.append)

    assert result["path"] == cache_path("abc123", 480)
    assert result["cached"] is False
    assert captured["options"]["format"] == format_selector(480)
    assert captured["options"]["merge_output_format"] == "mp4"
    assert captured["options"]["outtmpl"].endswith("abc123_480p.%(ext)s")
    assert captured["options"]["concurrent_fragment_downloads"] == 8
    assert captured["options"]["http_chunk_size"] == 10_485_760
    assert captured["options"]["retries"] == 5
    assert captured["options"]["fragment_retries"] == 5
    assert captured["options"]["postprocessor_args"]["merger+ffmpeg_o"] == [
        "-c", "copy", "-movflags", "+faststart"
    ]
    assert captured["options"]["external_downloader"] == {"https": "/usr/bin/aria2c"}
    assert captured["options"]["external_downloader_args"]["aria2c"] == [
        "-x", "16", "-s", "16", "-k", "1M"
    ]
    assert callbacks[-1]["progress"] == 100
    assert callbacks[-1]["timings"]["metadata_extraction_seconds"] == 0.25

    cached_result = download_video("https://youtu.be/abc123", 480)
    assert cached_result["cached"] is True


def test_simultaneous_download_requests_share_the_video_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(player, "_extract_info", lambda url: {"id": "abc123", "title": "Test"})
    monkeypatch.setattr(player, "info_timing", lambda video_id: 0.1)
    monkeypatch.setattr(player, "require_ffmpeg", lambda: None)
    download_count = 0
    count_lock = threading.Lock()

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def process_ie_result(self, info, download):
            nonlocal download_count
            assert download
            with count_lock:
                download_count += 1
            time.sleep(0.03)
            output = Path(self.options["outtmpl"].replace("%(ext)s", "mp4"))
            output.touch()

    from pathlib import Path

    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    results = []
    workers = [
        threading.Thread(target=lambda: results.append(download_video("https://youtu.be/abc123", 480)))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert download_count == 1
    assert len(results) == 2


def test_download_video_requires_ffmpeg_for_uncached_video(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
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
    result = download_video("https://youtu.be/abc123", 480)

    assert result["cached"] is True
    assert result["path"] == cached_file
    assert result["title"] == "Cached title"


def test_download_video_preserves_ytdlp_error_text(tmp_path, monkeypatch):
    monkeypatch.setattr(player, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(player, "_extract_info", lambda url: {"id": "abc123", "title": "Test"})
    monkeypatch.setattr(player, "require_ffmpeg", lambda: None)

    class BrokenYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def process_ie_result(self, info, download):
            raise DownloadError("ERROR: requested test format is unavailable")

    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", BrokenYoutubeDL)

    with pytest.raises(VideoUnavailableError, match="requested test format is unavailable"):
        download_video("https://youtu.be/abc123", 720)


def test_search_videos_extracts_flat_metadata_and_falls_back_to_thumbnail(monkeypatch):
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, query, download):
            captured.update(query=query, download=download)
            return {
                "entries": [
                    {
                        "id": "abcdefghijk",
                        "title": "Example",
                        "uploader": "Channel",
                        "duration": 91,
                        "view_count": 1200,
                        "upload_date": "20261001",
                    }
                ]
            }

    monkeypatch.setattr(player, "_search_cache", {})
    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    videos = search_videos("  cats  ", 12)

    assert captured["query"] == "ytsearch12:cats"
    assert captured["download"] is False
    assert captured["options"]["extract_flat"] is True
    assert captured["options"]["skip_download"] is True
    assert videos == [
        {
            "id": "abcdefghijk",
            "title": "Example",
            "channel": "Channel",
            "duration": 91,
            "view_count": 1200,
            "upload_date": "20261001",
            "thumbnail": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
        }
    ]


@pytest.mark.parametrize("query", ["", "   ", "x" * 201])
def test_search_videos_rejects_invalid_queries(query):
    with pytest.raises(ValueError):
        search_videos(query)


def test_search_videos_rejects_limits_above_96():
    with pytest.raises(ValueError, match="96 or fewer"):
        search_videos("cats", 97)


def test_search_videos_caches_by_query_and_limit(monkeypatch):
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, query, download):
            calls.append(query)
            return {
                "entries": [
                    {
                        "id": "abcdefghijk",
                        "title": "Original",
                        "thumbnail": "https://example.test/thumb.jpg",
                    }
                ]
            }

    monkeypatch.setattr(player, "_search_cache", {})
    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    first = search_videos("cache test", 12)
    first[0]["title"] = "mutated"
    second = search_videos("cache test", 12)
    search_videos("cache test", 24)

    assert calls == ["ytsearch12:cache test", "ytsearch24:cache test"]
    assert second[0]["title"] == "Original"


def test_search_videos_extends_cached_results_and_skips_duplicates(monkeypatch):
    calls = []

    def entry(number):
        video_id = f"vid{number:08d}"
        return {
            "id": video_id,
            "title": f"Video {number}",
            "thumbnail": f"https://example.test/{video_id}.jpg",
        }

    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, query, download):
            calls.append(query)
            if query.startswith("ytsearch12:"):
                entries = [entry(number) for number in range(1, 13)]
            else:
                entries = [entry(number) for number in range(12, 0, -1)]
                entries.extend(entry(number) for number in range(13, 25))
            return {"entries": entries}

    monkeypatch.setattr(player, "_search_cache", {})
    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    first_page = search_videos("dedupe", 12)
    second_page = search_videos("dedupe", 24, skip=12)
    cached_second_page = search_videos("dedupe", 24, skip=12)

    expected_ids = [f"vid{number:08d}" for number in range(13, 25)]
    assert calls == ["ytsearch12:dedupe", "ytsearch24:dedupe"]
    assert [video["id"] for video in first_page] == [f"vid{number:08d}" for number in range(1, 13)]
    assert [video["id"] for video in second_page] == expected_ids
    assert [video["id"] for video in cached_second_page] == expected_ids
    assert len({video["id"] for video in second_page}) == 12


def test_search_videos_refreshes_expired_cache_entry(monkeypatch):
    now = player.time.monotonic()
    monkeypatch.setattr(
        player,
        "_search_cache",
        {
            ("expired search", 12): (
                now - player.SEARCH_CACHE_TTL_SECONDS - 1,
                [{"id": "oldresultid", "title": "Old"}],
            )
        },
    )
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, query, download):
            calls.append(query)
            return {
                "entries": [
                    {
                        "id": "newresultid",
                        "title": "New",
                    }
                ]
            }

    monkeypatch.setattr(player.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    results = search_videos("expired search", 12)

    assert calls == ["ytsearch12:expired search"]
    assert results[0]["title"] == "New"