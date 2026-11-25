# HDTube

A local YouTube downloader that preserves source quality and warns you before a flash-drive copy fails.

## Quick start (Windows)

```bat
HDTube.bat
```

That installs `yt-dlp` and `ffmpeg` if needed, then opens the app in your browser at `http://127.0.0.1:7788`.

Close the command window to stop the app.

## Quick start (macOS / Linux)

```bash
./hdtube.sh
```

## What it does

1. Downloads the highest-resolution stream YouTube has (up to 8K) plus the best audio.
2. Merges them with `ffmpeg`.
3. Lets you choose between:
   - **Universal HD** (default) — MP4 + H.264 + AAC. Encoded once at visually-lossless CRF 18 so it plays on TVs, car head units, consoles, phones and almost everything else.
   - **Pure native** — no re-encoding. The exact video/audio streams YouTube served are muxed together. Best fidelity, but some older devices cannot decode VP9 / AV1.
   - **Audio only** — original audio remuxed to `.m4a`.
4. Inspects the destination drive's filesystem (FAT32 / exFAT / NTFS / …) and warns you if a single file cannot exceed 4 GB.
5. If you still need to copy to FAT32, it can split the finished file into `<4 GB` parts (with a `JOIN_PARTS.bat` to reassemble them on Windows).

## Why videos "lose quality" on a flash drive

Two common things happen:

- **FAT32 has a hard 4 GB file limit.** A 4K copy of a long video will silently fail partway through the transfer. HDTube detects the filesystem and tells you before the copy starts.
- **Old/smart TVs and car stereos only speak H.264.** YouTube mostly serves VP9 or AV1. If you copy those files straight across, the TV can refuse to play them. The Universal profile transcodes to H.264 so they play everywhere.

Both problems are handled without degrading the picture more than necessary.

## Manual setup

If the launchers do not work, install the two dependencies yourself:

```bash
python -m pip install -U yt-dlp imageio-ffmpeg
python app.py
```

Then open `http://127.0.0.1:7788`.

Requirements: Python 3.9+ and a working `python` on your PATH. `ffmpeg` ships with
`imageio-ffmpeg`, so nothing else needs installing.

## Using the app

1. Paste one or more YouTube links (one per line).
2. Pick a **profile** — Universal HD (most compatible), Pure native (no re-encode), or Audio only.
3. Pick the **destination folder** — point it straight at your USB drive.
4. Watch the badge next to the folder: it tells you the filesystem and whether a
   large file will fit. If it warns about FAT32, use **Split for FAT32** after the download.

## Project layout

```
HDTube/
├── HDTube.bat        # Windows launcher
├── hdtube.sh         # macOS / Linux launcher
├── app.py            # local HTTP server + JSON API (stdlib only)
├── downloader.py     # download engine: yt-dlp, ffmpeg, filesystem probing, splitting
├── ui/index.html     # single-file web interface
└── README.md
```

## Notes

- Keep the app window open while a download is in progress.
- The app only talks to YouTube. Nothing is uploaded anywhere.
- If YouTube asks for a bot check, export your browser cookies and set `HDTUBE_COOKIES=<path>` before launching, or use a different video.

## Legal

Downloading videos may violate YouTube's Terms of Service. Use this only for content
you own or have permission to download — for example your own uploads, or videos
released under a permissive licence. You are responsible for how you use it.
