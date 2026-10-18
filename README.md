# YouTube Viewer

A small Flask app that downloads YouTube H.264/AAC streams, merges them into MP4
with ffmpeg, and plays the cached file in the browser. The `.cache/` folder is
cleaned of files older than one hour and is excluded from git.

## Run

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Install ffmpeg separately before starting the app. On Debian/Ubuntu, run
`sudo apt install ffmpeg`. `python app.py` starts Waitress bound to
`127.0.0.1:5000` with Flask debug disabled. Open `http://127.0.0.1:5000`, submit
a YouTube video URL, and choose 360p, 480p, or 720p. Video and audio download in
the background; the page shows progress and enables playback when the MP4 is
ready. Non-media requests are limited to 10 per minute per IP; video Range
requests are exempt so seeking continues to work.

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

`python -m pytest tests/test_player.py` checks format selection, the ffmpeg
requirement, cache expiry, and cached-file reuse without contacting YouTube.
`python -m pytest tests/test_app.py` checks preparation status, playback routes,
and Range requests against temporary files.