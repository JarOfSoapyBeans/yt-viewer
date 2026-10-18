import pytest

from player import InvalidURLError, _progressive_mp4_formats, get_stream_url, parse_range_header, validate_url


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


def test_progressive_mp4_formats_excludes_separate_and_manifest_streams():
    info = {
        "formats": [
            {"format_id": "low", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "url": "https://media/low", "height": 360},
            {"format_id": "video-only", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "url": "https://media/video"},
            {"format_id": "audio-only", "ext": "mp4", "vcodec": "none", "acodec": "mp4a", "url": "https://media/audio"},
            {"format_id": "manifest", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "protocol": "m3u8_native", "url": "https://media/manifest"},
            {"format_id": "high", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "url": "https://media/high", "height": 720},
        ]
    }

    formats = _progressive_mp4_formats(info)

    assert [item["format_id"] for item in formats] == ["low", "high"]


def test_get_stream_url_selects_requested_quality_and_merges_headers(monkeypatch):
    info = {
        "http_headers": {"User-Agent": "browser-agent", "Referer": "https://youtube.com/"},
        "formats": [
            {
                "format_id": "low",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "url": "https://media/low",
                "height": 360,
                "http_headers": {"Cookie": "video-token"},
            },
            {
                "format_id": "high",
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "mp4a",
                "url": "https://media/high",
                "height": 720,
            },
        ],
    }
    monkeypatch.setattr("player._extract_info", lambda url: info)

    media_url, headers = get_stream_url("https://youtu.be/abc123", "low")
    best_url, _ = get_stream_url("https://youtu.be/abc123")

    assert media_url == "https://media/low"
    assert headers == {
        "User-Agent": "browser-agent",
        "Referer": "https://youtube.com/",
        "Cookie": "video-token",
    }
    assert best_url == "https://media/high"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-99", (0, 99)),
        ("bytes=100-", (100, None)),
        ("bytes=-500", (None, 500)),
    ],
)
def test_parse_range_header(header, expected):
    assert parse_range_header(header) == expected


@pytest.mark.parametrize("header", ["items=0-1", "bytes=", "bytes=1-0", "bytes=-0", "bytes=0-1,3-4", "bytes=x-y"])
def test_parse_range_header_rejects_malformed_ranges(header):
    with pytest.raises(ValueError):
        parse_range_header(header)