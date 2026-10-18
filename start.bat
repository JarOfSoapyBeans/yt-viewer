@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo Could not find .venv. Create it and install requirements first.
    echo   py -m venv .venv
    echo   .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 exit /b 1

start "YouTube Viewer" cmd /k python app.py
timeout /t 2 /nobreak >nul
start "" "http://localhost:5000"
