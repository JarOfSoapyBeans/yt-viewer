@echo off
setlocal

start "YouTube Viewer" cmd /k "cd /d ""%~dp0"" && call ""%~dp0.venv\Scripts\activate.bat"" && python app.py"
start "Cloudflare Tunnel" cmd /k "cloudflared tunnel --config ""%USERPROFILE%\.cloudflared\config-yt-viewer.yml"" run yt-viewer"