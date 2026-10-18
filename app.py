"""Flask web interface for streaming YouTube videos."""

from flask import Flask, Response, render_template_string, request, url_for
import requests

from player import NetworkError, PlayerError, get_info, get_stream_url, parse_range_header


app = Flask(__name__)

HOME_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Viewer</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; background: #eef1e8; color: #1d2a24; }
    body { margin: 0; min-height: 100vh; background: repeating-linear-gradient(135deg, transparent 0 28px, #dfe5d9 29px 30px); }
    main { box-sizing: border-box; width: min(100% - 40px, 960px); margin: 8vh auto; padding: 32px 0; border-top: 5px solid #dc4e37; }
    h1 { margin: 0 0 28px; font: 700 clamp(2rem, 8vw, 4.5rem)/1 Georgia, serif; }
    form { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
    label { flex-basis: 100%; font-size: .85rem; font-weight: 700; }
    input, select, button { box-sizing: border-box; min-height: 44px; border: 1px solid #87948a; border-radius: 3px; padding: 10px 12px; font: inherit; }
    input { flex: 1 1 280px; min-width: 0; background: #fff; }
    button { background: #1d2a24; color: #fff; cursor: pointer; }
    [role="alert"] { color: #a52b1c; }
    @media (max-width: 480px) { main { width: min(100% - 28px, 960px); margin-top: 28px; } form > button { flex: 1; } }
  </style>
</head>
<body>
  <main>
    <h1>YouTube Viewer</h1>
    {% if error %}<p role="alert">{{ error }}</p>{% endif %}
    <form action="{{ url_for('watch') }}" method="post">
      <label for="video-url">YouTube URL</label>
      <input id="video-url" name="url" type="url" required value="{{ url }}" placeholder="https://www.youtube.com/watch?v=...">
      <button type="submit">Watch</button>
    </form>
  </main>
</body>
</html>
"""

WATCH_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} | YouTube Viewer</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; background: #eef1e8; color: #1d2a24; }
    body { margin: 0; min-height: 100vh; background: repeating-linear-gradient(135deg, transparent 0 28px, #dfe5d9 29px 30px); }
    main { box-sizing: border-box; width: min(100% - 40px, 960px); margin: 5vh auto; padding: 24px 0; border-top: 5px solid #dc4e37; }
    h1 { margin: 18px 0 24px; font: 700 clamp(1.8rem, 6vw, 3.5rem)/1.05 Georgia, serif; overflow-wrap: anywhere; }
    a { color: #a52b1c; }
    form { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 18px; }
    label { font-size: .85rem; font-weight: 700; }
    select { min-height: 42px; border: 1px solid #87948a; border-radius: 3px; padding: 8px 32px 8px 10px; font: inherit; background: #fff; }
    video { display: block; width: 100%; max-width: 960px; aspect-ratio: 16 / 9; background: #101512; object-fit: contain; }
    [role="alert"] { color: #a52b1c; }
    @media (max-width: 480px) { main { width: min(100% - 28px, 960px); margin-top: 20px; } }
  </style>
</head>
<body>
  <main>
    <p><a href="{{ url_for('index') }}">New video</a></p>
    <h1>{{ title }}</h1>
    {% if error %}<p role="alert">{{ error }}</p>{% endif %}
    {% if video_url %}
      <form action="{{ url_for('watch') }}" method="post">
        <input type="hidden" name="url" value="{{ video_url }}">
        <label for="quality">Quality</label>
        <select id="quality" name="quality" onchange="this.form.requestSubmit()">
          {% for item in formats %}
            <option value="{{ item.format_id }}" {% if item.format_id == quality %}selected{% endif %}>
              {{ item.height or item.format_note or 'MP4' }}{% if item.height %}p{% endif %}
            </option>
          {% endfor %}
        </select>
      </form>
      <video controls playsinline preload="metadata" width="960" poster="{{ thumbnail or '' }}"
             src="{{ url_for('stream', url=video_url, quality=quality) }}">
        Your browser does not support HTML5 video.
      </video>
    {% endif %}
  </main>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(HOME_TEMPLATE, error=None, url="")


@app.post("/watch")
def watch():
    video_url = request.form.get("url", "")
    if not video_url.strip():
        return render_template_string(
            HOME_TEMPLATE,
            error="YouTube URL is required.",
            url="",
        ), 400
    video_url = video_url.strip()
    quality = request.form.get("quality", "best")
    try:
        info = get_info(video_url)
    except PlayerError as error:
        app.logger.exception("YouTube metadata lookup failed for %r", video_url)
        status_code = 502 if isinstance(error, NetworkError) else 400
        return render_template_string(HOME_TEMPLATE, error=str(error), url=video_url), status_code
    except Exception as error:
        app.logger.exception("Unexpected error looking up YouTube video %r", video_url)
        return render_template_string(HOME_TEMPLATE, error=str(error), url=video_url), 500

    format_ids = {item["format_id"] for item in info["formats"]}
    if quality != "best" and quality not in format_ids:
        return render_template_string(
            HOME_TEMPLATE,
            error="That quality is not available for this video.",
            url=video_url,
        ), 400

    selected_quality = quality
    if selected_quality == "best":
        selected_quality = info["formats"][-1]["format_id"]
    return render_template_string(
        WATCH_TEMPLATE,
        title=info["title"],
        formats=info["formats"],
        thumbnail=info["thumbnail"],
        video_url=video_url,
        quality=selected_quality,
        error=None,
    )


@app.get("/stream")
def stream():
    video_url = request.args.get("url", "")
    quality = request.args.get("quality", "best")
    try:
        media_url, headers = get_stream_url(video_url, quality)
        range_header = request.headers.get("Range")
        if range_header:
            parse_range_header(range_header)
            headers["Range"] = range_header
        headers["Accept-Encoding"] = "identity"
        upstream = requests.get(media_url, headers=headers, stream=True, timeout=(10, 60))
    except ValueError as error:
        return str(error), 400
    except PlayerError as error:
        app.logger.exception("YouTube stream lookup failed for %r", video_url)
        status_code = 502 if isinstance(error, NetworkError) else 400
        return str(error), status_code
    except requests.RequestException as error:
        app.logger.exception("Upstream video request failed for %r", video_url)
        return str(error) or error.__class__.__name__, 502
    except Exception as error:
        app.logger.exception("Unexpected error streaming YouTube video %r", video_url)
        return str(error) or error.__class__.__name__, 502

    if upstream.status_code not in {200, 206, 416}:
        status_code = upstream.status_code
        upstream.close()
        return f"The video server returned HTTP {status_code}.", 502

    response_headers = {}
    for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        value = upstream.headers.get(name)
        if value is not None:
            response_headers[name] = value

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(generate(), status=upstream.status_code, headers=response_headers, direct_passthrough=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)