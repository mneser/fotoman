"""Phone albums as first-class archives (locations, not a Fotomob store)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any

from fotoman.config import CACHE_DIR, IMAGE_EXTS, load_settings
from fotoman.db import db

log = logging.getLogger(__name__)

KIND_DEVICE = "device"
PATH_PREFIX = "fotomob://device/"

_lock = threading.RLock()
_online: set[str] = set()
_catalogs: dict[str, list[dict[str, Any]]] = {}


def is_device_kind(kind: Any) -> bool:
    return str(kind or "").strip().lower() == KIND_DEVICE


def is_device_path(path: str | None) -> bool:
    return str(path or "").startswith(PATH_PREFIX)


def is_device_source(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return is_device_kind(row.get("kind")) or is_device_path(row.get("path"))


def pair_id_from_path(path: str | None) -> str | None:
    raw = str(path or "")
    if not raw.startswith(PATH_PREFIX):
        return None
    rest = raw[len(PATH_PREFIX) :]
    pair, _, _asset = rest.partition("/")
    pair = pair.strip()
    return pair or None


def asset_id_from_path(path: str | None) -> str | None:
    raw = str(path or "")
    if not raw.startswith(PATH_PREFIX):
        return None
    rest = raw[len(PATH_PREFIX) :]
    _, sep, asset = rest.partition("/")
    if not sep:
        return None
    asset = asset.strip()
    return asset or None


def device_root(pair_id: str) -> str:
    return f"{PATH_PREFIX}{pair_id}"


def asset_path(pair_id: str, asset_id: str) -> str:
    return f"{PATH_PREFIX}{pair_id}/{asset_id}"


def pair_url(pair_id: str) -> str:
    """Fotomob link that opens the phone-as-archive flow."""
    base = str(load_settings().get("fotomob_url") or "").strip().rstrip("/")
    token = str(pair_id or "").strip()
    if not base:
        return token
    # Path token so an installed PWA can capture the link; ?device= still works.
    return f"{base}/d/{token}"


def vault_live_sql_subquery(alias: str = "p") -> str:
    """SQL fragment: location is searchable even if a device archive is offline."""
    prefix = f"{alias}." if alias else ""
    miss = f"{prefix}missing"
    src = f"{prefix}source_id"
    return (
        f"({miss} = 0 OR IFNULL("
        f"(SELECT kind FROM sources WHERE id = {src}), 'watched'"
        f") = '{KIND_DEVICE}')"
    )


def _flag(value: Any, default: bool = False) -> int:
    if value is None:
        return 1 if default else 0
    return 1 if value else 0


def normalize_shadow_path(raw: Any) -> str:
    """Resolve a user folder for named phone copies. Empty clears it."""
    text = str(raw or "").strip()
    if not text:
        return ""
    dest = Path(text).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    if not dest.is_dir():
        raise ValueError("Shadow path is not a directory")
    return str(dest.resolve())


def _safe_rel(rel: Any) -> str:
    text = str(rel or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in Path(text).parts if p not in ("", ".", "..")]
    return "/".join(parts)


def create_pair(
    label: str | None = None,
    *,
    shadow_path: str | None = None,
    shadow_enabled: bool | None = None,
    sync_enabled: bool | None = None,
) -> dict[str, Any]:
    """Register a device archive before the phone connects."""
    pair_id = secrets.token_urlsafe(8)
    path = device_root(pair_id)
    lbl = (label or "").strip() or "Phone"
    shadow = normalize_shadow_path(shadow_path)
    keep_shadow = _flag(shadow_enabled, default=True)
    keep_sync = _flag(sync_enabled, default=True)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO sources(
                path, label, enabled, kind, shadow_path, shadow_enabled, sync_enabled
            )
            VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (path, lbl, KIND_DEVICE, shadow or None, keep_shadow, keep_sync),
        )
        row = conn.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()
        if keep_shadow and not shadow:
            from fotoman.replica import default_shadow_path

            shadow = default_shadow_path(int(row["id"]))
            conn.execute(
                "UPDATE sources SET shadow_path = ?, shadow_enabled = 1 WHERE id = ?",
                (shadow, int(row["id"])),
            )
            row = conn.execute(
                "SELECT * FROM sources WHERE id = ?", (int(row["id"]),)
            ).fetchone()
    src = dict(row)
    out = annotate_source(src)
    _ensure_volume_collection(out)
    return out


def get_by_pair(pair_id: str) -> dict[str, Any] | None:
    path = device_root(str(pair_id or "").strip())
    with db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()
    return dict(row) if row else None


def annotate_source(src: dict[str, Any]) -> dict[str, Any]:
    out = dict(src)
    if not is_device_source(out):
        return out
    pair = pair_id_from_path(out.get("path"))
    out["pair_id"] = pair
    out["pair_url"] = pair_url(pair) if pair else ""
    out["shadow_path"] = str(out.get("shadow_path") or "").strip()
    out["shadow_enabled"] = bool(int(out.get("shadow_enabled") or 0))
    out["sync_enabled"] = bool(int(out.get("sync_enabled") or 0))
    out["exists"] = source_exists(out)
    return out


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
    """Update shadow/sync flags for a volume (folder, phone, or zip import)."""
    from fotoman import replica

    return replica.patch_source(
        source_id,
        shadow_path=shadow_path,
        shadow_enabled=shadow_enabled,
        sync_enabled=sync_enabled,
        has_shadow_path=has_shadow_path,
        has_shadow_enabled=has_shadow_enabled,
        has_sync_enabled=has_sync_enabled,
    )


def _ensure_volume_collection(src: dict[str, Any] | None) -> None:
    if not src or not is_device_source(src):
        return
    try:
        from fotoman import collections

        collections.ensure_folder_collection(
            str(src.get("label") or "Phone"),
            f"{int(src['id'])}:",
        )
    except Exception:
        log.exception("Could not share device volume as a collection")


def _ensure_upload_folder_collection(
    src: dict[str, Any] | None, folder: str
) -> dict[str, Any] | None:
    """Root Collection live-linked to a named upload folder (idempotent)."""
    name = clean_folder_name(folder)
    if not src or not is_device_source(src) or not name:
        return None
    try:
        from fotoman import collections

        return collections.ensure_folder_collection(name, f"{int(src['id'])}:{name}")
    except Exception:
        log.exception("Could not share upload folder as a collection")
        return None


def _queue_scan(src: dict[str, Any] | None, *, force: bool = False) -> None:
    if not src:
        return
    if not force and not int(src.get("sync_enabled") or 0):
        return
    pair = pair_id_from_path(src.get("path"))
    if pair and not get_catalog(pair):
        return
    try:
        from fotoman import indexer

        indexer.start_index_background(
            source_id=int(src["id"]), mode="scan", high_priority=True
        )
    except Exception:
        log.exception("Failed to queue device scan")


def is_online(pair_id: str | None) -> bool:
    if not pair_id:
        return False
    with _lock:
        return pair_id in _online


def source_exists(src: dict[str, Any] | None) -> bool:
    from fotoman import replica

    return replica.source_exists(src)


def set_online(pair_id: str, online: bool, label: str | None = None) -> dict[str, Any] | None:
    pair = str(pair_id or "").strip()
    if not pair:
        return None
    src = get_by_pair(pair)
    if not src:
        return None
    with _lock:
        if online:
            _online.add(pair)
        else:
            _online.discard(pair)
    if label:
        lbl = str(label).strip()
        if lbl and lbl != src.get("label"):
            with db() as conn:
                conn.execute(
                    "UPDATE sources SET label = ? WHERE id = ?",
                    (lbl, int(src["id"])),
                )
            src["label"] = lbl
    try:
        from fotoman.events import publish

        publish("sources", force=True)
    except Exception:
        pass
    out = annotate_source(get_by_pair(pair) or src)
    if online:
        _queue_scan(out)
    return out


def _clean_catalog_file(raw: dict[str, Any]) -> dict[str, Any] | None:
    asset_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or asset_id or "photo").strip() or "photo"
    if not asset_id:
        return None
    rel = str(raw.get("rel") or name).replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        rel = name
    suffix = Path(name).suffix.lower() or Path(rel).suffix.lower()
    if suffix and suffix not in IMAGE_EXTS:
        return None
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    try:
        mtime = float(raw.get("mtime") or 0)
    except (TypeError, ValueError):
        mtime = 0.0
    return {
        "id": asset_id,
        "name": name,
        "rel": rel,
        "size": size,
        "mtime": mtime,
        "type": str(raw.get("type") or ""),
    }


def clean_folder_name(raw: Any) -> str:
    text = str(raw or "").strip().replace("\\", "/").strip("/")
    if not text or ".." in Path(text).parts:
        return ""
    name = Path(text).name.strip()
    if not name or name in (".", ".."):
        return ""
    return name[:80]


def _rel_with_folder(item: dict[str, Any], folder: str) -> str:
    if not folder:
        return str(item.get("rel") or item.get("name") or item["id"])
    return f"{folder}/{Path(str(item.get('name') or item['id'])).name}"


def _shadow_dir(pair_id: str) -> Path:
    digest = hashlib.sha1(pair_id.encode()).hexdigest()[:16]
    return CACHE_DIR / "device" / digest


def named_shadow_file(pair_id: str | None, rel: str | None) -> Path | None:
    """User-facing copy at shadow_path / rel, or None if unset/unsafe."""
    if not pair_id:
        return None
    src = get_by_pair(pair_id)
    root = str((src or {}).get("shadow_path") or "").strip()
    safe = _safe_rel(rel)
    if not src or not root or not safe:
        return None
    dest = Path(root).expanduser() / safe
    try:
        dest.resolve().relative_to(Path(root).expanduser().resolve())
    except (ValueError, OSError):
        return None
    return dest


def local_copies(path: str, rel: str | None = None) -> list[Path]:
    """Cache file and named shadow copy for a device location."""
    pair = pair_id_from_path(path)
    asset = asset_id_from_path(path)
    if not pair or not asset:
        return []
    out: list[Path] = []
    cached = cache_file(pair, asset)
    if cached.is_file():
        out.append(cached)
    name_rel = rel
    if not name_rel:
        for item in get_catalog(pair):
            if item.get("id") == asset:
                name_rel = item.get("rel") or item.get("name")
                break
    named = named_shadow_file(pair, name_rel)
    if named and named.is_file() and named not in out:
        out.append(named)
    return out


def drop_catalog_asset(pair_id: str | None, asset_id: str | None) -> None:
    pair = str(pair_id or "").strip()
    asset = str(asset_id or "").strip()
    if not pair or not asset:
        return
    files = [item for item in get_catalog(pair) if item.get("id") != asset]
    _persist_catalog(pair, files)


def has_local_copy(pair_id: str | None, asset_id: str | None, rel: str | None = None) -> bool:
    if not pair_id or not asset_id:
        return False
    if cache_file(pair_id, asset_id).is_file():
        return True
    name_rel = rel
    if not name_rel:
        for item in get_catalog(pair_id):
            if item.get("id") == asset_id:
                name_rel = item.get("rel") or item.get("name")
                break
    dest = named_shadow_file(pair_id, name_rel)
    return bool(dest and dest.is_file())


def has_shadow(pair_id: str | None) -> bool:
    if not pair_id:
        return False
    root = _shadow_dir(pair_id)
    if root.is_dir() and any(
        p.is_file() and p.name != "catalog.json" for p in root.iterdir()
    ):
        return True
    src = get_by_pair(pair_id)
    shadow = str((src or {}).get("shadow_path") or "").strip()
    return bool(shadow and Path(shadow).expanduser().is_dir())


def _catalog_path(pair_id: str) -> Path:
    return _shadow_dir(pair_id) / "catalog.json"


def _persist_catalog(pair_id: str, files: list[dict[str, Any]]) -> None:
    with _lock:
        _catalogs[pair_id] = list(files)
    dest = _catalog_path(pair_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(files))


def _load_catalog(pair_id: str) -> list[dict[str, Any]]:
    with _lock:
        cached = _catalogs.get(pair_id)
        if cached is not None:
            return list(cached)
    path = _catalog_path(pair_id)
    files: list[dict[str, Any]] = []
    if path.is_file():
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            raw = []
        if isinstance(raw, list):
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, dict):
                    continue
                cleaned = _clean_catalog_file(item)
                if not cleaned or cleaned["id"] in seen:
                    continue
                seen.add(cleaned["id"])
                files.append(cleaned)
    with _lock:
        _catalogs[pair_id] = files
    return list(files)


def _merge_catalog(
    pair_id: str,
    incoming: list[dict[str, Any]],
    *,
    folder: str = "",
) -> list[dict[str, Any]]:
    by_id = {item["id"]: item for item in _load_catalog(pair_id)}
    for item in incoming:
        if folder:
            item = {**item, "rel": _rel_with_folder(item, folder)}
        by_id[item["id"]] = item
    merged = list(by_id.values())
    _persist_catalog(pair_id, merged)
    return merged


def apply_catalog(
    pair_id: str,
    files: list[dict[str, Any]] | None,
    *,
    queue_scan: bool = True,
    folder: str | None = None,
) -> dict[str, Any] | None:
    pair = str(pair_id or "").strip()
    src = get_by_pair(pair)
    if not src:
        return None
    dest_folder = clean_folder_name(folder)
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in files or []:
        if not isinstance(raw, dict):
            continue
        item = _clean_catalog_file(raw)
        if not item or item["id"] in seen:
            continue
        if dest_folder:
            item["rel"] = _rel_with_folder(item, dest_folder)
        seen.add(item["id"])
        cleaned.append(item)
    merged = _merge_catalog(pair, cleaned, folder="")
    with db() as conn:
        conn.execute(
            """
            UPDATE sources
            SET listed_at = datetime('now'), listed_count = ?
            WHERE id = ?
            """,
            (len(merged), int(src["id"])),
        )
    out = annotate_source(get_by_pair(pair) or src)
    if queue_scan:
        _queue_scan(out)
    try:
        from fotoman.events import publish

        publish("sources", force=True)
    except Exception:
        pass
    return {**out, "catalog_count": len(merged)}


def get_catalog(pair_id: str | None) -> list[dict[str, Any]]:
    if not pair_id:
        return []
    return _load_catalog(pair_id)


def cache_dir(pair_id: str) -> Path:
    dest = _shadow_dir(pair_id)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def cache_file(pair_id: str, asset_id: str) -> Path:
    name = hashlib.sha1(asset_id.encode()).hexdigest()
    return _shadow_dir(pair_id) / name


def clear_cache(pair_id: str) -> None:
    shutil.rmtree(_shadow_dir(pair_id), ignore_errors=True)
    with _lock:
        _catalogs.pop(pair_id, None)


def _write_named_shadow(
    pair_id: str, asset_id: str, data: bytes, rel: str | None = None
) -> None:
    src = get_by_pair(pair_id)
    if not src or not int(src.get("shadow_enabled") or 0):
        return
    name_rel = rel
    if not name_rel:
        for item in get_catalog(pair_id):
            if item.get("id") == asset_id:
                name_rel = item.get("rel") or item.get("name")
                break
    dest = named_shadow_file(pair_id, name_rel)
    if not dest:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def put_cache(
    pair_id: str, asset_id: str, data: bytes, *, rel: str | None = None
) -> Path:
    dest = cache_file(pair_id, asset_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    _write_named_shadow(pair_id, asset_id, data, rel)
    return dest


def put_upload(
    pair_id: str,
    raw: dict[str, Any] | None,
    *,
    folder: str | None = None,
    queue_scan: bool = False,
) -> dict[str, Any] | None:
    """Store an uploaded original in the shadow cache and merge the catalog."""
    pair = str(pair_id or "").strip()
    src = get_by_pair(pair)
    if not src:
        return None
    item = _clean_catalog_file(raw or {})
    if not item:
        raise ValueError("Invalid upload")
    dest_folder = clean_folder_name(folder if folder is not None else (raw or {}).get("folder"))
    if dest_folder:
        item["rel"] = _rel_with_folder(item, dest_folder)
    body = (raw or {}).get("body") or ""
    try:
        data = body if isinstance(body, bytes) else base64.b64decode(body)
    except Exception as e:
        raise ValueError("Invalid upload body") from e
    if not data:
        raise ValueError("Empty device file")
    put_cache(pair, item["id"], data, rel=item.get("rel"))
    item["size"] = len(data)
    merged = _merge_catalog(pair, [item])
    with db() as conn:
        conn.execute(
            """
            UPDATE sources
            SET listed_at = datetime('now'), listed_count = ?
            WHERE id = ?
            """,
            (len(merged), int(src["id"])),
        )
    out = annotate_source(get_by_pair(pair) or src)
    log.info("Device put %s %s", pair, item.get("rel"))
    _ensure_volume_collection(out)
    folder_coll = (
        _ensure_upload_folder_collection(out, dest_folder) if dest_folder else None
    )
    if queue_scan:
        _queue_scan(out, force=True)
    try:
        from fotoman.events import publish

        publish("sources", force=True)
    except Exception:
        pass
    payload = {
        **out,
        "catalog_count": len(merged),
        "asset_id": item["id"],
        "rel": item["rel"],
    }
    if folder_coll:
        token = str(
            folder_coll.get("share_code") or folder_coll.get("fotomob_share_token") or ""
        ).strip()
        if token:
            payload["share_code"] = token
            payload["share_url"] = str(folder_coll.get("share_url") or "").strip()
            payload["collection_id"] = folder_coll.get("id")
            payload["collection_name"] = str(folder_coll.get("name") or dest_folder)
    return payload


def fetch_bytes(pair_id: str, asset_id: str, timeout: float = 120.0) -> bytes:
    cached = cache_file(pair_id, asset_id)
    if cached.is_file():
        return cached.read_bytes()
    rel = None
    for item in get_catalog(pair_id):
        if item.get("id") == asset_id:
            rel = item.get("rel") or item.get("name")
            break
    named = named_shadow_file(pair_id, rel)
    if named and named.is_file():
        data = named.read_bytes()
        dest = cache_file(pair_id, asset_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return data
    from fotoman.fotomob_link import device_call_sync

    msg = device_call_sync(pair_id, "fetch", {"asset_id": asset_id}, timeout=timeout)
    raw = msg.get("body") or ""
    import base64

    data = base64.b64decode(raw)
    if not data:
        raise RuntimeError("Empty device file")
    put_cache(pair_id, asset_id, data, rel=rel)
    return data


def readable_path(path: str | None) -> Path | None:
    """Local file for hashing/EXIF/embed/original, or None if unavailable."""
    raw = str(path or "")
    if not raw:
        return None
    if not is_device_path(raw):
        p = Path(raw)
        if p.is_file():
            return p
        from fotoman import replica

        return replica.readable_stored_path(raw)
    pair = pair_id_from_path(raw)
    asset = asset_id_from_path(raw)
    if not pair or not asset:
        return None
    cached = cache_file(pair, asset)
    if cached.is_file():
        return cached
    rel = None
    for item in get_catalog(pair):
        if item.get("id") == asset:
            rel = item.get("rel") or item.get("name")
            break
    named = named_shadow_file(pair, rel)
    if named and named.is_file():
        return named
    if not is_online(pair):
        return None
    try:
        fetch_bytes(pair, asset)
    except Exception as e:
        log.warning("Device fetch failed for %s: %s", raw, e)
        return None
    return cached if cached.is_file() else None


def reset_runtime() -> None:
    """Test helper: drop in-memory device state."""
    with _lock:
        _online.clear()
        _catalogs.clear()
