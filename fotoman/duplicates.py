from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from send2trash import send2trash

from fotoman.config import load_settings
from fotoman.db import PHOTO_SELECT, content_thumb_path, db, gc_orphaned_content
from fotoman.index_store import get_store
from fotoman.metadata import assess_quality_file


def _trash_file(path: Path) -> bool:
    """Move a file to the OS trash. Returns True if trashed or already gone."""
    if not path.exists():
        return True
    try:
        send2trash(str(path))
        return True
    except OSError:
        return False

# Retakes/reframes often land ~0.85–0.90 in SigLIP space. Soften only on
# looser slider levels (Similar and below) and only within a short window.
_RETAKE_WINDOW_S = 90.0
_RETAKE_THR_FLOOR = 0.85
_RETAKE_THR_RELAX = 0.08
_RETAKE_THR_MAX = 0.92  # do not soften Burst/Same

# Bake edges at the loosest slider floor (Familiar / retake floor) so every
# level can filter in realtime without re-running FAISS.
CLUSTER_EDGE_FLOOR = _RETAKE_THR_FLOOR
CLUSTER_EDGE_K = 32
CLUSTER_EDGE_BATCH = 256


def pending_near_edge_count() -> int:
    """Embedded contents that still need near-edge baking."""
    with db() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) AS c FROM photo_contents
                WHERE embedding_ready = 1 AND near_edges_ready = 0
                """
            ).fetchone()["c"]
        )


def rebuild_near_edges(
    *,
    on_progress: Any | None = None,
    should_stop: Any | None = None,
) -> dict[str, Any]:
    """Bake loosest near-dup edges for contents with near_edges_ready=0.

    Stores undirected edges (content_a < content_b) with cosine/IP score at or
    above CLUSTER_EDGE_FLOOR. Safe to interrupt: unfinished contents stay dirty.
    """
    store = get_store()
    ids, vecs = store.all_vectors()
    if len(ids) < 2:
        with db() as conn:
            conn.execute(
                """
                UPDATE photo_contents SET near_edges_ready = 1
                WHERE embedding_ready = 1 AND near_edges_ready = 0
                """
            )
        return {"updated": 0, "edges": 0, "pending": 0}

    id_to_pos = {cid: i for i, cid in enumerate(ids)}
    with db() as conn:
        valid_ids = {
            int(r["id"]) for r in conn.execute("SELECT id FROM photo_contents").fetchall()
        }
        dirty = [
            int(r["id"])
            for r in conn.execute(
                """
                SELECT id FROM photo_contents
                WHERE embedding_ready = 1 AND near_edges_ready = 0
                ORDER BY id
                """
            ).fetchall()
            if int(r["id"]) in id_to_pos
        ]
        # Embedded flag but missing from FAISS — nothing to link; mark done.
        stale = [
            int(r["id"])
            for r in conn.execute(
                """
                SELECT id FROM photo_contents
                WHERE embedding_ready = 1 AND near_edges_ready = 0
                """
            ).fetchall()
            if int(r["id"]) not in id_to_pos
        ]
        if stale:
            conn.executemany(
                "UPDATE photo_contents SET near_edges_ready = 1 WHERE id = ?",
                [(i,) for i in stale],
            )

    if not dirty:
        return {"updated": 0, "edges": 0, "pending": 0}

    mat = np.ascontiguousarray(vecs)
    index = faiss.IndexFlatIP(mat.shape[1])
    index.add(mat)
    k = min(CLUSTER_EDGE_K, len(ids))

    updated = 0
    edges_n = 0
    total = len(dirty)
    if on_progress:
        on_progress(0, total)

    for start in range(0, len(dirty), CLUSTER_EDGE_BATCH):
        if should_stop and should_stop():
            break
        batch = dirty[start : start + CLUSTER_EDGE_BATCH]
        pos = np.array([id_to_pos[cid] for cid in batch], dtype=np.int64)
        scores, neighbors = index.search(mat[pos], k)

        edge_best: dict[tuple[int, int], float] = {}
        for row_i, cid in enumerate(batch):
            for score, j in zip(scores[row_i], neighbors[row_i]):
                if j < 0:
                    continue
                other = int(ids[int(j)])
                if other == cid or other not in valid_ids:
                    continue
                sc = float(score)
                if sc < CLUSTER_EDGE_FLOOR:
                    continue
                a, b = (cid, other) if cid < other else (other, cid)
                prev = edge_best.get((a, b))
                if prev is None or sc > prev:
                    edge_best[(a, b)] = sc

        with db() as conn:
            ph = ",".join("?" * len(batch))
            conn.execute(
                f"DELETE FROM content_near_edges WHERE content_a IN ({ph}) OR content_b IN ({ph})",
                tuple(batch) + tuple(batch),
            )
            if edge_best:
                conn.executemany(
                    """
                    INSERT INTO content_near_edges(content_a, content_b, score)
                    VALUES (?, ?, ?)
                    ON CONFLICT(content_a, content_b) DO UPDATE SET score = excluded.score
                    """,
                    [(a, b, sc) for (a, b), sc in edge_best.items()],
                )
            conn.executemany(
                "UPDATE photo_contents SET near_edges_ready = 1 WHERE id = ?",
                [(cid,) for cid in batch],
            )
        updated += len(batch)
        edges_n += len(edge_best)
        if on_progress:
            on_progress(min(updated, total), total)

    pending = pending_near_edge_count()
    return {"updated": updated, "edges": edges_n, "pending": pending}


def _pixel_area(row: dict[str, Any]) -> int:
    w = row.get("width") or 0
    h = row.get("height") or 0
    return int(w) * int(h)


def _ensure_quality_scores(photos: list[dict[str, Any]]) -> None:
    """Score contents missing quality so keeper pick can use them (thumbs preferred)."""
    need: dict[int, dict[str, Any]] = {}
    for p in photos:
        if p.get("quality") is None:
            need.setdefault(int(p["content_id"]), p)
    if not need:
        return
    updates: list[tuple[float, float, float, int]] = []
    scored: dict[int, dict[str, float]] = {}
    for cid, sample in need.items():
        thumb = content_thumb_path(cid)
        src = thumb if thumb.exists() else Path(sample["path"])
        q = assess_quality_file(src) if src.exists() else None
        if not q:
            continue
        scored[cid] = q
        updates.append((q["sharpness"], q["exposure"], q["quality"], cid))
    if not updates:
        return
    with db() as conn:
        conn.executemany(
            """
            UPDATE photo_contents
            SET sharpness = ?, exposure = ?, quality = ?
            WHERE id = ? AND quality IS NULL
            """,
            updates,
        )
    for p in photos:
        q = scored.get(int(p["content_id"]))
        if q:
            p["sharpness"] = q["sharpness"]
            p["exposure"] = q["exposure"]
            p["quality"] = q["quality"]


def _parse_taken_at(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def _pair_threshold(thr: float, ta: str | None, tb: str | None) -> float:
    """Lower the bar slightly for near-in-time retakes that embeddings underscored."""
    if thr > _RETAKE_THR_MAX:
        return thr
    da, db_ = _parse_taken_at(ta), _parse_taken_at(tb)
    if da is None or db_ is None:
        return thr
    if abs((da - db_).total_seconds()) > _RETAKE_WINDOW_S:
        return thr
    return max(_RETAKE_THR_FLOOR, thr - _RETAKE_THR_RELAX)


def _pick_keeper(members: list[dict[str, Any]]) -> dict[str, Any]:
    # Prefer live (non-culled) photos; among those, higher visual quality, then resolution.
    return max(
        members,
        key=lambda r: (
            0 if r.get("culled") else 1,
            float(r.get("quality") or 0.0),
            _pixel_area(r),
            r.get("size") or 0,
            -(r["id"]),
        ),
    )


def _live_clause(include_culled: bool) -> str:
    return "p.missing = 0" if include_culled else "p.culled = 0 AND p.missing = 0"


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
        live_n = sum(1 for m in members if not m.get("culled"))
        cur = conn.execute(
            "INSERT INTO photo_groups(kind, keeper_id, member_count) VALUES (?, ?, ?)",
            (kind, keeper["id"], live_n or len(members)),
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
    photo_ids: list[int] | None = None,
    include_culled: bool = False,
) -> dict[str, Any]:
    settings = load_settings()
    thr = float(threshold if threshold is not None else settings.get("near_dup_threshold", 0.92))
    if not (exact or formatted or near):
        exact = True

    scope = {int(i) for i in photo_ids} if photo_ids is not None else None
    if scope is not None and not scope:
        return {
            "groups": 0,
            "exact_groups": 0,
            "formatted_groups": 0,
            "near_groups": 0,
            "replicas": 0,
            "threshold": thr,
            "modes": {"exact": exact, "formatted": formatted, "near": near},
            "scoped": 0,
            "include_culled": include_culled,
        }

    live = _live_clause(include_culled)
    with db() as conn:
        if scope is None:
            photos = [
                dict(r)
                for r in conn.execute(f"{PHOTO_SELECT} WHERE {live}").fetchall()
            ]
            if include_culled:
                conn.execute("UPDATE photos SET group_id = NULL, is_keeper = 1 WHERE missing = 0")
            else:
                conn.execute(
                    "UPDATE photos SET group_id = NULL, is_keeper = 1 WHERE culled = 0 AND missing = 0"
                )
            conn.execute("DELETE FROM photo_groups")
        else:
            placeholders = ",".join("?" * len(scope))
            photos = [
                dict(r)
                for r in conn.execute(
                    f"{PHOTO_SELECT} WHERE {live} AND p.id IN ({placeholders})",
                    tuple(scope),
                ).fetchall()
            ]
            # When the visible set is removed-only, also pull live actives from the
            # same archives so removed photos can attach to kept alternatives.
            if include_culled and photos and not any(not p.get("culled") for p in photos):
                source_ids = {int(p["source_id"]) for p in photos}
                have = {int(p["id"]) for p in photos}
                src_ph = ",".join("?" * len(source_ids))
                extras = [
                    dict(r)
                    for r in conn.execute(
                        f"""
                        {PHOTO_SELECT}
                        WHERE p.culled = 0 AND p.missing = 0
                          AND p.source_id IN ({src_ph})
                        """,
                        tuple(source_ids),
                    ).fetchall()
                    if int(r["id"]) not in have
                ]
                photos.extend(extras)
            # Drop scoped photos from existing groups; leave other archives alone.
            clear_ids = list(scope) if include_culled else [p["id"] for p in photos]
            if clear_ids:
                conn.executemany(
                    "UPDATE photos SET group_id = NULL, is_keeper = 1 WHERE id = ?",
                    [(i,) for i in clear_ids],
                )
                _refresh_groups(conn)

    _ensure_quality_scores(photos)

    by_content: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for p in photos:
        by_content[int(p["content_id"])].append(p)
    # Prefer live members as content representatives for near-dup date/threshold checks.
    for members in by_content.values():
        members.sort(key=lambda p: (1 if p.get("culled") else 0, p["id"]))
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
        active_ids = [
            cid
            for cid, members in by_content.items()
            if members
            and members[0].get("embedding_ready")
            and not any(p["id"] in grouped for p in members)
        ]
        if len(active_ids) >= 2:
            id_to_i = {cid: i for i, cid in enumerate(active_ids)}
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

            def _consider(cid_a: int, cid_b: int, score: float) -> None:
                ia, ib = id_to_i.get(cid_a), id_to_i.get(cid_b)
                if ia is None or ib is None or ia == ib:
                    return
                a = by_content[cid_a][0]
                b = by_content[cid_b][0]
                ta, tb = a.get("taken_at"), b.get("taken_at")
                pair_thr = _pair_threshold(thr, ta, tb)
                if score < pair_thr:
                    return
                if ta and tb and ta[:10] != tb[:10] and score < min(0.98, thr + 0.04):
                    return
                union(ia, ib)

            used_cache = False
            with db() as conn:
                # Prefer baked loosest graph; filter by slider threshold in realtime.
                ready_n = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS c FROM photo_contents
                        WHERE id IN ({}) AND near_edges_ready = 1
                        """.format(",".join("?" * len(active_ids))),
                        tuple(active_ids),
                    ).fetchone()["c"]
                )
                if ready_n == len(active_ids):
                    ph = ",".join("?" * len(active_ids))
                    edges = conn.execute(
                        f"""
                        SELECT content_a, content_b, score
                        FROM content_near_edges
                        WHERE content_a IN ({ph}) AND content_b IN ({ph})
                          AND score >= ?
                        """,
                        tuple(active_ids) + tuple(active_ids) + (CLUSTER_EDGE_FLOOR,),
                    ).fetchall()
                    used_cache = True
                    for r in edges:
                        _consider(int(r["content_a"]), int(r["content_b"]), float(r["score"]))

            if not used_cache:
                # Clustering phase not caught up yet — live FAISS fallback.
                store = get_store()
                ids, vecs = store.all_vectors()
                id_to_pos = {cid: i for i, cid in enumerate(ids)}
                search_ids = [cid for cid in active_ids if cid in id_to_pos]
                if len(search_ids) >= 2:
                    pos = np.array([id_to_pos[cid] for cid in search_ids], dtype=np.int64)
                    mat = np.ascontiguousarray(vecs[pos])
                    index = faiss.IndexFlatIP(mat.shape[1])
                    index.add(mat)
                    k = min(CLUSTER_EDGE_K, len(search_ids))
                    scores, neighbors = index.search(mat, k)
                    for i, cid_a in enumerate(search_ids):
                        for score, j in zip(scores[i], neighbors[i]):
                            if j < 0 or j <= i:
                                continue
                            _consider(cid_a, search_ids[int(j)], float(score))

            clusters_ids: dict[int, list[int]] = defaultdict(list)
            for i, cid in enumerate(active_ids):
                clusters_ids[find(i)].append(cid)

            clusters = [
                [p for cid in member_ids for p in by_content[cid]]
                for member_ids in clusters_ids.values()
                if len(member_ids) >= 2
            ]
            with db() as conn:
                near_n = _insert_groups(conn, clusters, "near", grouped)
                created += near_n

    with db() as conn:
        if scope is None:
            replicas = conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE is_keeper = 0 AND culled = 0 AND missing = 0"
            ).fetchone()["c"]
        else:
            placeholders = ",".join("?" * len(scope))
            replicas = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM photos
                WHERE is_keeper = 0 AND culled = 0 AND missing = 0 AND id IN ({placeholders})
                """,
                tuple(scope),
            ).fetchone()["c"]
    out = {
        "groups": created,
        "exact_groups": exact_n,
        "formatted_groups": formatted_n,
        "near_groups": near_n,
        "replicas": replicas,
        "threshold": thr,
        "modes": {"exact": exact, "formatted": formatted, "near": near},
        "include_culled": include_culled,
    }
    if scope is not None:
        out["scoped"] = len(scope)
    return out


def _refresh_groups(conn) -> None:
    conn.execute(
        """
        UPDATE photo_groups SET member_count = (
            SELECT COUNT(*) FROM photos WHERE group_id = photo_groups.id AND culled = 0 AND missing = 0
        )
        """
    )
    # Prefer a live non-culled keeper; fall back to best remaining resolution
    for g in conn.execute("SELECT id, keeper_id FROM photo_groups").fetchall():
        gid = g["id"]
        keeper = conn.execute(
            "SELECT id FROM photos WHERE id = ? AND group_id = ? AND culled = 0 AND missing = 0",
            (g["keeper_id"], gid),
        ).fetchone()
        if keeper:
            continue
        members = [
            dict(r)
            for r in conn.execute(
                f"{PHOTO_SELECT} WHERE p.group_id = ? AND p.culled = 0 AND p.missing = 0", (gid,)
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
    # Keeps removed members attached so they can be restored from the Cluster tab.
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
                where = "p.group_id = ? AND p.culled = 0 AND p.missing = 0"
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
    """Move photos to the removed (culled=1). Optionally send files to the OS trash."""
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
            if delete_files and _trash_file(Path(r["path"])):
                deleted += 1
            removed += 1
        if ids:
            conn.executemany("UPDATE photos SET culled = 1 WHERE id = ?", [(i,) for i in ids])
            _refresh_groups(conn)

    # Soft remove keeps content embeddings so restore / shared copies still work.
    return {"removed": removed, "deleted_files": deleted}


def restore_photos(photo_ids: list[int]) -> dict[str, Any]:
    """Restore photos from the removed."""
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


def delete_photos(photo_ids: list[int]) -> dict[str, Any]:
    """Trash culled photo files and drop them from the index."""
    if not photo_ids:
        return {"deleted": 0, "trashed": 0, "failed": 0}
    with db() as conn:
        placeholders = ",".join("?" * len(photo_ids))
        rows = conn.execute(
            f"SELECT id, path, content_id FROM photos WHERE id IN ({placeholders}) AND culled = 1",
            photo_ids,
        ).fetchall()
        if not rows:
            return {"deleted": 0, "trashed": 0, "failed": 0}

        paths = sorted({r["path"] for r in rows})
        ok_paths: list[str] = []
        trashed = 0
        failed = 0
        for path in paths:
            if _trash_file(Path(path)):
                trashed += 1
                ok_paths.append(path)
            else:
                failed += 1

        if not ok_paths:
            return {"deleted": 0, "trashed": 0, "failed": failed}

        ph = ",".join("?" * len(ok_paths))
        doomed = conn.execute(
            f"SELECT id, content_id FROM photos WHERE path IN ({ph})", ok_paths
        ).fetchall()
        purge_ids = [int(r["id"]) for r in doomed]
        content_ids = {int(r["content_id"]) for r in doomed}
        conn.execute(f"DELETE FROM photos WHERE id IN ({','.join('?' * len(purge_ids))})", purge_ids)
        for cid in content_ids:
            gc_orphaned_content(conn, cid)
        _refresh_groups(conn)

    return {"deleted": len(purge_ids), "trashed": trashed, "failed": failed}


def cull_replicas(group_ids: list[int] | None = None, delete_files: bool = False) -> dict[str, Any]:
    """Remove all non-keeper replicas (optionally delete files)."""
    with db() as conn:
        if group_ids:
            rows = conn.execute(
                f"""
                SELECT id FROM photos
                WHERE culled = 0 AND missing = 0 AND is_keeper = 0 AND group_id IN ({",".join("?" * len(group_ids))})
                """,
                group_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM photos WHERE culled = 0 AND missing = 0 AND is_keeper = 0 AND group_id IS NOT NULL"
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
