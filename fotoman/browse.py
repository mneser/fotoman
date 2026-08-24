"""Browse views: folder tree, date tree, location tree, metadata groupings, and similarity clusters.

Folder view scans mounted archives on disk and falls back to the index when a
drive is unplugged (offline). Date view still reads enabled sources from the
filesystem. Camera / location / album / clusters use the indexed photos table.
Location tree nests countries under continents from countries.json.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fotoman.aspect import ASPECT_CLASS_SQL, ASPECT_CLASSES, ASPECT_LABELS
from fotoman.config import IMAGE_EXTS, ROOT, THUMBS_DIR
from fotoman.db import PHOTO_SELECT, db
from fotoman.metadata import make_thumbnail

_COUNTRIES_JSON = ROOT / "countries.json"


def _load_region_map() -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return (country_lower → region, region → canonical country names).

    Uses ``countries.json`` continent / oceanic groupings plus Aliases.
    """
    from fotoman.geocode import normalize_country

    if not _COUNTRIES_JSON.is_file():
        return {}, {}
    data = json.loads(_COUNTRIES_JSON.read_text())
    aliases = data.get("Aliases") or {}
    country_to_region: dict[str, str] = {}
    region_countries: dict[str, list[str]] = {}
    for region, names in data.items():
        if region == "Aliases" or not isinstance(names, list):
            continue
        canon_list: list[str] = []
        for name in names:
            canon = normalize_country(name) or str(name).strip()
            if not canon:
                continue
            country_to_region[canon.lower()] = region
            if canon not in canon_list:
                canon_list.append(canon)
        region_countries[region] = canon_list
    for alias, target in aliases.items():
        canon = normalize_country(str(target)) or str(target).strip()
        region = country_to_region.get(canon.lower())
        if not region:
            continue
        country_to_region[str(alias).lower()] = region
        alias_canon = normalize_country(str(alias))
        if alias_canon:
            country_to_region[alias_canon.lower()] = region
    return country_to_region, region_countries


def _sources(source_id: int | None = None) -> list[dict[str, Any]]:
    with db() as conn:
        if source_id is not None:
            rows = conn.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM sources
                WHERE enabled = 1 AND IFNULL(kind, 'watched') != 'backup'
                ORDER BY id
                """
            ).fetchall()
    return [dict(r) for r in rows]


def _culled_sql(include_culled: bool, alias: str = "") -> str:
    from fotoman.device import vault_live_sql_subquery

    col = f"{alias}.culled" if alias else "culled"
    live = vault_live_sql_subquery(alias or "p")
    if include_culled:
        return live
    return f"{col} = 0 AND {live}"


def _removed_paths() -> set[str]:
    """Absolute paths of photos in the removed."""
    with db() as conn:
        rows = conn.execute("SELECT path FROM photos WHERE culled = 1").fetchall()
    return {r["path"] for r in rows}


def _indexed_taken_at(include_culled: bool = False) -> dict[str, str]:
    """path -> taken_at for indexed photos."""
    with db() as conn:
        rows = conn.execute(
            f"""SELECT p.path, c.taken_at
               FROM photos p
               JOIN photo_contents c ON c.id = p.content_id
               WHERE {_culled_sql(include_culled, "p")}
                 AND c.taken_at IS NOT NULL AND c.taken_at != ''"""
        ).fetchall()
    return {r["path"]: r["taken_at"] for r in rows}


def resolve_source_file(source_id: int, rel: str) -> Path:
    """Resolve a relative path under a source; raises ValueError if invalid."""
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    if not row:
        raise ValueError("Source not found")
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise ValueError("Invalid path")
    from fotoman import replica

    src = dict(row)
    found = replica.readable_file(src, rel)
    if found is None or not found.is_file() or found.suffix.lower() not in IMAGE_EXTS:
        raise ValueError("Not an image file")
    return found


def browse_thumb_file(source_id: int, rel: str) -> Path:
    src = resolve_source_file(source_id, rel)
    st = src.stat()
    key = hashlib.sha1(
        f"{source_id}:{rel}:{st.st_mtime_ns}:{st.st_size}".encode()
    ).hexdigest()
    dest = THUMBS_DIR / "browse" / f"{key}.jpg"
    if not dest.exists():
        make_thumbnail(src, dest)
    return dest


def _iter_images(root: Path, *, recursive: bool = True):
    if not root.exists():
        return
    it = root.rglob("*") if recursive else root.iterdir()
    for p in it:
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() in IMAGE_EXTS:
            yield p


def _rel_of(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _walk_root(src: dict[str, Any]) -> Path | None:
    from fotoman import replica

    return replica.live_root(src)


def _canonical_path(src: dict[str, Any], walked: Path, path: Path) -> str:
    return str(Path(src["path"]).expanduser() / _rel_of(walked, path))


def _date_for_file(
    path: Path, indexed: dict[str, str], canonical: str | None = None
) -> str | None:
    """Prefer indexed EXIF date; otherwise file mtime. No on-the-fly EXIF."""
    taken = (indexed.get(canonical) if canonical else None) or indexed.get(str(path))
    if taken:
        return taken
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(sep=" ")
    except OSError:
        return None


def _enrich_from_db(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return items
    paths = [i["path"] for i in items]
    placeholders = ",".join("?" * len(paths))
    with db() as conn:
        rows = conn.execute(
            f"{PHOTO_SELECT} WHERE p.path IN ({placeholders})",
            paths,
        ).fetchall()
    by_path = {r["path"]: dict(r) for r in rows}
    for item in items:
        row = by_path.get(item["path"])
        if not row:
            continue
        item["id"] = row["id"]
        item["culled"] = row.get("culled", 0)
        if row.get("content_id") is not None:
            item["content_id"] = int(row["content_id"])
        if row.get("group_id") is not None:
            item["group_id"] = row["group_id"]
        if row.get("is_keeper") is not None:
            item["is_keeper"] = row["is_keeper"]
        if row.get("source_label"):
            item["source_label"] = row["source_label"]
        if row.get("taken_at"):
            item["taken_at"] = row["taken_at"]
        if row.get("place_label"):
            item["place_label"] = row["place_label"]
        if row.get("album"):
            item["album"] = row["album"]
        if row.get("camera"):
            item["camera"] = row["camera"]
        if row.get("size") is not None:
            item["size"] = row["size"]
        if row.get("width") is not None:
            item["width"] = row["width"]
        if row.get("height") is not None:
            item["height"] = row["height"]
        if row.get("lat") is not None:
            item["lat"] = row["lat"]
        if row.get("lon") is not None:
            item["lon"] = row["lon"]
        if row.get("orientation") is not None:
            item["orientation"] = int(row["orientation"] or 0)
        if row.get("aspect_class"):
            item["aspect_class"] = row["aspect_class"]
        if row.get("mirrored"):
            item["mirrored"] = True
    return items


def _empty_folder_node() -> dict[str, Any]:
    return {"__count": 0, "__direct": 0, "__children": {}, "__files": []}


def _add_rel_to_folder_node(src_node: dict[str, Any], rel: str) -> None:
    parts = Path(str(rel).replace("\\", "/")).parts
    if not parts:
        return
    node = src_node
    for seg in parts[:-1]:
        node["__count"] += 1
        node = node["__children"].setdefault(seg, _empty_folder_node())
    node["__count"] += 1
    node["__direct"] = node.get("__direct", 0) + 1


def _fill_folder_node_from_db(
    src_node: dict[str, Any], source_id: int, include_culled: bool
) -> None:
    culled = "1=1" if include_culled else "culled = 0"
    with db() as conn:
        rows = conn.execute(
            f"SELECT rel_path FROM photos WHERE source_id = ? AND {culled}",
            (int(source_id),),
        ).fetchall()
    for r in rows:
        rel = (r["rel_path"] or "").strip()
        if rel:
            _add_rel_to_folder_node(src_node, rel)


def folder_tree(
    source_id: int | None = None,
    include_files: bool = False,
    include_culled: bool = False,
) -> list[dict[str, Any]]:
    """Nested folder structure per archive.

    Mounted archives scan the disk (works before index). Unmounted archives
    keep their last indexed tree and are marked ``offline``.
    """
    tree: dict[str, Any] = {}
    label_ids: dict[str, int] = {}
    skip = set() if include_culled else _removed_paths()

    from fotoman import replica

    for src in _sources(source_id):
        root = Path(src["path"]).expanduser()
        label = src["label"] or root.name or str(root)
        label_ids[label] = src["id"]
        src_node = tree.setdefault(label, _empty_folder_node())
        walked = replica.live_root(src)
        if walked is None:
            src_node["__offline"] = not replica.source_exists(src)
            _fill_folder_node_from_db(src_node, int(src["id"]), include_culled)
            continue
        src_node["__offline"] = False
        walked = walked.resolve()
        for path in _iter_images(walked):
            canonical = _canonical_path(src, walked, path)
            if canonical in skip or str(path) in skip:
                continue
            parts = Path(_rel_of(walked, path)).parts
            node = src_node
            for seg in parts[:-1]:
                node["__count"] += 1
                node = node["__children"].setdefault(seg, _empty_folder_node())
            node["__count"] += 1
            node["__direct"] = node.get("__direct", 0) + 1
            if include_files:
                node.setdefault("__files", []).append(parts[-1])

    return _serialize_tree(tree, label_ids)


def _serialize_tree(
    tree: dict[str, Any], label_ids: dict[str, int]
) -> list[dict[str, Any]]:
    result = []
    for name, data in sorted(tree.items()):
        sid = label_ids[name]
        offline = bool(data.get("__offline"))
        entry: dict[str, Any] = {
            "name": name,
            "path": f"{sid}:",
            "count": data["__count"],
            "direct_count": data.get("__direct", 0),
            "offline": offline,
        }
        children = _serialize_subtree(data, sid, "", offline=offline)
        if children:
            entry["children"] = children
        result.append(entry)
    return result


def _serialize_subtree(
    data: dict[str, Any], source_id: int, prefix: str, *, offline: bool = False
) -> list[dict[str, Any]]:
    result = []
    for name, child in sorted(data.get("__children", {}).items()):
        rel = f"{prefix}/{name}" if prefix else name
        entry: dict[str, Any] = {
            "name": name,
            "path": f"{source_id}:{rel}",
            "count": child["__count"],
            "direct_count": child.get("__direct", 0),
            "offline": offline,
        }
        kids = _serialize_subtree(child, source_id, rel, offline=offline)
        if kids:
            entry["children"] = kids
        result.append(entry)
    for fname in sorted(data.get("__files") or []):
        rel = f"{prefix}/{fname}" if prefix else fname
        result.append({
            "name": fname,
            "path": f"{source_id}:{rel}",
            "count": 1,
            "direct_count": 1,
            "is_file": True,
            "offline": offline,
        })
    return result


def _parse_folder_key(key: str) -> tuple[int, str]:
    if ":" not in key:
        raise ValueError("Invalid folder key")
    sid_s, _, rel = key.partition(":")
    return int(sid_s), rel


def _db_photos_in_folder(
    source_id: int,
    rel: str,
    limit: int,
    include_culled: bool = False,
    include_subfolders: bool = True,
) -> list[dict[str, Any]]:
    """Indexed locations for an archive folder, including missing/offline copies."""
    live = "1=1" if include_culled else "p.culled = 0"
    rel = str(rel or "").replace("\\", "/").strip("/")
    clauses = [f"p.source_id = ?", live]
    params: list[Any] = [int(source_id)]
    if not rel:
        if not include_subfolders:
            clauses.append("instr(replace(p.rel_path, '\\', '/'), '/') = 0")
    else:
        prefix = rel + "/"
        if include_subfolders:
            clauses.append("replace(p.rel_path, '\\', '/') LIKE ?")
            params.append(prefix.replace("%", "\\%") + "%")
        else:
            clauses.append(
                "replace(p.rel_path, '\\', '/') LIKE ? "
                "AND instr(substr(replace(p.rel_path, '\\', '/'), ?), '/') = 0"
            )
            params.extend([prefix.replace("%", "\\%") + "%", len(prefix) + 1])
    where = " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(
            f"""{PHOTO_SELECT}
               WHERE {where}
               ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
               LIMIT ?""",
            [*params, int(limit)],
        ).fetchall()
    return [dict(r) for r in rows]


def _fs_photos_in_folder(
    key: str,
    limit: int,
    include_culled: bool = False,
    include_subfolders: bool = True,
) -> list[dict[str, Any]]:
    source_id, rel = _parse_folder_key(key)
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    if not row:
        return []
    from fotoman import replica

    src = dict(row)
    walked = replica.live_root(src)
    if walked is None:
        return _db_photos_in_folder(
            source_id,
            rel,
            limit,
            include_culled=include_culled,
            include_subfolders=include_subfolders,
        )
    root = Path(row["path"]).expanduser()
    source_label = row["label"] or root.name or str(root)
    walked = walked.resolve()
    base = (walked / rel).resolve() if rel else walked
    try:
        base.relative_to(walked)
    except ValueError:
        return []
    if not base.exists():
        return _db_photos_in_folder(
            source_id,
            rel,
            limit,
            include_culled=include_culled,
            include_subfolders=include_subfolders,
        )

    skip = set() if include_culled else _removed_paths()

    if base.is_file():
        if base.suffix.lower() not in IMAGE_EXTS:
            return []
        try:
            file_rel = _rel_of(walked, base)
            canonical = str(root / file_rel)
        except ValueError:
            return []
        if canonical in skip or str(base) in skip:
            return []
        try:
            mtime = base.stat().st_mtime
        except OSError:
            return []
        return _enrich_from_db([{
            "id": None,
            "source_id": source_id,
            "source_label": source_label,
            "path": canonical,
            "rel_path": file_rel,
            "taken_at": datetime.fromtimestamp(mtime).isoformat(sep=" "),
        }])

    if not base.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for path in _iter_images(base, recursive=include_subfolders):
        try:
            file_rel = _rel_of(walked, path)
            canonical = str(root / file_rel)
            mtime = path.stat().st_mtime
        except (ValueError, OSError):
            continue
        if canonical in skip or str(path) in skip:
            continue
        items.append({
            "id": None,
            "source_id": source_id,
            "source_label": source_label,
            "path": canonical,
            "rel_path": file_rel,
            "taken_at": datetime.fromtimestamp(mtime).isoformat(sep=" "),
            "_mtime": mtime,
        })

    items.sort(key=lambda x: (x["_mtime"], x["rel_path"]), reverse=True)
    out = items[:limit]
    for item in out:
        item.pop("_mtime", None)
    return _enrich_from_db(out)


_MONTH_NAMES = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def date_tree(
    include_culled: bool = False,
    *,
    newest_first: bool = True,
) -> list[dict[str, Any]]:
    """Return nested year → month → day tree from filesystem dates.

    Month/day children are only created when there are multiple subgroups.
    """
    indexed = _indexed_taken_at(include_culled=include_culled)
    skip = set() if include_culled else _removed_paths()
    # year -> {__count, __children: month -> {__count, __children: day -> {__count}}}
    tree: dict[str, Any] = {}

    def add_taken(taken: str | None) -> None:
        if not taken or len(taken) < 10:
            return
        year, month, day = taken[:4], taken[5:7], taken[8:10]
        y_node = tree.setdefault(year, {"__count": 0, "__children": {}})
        y_node["__count"] += 1
        m_node = y_node["__children"].setdefault(
            month, {"__count": 0, "__children": {}}
        )
        m_node["__count"] += 1
        d_node = m_node["__children"].setdefault(day, {"__count": 0})
        d_node["__count"] += 1

    for src in _sources():
        if _walk_root(src) is None:
            with db() as conn:
                rows = conn.execute(
                    f"""
                    SELECT c.taken_at
                    FROM photos p
                    JOIN photo_contents c ON c.id = p.content_id
                    WHERE p.source_id = ?
                      AND {_culled_sql(include_culled, "p")}
                      AND c.taken_at IS NOT NULL AND c.taken_at != ''
                    """,
                    (int(src["id"]),),
                ).fetchall()
            for r in rows:
                add_taken(r["taken_at"])
            continue
        walked = _walk_root(src)
        assert walked is not None
        walked = walked.resolve()
        for path in _iter_images(walked):
            canonical = _canonical_path(src, walked, path)
            if canonical in skip or str(path) in skip:
                continue
            add_taken(_date_for_file(path, indexed, canonical))

    reverse = bool(newest_first)
    result: list[dict[str, Any]] = []
    for year in sorted(tree.keys(), reverse=reverse):
        y_data = tree[year]
        month_keys = sorted(y_data["__children"].keys(), reverse=reverse)
        months: list[dict[str, Any]] = []
        # Nest months only when a year has multiple months
        if len(month_keys) >= 2:
            for month in month_keys:
                m_data = y_data["__children"][month]
                day_keys = sorted(m_data["__children"].keys(), reverse=reverse)
                month_num = int(month) if month.isdigit() else 0
                month_entry: dict[str, Any] = {
                    "name": _MONTH_NAMES[month_num] if 1 <= month_num <= 12 else month,
                    "path": f"{year}-{month}",
                    "count": m_data["__count"],
                    "direct_count": 0 if len(day_keys) >= 2 else m_data["__count"],
                }
                # Nest days only when a month has multiple days
                if len(day_keys) >= 2:
                    month_entry["children"] = [
                        {
                            "name": day,
                            "path": f"{year}-{month}-{day}",
                            "count": m_data["__children"][day]["__count"],
                            "direct_count": m_data["__children"][day]["__count"],
                        }
                        for day in day_keys
                    ]
                months.append(month_entry)
        year_entry: dict[str, Any] = {
            "name": year,
            "path": year,
            "count": y_data["__count"],
            "direct_count": 0 if months else y_data["__count"],
        }
        if months:
            year_entry["children"] = months
        result.append(year_entry)
    return result


def _db_dated_photos(
    source_id: int, include_culled: bool = False
) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            f"""
            {PHOTO_SELECT}
            WHERE p.source_id = ?
              AND {_culled_sql(include_culled, "p")}
              AND c.taken_at IS NOT NULL AND c.taken_at != ''
            ORDER BY c.taken_at DESC, p.id DESC
            """,
            (int(source_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def _date_groups_from_fs() -> list[dict[str, Any]]:
    """Flat YYYY-MM list (kept for /api/browse/groups?group_by=date)."""
    indexed = _indexed_taken_at()
    counts: dict[str, int] = {}
    for src in _sources():
        if _walk_root(src) is None:
            for row in _db_dated_photos(int(src["id"])):
                taken = str(row.get("taken_at") or "")
                if len(taken) >= 7:
                    month = taken[:7]
                    counts[month] = counts.get(month, 0) + 1
            continue
        walked = _walk_root(src)
        assert walked is not None
        walked = walked.resolve()
        for path in _iter_images(walked):
            canonical = _canonical_path(src, walked, path)
            taken = _date_for_file(path, indexed, canonical)
            if not taken or len(taken) < 7:
                continue
            month = taken[:7]
            counts[month] = counts.get(month, 0) + 1
    return [
        {"key": k, "count": counts[k]}
        for k in sorted(counts.keys(), reverse=True)
    ]


def _fs_photos_for_date(
    key: str,
    limit: int,
    include_culled: bool = False,
    include_subfolders: bool = True,
) -> list[dict[str, Any]]:
    """Photos matching a year (YYYY), month (YYYY-MM), or day (YYYY-MM-DD) key.

    Empty ``key`` returns every dated photo (primary Dates branch).

    When ``include_subfolders`` is False, omit photos that belong under child
    tree nodes (same nesting rules as ``date_tree``): a year with multiple
    months, or a month with multiple days, returns no direct leaves.
    """
    indexed = _indexed_taken_at(include_culled=include_culled)
    skip = set() if include_culled else _removed_paths()
    items: list[dict[str, Any]] = []
    # For non-recursive year/month keys, discover whether children would nest.
    child_keys: set[str] = set()
    for src in _sources():
        if _walk_root(src) is None:
            for row in _db_dated_photos(int(src["id"]), include_culled):
                taken = str(row.get("taken_at") or "")
                if not taken:
                    continue
                if key and not taken.startswith(key):
                    continue
                if not include_subfolders and key:
                    if len(key) == 4 and len(taken) >= 7:
                        child_keys.add(taken[:7])
                    elif len(key) == 7 and len(taken) >= 10:
                        child_keys.add(taken[:10])
                items.append(row)
            continue
        walked = _walk_root(src)
        assert walked is not None
        walked = walked.resolve()
        primary = Path(src["path"]).expanduser()
        for path in _iter_images(walked):
            try:
                file_rel = _rel_of(walked, path)
                canonical = str(primary / file_rel)
            except ValueError:
                continue
            if canonical in skip or str(path) in skip:
                continue
            taken = _date_for_file(path, indexed, canonical)
            if not taken:
                continue
            if key and not taken.startswith(key):
                continue
            if not include_subfolders and key:
                if len(key) == 4 and len(taken) >= 7:
                    child_keys.add(taken[:7])
                elif len(key) == 7 and len(taken) >= 10:
                    child_keys.add(taken[:10])
            items.append({
                "id": None,
                "source_id": src["id"],
                "source_label": src["label"] or primary.name or str(primary),
                "path": canonical,
                "rel_path": file_rel,
                "taken_at": taken,
            })
    if not include_subfolders and key and len(key) in (4, 7) and len(child_keys) >= 2:
        # Parent has nested children in the tree — no direct gallery leaves.
        return []
    items.sort(key=lambda x: (x.get("taken_at") or "", x["rel_path"]), reverse=True)
    return _enrich_from_db(items[:limit])


def _fs_photos_all_folders(
    limit: int,
    include_culled: bool = False,
    include_subfolders: bool = True,
) -> list[dict[str, Any]]:
    """All images across enabled archives (primary Folders branch)."""
    skip = set() if include_culled else _removed_paths()
    items: list[dict[str, Any]] = []
    for src in _sources():
        if _walk_root(src) is None:
            items.extend(
                _db_photos_in_folder(
                    int(src["id"]),
                    "",
                    limit,
                    include_culled=include_culled,
                    include_subfolders=include_subfolders,
                )
            )
            continue
        walked = _walk_root(src)
        if walked is None:
            continue
        walked = walked.resolve()
        primary = Path(src["path"]).expanduser()
        source_label = src["label"] or primary.name or str(primary)
        for path in _iter_images(walked, recursive=include_subfolders):
            try:
                file_rel = _rel_of(walked, path)
                canonical = str(primary / file_rel)
                mtime = path.stat().st_mtime
            except (ValueError, OSError):
                continue
            if canonical in skip or str(path) in skip:
                continue
            items.append({
                "id": None,
                "source_id": src["id"],
                "source_label": source_label,
                "path": canonical,
                "rel_path": file_rel,
                "taken_at": datetime.fromtimestamp(mtime).isoformat(sep=" "),
                "_mtime": mtime,
            })
    items.sort(key=lambda x: (x["_mtime"], x["rel_path"]), reverse=True)
    out = items[:limit]
    for item in out:
        item.pop("_mtime", None)
    return _enrich_from_db(out)


_HAS_PLACE_SQL = """(
    (c.country IS NOT NULL AND c.country != '')
    OR (c.city IS NOT NULL AND c.city != '')
    OR (c.place_label IS NOT NULL AND c.place_label != '')
)"""
_NO_PLACE_SQL = """(
    (c.country IS NULL OR c.country = '')
    AND (c.city IS NULL OR c.city = '')
    AND (c.place_label IS NULL OR c.place_label = '')
)"""


def _photos_with_place(
    limit: int, include_culled: bool = False
) -> list[dict[str, Any]]:
    """All photos with any place/country/city set (primary Places branch)."""
    with db() as conn:
        rows = conn.execute(
            f"""{PHOTO_SELECT}
                WHERE {_culled_sql(include_culled, "p")}
                  AND {_HAS_PLACE_SQL}
                ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _photos_without_place(
    limit: int, include_culled: bool = False
) -> list[dict[str, Any]]:
    """Photos with no place_label / country / city (unlabeled Places branch)."""
    with db() as conn:
        rows = conn.execute(
            f"""{PHOTO_SELECT}
                WHERE {_culled_sql(include_culled, "p")}
                  AND {_NO_PLACE_SQL}
                ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _collapse_pass_through(node: dict[str, Any]) -> dict[str, Any]:
    """Fold a sole child into the parent name, keeping the child's query path.

    Recursive. Examples:
      Laos → Louangphabang → venue  →  ``Laos / Louangphabang`` → venue
      UAE → Dubai (leaf)             →  ``United Arab Emirates / Dubai``

    The merged node adopts the child's ``path`` / ``kind`` / counts so gallery
    scope (including nested-off direct leaves) matches the deepest folded
    place. A sole leaf is only folded when the parent has no direct photos of
    its own — otherwise the leaf stays a child (city directs + one venue).
    """
    children = node.get("children")
    if not children:
        return node
    collapsed = [_collapse_pass_through(dict(c)) for c in children]
    while len(collapsed) == 1:
        mid = collapsed[0]
        mid_children = list(mid.get("children") or [])
        parent_direct = int(node.get("direct_count") or 0)
        if mid_children:
            if parent_direct > 0:
                break
            node["name"] = f"{node['name']} / {mid['name']}"
            node["path"] = mid["path"]
            node["kind"] = mid["kind"]
            node["count"] = mid["count"]
            node["direct_count"] = int(mid.get("direct_count") or 0)
            collapsed = mid_children
        elif parent_direct == 0:
            node["name"] = f"{node['name']} / {mid['name']}"
            node["path"] = mid["path"]
            node["kind"] = mid["kind"]
            node["count"] = mid["count"]
            node["direct_count"] = int(mid.get("direct_count") or mid["count"] or 0)
            collapsed = []
        else:
            break
    if collapsed:
        node["children"] = collapsed
    else:
        node.pop("children", None)
    return node


def _place_tree_parts(label: str) -> list[str]:
    """Country-first path from a reverse-hierarchy Place label."""
    raw = [p.strip() for p in str(label).split(",") if p.strip()]
    if not raw:
        return []
    parts: list[str] = []
    for name in reversed(raw):
        if not parts or parts[-1].lower() != name.lower():
            parts.append(name)
    return parts


def _place_trie_bump(trie: dict[str, Any], parts: list[str], count: int) -> None:
    node = trie
    for i, name in enumerate(parts):
        child = node["children"].setdefault(
            name, {"count": 0, "direct": 0, "children": {}}
        )
        child["count"] += count
        if i == len(parts) - 1:
            child["direct"] += count
        node = child


def _nodes_from_place_trie(
    children: dict[str, Any], prefix: list[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, data in sorted(
        children.items(), key=lambda x: (-x[1]["count"], x[0].lower())
    ):
        path_names = prefix + [name]
        suffix = ", ".join(reversed(path_names))
        node: dict[str, Any] = {
            "name": name,
            "path": f"place-suffix::{suffix}",
            "kind": "place",
            "count": data["count"],
            "direct_count": data["direct"],
        }
        nested = _nodes_from_place_trie(data["children"], path_names)
        if nested:
            node["children"] = nested
        out.append(node)
    return out


def location_tree(include_culled: bool = False) -> list[dict[str, Any]]:
    """Nest Places by reversed Place labels (country → … → venue).

    ``Quebec, Hawaii, United States`` becomes United States → Hawaii → Quebec,
    matching folder-style Place edits. Continents still wrap known countries.
    A sole child is folded into the parent display name (``Parent / Child``).
    """
    from fotoman.geocode import _format_place

    with db() as conn:
        labeled = conn.execute(
            f"""SELECT c.place_label AS label, COUNT(*) AS count
               FROM photos p
               JOIN photo_contents c ON c.id = p.content_id
               JOIN sources s ON s.id = p.source_id
               WHERE {_culled_sql(include_culled, 'p')}
                 AND c.place_label IS NOT NULL AND c.place_label != ''
               GROUP BY c.place_label"""
        ).fetchall()
        structured = conn.execute(
            f"""SELECT c.country, c.city, c.region, c.venue, COUNT(*) AS count
               FROM photos p
               JOIN photo_contents c ON c.id = p.content_id
               JOIN sources s ON s.id = p.source_id
               WHERE {_culled_sql(include_culled, 'p')}
                 AND (c.place_label IS NULL OR c.place_label = '')
                 AND c.country IS NOT NULL AND c.country != ''
               GROUP BY c.country, c.city, c.region, c.venue"""
        ).fetchall()

    trie: dict[str, Any] = {"children": {}}
    for r in labeled:
        parts = _place_tree_parts(r["label"] or "")
        if parts:
            _place_trie_bump(trie, parts, int(r["count"]))
    for r in structured:
        label = _format_place(
            r["city"], r["region"], r["country"], venue=r["venue"]
        )
        parts = _place_tree_parts(label or r["country"] or "")
        if parts:
            _place_trie_bump(trie, parts, int(r["count"]))

    top_nodes = _nodes_from_place_trie(trie["children"], [])
    country_to_region, _ = _load_region_map()
    by_region: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for node in top_nodes:
        region = country_to_region.get(node["name"].lower())
        if region:
            by_region.setdefault(region, []).append(node)
        else:
            ungrouped.append(node)

    result: list[dict[str, Any]] = []
    for region, nodes in by_region.items():
        nodes.sort(key=lambda x: (-x["count"], x["name"].lower()))
        result.append(
            {
                "name": region,
                "path": f"region::{region}",
                "kind": "region",
                "count": sum(n["count"] for n in nodes),
                "direct_count": 0,
                "children": nodes,
            }
        )
    result.extend(ungrouped)
    result = [_collapse_pass_through(n) for n in result]
    result.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return result


def _photos_for_location(
    key: str,
    limit: int,
    include_culled: bool = False,
    include_subfolders: bool = True,
) -> list[dict[str, Any]]:
    from fotoman.geocode import normalize_country

    if key == "unplaced":
        return _photos_without_place(limit, include_culled=include_culled)

    clauses = [_culled_sql(include_culled, "p")]
    params: list[Any] = []
    if key.startswith("region::"):
        if not include_subfolders:
            # Region nodes only aggregate countries — no direct leaves.
            return []
        region = key[len("region::") :]
        _, region_countries = _load_region_map()
        countries = region_countries.get(region) or []
        if not countries:
            return []
        parts: list[str] = []
        for country in countries:
            parts.append(
                """(
                    c.country = ?
                    OR c.place_label = ?
                    OR c.place_label LIKE ?
                )"""
            )
            params.extend([country, country, f"%, {country}"])
        clauses.append("(" + " OR ".join(parts) + ")")
    elif key.startswith("country::"):
        country = normalize_country(key[len("country::") :]) or key[len("country::") :]
        if include_subfolders:
            # Structured country, or freeform country / "…, Country" not yet normalized.
            clauses.append(
                """(
                    c.country = ?
                    OR (
                        (c.country IS NULL OR c.country = '')
                        AND (c.place_label = ? OR c.place_label LIKE ?)
                    )
                )"""
            )
            params.extend([country, country, f"%, {country}"])
        else:
            # Direct country leaves only — exclude city / region place nodes.
            clauses.append(
                """(
                    (
                        c.country = ?
                        AND (c.city IS NULL OR c.city = '')
                        AND (c.region IS NULL OR c.region = '')
                    )
                    OR (
                        (c.country IS NULL OR c.country = '')
                        AND c.place_label = ?
                    )
                )"""
            )
            params.extend([country, country])
    elif key.startswith("city::"):
        rest = key[len("city::") :]
        country, _, city = rest.partition("::")
        country = normalize_country(country) or country
        if include_subfolders:
            # City match, or admin-region place node (Hawaii) including venues.
            clauses.append(
                """(
                    (c.country = ? AND (
                        c.city = ?
                        OR (
                            (c.city IS NULL OR c.city = '')
                            AND c.region = ?
                        )
                    ))
                    OR (
                        (c.country IS NULL OR c.country = '')
                        AND (
                            c.place_label = ?
                            OR c.place_label LIKE ?
                        )
                    )
                )"""
            )
            params.extend(
                [
                    country,
                    city,
                    city,
                    f"{city}, {country}",
                    f"{city}, %, {country}",
                ]
            )
        else:
            # Direct place leaves only — exclude venue children.
            clauses.append(
                """(
                    c.country = ?
                    AND (c.venue IS NULL OR c.venue = '')
                    AND (
                        c.city = ?
                        OR (
                            (c.city IS NULL OR c.city = '')
                            AND c.region = ?
                        )
                    )
                )"""
            )
            params.extend([country, city, city])
    elif key.startswith("venue::"):
        rest = key[len("venue::") :]
        country, _, rest2 = rest.partition("::")
        city, _, venue = rest2.partition("::")
        country = normalize_country(country) or country
        clauses.append(
            """(
                c.country = ? AND c.venue = ?
                AND (
                    c.city = ?
                    OR (
                        (c.city IS NULL OR c.city = '')
                        AND c.region = ?
                    )
                )
            )"""
        )
        params.extend([country, venue, city, city])
    elif key.startswith("place::"):
        clauses.append("c.place_label = ?")
        params.append(key[len("place::") :])
    elif key.startswith("place-suffix::"):
        label = key[len("place-suffix::") :]
        if include_subfolders:
            clauses.append("(c.place_label = ? OR c.place_label LIKE ?)")
            params.extend([label, f"%, {label}"])
        else:
            clauses.append("c.place_label = ?")
            params.append(label)
    else:
        clauses.append("c.place_label = ?")
        params.append(key)

    where = " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(
            f"""{PHOTO_SELECT}
                WHERE {where}
                ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
                LIMIT ?""",
            [*params, limit],
        ).fetchall()
    return [dict(r) for r in rows]


def group_list(group_by: str, include_culled: bool = False) -> list[dict[str, Any]]:
    """Return distinct group keys with photo counts."""
    if group_by == "date":
        return _date_groups_from_fs()

    with db() as conn:
        if group_by == "aspect":
            rows = conn.execute(
                f"""SELECT {ASPECT_CLASS_SQL} AS key, COUNT(*) AS count
                    FROM photos p
                    JOIN photo_contents c ON c.id = p.content_id
                    WHERE {_culled_sql(include_culled, 'p')}
                      AND ({ASPECT_CLASS_SQL}) IS NOT NULL
                    GROUP BY {ASPECT_CLASS_SQL}""",
            ).fetchall()
            counts = {r["key"]: r["count"] for r in rows}
            return [
                {"key": k, "label": ASPECT_LABELS[k], "count": counts[k]}
                for k in ASPECT_CLASSES
                if counts.get(k)
            ]
        if group_by == "album":
            col = "p.album"
        elif group_by == "camera":
            col = "c.camera"
        else:
            col = "c.place_label"
        rows = conn.execute(
            f"""SELECT {col} AS key, COUNT(*) AS count
                FROM photos p
                JOIN photo_contents c ON c.id = p.content_id
                WHERE {_culled_sql(include_culled, 'p')}
                  AND {col} IS NOT NULL AND {col} != ''
                GROUP BY {col} ORDER BY count DESC""",
        ).fetchall()

    return [{"key": r["key"], "count": r["count"]} for r in rows]


def photos_for_group(
    group_by: str,
    key: str,
    limit: int = 200,
    include_culled: bool = False,
    include_subfolders: bool = True,
    labeled: bool | None = None,
) -> list[dict[str, Any]]:
    """Return photos matching a specific group key.

    ``key="all"`` means the primary branch: every photo in that category
    (used when “Show files in sub-folders” is on and the root is selected).
    """
    if key == "all":
        if group_by == "folder":
            return _fs_photos_all_folders(
                limit,
                include_culled=include_culled,
                include_subfolders=include_subfolders,
            )
        if group_by == "date":
            return _fs_photos_for_date("", limit, include_culled=include_culled)
        if group_by == "location":
            if labeled is False:
                return _photos_without_place(limit, include_culled=include_culled)
            return _photos_with_place(limit, include_culled=include_culled)
        if group_by == "person":
            from fotoman import faces

            return faces.photos_with_faces(
                limit, include_culled=include_culled, labeled=labeled
            )
        if group_by == "camera":
            with db() as conn:
                rows = conn.execute(
                    f"""{PHOTO_SELECT}
                        WHERE {_culled_sql(include_culled, "p")}
                          AND c.camera IS NOT NULL AND c.camera != ''
                        ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
                        LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        if group_by == "collection":
            from fotoman import collections

            by_id: dict[int, dict[str, Any]] = {}
            for folder in collections.list_collections(include_culled=include_culled):
                for p in collections.photos_for_collection(
                    int(folder["id"]),
                    limit=limit,
                    include_culled=include_culled,
                    include_subfolders=True,
                ):
                    pid = p.get("id")
                    if pid is not None:
                        by_id[int(pid)] = p
            items = sorted(
                by_id.values(),
                key=lambda p: (p.get("taken_at") or "", p.get("id") or 0),
                reverse=True,
            )
            return items[:limit]
        if group_by == "aspect":
            with db() as conn:
                rows = conn.execute(
                    f"""{PHOTO_SELECT}
                        WHERE {_culled_sql(include_culled, "p")}
                          AND ({ASPECT_CLASS_SQL}) IS NOT NULL
                        ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
                        LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        if group_by == "remove":
            pass  # fall through to remove handling below
        else:
            return []

    if group_by == "folder":
        try:
            return _fs_photos_in_folder(
                key,
                limit,
                include_culled=include_culled,
                include_subfolders=include_subfolders,
            )
        except ValueError:
            return []
    if group_by == "date":
        return _fs_photos_for_date(
            key,
            limit,
            include_culled=include_culled,
            include_subfolders=include_subfolders,
        )
    if group_by == "location":
        return _photos_for_location(
            key,
            limit,
            include_culled=include_culled,
            include_subfolders=include_subfolders,
        )
    if group_by == "person":
        from fotoman import faces

        try:
            return faces.photos_for_person(
                int(key), limit, include_culled=include_culled
            )
        except (TypeError, ValueError):
            return []
    if group_by == "collection":
        from fotoman import collections

        try:
            return collections.photos_for_collection(
                collections.resolve_collection_key(key),
                limit,
                include_culled=include_culled,
                include_subfolders=include_subfolders,
            )
        except (TypeError, ValueError):
            return []
    if group_by == "remove":
        with db() as conn:
            rows = conn.execute(
                f"""{PHOTO_SELECT}
                   WHERE p.culled = 1
                   ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    clauses = [_culled_sql(include_culled, "p")]
    params: list[Any] = []

    if group_by == "camera":
        clauses.append("c.camera = ?")
        params.append(key)
    elif group_by == "aspect":
        if key not in ASPECT_CLASSES:
            return []
        clauses.append(f"({ASPECT_CLASS_SQL}) = ?")
        params.append(key)
    elif group_by == "album":
        clauses.append("p.album = ?")
        params.append(key)
    elif group_by == "cluster":
        clauses.append("p.group_id = ?")
        params.append(int(key))

    where = " AND ".join(clauses)
    order = (
        "ORDER BY p.culled ASC, (COALESCE(c.width,0)*COALESCE(c.height,0)) DESC, c.size DESC, p.id DESC"
        if group_by == "cluster"
        else "ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC"
    )
    with db() as conn:
        rows = conn.execute(
            f"{PHOTO_SELECT} WHERE {where} {order} LIMIT ?",
            [*params, limit],
        ).fetchall()

    return [dict(r) for r in rows]


def cluster_list(
    threshold: float = 0.92,
    limit: int = 50,
    include_culled: bool = False,
) -> list[dict[str, Any]]:
    """Return existing duplicate groups as browsable clusters."""
    with db() as conn:
        groups = conn.execute(
            """SELECT g.id, g.kind, g.member_count, g.keeper_id
               FROM photo_groups g
               WHERE g.member_count >= 2
               ORDER BY g.member_count DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        result = []
        for g in groups:
            members = conn.execute(
                f"""{PHOTO_SELECT}
                   WHERE p.group_id = ? AND {_culled_sql(include_culled, 'p')}
                   ORDER BY p.culled ASC,
                            (COALESCE(c.width,0)*COALESCE(c.height,0)) DESC, c.size DESC,
                            p.is_keeper DESC""",
                (g["id"],),
            ).fetchall()
            if len(members) < 2:
                continue
            result.append({
                "id": g["id"],
                "kind": g["kind"],
                "member_count": len(members),
                "members": [dict(m) for m in members],
            })

    return result


def _find_tree_node(nodes: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    want = str(path)
    for node in nodes or []:
        if str(node.get("path")) == want:
            return node
        found = _find_tree_node(node.get("children") or [], want)
        if found:
            return found
    return None


def subtree_for_key(
    group_by: str,
    key: str,
    include_culled: bool = False,
) -> dict[str, Any] | None:
    """Source browse node (with children) to copy into a collection tree."""
    if not key:
        return None
    if group_by == "folder":
        source_id = None
        sid, sep, _rest = str(key).partition(":")
        if sep and sid.isdigit():
            source_id = int(sid)
        return _find_tree_node(
            folder_tree(
                source_id=source_id,
                include_files=False,
                include_culled=include_culled,
            ),
            key,
        )
    if group_by == "date":
        from fotoman.config import load_settings

        order = str(load_settings().get("date_tree_order") or "newest").lower()
        return _find_tree_node(
            date_tree(
                include_culled=include_culled,
                newest_first=order != "oldest",
            ),
            key,
        )
    if group_by == "location":
        return _find_tree_node(location_tree(include_culled=include_culled), key)
    if group_by == "camera":
        return {"name": key, "path": key, "children": []}
    if group_by == "aspect":
        if key not in ASPECT_CLASSES:
            return None
        return {"name": ASPECT_LABELS[key], "path": key, "children": []}
    if group_by == "person":
        from fotoman import faces

        try:
            pid = int(key)
        except (TypeError, ValueError):
            return None
        for person in faces.people_list():
            if int(person["id"]) == pid:
                return {
                    "name": person.get("display_name") or f"Person {pid}",
                    "path": str(pid),
                    "children": [],
                }
        return None
    return None
