"""Inspect / unpack / normalize export archives into OUT."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from reclaim.manifest import ManifestStore, RunRecord, RunStats, now
from reclaim.place import (
    DedupMode,
    Layout,
    apply_place_to_stats,
    is_media,
    place_file,
)
from reclaim.util import MEDIA_EXTS, ExitCode


SOURCE_GOOGLE_PHOTOS = "google_photos"
SOURCE_GOOGLE_TAKEOUT = "google_takeout"
SOURCE_FACEBOOK = "facebook"
SOURCE_INSTAGRAM = "instagram"


@dataclass
class InspectReport:
    path: str
    kind: str  # zip | dir | multipart
    source_guess: str | None
    media_count: int = 0
    zip_parts: list[str] = field(default_factory=list)
    sample_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def guess_source(path: Path) -> str | None:
    name = path.name.lower()
    if "takeout" in name or "google" in name:
        return SOURCE_GOOGLE_TAKEOUT
    if "facebook" in name or name.startswith("posts") or "your_facebook" in name:
        return SOURCE_FACEBOOK
    if "instagram" in name or name.startswith("instagram-"):
        return SOURCE_INSTAGRAM
    if path.is_dir():
        # peek
        for child in list(path.iterdir())[:50]:
            n = child.name.lower()
            if n == "takeout" or "google photos" in n:
                return SOURCE_GOOGLE_TAKEOUT
            if "your_instagram" in n or n == "media":
                return SOURCE_INSTAGRAM
            if n in ("posts", "photos_and_videos", "your_facebook_activity"):
                return SOURCE_FACEBOOK
    return None


def find_multipart_zips(path: Path) -> list[Path]:
    """Detect Takeout-style multi-part archives next to path."""
    if path.is_dir():
        zips = sorted(path.glob("*.zip")) + sorted(path.glob("*.ZIP"))
        if not zips:
            return []
        # Group by stem without -001 / .01 suffixes
        return zips

    if not path.suffix.lower() == ".zip":
        return [path] if path.exists() else []

    parent = path.parent
    stem = path.stem
    # takeout-20240101T120000Z-001.zip or foo-01.zip / foo.zip
    patterns = [
        re.compile(re.escape(stem.rsplit("-", 1)[0]) + r"-\d+\.zip$", re.I),
        re.compile(re.escape(stem) + r"\.zip$", re.I),
    ]
    # Also match stem ending in -001
    m = re.match(r"(.+)-\d+$", stem)
    base = m.group(1) if m else stem
    found = sorted(
        {
            p
            for p in parent.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".zip"
            and (p.stem == stem or p.stem.startswith(base + "-") or p.stem == base)
        },
        key=lambda p: p.name.lower(),
    )
    return found or [path]


def inspect_input(path: Path) -> InspectReport:
    path = path.expanduser().resolve()
    if not path.exists():
        return InspectReport(
            path=str(path),
            kind="missing",
            source_guess=None,
            notes=["path does not exist"],
        )

    if path.is_file() and path.suffix.lower() == ".zip":
        parts = find_multipart_zips(path)
        kind = "multipart" if len(parts) > 1 else "zip"
        report = InspectReport(
            path=str(path),
            kind=kind,
            source_guess=guess_source(path),
            zip_parts=[p.name for p in parts],
        )
        try:
            with zipfile.ZipFile(path) as zf:
                names = [n for n in zf.namelist() if not n.endswith("/")]
                media = [
                    n
                    for n in names
                    if Path(n).suffix.lower() in MEDIA_EXTS
                ]
                report.media_count = len(media)
                report.sample_paths = media[:15]
                joined = "\n".join(names[:200]).lower()
                if report.source_guess is None:
                    if "takeout" in joined or "google photos" in joined:
                        report.source_guess = SOURCE_GOOGLE_TAKEOUT
                    elif "your_instagram" in joined or "/media/" in joined:
                        report.source_guess = SOURCE_INSTAGRAM
                    elif "posts/media" in joined or "your_facebook" in joined:
                        report.source_guess = SOURCE_FACEBOOK
        except zipfile.BadZipFile:
            report.notes.append("not a valid zip")
        return report

    if path.is_dir():
        report = InspectReport(
            path=str(path),
            kind="dir",
            source_guess=guess_source(path),
        )
        samples: list[str] = []
        count = 0
        for p in path.rglob("*"):
            if p.is_file() and is_media(p):
                count += 1
                if len(samples) < 15:
                    try:
                        samples.append(str(p.relative_to(path)))
                    except ValueError:
                        samples.append(str(p))
        report.media_count = count
        report.sample_paths = samples
        zips = find_multipart_zips(path)
        if zips:
            report.zip_parts = [p.name for p in zips]
            report.notes.append(f"{len(zips)} zip(s) in directory")
        return report

    return InspectReport(
        path=str(path),
        kind="file",
        source_guess=guess_source(path),
        notes=["unsupported input type"],
    )


def _album_from_takeout_path(rel: Path) -> str | None:
    parts = list(rel.parts)
    # Takeout/Google Photos/Album/file.jpg
    lower = [p.lower() for p in parts]
    try:
        idx = lower.index("google photos")
        if idx + 1 < len(parts) - 1:
            return parts[idx + 1]
    except ValueError:
        pass
    if len(parts) >= 2:
        return parts[-2]
    return None


def _date_from_sidecar(media: Path) -> str | None:
    """Best-effort date from Google Takeout JSON sidecar."""
    for candidate in (Path(str(media) + ".json"), media.with_suffix(media.suffix + ".json")):
        if not candidate.exists():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ts = (
            data.get("photoTakenTime", {}).get("timestamp")
            or data.get("creationTime", {}).get("timestamp")
        )
        if ts:
            try:
                from datetime import datetime, timezone

                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                return dt.strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                pass
    return None


def _facebook_album(rel: Path) -> str | None:
    parts = rel.parts
    # photos_and_videos/album_name/... or posts/media/...
    for i, p in enumerate(parts):
        pl = p.lower()
        if pl in ("photos_and_videos", "posts", "media") and i + 1 < len(parts) - 1:
            nxt = parts[i + 1]
            if nxt.lower() not in ("media", "photos", "videos"):
                return nxt
            if i + 2 < len(parts) - 1:
                return parts[i + 2]
    if len(parts) >= 2:
        return parts[-2]
    return None


def _instagram_album(rel: Path) -> str | None:
    parts = rel.parts
    # media/posts, media/stories, media/profile, ...
    for i, p in enumerate(parts):
        if p.lower() == "media" and i + 1 < len(parts):
            return parts[i + 1]
    if len(parts) >= 2:
        return parts[-2]
    return None


def _album_and_date(rel: Path, source: str, media: Path | None = None) -> tuple[str | None, str | None]:
    album: str | None = None
    date_prefix: str | None = None
    if source in (SOURCE_GOOGLE_TAKEOUT, SOURCE_GOOGLE_PHOTOS):
        album = _album_from_takeout_path(rel)
        if media is not None:
            date_prefix = _date_from_sidecar(media)
    elif source == SOURCE_FACEBOOK:
        album = _facebook_album(rel)
    elif source == SOURCE_INSTAGRAM:
        album = _instagram_album(rel)
    return album, date_prefix


def _iter_media_from_dir(
    root: Path,
    source: str,
) -> list[tuple[Path, str | None, str | None]]:
    """Return (path, album, date_prefix) tuples."""
    items: list[tuple[Path, str | None, str | None]] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or not is_media(p):
            continue
        # skip macOS junk / sidecar-looking
        if p.name.startswith(".") or p.name.startswith("__MACOSX"):
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = Path(p.name)
        album, date_prefix = _album_and_date(rel, source, p)
        items.append((p, album, date_prefix))
    return items


def _zip_rel(name: str) -> Path | None:
    rel = Path(name.replace("\\", "/"))
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        return None
    if rel.name.startswith(".") or "__MACOSX" in rel.parts:
        return None
    return rel


def _place_media_from_zips(
    zips: list[Path],
    *,
    source: str,
    store: ManifestStore,
    layout: Layout,
    dedup: DedupMode,
    stats: RunStats,
    log: Callable[[str], None],
) -> set[str]:
    """Copy only media members (one file at a time). Never extractall."""
    albums_seen: set[str] = set()
    for zpath in zips:
        with zipfile.ZipFile(zpath) as zf:
            members: list[tuple[zipfile.ZipInfo, Path]] = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                rel = _zip_rel(info.filename)
                if rel is None or not is_media(rel):
                    continue
                members.append((info, rel))
            log(f"{zpath.name}: {len(members)} media file(s)")
            for i, (info, rel) in enumerate(members, 1):
                album, date_prefix = _album_and_date(rel, source)
                if album:
                    albums_seen.add(album)
                if i == 1 or i % 20 == 0 or i == len(members):
                    log(f"unpack {i}/{len(members)} {rel.name}")
                tmp_path: Path | None = None
                try:
                    with zf.open(info) as src_f:
                        fd, tmp_name = tempfile.mkstemp(suffix=rel.suffix)
                        tmp_path = Path(tmp_name)
                        with os.fdopen(fd, "wb") as out_f:
                            shutil.copyfileobj(src_f, out_f, length=1024 * 1024)
                    result = place_file(
                        tmp_path,
                        store=store,
                        source=source,
                        layout=layout,
                        dedup=dedup,
                        album=album,
                        date_prefix=date_prefix,
                        original_name=rel.name,
                    )
                    apply_place_to_stats(stats, result)
                except (OSError, zipfile.BadZipFile) as e:
                    stats.errors += 1
                    log(f"error: {rel.name}: {e}")
                finally:
                    if tmp_path is not None:
                        tmp_path.unlink(missing_ok=True)
    return albums_seen


def _place_media_from_dir(
    work_root: Path,
    *,
    source: str,
    store: ManifestStore,
    layout: Layout,
    dedup: DedupMode,
    stats: RunStats,
    log: Callable[[str], None],
) -> set[str]:
    items = _iter_media_from_dir(work_root, source)
    log(f"found {len(items)} media file(s); placing into {store.out}…")
    albums_seen: set[str] = set()
    for media_path, album, date_prefix in items:
        if album:
            albums_seen.add(album)
        try:
            result = place_file(
                media_path,
                store=store,
                source=source,
                layout=layout,
                dedup=dedup,
                album=album,
                date_prefix=date_prefix,
                original_name=media_path.name,
            )
            apply_place_to_stats(stats, result)
        except OSError as e:
            stats.errors += 1
            log(f"error: {media_path.name}: {e}")
    return albums_seen


def normalize(
    input_path: Path,
    out: Path,
    *,
    source: str | None = None,
    layout: Layout = Layout.ALBUMS,
    dedup: DedupMode = DedupMode.HARDLINK,
    progress: Callable[[str], None] | None = None,
) -> tuple[RunStats, ExitCode]:
    """Normalize INPUT (zip/dir) into OUT/<source>/…"""
    input_path = input_path.expanduser().resolve()
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    log = progress or (lambda _m: None)
    stats = RunStats()
    store = ManifestStore(out)
    started = now()

    report = inspect_input(input_path)
    src = source or report.source_guess or SOURCE_GOOGLE_TAKEOUT

    albums_seen: set[str] = set()
    try:
        if input_path.is_file() and input_path.suffix.lower() == ".zip":
            parts = find_multipart_zips(input_path)
            log(f"reading {len(parts)} zip part(s)…")
            albums_seen = _place_media_from_zips(
                parts,
                source=src,
                store=store,
                layout=layout,
                dedup=dedup,
                stats=stats,
                log=log,
            )
        elif input_path.is_dir():
            zips = sorted(
                p for p in input_path.iterdir() if p.is_file() and p.suffix.lower() == ".zip"
            )
            if zips and not any(is_media(p) for p in input_path.rglob("*") if p.is_file()):
                log(f"reading {len(zips)} zip(s) from directory…")
                albums_seen = _place_media_from_zips(
                    zips,
                    source=src,
                    store=store,
                    layout=layout,
                    dedup=dedup,
                    stats=stats,
                    log=log,
                )
            else:
                albums_seen = _place_media_from_dir(
                    input_path,
                    source=src,
                    store=store,
                    layout=layout,
                    dedup=dedup,
                    stats=stats,
                    log=log,
                )
        else:
            log(f"unsupported input: {input_path}")
            return stats, ExitCode.USAGE
    except zipfile.BadZipFile as e:
        log(f"bad zip: {e}")
        return stats, ExitCode.FATAL_IO

    stats.albums = len(albums_seen)
    code = ExitCode.OK if stats.errors == 0 else ExitCode.PARTIAL
    store.write_run(
        RunRecord(
            command="normalize",
            source=src,
            started_at=started,
            finished_at=now(),
            output=str(out),
            stats=stats.__dict__.copy(),
            ok=code == ExitCode.OK,
            message=f"{stats.extracted} extracted, {stats.hardlinked_exact} hardlinked, {stats.skipped_existing} skipped",
        )
    )
    return stats, code
