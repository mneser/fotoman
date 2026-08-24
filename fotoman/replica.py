"""Per-volume shadow mounts: union fill, never delete."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fotoman.config import DATA_DIR
from fotoman.db import db


def default_shadow_path(source_id: int) -> str:
    dest = DATA_DIR / "replicas" / str(int(source_id))
    dest.mkdir(parents=True, exist_ok=True)
    return str(dest.resolve())


def normalize_shadow_path(raw: Any) -> str:
    from fotoman.device import normalize_shadow_path as _normalize

    return _normalize(raw)


def _safe_rel(rel: Any) -> str:
    text = str(rel or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in Path(text).parts if p not in ("", ".", "..")]
    return "/".join(parts)


def _paths_overlap(a: Path, b: Path) -> bool:
    try:
        left, right = a.expanduser().resolve(), b.expanduser().resolve()
    except OSError:
        return False
    if left == right:
        return True
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _is_backup(src: dict[str, Any] | None) -> bool:
    from fotoman import backup

    return backup.is_backup(src)


def _is_device(src: dict[str, Any] | None) -> bool:
    from fotoman import device

    return device.is_device_source(src)


def shadow_root(src: dict[str, Any] | None) -> Path | None:
    if not src or not int(src.get("shadow_enabled") or 0):
        return None
    raw = str(src.get("shadow_path") or "").strip()
    if not raw:
        return None
    dest = Path(raw).expanduser()
    return dest if dest.is_dir() else None


def primary_root(src: dict[str, Any] | None) -> Path | None:
    if not src or _is_device(src):
        return None
    dest = Path(str(src.get("path") or "")).expanduser()
    return dest if dest.is_dir() else None


def live_root(src: dict[str, Any] | None) -> Path | None:
    """Filesystem root to walk: preferred mount, else shadow.

    Phone volumes are not a mounted folder. Their shadow is a named copy,
    not the tree to browse — otherwise Fotoman walks JPEG copies whose
    paths do not match indexed ``fotomob://device/…`` locations and the
    volume looks empty.
    """
    if _is_device(src):
        return None
    return primary_root(src) or shadow_root(src)


def source_exists(src: dict[str, Any] | None) -> bool:
    if not src:
        return False
    if _is_device(src):
        from fotoman import device

        pair = device.pair_id_from_path(src.get("path"))
        return device.is_online(pair) or device.has_shadow(pair) or shadow_root(src) is not None
    return primary_root(src) is not None or shadow_root(src) is not None


def canonical_path(src: dict[str, Any], rel: str) -> str:
    safe = _safe_rel(rel)
    root = Path(str(src.get("path") or "")).expanduser()
    return str(root / safe) if safe else str(root)


def readable_file(src: dict[str, Any] | None, rel: str) -> Path | None:
    if not src:
        return None
    safe = _safe_rel(rel)
    if not safe:
        return None
    primary = primary_root(src)
    if primary:
        dest = primary / safe
        if dest.is_file():
            return dest
    shadow = shadow_root(src)
    if shadow:
        dest = shadow / safe
        if dest.is_file():
            return dest
    return None


def readable_stored_path(store_path: str) -> Path | None:
    """Resolve a photos.path to a readable file, including a shadow copy."""
    raw = str(store_path or "")
    if not raw:
        return None
    p = Path(raw)
    if p.is_file():
        return p
    with db() as conn:
        row = conn.execute(
            """
            SELECT p.rel_path, s.*
            FROM photos p
            JOIN sources s ON s.id = p.source_id
            WHERE p.path = ?
            """,
            (raw,),
        ).fetchone()
    if not row:
        return None
    src = dict(row)
    return readable_file(src, str(row["rel_path"] or ""))


def _list_images(root: Path) -> list[Path]:
    from fotoman.indexer import list_image_files

    return list_image_files(root)


def union_files(src: dict[str, Any]) -> dict[str, Path]:
    """Relative path → readable file. Primary wins when both have a copy."""
    out: dict[str, Path] = {}
    shadow = shadow_root(src)
    primary = primary_root(src)
    if shadow:
        for path in _list_images(shadow):
            try:
                rel = str(path.relative_to(shadow))
            except ValueError:
                continue
            out[rel] = path
    if primary:
        for path in _list_images(primary):
            try:
                rel = str(path.relative_to(primary))
            except ValueError:
                continue
            out[rel] = path
    return out


def wants_sync(src: dict[str, Any] | None) -> bool:
    if not src or _is_backup(src):
        return False
    return bool(
        int(src.get("sync_enabled") or 0)
        and int(src.get("shadow_enabled") or 0)
        and str(src.get("shadow_path") or "").strip()
    )


def annotate_source(src: dict[str, Any]) -> dict[str, Any]:
    from fotoman import device

    out = dict(src)
    if device.is_device_source(out):
        out = device.annotate_source(out)
    out["shadow_path"] = str(out.get("shadow_path") or "").strip()
    out["shadow_enabled"] = bool(int(out.get("shadow_enabled") or 0))
    out["sync_enabled"] = bool(int(out.get("sync_enabled") or 0))
    out["exists"] = source_exists(out)
    return out


def _queue_scan(src: dict[str, Any]) -> None:
    from fotoman import backup, device, indexer

    if backup.is_backup(src):
        return
    if device.is_device_source(src):
        if src.get("sync_enabled") and device.is_online(src.get("pair_id")):
            device._queue_scan(src)
        return
    indexer.start_index_background(
        source_id=int(src["id"]), mode="scan", high_priority=True
    )


def patch_source(
    source_id: int,
    *,
    shadow_path: str | None = None,
    shadow_enabled: bool | None = None,
    sync_enabled: bool | None = None,
    has_shadow_path: bool = False,
    has_shadow_enabled: bool = False,
    has_sync_enabled: bool = False,
) -> dict[str, Any]:
    """Update shadow/sync flags for any non-backup volume."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (int(source_id),)
        ).fetchone()
    if not row:
        raise ValueError(f"Source not found: {source_id}")
    src = dict(row)
    if _is_backup(src):
        return annotate_source(src)

    updates: dict[str, Any] = {}
    next_path = src.get("shadow_path")
    if has_shadow_path:
        next_path = normalize_shadow_path(shadow_path) or None
        updates["shadow_path"] = next_path
        if not next_path:
            updates["shadow_enabled"] = 0
        elif not has_shadow_enabled:
            updates["shadow_enabled"] = 1
    if has_shadow_enabled:
        if shadow_enabled and not next_path:
            next_path = default_shadow_path(int(src["id"]))
            updates["shadow_path"] = next_path
        updates["shadow_enabled"] = 1 if shadow_enabled and next_path else 0
    if has_sync_enabled:
        updates["sync_enabled"] = 1 if sync_enabled else 0

    check_path = next_path if has_shadow_path or has_shadow_enabled else src.get("shadow_path")
    if check_path and not _is_device(src):
        if _paths_overlap(Path(str(src["path"])), Path(str(check_path))):
            raise ValueError("Shadow folder cannot be inside the volume (or vice versa)")

    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        with db() as conn:
            conn.execute(
                f"UPDATE sources SET {sets} WHERE id = ?",
                (*updates.values(), int(source_id)),
            )
            row = conn.execute(
                "SELECT * FROM sources WHERE id = ?", (int(source_id),)
            ).fetchone()
        src = dict(row)

    out = annotate_source(src)
    shadow_on = bool(out.get("shadow_enabled") and out.get("shadow_path"))
    sync_on = bool(out.get("sync_enabled"))
    if (has_shadow_enabled and shadow_on and sync_on) or (
        has_sync_enabled and sync_on and shadow_on
    ):
        _queue_scan(out)
    elif _is_device(src) and has_sync_enabled and sync_on:
        _queue_scan(out)
    return out


def _copy_missing(src_file: Path, dest: Path) -> bool:
    if dest.is_file():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_file, dest)
    return True


def _sync_device(src: dict[str, Any]) -> dict[str, int | bool]:
    from fotoman import device

    stats: dict[str, int | bool] = {
        "copied_to_shadow": 0,
        "copied_to_primary": 0,
        "offline": False,
    }
    pair = device.pair_id_from_path(src.get("path"))
    shadow = shadow_root(src)
    if not pair or not shadow:
        stats["offline"] = shadow is None
        return stats
    for item in device.get_catalog(pair):
        rel = item.get("rel") or item.get("name")
        dest = device.named_shadow_file(pair, rel)
        if dest and dest.is_file():
            continue
        store = device.asset_path(pair, str(item.get("id") or ""))
        readable = device.readable_path(store)
        if readable is None or dest is None:
            continue
        if _copy_missing(readable, dest):
            stats["copied_to_shadow"] = int(stats["copied_to_shadow"]) + 1
    return stats


def sync_source(src: dict[str, Any]) -> dict[str, int | bool]:
    """Copy missing files onto the other mounted side. Never deletes."""
    stats: dict[str, int | bool] = {
        "copied_to_shadow": 0,
        "copied_to_primary": 0,
        "offline": False,
    }
    if not wants_sync(src):
        return stats
    if _is_device(src):
        return _sync_device(src)

    primary = Path(str(src.get("path") or "")).expanduser()
    shadow = Path(str(src.get("shadow_path") or "")).expanduser()
    primary_on = primary.is_dir()
    shadow_on = shadow.is_dir()
    if not primary_on and not shadow_on:
        stats["offline"] = True
        return stats
    if not shadow_on or not primary_on:
        return stats

    by_rel: dict[str, Path] = {}
    for path in _list_images(shadow):
        try:
            by_rel[str(path.relative_to(shadow))] = path
        except ValueError:
            continue
    prim_rels: dict[str, Path] = {}
    for path in _list_images(primary):
        try:
            prim_rels[str(path.relative_to(primary))] = path
        except ValueError:
            continue
    for rel, path in prim_rels.items():
        if _copy_missing(path, shadow / _safe_rel(rel)):
            stats["copied_to_shadow"] = int(stats["copied_to_shadow"]) + 1
    for rel, path in by_rel.items():
        if _copy_missing(path, primary / _safe_rel(rel)):
            stats["copied_to_primary"] = int(stats["copied_to_primary"]) + 1
    return stats
