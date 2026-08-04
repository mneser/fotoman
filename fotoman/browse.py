"""Browse views: folder tree, date tree, location tree, metadata groupings, and similarity bundles.

Folder and date views read enabled sources from the filesystem (no index required).
Camera / location / album / bundles still use the indexed photos table.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from fotoman.config import IMAGE_EXTS, THUMBS_DIR
from fotoman.db import PHOTO_SELECT, db
from fotoman.metadata import make_thumbnail


def _sources(source_id: int | None = None) -> list[dict[str, Any]]:
    with db() as conn:
        if source_id is not None:
            rows = conn.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sources WHERE enabled = 1 ORDER BY id"
            ).fetchall()
    return [dict(r) for r in rows]


def _culled_sql(include_culled: bool, alias: str = "") -> str:
    col = f"{alias}.culled" if alias else "culled"
    return "1=1" if include_culled else f"{col} = 0"


def _removed_paths() -> set[str]:
    """Absolute paths of photos in the remove bin."""
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
            "SELECT path FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
    if not row:
        raise ValueError("Source not found")
    root = Path(row["path"]).expanduser().resolve()
    # Normalize rel: strip leading slashes, reject traversal
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise ValueError("Invalid path")
    full = (root / rel_path).resolve()
    try:
        full.relative_to(root)
    except ValueError as e:
        raise ValueError("Path outside source") from e
    if not full.is_file() or full.suffix.lower() not in IMAGE_EXTS:
        raise ValueError("Not an image file")
    return full


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


def _date_for_file(path: Path, indexed: dict[str, str]) -> str | None:
    """Prefer indexed EXIF date; otherwise file mtime. No on-the-fly EXIF."""
    taken = indexed.get(str(path))
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
    return items


def folder_tree(
    source_id: int | None = None,
    include_files: bool = False,
    include_culled: bool = False,
) -> list[dict[str, Any]]:
    """Return nested folder structure by scanning source directories on disk.

    When ``include_files`` is True, image filenames appear as leaf nodes under
    their folders. Removed photos are omitted unless ``include_culled``.
    """
    tree: dict[str, Any] = {}
    # label -> source_id (first wins if duplicate labels)
    label_ids: dict[str, int] = {}
    skip = set() if include_culled else _removed_paths()

    for src in _sources(source_id):
        root = Path(src["path"]).expanduser().resolve()
        label = src["label"] or root.name or str(root)
        label_ids[label] = src["id"]
        src_node = tree.setdefault(
            label, {"__count": 0, "__children": {}, "__files": []}
        )
        if not root.exists():
            continue
        for path in _iter_images(root):
            if str(path) in skip:
                continue
            parts = Path(_rel_of(root, path)).parts
            node = src_node
            for seg in parts[:-1]:
                node["__count"] += 1
                node = node["__children"].setdefault(
                    seg, {"__count": 0, "__children": {}, "__files": []}
                )
            node["__count"] += 1
            if include_files:
                node.setdefault("__files", []).append(parts[-1])

    return _serialize_tree(tree, label_ids)


def _serialize_tree(
    tree: dict[str, Any], label_ids: dict[str, int]
) -> list[dict[str, Any]]:
    result = []
    for name, data in sorted(tree.items()):
        sid = label_ids[name]
        entry: dict[str, Any] = {
            "name": name,
            "path": f"{sid}:",
            "count": data["__count"],
        }
        children = _serialize_subtree(data, sid, "")
        if children:
            entry["children"] = children
        result.append(entry)
    return result


def _serialize_subtree(
    data: dict[str, Any], source_id: int, prefix: str
) -> list[dict[str, Any]]:
    result = []
    for name, child in sorted(data.get("__children", {}).items()):
        rel = f"{prefix}/{name}" if prefix else name
        entry: dict[str, Any] = {
            "name": name,
            "path": f"{source_id}:{rel}",
            "count": child["__count"],
        }
        kids = _serialize_subtree(child, source_id, rel)
        if kids:
            entry["children"] = kids
        result.append(entry)
    for fname in sorted(data.get("__files") or []):
        rel = f"{prefix}/{fname}" if prefix else fname
        result.append({
            "name": fname,
            "path": f"{source_id}:{rel}",
            "count": 1,
            "is_file": True,
        })
    return result


def _parse_folder_key(key: str) -> tuple[int, str]:
    if ":" not in key:
        raise ValueError("Invalid folder key")
    sid_s, _, rel = key.partition(":")
    return int(sid_s), rel


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
    root = Path(row["path"]).expanduser().resolve()
    source_label = row["label"] or root.name or str(root)
    base = (root / rel).resolve() if rel else root
    try:
        base.relative_to(root)
    except ValueError:
        return []
    if not base.exists():
        return []

    skip = set() if include_culled else _removed_paths()

    if base.is_file():
        if base.suffix.lower() not in IMAGE_EXTS:
            return []
        if str(base) in skip:
            return []
        try:
            mtime = base.stat().st_mtime
        except OSError:
            return []
        return _enrich_from_db([{
            "id": None,
            "source_id": source_id,
            "source_label": source_label,
            "path": str(base),
            "rel_path": _rel_of(root, base),
            "taken_at": datetime.fromtimestamp(mtime).isoformat(sep=" "),
        }])

    if not base.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for path in _iter_images(base, recursive=include_subfolders):
        if str(path) in skip:
            continue
        try:
            file_rel = _rel_of(root, path)
            mtime = path.stat().st_mtime
        except (ValueError, OSError):
            continue
        items.append({
            "id": None,
            "source_id": source_id,
            "source_label": source_label,
            "path": str(path),
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


def date_tree(include_culled: bool = False) -> list[dict[str, Any]]:
    """Return nested year → month → day tree from filesystem dates.

    Month/day children are only created when there are multiple subgroups
    (same rule as location cities under a country).
    """
    indexed = _indexed_taken_at(include_culled=include_culled)
    skip = set() if include_culled else _removed_paths()
    # year -> {__count, __children: month -> {__count, __children: day -> {__count}}}
    tree: dict[str, Any] = {}
    for src in _sources():
        root = Path(src["path"]).expanduser().resolve()
        for path in _iter_images(root):
            if str(path) in skip:
                continue
            taken = _date_for_file(path, indexed)
            if not taken or len(taken) < 10:
                continue
            year, month, day = taken[:4], taken[5:7], taken[8:10]
            y_node = tree.setdefault(year, {"__count": 0, "__children": {}})
            y_node["__count"] += 1
            m_node = y_node["__children"].setdefault(
                month, {"__count": 0, "__children": {}}
            )
            m_node["__count"] += 1
            d_node = m_node["__children"].setdefault(day, {"__count": 0})
            d_node["__count"] += 1

    result: list[dict[str, Any]] = []
    for year in sorted(tree.keys(), reverse=True):
        y_data = tree[year]
        month_keys = sorted(y_data["__children"].keys(), reverse=True)
        months: list[dict[str, Any]] = []
        # Nest months only when a year has multiple months
        if len(month_keys) >= 2:
            for month in month_keys:
                m_data = y_data["__children"][month]
                day_keys = sorted(m_data["__children"].keys(), reverse=True)
                month_num = int(month) if month.isdigit() else 0
                month_entry: dict[str, Any] = {
                    "name": _MONTH_NAMES[month_num] if 1 <= month_num <= 12 else month,
                    "path": f"{year}-{month}",
                    "count": m_data["__count"],
                }
                # Nest days only when a month has multiple days
                if len(day_keys) >= 2:
                    month_entry["children"] = [
                        {
                            "name": day,
                            "path": f"{year}-{month}-{day}",
                            "count": m_data["__children"][day]["__count"],
                        }
                        for day in day_keys
                    ]
                months.append(month_entry)
        year_entry: dict[str, Any] = {
            "name": year,
            "path": year,
            "count": y_data["__count"],
        }
        if months:
            year_entry["children"] = months
        result.append(year_entry)
    return result


def _date_groups_from_fs() -> list[dict[str, Any]]:
    """Flat YYYY-MM list (kept for /api/browse/groups?group_by=date)."""
    indexed = _indexed_taken_at()
    counts: dict[str, int] = {}
    for src in _sources():
        root = Path(src["path"]).expanduser().resolve()
        for path in _iter_images(root):
            taken = _date_for_file(path, indexed)
            if not taken or len(taken) < 7:
                continue
            month = taken[:7]
            counts[month] = counts.get(month, 0) + 1
    return [
        {"key": k, "count": counts[k]}
        for k in sorted(counts.keys(), reverse=True)
    ]


def _fs_photos_for_date(
    key: str, limit: int, include_culled: bool = False
) -> list[dict[str, Any]]:
    """Photos matching a year (YYYY), month (YYYY-MM), or day (YYYY-MM-DD) key."""
    indexed = _indexed_taken_at(include_culled=include_culled)
    skip = set() if include_culled else _removed_paths()
    items: list[dict[str, Any]] = []
    for src in _sources():
        root = Path(src["path"]).expanduser().resolve()
        for path in _iter_images(root):
            if str(path) in skip:
                continue
            taken = _date_for_file(path, indexed)
            if not taken or not taken.startswith(key):
                continue
            items.append({
                "id": None,
                "source_id": src["id"],
                "source_label": src["label"] or root.name or str(root),
                "path": str(path),
                "rel_path": _rel_of(root, path),
                "taken_at": taken,
            })
    items.sort(key=lambda x: (x.get("taken_at") or "", x["rel_path"]), reverse=True)
    return _enrich_from_db(items[:limit])


def location_tree(min_cities: int = 2, include_culled: bool = False) -> list[dict[str, Any]]:
    """Return country → city tree plus freeform place_label leaves (no country).

    Cities nest under a country only when it has at least ``min_cities``
    distinct cities; otherwise the country is a single leaf. Manual / directory
    ``City, Country`` labels (no structured country yet) are folded into the
    same tree; other freeform labels stay top-level leaves.
    """
    from fotoman.geocode import normalize_country, parse_city_country

    with db() as conn:
        rows = conn.execute(
            f"""SELECT c.country, c.city, COUNT(*) AS count
               FROM photos p
               JOIN photo_contents c ON c.id = p.content_id
               WHERE {_culled_sql(include_culled, 'p')}
                 AND c.country IS NOT NULL AND c.country != ''
               GROUP BY c.country, c.city"""
        ).fetchall()
        freeform = conn.execute(
            f"""SELECT c.place_label AS label, COUNT(*) AS count
               FROM photos p
               JOIN photo_contents c ON c.id = p.content_id
               WHERE {_culled_sql(include_culled, 'p')}
                 AND c.place_label IS NOT NULL AND c.place_label != ''
                 AND (c.country IS NULL OR c.country = '')
               GROUP BY c.place_label"""
        ).fetchall()

    countries: dict[str, dict[str, Any]] = {}
    for r in rows:
        country = normalize_country(r["country"]) or r["country"]
        city = (r["city"] or "").strip() or None
        entry = countries.setdefault(country, {"count": 0, "cities": {}})
        entry["count"] += r["count"]
        if city:
            cities: dict[str, int] = entry["cities"]
            cities[city] = cities.get(city, 0) + r["count"]

    leftover_freeform: list[dict[str, Any]] = []
    for r in freeform:
        label = (r["label"] or "").strip()
        if not label:
            continue
        parsed = parse_city_country(label)
        country = parsed.get("country")
        city = parsed.get("city")
        if country and city:
            entry = countries.setdefault(country, {"count": 0, "cities": {}})
            entry["count"] += int(r["count"])
            cities = entry["cities"]
            cities[city] = cities.get(city, 0) + int(r["count"])
        else:
            leftover_freeform.append(r)

    result: list[dict[str, Any]] = []
    for country, data in countries.items():
        node: dict[str, Any] = {
            "name": country,
            "path": f"country::{country}",
            "count": data["count"],
        }
        cities = data["cities"]
        if len(cities) >= min_cities:
            node["children"] = [
                {
                    "name": city,
                    "path": f"city::{country}::{city}",
                    "count": cnt,
                }
                for city, cnt in sorted(
                    cities.items(), key=lambda x: (-x[1], x[0].lower())
                )
            ]
        result.append(node)

    for r in leftover_freeform:
        label = (r["label"] or "").strip()
        result.append(
            {
                "name": label,
                "path": f"place::{label}",
                "count": int(r["count"]),
            }
        )

    result.sort(key=lambda x: (-x["count"], x["name"].lower()))
    return result


def _photos_for_location(
    key: str, limit: int, include_culled: bool = False
) -> list[dict[str, Any]]:
    from fotoman.geocode import normalize_country

    clauses = [_culled_sql(include_culled, "p")]
    params: list[Any] = []
    if key.startswith("country::"):
        country = normalize_country(key[len("country::") :]) or key[len("country::") :]
        # Structured country, or freeform "…, Country" not yet normalized.
        clauses.append(
            """(
                c.country = ?
                OR (
                    (c.country IS NULL OR c.country = '')
                    AND c.place_label LIKE ?
                )
            )"""
        )
        params.extend([country, f"%, {country}"])
    elif key.startswith("city::"):
        rest = key[len("city::") :]
        country, _, city = rest.partition("::")
        country = normalize_country(country) or country
        clauses.append(
            """(
                (c.country = ? AND c.city = ?)
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
                f"{city}, {country}",
                f"{city}, %, {country}",
            ]
        )
    elif key.startswith("place::"):
        clauses.append("c.place_label = ?")
        params.append(key[len("place::") :])
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
) -> list[dict[str, Any]]:
    """Return photos matching a specific group key."""
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
        return _fs_photos_for_date(key, limit, include_culled=include_culled)
    if group_by == "location":
        return _photos_for_location(key, limit, include_culled=include_culled)
    if group_by == "person":
        from fotoman import faces

        try:
            return faces.photos_for_person(
                int(key), limit, include_culled=include_culled
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
    elif group_by == "album":
        clauses.append("p.album = ?")
        params.append(key)
    elif group_by == "bundle":
        clauses.append("p.group_id = ?")
        params.append(int(key))

    where = " AND ".join(clauses)
    order = (
        "ORDER BY p.culled ASC, (COALESCE(c.width,0)*COALESCE(c.height,0)) DESC, c.size DESC, p.id DESC"
        if group_by == "bundle"
        else "ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC"
    )
    with db() as conn:
        rows = conn.execute(
            f"{PHOTO_SELECT} WHERE {where} {order} LIMIT ?",
            [*params, limit],
        ).fetchall()

    return [dict(r) for r in rows]


def bundle_list(
    threshold: float = 0.92,
    limit: int = 50,
    include_culled: bool = False,
) -> list[dict[str, Any]]:
    """Return existing duplicate groups as browsable bundles."""
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
