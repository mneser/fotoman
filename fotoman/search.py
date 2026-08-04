from __future__ import annotations

import math
from typing import Any

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
    col = f"{alias}.culled" if alias else "culled"
    return "1=1" if include_culled else f"{col} = 0"


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
    placeholders = ",".join("?" * len(content_ids))
    culled = _culled_clause(include_culled, "p")
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
              )
            """,
            content_ids,
        ).fetchall()
    return {int(r["content_id"]): dict(r) for r in rows}


def search_visual(
    query: str, top_k: int = 48, include_culled: bool = False
) -> list[dict[str, Any]]:
    vec = embed_text(query)
    hits = get_store().search(vec, top_k=top_k * 2)
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
    place: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    has_gps: bool | None = None,
    source_id: int | None = None,
    keepers_only: bool = False,
    include_culled: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    clauses = [_culled_clause(include_culled, "p")]
    params: list[Any] = []

    if text:
        clauses.append(
            "(p.rel_path LIKE ? OR p.album LIKE ? OR c.camera LIKE ? OR p.path LIKE ? "
            "OR c.city LIKE ? OR c.region LIKE ? OR c.country LIKE ? OR c.place_label LIKE ?)"
        )
        like = f"%{text}%"
        params.extend([like] * 8)
    if taken_from:
        clauses.append("c.taken_at >= ?")
        params.append(taken_from)
    if taken_to:
        clauses.append("c.taken_at <= ?")
        params.append(taken_to)
    if camera:
        clauses.append("c.camera LIKE ?")
        params.append(f"%{camera}%")
    if album:
        clauses.append("p.album LIKE ?")
        params.append(f"%{album}%")
    if place:
        like = f"%{place}%"
        clauses.append(
            "(c.city LIKE ? OR c.region LIKE ? OR c.country LIKE ? OR c.place_label LIKE ?)"
        )
        params.extend([like, like, like, like])
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
    source_id: int | None = None,
    include_culled: bool = False,
) -> list[dict[str, Any]]:
    visual = search_visual(query, top_k=top_k * 3, include_culled=include_culled)
    place_l = place.lower() if place else None
    out = []
    for item in visual:
        if taken_from and (not item.get("taken_at") or item["taken_at"] < taken_from):
            continue
        if taken_to and (not item.get("taken_at") or item["taken_at"] > taken_to):
            continue
        if album and album.lower() not in (item.get("album") or "").lower():
            continue
        if camera and camera.lower() not in (item.get("camera") or "").lower():
            continue
        if source_id is not None and item.get("source_id") != source_id:
            continue
        if place_l:
            blob = " ".join(
                str(item.get(k) or "") for k in ("city", "region", "country", "place_label")
            ).lower()
            if place_l not in blob:
                continue
        if lat is not None and lon is not None:
            if item.get("lat") is None or item.get("lon") is None:
                continue
            dist = haversine_km(lat, lon, item["lat"], item["lon"])
            if dist > (radius_km if radius_km is not None else 25.0):
                continue
            item["distance_km"] = round(dist, 3)
        out.append(item)
        if len(out) >= top_k:
            break
    return out


def suggest_places(q: str, limit: int = 12) -> list[dict[str, Any]]:
    """Predictive place suggestions from indexed city/region/country labels."""
    q = (q or "").strip()
    if len(q) < 1:
        return []
    like = f"%{q}%"
    prefix = f"{q}%"
    with db() as conn:
        rows = conn.execute(
            """
            SELECT label, kind, COUNT(*) AS count FROM (
                SELECT c.city AS label, 'city' AS kind
                FROM photos p JOIN photo_contents c ON c.id = p.content_id
                WHERE p.culled = 0 AND c.city IS NOT NULL AND c.city != '' AND c.city LIKE ?
                UNION ALL
                SELECT c.region AS label, 'region' AS kind
                FROM photos p JOIN photo_contents c ON c.id = p.content_id
                WHERE p.culled = 0 AND c.region IS NOT NULL AND c.region != '' AND c.region LIKE ?
                UNION ALL
                SELECT c.country AS label, 'country' AS kind
                FROM photos p JOIN photo_contents c ON c.id = p.content_id
                WHERE p.culled = 0 AND c.country IS NOT NULL AND c.country != '' AND c.country LIKE ?
                UNION ALL
                SELECT c.place_label AS label, 'place' AS kind
                FROM photos p JOIN photo_contents c ON c.id = p.content_id
                WHERE p.culled = 0 AND c.place_label IS NOT NULL AND c.place_label != ''
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
    culled = "" if include_culled else "AND p.culled = 0"
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
    with db() as conn:
        return {
            "photos": conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE culled = 0"
            ).fetchone()["c"],
            "culled": conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE culled = 1"
            ).fetchone()["c"],
            "embedded": conn.execute(
                """
                SELECT COUNT(*) AS c FROM photos p
                JOIN photo_contents c ON c.id = p.content_id
                WHERE c.embedding_ready = 1 AND p.culled = 0
                """
            ).fetchone()["c"],
            "with_gps": conn.execute(
                """
                SELECT COUNT(*) AS c FROM photos p
                JOIN photo_contents c ON c.id = p.content_id
                WHERE c.lat IS NOT NULL AND p.culled = 0
                """
            ).fetchone()["c"],
            "with_place": conn.execute(
                """
                SELECT COUNT(*) AS c FROM photos p
                JOIN photo_contents c ON c.id = p.content_id
                WHERE c.place_label IS NOT NULL AND p.culled = 0
                """
            ).fetchone()["c"],
            "sources": conn.execute(
                "SELECT COUNT(*) AS c FROM sources WHERE enabled = 1"
            ).fetchone()["c"],
            "groups": conn.execute("SELECT COUNT(*) AS c FROM photo_groups").fetchone()["c"],
            "replicas": conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE is_keeper = 0 AND culled = 0 AND group_id IS NOT NULL"
            ).fetchone()["c"],
            "contents": conn.execute(
                "SELECT COUNT(*) AS c FROM photo_contents"
            ).fetchone()["c"],
        }
