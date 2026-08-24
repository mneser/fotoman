"""Optional Reclaimer normalize + add-as-archive (Fotoman still runs without it)."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

from fotoman import events, indexer
from fotoman.config import RECLAIMED_DIR

_RECLAIMER_ROOT = Path(__file__).resolve().parent.parent / "reclaimer"

DEST_NAMES = {
    "facebook": "facebook",
    "instagram": "instagram",
    "google_takeout": "google",
    "google_photos": "google",
}

_lock = threading.Lock()
_state: dict[str, Any] = {
    "running": False,
    "message": "",
    "error": None,
    "path": None,
    "source_id": None,
    "extracted": 0,
    "total": 0,
}
_thread: threading.Thread | None = None


def _ensure_reclaim() -> None:
    try:
        import reclaim  # noqa: F401
        return
    except ImportError:
        pass
    if _RECLAIMER_ROOT.is_dir() and str(_RECLAIMER_ROOT) not in sys.path:
        sys.path.insert(0, str(_RECLAIMER_ROOT))
    try:
        import reclaim  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "Reclaimer is not installed. From the repo: pip install -e reclaimer"
        ) from e


def get_status() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def _set(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)
    events.publish("index", force=True)


def _dest_for(zpath: Path, dest: str | None, source: str) -> Path:
    folder = DEST_NAMES.get(source, source)
    if dest:
        return Path(dest).expanduser().resolve()
    return (RECLAIMED_DIR / folder).resolve()


def prepare_archive(zip_path: str, dest: str | None = None) -> dict[str, Any]:
    """Create dest and add the archive row immediately (before unpack)."""
    _ensure_reclaim()
    from reclaim.normalize import guess_source

    zpath = Path(zip_path).expanduser().resolve()
    if not zpath.is_file() or zpath.suffix.lower() != ".zip":
        raise ValueError("Choose a .zip export")
    source = guess_source(zpath) or "google_takeout"
    out = _dest_for(zpath, dest, source)
    out.mkdir(parents=True, exist_ok=True)
    src = indexer.add_source(str(out), source)
    return {"zip_path": str(zpath), "dest": str(out), "source": source, "archive": src}


def import_zip(zip_path: str, dest: str | None = None, *, progress=None) -> dict[str, Any]:
    """Unpack/normalize a Takeout / Facebook / Instagram zip into dest, return stats."""
    _ensure_reclaim()
    from reclaim.normalize import inspect_input, normalize
    from reclaim.place import DedupMode, Layout
    from reclaim.util import ExitCode

    zpath = Path(zip_path).expanduser().resolve()
    if not zpath.is_file() or zpath.suffix.lower() != ".zip":
        raise ValueError("Choose a .zip export")

    report = inspect_input(zpath)
    source = report.source_guess or "google_takeout"
    folder = DEST_NAMES.get(source, source)
    out = Path(dest).expanduser().resolve() if dest else (RECLAIMED_DIR / folder).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(f"unpack 0/{report.media_count}")

    stats, code = normalize(
        zpath,
        out,
        source=source,
        layout=Layout.ALBUMS,
        dedup=DedupMode.HARDLINK,
        progress=progress,
    )
    if code not in (ExitCode.OK, ExitCode.PARTIAL):
        raise RuntimeError(f"Normalize failed (exit {int(code)})")
    if stats.extracted == 0 and stats.skipped_existing == 0 and stats.hardlinked_exact == 0:
        raise RuntimeError("No media files found in that zip")

    return {
        "path": str(out),
        "source": source,
        "extracted": stats.extracted,
        "skipped_existing": stats.skipped_existing,
        "hardlinked_exact": stats.hardlinked_exact,
        "errors": stats.errors,
        "media_count": report.media_count,
    }


def start_import_zip(zip_path: str, dest: str | None = None) -> dict[str, Any]:
    """Add archive banner immediately, then unpack on a worker thread."""
    global _thread
    with _lock:
        if _state["running"]:
            raise RuntimeError("An import is already running")

    prepared = prepare_archive(zip_path, dest)
    src = prepared["archive"]
    sid = int(src["id"])
    with _lock:
        if _state["running"]:
            raise RuntimeError("An import is already running")
        _state.update(
            {
                "running": True,
                "message": "Unpack",
                "error": None,
                "path": prepared["dest"],
                "source_id": sid,
                "extracted": 0,
                "total": 0,
            }
        )

    def _progress(m: str) -> None:
        kw: dict[str, Any] = {"message": str(m)}
        parts = str(m).replace(",", " ").split()
        for p in parts:
            if "/" in p and p.replace("/", "").isdigit():
                cur, tot = p.split("/", 1)
                kw["extracted"] = int(cur)
                kw["total"] = int(tot)
                break
        _set(**kw)

    def _run() -> None:
        try:
            result = import_zip(
                prepared["zip_path"],
                prepared["dest"],
                progress=_progress,
            )
            indexer.start_index_background(source_id=sid, mode="scan", high_priority=True)
            n = int(result.get("extracted") or 0)
            _set(
                running=False,
                message=f"Imported {n} file{'s' if n != 1 else ''}",
                error=None,
                path=result["path"],
                source_id=sid,
                extracted=n,
                total=int(result.get("media_count") or n),
            )
        except Exception as e:
            _set(running=False, message=str(e), error=str(e), source_id=sid)

    _thread = threading.Thread(target=_run, name="reclaim-import", daemon=True)
    _thread.start()
    events.publish("index", force=True)
    return {**get_status(), "id": sid, "archive": src}
