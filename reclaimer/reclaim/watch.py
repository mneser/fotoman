"""Watch a directory for export zips; stabilize; hand off to normalize."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from reclaim.jobs import PendingJob, save_job
from reclaim.normalize import normalize
from reclaim.place import DedupMode, Layout
from reclaim.util import ExitCode


@dataclass
class WatchMatch:
    paths: list[Path]


def _size_snapshot(paths: list[Path]) -> dict[str, int]:
    snap: dict[str, int] = {}
    for p in paths:
        try:
            snap[str(p)] = p.stat().st_size
        except OSError:
            snap[str(p)] = -1
    return snap


def wait_until_stable(
    paths: list[Path],
    *,
    settle_seconds: float = 3.0,
    checks: int = 3,
    progress: Callable[[str], None] | None = None,
) -> bool:
    """Return True when size is unchanged across `checks` intervals."""
    log = progress or (lambda _m: None)
    if not paths:
        return False
    prev = _size_snapshot(paths)
    stable = 0
    while stable < checks:
        time.sleep(settle_seconds)
        cur = _size_snapshot(paths)
        if cur == prev and all(v >= 0 for v in cur.values()):
            stable += 1
            log(f"size stable ({stable}/{checks})")
        else:
            stable = 0
            log("download still growing…")
            prev = cur
    return True


def find_matching_archives(
    watch_dir: Path,
    *,
    patterns: list[str],
    since_mtime: float | None = None,
) -> list[Path]:
    """Find zip files whose name matches any substring pattern (case-insensitive)."""
    watch_dir = watch_dir.expanduser()
    if not watch_dir.is_dir():
        return []
    pats = [p.lower() for p in patterns]
    found: list[Path] = []
    for p in watch_dir.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() != ".zip":
            continue
        # skip incomplete browser downloads
        if p.name.endswith(".crdownload") or p.name.endswith(".part"):
            continue
        name = p.name.lower()
        if not any(pat in name for pat in pats):
            continue
        if since_mtime is not None:
            try:
                if p.stat().st_mtime < since_mtime - 1:
                    continue
            except OSError:
                continue
        found.append(p)
    return sorted(found, key=lambda x: x.stat().st_mtime)


def watch_for_archives(
    watch_dir: Path,
    *,
    patterns: list[str],
    since_mtime: float,
    wait_hours: float,
    progress: Callable[[str], None] | None = None,
) -> list[Path] | None:
    """
    Poll watch_dir until matching zips appear and stabilize.
    Returns paths, or None if timed out.
    """
    log = progress or (lambda _m: None)
    deadline = time.time() + max(wait_hours, 0) * 3600
    # If wait_hours == 0, do a single scan (no long hang)
    single_pass = wait_hours <= 0

    while True:
        matches = find_matching_archives(
            watch_dir, patterns=patterns, since_mtime=since_mtime
        )
        if matches:
            log(f"found: {', '.join(m.name for m in matches)}")
            if wait_until_stable(matches, progress=log):
                return matches
        if single_pass:
            return None
        if time.time() >= deadline:
            return None
        log(f"watching {watch_dir} for export zip…")
        time.sleep(15.0)


def run_watch_and_normalize(
    *,
    platform: str,
    source: str,
    out: Path,
    watch_dir: Path,
    patterns: list[str],
    wait_hours: float,
    layout: Layout,
    dedup: DedupMode,
    started_mtime: float,
    progress: Callable[[str], None] | None = None,
) -> ExitCode:
    log = progress or (lambda _m: None)
    out = out.expanduser().resolve()
    watch_dir = watch_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    watch_dir.mkdir(parents=True, exist_ok=True)

    matches = watch_for_archives(
        watch_dir,
        patterns=patterns,
        since_mtime=started_mtime,
        wait_hours=wait_hours,
        progress=log,
    )
    if not matches:
        job = PendingJob.create(
            platform=platform,
            output=out,
            watch_dir=watch_dir,
            wait_hours=wait_hours,
            message=(
                "Export still preparing or zip not in watch dir. "
                "Re-run the same fetch command later, or: reclaim status"
            ),
        )
        save_job(job)
        log(job.message)
        log(f"pending job saved: {job.id}")
        return ExitCode.PENDING

    # Prefer the newest zip (or first of multipart set) as normalize input
    primary = matches[0] if len(matches) == 1 else matches[0].parent
    if len(matches) > 1:
        # If all share a multipart stem, point at the first part; normalize finds siblings
        primary = matches[0]
        log(f"multipart: {len(matches)} zip(s)")

    stats, code = normalize(
        primary,
        out,
        source=source,
        layout=layout,
        dedup=dedup,
        progress=log,
    )
    log(
        f"normalize: {stats.extracted} extracted, "
        f"{stats.hardlinked_exact} hardlinked_exact, "
        f"{stats.skipped_existing} skipped_existing, "
        f"{stats.errors} errors"
    )
    log(f"output: {out}")
    return code
