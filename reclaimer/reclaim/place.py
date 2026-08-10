"""Place files into OUT with exact-hash dedup."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from reclaim.manifest import ManifestEntry, ManifestStore, RunStats
from reclaim.util import (
    MEDIA_EXTS,
    atomic_copy,
    file_sha1,
    same_device,
    unique_path,
)


class DedupMode(str, Enum):
    SKIP = "skip"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"


class Layout(str, Enum):
    ALBUMS = "albums"
    DATE = "date"
    FLAT = "flat"


@dataclass
class PlaceResult:
    action: str  # copied | hardlinked | symlinked | skipped
    relpath: str | None
    sha1: str
    bytes: int


def dest_relpath(
    *,
    source: str,
    layout: Layout,
    filename: str,
    album: str | None,
    date_prefix: str | None,
) -> Path:
    """Relative path under OUT."""
    safe_name = Path(filename).name
    if layout == Layout.FLAT:
        return Path(source) / safe_name
    if layout == Layout.DATE:
        parts = [source]
        if date_prefix:
            # YYYY/MM or YYYY-MM-DD → YYYY/MM
            dp = date_prefix.replace("-", "/")[:7]
            parts.extend(dp.split("/"))
        parts.append(safe_name)
        return Path(*parts)
    # albums
    album_name = album or "_unsorted"
    album_name = album_name.strip().replace("/", "_").replace("\\", "_") or "_unsorted"
    return Path(source) / album_name / safe_name


def place_file(
    src: Path,
    *,
    store: ManifestStore,
    source: str,
    layout: Layout,
    dedup: DedupMode,
    album: str | None = None,
    date_prefix: str | None = None,
    media_id: str | None = None,
    original_name: str | None = None,
    known_sha1: str | None = None,
) -> PlaceResult:
    """Copy/link src into OUT; update manifest. Never modifies src."""
    if media_id and store.has_media_id(media_id):
        entry = store._by_media_id[media_id]
        return PlaceResult(
            action="skipped",
            relpath=entry.relpath,
            sha1=entry.sha1,
            bytes=entry.bytes or 0,
        )

    sha1 = known_sha1 or file_sha1(src)
    size = src.stat().st_size

    if store.has_sha1(sha1):
        existing = store.path_for_sha1(sha1)
        if existing is not None:
            if dedup == DedupMode.SKIP:
                return PlaceResult("skipped", store._by_sha1[sha1].relpath, sha1, size)

            rel = dest_relpath(
                source=source,
                layout=layout,
                filename=original_name or src.name,
                album=album,
                date_prefix=date_prefix,
            )
            dest = unique_path(store.out / rel)
            dest.parent.mkdir(parents=True, exist_ok=True)

            if dedup == DedupMode.HARDLINK:
                if same_device(existing, dest.parent):
                    try:
                        os.link(existing, dest)
                        action = "hardlinked"
                    except OSError:
                        atomic_copy(src, dest)
                        action = "copied"
                else:
                    # cross-device: fall back to skip (per spec)
                    return PlaceResult(
                        "skipped", store._by_sha1[sha1].relpath, sha1, size
                    )
            else:  # symlink
                try:
                    dest.symlink_to(existing)
                    action = "symlinked"
                except OSError:
                    atomic_copy(src, dest)
                    action = "copied"

            rel_s = str(dest.relative_to(store.out))
            store.append(
                ManifestEntry(
                    sha1=sha1,
                    relpath=rel_s,
                    source=source,
                    media_id=media_id,
                    album=album,
                    original_name=original_name or src.name,
                    bytes=size,
                    action=action,
                )
            )
            return PlaceResult(action, rel_s, sha1, size)

    # New content
    rel = dest_relpath(
        source=source,
        layout=layout,
        filename=original_name or src.name,
        album=album,
        date_prefix=date_prefix,
    )
    dest = unique_path(store.out / rel)
    atomic_copy(src, dest)
    rel_s = str(dest.relative_to(store.out))
    store.append(
        ManifestEntry(
            sha1=sha1,
            relpath=rel_s,
            source=source,
            media_id=media_id,
            album=album,
            original_name=original_name or src.name,
            bytes=size,
            action="copied",
        )
    )
    return PlaceResult("copied", rel_s, sha1, size)


def apply_place_to_stats(stats: RunStats, result: PlaceResult) -> None:
    if result.action == "copied":
        stats.extracted += 1
    elif result.action == "skipped":
        stats.skipped_existing += 1
    elif result.action == "hardlinked":
        stats.hardlinked_exact += 1
    elif result.action == "symlinked":
        stats.symlinked_exact += 1


def is_media(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTS
