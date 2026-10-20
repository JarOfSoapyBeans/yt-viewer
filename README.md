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
start from the recommendations on the home page, search for a video, or paste
a YouTube video URL to open it directly. Search and playback stay on the same
page; searches use the `?q=...` query and playback uses the player view. Search
and watch history is stored in the browser, with controls to pause or clear it.
Recommendation results are cached in the current tab for 10 minutes. Choose
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
requests against temporary files. `python -m pytest tests/test_feed.py` checks
recommendation interleaving, duplicate removal, and watched-video filtering
without network access. `python -m pytest` runs the complete suite.

## Home page smoke test

1. Open `/`, use **Clear history**, and confirm the welcome message and starter
   topic chips appear. Choose a topic and confirm the usual `?q=...` results.
2. Search for at least two different topics, then return to `/`. Confirm the
   recent-search chips, both “Because you searched” rows, and recommendations
   appear. Search history should be lowercase, newest first.
3. Open a recommendation and use the player’s back button. Confirm the home
   feed returns at the previous scroll position without repeating feed requests.
   The video should appear in “Recently watched”; use its `x` to remove it.
4. Pause history, search and open a video, then return home. Confirm neither
   action was added to history. Unpause and use **Clear history** to confirm the
   feed and history rows are cleared.
5. For **Show more**, build a history with at least four searches and use the
   browser Network panel to confirm that the first 24 recommendation cards
   expand using already-fetched results. Feed requests should be served again
   after the 10-minute session cache expires.

## Sidebar panels smoke test

Test in this order:

1. **Persistent layout and sidebar:** Open `/`, run a search, open a video, and
   open/close History and Settings each time. Confirm the main area remains
   visible and playback continues while a panel is open. Click the active
   sidebar item, the close button, outside the panel, and press Escape to close
   it. Confirm focus returns to the sidebar item and Tab stays inside an open
   panel. Refresh `?view=history` and `?view=settings`, then use Back/Forward.
   At phone width, confirm bottom navigation and a bottom-sheet panel. Collapse
   the desktop sidebar and refresh to verify the choice is remembered.
2. **Settings panel:** Change theme, default quality, autoplay, recommendation
   options, and result batch size; verify each applies immediately. Pause
   history, try a search and open a video, then unpause and confirm neither was
   saved. Test each confirmed clear action, export data, clear history, import
   the exported JSON, and then use **Reset to defaults**.
3. **History panel:** Add a few searches and open videos. Check Watched and
   Searches tabs, title-only watched rows and date groups, filtering, removing
   one item, and **Clear all** with both confirmation choices. Verify row
   clicks open playback or rerun a search and close the panel, and that empty
   states appear after clearing.

## Player UI smoke test

1. Open a video from search. Confirm the page title is followed closely by a
   16:9 black player, then the video title/details and the **Back to results**,
   quality, and playback-speed controls. Confirm the sidebar remains available.
   At phone width, verify the player/actions stack and the action buttons fill
   the content width.
2. With autoplay off, confirm the existing “Preparing video...” progress is
   available in the collapsed **Video status** section and that the player
   becomes ready without changing the `/api/status` then `/stream` flow.
   Expand the section to inspect progress. Change quality and confirm the
   existing preparation starts again at the selected quality.
3. Use the player overlay to play/pause, drag or click the seek bar, skip
   backward/forward, adjust volume, change speed, enter picture-in-picture
   where supported, and enter/exit fullscreen. In the Network panel, confirm
   seeking requests `/stream` with a `Range` header and receives partial
   content. Hover or touch to show the controls and wait 2.5 seconds while
   playing to confirm they fade away; pause to confirm they stay visible.
4. Focus the player and test Space/K, Left/Right, J/L, Up/Down, M, F, and
   number keys 0–9. Focus the search field and confirm typing does not trigger
   player shortcuts. Refresh and verify volume, mute, and speed are remembered.
5. Simulate a stream failure (for example, use the browser Network panel to
   block the `/stream` request) and confirm the error overlay offers **Retry**.
   Check the accent-colored scrollbar in the main page and the History and
   Settings panels, and enable reduced motion in the OS/browser to verify
   fades and transitions are disabled.
6. Right-click the video picture (not a control) and confirm the browser offers
   **Save video as**. Test both **Download** buttons with the video ready and
   confirm the MP4 name is based on the title. Change quality and download
   again to verify the selected quality is used. While a video is preparing,
   use either Download button and confirm **Video is still preparing** appears
   in the expanded Video status section.