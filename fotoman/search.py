from __future__ import annotations

import math
from typing import Any

from fotoman.aspect import ASPECT_CLASS_SQL, ASPECT_CLASSES
from fotoman.db import PHOTO_SELECT, db
from fotoman.embed import embed_text
from fotoman.index_store import get_store


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _culled_clause(include_culled: bool, alias: str = "") -> str:
    from fotoman.device import vault_live_sql_subquery

    col = f"{alias}.culled" if alias else "culled"
    live = vault_live_sql_subquery(alias or "p")
    if include_culled:
        return live
    return f"{col} = 0 AND {live}"


def _photos_by_ids(ids: list[int], include_culled: bool = False) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    q = (
        f"{PHOTO_SELECT} WHERE p.id IN ({','.join('?' * len(ids))})"
        f" AND {_culled_clause(include_culled, 'p')}"
    )
    with db() as conn:
        rows = conn.execute(q, ids).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _one_photo_per_content(
    content_ids: list[int], include_culled: bool = False
) -> dict[int, dict[str, Any]]:
    """Map content_id → one representative location photo."""
    if not content_ids:
        return {}
    from fotoman.device import vault_live_sql_subquery

    placeholders = ",".join("?" * len(content_ids))
    culled = _culled_clause(include_culled, "p")
    live_p2 = vault_live_sql_subquery("p2")
    with db() as conn:
        rows = conn.execute(
            f"""
            {PHOTO_SELECT}
            WHERE p.content_id IN ({placeholders})
              AND {culled}
              AND p.id = (
                SELECT MIN(p2.id) FROM photos p2
                WHERE p2.content_id = p.content_id
                  AND {"1=1" if include_culled else "p2.culled = 0"}
                  AND {live_p2}
              )
            """,
            content_ids,
        ).fetchall()
    return {int(r["content_id"]): dict(r) for r in rows}


def _and_sets(*groups: set[int] | None) -> set[int] | None:
    out: set[int] | None = None
    for g in groups:
        if g is None:
            continue
        out = set(g) if out is None else out & g
    return out


def _collection_scope(
    collection_id: int | None,
    collection: str | None,
    include_subfolders: bool = True,
) -> set[int] | None:
    if collection_id is None and not (collection and str(collection).strip()):
        return None
    from fotoman import collections

    fid = (
        int(collection_id)
        if collection_id is not None
        else collections.resolve_collection_key(str(collection))
    )
    return set(
        collections.content_ids_for_collection(
            fid, include_subfolders=include_subfolders
        )
    )


def _person_scope(person_id: int | None) -> set[int] | None:
    if person_id is None:
        return None
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT content_id FROM faces WHERE person_id = ?",
            (int(person_id),),
        ).fetchall()
    return {int(r["content_id"]) for r in rows}


def search_visual(
    query: str,
    top_k: int = 48,
    include_culled: bool = False,
    content_ids: set[int] | None = None,
    place: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    taken_from: str | None = None,
    taken_to: str | None = None,
    album: str | None = None,
    camera: str | None = None,
    aspect: str | None = None,
    source_id: int | None = None,
    has_gps: bool | None = None,
    keepers_only: bool = False,
    collection_id: int | None = None,
    collection: str | None = None,
    person_id: int | None = None,
    include_subfolders: bool = True,
) -> list[dict[str, Any]]:
    scoped = _and_sets(
        content_ids,
        _collection_scope(collection_id, collection, include_subfolders),
        _person_scope(person_id),
    )
    meta_needed = any(
        [
            place,
            lat is not None,
            taken_from,
            taken_to,
            album,
            camera,
            aspect,
            source_id is not None,
            has_gps is not None,
            keepers_only,
        ]
    )
    if meta_needed:
        meta_rows = search_metadata(
            taken_from=taken_from,
            taken_to=taken_to,
            camera=camera,
            album=album,
            aspect=aspect,
            place=place,
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            has_gps=has_gps,
            source_id=source_id,
            keepers_only=keepers_only,
            include_culled=include_culled,
            content_ids=scoped,
            collection_id=None,
            collection=None,
            person_id=None,
            limit=max(top_k * 20, 2000),
        )
        scoped = {int(r["content_id"]) for r in meta_rows}

    parts = [p.strip() for p in query.split("|") if p.strip()]
    if len(parts) > 1:
        best: dict[int, dict[str, Any]] = {}
        for part in parts:
            for item in search_visual(
                part,
                top_k=top_k,
                include_culled=include_culled,
                content_ids=scoped,
            ):
                cid = int(item["content_id"])
                if cid not in best or item["score"] > best[cid]["score"]:
                    best[cid] = item
        return sorted(best.values(), key=lambda x: -x["score"])[:top_k]

    store = get_store()
    fetch_k = top_k * 2
    if scoped is not None:
        fetch_k = max(fetch_k, store.index.ntotal)
    vec = embed_text(query)
    hits = store.search(vec, top_k=fetch_k)
    if scoped is not None:
        hits = [(cid, score) for cid, score in hits if cid in scoped]
    return _hits_to_photos(hits, top_k=top_k, include_culled=include_culled)


def search_similar(
    content_id: int,
    top_k: int = 48,
    include_culled: bool = False,
    content_ids: set[int] | None = None,
) -> list[dict[str, Any]] | None:
    """Rank photos by visual similarity to an already-embedded content id.

    Returns None if the query content has no FAISS vector.
    """
    store = get_store()
    vec = store.vector_for(content_id)
    if vec is None:
        return None
    fetch_k = top_k + 1
    if content_ids is not None:
        fetch_k = max(fetch_k, store.index.ntotal)
    hits = [
        (cid, score)
        for cid, score in store.search(vec, top_k=fetch_k)
        if cid != content_id
    ]
    if content_ids is not None:
        hits = [(cid, score) for cid, score in hits if cid in content_ids]
    return _hits_to_photos(hits, top_k=top_k, include_culled=include_culled)


def _hits_to_photos(
    hits: list[tuple[int, float]],
    top_k: int,
    include_culled: bool = False,
) -> list[dict[str, Any]]:
    by_content = _one_photo_per_content(
        [cid for cid, _ in hits], include_culled=include_culled
    )
    out = []
    for cid, score in hits:
        p = by_content.get(cid)
        if not p:
            continue
        item = dict(p)
        item["score"] = score
        out.append(item)
        if len(out) >= top_k:
            break
    return out


def search_metadata(
    text: str | None = None,
    taken_from: str | None = None,
    taken_to: str | None = None,
    camera: str | None = None,
    album: str | None = None,
    aspect: str | None = None,
    place: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    has_gps: bool | None = None,
    source_id: int | None = None,
    keepers_only: bool = False,
    include_culled: bool = False,
    content_ids: set[int] | None = None,
    collection_id: int | None = None,
    collection: str | None = None,
    person_id: int | None = None,
    include_subfolders: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses = [_culled_clause(include_culled, "p")]
    params: list[Any] = []

    allow = _and_sets(
        content_ids,
        _collection_scope(collection_id, collection, include_subfolders),
        _person_scope(person_id),
    )
    if allow is not None:
        if not allow:
            return []
        placeholders = ",".join("?" * len(allow))
        clauses.append(f"c.id IN ({placeholders})")
        params.extend(list(allow))

    if text:
        clauses.append(
            "(p.rel_path LIKE ? OR p.album LIKE ? OR c.camera LIKE ? OR p.path LIKE ? "
            "OR c.city LIKE ? OR c.region LIKE ? OR c.country LIKE ? OR c.place_label LIKE ? "
            "OR c.venue LIKE ?)"
        )
        like = f"%{text}%"
        params.extend([like] * 9)
    if taken_from:
        clauses.append("c.taken_at >= ?")
        params.append(taken_from)
    if taken_to:
        clauses.append("c.taken_at <= ?")
        params.append(taken_to)
    if camera:
        clauses.append("c.camera LIKE ?")
        params.append(f"%{camera}%")
    if aspect:
        if aspect not in ASPECT_CLASSES:
            return []
        clauses.append(f"({ASPECT_CLASS_SQL}) = ?")
        params.append(aspect)
    if album:
        clauses.append("p.album LIKE ?")
        params.append(f"%{album}%")
    if place:
        like = f"%{place}%"
        clauses.append(
            "(c.city LIKE ? OR c.region LIKE ? OR c.country LIKE ? OR c.place_label LIKE ? "
            "OR c.venue LIKE ?)"
        )
        params.extend([like, like, like, like, like])
    if source_id is not None:
        clauses.append("p.source_id = ?")
        params.append(source_id)
    if has_gps is True:
        clauses.append("c.lat IS NOT NULL AND c.lon IS NOT NULL")
    elif has_gps is False:
        clauses.append("c.lat IS NULL OR c.lon IS NULL")
    if keepers_only:
        clauses.append("p.is_keeper = 1")

    where = " AND ".join(clauses)
    sql = (
        f"{PHOTO_SELECT} WHERE {where} "
        f"ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC LIMIT ?"
    )
    params.append(limit * 5 if lat is not None and lon is not None else limit)

    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    if lat is not None and lon is not None:
        rad = radius_km if radius_km is not None else 25.0
        filtered = []
        for r in rows:
            if r["lat"] is None or r["lon"] is None:
                continue
            dist = haversine_km(lat, lon, r["lat"], r["lon"])
            if dist <= rad:
                r["distance_km"] = round(dist, 3)
                filtered.append(r)
        filtered.sort(key=lambda x: x.get("distance_km", 1e9))
        return filtered[:limit]
    return rows[:limit]


def hybrid_search(
    query: str,
    top_k: int = 48,
    place: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    taken_from: str | None = None,
    taken_to: str | None = None,
    album: str | None = None,
    camera: str | None = None,
    aspect: str | None = None,
    source_id: int | None = None,
    has_gps: bool | None = None,
    keepers_only: bool = False,
    include_culled: bool = False,
    content_ids: set[int] | None = None,
    collection_id: int | None = None,
    collection: str | None = None,
    person_id: int | None = None,
    include_subfolders: bool = True,
) -> list[dict[str, Any]]:
    return search_visual(
        query,
        top_k=top_k,
        include_culled=include_culled,
        content_ids=content_ids,
        place=place,
        lat=lat,
        lon=lon,
        radius_km=radius_km,
        taken_from=taken_from,
        taken_to=taken_to,
        album=album,
        camera=camera,
        aspect=aspect,
        source_id=source_id,
        has_gps=has_gps,
        keepers_only=keepers_only,
        collection_id=collection_id,
        collection=collection,
        person_id=person_id,
        include_subfolders=include_subfolders,
    )


def suggest_places(q: str, limit: int = 12) -> list[dict[str, Any]]:
    """Predictive place suggestions from indexed city/region/country labels."""
    from fotoman.device import vault_live_sql_subquery

    q = (q or "").strip()
    if len(q) < 1:
        return []
    like = f"%{q}%"
    prefix = f"{q}%"
    live = vault_live_sql_subquery("p")
    with db() as conn:
        rows = conn.execute(
            """
            SELECT label, kind, COUNT(*) AS count FROM (
                SELECT c.city AS label, 'city' AS kind
                FROM photos p JOIN photo_contents c ON c.id = p.content_id
                WHERE p.culled = 0 AND {live} AND c.city IS NOT NULL AND c.city != '' AND c.city LIKE ?
                UNION ALL
                SELECT c.region AS label, 'region' AS kind
                FROM photos p JOIN photo_contents c ON c.id = p.content_id
                WHERE p.culled = 0 AND {live} AND c.region IS NOT NULL AND c.region != '' AND c.region LIKE ?
                UNION ALL
                SELECT c.country AS label, 'country' AS kind
                FROM photos p JOIN photo_contents c ON c.id = p.content_id
                WHERE p.culled = 0 AND {live} AND c.country IS NOT NULL AND c.country != '' AND c.country LIKE ?
                UNION ALL
                SELECT c.place_label AS label, 'place' AS kind
                FROM photos p JOIN photo_contents c ON c.id = p.content_id
                WHERE p.culled = 0 AND {live} AND c.place_label IS NOT NULL AND c.place_label != ''
                  AND c.place_label LIKE ?
            )
            GROUP BY label, kind
            ORDER BY
                CASE WHEN label LIKE ? THEN 0 ELSE 1 END,
                count DESC,
                length(label) ASC
            LIMIT ?
            """,
            (like, like, like, like, prefix, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def map_photos(include_culled: bool = False) -> list[dict[str, Any]]:
    """Rows for map pins (GPS + metadata for lightbox)."""
    from fotoman.geocode import fill_missing_place_coords

    # City-center pins for manually placed photos (cached Nominatim lookup).
    fill_missing_place_coords(fetch_limit=3)
    live = _culled_clause(include_culled, "p")
    culled = f"AND {live}"
    with db() as conn:
        rows = conn.execute(
            f"""
            {PHOTO_SELECT}
            WHERE c.lat IS NOT NULL AND c.lon IS NOT NULL {culled}
            ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict[str, Any]:
    from fotoman.device import vault_live_sql_subquery

    live = vault_live_sql_subquery("p")
    with db() as conn:
        return {
            "photos": conn.execute(
                f"SELECT COUNT(*) AS c FROM photos p WHERE p.culled = 0 AND {live}"
            ).fetchone()["c"],
            "culled": conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE culled = 1"
            ).fetchone()["c"],
            "embedded": conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM photos p
                JOIN photo_contents c ON c.id = p.content_id
                WHERE c.embedding_ready = 1 AND p.culled = 0 AND {live}
                """
            ).fetchone()["c"],
            "with_gps": conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM photos p
                JOIN photo_contents c ON c.id = p.content_id
                WHERE c.lat IS NOT NULL AND p.culled = 0 AND {live}
                """
            ).fetchone()["c"],
            "with_place": conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM photos p
                JOIN photo_contents c ON c.id = p.content_id
                WHERE c.place_label IS NOT NULL AND p.culled = 0 AND {live}
                """
            ).fetchone()["c"],
            "sources": conn.execute(
                "SELECT COUNT(*) AS c FROM sources WHERE enabled = 1"
            ).fetchone()["c"],
            "groups": conn.execute("SELECT COUNT(*) AS c FROM photo_groups").fetchone()["c"],
            "replicas": conn.execute(
                f"SELECT COUNT(*) AS c FROM photos p WHERE p.is_keeper = 0 AND p.culled = 0 AND {live} AND p.group_id IS NOT NULL"
            ).fetchone()["c"],
            "contents": conn.execute(
                "SELECT COUNT(*) AS c FROM photo_contents"
            ).fetchone()["c"],
        }
