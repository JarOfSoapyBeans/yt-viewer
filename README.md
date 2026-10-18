# YouTube Viewer

A small Flask app that plays YouTube progressive MP4 streams in the browser. It
does not download videos to disk; seeking is supported through HTTP byte ranges.

## Run

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000` and submit a YouTube video URL. Only formats that
contain both audio and video in one MP4 stream are offered.

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