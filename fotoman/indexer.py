from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from fotoman.config import IMAGE_EXTS, THUMBS_DIR
from fotoman.db import content_thumb_path, db, gc_orphaned_content, get_index_job, save_index_job
from fotoman.embed import embed_images
from fotoman.events import publish
from fotoman.geocode import (
    _format_place as format_place,
    forward_geocode_label,
    is_valid_gps,
    lookup_city_coords,
    parse_city_country,
    reverse_places,
)
from fotoman.index_store import get_store
from fotoman.metadata import (
    album_from_rel,
    extract_exif_and_thumbnail,
    file_sha1,
    pixel_fingerprint,
    place_from_rel,
)

ProgressCb = Callable[[dict[str, Any]], None]

# Commit scan rows and persist job progress this often
_SCAN_COMMIT_EVERY = 25

_state: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
    "error": None,
    "reembed": False,
    "source_id": None,
    "embed_suspended": False,
    "pending_embed": 0,
}
_lock = threading.RLock()
_persist_counter = 0
# High-priority scan requests (e.g. newly added archives) wait here when the
# indexer is busy. They always run before geocode/embed (post-hash) work.
_pending: deque[dict[str, Any]] = deque()
_preempt_for_scan = False
_suspend_requested = False
# True when face detection auto-paused heavy work and should resume it after.
_paused_for_faces = False


def get_index_status() -> dict[str, Any]:
    with _lock:
        snap = dict(_state)
        queued_embed = [
            int(j["source_id"])
            for j in _pending
            if (j.get("mode") or "") == "embed" and j.get("source_id") is not None
        ]
        queued_index = [
            int(j["source_id"])
            for j in _pending
            if (j.get("mode") or "scan") in ("list", "scan", "index")
            and j.get("source_id") is not None
        ]
    snap["queued_embed"] = queued_embed
    snap["queued_embed_count"] = len(queued_embed)
    snap["queued_index"] = queued_index
    if "pending_embed" not in snap or not snap["running"]:
        snap["pending_embed"] = count_pending_embeds()
    return snap


def _dequeue_embed_job_unlocked(source_id: int) -> bool:
    """Remove a queued embed job for this archive. Caller holds _lock."""
    before = len(_pending)
    kept: deque[dict[str, Any]] = deque()
    for existing in _pending:
        if (
            (existing.get("mode") or "") == "embed"
            and existing.get("source_id") is not None
            and int(existing["source_id"]) == int(source_id)
        ):
            continue
        kept.append(existing)
    _pending.clear()
    _pending.extend(kept)
    return len(_pending) < before


def count_pending_embeds(source_id: int | None = None) -> int:
    """Count unique contents that still need embedding."""
    with db() as conn:
        if source_id is None:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT c.id) AS c
                FROM photo_contents c
                JOIN photos p ON p.content_id = c.id
                JOIN sources s ON s.id = p.source_id
                WHERE c.embedding_ready = 0
                  AND s.enabled = 1
                  AND s.embed_enabled = 1
                """
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT c.id) AS c
                FROM photo_contents c
                JOIN photos p ON p.content_id = c.id
                WHERE c.embedding_ready = 0 AND p.source_id = ?
                """,
                (source_id,),
            ).fetchone()
    return int(row["c"] if row else 0)


def _pending_embed_items(source_id: int | None = None) -> list[tuple[int, Path]]:
    """Contents not yet embedded, with one readable path each. Returns (content_id, path)."""
    with db() as conn:
        if source_id is None:
            rows = conn.execute(
                """
                SELECT c.id AS content_id, MIN(p.path) AS path
                FROM photo_contents c
                JOIN photos p ON p.content_id = c.id
                WHERE c.embedding_ready = 0
                GROUP BY c.id
                ORDER BY c.id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.id AS content_id, MIN(p.path) AS path
                FROM photo_contents c
                JOIN photos p ON p.content_id = c.id
                WHERE c.embedding_ready = 0 AND p.source_id = ?
                GROUP BY c.id
                ORDER BY c.id
                """,
                (source_id,),
            ).fetchall()
    items: list[tuple[int, Path]] = []
    for r in rows:
        p = Path(r["path"])
        if p.is_file():
            items.append((int(r["content_id"]), p))
    return items


def _set(persist: bool = True, force_persist: bool = False, **kwargs: Any) -> None:
    global _persist_counter
    with _lock:
        important = any(
            k in kwargs and kwargs[k] != _state.get(k)
            for k in ("phase", "running", "error", "reembed", "source_id", "embed_suspended")
        )
        _state.update(kwargs)
        snap = dict(_state)
    # Notify UI on every progress tick (SSE throttled); force on phase changes.
    publish("index", snap, force=important or force_persist)
    if not persist:
        return
    _persist_counter += 1
    if force_persist or important or _persist_counter >= _SCAN_COMMIT_EVERY:
        _persist_counter = 0
        save_index_job(
            running=bool(snap["running"]),
            phase=str(snap["phase"]),
            current=int(snap["current"] or 0),
            total=int(snap["total"] or 0),
            message=str(snap.get("message") or ""),
            error=snap.get("error"),
            reembed=bool(snap.get("reembed")),
            source_id=snap.get("source_id"),
        )


def load_persisted_status() -> None:
    """Hydrate in-memory status from SQLite (call after init_db)."""
    job = get_index_job()
    phase = job.get("phase") or "idle"
    with _lock:
        _state.update(
            {
                "running": False,  # process just started; resume decides whether to restart
                "phase": phase,
                "current": int(job.get("current") or 0),
                "total": int(job.get("total") or 0),
                "message": job.get("message") or "",
                "error": job.get("error"),
                "reembed": bool(job.get("reembed")),
                "source_id": job.get("source_id"),
                "embed_suspended": phase == "suspended",
            }
        )


def list_image_files(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name.startswith("."):
            continue
        if p.suffix.lower() in IMAGE_EXTS:
            files.append(p)
    return files


def add_source(path: str, label: str | None = None) -> dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise ValueError(f"Source path does not exist or is not a directory: {p}")
    lbl = label or p.name or str(p)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO sources(path, label, enabled) VALUES (?, ?, 1)
            ON CONFLICT(path) DO UPDATE SET label=excluded.label, enabled=1
            """,
            (str(p), lbl),
        )
        row = conn.execute("SELECT * FROM sources WHERE path = ?", (str(p),)).fetchone()
        return dict(row)


def set_source_enabled(source_id: int, enabled: bool) -> None:
    with db() as conn:
        conn.execute("UPDATE sources SET enabled = ? WHERE id = ?", (1 if enabled else 0, source_id))


def set_source_label(source_id: int, label: str) -> dict[str, Any]:
    lbl = label.strip()
    if not lbl:
        raise ValueError("Label cannot be empty")
    with db() as conn:
        cur = conn.execute("UPDATE sources SET label = ? WHERE id = ?", (lbl, source_id))
        if cur.rowcount == 0:
            raise ValueError(f"Source not found: {source_id}")
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return dict(row)


def _other_source_for_path(
    photo_path: str, others: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the most specific remaining archive that contains this photo path."""
    matches: list[dict[str, Any]] = []
    for src in others:
        root = str(src["path"]).rstrip("/")
        if photo_path == root or photo_path.startswith(root + "/"):
            matches.append(src)
    if not matches:
        return None
    return max(matches, key=lambda s: len(str(s["path"]).rstrip("/")))


def remove_source(source_id: int) -> None:
    """Remove an archive and its location rows. Shared content is kept if other archives still reference it."""
    with db() as conn:
        content_ids = [
            int(r["content_id"])
            for r in conn.execute(
                "SELECT DISTINCT content_id FROM photos WHERE source_id = ?",
                (source_id,),
            ).fetchall()
        ]
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        for cid in content_ids:
            gc_orphaned_content(conn, cid)


def _take_pending() -> list[dict[str, Any]]:
    """Drain queued index requests (scans first). Clears preempt flag. Caller must hold _lock."""
    global _preempt_for_scan
    items = list(_pending)
    _pending.clear()
    _preempt_for_scan = False
    return items


def _should_preempt() -> bool:
    with _lock:
        return _preempt_for_scan or bool(_pending)


def _should_suspend_embed() -> bool:
    """True when embedding should not run (user Pause / waiting for Go)."""
    with _lock:
        return _suspend_requested or bool(_state.get("embed_suspended"))


def _should_pause_heavy() -> bool:
    """True when face detection needs index/geocode to yield the CPU."""
    with _lock:
        return _paused_for_faces


def _enqueue_index_job_unlocked(job: dict[str, Any], *, high_priority: bool) -> None:
    """Queue a job while holding _lock. High-priority list/index preempts post-hash work."""
    global _preempt_for_scan
    sid = job.get("source_id")
    mode = job.get("mode") or "scan"
    # Drop a duplicate pending request for the same source+mode (keep newest flags).
    kept: deque[dict[str, Any]] = deque()
    for existing in _pending:
        same_sid = existing.get("source_id") == sid and (
            sid is not None or existing.get("source_id") is None
        )
        same_mode = (existing.get("mode") or "scan") == mode
        if same_sid and same_mode:
            continue
        kept.append(existing)
    _pending.clear()
    _pending.extend(kept)
    if high_priority:
        _pending.appendleft(job)
        # Preempt geocode/grayscale/embed (and mid-EXIF index) so new-archive hashing runs next.
        if _state["phase"] in ("embed", "geocode", "grayscale", "index"):
            _preempt_for_scan = True
    else:
        _pending.append(job)


def _load_sources(source_id: int | None) -> list[dict[str, Any]]:
    with db() as conn:
        if source_id is None:
            rows = conn.execute("SELECT * FROM sources WHERE enabled = 1").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchall()
            if not rows:
                raise ValueError(f"Source not found: {source_id}")
    return [dict(r) for r in rows]


def _delete_photo_ids(photo_ids: list[int]) -> None:
    if not photo_ids:
        return
    with db() as conn:
        placeholders = ",".join("?" * len(photo_ids))
        content_ids = [
            int(r["content_id"])
            for r in conn.execute(
                f"SELECT DISTINCT content_id FROM photos WHERE id IN ({placeholders})",
                photo_ids,
            ).fetchall()
        ]
        conn.executemany("DELETE FROM photos WHERE id = ?", [(i,) for i in photo_ids])
        for cid in content_ids:
            gc_orphaned_content(conn, cid)


def _list_source(src: dict[str, Any]) -> dict[str, Any]:
    """Fast directory listing + delta vs indexed photos. Updates listed_at/count."""
    sid = int(src["id"])
    root = Path(src["path"])
    label = src["label"]
    if not root.exists():
        _set(
            phase="list",
            current=0,
            total=0,
            message=f"Skipping missing source: {root}",
            source_id=sid,
            force_persist=True,
        )
        return {
            "source": src,
            "listed_count": 0,
            "to_index": [],
            "removed_ids": [],
            "changed": False,
            "missing": True,
        }

    files = list_image_files(root)
    _set(
        phase="list",
        current=0,
        total=len(files),
        message=f"Listing {label}",
        source_id=sid,
        force_persist=True,
    )

    with db() as conn:
        existing_rows = conn.execute(
            """
            SELECT p.path, p.id, c.size, p.mtime, c.embedding_ready, p.content_id,
                   c.width
            FROM photos p
            JOIN photo_contents c ON c.id = p.content_id
            WHERE p.source_id = ?
            """,
            (sid,),
        ).fetchall()
    by_path = {r["path"]: dict(r) for r in existing_rows}

    to_index: list[tuple[Path, str, float, int, dict[str, Any] | None]] = []
    seen: set[str] = set()
    for i, path in enumerate(files, 1):
        if i == 1 or i % 50 == 0 or i == len(files):
            _set(current=i, persist=False)
        try:
            st = path.stat()
        except OSError:
            continue
        path_s = str(path)
        seen.add(path_s)
        existing = by_path.get(path_s)
        # Re-index when size/mtime changed, or hash-linked stub still needs EXIF.
        if (
            existing
            and existing["size"] == st.st_size
            and abs((existing["mtime"] or 0) - st.st_mtime) < 0.001
            and existing["width"] is not None
        ):
            continue
        rel = str(path.relative_to(root))
        to_index.append((path, rel, st.st_mtime, st.st_size, existing))

    removed_ids = [int(r["id"]) for p, r in by_path.items() if p not in seen]
    if removed_ids:
        _delete_photo_ids(removed_ids)

    listed_count = len(seen)
    with db() as conn:
        conn.execute(
            """
            UPDATE sources
            SET listed_at = datetime('now'), listed_count = ?
            WHERE id = ?
            """,
            (listed_count, sid),
        )

    changed = bool(to_index) or bool(removed_ids)
    _set(
        current=listed_count,
        total=listed_count,
        message=(
            f"Listed {label}: {listed_count} files"
            + (f", {len(to_index)} to index" if to_index else ", no changes")
            + (f", {len(removed_ids)} removed" if removed_ids else "")
        ),
        force_persist=True,
    )
    return {
        "source": src,
        "listed_count": listed_count,
        "to_index": to_index,
        "removed_ids": removed_ids,
        "changed": changed,
        "missing": False,
    }


def _index_source_files(
    src: dict[str, Any],
    to_index: list[tuple[Path, str, float, int, dict[str, Any] | None]],
) -> tuple[int, list[tuple[int, Path]]]:
    """Hash-link then EXIF/thumb for listed deltas. Reuses content by hash.

    Pass 1 hashes every file and links photos → contents so new archives become
    searchable before slower post-hash work. Pass 2 fills EXIF/thumbs only for
    contents that still need metadata. Geocode/embed run later (after other
    pending index jobs). Returns (discovered, to_embed) with content_id paths.
    """
    if not to_index:
        return 0, []

    sid = int(src["id"])
    label = src["label"]
    discovered = 0
    to_embed: list[tuple[int, Path]] = []
    seen_embed: set[int] = set()
    # content_id → path for contents that still need EXIF/thumb after hash-link.
    need_meta: dict[int, Path] = {}

    _set(
        phase="index",
        current=0,
        total=len(to_index),
        message=f"Indexing {label}",
        source_id=sid,
        force_persist=True,
    )

    def link_hashed(
        *,
        path: Path,
        rel: str,
        fhash: str,
        size: int,
        mtime: float,
    ) -> None:
        nonlocal discovered
        album = album_from_rel(rel)
        path_s = str(path)
        with db() as conn:
            existing = conn.execute(
                """
                SELECT id, embedding_ready, width
                FROM photo_contents WHERE file_hash = ?
                """,
                (fhash,),
            ).fetchone()

            if existing:
                content_id = int(existing["id"])
                conn.execute(
                    """
                    UPDATE photo_contents SET size = COALESCE(size, ?)
                    WHERE id = ?
                    """,
                    (size, content_id),
                )
                need_embed = not int(existing["embedding_ready"] or 0)
                if existing["width"] is None:
                    need_meta[content_id] = path
            else:
                cur = conn.execute(
                    """
                    INSERT INTO photo_contents(
                        file_hash, size, embedding_ready, faces_ready
                    ) VALUES (?, ?, 0, 0)
                    """,
                    (fhash, size),
                )
                content_id = int(cur.lastrowid)
                need_embed = True
                need_meta[content_id] = path

            old = conn.execute(
                "SELECT id, content_id FROM photos WHERE source_id = ? AND path = ?",
                (sid, path_s),
            ).fetchone()
            old_content_id = int(old["content_id"]) if old else None

            conn.execute(
                """
                INSERT INTO photos(
                    content_id, source_id, path, rel_path, mtime, album,
                    culled, is_keeper
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 1)
                ON CONFLICT(source_id, path) DO UPDATE SET
                    content_id=excluded.content_id,
                    rel_path=excluded.rel_path,
                    mtime=excluded.mtime,
                    album=excluded.album
                """,
                (content_id, sid, path_s, rel, mtime, album),
            )

            if old_content_id is not None and old_content_id != content_id:
                gc_orphaned_content(conn, old_content_id)

        discovered += 1
        if need_embed and content_id not in seen_embed:
            seen_embed.add(content_id)
            to_embed.append((content_id, path))

    # Pass 1: hash + link (prioritize getting the archive into the index).
    for i, (path, rel, mtime, size, _existing) in enumerate(to_index, 1):
        if i > 1 and (_should_preempt() or _should_pause_heavy()):
            _set(current=i - 1, force_persist=True)
            return discovered, to_embed

        _set(current=i, persist=False)
        try:
            fhash = file_sha1(path)
        except OSError:
            continue

        link_hashed(path=path, rel=rel, fhash=fhash, size=size, mtime=mtime)
        if i % _SCAN_COMMIT_EVERY == 0:
            _set(current=i, force_persist=True)

    # Prefer other pending index jobs (e.g. another new archive) over EXIF.
    if need_meta and (_should_preempt() or _should_pause_heavy()):
        _set(current=len(to_index), force_persist=True)
        return discovered, to_embed

    # Pass 2: EXIF/thumb only for contents still missing metadata.
    meta_items = list(need_meta.items())
    if meta_items:
        _set(
            phase="index",
            current=0,
            total=len(meta_items),
            message=f"Reading metadata — {label}",
            source_id=sid,
            force_persist=True,
        )
        for i, (content_id, path) in enumerate(meta_items, 1):
            if i > 1 and (_should_preempt() or _should_pause_heavy()):
                _set(current=i - 1, force_persist=True)
                return discovered, to_embed

            _set(current=i, persist=False)
            thumb_tmp: Path | None = THUMBS_DIR / f"_pending_{sid}_{content_id}.jpg"
            try:
                meta = extract_exif_and_thumbnail(path, thumb_tmp)
            except Exception:
                meta = {
                    "width": None,
                    "height": None,
                    "taken_at": None,
                    "camera": None,
                    "lat": None,
                    "lon": None,
                }
                if thumb_tmp is not None:
                    thumb_tmp.unlink(missing_ok=True)
                    thumb_tmp = None

            with db() as conn:
                # Another path may have filled this content while we waited.
                row = conn.execute(
                    "SELECT width FROM photo_contents WHERE id = ?",
                    (content_id,),
                ).fetchone()
                if row and row["width"] is None:
                    conn.execute(
                        """
                        UPDATE photo_contents SET
                            width=COALESCE(width, ?),
                            height=COALESCE(height, ?),
                            taken_at=COALESCE(taken_at, ?),
                            camera=COALESCE(camera, ?),
                            lat=COALESCE(lat, ?),
                            lon=COALESCE(lon, ?)
                        WHERE id = ?
                        """,
                        (
                            meta["width"],
                            meta["height"],
                            meta["taken_at"],
                            meta["camera"],
                            meta["lat"],
                            meta["lon"],
                            content_id,
                        ),
                    )

            dst = content_thumb_path(content_id)
            if thumb_tmp is not None and thumb_tmp.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if not dst.exists():
                    thumb_tmp.replace(dst)
                else:
                    thumb_tmp.unlink(missing_ok=True)

            if i % _SCAN_COMMIT_EVERY == 0:
                _set(current=i, force_persist=True)

    _set(current=len(to_index), total=len(to_index), force_persist=True)
    with db() as conn:
        conn.execute(
            "UPDATE sources SET scanned_at = datetime('now') WHERE id = ?",
            (sid,),
        )
    return discovered, to_embed


def _process_sources(
    *,
    mode: str,
    reembed: bool = False,
    source_id: int | None = None,
) -> tuple[int, list[tuple[int, Path]], bool]:
    """List and optionally index archives.

    Returns (discovered, to_embed, interrupted).
    mode:
      - list: directory listing; index only when listing finds changes
      - scan/index: listing + index deltas (manual Scan / forced index)
    """
    job_mode = mode if mode in ("list", "scan", "index") else "scan"
    do_index_always = job_mode in ("scan", "index")

    _set(
        force_persist=True,
        running=True,
        phase="list",
        current=0,
        total=0,
        message="Listing archives" if source_id is None else "Listing archive",
        error=None,
        reembed=reembed,
        source_id=source_id,
    )
    _reconcile_embedding_flags(source_id=source_id)

    sources = _load_sources(source_id)
    discovered = 0
    to_embed: list[tuple[int, Path]] = []

    for src in sources:
        listing = _list_source(src)
        if listing.get("missing"):
            continue

        should_index = do_index_always or listing["changed"]
        if not should_index:
            # Mark first-round complete even when empty / unchanged so we
            # don't keep re-queueing via ensure_first_round_scans.
            with db() as conn:
                conn.execute(
                    """
                    UPDATE sources SET scanned_at = datetime('now')
                    WHERE id = ? AND scanned_at IS NULL
                    """,
                    (int(src["id"]),),
                )
            continue
        if _should_pause_heavy() and not do_index_always:
            # Face detection holds the CPU — stop indexing until it yields.
            return discovered, to_embed, True

        found, embeds = _index_source_files(src, listing["to_index"])
        discovered += found
        to_embed.extend(embeds)

        if reembed:
            # Force re-embed of all contents still linked from this archive.
            with db() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT c.id AS content_id, MIN(p.path) AS path
                    FROM photo_contents c
                    JOIN photos p ON p.content_id = c.id
                    WHERE p.source_id = ?
                    GROUP BY c.id
                    """,
                    (int(src["id"]),),
                ).fetchall()
            have = {cid for cid, _ in to_embed}
            for r in rows:
                cid = int(r["content_id"])
                if cid not in have and Path(r["path"]).is_file():
                    to_embed.append((cid, Path(r["path"])))
                    have.add(cid)

        if _should_preempt() or _should_pause_heavy():
            return discovered, to_embed, True

        # Removals-only pass: stamp scanned_at when there was nothing to index.
        if not listing["to_index"] and listing["changed"]:
            with db() as conn:
                conn.execute(
                    "UPDATE sources SET scanned_at = datetime('now') WHERE id = ?",
                    (int(src["id"]),),
                )

    return discovered, to_embed, False


def _scan_sources(
    *,
    reembed: bool = False,
    source_id: int | None = None,
) -> tuple[int, list[tuple[int, Path]]]:
    """Back-compat: full list+index pass."""
    discovered, to_embed, _ = _process_sources(
        mode="scan", reembed=reembed, source_id=source_id
    )
    return discovered, to_embed


def _embed_pending(
    to_embed: list[tuple[int, Path]],
    *,
    batch_size: int = 8,
) -> list[tuple[int, Path]]:
    """Embed contents. Returns any remaining items if preempted or suspended."""
    if not to_embed:
        return []

    # Deduplicate by content_id (first path wins).
    by_id: dict[int, Path] = {}
    for cid, path in to_embed:
        by_id.setdefault(int(cid), path)
    to_embed = list(by_id.items())

    store = get_store()
    store.remove_ids({cid for cid, _ in to_embed})

    _set(
        phase="embed",
        current=0,
        total=len(to_embed),
        message="Embedding images",
        force_persist=True,
    )
    for i in range(0, len(to_embed), batch_size):
        if _should_preempt() or _should_suspend_embed():
            store.save()
            return to_embed[i:]

        chunk = to_embed[i : i + batch_size]
        paths = [p for _, p in chunk]
        ids = [cid for cid, _ in chunk]
        try:
            vecs = embed_images(paths, batch_size=batch_size)
            store.add(ids, vecs)
            # Persist FAISS before marking ready so restart never loses vectors.
            store.save()
            with db() as conn:
                conn.executemany(
                    "UPDATE photo_contents SET embedding_ready = 1 WHERE id = ?",
                    [(cid,) for cid in ids],
                )
        except Exception as e:
            _set(message=f"Embed error: {e}")
        done = min(i + batch_size, len(to_embed))
        _set(
            current=done,
            pending_embed=max(0, len(to_embed) - done),
            force_persist=True,
        )

    return []


def _reconcile_embedding_flags(source_id: int | None = None) -> None:
    """Clear embedding_ready when FAISS no longer has the vector (crash mid-embed)."""
    store = get_store()
    faiss_ids = set(store.ids)
    with db() as conn:
        if source_id is None:
            ready = conn.execute(
                "SELECT id FROM photo_contents WHERE embedding_ready = 1"
            ).fetchall()
        else:
            ready = conn.execute(
                """
                SELECT DISTINCT c.id
                FROM photo_contents c
                JOIN photos p ON p.content_id = c.id
                WHERE c.embedding_ready = 1 AND p.source_id = ?
                """,
                (source_id,),
            ).fetchall()
        stale = [(r["id"],) for r in ready if r["id"] not in faiss_ids]
        if stale:
            conn.executemany(
                "UPDATE photo_contents SET embedding_ready = 0 WHERE id = ?", stale
            )


def _backfill_places(batch_size: int = 500, source_id: int | None = None) -> None:
    params: tuple[Any, ...] = ()
    if source_id is not None:
        sql = """
            SELECT DISTINCT c.id, c.lat, c.lon
            FROM photo_contents c
            JOIN photos p ON p.content_id = c.id
            WHERE p.source_id = ?
              AND p.culled = 0
              AND c.lat IS NOT NULL AND c.lon IS NOT NULL
              AND (c.place_label IS NULL OR c.place_label = '')
        """
        params = (source_id,)
    else:
        sql = """
            SELECT id, lat, lon FROM photo_contents
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              AND (place_label IS NULL OR place_label = '')
        """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    rows = [r for r in rows if is_valid_gps(r["lat"], r["lon"])]
    if not rows:
        return
    _set(
        phase="geocode",
        current=0,
        total=len(rows),
        message="Resolving place names",
        source_id=source_id,
        force_persist=True,
    )
    for i in range(0, len(rows), batch_size):
        if i > 0 and (_should_preempt() or _should_pause_heavy()):
            return
        chunk = rows[i : i + batch_size]
        coords = [(r["lat"], r["lon"]) for r in chunk]
        places = reverse_places(coords)
        with db() as conn:
            conn.executemany(
                """
                UPDATE photo_contents
                SET city = ?, region = ?, country = ?, place_label = ?
                WHERE id = ?
                """,
                [
                    (p["city"], p["region"], p["country"], p["place_label"], r["id"])
                    for r, p in zip(chunk, places)
                ],
            )
        _set(current=min(i + batch_size, len(rows)), force_persist=True)


def _backfill_places_from_paths(source_id: int | None = None) -> None:
    """Fill empty place_label from directory names when GPS geocode did not."""
    params: tuple[Any, ...] = ()
    if source_id is not None:
        sql = """
            SELECT c.id, MIN(p.rel_path) AS rel_path
            FROM photo_contents c
            JOIN photos p ON p.content_id = c.id
            WHERE p.source_id = ?
              AND p.culled = 0
              AND (c.place_label IS NULL OR c.place_label = '')
            GROUP BY c.id
        """
        params = (source_id,)
    else:
        sql = """
            SELECT c.id, MIN(p.rel_path) AS rel_path
            FROM photo_contents c
            JOIN photos p ON p.content_id = c.id
            WHERE p.culled = 0
              AND (c.place_label IS NULL OR c.place_label = '')
            GROUP BY c.id
        """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    updates: list[tuple[str, int]] = []
    for r in rows:
        label = place_from_rel(r["rel_path"] or "")
        if label:
            updates.append((label, int(r["id"])))
    if not updates:
        return
    _set(
        phase="geocode",
        current=0,
        total=len(updates),
        message="Filling places from folders",
        source_id=source_id,
        force_persist=True,
    )
    with db() as conn:
        conn.executemany(
            """
            UPDATE photo_contents
            SET place_label = ?
            WHERE id = ? AND (place_label IS NULL OR place_label = '')
            """,
            updates,
        )
    _set(current=len(updates), force_persist=True)


def _forward_geocode_folder_places(source_id: int | None = None) -> None:
    """Forward-geocode freeform place_labels (folder-derived) into structured fields + coords."""
    params: tuple[Any, ...] = ()
    if source_id is not None:
        sql = """
            SELECT DISTINCT c.place_label
            FROM photo_contents c
            JOIN photos p ON p.content_id = c.id
            WHERE p.source_id = ?
              AND c.place_label IS NOT NULL AND c.place_label != ''
              AND (c.country IS NULL OR c.country = '')
              AND c.lat IS NULL
        """
        params = (source_id,)
    else:
        sql = """
            SELECT DISTINCT place_label FROM photo_contents
            WHERE place_label IS NOT NULL AND place_label != ''
              AND (country IS NULL OR country = '')
              AND lat IS NULL
        """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return
    labels = [r["place_label"] for r in rows]
    _set(
        phase="geocode",
        current=0,
        total=len(labels),
        message="Geocoding folder places",
        source_id=source_id,
        force_persist=True,
    )
    for i, label in enumerate(labels, 1):
        if i > 1 and (_should_preempt() or _should_pause_heavy()):
            return
        # Try structured parse first (e.g. "Paris, France")
        parsed = parse_city_country(label)
        if parsed.get("city") and parsed.get("country"):
            coords = lookup_city_coords(parsed["city"], parsed["country"], parsed.get("region"))
            if coords:
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE photo_contents
                        SET city = ?, region = ?, country = ?, place_label = ?,
                            lat = COALESCE(lat, ?), lon = COALESCE(lon, ?)
                        WHERE place_label = ? AND (country IS NULL OR country = '') AND lat IS NULL
                        """,
                        (
                            parsed["city"], parsed.get("region"), parsed["country"],
                            parsed["place_label"], coords[0], coords[1],
                            label,
                        ),
                    )
                _set(current=i, persist=False)
                continue
            # Nominatim failed but we still have structured fields
            with db() as conn:
                conn.execute(
                    """
                    UPDATE photo_contents
                    SET city = ?, region = ?, country = ?, place_label = ?
                    WHERE place_label = ? AND (country IS NULL OR country = '') AND lat IS NULL
                    """,
                    (parsed["city"], parsed.get("region"), parsed["country"],
                     parsed["place_label"], label),
                )
            _set(current=i, persist=False)
            continue

        # Freeform single-token label (e.g. "Amsterdam")
        result = forward_geocode_label(label)
        if result:
            city = result.get("city") or label
            country = result.get("country")
            region = result.get("region")
            place_label = format_place(city, region, country) or label
            with db() as conn:
                conn.execute(
                    """
                    UPDATE photo_contents
                    SET city = ?, region = ?, country = ?, place_label = ?,
                        lat = COALESCE(lat, ?), lon = COALESCE(lon, ?)
                    WHERE place_label = ? AND (country IS NULL OR country = '') AND lat IS NULL
                    """,
                    (city, region, country, place_label,
                     result["lat"], result["lon"], label),
                )
        _set(current=i, persist=False)
    _set(current=len(labels), force_persist=True)


def _compute_fingerprints(batch_size: int = 200, source_id: int | None = None) -> None:
    """Compute grayscale pixel fingerprints for duplicate/similarity detection."""
    params: tuple[Any, ...] = ()
    if source_id is not None:
        sql = """
            SELECT DISTINCT c.id, c.orientation, MIN(p.path) AS path
            FROM photo_contents c
            JOIN photos p ON p.content_id = c.id
            WHERE p.source_id = ? AND c.fingerprint IS NULL AND c.width IS NOT NULL
            GROUP BY c.id
        """
        params = (source_id,)
    else:
        sql = """
            SELECT c.id, c.orientation, MIN(p.path) AS path
            FROM photo_contents c
            JOIN photos p ON p.content_id = c.id
            WHERE c.fingerprint IS NULL AND c.width IS NOT NULL
            GROUP BY c.id
        """
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        return
    _set(
        phase="grayscale",
        current=0,
        total=len(rows),
        message="Computing fingerprints",
        source_id=source_id,
        force_persist=True,
    )
    for i in range(0, len(rows), batch_size):
        if i > 0 and (_should_preempt() or _should_pause_heavy()):
            return
        chunk = rows[i : i + batch_size]
        updates: list[tuple[str, int]] = []
        for r in chunk:
            thumb = content_thumb_path(int(r["id"]))
            src = thumb if thumb.exists() else Path(r["path"])
            orient = int(r["orientation"] or 0)
            fp = (
                pixel_fingerprint(src, orientation=orient) if src.exists() else None
            )
            updates.append((fp or "", int(r["id"])))
        if updates:
            with db() as conn:
                conn.executemany(
                    "UPDATE photo_contents SET fingerprint = ? WHERE id = ?",
                    updates,
                )
        _set(current=min(i + len(chunk), len(rows)), force_persist=True)


def _finish_idle(
    *,
    phase: str,
    message: str,
    embed_suspended: bool = False,
    source_id: int | None = None,
    reembed: bool = False,
) -> dict[str, Any]:
    global _suspend_requested
    pending = count_pending_embeds()
    with _lock:
        _suspend_requested = False
        _state.update(
            {
                "running": False,
                "phase": phase,
                "message": message,
                "error": None,
                "embed_suspended": embed_suspended,
                "pending_embed": pending,
                "source_id": source_id if source_id is not None else _state.get("source_id"),
                "reembed": reembed,
            }
        )
        snap = dict(_state)
    save_index_job(
        running=False,
        phase=phase,
        current=int(snap["current"] or 0),
        total=int(snap["total"] or 0),
        message=message,
        error=None,
        reembed=bool(snap.get("reembed")),
        source_id=snap.get("source_id"),
    )
    publish("index", snap)
    return get_index_status()


def scan_and_embed(
    batch_size: int = 8,
    reembed: bool = False,
    source_id: int | None = None,
    mode: str = "scan",
) -> dict[str, Any]:
    """List/index and/or embed.

    Listing and indexing run automatically. Embedding only runs for explicit
    ``mode="embed"`` jobs (user Go / resume); after index, pending embeds wait.
    """
    valid_modes = ("list", "scan", "index", "embed")
    jobs: deque[dict[str, Any]] = deque(
        [
            {
                "batch_size": batch_size,
                "reembed": reembed,
                "source_id": source_id,
                "mode": mode if mode in valid_modes else "scan",
            }
        ]
    )
    deferred_embed: list[tuple[int, Path]] = []
    discovered_total = 0
    embedded_total = 0
    listed_only = True

    try:
        while True:
            with _lock:
                pending = _take_pending()
            if pending:
                jobs = deque(pending) + jobs

            if jobs:
                job = jobs.popleft()
                job_mode = job.get("mode") or "scan"
                if job_mode == "embed":
                    listed_only = False
                    embed_sid = job.get("source_id")
                    if embed_sid is not None:
                        _set(source_id=embed_sid, force_persist=True)
                    deferred_embed.extend(_pending_embed_items(embed_sid))
                else:
                    sid = job.get("source_id")
                    discovered, to_embed, interrupted = _process_sources(
                        mode=job_mode,
                        reembed=bool(job.get("reembed")),
                        source_id=sid,
                    )
                    discovered_total += discovered
                    if discovered or to_embed or job_mode in ("scan", "index"):
                        listed_only = False
                    # Do not auto-embed after list/index — wait for user Go.
                    if interrupted:
                        waiting = count_pending_embeds(sid)
                        if _should_pause_heavy():
                            # Face job owns the CPU; leave pending embeds for later.
                            if waiting:
                                return _finish_idle(
                                    phase="suspended",
                                    message=(
                                        f"Paused — {waiting} contents waiting to embed."
                                    ),
                                    embed_suspended=True,
                                )
                            return _finish_idle(
                                phase="suspended",
                                message="Indexing paused for face scan.",
                                embed_suspended=True,
                            )
                        if _should_preempt() and sid is not None:
                            jobs.append(
                                {
                                    "batch_size": job.get("batch_size") or batch_size,
                                    "reembed": bool(job.get("reembed")),
                                    "source_id": sid,
                                    "mode": "scan",
                                }
                            )
                continue

            if deferred_embed:
                by_id: dict[int, Path] = {cid: path for cid, path in deferred_embed}
                deferred_embed = list(by_id.items())

            with _lock:
                suspended = bool(_state.get("embed_suspended")) or _suspend_requested
                # New-archive index jobs jump ahead of geocode/embed.
                if _pending or _preempt_for_scan:
                    continue

            if deferred_embed and suspended:
                return _finish_idle(
                    phase="suspended",
                    message=(
                        f"Paused — {len(deferred_embed)} contents waiting to embed."
                    ),
                    embed_suspended=True,
                )

            # Geocode + grayscale whenever pending remains (even quiet list-only
            # startups), so interrupted backfill resumes across restart.
            if not _should_pause_heavy():
                _backfill_places()
                if _should_preempt():
                    continue
                if _should_pause_heavy():
                    waiting = count_pending_embeds()
                    return _finish_idle(
                        phase="suspended",
                        message=(
                            f"Paused — {waiting} contents waiting to embed."
                            if waiting
                            else "Indexing paused for face scan."
                        ),
                        embed_suspended=True,
                    )
                _backfill_places_from_paths()
                if _should_preempt():
                    continue
                if _should_pause_heavy():
                    waiting = count_pending_embeds()
                    return _finish_idle(
                        phase="suspended",
                        message=(
                            f"Paused — {waiting} contents waiting to embed."
                            if waiting
                            else "Indexing paused for face scan."
                        ),
                        embed_suspended=True,
                    )
                _forward_geocode_folder_places()
                if _should_preempt():
                    continue
                if _should_pause_heavy():
                    waiting = count_pending_embeds()
                    return _finish_idle(
                        phase="suspended",
                        message=(
                            f"Paused — {waiting} contents waiting to embed."
                            if waiting
                            else "Indexing paused for face scan."
                        ),
                        embed_suspended=True,
                    )
                _compute_fingerprints()
                if _should_preempt():
                    continue
                if _should_pause_heavy():
                    waiting = count_pending_embeds()
                    return _finish_idle(
                        phase="suspended",
                        message=(
                            f"Paused — {waiting} contents waiting to embed."
                            if waiting
                            else "Indexing paused for face scan."
                        ),
                        embed_suspended=True,
                    )

            if deferred_embed:
                listed_only = False
                before = len(deferred_embed)
                deferred_embed = _embed_pending(
                    deferred_embed, batch_size=batch_size
                )
                embedded_total += before - len(deferred_embed)
                if deferred_embed:
                    if _should_preempt():
                        continue
                    if _should_suspend_embed():
                        return _finish_idle(
                            phase="suspended",
                            message=(
                                f"Paused — "
                                f"{len(deferred_embed)} contents remaining to embed."
                            ),
                            embed_suspended=True,
                        )
                    continue
                continue

            with _lock:
                if _pending or _preempt_for_scan:
                    continue
                if listed_only and discovered_total == 0 and embedded_total == 0:
                    snap_message = "Listing done. No changes."
                else:
                    snap_message = (
                        f"Indexed. New/updated: {discovered_total}, "
                        f"embedded: {embedded_total}"
                    )
            # Embedding is opt-in: any pending vectors wait for Go.
            waiting = count_pending_embeds()
            if waiting > 0:
                return _finish_idle(
                    phase="suspended",
                    message=f"Paused — {waiting} contents waiting to embed.",
                    embed_suspended=True,
                )
            return _finish_idle(
                phase="done", message=snap_message, embed_suspended=False
            )
    except Exception as e:
        _set(running=False, phase="error", error=str(e), message=str(e), force_persist=True)
        raise


def enabled_source_ids() -> list[int]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id FROM sources WHERE enabled = 1 ORDER BY id"
        ).fetchall()
    return [int(r["id"]) for r in rows]


def start_jobs_all_enabled(
    *,
    mode: str = "list",
    reembed: bool = False,
    high_priority: bool = False,
) -> list[int]:
    """Queue one job per enabled archive so progress is per-folder from 0.

    When high_priority is set, only the first job preempts embedding; the rest
    follow in id order (appendleft on every job would reverse the queue).
    """
    ids = enabled_source_ids()
    if not ids:
        return []
    start_index_background(
        reembed=reembed,
        source_id=ids[0],
        mode=mode,
        high_priority=high_priority,
    )
    for sid in ids[1:]:
        start_index_background(
            reembed=reembed,
            source_id=sid,
            mode=mode,
            high_priority=False,
        )
    return ids


def start_list_all_enabled(
    *,
    high_priority: bool = False,
) -> list[int]:
    """On startup: re-list only; index/embed run solely when listing finds changes."""
    return start_jobs_all_enabled(mode="list", high_priority=high_priority)


def start_scan_all_enabled(
    *,
    reembed: bool = False,
    high_priority: bool = False,
) -> list[int]:
    """Queue list+index for every enabled archive."""
    return start_jobs_all_enabled(
        mode="scan", reembed=reembed, high_priority=high_priority
    )


def start_index_background(
    batch_size: int = 8,
    reembed: bool = False,
    source_id: int | None = None,
    *,
    mode: str = "scan",
    high_priority: bool = False,
) -> None:
    """Start indexing, or queue if already running.

    mode:
      - list: directory listing; index only if listing finds changes
      - scan/index: list + index deltas (embedding waits for Go)
      - embed: embed photos with embedding_ready=0 only

    high_priority (new archives): jump the queue and preempt geocode/embed (and
    mid-index EXIF) so hash-linking the new archive runs next.

    A list/scan/index with source_id=None expands into one job per enabled archive.
    """
    valid = ("list", "scan", "index", "embed")
    job_mode = mode if mode in valid else "scan"
    # Expand all-archive requests into per-source jobs so each folder
    # reports its own 0..N progress instead of one shared counter.
    if job_mode in ("list", "scan", "index") and source_id is None:
        start_jobs_all_enabled(
            mode=job_mode, reembed=reembed, high_priority=high_priority
        )
        return
    job = {
        "batch_size": batch_size,
        "reembed": reembed,
        "source_id": source_id,
        "mode": job_mode,
    }
    with _lock:
        if _state["running"]:
            _enqueue_index_job_unlocked(job, high_priority=high_priority)
            return
        _state["running"] = True
        _state["phase"] = "starting"
        _state["current"] = 0
        _state["total"] = 0
        _state["message"] = "Starting…"
        _state["error"] = None
        _state["reembed"] = reembed
        _state["source_id"] = source_id
        if job_mode == "embed":
            _state["embed_suspended"] = False

    t = threading.Thread(
        target=scan_and_embed,
        kwargs={
            "batch_size": batch_size,
            "reembed": reembed,
            "source_id": source_id,
            "mode": job_mode,
        },
        daemon=True,
    )
    t.start()


def suspend_embedding(source_id: int | None = None) -> dict[str, Any]:
    """Pause embedding for this archive, or remove its embed job from the queue.

    Listing/indexing are never paused by this — they stay mandatory and prioritized.
    When source_id is set, only suspend the active embed if it matches that archive;
    otherwise dequeue a waiting embed job for that archive.
    """
    global _suspend_requested
    with _lock:
        if source_id is not None and _dequeue_embed_job_unlocked(int(source_id)):
            return get_index_status()
        cur_sid = _state.get("source_id")
        phase = _state["phase"]
        running = _state["running"]
        if (
            source_id is not None
            and cur_sid is not None
            and int(cur_sid) != int(source_id)
        ):
            # Another archive is active — only queue removal applies (already tried).
            return get_index_status()
        # Only suspend embedding; never cancel list/scan/index/geocode/grayscale.
        if running and phase not in ("embed", "suspended", "idle", "done", "error"):
            if phase in ("list", "index", "scan", "geocode", "grayscale", "starting"):
                return get_index_status()
        _suspend_requested = True
        _state["embed_suspended"] = True
        if source_id is not None:
            _state["source_id"] = source_id
        # While embedding is in flight, just set the flag; the worker stops soon.
        if running and phase == "embed":
            return get_index_status()
        pending = count_pending_embeds(source_id)
        if pending:
            _state["phase"] = "suspended"
            _state["message"] = f"Paused — {pending} photos waiting to embed."
            _state["pending_embed"] = pending
            snap = dict(_state)
            if not running:
                save_index_job(
                    running=False,
                    phase="suspended",
                    current=int(snap["current"] or 0),
                    total=int(snap["total"] or 0),
                    message=str(snap["message"]),
                    error=None,
                    reembed=bool(snap.get("reembed")),
                    source_id=snap.get("source_id"),
                )
        return get_index_status()


def resume_embedding(source_id: int | None = None) -> dict[str, Any]:
    """Clear suspend and start embedding pending photos (Go)."""
    global _suspend_requested, _paused_for_faces
    # Face detection still owns the CPU if it is running (including paused).
    try:
        from fotoman import faces as faces_mod

        fst = faces_mod.get_status()
        if fst.get("running"):
            with _lock:
                return dict(_state)
    except Exception:
        pass
    with _lock:
        _suspend_requested = False
        _state["embed_suspended"] = False
        _paused_for_faces = False
        if _state["running"]:
            # A list/index may be in progress; embedding can be queued after.
            pass

    pending = count_pending_embeds(source_id)
    if pending > 0:
        start_index_background(source_id=source_id, mode="embed", high_priority=False)
    return get_index_status()


def yield_for_faces(timeout: float = 90.0) -> bool:
    """Pause heavy indexer work so face detection can use the CPU/RAM.

    Returns True if this call paused work that should be resumed via
    resume_after_faces() when detection finishes.
    """
    global _paused_for_faces
    with _lock:
        already_suspended = bool(_state.get("embed_suspended")) or _suspend_requested
        running = bool(_state["running"])
        phase = str(_state.get("phase") or "")
        heavy = running and phase in ("embed", "index", "geocode", "grayscale")

    if already_suspended:
        # Embed already paused — still stop in-flight index/geocode for faces,
        # but do not claim ownership of the embed resume.
        prev = False
        with _lock:
            prev = _paused_for_faces
            _paused_for_faces = True
        try:
            _wait_indexer_idle_of_heavy(timeout)
        finally:
            with _lock:
                _paused_for_faces = prev
        return False

    if not heavy:
        with _lock:
            _paused_for_faces = False
        return False

    with _lock:
        _paused_for_faces = True
    suspend_embedding()
    _wait_indexer_idle_of_heavy(timeout)
    return True


def _wait_indexer_idle_of_heavy(timeout: float) -> None:
    """Block until indexer leaves embed/index/geocode/grayscale (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _lock:
            running = bool(_state["running"])
            phase = str(_state.get("phase") or "")
        if not running or phase not in ("embed", "index", "geocode", "grayscale"):
            return
        time.sleep(0.25)


def resume_after_faces() -> dict[str, Any] | None:
    """Resume work if yield_for_faces() paused it for face detection."""
    global _paused_for_faces
    with _lock:
        should = _paused_for_faces
        _paused_for_faces = False
        source_id = _state.get("source_id")
    if not should:
        return None
    # Finish any interrupted index first; embedding stays opt-in via resume.
    if source_id is not None:
        start_index_background(source_id=int(source_id), mode="scan", high_priority=False)
    return resume_embedding(source_id=int(source_id) if source_id is not None else None)


def resume_if_interrupted() -> bool:
    """If a previous process died mid-index, restart indexing. Returns True if resumed."""
    job = get_index_job()
    if not job.get("running"):
        load_persisted_status()
        return False
    reembed = bool(job.get("reembed"))
    source_id = job.get("source_id")
    phase = job.get("phase") or "scan"
    if phase == "embed":
        mode = "embed"
    elif phase == "list":
        mode = "list"
    elif phase in ("geocode", "grayscale"):
        # Remaining places/fingerprints are picked up via durable NULL columns;
        # use scan so post-index backfill always runs after list.
        mode = "scan"
    else:
        # index / scan / starting — re-list and index remaining deltas
        mode = "scan"
    with _lock:
        _state.update(
            {
                "running": False,
                "phase": phase,
                "current": int(job.get("current") or 0),
                "total": int(job.get("total") or 0),
                "message": "Resuming interrupted index…",
                "error": None,
                "reembed": reembed,
                "source_id": source_id,
                "embed_suspended": False,
            }
        )
    start_index_background(reembed=reembed, source_id=source_id, mode=mode)
    return True


def unscanned_source_ids() -> list[int]:
    """Enabled archives that have never completed a first-round scan."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, path FROM sources
            WHERE enabled = 1 AND scanned_at IS NULL
            ORDER BY id
            """
        ).fetchall()
    # Skip missing mounts so we don't busy-loop empty scan jobs.
    return [int(r["id"]) for r in rows if Path(r["path"]).exists()]



def ensure_first_round_scans() -> list[int]:
    """Queue first-round list+index for unscanned archives (above any embedding work)."""
    ids = unscanned_source_ids()
    if not ids:
        return []

    active_phases = ("starting", "list", "index", "scan", "geocode", "grayscale")
    with _lock:
        running = _state["running"]
        current_sid = _state["source_id"] if running else None
        phase = _state["phase"] if running else None
        # A full (all-sources) job already covers unscanned archives.
        if running and current_sid is None and phase in active_phases:
            return []
        pending_scan_ids = {
            j.get("source_id")
            for j in _pending
            if (j.get("mode") or "scan") in ("list", "scan", "index")
        }
        scanning_now = (
            current_sid if running and phase in active_phases else None
        )

    queued: list[int] = []
    for sid in ids:
        if sid == scanning_now or sid in pending_scan_ids:
            continue
        start_index_background(source_id=sid, mode="scan", high_priority=True)
        queued.append(sid)
        pending_scan_ids.add(sid)
    return queued
