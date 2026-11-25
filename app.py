"""HDTube - local web server (stdlib only, no Flask needed)."""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui"

# On a cloud host like Vercel the filesystem is read-only and functions are
# stateless, so we cannot create a ~/Videos/HDTube output directory eagerly.
# Use /tmp for writes if PORT is set, otherwise the user's Videos folder.
if os.environ.get("PORT"):
    DEFAULT_OUT = Path("/tmp/hdtube-out")
    _VERCEL = True
else:
    DEFAULT_OUT = Path.home() / "Videos" / "HDTube"
    _VERCEL = False

try:
    from downloader import JobManager, probe_env, inspect_target, split_for_fat32, FFMPEG
    MANAGER = JobManager(DEFAULT_OUT)
except Exception as _import_err:
    # Degrade gracefully on serverless hosts where yt-dlp/ffmpeg cannot import
    JobManager = None  # type: ignore
    probe_env = lambda: {"yt_dlp": False, "ffmpeg": None, "error": str(_import_err)}  # type: ignore
    inspect_target = lambda path: {"error": str(_import_err)}  # type: ignore
    split_for_fat32 = None  # type: ignore
    FFMPEG = None
    MANAGER = None



class Handler(BaseHTTPRequestHandler):
    server_version = "HDTube/1.0"

    # ---------- plumbing ----------
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _serve_file(self, path: Path, download=False):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------- routes ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            return self._serve_file(UI / "index.html")
        if p.startswith("/static/"):
            target = (UI / p[len("/static/"):]).resolve()
            if UI.resolve() in target.parents:
                return self._serve_file(target)
            self.send_error(403)
            return

        if p == "/api/env":
            return self._json(probe_env())

        if p == "/api/jobs":
            return self._json({"jobs": MANAGER.all() if MANAGER else [],
                               "default_dir": str(DEFAULT_OUT),
                               "serverless": _VERCEL})

        if p == "/api/target":
            path = (q.get("path") or [str(DEFAULT_OUT)])[0]
            return self._json(inspect_target(path))

        if p == "/api/browse":
            # open a native folder picker on the machine running the server
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
                chosen = filedialog.askdirectory(title="Choose a folder (e.g. your USB drive)")
                root.destroy()
                return self._json({"path": chosen or ""})
            except Exception as e:
                return self._json({"path": "", "error": str(e)})

        if p == "/api/reveal":
            fp = (q.get("path") or [""])[0]
            try:
                if os.name == "nt":
                    os.startfile(str(Path(fp).parent if Path(fp).is_file() else fp))
                elif sys_platform_ok():
                    import subprocess
                    subprocess.Popen(["open" if False else "xdg-open", str(Path(fp).parent)])
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})

        if p == "/api/file":
            fp = (q.get("path") or [""])[0]
            return self._serve_file(Path(fp), download=(q.get("dl") == ["1"]))

        self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        body = self._read_json()

        if p == "/api/info":
            if not MANAGER:
                return self._json({"error": "Serverless mode: YouTube downloading is not available here. Please run HDTube locally."}, 503)
            url = (body.get("url") or "").strip()
            if not url:
                return self._json({"error": "Paste a YouTube link first."}, 400)
            try:
                return self._json(MANAGER.fetch_info(url))
            except Exception as e:
                return self._json({"error": str(e)[:300]}, 400)

        if p == "/api/download":
            if not MANAGER:
                return self._json({"error": "Serverless mode: downloads are not available here. Please run HDTube locally."}, 503)
            urls = body.get("urls") or ([body["url"]] if body.get("url") else [])
            urls = [u for u in urls if isinstance(u, str) and u.strip()]
            if not urls:
                return self._json({"error": "No links supplied."}, 400)
            outdir = Path(body.get("outdir") or DEFAULT_OUT)
            mode = body.get("mode") or "universal"
            quality = str(body.get("quality") or "best")
            codec = body.get("codec") or "h264"
            hardware = body.get("hardware") or "auto"

            ids = []
            for u in urls:
                job = MANAGER.add(u)
                MANAGER.start(job, mode, outdir, quality, codec, hardware)
                ids.append(job.id)
            return self._json({"ids": ids})

        if p == "/api/cancel":
            if MANAGER:
                MANAGER.cancel(body.get("id") or "")
            return self._json({"ok": True})

        if p == "/api/split":
            if not split_for_fat32:
                return self._json({"error": "Serverless mode: file splitting is not available here."}, 503)
            fp = body.get("path") or ""
            try:
                parts = split_for_fat32(fp)
                return self._json({"ok": True, "parts": parts, "count": len(parts)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:300]}, 400)

        if p == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        self.send_error(404)


def sys_platform_ok() -> bool:
    return os.name != "nt"


def run(port: int | None = None, open_browser: bool = True):
    # Hosting platforms (Render, Railway, Fly, Heroku) inject PORT and expect the
    # process to bind 0.0.0.0. Locally we stay on loopback so nothing is exposed.
    env_port = os.environ.get("PORT")
    if port is None:
        port = int(env_port) if env_port else 7788
    host = "0.0.0.0" if env_port else os.environ.get("HDTUBE_HOST", "127.0.0.1")
    local = host in ("127.0.0.1", "localhost")

    srv = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{'127.0.0.1' if local else host}:{port}/"
    print("\n  HDTube is running")
    print(f"  -> {url}")
    print("  ffmpeg:", FFMPEG or "NOT FOUND")
    print("  Close this window to stop.\n")
    if open_browser and local:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    run()

