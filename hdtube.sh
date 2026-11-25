#!/usr/bin/env bash
# HDTube launcher for macOS / Linux
cd "$(dirname "$0")" || exit 1

if ! python3 -c "import yt_dlp, imageio_ffmpeg" >/dev/null 2>&1; then
  echo "First run - installing dependencies..."
  python3 -m pip install --quiet --upgrade yt-dlp imageio-ffmpeg || {
    echo "Install failed. Ensure python3 and pip are available."; exit 1; }
fi

exec python3 app.py
