"""Probe yt-dlp extraction outside the Flask application."""

import argparse
import sys
import traceback

import yt_dlp


def main():
    parser = argparse.ArgumentParser(description="Inspect a URL with yt-dlp without downloading it.")
    parser.add_argument("url", help="Video URL to inspect")
    args = parser.parse_args()

    options = {"quiet": True, "no_warnings": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(args.url, download=False)
    except Exception as error:
        print(f"yt-dlp failed: {error}", file=sys.stderr)
        traceback.print_exc()
        return 1

    print(f"Title: {info.get('title') or 'Unknown title'}")
    formats = info.get("formats") or []
    if not formats:
        print("Formats: none reported")
        return 0

    print("Formats:")
    for item in formats:
        resolution = item.get("resolution") or (
            f"{item.get('width')}x{item.get('height')}"
            if item.get("width") or item.get("height")
            else "audio-only/unknown"
        )
        print(
            "  {format_id} | {ext} | {resolution} | video={vcodec} | audio={acodec} | {protocol}".format(
                format_id=item.get("format_id", "?"),
                ext=item.get("ext", "?"),
                resolution=resolution,
                vcodec=item.get("vcodec", "?"),
                acodec=item.get("acodec", "?"),
                protocol=item.get("protocol", "?"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
