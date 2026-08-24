from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from send2trash import send2trash

from fotoman.cluster_scene import (
    CLUSTER_SCENE_MATCH,
    cluster_scene_contents,
    scene_params_from_threshold,
)
from fotoman.config import load_settings
from fotoman.db import PHOTO_SELECT, content_thumb_path, db, gc_orphaned_content
from fotoman.index_store import get_store
from fotoman.metadata import assess_quality_file

CLUSTER_HASH_VECTOR = "HASH_VECTOR"
CLUSTER_METHODS = (CLUSTER_HASH_VECTOR, CLUSTER_SCENE_MATCH)


def _trash_file(path: Path) -> bool:
    """Move a file to the OS trash. Returns True if trashed or already gone."""
    if not path.exists():
        return True
    try:
        send2trash(str(path))
        return True
    except Exception:
        # send2trash can raise OSError or platform-specific errors.
        return False


def _resolved_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _source_roots(row: dict[str, Any]) -> list[Path]:
    out: list[Path] = []
    for key in ("source_path", "shadow_path"):
        p = _resolved_path(row.get(key))
        if p is not None:
            out.append(p)
    return out


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _dir_is_empty(path: Path) -> bool:
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return False


def _prune_empty_dirs(start: Path, roots: list[Path]) -> None:
    """Remove empty parent dirs up to (but not including) a source root."""
    if not roots:
        return
    try:
        cur = start.expanduser().resolve()
    except OSError:
        return
    while True:
        if not any(_is_under(cur, root) and cur != root for root in roots):
            break
        if not cur.is_dir() or not _dir_is_empty(cur):
            break
        parent = cur.parent
        try:
            cur.rmdir()
        except OSError:
            break
        cur = parent


def _fs_parent_for_prune(row: dict[str, Any]) -> Path | None:
    from fotoman import device

    path = str(row.get("path") or "")
    if device.is_device_path(path):
        named = device.named_shadow_file(
            device.pair_id_from_path(path), row.get("rel_path")
        )
        return named.parent if named else None
    try:
        return Path(path).parent
    except Exception:
        return None


def _prune_empty_parents(rows: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for r in rows:
        parent = _fs_parent_for_prune(r)
        if parent is None:
            continue
        key = str(parent)
        if key in seen:
            continue
        seen.add(key)
        _prune_empty_dirs(parent, _source_roots(r))


def _trash_indexed_file(row: dict[str, Any], *, drop_catalog: bool = False) -> bool:
    from fotoman import device

    path = str(row.get("path") or "")
    if device.is_device_path(path):
        ok = False
        for dest in device.local_copies(path, row.get("rel_path")):
            if _trash_file(dest):
                ok = True
        if drop_catalog:
            device.drop_catalog_asset(
                device.pair_id_from_path(path),
                device.asset_id_from_path(path),
            )
        return True
    return _trash_file(Path(path))


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


# log2(area): +1 ≈ 2× pixels. Quality is typically ~3–8; weight so ~0.5
# quality pts ≈ one doubling of resolution (small pixel gaps lose to sharpness).
_KEEPER_QUALITY_WEIGHT = 2.0


def _keeper_score(row: dict[str, Any]) -> float:
    area = max(_pixel_area(row), 1)
    quality = float(row.get("quality") or 0.0)
    return math.log2(area) + _KEEPER_QUALITY_WEIGHT * quality


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
    # Live first; then weighted resolution+quality (not lexicographic pixels).
    return max(
        members,
        key=lambda r: (
            0 if r.get("culled") else 1,
            _keeper_score(r),
            r.get("size") or 0,
            -(r["id"]),
        ),
    )


def _live_clause(include_culled: bool) -> str:
    return "p.missing = 0" if include_culled else "p.culled = 0 AND p.missing = 0"


def _sort_group_members(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        members,
        key=lambda m: (
            1 if m.get("culled") else 0,
            -_pixel_area(m),
            -(m.get("size") or 0),
            0 if m.get("is_keeper") else 1,
            -(m.get("id") or 0),
        ),
    )


def _materialize_group(
    kind: str,
    members: list[dict[str, Any]],
    *,
    group_id: int,
    scope: set[int] | None = None,
) -> dict[str, Any]:
    keeper = _pick_keeper(members)
    annotated: list[dict[str, Any]] = []
    for m in members:
        row = dict(m)
        row["group_id"] = group_id
        row["is_keeper"] = 1 if m["id"] == keeper["id"] else 0
        row["in_scope"] = True if scope is None else m["id"] in scope
        annotated.append(row)
    annotated = _sort_group_members(annotated)
    # Prefer current-tree members first so the selection is easy to spot.
    if scope is not None:
        annotated.sort(
            key=lambda m: (
                0 if m.get("in_scope") else 1,
                1 if m.get("culled") else 0,
                -_pixel_area(m),
                -(m.get("size") or 0),
                0 if m.get("is_keeper") else 1,
                -(m.get("id") or 0),
            )
        )
    live_n = sum(1 for m in annotated if not m.get("culled"))
    return {
        "id": group_id,
        "kind": kind,
        "keeper_id": keeper["id"],
        "member_count": live_n or len(annotated),
        "members": annotated,
        "best_id": annotated[0]["id"] if annotated else keeper["id"],
    }


def _insert_groups(
    conn,
    clusters: list[list[dict[str, Any]]],
    kind: str,
    grouped: set[int],
    *,
    scope: set[int] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    created = 0
    items: list[dict[str, Any]] = []
    for members in clusters:
        if len(members) < 2:
            continue
        keeper = _pick_keeper(members)
        live_n = sum(1 for m in members if not m.get("culled"))
        cur = conn.execute(
            "INSERT INTO photo_groups(kind, keeper_id, member_count) VALUES (?, ?, ?)",
            (kind, keeper["id"], live_n or len(members)),
        )
        gid = int(cur.lastrowid)
        created += 1
        for m in members:
            grouped.add(m["id"])
            conn.execute(
                "UPDATE photos SET group_id = ?, is_keeper = ? WHERE id = ?",
                (gid, 1 if m["id"] == keeper["id"] else 0, m["id"]),
            )
        items.append(_materialize_group(kind, members, group_id=gid, scope=scope))
    return created, items


def _load_cluster_photos(
    scope: set[int] | None,
    *,
    include_culled: bool,
) -> list[dict[str, Any]]:
    live = _live_clause(include_culled)
    with db() as conn:
        if scope is None:
            return [
                dict(r)
                for r in conn.execute(f"{PHOTO_SELECT} WHERE {live}").fetchall()
            ]
        placeholders = ",".join("?" * len(scope))
        return [
            dict(r)
            for r in conn.execute(
                f"{PHOTO_SELECT} WHERE {live} AND p.id IN ({placeholders})",
                tuple(scope),
            ).fetchall()
        ]


def _chunked(ids, size: int = 800) -> list:
    seq = list(ids)
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _near_partner_content_ids(seed_cids: set[int]) -> set[int]:
    """Content ids reachable from seeds via baked near edges (and FAISS if unbaked)."""
    if not seed_cids:
        return set()
    reachable = set(seed_cids)
    frontier = set(seed_cids)
    with db() as conn:
        while frontier:
            new: set[int] = set()
            for chunk in _chunked(frontier):
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"""
                    SELECT content_a, content_b
                    FROM content_near_edges
                    WHERE score >= ?
                      AND (content_a IN ({ph}) OR content_b IN ({ph}))
                    """,
                    (CLUSTER_EDGE_FLOOR, *chunk, *chunk),
                ).fetchall()
                for r in rows:
                    for cid in (int(r["content_a"]), int(r["content_b"])):
                        if cid not in reachable:
                            new.add(cid)
            if not new:
                break
            reachable |= new
            frontier = new

        unready = [
            int(r["id"])
            for chunk in _chunked(seed_cids)
            for r in conn.execute(
                f"""
                SELECT id FROM photo_contents
                WHERE id IN ({",".join("?" * len(chunk))})
                  AND embedding_ready = 1 AND near_edges_ready = 0
                """,
                tuple(chunk),
            ).fetchall()
        ]

    if unready:
        store = get_store()
        ids, vecs = store.all_vectors()
        id_to_pos = {cid: i for i, cid in enumerate(ids)}
        search = [cid for cid in unready if cid in id_to_pos]
        if len(ids) >= 2 and search:
            mat = np.ascontiguousarray(vecs)
            index = faiss.IndexFlatIP(mat.shape[1])
            index.add(mat)
            k = min(CLUSTER_EDGE_K, len(ids))
            pos = np.array([id_to_pos[cid] for cid in search], dtype=np.int64)
            scores, neighbors = index.search(mat[pos], k)
            for row_i in range(len(search)):
                for score, j in zip(scores[row_i], neighbors[row_i]):
                    if j < 0 or float(score) < CLUSTER_EDGE_FLOOR:
                        continue
                    other = int(ids[int(j)])
                    if other not in reachable:
                        reachable.add(other)

    return reachable


def _normalize_clustering(clustering: str | None) -> str:
    method = str(clustering or CLUSTER_HASH_VECTOR).strip().upper()
    if method not in CLUSTER_METHODS:
        return CLUSTER_HASH_VECTOR
    return method


def _expand_cluster_partners(
    seed_photos: list[dict[str, Any]],
    *,
    exact: bool,
    formatted: bool,
    near: bool,
    include_culled: bool,
    clustering: str = CLUSTER_HASH_VECTOR,
) -> list[dict[str, Any]]:
    """Add matching photos from all enabled archives for the current cluster modes."""
    if not seed_photos:
        return seed_photos
    live = _live_clause(include_culled)
    have = {int(p["id"]) for p in seed_photos}
    seed_cids = {int(p["content_id"]) for p in seed_photos}
    target_cids = set(seed_cids)
    # Same bytes across archives share content_id; always pull those replicas.
    if exact or formatted or near:
        target_cids |= seed_cids
    fingerprints = (
        {p["fingerprint"] for p in seed_photos if p.get("fingerprint")}
        if formatted
        else set()
    )
    # HASH_VECTOR walks the baked embedding graph (lookalikes in other cities).
    # SCENE_MATCH only adds exact / formatted replicas.
    if near and clustering == CLUSTER_HASH_VECTOR:
        target_cids |= _near_partner_content_ids(seed_cids)

    extras: list[dict[str, Any]] = []
    with db() as conn:
        for chunk in _chunked(target_cids):
            ph = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"""
                {PHOTO_SELECT}
                WHERE {live} AND s.enabled = 1 AND p.content_id IN ({ph})
                """,
                tuple(chunk),
            ).fetchall():
                pid = int(r["id"])
                if pid not in have:
                    have.add(pid)
                    extras.append(dict(r))
        if fingerprints:
            for chunk in _chunked(list(fingerprints), size=400):
                ph = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"""
                    {PHOTO_SELECT}
                    WHERE {live} AND s.enabled = 1 AND c.fingerprint IN ({ph})
                    """,
                    tuple(chunk),
                ).fetchall():
                    pid = int(r["id"])
                    if pid not in have:
                        have.add(pid)
                        extras.append(dict(r))

    if not extras:
        return seed_photos
    return seed_photos + extras


def _clear_groups_for_scope(
    scope: set[int] | None,
    photos: list[dict[str, Any]],
    *,
    include_culled: bool,
) -> None:
    with db() as conn:
        if scope is None:
            if include_culled:
                conn.execute("UPDATE photos SET group_id = NULL, is_keeper = 1 WHERE missing = 0")
            else:
                conn.execute(
                    "UPDATE photos SET group_id = NULL, is_keeper = 1 WHERE culled = 0 AND missing = 0"
                )
            conn.execute("DELETE FROM photo_groups")
            return
        # Only touch ids in scope (callers pass scope ∪ new cluster members).
        clear_ids = (
            list(scope)
            if include_culled
            else [p["id"] for p in photos if p["id"] in scope]
        )
        if clear_ids:
            conn.executemany(
                "UPDATE photos SET group_id = NULL, is_keeper = 1 WHERE id = ?",
                [(i,) for i in clear_ids],
            )
            _refresh_groups(conn)


def _find_clusters(
    photos: list[dict[str, Any]],
    thr: float,
    *,
    exact: bool,
    formatted: bool,
    near: bool,
    clustering: str = CLUSTER_HASH_VECTOR,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Compute (kind, members) clusters from photos. Does not write group rows.

    ``clustering`` is HASH_VECTOR (file hash + exact fingerprint + SigLIP
    union-find) or SCENE_MATCH (time + perceptual fingerprint + color +
    homography, with embeddings as a cue not a chain).
    """
    _ensure_quality_scores(photos)

    by_content: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for p in photos:
        by_content[int(p["content_id"])].append(p)
    # Prefer live members as content representatives for near-dup date/threshold checks.
    for members in by_content.values():
        members.sort(key=lambda p: (1 if p.get("culled") else 0, p["id"]))

    grouped: set[int] = set()
    out: list[tuple[str, list[dict[str, Any]]]] = []

    if exact:
        by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in photos:
            if p.get("file_hash"):
                by_hash[p["file_hash"]].append(p)
        for members in by_hash.values():
            if len(members) < 2:
                continue
            for m in members:
                grouped.add(m["id"])
            out.append(("exact", members))

    if formatted:
        by_px: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in photos:
            if p["id"] in grouped:
                continue
            fp = p.get("fingerprint")
            if fp:
                by_px[fp].append(p)
        for members in by_px.values():
            if len(members) < 2:
                continue
            for m in members:
                grouped.add(m["id"])
            out.append(("formatted", members))

    if near:
        if clustering == CLUSTER_SCENE_MATCH:
            active_ids = [
                cid
                for cid, members in by_content.items()
                if members and not any(p["id"] in grouped for p in members)
            ]
            if len(active_ids) >= 2:
                params = scene_params_from_threshold(thr)
                for member_ids in cluster_scene_contents(by_content, active_ids, params):
                    members = [p for cid in member_ids for p in by_content[cid]]
                    for m in members:
                        grouped.add(m["id"])
                    out.append(("near", members))
        else:
            _find_hash_vector_near(by_content, grouped, out, thr)

    return out


def _find_hash_vector_near(
    by_content: dict[int, list[dict[str, Any]]],
    grouped: set[int],
    out: list[tuple[str, list[dict[str, Any]]]],
    thr: float,
) -> None:
    """HASH_VECTOR near-dups: union-find on SigLIP cosine (baked edges / FAISS)."""
    active_ids = [
        cid
        for cid, members in by_content.items()
        if members
        and members[0].get("embedding_ready")
        and not any(p["id"] in grouped for p in members)
    ]
    if len(active_ids) < 2:
        return
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

    for member_ids in clusters_ids.values():
        if len(member_ids) < 2:
            continue
        members = [p for cid in member_ids for p in by_content[cid]]
        for m in members:
            grouped.add(m["id"])
        out.append(("near", members))


def rebuild_groups(
    threshold: float | None = None,
    *,
    exact: bool = True,
    formatted: bool = False,
    near: bool = True,
    photo_ids: list[int] | None = None,
    include_culled: bool = False,
    persist: bool = True,
    clustering: str = CLUSTER_HASH_VECTOR,
) -> dict[str, Any]:
    """Cluster photos. With persist=False, only compute and return items (no DB writes)."""
    settings = load_settings()
    thr = float(threshold if threshold is not None else settings.get("near_dup_threshold", 0.92))
    method = _normalize_clustering(clustering)
    if not (exact or formatted or near):
        exact = True

    scope = {int(i) for i in photo_ids} if photo_ids is not None else None
    empty = {
        "groups": 0,
        "items": [],
        "exact_groups": 0,
        "formatted_groups": 0,
        "near_groups": 0,
        "replicas": 0,
        "threshold": thr,
        "clustering": method,
        "modes": {"exact": exact, "formatted": formatted, "near": near},
        "include_culled": include_culled,
        "persisted": False,
    }
    if scope is not None and not scope:
        empty["scoped"] = 0
        return empty

    photos = _load_cluster_photos(scope, include_culled=include_culled)
    if scope is not None:
        photos = _expand_cluster_partners(
            photos,
            exact=exact,
            formatted=formatted,
            near=near,
            include_culled=include_culled,
            clustering=method,
        )
    clusters = _find_clusters(
        photos,
        thr,
        exact=exact,
        formatted=formatted,
        near=near,
        clustering=method,
    )
    if scope is not None:
        clusters = [
            (kind, members)
            for kind, members in clusters
            if any(m["id"] in scope for m in members)
        ]

    exact_n = sum(1 for kind, _ in clusters if kind == "exact")
    formatted_n = sum(1 for kind, _ in clusters if kind == "formatted")
    near_n = sum(1 for kind, _ in clusters if kind == "near")

    items: list[dict[str, Any]]
    if persist:
        touch = set(scope) if scope is not None else None
        if touch is not None:
            touch |= {m["id"] for _, members in clusters for m in members}
        _clear_groups_for_scope(touch, photos, include_culled=include_culled)
        items = []
        grouped: set[int] = set()
        with db() as conn:
            by_kind: dict[str, list[list[dict[str, Any]]]] = defaultdict(list)
            for kind, members in clusters:
                by_kind[kind].append(members)
            for kind in ("exact", "formatted", "near"):
                _, kind_items = _insert_groups(
                    conn, by_kind.get(kind, []), kind, grouped, scope=scope
                )
                items.extend(kind_items)
    else:
        items = [
            _materialize_group(kind, members, group_id=-(i + 1), scope=scope)
            for i, (kind, members) in enumerate(clusters)
        ]

    replicas = sum(
        1
        for g in items
        for m in g.get("members") or []
        if not m.get("culled") and not m.get("is_keeper")
    )
    out = {
        "groups": len(items),
        "items": items,
        "exact_groups": exact_n,
        "formatted_groups": formatted_n,
        "near_groups": near_n,
        "replicas": replicas,
        "threshold": thr,
        "clustering": method,
        "modes": {"exact": exact, "formatted": formatted, "near": near},
        "include_culled": include_culled,
        "persisted": persist,
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
    trashed_rows: list[dict[str, Any]] = []
    with db() as conn:
        placeholders = ",".join("?" * len(photo_ids))
        rows = conn.execute(
            f"""
            SELECT p.id, p.path, p.rel_path, s.path AS source_path, s.shadow_path
            FROM photos p
            JOIN sources s ON s.id = p.source_id
            WHERE p.id IN ({placeholders}) AND p.culled = 0
            """,
            photo_ids,
        ).fetchall()
        for r in rows:
            ids.append(r["id"])
            data = dict(r)
            if delete_files and _trash_file(Path(r["path"])):
                # Shadow/replica copies are left in place on purpose.
                deleted += 1
                trashed_rows.append(data)
            removed += 1
        if ids:
            conn.executemany("UPDATE photos SET culled = 1 WHERE id = ?", [(i,) for i in ids])
            _refresh_groups(conn)

    if trashed_rows:
        _prune_empty_parents(trashed_rows)

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
    empty = {
        "deleted": 0,
        "trashed": 0,
        "failed": 0,
        "deleted_ids": [],
        "failed_ids": [],
    }
    if not photo_ids:
        return empty
    with db() as conn:
        placeholders = ",".join("?" * len(photo_ids))
        rows = conn.execute(
            f"""
            SELECT p.id, p.path, p.rel_path, p.content_id, s.path AS source_path, s.shadow_path
            FROM photos p
            JOIN sources s ON s.id = p.source_id
            WHERE p.id IN ({placeholders}) AND p.culled = 1
            """,
            photo_ids,
        ).fetchall()
        if not rows:
            return empty

        by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_path[r["path"]].append(dict(r))

        ok_paths: list[str] = []
        failed_ids: list[int] = []
        trashed = 0
        for path, members in sorted(by_path.items()):
            if _trash_indexed_file(members[0], drop_catalog=True):
                trashed += 1
                ok_paths.append(path)
            else:
                failed_ids.extend(int(m["id"]) for m in members)

        if not ok_paths:
            return {
                "deleted": 0,
                "trashed": 0,
                "failed": len(failed_ids),
                "deleted_ids": [],
                "failed_ids": failed_ids,
            }

        ph = ",".join("?" * len(ok_paths))
        doomed = conn.execute(
            f"SELECT id, content_id FROM photos WHERE path IN ({ph})", ok_paths
        ).fetchall()
        purge_ids = [int(r["id"]) for r in doomed]
        content_ids = {int(r["content_id"]) for r in doomed}
        conn.execute(
            f"DELETE FROM photos WHERE id IN ({','.join('?' * len(purge_ids))})",
            purge_ids,
        )
        for cid in content_ids:
            gc_orphaned_content(conn, cid)
        _refresh_groups(conn)

    if ok_paths:
        _prune_empty_parents([by_path[p][0] for p in ok_paths])

    return {
        "deleted": len(purge_ids),
        "trashed": trashed,
        "failed": len(failed_ids),
        "deleted_ids": purge_ids,
        "failed_ids": failed_ids,
    }


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
