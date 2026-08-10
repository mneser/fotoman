"""User-created virtual folders keyed by photo content (hash vault)."""

from __future__ import annotations

from typing import Any

from fotoman.db import db


class NameConflict(Exception):
    def __init__(self, existing: dict[str, Any]):
        self.existing = existing
        super().__init__("Virtual folder name already exists")


def _culled_sql(include_culled: bool, alias: str = "p") -> str:
    col = f"{alias}.culled"
    miss = f"{alias}.missing"
    live = f"{miss} = 0"
    if include_culled:
        return live
    return f"{col} = 0 AND {live}"


def _normalize_name(name: str) -> str:
    return (name or "").strip()


def _content_ids_for_photos(conn, photo_ids: list[int]) -> list[int]:
    if not photo_ids:
        return []
    placeholders = ",".join("?" * len(photo_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT content_id
        FROM photos
        WHERE id IN ({placeholders}) AND content_id IS NOT NULL
        """,
        [int(i) for i in photo_ids],
    ).fetchall()
    return [int(r["content_id"]) for r in rows]


def list_folders(include_culled: bool = False) -> list[dict[str, Any]]:
    culled = _culled_sql(include_culled, "p")
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT
              vf.id,
              vf.name,
              COALESCE((
                SELECT COUNT(DISTINCT m.content_id)
                FROM virtual_folder_members m
                JOIN photos p ON p.content_id = m.content_id
                WHERE m.folder_id = vf.id AND {culled}
              ), 0) AS count
            FROM virtual_folders vf
            ORDER BY lower(vf.name), vf.id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_folder(folder_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT id, name, created_at FROM virtual_folders WHERE id = ?",
            (int(folder_id),),
        ).fetchone()
    return dict(row) if row else None


def create_folder(name: str) -> dict[str, Any]:
    label = _normalize_name(name)
    if not label:
        raise ValueError("Folder name is required")
    with db() as conn:
        existing = conn.execute(
            """
            SELECT id, name FROM virtual_folders
            WHERE lower(trim(name)) = lower(?)
            """,
            (label,),
        ).fetchone()
        if existing:
            raise NameConflict(dict(existing))
        cur = conn.execute(
            "INSERT INTO virtual_folders(name) VALUES (?)",
            (label,),
        )
        folder_id = int(cur.lastrowid)
        row = conn.execute(
            "SELECT id, name, created_at FROM virtual_folders WHERE id = ?",
            (folder_id,),
        ).fetchone()
    out = dict(row)
    out["count"] = 0
    return out


def rename_folder(folder_id: int, name: str) -> dict[str, Any]:
    label = _normalize_name(name)
    if not label:
        raise ValueError("Folder name is required")
    with db() as conn:
        row = conn.execute(
            "SELECT id, name FROM virtual_folders WHERE id = ?",
            (int(folder_id),),
        ).fetchone()
        if not row:
            raise KeyError("not found")
        conflict = conn.execute(
            """
            SELECT id, name FROM virtual_folders
            WHERE lower(trim(name)) = lower(?) AND id != ?
            """,
            (label, int(folder_id)),
        ).fetchone()
        if conflict:
            raise NameConflict(dict(conflict))
        conn.execute(
            "UPDATE virtual_folders SET name = ? WHERE id = ?",
            (label, int(folder_id)),
        )
        updated = conn.execute(
            "SELECT id, name, created_at FROM virtual_folders WHERE id = ?",
            (int(folder_id),),
        ).fetchone()
    return dict(updated)


def delete_folder(folder_id: int) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute(
            "SELECT id, name FROM virtual_folders WHERE id = ?",
            (int(folder_id),),
        ).fetchone()
        if not row:
            raise KeyError("not found")
        conn.execute("DELETE FROM virtual_folders WHERE id = ?", (int(folder_id),))
    return {"deleted": True, "id": int(folder_id), "name": row["name"]}


def add_members(folder_id: int, photo_ids: list[int]) -> dict[str, Any]:
    with db() as conn:
        folder = conn.execute(
            "SELECT id FROM virtual_folders WHERE id = ?",
            (int(folder_id),),
        ).fetchone()
        if not folder:
            raise KeyError("not found")
        content_ids = _content_ids_for_photos(conn, photo_ids)
        added = 0
        for cid in content_ids:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO virtual_folder_members(folder_id, content_id)
                VALUES (?, ?)
                """,
                (int(folder_id), cid),
            )
            added += cur.rowcount
    return {"folder_id": int(folder_id), "added": added, "requested": len(photo_ids)}


def remove_members(folder_id: int, photo_ids: list[int]) -> dict[str, Any]:
    with db() as conn:
        folder = conn.execute(
            "SELECT id FROM virtual_folders WHERE id = ?",
            (int(folder_id),),
        ).fetchone()
        if not folder:
            raise KeyError("not found")
        content_ids = _content_ids_for_photos(conn, photo_ids)
        removed = 0
        if content_ids:
            placeholders = ",".join("?" * len(content_ids))
            cur = conn.execute(
                f"""
                DELETE FROM virtual_folder_members
                WHERE folder_id = ? AND content_id IN ({placeholders})
                """,
                [int(folder_id), *content_ids],
            )
            removed = cur.rowcount
    return {
        "folder_id": int(folder_id),
        "removed": removed,
        "requested": len(photo_ids),
    }


def photos_for_folder(
    folder_id: int,
    limit: int = 200,
    include_culled: bool = False,
) -> list[dict[str, Any]]:
    culled = _culled_sql(include_culled, "p")
    with db() as conn:
        folder = conn.execute(
            "SELECT id FROM virtual_folders WHERE id = ?",
            (int(folder_id),),
        ).fetchone()
        if not folder:
            return []
        rows = conn.execute(
            f"""
            WITH members AS (
              SELECT content_id FROM virtual_folder_members WHERE folder_id = ?
            ),
            keepers AS (
              SELECT p.content_id, MIN(p.id) AS photo_id
              FROM photos p
              JOIN members m ON m.content_id = p.content_id
              WHERE {culled}
              GROUP BY p.content_id
            )
            SELECT
              p.id, p.source_id, p.path, p.rel_path, p.mtime, p.album,
              p.culled, p.missing, p.group_id, p.is_keeper, p.content_id,
              s.label AS source_label,
              c.file_hash, c.size, c.width, c.height, c.taken_at, c.camera,
              c.lat, c.lon, c.gps, c.venue, c.city, c.region, c.country, c.place_label,
              c.orientation, c.mirrored, c.fingerprint, c.embedding_ready, c.faces_ready, c.indexed_at
            FROM keepers k
            JOIN photos p ON p.id = k.photo_id
            JOIN photo_contents c ON c.id = p.content_id
            JOIN sources s ON s.id = p.source_id
            ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
            LIMIT ?
            """,
            (int(folder_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]
