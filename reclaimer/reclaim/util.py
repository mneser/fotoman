"""Exit codes and shared helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from enum import IntEnum
from pathlib import Path


class ExitCode(IntEnum):
    OK = 0
    USAGE = 1
    PARTIAL = 2
    FATAL_IO = 3
    PENDING = 4


IMAGE_EXTS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
        ".bmp",
        ".raw",
        ".cr2",
        ".nef",
        ".arw",
        ".dng",
    }
)
VIDEO_EXTS = frozenset(
    {
        ".mp4",
        ".mov",
        ".m4v",
        ".avi",
        ".mkv",
        ".webm",
        ".3gp",
        ".mts",
        ".m2ts",
    }
)
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS


def file_sha1(path: Path, blocksize: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            chunk = f.read(blocksize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".reclaim-", suffix=".tmp", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".reclaim-", suffix=".tmp", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out, length=1 << 20)
            out.flush()
            os.fsync(out.fileno())
        try:
            shutil.copystat(src, tmp, follow_symlinks=True)
        except OSError:
            pass
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def unique_path(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    parent = dest.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def open_url(url: str) -> None:
    """Open URL in the default browser; fall back to printing it."""
    import shutil as sh
    import subprocess
    import webbrowser

    xdg = sh.which("xdg-open")
    if xdg:
        try:
            subprocess.Popen(
                [xdg, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except OSError:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        print(f"Open this URL in your browser:\n  {url}")
