from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from fotoman.config import load_settings
from fotoman.db import PHOTO_SELECT, db
from fotoman.index_store import get_store


def _pixel_area(row: dict[str, Any]) -> int:
    w = row.get("width") or 0
    h = row.get("height") or 0
    return int(w) * int(h)


def _pick_keeper(members: list[dict[str, Any]]) -> dict[str, Any]:
    return max(members, key=lambda r: (_pixel_area(r), r.get("size") or 0, -(r["id"])))


def _insert_groups(
    conn,
    clusters: list[list[dict[str, Any]]],
    kind: str,
    grouped: set[int],
) -> int:
    created = 0
    for members in clusters:
        if len(members) < 2:
            continue
        keeper = _pick_keeper(members)
        cur = conn.execute(
            "INSERT INTO photo_groups(kind, keeper_id, member_count) VALUES (?, ?, ?)",
            (kind, keeper["id"], len(members)),
        )
        gid = cur.lastrowid
        created += 1
        for m in members:
            grouped.add(m["id"])
            conn.execute(
                "UPDATE photos SET group_id = ?, is_keeper = ? WHERE id = ?",
                (gid, 1 if m["id"] == keeper["id"] else 0, m["id"]),
            )
    return created


def rebuild_groups(
    threshold: float | None = None,
    *,
    exact: bool = True,
    formatted: bool = False,
    near: bool = True,
) -> dict[str, Any]:
    settings = load_settings()
    thr = float(threshold if threshold is not None else settings.get("near_dup_threshold", 0.92))
    if not (exact or formatted or near):
        exact = True

    with db() as conn:
        photos = [
            dict(r)
            for r in conn.execute(
                f"{PHOTO_SELECT} WHERE p.culled = 0"
            ).fetchall()
        ]
        conn.execute("UPDATE photos SET group_id = NULL, is_keeper = 1 WHERE culled = 0")
        conn.execute("DELETE FROM photo_groups")

    by_id = {p["id"]: p for p in photos}
    by_content: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for p in photos:
        by_content[int(p["content_id"])].append(p)
    grouped: set[int] = set()
    created = 0
    exact_n = formatted_n = near_n = 0

    if exact:
        by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in photos:
            if p.get("file_hash"):
                by_hash[p["file_hash"]].append(p)
        clusters = [m for m in by_hash.values() if len(m) >= 2]
        with db() as conn:
            exact_n = _insert_groups(conn, clusters, "exact", grouped)
            created += exact_n

    if formatted:
        by_px: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in photos:
            if p["id"] in grouped:
                continue
            fp = p.get("fingerprint")
            if fp:
                by_px[fp].append(p)
        clusters = [m for m in by_px.values() if len(m) >= 2]
        with db() as conn:
            formatted_n = _insert_groups(conn, clusters, "formatted", grouped)
            created += formatted_n

    if near:
        store = get_store()
        ids, vecs = store.all_vectors()
        id_to_pos = {cid: i for i, cid in enumerate(ids)}
        active_ids = [
            cid
            for cid in ids
            if cid in by_content
            and by_content[cid]
            and by_content[cid][0].get("embedding_ready")
            and not any(p["id"] in grouped for p in by_content[cid])
        ]

        if len(active_ids) >= 2:
            pos = np.array([id_to_pos[cid] for cid in active_ids], dtype=np.int64)
            mat = np.ascontiguousarray(vecs[pos])
            index = faiss.IndexFlatIP(mat.shape[1])
            index.add(mat)

            parent = list(range(len(active_ids)))

            def find(a: int) -> int:
                while parent[a] != a:
                    parent[a] = parent[parent[a]]
                    a = parent[a]
                return a

            def union(a: int, b: int) -> None:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

            k = min(32, len(active_ids))
            scores, neighbors = index.search(mat, k)
            for i in range(len(active_ids)):
                a = by_content[active_ids[i]][0]
                for score, j in zip(scores[i], neighbors[i]):
                    if j < 0 or j <= i or score < thr:
                        continue
                    b = by_content[active_ids[j]][0]
                    ta, tb = a.get("taken_at"), b.get("taken_at")
                    if ta and tb and ta[:10] != tb[:10] and score < min(0.98, thr + 0.04):
                        continue
                    union(i, int(j))

            clusters_ids: dict[int, list[int]] = defaultdict(list)
            for i in range(len(active_ids)):
                clusters_ids[find(i)].append(active_ids[i])

            clusters = [
                [p for cid in member_ids for p in by_content[cid]]
                for member_ids in clusters_ids.values()
                if len(member_ids) >= 2
            ]
            with db() as conn:
                near_n = _insert_groups(conn, clusters, "near", grouped)
                created += near_n

    with db() as conn:
        replicas = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE is_keeper = 0 AND culled = 0"
        ).fetchone()["c"]
    return {
        "groups": created,
        "exact_groups": exact_n,
        "formatted_groups": formatted_n,
        "near_groups": near_n,
        "replicas": replicas,
        "threshold": thr,
        "modes": {"exact": exact, "formatted": formatted, "near": near},
    }


def _refresh_groups(conn) -> None:
    conn.execute(
        """
        UPDATE photo_groups SET member_count = (
            SELECT COUNT(*) FROM photos WHERE group_id = photo_groups.id AND culled = 0
        )
        """
    )
    # Prefer a live non-culled keeper; fall back to best remaining resolution
    for g in conn.execute("SELECT id, keeper_id FROM photo_groups").fetchall():
        gid = g["id"]
        keeper = conn.execute(
            "SELECT id FROM photos WHERE id = ? AND group_id = ? AND culled = 0",
            (g["keeper_id"], gid),
        ).fetchone()
        if keeper:
            continue
        members = [
            dict(r)
            for r in conn.execute(
                f"{PHOTO_SELECT} WHERE p.group_id = ? AND p.culled = 0", (gid,)
            ).fetchall()
        ]
        if not members:
            continue
        best = _pick_keeper(members)
        conn.execute("UPDATE photos SET is_keeper = 0 WHERE group_id = ?", (gid,))
        conn.execute("UPDATE photos SET is_keeper = 1 WHERE id = ?", (best["id"],))
        conn.execute(
            "UPDATE photo_groups SET keeper_id = ? WHERE id = ?", (best["id"], gid)
        )

    # Drop only when fewer than 2 photos remain linked (active + removed).
    # Keeps removed members attached so they can be restored from the Bundle tab.
    conn.execute(
        """
        DELETE FROM photo_groups WHERE (
            SELECT COUNT(*) FROM photos WHERE group_id = photo_groups.id
        ) < 2
        """
    )
    conn.execute(
        """
        UPDATE photos SET group_id = NULL, is_keeper = 1
        WHERE group_id IS NOT NULL
          AND group_id NOT IN (SELECT id FROM photo_groups)
        """
    )


def list_groups(kind: str | None = None, include_removed: bool = False) -> list[dict[str, Any]]:
    q = """
        SELECT g.*, k.path AS keeper_path, k.rel_path AS keeper_rel_path,
               c.width AS keeper_width, c.height AS keeper_height, c.size AS keeper_size
        FROM photo_groups g
        LEFT JOIN photos k ON k.id = g.keeper_id
        LEFT JOIN photo_contents c ON c.id = k.content_id
    """
    params: tuple = ()
    if kind:
        q += " WHERE g.kind = ?"
        params = (kind,)
    q += " ORDER BY g.member_count DESC, g.id DESC"
    with db() as conn:
        groups = [dict(r) for r in conn.execute(q, params).fetchall()]
        for g in groups:
            if include_removed:
                where = "p.group_id = ?"
            else:
                where = "p.group_id = ? AND p.culled = 0"
            members = [
                dict(r)
                for r in conn.execute(
                    f"""
                    {PHOTO_SELECT}
                    WHERE {where}
                    ORDER BY p.culled ASC,
                             (COALESCE(c.width,0)*COALESCE(c.height,0)) DESC,
                             c.size DESC,
                             p.is_keeper DESC
                    """,
                    (g["id"],),
                ).fetchall()
            ]
            g["members"] = members
            g["best_id"] = members[0]["id"] if members else g.get("keeper_id")
    return groups


def remove_photos(photo_ids: list[int], delete_files: bool = False) -> dict[str, Any]:
    """Move photos to the remove bin (culled=1). Optionally delete files from disk."""
    if not photo_ids:
        return {"removed": 0, "deleted_files": 0}
    removed = 0
    deleted = 0
    ids: list[int] = []
    with db() as conn:
        placeholders = ",".join("?" * len(photo_ids))
        rows = conn.execute(
            f"SELECT id, path FROM photos WHERE id IN ({placeholders}) AND culled = 0",
            photo_ids,
        ).fetchall()
        for r in rows:
            ids.append(r["id"])
            if delete_files:
                try:
                    p = Path(r["path"])
                    if p.exists():
                        p.unlink()
                        deleted += 1
                except OSError:
                    pass
            removed += 1
        if ids:
            conn.executemany("UPDATE photos SET culled = 1 WHERE id = ?", [(i,) for i in ids])
            _refresh_groups(conn)

    # Soft remove keeps content embeddings so restore / shared copies still work.
    return {"removed": removed, "deleted_files": deleted}


def restore_photos(photo_ids: list[int]) -> dict[str, Any]:
    """Restore photos from the remove bin."""
    if not photo_ids:
        return {"restored": 0}
    with db() as conn:
        placeholders = ",".join("?" * len(photo_ids))
        rows = conn.execute(
            f"SELECT id FROM photos WHERE id IN ({placeholders}) AND culled = 1",
            photo_ids,
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.executemany("UPDATE photos SET culled = 0 WHERE id = ?", [(i,) for i in ids])
            _refresh_groups(conn)
    return {"restored": len(ids)}


def cull_replicas(group_ids: list[int] | None = None, delete_files: bool = False) -> dict[str, Any]:
    """Remove all non-keeper replicas (optionally delete files)."""
    with db() as conn:
        if group_ids:
            rows = conn.execute(
                f"""
                SELECT id FROM photos
                WHERE culled = 0 AND is_keeper = 0 AND group_id IN ({",".join("?" * len(group_ids))})
                """,
                group_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM photos WHERE culled = 0 AND is_keeper = 0 AND group_id IS NOT NULL"
            ).fetchall()
        ids = [r["id"] for r in rows]
    result = remove_photos(ids, delete_files=delete_files)
    return {"culled": result["removed"], "deleted_files": result["deleted_files"]}


def set_keeper(photo_id: int) -> None:
    with db() as conn:
        row = conn.execute(
            "SELECT group_id, culled FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if not row or not row["group_id"]:
            raise ValueError("Photo is not in a duplicate group")
        if row["culled"]:
            conn.execute("UPDATE photos SET culled = 0 WHERE id = ?", (photo_id,))
        gid = row["group_id"]
        conn.execute("UPDATE photos SET is_keeper = 0 WHERE group_id = ?", (gid,))
        conn.execute("UPDATE photos SET is_keeper = 1 WHERE id = ?", (photo_id,))
        conn.execute("UPDATE photo_groups SET keeper_id = ? WHERE id = ?", (photo_id, gid))
        _refresh_groups(conn)
