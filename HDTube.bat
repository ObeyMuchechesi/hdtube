@echo off
title HDTube
cd /d "%~dp0"

echo.
echo   HDTube - starting...
echo.

python -c "import yt_dlp, imageio_ffmpeg" >nul 2>&1
if errorlevel 1 (
    echo   First run - installing dependencies, this takes a moment.
    python -m pip install --quiet --upgrade yt-dlp imageio-ffmpeg
    if errorlevel 1 (
        echo.
        echo   Could not install dependencies. Make sure Python 3.9+ is installed.
        pause
        exit /b 1
    )
    echo   Ready.
    echo.
)

python app.py
if errorlevel 1 pause
