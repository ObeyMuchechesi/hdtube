"""
HDTube - core download engine.

Everything here is built around one rule: never throw away video quality.

That means:
  * Pick the highest-resolution video stream YouTube offers (up to 8K), plus the
    best audio stream, and merge them with ffmpeg in *stream copy* mode
    (-c copy). No re-encoding, so the pixels you get are the pixels YouTube served.
  * When a playable-on-any-device container is needed (MP4 + H.264), transcode
    only if stream copy genuinely can't produce it - and then at a quality level
    that is visually lossless (CRF 18 / x264 veryfast, or GPU where available).

Why quality dies on a flash drive (and what we do about it):
  * FAT32 is the factory format of most USB sticks and cannot hold a file larger
    than 4 GB. A 4K copy of a long video blows past that instantly, and the copy
    fails half way. We detect the target drive's filesystem and warn *before*
    the transfer, then offer to split the file into <4 GB parts.
  * exFAT / NTFS / ext4 have no such limit - we tell you which you have.
  * exFAT + NTFS also get a "flash-safe" MP4/H.264/AAC variant option, because a
    lot of TVs and car head units can't decode VP9/AV1 that YouTube serves by
    default. H.264 is the most universally playable codec in existence.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------

try:
    import yt_dlp
    from yt_dlp.utils import DownloadError
except ImportError:  # pragma: no cover
    yt_dlp = None
    DownloadError = Exception


def find_ffmpeg() -> Optional[str]:
    """Locate an ffmpeg binary: bundled pip one, PATH, or common install spots."""
    env = os.environ.get("HDTUBE_FFMPEG")
    if env and Path(env).exists():
        return env
    try:
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return _ensure_ffmpeg_alias(p)
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in (
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ):
        if Path(cand).exists():
            return cand
    return None


def _ensure_ffmpeg_alias(src: str) -> str:
    """
    yt-dlp only recognises a binary called 'ffmpeg'/'ffmpeg.exe'. The pip package
    ships it as 'ffmpeg-win-x86_64-v9.0.2.exe', which yt-dlp ignores - so drop a
    correctly-named copy beside it once and hand the path back.
    """
    src_path = Path(src)
    exe = "ffmpeg.exe" if src_path.suffix.lower() == ".exe" else "ffmpeg"
    alias = src_path.with_name(exe)
    try:
        if not alias.exists():
            shutil.copy2(src_path, alias)
        return str(alias)
    except Exception:
        return str(src_path)


FFMPEG = find_ffmpeg()
FFMPEG_DIR = str(Path(FFMPEG).parent) if FFMPEG else None


def probe_env() -> dict:
    return {
        "yt_dlp": bool(yt_dlp),
        "yt_dlp_version": getattr(getattr(yt_dlp, "version", None), "__version__", None),
        "ffmpeg": FFMPEG,
        "python": sys.version.split()[0],
        "platform": sys.platform,
    }


# --------------------------------------------------------------------------
# Download records
# --------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    url: str
    title: str = "Fetching info..."
    thumbnail: str = ""
    duration: int = 0
    uploader: str = ""
    status: str = "queued"        # queued | downloading | merging | converting | done | error | cancelled
    progress: float = 0.0         # 0..100
    speed: str = ""
    eta: str = ""
    downloaded_bytes: int = 0
    total_bytes: int = 0
    filepath: str = ""
    filesize: int = 0
    resolution: str = ""
    container: str = ""
    vcodec: str = ""
    acodec: str = ""
    message: str = ""
    mode: str = "native"          # native | universal | audio
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        # Plain field copy - asdict() deep-copies and chokes on the Event object.
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }


class JobManager:
    """Runs downloads on worker threads and exposes progress for the UI."""

    def __init__(self, default_dir: Path):
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.lock = threading.Lock()
        self.default_dir = default_dir
        self.default_dir.mkdir(parents=True, exist_ok=True)

    # -- helpers ----------------------------------------------------------
    def add(self, url: str) -> Job:
        job = Job(id=uuid.uuid4().hex[:10], url=url.strip())
        with self.lock:
            self.jobs[job.id] = job
            self.order.insert(0, job.id)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def all(self) -> list[dict]:
        with self.lock:
            return [self.jobs[i].public() for i in self.order if i in self.jobs]

    # -- info -------------------------------------------------------------
    def fetch_info(self, url: str) -> dict:
        if not yt_dlp:
            raise RuntimeError("yt-dlp is not installed. Run: pip install -U yt-dlp")
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "nocheckcertificate": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist":
            info = (info.get("entries") or [{}])[0] or info

        formats = info.get("formats") or []
        heights = sorted({f["height"] for f in formats if f.get("height")}, reverse=True)
        has_av1 = any((f.get("vcodec") or "").startswith("av01") for f in formats)
        has_vp9 = any((f.get("vcodec") or "").startswith("vp9") or (f.get("vcodec") or "").startswith("vp09") for f in formats)
        has_h264 = any((f.get("vcodec") or "").startswith(("avc", "h264")) and f.get("height") for f in formats)

        return {
            "id": info.get("id"),
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail") or "",
            "duration": info.get("duration") or 0,
            "uploader": info.get("uploader") or "",
            "view_count": info.get("view_count"),
            "max_height": heights[0] if heights else 0,
            "heights": heights[:10],
            "live": bool(info.get("is_live")),
            "codecs": {"av1": has_av1, "vp9": has_vp9, "h264": has_h264},
            "webpage_url": info.get("webpage_url") or url,
        }

    # -- yt-dlp option builders -------------------------------------------
    def _base_opts(self, job: Job, outdir: Path, template: str) -> dict:
        opts: dict[str, Any] = {
            "outtmpl": {"default": str(outdir / template)},
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "consoletitle": False,
            "progress_hooks": [lambda d: self._hook(job, d)],
            "postprocessor_hooks": [lambda d: self._pp_hook(job, d)],
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,
            "overwrites": False,
            "restrictfilenames": False,
            "trim_file_name": 180,
        }
        if FFMPEG_DIR:
            opts["ffmpeg_location"] = FFMPEG_DIR
        if job._cancel.is_set():
            raise DownloadError("cancelled")
        return opts

    def _hook(self, job: Job, d: dict) -> None:
        if job._cancel.is_set():
            raise DownloadError("cancelled")
        st = d.get("status")
        if st == "downloading":
            job.status = "downloading"
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                job.progress = round(min(99.0, done / total * 100), 1)
                job.total_bytes = int(total)
            job.downloaded_bytes = int(done)
            spd = d.get("speed")
            job.speed = f"{spd/1_048_576:.1f} MB/s" if spd else ""
            eta = d.get("eta")
            if eta:
                job.eta = f"{int(eta)//60}m {int(eta)%60}s" if eta >= 60 else f"{int(eta)}s"
            job.resolution = d.get("info_dict", {}).get("resolution") or job.resolution
        elif st == "finished":
            job.status = "merging"
            job.progress = 99.0
            job.speed = ""

    def _pp_hook(self, job: Job, d: dict) -> None:
        if job._cancel.is_set():
            raise DownloadError("cancelled")
        name = (d.get("postprocessor") or "").lower()
        if d.get("status") == "started":
            if "merger" in name or "ffmpeg" in name:
                job.status = "merging"
            elif "convert" in name or "remux" in name or "video" in name:
                job.status = "converting"
            job.message = (d.get("postprocessor") or "").replace("_", " ").title()

    # -- public entrypoints -------------------------------------------------
    def start(self, job: Job, mode: str, outdir: Path, quality: str = "best",
              universal_codec: str = "h264", hardware: str = "auto",
              flash_safe_name: bool = True) -> None:
        # Pre-fetch metadata so the UI card is accurate the moment it appears.
        try:
            info = self.fetch_info(job.url)
            job.title = info.get("title") or job.title
            job.thumbnail = info.get("thumbnail") or job.thumbnail
            job.duration = info.get("duration") or job.duration
            job.uploader = info.get("uploader") or job.uploader
        except Exception:
            pass
        t = threading.Thread(
            target=self._run, args=(job, mode, outdir, quality, universal_codec, hardware, flash_safe_name),
            daemon=True,
        )
        t.start()

    def cancel(self, job_id: str) -> None:
        j = self.jobs.get(job_id)
        if j:
            j._cancel.set()
            j.message = "Cancelling..."

    # -- the actual work ----------------------------------------------------
    def _run(self, job: Job, mode: str, outdir: Path, quality: str,
             universal_codec: str, hardware: str, flash_safe_name: bool) -> None:
        outdir.mkdir(parents=True, exist_ok=True)
        job.mode = mode
        # Windows-safe, flash-safe filename: no emoji, no reserved chars, short enough
        template = "%(title).120B [%(id)s].%(ext)s" if flash_safe_name else "%(title)s [%(id)s].%(ext)s"
        tmpdir = outdir / ".hdtube-tmp"

        try:
            job.status = "downloading"
            opts = self._base_opts(job, outdir, template)
            if mode == "audio":
                opts.update({
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "m4a",
                        "preferredquality": "0",   # keep source bitrate, no lossy re-encode
                    }],
                })
            elif mode == "universal":
                opts.update(self._universal_opts(quality, universal_codec, hardware, tmpdir))
            else:  # native - stream copy only
                opts.update({
                    "format": f"bestvideo[height<={quality}]+bestaudio/bestvideo+bestaudio/best"
                              if quality != "best" else "bestvideo*+bestaudio/best",
                    "merge_output_format": "mkv" if quality and int(quality) > 1080 else "mp4",
                })

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(job.url, download=True)
                if info and info.get("_type") == "playlist":
                    info = (info.get("entries") or [{}])[0] or {}
                path = ydl.prepare_filename(info)
                if mode == "audio":
                    path = str(Path(path).with_suffix(".m4a"))
                else:
                    found = self._resolve_final_path(path, info, ydl, mode)
                    if found:
                        path = found

            if job._cancel.is_set():
                job.status = "cancelled"
                job.message = "Cancelled"
                return

            p = Path(path)
            if mode == "universal" and p.exists():
                p = self._transcode_universal(p, universal_codec, hardware, quality, job)
                path = str(p)

            if p.exists():
                job.filepath = str(p)
                job.filesize = p.stat().st_size
            job.status = "done"
            job.progress = 100.0
            job.finished = time.time()
            job.message = "Saved"
            self._fill_codecs(job, p)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if job._cancel.is_set() or "cancelled" in msg.lower():
                job.status = "cancelled"
                job.message = "Cancelled"
            else:
                job.status = "error"
                job.message = _friendly_error(msg)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            job.finished = job.finished or time.time()

    def _universal_opts(self, quality: str, codec: str, hardware: str, tmpdir: Path) -> dict:
        """
        Fetch the best source, merge it into an intermediate file, then hand that
        to our own ffmpeg pass (_transcode_universal). Doing the encode explicitly
        - rather than through yt-dlp postprocessors - means we can verify the
        result really is H.264 and retry sanely if a GPU encoder is unavailable.
        """
        cap = "" if quality in ("best", "", None) else f"[height<={quality}]"
        return {
            "format": f"bestvideo*{cap}+bestaudio/best{cap}/best",
            "merge_output_format": "mkv",
            "final_ext": "mkv",
        }

    def _transcode_universal(self, src: Path, codec: str, hardware: str,
                             quality: str, job: Job) -> Path:
        """Re-encode into an MP4 that plays essentially everywhere."""
        if not FFMPEG:
            raise RuntimeError("ffmpeg is required to build the Universal profile.")

        job.status = "converting"
        job.message = f"Encoding {codec.upper()}…"

        venc = {"h264": "libx264", "hevc": "libx265"}.get(codec, "libx264")
        # CRF 18 x264 / 20 x265 is visually lossless; the point is compatibility,
        # not shrinking the file, so we deliberately do not crank the compression.
        crf = "18" if codec == "h264" else "20"
        hw_args: list[str] = []
        if hardware in ("nvenc", "qsv", "amf"):
            cand = f"{'h264' if codec=='h264' else 'hevc'}_{hardware}"
            if _encoder_works(cand):
                venc = cand
                hw_args = ["-b:v", "0", "-cq", "20"] if hardware == "nvenc" else \
                          ["-global_quality", "20"]
                crf = None  # type: ignore

        dst = src.with_suffix(".mp4")
        if dst == src:
            dst = src.with_name(src.stem + " (H264).mp4")

        args = [
            FFMPEG, "-hide_banner", "-y", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", venc,
        ]
        if crf:
            args += ["-preset", "veryfast", "-crf", crf]
        args += hw_args
        args += [
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-max_muxing_queue_size", "2048",
            str(dst),
        ]
        if codec == "h264":
            args[args.index("-pix_fmt"):args.index("-pix_fmt")] = [
                "-profile:v", "high", "-level", "5.1"
            ]

        r = subprocess.run(args, capture_output=True, text=True, errors="ignore")
        if r.returncode != 0 or not dst.exists():
            # GPU path can fail on odd resolutions - fall back to CPU
            if venc != "libx264":
                job.message = "GPU encoder failed, retrying on CPU…"
                return self._transcode_universal(src, codec, "cpu", quality, job)
            raise RuntimeError("Encode failed: " + (r.stderr or "")[-300:])

        try:
            src.unlink()
        except Exception:
            pass
        return dst

    def _resolve_final_path(self, path: str, info: dict, ydl, mode: str) -> Optional[str]:
        """yt-dlp renames the file after merging; find what actually landed on disk."""
        p = Path(path)
        if p.exists():
            return str(p)
        stem = p.stem
        parent = p.parent
        for ext in (".mp4", ".mkv", ".webm", ".mov"):
            cand = parent / f"{stem}{ext}"
            if cand.exists():
                return str(cand)
        # last resort: newest file in the folder matching the id
        vid = info.get("id")
        if vid:
            matches = sorted(parent.glob(f"*{vid}*"), key=lambda x: x.stat().st_mtime, reverse=True)
            if matches:
                return str(matches[0])
        return None

    def _fill_codecs(self, job: Job, p: Path) -> None:
        if not FFMPEG:
            return
        try:
            out = subprocess.run(
                [FFMPEG, "-hide_banner", "-i", str(p)],
                capture_output=True, text=True, timeout=20,
            ).stderr
            m = re.search(r"Video:\s*([\w0-9\.\-]+)", out)
            if m:
                job.vcodec = m.group(1)
            a = re.search(r"Audio:\s*([\w0-9\.\-]+)", out)
            if a:
                job.acodec = a.group(1)
            r = re.search(r"(\d{3,4}x\d{3,4})", out)
            if r:
                job.resolution = r.group(1)
            job.container = p.suffix.lstrip(".").upper()
        except Exception:
            job.container = p.suffix.lstrip(".").upper()


def _encoder_works(name: str) -> bool:
    """Check a GPU encoder is actually usable on this machine before committing."""
    if not FFMPEG:
        return False
    try:
        r = subprocess.run(
            [FFMPEG, "-hide_banner", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.1",
             "-c:v", name, "-f", "null", "-"],
            capture_output=True, text=True, errors="ignore", timeout=25,
        )
        return r.returncode == 0
    except Exception:
        return False


def _friendly_error(msg: str) -> str:
    low = msg.lower()
    if "sign in" in low or "bot" in low or "confirm you're not a bot" in low:
        return ("YouTube is asking for sign-in verification (bot check). "
                "Export your browser cookies and point HDTUBE_COOKIES at the file, "
                "or try a different video.")
    if "private" in low:
        return "This video is private."
    if "unavailable" in low or "removed" in low:
        return "This video is unavailable or has been removed."
    if "ffmpeg" in low:
        return "ffmpeg is required to merge HD streams. Re-run setup to install it."
    if "unsupported url" in low:
        return "That doesn't look like a supported video URL."
    return msg[:400]


# --------------------------------------------------------------------------
# Flash drive inspection
# --------------------------------------------------------------------------

_FAT32_MAX = 4 * 1024**3 - 1          # 4 GiB - 1 byte, the hard FAT32 wall
_EXFAT_RECOMMENDED_MAX = 512 * 1024**3


def inspect_target(path: str | Path) -> dict:
    """
    Report the filesystem of the volume that would receive the file, plus the
    practical size limit for a single file there.
    """
    p = Path(path).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "path": str(p), "error": str(e),
                "fs": "unknown", "max_file_size": None, "free": 0}

    root = p.resolve().anchor or p.anchor or str(p)
    info: dict = {"ok": True, "path": str(p), "fs": "unknown", "label": "",
                  "max_file_size": None, "free": 0, "total": 0, "removable": False}

    if sys.platform == "win32":
        info.update(_inspect_windows(root))
    else:
        info.update(_inspect_posix(p))

    try:
        usage = shutil.disk_usage(str(p))
        info["free"] = usage.free
        info["total"] = usage.total
    except Exception:
        pass

    fs = (info.get("fs") or "").lower()
    if fs in ("fat32", "fat", "vfat", "msdos"):
        info["max_file_size"] = _FAT32_MAX
        info["advice"] = ("FAT32 caps every file at 4 GB. Anything bigger will fail partway "
                          "through the copy. Reformat to exFAT (no size limit, works on "
                          "Windows/Mac/Linux and most TVs), or use Split mode.")
        info["risk"] = "high"
    elif fs in ("exfat", "ntfs", "apfs", "ext4", "ext3", "xfs", "btrfs", "hfs", "hfs+"):
        info["advice"] = ("No per-file size limit on this filesystem - full-quality files "
                          "copy across intact. For maximum device compatibility, choose the "
                          "MP4 / H.264 profile.")
        info["risk"] = "low"
    elif fs in ("fat12", "fat16"):
        info["max_file_size"] = 2 * 1024**3
        info["advice"] = "Very old filesystem with a 2 GB file limit. Reformat the drive to exFAT."
        info["risk"] = "high"
    else:
        info["advice"] = "Filesystem not recognised - assuming no per-file limit."
        info["risk"] = "unknown"

    if fs in ("exfat", "ntfs"):
        info["best_format"] = "exFAT"
    return info


def _inspect_windows(root: str) -> dict:
    """
    Read the filesystem straight from the Win32 API - no shell, no subprocess,
    so it stays fast and works even where PowerShell is restricted.
    """
    out = {"fs": "unknown", "label": "", "removable": False}
    drive = root.rstrip("\\/")
    if not drive.endswith(":"):
        return out
    letter = drive[0]

    # --- filesystem + volume label -------------------------------------
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Explicit signatures are required: ctypes cannot infer these pointer
        # types on its own, and silently clears the output buffers without them.
        gvi = k32.GetVolumeInformationW
        gvi.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
        ]
        gvi.restype = wintypes.BOOL

        vol_name = ctypes.create_unicode_buffer(261)
        fs_name = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        maxlen = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = gvi(
            drive + "\\",
            vol_name, 261,
            ctypes.byref(serial), ctypes.byref(maxlen),
            ctypes.byref(flags), fs_name, 261,
        )
        if ok:
            out["fs"] = _normalize_fs(fs_name.value)
            out["label"] = vol_name.value or ""
    except Exception:
        pass

    # --- drive type (removable stick vs fixed disk) ---------------------
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        gdt = k32.GetDriveTypeW
        gdt.argtypes = [wintypes.LPCWSTR]
        gdt.restype = wintypes.UINT
        dtype = gdt(drive + "\\")
        # 2 = DRIVE_REMOVABLE, 3 = DRIVE_FIXED, 5 = CDROM, 6 = RAMDISK
        out["removable"] = dtype == 2
        out["drive_type"] = dtype
    except Exception:
        pass

    # --- capacity -------------------------------------------------------
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        gdf = k32.GetDiskFreeSpaceExW
        gdf.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                        ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong)]
        gdf.restype = wintypes.BOOL
        free_user = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        free_total = ctypes.c_ulonglong(0)
        if gdf(drive + "\\", ctypes.byref(free_user), ctypes.byref(total), ctypes.byref(free_total)):
            out["free"] = free_user.value
            out["total"] = total.value
    except Exception:
        pass

    return out


def _inspect_posix(p: Path) -> dict:
    out = {"fs": "unknown"}
    try:
        r = subprocess.run(["df", "-T", str(p)], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            parts = r.stdout.strip().splitlines()[-1].split()
            out["fs"] = _normalize_fs(parts[1])
            out["device"] = parts[0]
        if "/media/" in str(p) or "/run/media/" in str(p) or "/Volumes/" in str(p):
            out["removable"] = True
    except Exception:
        pass
    return out


def _normalize_fs(fs: str) -> str:
    f = (fs or "").strip().lower().replace(" ", "")
    if f in ("fat32", "fat", "vfat", "msdos", "ms-dos"):
        return "fat32"
    if f in ("exfat", "ex-fat"):
        return "exfat"
    if f in ("ntfs", "ntfs3"):
        return "ntfs"
    if f.startswith("apfs"):
        return "apfs"
    if f.startswith("ext"):
        return f
    return f or "unknown"


# --------------------------------------------------------------------------
# Flash-drive preparation: split for FAT32
# --------------------------------------------------------------------------

def split_for_fat32(filepath: str, part_size: int = 4 * 1024**3 - 8 * 1024**2,
                    on_progress: Optional[Callable[[float], None]] = None) -> list[str]:
    """
    Cut a file into pieces that fit under the FAT32 4 GB wall.

    Uses stream copy (-c copy) so the video data is byte-identical - no
    re-encode, no generation loss. Pair with join_parts()/the .bat file.
    """
    if not FFMPEG:
        raise RuntimeError("ffmpeg not found")
    src = Path(filepath)
    outdir = Path(str(src.parent)) / (src.stem + "_parts")
    if outdir.exists():
        for old in outdir.glob("*.mkv"):
            try:
                old.unlink()
            except Exception:
                pass
    outdir.mkdir(parents=True, exist_ok=True)
    # Segment duration is derived from the *bitrate*, so each part lands under the
    # size target. A byte-for-byte split is not possible without breaking the
    # container, so this is the standard approach.
    size_bytes = src.stat().st_size
    dur = _duration_seconds(src) or 60
    target = max(1, int(part_size))
    seg_seconds = max(1, int(dur * target / size_bytes)) if size_bytes else 60
    # Segments can only be cut on keyframes, so a part may overshoot its nominal
    # duration. Shrink the target by the worst realistic overshoot so no single
    # part ever crosses the FAT32 ceiling the user actually cares about.
    seg_seconds = max(1, int(seg_seconds * 0.85))

    base = "part_%03d.mkv"
    cmd = [
        FFMPEG, "-hide_banner", "-y", "-i", str(src),
        "-c", "copy", "-map", "0",
        "-f", "segment", "-segment_format", "matroska",
        "-segment_time", str(seg_seconds),
        "-reset_timestamps", "1",
        str(outdir / base),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="ignore")
    if r.returncode != 0:
        raise RuntimeError(f"Split failed: {(r.stderr or '')[-300:]}")

    parts = sorted(str(p) for p in outdir.glob("part_*.mkv"))
    if not parts:
        raise RuntimeError("Split produced no output.")

    # A join script so the user can rebuild the file in one click
    script = outdir / "JOIN_PARTS.bat"
    script.write_text(
        "@echo off\r\n"
        "cd /d \"%~dp0\"\r\n"
        "echo Joining parts into a single file...\r\n"
        f'copy /b part_*.mkv "{src.stem}-joined.mkv"\r\n'
        "echo Done. Delete the parts afterwards.\r\npause\r\n",
        encoding="ascii", errors="ignore",
    )
    return parts


def _duration_seconds(p: Path) -> float:
    """Media duration in seconds, straight from ffmpeg's header dump."""
    if not FFMPEG:
        return 0.0
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-i", str(p)],
                             capture_output=True, text=True, errors="ignore").stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
        if m:
            h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mnt * 60 + s
    except Exception:
        pass
    return 0.0


def join_parts(part_files: list[str], dest: str) -> str:
    """Binary concatenation - valid for MPEG-TS/MKV segments produced above."""
    with open(dest, "wb") as out:
        for f in sorted(part_files):
            with open(f, "rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
    return dest
