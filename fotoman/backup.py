"""Collection-bound backup archives: incremental copy without re-indexing."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

from fotoman.db import db
from fotoman.metadata import write_taken_at_mtime

KIND_WATCHED = "watched"
KIND_BACKUP = "backup"

_lock = threading.Lock()
_syncing: set[int] = set()


def is_backup(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return str(row.get("kind") or KIND_WATCHED) == KIND_BACKUP


def get_source(source_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (int(source_id),)
        ).fetchone()
    return dict(row) if row else None


def get_source_by_path(path: str) -> dict[str, Any] | None:
    p = str(Path(path).expanduser().resolve())
    with db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE path = ?", (p,)).fetchone()
    return dict(row) if row else None


def bind_backup(source_id: int, collection_id: int) -> dict[str, Any]:
    with db() as conn:
        cur = conn.execute(
            """
            UPDATE sources
            SET kind = ?, backup_collection_id = ?
            WHERE id = ?
            """,
            (KIND_BACKUP, int(collection_id), int(source_id)),
        )
        if cur.rowcount == 0:
            raise KeyError("not found")
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (int(source_id),)
        ).fetchone()
    return dict(row)


def _present_content_ids(conn, source_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT content_id FROM photos
        WHERE source_id = ? AND missing = 0 AND content_id IS NOT NULL
        """,
        (int(source_id),),
    ).fetchall()
    return {int(r["content_id"]) for r in rows}


def _existing_by_content(conn, source_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM photos WHERE source_id = ? AND content_id IS NOT NULL",
        (int(source_id),),
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        out[int(r["content_id"])] = dict(r)
    return out


def status_for_source(src: dict[str, Any], *, include_culled: bool = False) -> dict[str, Any]:
    """Pending / copied counts for a backup archive (no filesystem walk)."""
    from fotoman import collections

    collection_id = src.get("backup_collection_id")
    if collection_id is None:
        return {
            "backup_pending_count": 0,
            "backup_copied_count": int(src.get("photo_count") or 0),
            "backup_extra_count": 0,
        }
    with db() as conn:
        desired = set(
            collections._content_ids_for_collection(
                conn, int(collection_id), include_culled, True
            )
        )
        present = _present_content_ids(conn, int(src["id"]))
    return {
        "backup_pending_count": len(desired - present),
        "backup_copied_count": len(desired & present),
        "backup_extra_count": len(present - desired),
    }


def sync_backup(
    source_id: int,
    *,
    include_culled: bool = False,
    write_mtime: bool = True,
) -> dict[str, Any]:
    """Copy new collection members onto a backup archive; hash-link, no re-embed.

    Does not delete extras left on the dest. Recopies when a tracked file is gone.
    """
    from fotoman import indexer, collections

    src = get_source(source_id)
    if not src:
        raise KeyError("not found")
    if not is_backup(src):
        raise ValueError("Not a backup volume")
    collection_id = src.get("backup_collection_id")
    if collection_id is None:
        raise ValueError("Backup volume is not bound to a collection")
    root = Path(src["path"]).expanduser().resolve()
    if not root.exists():
        return {
            "exported": 0,
            "copied": 0,
            "skipped": 0,
            "failed": 0,
            "extra": 0,
            "pending": 0,
            "offline": True,
            "dest": str(root),
            "errors": [],
            "archive": src,
        }
    dest = root.parent
    culled = collections._culled_sql(include_culled, "p")
    with db() as conn:
        folder = collections._collection_row(conn, int(collection_id))
        if not folder:
            raise ValueError("Bound collection no longer exists")
        items = collections._walk_export_nodes(
            conn, int(collection_id), "", culled, include_culled
        )
        existing = _existing_by_content(conn, int(source_id))
        present = _present_content_ids(conn, int(source_id))
        desired = {
            int(cid)
            for *_, cid in items
            if cid is not None
        }

    copied_rows: list[tuple[int, str, str, float]] = []
    exported = 0
    skipped = 0
    failed = 0
    errors: list[dict] = []

    for pid, src_s, rel_dir, taken_at, content_id in items:
        if content_id is None:
            skipped += 1
            continue
        cid = int(content_id)
        prev = existing.get(cid)
        if prev and not prev.get("missing"):
            prev_path = Path(prev["path"] or "")
            if prev_path.is_file():
                skipped += 1
                continue
        out_dir = dest / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        if not src_s:
            skipped += 1
            continue
        src_path = Path(src_s)
        orig_name = src_path.name
        if not src_path.is_file():
            from fotoman import device

            local = device.readable_path(src_s)
            if not local:
                skipped += 1
                continue
            src_path = local
        try:
            src_res = src_path.resolve()
            try:
                src_res.relative_to(root)
                skipped += 1
                continue
            except ValueError:
                pass
            if prev and prev.get("path"):
                out = Path(prev["path"])
                out.parent.mkdir(parents=True, exist_ok=True)
            else:
                out = collections._unique_export_path(out_dir, orig_name)
            shutil.copy2(src_path, out)
            if write_mtime and taken_at:
                try:
                    write_taken_at_mtime(out, taken_at)
                except Exception:
                    pass
            exported += 1
            try:
                mtime = float(out.stat().st_mtime)
            except OSError:
                mtime = 0.0
            copied_rows.append(
                (cid, str(out.resolve()), out.resolve().relative_to(root).as_posix(), mtime)
            )
        except OSError as e:
            failed += 1
            if len(errors) < 5:
                errors.append({"photo_id": pid, "error": str(e)})

    if copied_rows:
        indexer.add_linked_locations(int(source_id), copied_rows)

    extra = len(present - desired)
    archive = get_source(source_id) or src
    pending = status_for_source(
        {**archive, "photo_count": archive.get("listed_count")},
        include_culled=include_culled,
    )
    return {
        "exported": exported,
        "copied": exported,
        "skipped": skipped,
        "failed": failed,
        "extra": extra,
        "pending": pending["backup_pending_count"],
        "offline": False,
        "dest": str(root),
        "errors": errors,
        "archive": {**archive, **pending, "linked": exported},
    }


def covering_backup_ids(collection_id: int) -> list[int]:
    """Backup archives bound to this collection or an ancestor (subtree copies)."""
    from fotoman import collections

    with db() as conn:
        ids = [int(collection_id), *collections._ancestor_ids(conn, int(collection_id))]
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT id, path FROM sources
            WHERE enabled = 1
              AND IFNULL(kind, 'watched') = 'backup'
              AND backup_collection_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    return [int(r["id"]) for r in rows if Path(r["path"]).exists()]


def schedule_collection(collection_id: int) -> list[int]:
    """Copy new members onto backups covering this collection (background)."""
    started: list[int] = []
    for sid in covering_backup_ids(collection_id):
        if start_sync_background(sid):
            started.append(sid)
    return started


def schedule_pending() -> list[int]:
    """Catch up live-linked backups after a watched archive gains files."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM sources
            WHERE enabled = 1 AND IFNULL(kind, 'watched') = 'backup'
            """
        ).fetchall()
    started: list[int] = []
    for row in rows:
        src = dict(row)
        if not Path(src["path"]).exists():
            continue
        if status_for_source(src)["backup_pending_count"] <= 0:
            continue
        if start_sync_background(int(src["id"])):
            started.append(int(src["id"]))
    return started


def start_sync_background(source_id: int) -> bool:
    """Run sync in a daemon thread. Returns False if already running for this archive."""
    sid = int(source_id)
    with _lock:
        if sid in _syncing:
            return False
        _syncing.add(sid)

    def run() -> None:
        try:
            sync_backup(sid)
        except Exception:
            pass
        finally:
            with _lock:
                _syncing.discard(sid)
            try:
                from fotoman import events

                events.publish("index", force=True)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True, name=f"backup-sync-{sid}").start()
    return True
