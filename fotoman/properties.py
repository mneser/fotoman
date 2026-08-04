"""Batch edit of photo properties (DB + on-disk metadata).

``apply_properties`` accepts a dict of known fields so new properties can be
added without changing the API shape: ``{ photo_ids, properties: { … } }``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fotoman.db import PHOTO_SELECT, content_thumb_path, db, gc_orphaned_content
from fotoman.geocode import lookup_city_coords, parse_city_country
from fotoman.metadata import (
    file_sha1,
    normalize_orientation,
    normalize_taken_at,
    pixel_fingerprint,
    write_taken_at,
)

# Keys accepted in ``properties``. Handlers run in this order.
KNOWN_PROPERTIES = ("taken_at", "place_label", "orientation")


def apply_properties(photo_ids: list[int], properties: dict[str, Any]) -> dict[str, Any]:
    """Apply property patch to selected photos. Unknown keys are rejected.

    ``place_label: ""`` clears place fields (city/region/country/label).
    """
    if not photo_ids:
        return {"updated": 0, "errors": []}
    raw = dict(properties or {})
    clear_place = "place_label" in raw and (
        raw["place_label"] is None or str(raw["place_label"]).strip() == ""
    )
    props = {k: v for k, v in raw.items() if v is not None and v != ""}
    if clear_place:
        props["place_label"] = ""
    unknown = sorted(set(props) - set(KNOWN_PROPERTIES))
    if unknown:
        raise ValueError(f"Unsupported properties: {', '.join(unknown)}")
    if not props:
        raise ValueError("No properties to update")

    taken_at = normalize_taken_at(str(props["taken_at"])) if "taken_at" in props else None
    place: dict[str, str | None] | None = None
    clear_place_fields = False
    place_coords: tuple[float, float] | None = None
    if "place_label" in props:
        if clear_place:
            clear_place_fields = True
        else:
            place = parse_city_country(str(props["place_label"]).strip())
            if place.get("city") and place.get("country"):
                place_coords = lookup_city_coords(
                    place["city"], place["country"], place.get("region")
                )
    orientation = (
        normalize_orientation(props["orientation"]) if "orientation" in props else None
    )
    if (
        taken_at is None
        and place is None
        and not clear_place_fields
        and orientation is None
    ):
        raise ValueError("No properties to update")

    updated = 0
    errors: list[dict[str, Any]] = []
    save_faiss = False

    with db() as conn:
        placeholders = ",".join("?" * len(photo_ids))
        rows = [
            dict(r)
            for r in conn.execute(
                f"{PHOTO_SELECT} WHERE p.id IN ({placeholders})",
                photo_ids,
            ).fetchall()
        ]
        by_id = {int(r["id"]): r for r in rows}

        for pid in photo_ids:
            row = by_id.get(int(pid))
            if not row:
                errors.append({"photo_id": pid, "error": "Not found"})
                continue
            try:
                if clear_place_fields:
                    conn.execute(
                        """
                        UPDATE photo_contents
                        SET place_label = NULL, city = NULL, region = NULL, country = NULL
                        WHERE id = ?
                        """,
                        (int(row["content_id"]),),
                    )
                elif place is not None:
                    content_id = int(row["content_id"])
                    # Keep existing EXIF GPS; only fill city-center coords when missing.
                    set_lat = place_coords is not None and row.get("lat") is None
                    set_lon = place_coords is not None and row.get("lon") is None
                    conn.execute(
                        """
                        UPDATE photo_contents
                        SET place_label = ?,
                            city = ?,
                            region = ?,
                            country = ?,
                            lat = CASE WHEN ? THEN ? ELSE lat END,
                            lon = CASE WHEN ? THEN ? ELSE lon END
                        WHERE id = ?
                        """,
                        (
                            place["place_label"],
                            place["city"],
                            place["region"],
                            place["country"],
                            1 if set_lat else 0,
                            place_coords[0] if place_coords else None,
                            1 if set_lon else 0,
                            place_coords[1] if place_coords else None,
                            content_id,
                        ),
                    )
                if orientation is not None:
                    content_id = int(row["content_id"])
                    conn.execute(
                        "UPDATE photo_contents SET orientation = ? WHERE id = ?",
                        (orientation, content_id),
                    )
                    thumb = content_thumb_path(content_id)
                    src = thumb if thumb.exists() else Path(row["path"])
                    fp = (
                        pixel_fingerprint(src, orientation=orientation)
                        if src.exists()
                        else None
                    )
                    if fp:
                        conn.execute(
                            "UPDATE photo_contents SET fingerprint = ? WHERE id = ?",
                            (fp, content_id),
                        )
                if taken_at is not None:
                    path = Path(row["path"])
                    if not path.is_file():
                        errors.append({"photo_id": pid, "error": "File missing"})
                        continue
                    write_taken_at(path, taken_at)
                    _relink_after_file_change(conn, row, taken_at=taken_at)
                    save_faiss = True
                updated += 1
            except Exception as e:
                errors.append({"photo_id": pid, "error": str(e)})

    if save_faiss:
        try:
            from fotoman.index_store import get_store

            get_store().save()
        except Exception:
            pass

    return {"updated": updated, "errors": errors}


def _relink_after_file_change(
    conn,
    row: dict[str, Any],
    *,
    taken_at: str,
) -> None:
    """Rehash file, update/split content, set taken_at. Pixels unchanged → keep faces/embed."""
    path = Path(row["path"])
    photo_id = int(row["id"])
    old_content_id = int(row["content_id"])
    new_hash = file_sha1(path)
    st = path.stat()
    size = int(st.st_size)
    mtime = float(st.st_mtime)

    conn.execute(
        "UPDATE photos SET mtime = ? WHERE id = ?",
        (mtime, photo_id),
    )

    refcount = conn.execute(
        "SELECT COUNT(*) AS c FROM photos WHERE content_id = ?",
        (old_content_id,),
    ).fetchone()["c"]

    existing = conn.execute(
        "SELECT id FROM photo_contents WHERE file_hash = ?",
        (new_hash,),
    ).fetchone()

    if existing and int(existing["id"]) == old_content_id:
        conn.execute(
            "UPDATE photo_contents SET taken_at = ?, size = ? WHERE id = ?",
            (taken_at, size, old_content_id),
        )
        return

    if existing:
        new_id = int(existing["id"])
        conn.execute(
            "UPDATE photo_contents SET taken_at = ?, size = COALESCE(size, ?) WHERE id = ?",
            (taken_at, size, new_id),
        )
        conn.execute(
            "UPDATE photos SET content_id = ? WHERE id = ?",
            (new_id, photo_id),
        )
        gc_orphaned_content(conn, old_content_id)
        return

    if refcount == 1:
        conn.execute(
            """
            UPDATE photo_contents
            SET file_hash = ?, size = ?, taken_at = ?
            WHERE id = ?
            """,
            (new_hash, size, taken_at, old_content_id),
        )
        return

    # Shared content: split this location onto a cloned content row.
    old = conn.execute(
        "SELECT * FROM photo_contents WHERE id = ?", (old_content_id,)
    ).fetchone()
    cur = conn.execute(
        """
        INSERT INTO photo_contents(
            file_hash, size, width, height, taken_at, camera,
            lat, lon, city, region, country, place_label, orientation, fingerprint,
            embedding_ready, faces_ready, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_hash,
            size,
            old["width"],
            old["height"],
            taken_at,
            old["camera"],
            old["lat"],
            old["lon"],
            old["city"],
            old["region"],
            old["country"],
            old["place_label"],
            int(old["orientation"] or 0),
            old["fingerprint"],
            old["embedding_ready"],
            old["faces_ready"],
            old["indexed_at"],
        ),
    )
    new_id = int(cur.lastrowid)
    conn.execute(
        "UPDATE photos SET content_id = ? WHERE id = ?",
        (new_id, photo_id),
    )

    # Copy face rows to the new content.
    faces = conn.execute(
        "SELECT * FROM faces WHERE content_id = ?", (old_content_id,)
    ).fetchall()
    for f in faces:
        conn.execute(
            """
            INSERT INTO faces(
                content_id, person_id, bbox_x, bbox_y, bbox_w, bbox_h,
                det_score, embedding
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                f["person_id"],
                f["bbox_x"],
                f["bbox_y"],
                f["bbox_w"],
                f["bbox_h"],
                f["det_score"],
                f["embedding"],
            ),
        )

    old_thumb = content_thumb_path(old_content_id)
    new_thumb = content_thumb_path(new_id)
    if old_thumb.exists():
        new_thumb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_thumb, new_thumb)

    if int(old["embedding_ready"] or 0):
        try:
            from fotoman.index_store import get_store
            import numpy as np

            store = get_store()
            if old_content_id in store.ids:
                pos = store.ids.index(old_content_id)
                vec = store.index.reconstruct(pos).astype(np.float32).reshape(1, -1)
                store.add([new_id], vec)
        except Exception:
            conn.execute(
                "UPDATE photo_contents SET embedding_ready = 0 WHERE id = ?",
                (new_id,),
            )
