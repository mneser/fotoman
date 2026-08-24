"""User-created collections keyed by photo content (hash vault)."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any

from fotoman.db import db
from fotoman.metadata import write_taken_at_mtime
from fotoman.config import (
    fotomob_share_url,
    fotomob_subfolder_share,
    load_settings,
    new_share_token,
)

_FOTOMOB_CONTENT_CACHE: tuple[float, frozenset[int]] | None = None
_FOTOMOB_CONTENT_TTL = 10.0

LINK_GROUPS = frozenset({"folder", "date", "location", "camera", "aspect", "person"})


class NameConflict(Exception):
    def __init__(self, existing: dict[str, Any]):
        self.existing = existing
        super().__init__("Collection name already exists")


_UNSAFE_FS = re.compile(r'[\\/:*?"<>|]+')


def _culled_sql(include_culled: bool, alias: str = "p") -> str:
    from fotoman.device import vault_live_sql_subquery

    col = f"{alias}.culled"
    live = vault_live_sql_subquery(alias)
    if include_culled:
        return live
    return f"{col} = 0 AND {live}"


def _normalize_name(name: str) -> str:
    return (name or "").strip()


def _safe_fs_name(name: str) -> str:
    label = _UNSAFE_FS.sub("_", (name or "").strip()).strip(" .")
    return label or "collection"


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


def _cluster_content_ids(conn, content_ids: list[int]) -> list[int]:
    """Include every live content that shares a near-dup group with ``content_ids``."""
    if not content_ids:
        return []
    placeholders = ",".join("?" * len(content_ids))
    group_rows = conn.execute(
        f"""
        SELECT DISTINCT group_id
        FROM photos
        WHERE content_id IN ({placeholders})
          AND group_id IS NOT NULL
          AND missing = 0 AND culled = 0
        """,
        content_ids,
    ).fetchall()
    extra = set(int(c) for c in content_ids)
    gids = [int(r["group_id"]) for r in group_rows]
    if gids:
        gph = ",".join("?" * len(gids))
        rows = conn.execute(
            f"""
            SELECT DISTINCT content_id
            FROM photos
            WHERE group_id IN ({gph})
              AND content_id IS NOT NULL
              AND missing = 0 AND culled = 0
            """,
            gids,
        ).fetchall()
        extra.update(int(r["content_id"]) for r in rows)
    return list(extra)


def _collection_row(conn, collection_id: int):
    return conn.execute(
        "SELECT * FROM collections WHERE id = ?",
        (int(collection_id),),
    ).fetchone()


def _link_of(row) -> tuple[str, str] | None:
    if row is None:
        return None
    data = dict(row)
    gb = str(data.get("link_group_by") or "").strip()
    key = str(data.get("link_key") or "").strip()
    if gb not in LINK_GROUPS or not key:
        return None
    return gb, key


def _is_source_subdir(conn, row) -> bool:
    """Linked node whose parent is also source-linked (imported tree child)."""
    if not _link_of(row):
        return False
    pid = row["parent_id"] if row["parent_id"] is not None else None
    if pid is None:
        return False
    return _link_of(_collection_row(conn, int(pid))) is not None


def _apply_link_flags(conn, node: dict[str, Any]) -> dict[str, Any]:
    node["linked"] = _link_of(node) is not None
    node["source_subdir"] = _is_source_subdir(conn, node)
    return node


def _annotate_linked(nodes: list[dict[str, Any]], parent_linked: bool = False) -> None:
    for n in nodes:
        n["linked"] = _link_of(n) is not None
        n["source_subdir"] = bool(n["linked"] and parent_linked)
        _annotate_linked(n.get("children") or [], n["linked"])


def _live_content_ids(
    group_by: str,
    key: str,
    include_culled: bool,
    include_subfolders: bool,
) -> list[int]:
    from fotoman import browse

    photos = browse.photos_for_group(
        group_by,
        key,
        limit=100000,
        include_culled=include_culled,
        include_subfolders=include_subfolders,
    )
    out: list[int] = []
    seen: set[int] = set()
    for p in photos:
        cid = p.get("content_id")
        if cid is None:
            continue
        cid = int(cid)
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def _content_ids_for_collection(
    conn,
    collection_id: int,
    include_culled: bool,
    include_subfolders: bool,
) -> list[int]:
    row = _collection_row(conn, collection_id)
    if not row:
        return []
    ids = _subtree_ids(conn, collection_id) if include_subfolders else [int(collection_id)]
    id_set = set(ids)
    seen = set(_member_content_ids(conn, ids))
    for fid in ids:
        frow = row if fid == int(collection_id) else _collection_row(conn, fid)
        link = _link_of(frow) if frow else None
        if not link:
            continue
        if include_subfolders:
            pid = frow["parent_id"] if frow["parent_id"] is not None else None
            if pid is not None and int(pid) in id_set:
                parent = row if int(pid) == int(collection_id) else _collection_row(conn, int(pid))
                if _link_of(parent):
                    # Nested source node — ancestor's include_subfolders fetch covers it.
                    continue
            nested = True
        else:
            nested = False
        seen.update(_live_content_ids(link[0], link[1], include_culled, nested))
    return list(seen)


def _child_ids(conn, collection_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM collections WHERE parent_id = ? ORDER BY lower(name), id",
        (int(collection_id),),
    ).fetchall()
    return [int(r["id"]) for r in rows]


def _descendant_ids(conn, collection_id: int) -> list[int]:
    out: list[int] = []
    stack = _child_ids(conn, collection_id)
    while stack:
        fid = stack.pop()
        out.append(fid)
        stack.extend(_child_ids(conn, fid))
    return out


def _ancestor_ids(conn, collection_id: int) -> list[int]:
    out: list[int] = []
    row = _collection_row(conn, collection_id)
    while row and row["parent_id"] is not None:
        pid = int(row["parent_id"])
        out.append(pid)
        row = _collection_row(conn, pid)
    return out


def _root_id(conn, collection_id: int) -> int:
    fid = int(collection_id)
    row = _collection_row(conn, fid)
    while row and row["parent_id"] is not None:
        fid = int(row["parent_id"])
        row = _collection_row(conn, fid)
    return fid


def _same_collection_tree(conn, a_id: int, b_id: int) -> bool:
    return _root_id(conn, a_id) == _root_id(conn, b_id)


def _subtree_ids(conn, collection_id: int) -> list[int]:
    return [int(collection_id), *_descendant_ids(conn, collection_id)]


def _is_descendant(conn, collection_id: int, of_id: int) -> bool:
    return int(collection_id) in _descendant_ids(conn, of_id)


def _member_live_counts(conn, culled: str) -> dict[int, int]:
    rows = conn.execute(
        f"""
        SELECT cm.collection_id, COUNT(DISTINCT p.content_id)
        FROM collection_members cm
        JOIN photos p ON p.content_id = cm.content_id
        WHERE {culled}
        GROUP BY cm.collection_id
        """
    ).fetchall()
    return {int(r[0]): int(r[1] or 0) for r in rows}


def _index_browse_counts(
    nodes: list[dict[str, Any]], group_by: str, out: dict[tuple[str, str], tuple[int, int]]
) -> None:
    for n in nodes or []:
        path = str(n.get("path") or n.get("key") or "")
        if path:
            total = int(n.get("count") or 0)
            if n.get("direct_count") is not None:
                direct = int(n["direct_count"])
            else:
                direct = 0 if n.get("children") else total
            out[(group_by, path)] = (direct, total)
        _index_browse_counts(n.get("children") or [], group_by, out)


def _folder_trees_for_links(
    include_culled: bool, links: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    from fotoman import browse

    keys = [k for gb, k in links if gb == "folder"]
    sids: list[int] = []
    for k in keys:
        sid = _source_id_from_folder_key(k)
        if sid is None:
            return browse.folder_tree(
                include_files=False, include_culled=include_culled
            )
        sids.append(sid)
    nodes: list[dict[str, Any]] = []
    for sid in sorted(set(sids)):
        nodes.extend(
            browse.folder_tree(
                source_id=sid, include_files=False, include_culled=include_culled
            )
        )
    return nodes


def _load_linked_browse(
    include_culled: bool, links: set[tuple[str, str]]
) -> tuple[dict[str, list[dict[str, Any]]], dict[tuple[str, str], tuple[int, int]]]:
    """One browse-tree walk per linked grouping. Returns (trees, counts)."""
    if not links:
        return {}, {}
    from fotoman import browse
    from fotoman.config import load_settings

    groups = {gb for gb, _ in links}
    trees: dict[str, list[dict[str, Any]]] = {}
    out: dict[tuple[str, str], tuple[int, int]] = {}
    if "date" in groups:
        order = str(load_settings().get("date_tree_order") or "newest").lower()
        nodes = browse.date_tree(
            include_culled=include_culled, newest_first=order != "oldest"
        )
        trees["date"] = nodes
        _index_browse_counts(nodes, "date", out)
    if "location" in groups:
        nodes = browse.location_tree(include_culled=include_culled)
        trees["location"] = nodes
        _index_browse_counts(nodes, "location", out)
    if "folder" in groups:
        nodes = _folder_trees_for_links(include_culled, links)
        trees["folder"] = nodes
        _index_browse_counts(nodes, "folder", out)
    for gb in ("camera", "aspect"):
        if gb not in groups:
            continue
        for g in browse.group_list(gb, include_culled=include_culled):
            key = str(g.get("key") or "")
            if not key:
                continue
            n = int(g.get("count") or 0)
            out[(gb, key)] = (n, n)
    if "person" in groups:
        from fotoman import faces

        for person in faces.people_list():
            n = int(person.get("photo_count") or 0)
            out[("person", str(person["id"]))] = (n, n)
    return trees, out


def _apply_list_counts(
    nodes: list[dict[str, Any]],
    member_direct: dict[int, int],
    live_counts: dict[tuple[str, str], tuple[int, int]],
) -> None:
    for n in nodes:
        kids = n.get("children") or []
        _apply_list_counts(kids, member_direct, live_counts)
        link = _link_of(n)
        if link:
            direct, total = live_counts.get(link, (0, 0))
            n["direct_count"] = int(direct)
            n["count"] = int(total)
            continue
        mem = int(member_direct.get(int(n["id"]), 0) or 0)
        n["direct_count"] = mem
        n["count"] = mem + sum(int(k.get("count") or 0) for k in kids)


def _conflict_sibling(conn, name: str, parent_id: int | None, exclude_id: int | None = None):
    if parent_id is None:
        sql = """
            SELECT id, name, parent_id FROM collections
            WHERE parent_id IS NULL AND lower(trim(name)) = lower(?)
        """
        params: list[Any] = [name]
    else:
        sql = """
            SELECT id, name, parent_id FROM collections
            WHERE parent_id = ? AND lower(trim(name)) = lower(?)
        """
        params = [int(parent_id), name]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(int(exclude_id))
    return conn.execute(sql, params).fetchone()


def list_collections(include_culled: bool = False, fotomob_only: bool = False) -> list[dict[str, Any]]:
    with db() as conn:
        link_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, parent_id, link_group_by, link_key
                FROM collections
                WHERE IFNULL(link_group_by, '') != ''
                  AND IFNULL(link_key, '') != ''
                """
            )
        ]
    links = {
        (str(r["link_group_by"]), str(r["link_key"])) for r in link_rows
    }
    trees, live_counts = _load_linked_browse(include_culled, links)
    _apply_linked_tree_sync(link_rows, trees)
    culled = _culled_sql(include_culled, "p")
    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM collections
            ORDER BY lower(name), id
            """
        ).fetchall()
        member_direct = _member_live_counts(conn, culled)
        by_parent: dict[int | None, list[dict[str, Any]]] = {}
        nodes: dict[int, dict[str, Any]] = {}
        for r in rows:
            node = dict(r)
            fid = int(node["id"])
            node["children"] = []
            nodes[fid] = node
            pid = node["parent_id"]
            key = int(pid) if pid is not None else None
            by_parent.setdefault(key, []).append(node)
        for fid, node in nodes.items():
            node["children"] = by_parent.get(fid, [])
            node["fotomob_shared"] = bool(int(node.get("fotomob_shared") or 0))
        roots = by_parent.get(None, [])
    _apply_list_counts(roots, member_direct, live_counts)
    _annotate_fotomob(roots)
    _annotate_share_links(roots)
    _annotate_linked(roots)
    if fotomob_only:
        return _hoist_shared(roots)
    return roots


def _annotate_fotomob(nodes: list[dict[str, Any]], ancestor_shared: bool = False) -> None:
    for n in nodes:
        n["fotomob_shared"] = bool(n.get("fotomob_shared"))
        n["fotomob_visible"] = ancestor_shared or n["fotomob_shared"]
        _annotate_fotomob(n.get("children") or [], n["fotomob_visible"])


def _annotate_share_links(nodes: list[dict[str, Any]], *, nested: bool = False) -> None:
    allow = (not nested) or fotomob_subfolder_share()
    for n in nodes:
        token = str(n.get("fotomob_share_token") or "").strip()
        if allow and n.get("fotomob_shared") and token:
            n["share_code"] = token
            n["share_url"] = fotomob_share_url(token)
        _annotate_share_links(n.get("children") or [], nested=True)


def _hoist_shared(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        if n.get("fotomob_shared"):
            out.append(n)
        else:
            out.extend(_hoist_shared(n.get("children") or []))
    return out


def _pick_subtree_roots(nodes: list[dict[str, Any]], want: set[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        if int(n["id"]) in want:
            out.append(n)
        else:
            out.extend(_pick_subtree_roots(n.get("children") or [], want))
    return out


def shared_collection_ids_for_tokens(tokens: list[str]) -> list[int]:
    cleaned = [str(t).strip() for t in tokens if str(t).strip()]
    if not cleaned:
        return []
    placeholders = ",".join("?" * len(cleaned))
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT id FROM collections
            WHERE fotomob_shared != 0
              AND fotomob_share_token IN ({placeholders})
            """,
            cleaned,
        ).fetchall()
    return [int(r["id"]) for r in rows]


def collection_ids_for_share_roots(root_ids: list[int]) -> set[int]:
    if not root_ids:
        return set()
    with db() as conn:
        out: set[int] = set()
        for fid in root_ids:
            out.update(_subtree_ids(conn, int(fid)))
        return out


def content_ids_for_share_roots(root_ids: list[int]) -> set[int]:
    if not root_ids:
        return set()
    with db() as conn:
        seen: set[int] = set()
        skip: set[int] = set()
        for fid in root_ids:
            fid = int(fid)
            if fid in skip:
                continue
            for kid in _descendant_ids(conn, fid):
                skip.add(kid)
            seen.update(_content_ids_for_collection(conn, fid, False, True))
        return seen


def list_collections_for_share_tokens(
    tokens: list[str], include_culled: bool = False
) -> list[dict[str, Any]]:
    roots = shared_collection_ids_for_tokens(tokens)
    if not roots:
        return []
    tree = list_collections(include_culled=include_culled)
    return _pick_subtree_roots(tree, set(roots))


def invalidate_fotomob_share_cache() -> None:
    global _FOTOMOB_CONTENT_CACHE
    _FOTOMOB_CONTENT_CACHE = None


def _fotomob_accessible_collection_ids(conn) -> set[int]:
    rows = conn.execute(
        "SELECT id, parent_id, fotomob_shared FROM collections"
    ).fetchall()
    children: dict[int | None, list[int]] = {}
    roots: list[int] = []
    for r in rows:
        fid = int(r["id"])
        pid = int(r["parent_id"]) if r["parent_id"] is not None else None
        children.setdefault(pid, []).append(fid)
        if int(r["fotomob_shared"] or 0):
            roots.append(fid)
    out: set[int] = set()
    stack = list(roots)
    while stack:
        fid = stack.pop()
        if fid in out:
            continue
        out.add(fid)
        stack.extend(children.get(fid, []))
    return out


def collection_is_fotomob_visible(collection_id: int) -> bool:
    with db() as conn:
        return int(collection_id) in _fotomob_accessible_collection_ids(conn)


def fotomob_visible_content_ids() -> set[int]:
    global _FOTOMOB_CONTENT_CACHE
    now = time.monotonic()
    cached = _FOTOMOB_CONTENT_CACHE
    if cached and now - cached[0] < _FOTOMOB_CONTENT_TTL:
        return set(cached[1])
    with db() as conn:
        roots = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM collections WHERE fotomob_shared != 0"
            )
        ]
        seen: set[int] = set()
        skip: set[int] = set()
        for fid in roots:
            if fid in skip:
                continue
            for kid in _descendant_ids(conn, fid):
                skip.add(kid)
            seen.update(_content_ids_for_collection(conn, fid, False, True))
    _FOTOMOB_CONTENT_CACHE = (now, frozenset(seen))
    return set(seen)


def _allocate_share_token(conn) -> str:
    for _ in range(8):
        token = new_share_token()
        hit = conn.execute(
            "SELECT 1 FROM collections WHERE fotomob_share_token = ?",
            (token,),
        ).fetchone()
        if not hit:
            return token
    raise RuntimeError("Could not allocate a share token")


def _ensure_collection_shared(conn, collection_id: int) -> None:
    row = _collection_row(conn, collection_id)
    if not row:
        return
    token = str(row["fotomob_share_token"] or "").strip()
    if int(row["fotomob_shared"] or 0) and token:
        return
    if not token:
        token = _allocate_share_token(conn)
    conn.execute(
        """
        UPDATE collections
        SET fotomob_shared = 1, fotomob_share_token = ?
        WHERE id = ?
        """,
        (token, int(collection_id)),
    )


def _public_collection(row: dict[str, Any]) -> dict[str, Any]:
    row["fotomob_shared"] = bool(int(row.get("fotomob_shared") or 0))
    row["linked"] = _link_of(row) is not None
    row.setdefault("source_subdir", False)
    token = str(row.get("fotomob_share_token") or "").strip()
    nested = row.get("parent_id") is not None
    if row["fotomob_shared"] and token and ((not nested) or fotomob_subfolder_share()):
        row["share_code"] = token
        row["share_url"] = fotomob_share_url(token)
    return row


def get_collection(collection_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = _collection_row(conn, collection_id)
        if not row:
            return None
        return _apply_link_flags(conn, _public_collection(dict(row)))


def resolve_collection_key(key: str) -> int:
    """Resolve a collection id, unique name, or Parent/Child path."""
    raw = (key or "").strip()
    if not raw:
        raise ValueError("Collection key required")
    if raw.isdigit():
        fid = int(raw)
        with db() as conn:
            if _collection_row(conn, fid):
                return fid
        # Digit names (a dropped year like "2019") are not collection ids.
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    with db() as conn:
        if len(parts) == 1:
            rows = conn.execute(
                """
                SELECT id FROM collections
                WHERE lower(trim(name)) = lower(?)
                """,
                (parts[0],),
            ).fetchall()
            if not rows:
                raise ValueError("Collection not found")
            if len(rows) > 1:
                raise ValueError("Collection name is ambiguous; use id or Parent/Name")
            return int(rows[0]["id"])
        parent_id: int | None = None
        fid = None
        for part in parts:
            row = _conflict_sibling(conn, part, parent_id)
            if not row:
                raise ValueError("Collection not found")
            fid = int(row["id"])
            parent_id = fid
        if fid is None:
            raise ValueError("Collection not found")
        return fid


def content_ids_for_collection(
    collection_id: int,
    include_subfolders: bool = True,
    include_culled: bool = True,
) -> list[int]:
    with db() as conn:
        return _content_ids_for_collection(
            conn, collection_id, include_culled, include_subfolders
        )


def create_collection(name: str, parent_id: int | None = None) -> dict[str, Any]:
    label = _normalize_name(name)
    if not label:
        raise ValueError("Folder name is required")
    with db() as conn:
        pid = int(parent_id) if parent_id is not None else None
        if pid is not None:
            parent = _collection_row(conn, pid)
            if not parent:
                raise KeyError("not found")
            if _link_of(parent):
                raise ValueError("Cannot add a subfolder to a source folder")
        existing = _conflict_sibling(conn, label, pid)
        if existing:
            raise NameConflict(dict(existing))
        token = _allocate_share_token(conn)
        cur = conn.execute(
            """
            INSERT INTO collections(name, parent_id, fotomob_shared, fotomob_share_token)
            VALUES (?, ?, 1, ?)
            """,
            (label, pid, token),
        )
        collection_id = int(cur.lastrowid)
        row = _collection_row(conn, collection_id)
        out = _apply_link_flags(conn, _public_collection(dict(row)))
    invalidate_fotomob_share_cache()
    out["count"] = 0
    out["direct_count"] = 0
    out["children"] = []
    return out


def rename_collection(
    collection_id: int,
    name: str | None = None,
    parent_id: int | None = None,
    set_parent: bool = False,
) -> dict[str, Any]:
    with db() as conn:
        row = _collection_row(conn, collection_id)
        if not row:
            raise KeyError("not found")
        if name is not None and _is_source_subdir(conn, row):
            raise ValueError("Linked children cannot be renamed in Collections")
        label = _normalize_name(name) if name is not None else row["name"]
        if not label:
            raise ValueError("Folder name is required")
        pid = row["parent_id"]
        if set_parent:
            if _is_source_subdir(conn, row):
                raise ValueError("Source subfolders cannot be moved in Collections")
            pid = int(parent_id) if parent_id is not None else None
            if pid is not None:
                if pid == int(collection_id) or _is_descendant(conn, pid, collection_id):
                    raise ValueError("Cannot move a collection into itself")
                dest = _collection_row(conn, pid)
                if not dest:
                    raise KeyError("not found")
                if _link_of(dest):
                    raise ValueError("Cannot move a collection into a source folder")
        existing = _conflict_sibling(conn, label, pid, exclude_id=int(collection_id))
        if existing:
            raise NameConflict(dict(existing))
        conn.execute(
            "UPDATE collections SET name = ?, parent_id = ? WHERE id = ?",
            (label, pid, int(collection_id)),
        )
        updated = _collection_row(conn, collection_id)
        return _apply_link_flags(conn, _public_collection(dict(updated)))


def delete_collection(collection_id: int) -> dict[str, Any]:
    with db() as conn:
        row = _collection_row(conn, collection_id)
        if not row:
            raise KeyError("not found")
        if _is_source_subdir(conn, row):
            raise ValueError("Source subfolders cannot be deleted from Collections")
        ids = _subtree_ids(conn, collection_id)
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE sources SET backup_collection_id = NULL WHERE backup_collection_id IN ({placeholders})",
            ids,
        )
        for fid in reversed(ids):
            conn.execute("DELETE FROM collections WHERE id = ?", (fid,))
    invalidate_fotomob_share_cache()
    return {"deleted": True, "id": int(collection_id), "name": row["name"]}


def add_members(collection_id: int, photo_ids: list[int]) -> dict[str, Any]:
    with db() as conn:
        folder = _collection_row(conn, collection_id)
        if not folder:
            raise KeyError("not found")
        if _link_of(folder):
            raise ValueError("Cannot add photos to a source folder")
        content_ids = _content_ids_for_photos(conn, photo_ids)
        copies = str(load_settings().get("collection_copies") or "primary").lower()
        if copies == "cluster":
            content_ids = _cluster_content_ids(conn, content_ids)
        added = 0
        for cid in content_ids:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO collection_members(collection_id, content_id)
                VALUES (?, ?)
                """,
                (int(collection_id), cid),
            )
            added += cur.rowcount
    if added:
        from fotoman import backup

        backup.schedule_collection(int(collection_id))
    return {"collection_id": int(collection_id), "added": added, "requested": len(photo_ids)}


def move_members(
    from_collection_id: int, to_collection_id: int, photo_ids: list[int]
) -> dict[str, Any]:
    """Relocate membership within one collection tree (not a cross-album copy)."""
    if int(from_collection_id) == int(to_collection_id):
        return {
            "collection_id": int(to_collection_id),
            "from_collection_id": int(from_collection_id),
            "added": 0,
            "removed": 0,
            "requested": len(photo_ids),
        }
    with db() as conn:
        src = _collection_row(conn, from_collection_id)
        dest = _collection_row(conn, to_collection_id)
        if not src or not dest:
            raise KeyError("not found")
        if _link_of(dest):
            raise ValueError("Cannot move photos into a source folder")
        if not _same_collection_tree(conn, from_collection_id, to_collection_id):
            raise ValueError("Can only move photos within the same collection")
        content_ids = _content_ids_for_photos(conn, photo_ids)
        copies = str(load_settings().get("collection_copies") or "primary").lower()
        if copies == "cluster":
            content_ids = _cluster_content_ids(conn, content_ids)
        if not content_ids:
            return {
                "collection_id": int(to_collection_id),
                "from_collection_id": int(from_collection_id),
                "added": 0,
                "removed": 0,
                "requested": len(photo_ids),
            }
        added = 0
        for cid in content_ids:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO collection_members(collection_id, content_id)
                VALUES (?, ?)
                """,
                (int(to_collection_id), cid),
            )
            added += cur.rowcount
        # Drop membership anywhere under the browse source (parent may show child members).
        scope = [
            fid
            for fid in _subtree_ids(conn, from_collection_id)
            if fid != int(to_collection_id)
            and not _link_of(_collection_row(conn, fid))
        ]
        removed = 0
        if scope:
            ph_c = ",".join("?" * len(content_ids))
            ph_f = ",".join("?" * len(scope))
            cur = conn.execute(
                f"""
                DELETE FROM collection_members
                WHERE collection_id IN ({ph_f}) AND content_id IN ({ph_c})
                """,
                [*scope, *content_ids],
            )
            removed = cur.rowcount
    if added or removed:
        from fotoman import backup

        backup.schedule_collection(int(to_collection_id))
        backup.schedule_collection(int(from_collection_id))
    return {
        "collection_id": int(to_collection_id),
        "from_collection_id": int(from_collection_id),
        "added": added,
        "removed": removed,
        "requested": len(photo_ids),
    }


def _linked_sibling(conn, parent_id: int | None, group_by: str, key: str):
    if parent_id is None:
        return conn.execute(
            """
            SELECT * FROM collections
            WHERE parent_id IS NULL AND link_group_by = ? AND link_key = ?
            """,
            (group_by, key),
        ).fetchone()
    return conn.execute(
        """
        SELECT * FROM collections
        WHERE parent_id = ? AND link_group_by = ? AND link_key = ?
        """,
        (int(parent_id), group_by, key),
    ).fetchone()


def _ensure_linked_collection(
    parent_id: int | None,
    name: str,
    group_by: str,
    key: str,
    *,
    conn=None,
    update_name: bool = False,
) -> tuple[int, bool]:
    label = _normalize_name(name) or "Untitled"
    gb = str(group_by or "").strip()
    src = str(key or "").strip()
    if gb not in LINK_GROUPS:
        raise ValueError("Cannot link this tree")
    if not src:
        raise ValueError("Missing source key")
    pid = int(parent_id) if parent_id is not None else None

    def work(conn):
        if pid is not None and not _collection_row(conn, pid):
            raise KeyError("not found")
        by_link = _linked_sibling(conn, pid, gb, src)
        if by_link:
            eid = int(by_link["id"])
            if update_name and label != str(by_link["name"] or ""):
                clash = _conflict_sibling(conn, label, pid, exclude_id=eid)
                if not clash:
                    conn.execute(
                        "UPDATE collections SET name = ? WHERE id = ?",
                        (label, eid),
                    )
            _ensure_collection_shared(conn, eid)
            return eid, False
        existing = _conflict_sibling(conn, label, pid)
        if existing:
            eid = int(existing["id"])
            row = _collection_row(conn, eid)
            cur_link = _link_of(row)
            new_link = (gb, src)
            if cur_link and cur_link != new_link:
                raise NameConflict(dict(existing))
            if cur_link != new_link:
                conn.execute(
                    "UPDATE collections SET link_group_by = ?, link_key = ? WHERE id = ?",
                    (gb, src, eid),
                )
            _ensure_collection_shared(conn, eid)
            return eid, False
        token = _allocate_share_token(conn)
        cur = conn.execute(
            """
            INSERT INTO collections(
                name, parent_id, link_group_by, link_key,
                fotomob_shared, fotomob_share_token
            )
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (label, pid, gb, src, token),
        )
        return int(cur.lastrowid), True

    if conn is not None:
        return work(conn)
    with db() as owned:
        return work(owned)


def ensure_folder_collection(name: str, folder_key: str) -> dict[str, Any] | None:
    """Live-link a browse folder as a shared collection (idempotent)."""
    key = str(folder_key or "").strip()
    label = _normalize_name(name) or "Phone"
    if not key:
        return None
    with db() as conn:
        row = conn.execute(
            """
            SELECT * FROM collections
            WHERE parent_id IS NULL AND link_group_by = 'folder' AND link_key = ?
            ORDER BY id
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        if row:
            cid = int(row["id"])
            _ensure_collection_shared(conn, cid)
            row = _collection_row(conn, cid)
            invalidate_fotomob_share_cache()
            return _public_collection(dict(row)) if row else None
    try:
        cid, _created = _ensure_linked_collection(None, label, "folder", key)
    except NameConflict:
        suffix = key.split(":")[0][-4:]
        try:
            cid, _created = _ensure_linked_collection(
                None, f"{label} {suffix}".strip(), "folder", key
            )
        except NameConflict:
            return None
    invalidate_fotomob_share_cache()
    return get_collection(cid)


def _source_id_from_folder_key(folder_key: str) -> int | None:
    raw = str(folder_key or "").strip()
    sid, sep, _rest = raw.partition(":")
    if not sep or not sid.isdigit():
        return None
    return int(sid)


def _index_tree_by_path(
    nodes: list[dict[str, Any]], out: dict[str, dict[str, Any]]
) -> None:
    for node in nodes or []:
        path = str(node.get("path") or "")
        if path:
            out[path] = node
        _index_tree_by_path(node.get("children") or [], out)


def _purge_collection_tree(conn, collection_id: int) -> None:
    ids = _subtree_ids(conn, collection_id)
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE sources SET backup_collection_id = NULL WHERE backup_collection_id IN ({placeholders})",
        ids,
    )
    for fid in reversed(ids):
        conn.execute("DELETE FROM collections WHERE id = ?", (fid,))


def _flatten_date_collection(conn, collection_id: int) -> bool:
    """Date drops are a single live-linked node — no month/day subfolders."""
    rows = conn.execute(
        "SELECT id FROM collections WHERE parent_id = ?",
        (int(collection_id),),
    ).fetchall()
    if not rows:
        return False
    for row in rows:
        _purge_collection_tree(conn, int(row["id"]))
    return True


def _sync_linked_children(
    conn, collection_id: int, node: dict[str, Any], group_by: str
) -> bool:
    if group_by == "date":
        return _flatten_date_collection(conn, collection_id)
    changed = False
    live: dict[str, dict[str, Any]] = {}
    for kid in node.get("children") or []:
        if kid.get("is_file"):
            continue
        key = str(kid.get("path") or "")
        if not key:
            continue
        live[key] = kid
        kid_id, created = _ensure_linked_collection(
            collection_id,
            kid.get("name") or "",
            group_by,
            key,
            conn=conn,
            update_name=True,
        )
        if created:
            changed = True
        if _sync_linked_children(conn, kid_id, kid, group_by):
            changed = True
    rows = conn.execute(
        """
        SELECT id, link_key FROM collections
        WHERE parent_id = ? AND link_group_by = ?
        """,
        (int(collection_id), group_by),
    ).fetchall()
    for row in rows:
        if str(row["link_key"] or "") not in live:
            _purge_collection_tree(conn, int(row["id"]))
            changed = True
    return changed


def _apply_linked_tree_sync(
    rows: list[dict[str, Any]], trees: dict[str, list[dict[str, Any]]]
) -> None:
    if not rows or not trees:
        return
    by_group: dict[str, dict[str, dict[str, Any]]] = {}
    for gb, nodes in trees.items():
        idx: dict[str, dict[str, Any]] = {}
        _index_tree_by_path(nodes, idx)
        by_group[gb] = idx
    linked_ids = {int(r["id"]) for r in rows}
    changed = False
    with db() as conn:
        for r in rows:
            pid = r["parent_id"]
            if pid is not None and int(pid) in linked_ids:
                continue
            gb = str(r["link_group_by"] or "")
            idx = by_group.get(gb)
            if not idx:
                continue
            node = idx.get(str(r["link_key"] or ""))
            if node is None:
                continue
            if _sync_linked_children(conn, int(r["id"]), node, gb):
                changed = True
    if changed:
        invalidate_fotomob_share_cache()


def sync_linked_trees(
    *, source_id: int | None = None, include_culled: bool = False
) -> None:
    """Keep linked collection children in sync with live browse trees."""
    sql = """
        SELECT id, parent_id, link_group_by, link_key
        FROM collections
        WHERE IFNULL(link_group_by, '') != '' AND IFNULL(link_key, '') != ''
    """
    params: list[Any] = []
    if source_id is not None:
        sql += " AND link_group_by = 'folder' AND (link_key = ? OR link_key LIKE ?)"
        prefix = f"{int(source_id)}:"
        params = [prefix, prefix + "%"]
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
    if not rows:
        return
    links = {(str(r["link_group_by"]), str(r["link_key"])) for r in rows}
    trees, _counts = _load_linked_browse(include_culled, links)
    _apply_linked_tree_sync(rows, trees)


def sync_linked_folder_trees(*, source_id: int | None = None) -> None:
    """Keep folder-linked collection children in sync with the volume tree."""
    sync_linked_trees(source_id=source_id)


def _import_browse_node(
    parent_id: int | None,
    node: dict[str, Any],
    group_by: str,
    *,
    conn,
    update_name: bool = False,
) -> tuple[int, int]:
    if node.get("is_file"):
        return 0, 0
    key = str(node.get("path") or "")
    child_id, created = _ensure_linked_collection(
        parent_id,
        node.get("name") or "",
        group_by,
        key,
        conn=conn,
        update_name=update_name,
    )
    created_n = 1 if created else 0
    if group_by == "date":
        return created_n, child_id
    for kid in node.get("children") or []:
        created_n += _import_browse_node(
            child_id, kid, group_by, conn=conn, update_name=True
        )[0]
    return created_n, child_id


def import_browse_branch(
    collection_id: int | None,
    group_by: str,
    key: str,
    include_culled: bool = False,
) -> dict[str, Any]:
    """Live-link a Places/Dates/Folders/People/Cameras/Aspect node into a collection.

    Date drops stay a single node (no month/day subfolders). Individual photos
    added via ``add_members`` stay as a membership snapshot.
    """
    from fotoman import browse

    if collection_id is not None:
        with db() as conn:
            parent = _collection_row(conn, collection_id)
            if not parent:
                raise KeyError("not found")
            if _link_of(parent):
                raise ValueError("Cannot add a subfolder to a source folder")
    node = browse.subtree_for_key(group_by, key, include_culled=include_culled)
    if not node:
        raise KeyError("source not found")
    with db() as conn:
        created, root_id = _import_browse_node(
            collection_id, node, group_by, conn=conn, update_name=False
        )
    from fotoman import backup

    backup.schedule_collection(int(root_id))
    invalidate_fotomob_share_cache()
    return {
        "collection_id": int(root_id),
        "parent_id": int(collection_id) if collection_id is not None else None,
        "created": created,
        "name": node.get("name"),
    }


def remove_members(collection_id: int, photo_ids: list[int]) -> dict[str, Any]:
    with db() as conn:
        folder = _collection_row(conn, collection_id)
        if not folder:
            raise KeyError("not found")
        if _link_of(folder):
            raise ValueError("Cannot remove photos from a source folder")
        content_ids = _content_ids_for_photos(conn, photo_ids)
        removed = 0
        if content_ids:
            placeholders = ",".join("?" * len(content_ids))
            cur = conn.execute(
                f"""
                DELETE FROM collection_members
                WHERE collection_id = ? AND content_id IN ({placeholders})
                """,
                [int(collection_id), *content_ids],
            )
            removed = cur.rowcount
    return {
        "collection_id": int(collection_id),
        "removed": removed,
        "requested": len(photo_ids),
    }


def _member_content_ids(conn, collection_ids: list[int]) -> list[int]:
    if not collection_ids:
        return []
    placeholders = ",".join("?" * len(collection_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT content_id FROM collection_members
        WHERE collection_id IN ({placeholders})
        """,
        [int(i) for i in collection_ids],
    ).fetchall()
    return [int(r["content_id"]) for r in rows]


def _primary_photo_sql(culled: str) -> str:
    return f"""
        SELECT p.id, p.path, p.content_id, c.taken_at
        FROM photos p
        JOIN photo_contents c ON c.id = p.content_id
        JOIN sources s ON s.id = p.source_id
        WHERE p.content_id = ? AND {culled}
        ORDER BY CASE WHEN IFNULL(s.kind, 'watched') = 'backup' THEN 1 ELSE 0 END,
                 p.is_keeper DESC, p.id ASC
        LIMIT 1
    """


def photos_for_collection(
    collection_id: int,
    limit: int = 200,
    include_culled: bool = False,
    include_subfolders: bool = True,
) -> list[dict[str, Any]]:
    culled = _culled_sql(include_culled, "p")
    with db() as conn:
        folder = _collection_row(conn, collection_id)
        if not folder:
            return []
        cids = _content_ids_for_collection(
            conn, collection_id, include_culled, include_subfolders
        )
        if not cids:
            return []
        placeholders = ",".join("?" * len(cids))
        rows = conn.execute(
            f"""
            WITH keepers AS (
              SELECT p.content_id, MIN(p.id) AS photo_id
              FROM photos p
              WHERE p.content_id IN ({placeholders}) AND {culled}
              GROUP BY p.content_id
            )
            SELECT
              p.id, p.source_id, p.path, p.rel_path, p.mtime, p.album,
              p.culled, p.missing, p.group_id, p.is_keeper, p.content_id,
              s.label AS source_label,
              c.file_hash, c.size, c.width, c.height, c.taken_at, c.taken_at_source, c.camera,
              c.lat, c.lon, c.gps, c.coord_source, c.venue, c.city, c.region, c.country, c.place_label,
              c.orientation, c.mirrored, c.fingerprint, c.embedding_ready, c.faces_ready, c.indexed_at
            FROM keepers k
            JOIN photos p ON p.id = k.photo_id
            JOIN photo_contents c ON c.id = p.content_id
            JOIN sources s ON s.id = p.source_id
            ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
            LIMIT ?
            """,
            [*cids, int(limit)],
        ).fetchall()
    return [dict(r) for r in rows]


def _walk_export_nodes(
    conn, collection_id: int, rel: str, culled: str, include_culled: bool
) -> list[tuple[int, str, str, str | None, int | None]]:
    row = _collection_row(conn, collection_id)
    if not row:
        return []
    name = _safe_fs_name(row["name"])
    here = str(Path(rel) / name) if rel else name
    items: list[tuple[int, str, str, str | None, int | None]] = []
    content_ids = _content_ids_for_collection(conn, int(collection_id), include_culled, False)
    photos = []
    for cid in content_ids:
        photo = conn.execute(_primary_photo_sql(culled), (cid,)).fetchone()
        if photo:
            photos.append(dict(photo))
    for photo in photos:
        items.append(
            (
                int(photo["id"]),
                photo["path"] or "",
                here,
                photo.get("taken_at"),
                int(photo["content_id"]) if photo.get("content_id") is not None else None,
            )
        )
    for cid in _child_ids(conn, collection_id):
        items.extend(_walk_export_nodes(conn, cid, here, culled, include_culled))
    return items


def export_collection(
    collection_id: int,
    dest: Path,
    *,
    include_culled: bool = False,
    write_mtime: bool = True,
    add_archive: bool = True,
) -> dict[str, Any]:
    """Copy a collection tree to dest/<name>/ and register it as a backup archive.

    Incremental catch-up is ``backup.sync_backup`` (same copy path).
    ``add_archive=False`` is a one-shot dump with no archive row.
    """
    from fotoman import backup, indexer

    culled = _culled_sql(include_culled, "p")
    with db() as conn:
        folder = _collection_row(conn, collection_id)
        if not folder:
            raise KeyError("not found")
        root_name = _safe_fs_name(folder["name"])
        items = (
            []
            if add_archive
            else _walk_export_nodes(conn, collection_id, "", culled, include_culled)
        )
    root = dest / root_name
    root.mkdir(parents=True, exist_ok=True)
    if add_archive:
        existing = backup.get_source_by_path(str(root))
        if existing and not backup.is_backup(existing):
            raise ValueError("Destination is already a watched volume")
        if existing:
            backup.bind_backup(int(existing["id"]), int(collection_id))
            return backup.sync_backup(
                int(existing["id"]),
                include_culled=include_culled,
                write_mtime=write_mtime,
            )
        src = indexer.add_linked_archive(
            str(root),
            folder["name"],
            [],
            kind="backup",
            backup_collection_id=int(collection_id),
        )
        return backup.sync_backup(
            int(src["id"]),
            include_culled=include_culled,
            write_mtime=write_mtime,
        )
    exported = 0
    skipped = 0
    failed = 0
    errors: list[dict] = []
    for pid, src_s, rel_dir, taken_at, content_id in items:
        out_dir = dest / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        if not src_s:
            skipped += 1
            continue
        src = Path(src_s)
        orig_name = src.name
        if not src.is_file():
            from fotoman import device

            local = device.readable_path(src_s)
            if not local:
                skipped += 1
                continue
            src = local
        try:
            out = _unique_export_path(out_dir, orig_name)
            shutil.copy2(src, out)
            if write_mtime and taken_at:
                try:
                    write_taken_at_mtime(out, taken_at)
                except Exception:
                    pass
            exported += 1
        except OSError as e:
            failed += 1
            if len(errors) < 5:
                errors.append({"photo_id": pid, "error": str(e)})
    return {
        "exported": exported,
        "skipped": skipped,
        "failed": failed,
        "dest": str(root),
        "errors": errors,
        "archive": None,
    }


def _unique_export_path(dest_dir: Path, name: str) -> Path:
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    n = 2
    while True:
        candidate = dest_dir / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def photo_paths_for_export(
    collection_id: int,
    include_culled: bool = False,
) -> list[tuple[int, str]]:
    """One live file path per unique content in the folder (no gallery limit)."""
    culled = _culled_sql(include_culled, "p")
    with db() as conn:
        folder = _collection_row(conn, collection_id)
        if not folder:
            raise KeyError("not found")
        cids = _content_ids_for_collection(conn, collection_id, include_culled, True)
        if not cids:
            return []
        placeholders = ",".join("?" * len(cids))
        rows = conn.execute(
            f"""
            WITH keepers AS (
              SELECT p.content_id, MIN(p.id) AS photo_id
              FROM photos p
              WHERE p.content_id IN ({placeholders}) AND {culled}
              GROUP BY p.content_id
            )
            SELECT p.id, p.path
            FROM keepers k
            JOIN photos p ON p.id = k.photo_id
            """,
            cids,
        ).fetchall()
    return [(int(r["id"]), r["path"]) for r in rows]
