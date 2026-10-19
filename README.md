# YouTube Viewer

A small Flask app for searching YouTube videos and playing cached H.264/AAC
streams. Playback merges video and audio into MP4 with ffmpeg. The `.cache/`
folder is cleaned of files older than one hour and is excluded from git.

## Run

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Install ffmpeg separately before starting the app. On Debian/Ubuntu, run
`sudo apt install ffmpeg`. `python app.py` starts Waitress bound to
`127.0.0.1:5000` with Flask debug disabled. Open `http://127.0.0.1:5000` and
search for a video, or paste a YouTube video URL to open it directly. Choose
360p, 480p, or 720p in the player. Video and audio download in the background;
the page shows progress and enables playback when the MP4 is ready. Search
results are held in memory for 10 minutes. Non-media requests are limited to
10 per minute per IP; video Range requests are exempt so seeking continues to
work.

`GET /api/search?q=...&limit=12&skip=0` returns flat video metadata without
downloading media. The limit is the total yt-dlp result window, up to 96;
`skip` returns only results after the already displayed page.
`GET /api/info?url=...` fetches metadata for a video. The info and status
endpoints accept either a YouTube URL or an 11-character video ID.

## How to run

On Windows, create `.venv` and install the requirements first. Ensure
`cloudflared` is on `PATH` and `%USERPROFILE%\.cloudflared\config-yt-viewer.yml`
is configured for the `yt-viewer` tunnel, then run `start-all.bat`. It opens one
Command Prompt for the app and another for the Cloudflare tunnel.

## Test

```sh
python -m pytest
```

## Diagnose yt-dlp

Inspect a video URL directly, without starting Flask or downloading the video:

```sh
python check_ytdlp.py "https://www.youtube.com/watch?v=..."
```

On failure, the script prints the yt-dlp error and full traceback.

## Test ffmpeg and cache behavior

`python -m pytest tests/test_player.py` checks URL/ID normalization, flat
metadata extraction, search validation and caching, format selection, ffmpeg
requirements, cache expiry, and cached-file reuse without contacting YouTube.
`python -m pytest tests/test_app.py` checks search API validation and rate
limiting, ID normalization, preparation status, playback routes, and Range
requests against temporary files. `python -m pytest` runs the complete suite.