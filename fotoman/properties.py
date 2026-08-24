"""Batch edit of photo properties (DB + on-disk metadata).

``apply_properties`` accepts a dict of known fields so new properties can be
added without changing the API shape: ``{ photo_ids, properties: { … } }``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fotoman.config import load_settings
from fotoman.db import PHOTO_SELECT, content_thumb_path, db
from fotoman.geocode import (
    is_valid_gps,
    resolve_manual_place,
    reverse_one,
)
from fotoman.metadata import (
    can_replace_coords,
    can_write_exif,
    coords_close,
    extract_exif,
    file_sha1,
    gps_flag,
    infer_coord_source,
    mtime_to_taken_at,
    normalize_coord_source,
    normalize_orientation,
    normalize_taken_at,
    pixel_fingerprint,
    write_gps,
    write_taken_at,
    write_taken_at_mtime,
)

# Keys accepted in ``properties``. Handlers run in this order.
KNOWN_PROPERTIES = ("taken_at", "place_label", "orientation", "mirrored", "lat", "lon")


def _local_photo_path(row: dict[str, Any]) -> Path | None:
    """Readable file for EXIF, including phone cache / named shadow copies."""
    from fotoman import device

    return device.readable_path(row.get("path"))


def _place_payload(place: dict[str, Any] | None) -> dict[str, Any]:
    place = place or {}
    return {
        "place_label": place.get("place_label"),
        "venue": place.get("venue"),
        "city": place.get("city"),
        "region": place.get("region"),
        "country": place.get("country"),
    }


def apply_properties(
    photo_ids: list[int],
    properties: dict[str, Any],
    *,
    write_to_exif: bool = False,
    overwrite_coords: bool = False,
) -> dict[str, Any]:
    """Apply property patch to selected photos. Unknown keys are rejected.

    ``place_label: ""`` clears place fields (city/region/country/label).
    ``write_to_exif`` writes a specified timestamp into the file. Coordinates
    are not written here — use ``write_exif`` from the lightbox.
    """
    if not photo_ids:
        return {"updated": 0, "errors": []}
    raw = dict(properties or {})
    clear_place = "place_label" in raw and (
        raw["place_label"] is None or str(raw["place_label"]).strip() == ""
    )
    # ``mirrored: false`` is a valid update (bool False must not be dropped).
    props = {
        k: v
        for k, v in raw.items()
        if k == "mirrored" or (v is not None and v != "")
    }
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
    coords: tuple[float, float] | None = None
    if "lat" in props or "lon" in props:
        if "lat" not in props or "lon" not in props:
            raise ValueError("lat and lon must be set together")
        try:
            lat = float(props["lat"])
            lon = float(props["lon"])
        except (TypeError, ValueError) as e:
            raise ValueError("Invalid coordinates") from e
        if not is_valid_gps(lat, lon):
            raise ValueError("Invalid coordinates")
        coords = (lat, lon)
    if "place_label" in props:
        if clear_place:
            clear_place_fields = True
        else:
            label = str(props["place_label"]).strip()
            place, place_coords = resolve_manual_place(label, fetch=True)
    reverse_place: dict[str, str | None] | None = None
    if coords is not None and place is None and not clear_place_fields:
        try:
            hit = reverse_one(coords[0], coords[1])
            if (hit or {}).get("place_label"):
                reverse_place = hit
        except Exception:
            reverse_place = None
    orientation = (
        normalize_orientation(props["orientation"]) if "orientation" in props else None
    )
    mirrored = bool(props["mirrored"]) if "mirrored" in props else None
    if (
        taken_at is None
        and place is None
        and not clear_place_fields
        and orientation is None
        and mirrored is None
        and coords is None
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
                final_lat = row.get("lat")
                final_lon = row.get("lon")
                final_src = infer_coord_source(row)
                if clear_place_fields:
                    keep_coords = final_src in ("exif", "manual")
                    conn.execute(
                        """
                        UPDATE photo_contents
                        SET place_label = NULL, venue = NULL, city = NULL, region = NULL, country = NULL,
                            lat = CASE WHEN ? THEN lat ELSE NULL END,
                            lon = CASE WHEN ? THEN lon ELSE NULL END
                        WHERE id = ?
                        """,
                        (1 if keep_coords else 0, 1 if keep_coords else 0, int(row["content_id"])),
                    )
                    if not keep_coords:
                        final_lat = final_lon = None
                        final_src = None
                elif place is not None:
                    content_id = int(row["content_id"])
                    existing_src = infer_coord_source(row)
                    # City-center pins only replace empty/geocode coords — never
                    # camera GPS or a manual map pin.
                    apply_geocode = place_coords is not None and (
                        overwrite_coords
                        or can_replace_coords(existing_src, "geocode")
                    )
                    if apply_geocode:
                        conn.execute(
                            """
                            UPDATE photo_contents
                            SET place_label = ?,
                                venue = ?,
                                city = ?,
                                region = ?,
                                country = ?,
                                lat = ?,
                                lon = ?,
                                gps = 0,
                                coord_source = 'geocode'
                            WHERE id = ?
                            """,
                            (
                                place["place_label"],
                                place.get("venue"),
                                place["city"],
                                place["region"],
                                place["country"],
                                place_coords[0],
                                place_coords[1],
                                content_id,
                            ),
                        )
                        final_lat, final_lon, final_src = (
                            place_coords[0],
                            place_coords[1],
                            "geocode",
                        )
                    else:
                        clear_geocode = existing_src not in ("exif", "manual")
                        conn.execute(
                            """
                            UPDATE photo_contents
                            SET place_label = ?,
                                venue = ?,
                                city = ?,
                                region = ?,
                                country = ?,
                                lat = CASE WHEN ? THEN NULL ELSE lat END,
                                lon = CASE WHEN ? THEN NULL ELSE lon END,
                                gps = CASE WHEN ? THEN 0 ELSE gps END,
                                coord_source = CASE WHEN ? THEN NULL ELSE coord_source END
                            WHERE id = ?
                            """,
                            (
                                place["place_label"],
                                place.get("venue"),
                                place["city"],
                                place["region"],
                                place["country"],
                                1 if clear_geocode else 0,
                                1 if clear_geocode else 0,
                                1 if clear_geocode else 0,
                                1 if clear_geocode else 0,
                                content_id,
                            ),
                        )
                        if clear_geocode:
                            final_lat = final_lon = None
                            final_src = None
                if coords is not None:
                    if reverse_place is not None:
                        conn.execute(
                            """
                            UPDATE photo_contents
                            SET lat = ?, lon = ?, gps = 0, coord_source = 'manual',
                                place_label = ?, venue = NULL, city = ?, region = ?, country = ?
                            WHERE id = ?
                            """,
                            (
                                coords[0],
                                coords[1],
                                reverse_place.get("place_label"),
                                reverse_place.get("city"),
                                reverse_place.get("region"),
                                reverse_place.get("country"),
                                int(row["content_id"]),
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE photo_contents
                            SET lat = ?, lon = ?, gps = 0, coord_source = 'manual'
                            WHERE id = ?
                            """,
                            (coords[0], coords[1], int(row["content_id"])),
                        )
                    final_lat, final_lon, final_src = coords[0], coords[1], "manual"
                if orientation is not None or mirrored is not None:
                    content_id = int(row["content_id"])
                    new_orient = (
                        orientation
                        if orientation is not None
                        else int(row.get("orientation") or 0)
                    )
                    new_mirror = (
                        mirrored
                        if mirrored is not None
                        else bool(row.get("mirrored"))
                    )
                    conn.execute(
                        "UPDATE photo_contents SET orientation = ?, mirrored = ? WHERE id = ?",
                        (new_orient, 1 if new_mirror else 0, content_id),
                    )
                    thumb = content_thumb_path(content_id)
                    src = thumb if thumb.exists() else Path(row["path"])
                    fp = (
                        pixel_fingerprint(
                            src, orientation=new_orient, mirrored=new_mirror
                        )
                        if src.exists()
                        else None
                    )
                    if fp:
                        conn.execute(
                            "UPDATE photo_contents SET fingerprint = ? WHERE id = ?",
                            (fp, content_id),
                        )
                if taken_at is not None or write_to_exif:
                    path = Path(row["path"])
                    write_mtime = taken_at is not None and bool(
                        load_settings().get("write_date_to_mtime")
                    )
                    want_exif_date = bool(write_to_exif and taken_at)
                    want_exif_gps = False
                    wrote_file = False
                    content_updates: dict[str, Any] = {}
                    if (want_exif_date or want_exif_gps) and path.is_file():
                        if not can_write_exif(path):
                            errors.append(
                                {
                                    "photo_id": pid,
                                    "error": (
                                        f"Cannot write EXIF for "
                                        f"{path.suffix.lower() or 'unknown'} files"
                                    ),
                                }
                            )
                        else:
                            try:
                                if want_exif_date:
                                    write_taken_at(path, taken_at)
                                    content_updates["taken_at"] = taken_at
                                    content_updates["taken_at_source"] = "manual"
                                    wrote_file = True
                                if want_exif_gps:
                                    write_gps(
                                        path, float(final_lat), float(final_lon)
                                    )
                                    content_updates["lat"] = float(final_lat)
                                    content_updates["lon"] = float(final_lon)
                                    content_updates["coord_source"] = final_src
                                    content_updates["gps"] = gps_flag(final_src)
                                    wrote_file = True
                            except Exception as e:
                                errors.append({"photo_id": pid, "error": str(e)})
                    if write_mtime and path.is_file():
                        try:
                            new_mtime = write_taken_at_mtime(path, taken_at)
                            if not wrote_file:
                                conn.execute(
                                    "UPDATE photos SET mtime = ? WHERE id = ?",
                                    (new_mtime, int(row["id"])),
                                )
                        except Exception:
                            pass
                    if wrote_file:
                        _relink_after_file_change(
                            conn,
                            row,
                            taken_at=taken_at if want_exif_date else None,
                            content_updates=content_updates,
                        )
                        save_faiss = True
                    elif taken_at is not None:
                        # DB-only, or EXIF write unsupported/failed.
                        conn.execute(
                            """
                            UPDATE photo_contents
                            SET taken_at = ?, taken_at_source = 'manual'
                            WHERE id = ?
                            """,
                            (taken_at, int(row["content_id"])),
                        )
                updated += 1
            except Exception as e:
                errors.append({"photo_id": pid, "error": str(e)})

    if save_faiss:
        try:
            from fotoman.index_store import get_store

            get_store().save()
        except Exception:
            pass

    out: dict[str, Any] = {"updated": updated, "errors": errors}
    if clear_place_fields:
        out["place"] = None
    elif place is not None:
        place_out = dict(place)
        if place_coords is not None:
            place_out["lat"] = place_coords[0]
            place_out["lon"] = place_coords[1]
            place_out["gps"] = 0
            place_out["coord_source"] = "geocode"
        out["place"] = place_out
    if coords is not None:
        out["lat"] = coords[0]
        out["lon"] = coords[1]
        out["gps"] = 0
        out["coord_source"] = "manual"
        if reverse_place is not None and "place" not in out:
            place_out = dict(reverse_place)
            place_out["lat"] = coords[0]
            place_out["lon"] = coords[1]
            place_out["gps"] = 0
            place_out["coord_source"] = "manual"
            out["place"] = place_out
    return out


def _taken_at_equal(a: str | None, b: str | None) -> bool:
    if not a and not b:
        return True
    if not a or not b:
        return False
    try:
        return normalize_taken_at(str(a)) == normalize_taken_at(str(b))
    except ValueError:
        return str(a).strip() == str(b).strip()


def file_meta_for_photos(photo_ids: list[int]) -> dict[str, Any]:
    """Compare DB date/GPS against EXIF and filesystem mtime."""
    if not photo_ids:
        return {"photos": []}
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
    photos: list[dict[str, Any]] = []
    for pid in photo_ids:
        row = by_id.get(int(pid))
        if not row:
            photos.append({"photo_id": pid, "error": "Not found"})
            continue
        path = _local_photo_path(row)
        file_exists = bool(path and path.is_file())
        exif = extract_exif(path) if file_exists else {}
        try:
            file_mtime = float(path.stat().st_mtime) if file_exists else row.get("mtime")
        except OSError:
            file_mtime = row.get("mtime")
        mtime_taken = mtime_to_taken_at(file_mtime)
        db_taken = row.get("taken_at")
        file_taken = exif.get("taken_at")
        db_lat, db_lon = row.get("lat"), row.get("lon")
        file_lat, file_lon = exif.get("lat"), exif.get("lon")
        coord_source = infer_coord_source(row)
        photos.append(
            {
                "photo_id": int(pid),
                "path": row.get("path"),
                "taken_at": db_taken,
                "taken_at_source": row.get("taken_at_source"),
                "file_taken_at": file_taken,
                "mtime_taken_at": mtime_taken,
                "date_matches_exif": _taken_at_equal(db_taken, file_taken),
                "date_matches_mtime": _taken_at_equal(db_taken, mtime_taken),
                "lat": db_lat,
                "lon": db_lon,
                "coord_source": coord_source,
                "file_lat": file_lat,
                "file_lon": file_lon,
                "gps_matches_exif": coords_close(db_lat, db_lon, file_lat, file_lon),
                "can_write_exif": file_exists and can_write_exif(path),
                "file_missing": not file_exists,
            }
        )
    return {"photos": photos}


def write_exif(
    photo_ids: list[int],
    *,
    taken_at: bool = True,
    gps: bool = True,
) -> dict[str, Any]:
    """Write current DB date and/or coordinates into JPEG/TIFF EXIF, then rehash.

    Place-geocoded pins are written too; after a GPS write the stored source
    becomes ``exif`` so the file is the source of truth.
    """
    if not photo_ids:
        return {"updated": 0, "errors": [], "skipped": []}
    updated = 0
    errors: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
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
            path = _local_photo_path(row)
            if not path or not path.is_file():
                errors.append({"photo_id": pid, "error": "File missing"})
                continue
            if not can_write_exif(path):
                errors.append(
                    {
                        "photo_id": pid,
                        "error": f"Cannot write EXIF for {path.suffix.lower() or 'unknown'} files",
                    }
                )
                continue
            want_date = bool(taken_at and row.get("taken_at"))
            want_gps = bool(
                gps and row.get("lat") is not None and row.get("lon") is not None
            )
            if not want_date and not want_gps:
                skipped.append({"photo_id": pid, "reason": "nothing to write"})
                continue
            content_updates: dict[str, Any] = {}
            try:
                if want_date:
                    write_taken_at(path, str(row["taken_at"]))
                    content_updates["taken_at"] = normalize_taken_at(str(row["taken_at"]))
                    content_updates["taken_at_source"] = (
                        "manual"
                        if row.get("taken_at_source") == "manual"
                        else (row.get("taken_at_source") or "exif")
                    )
                if want_gps:
                    write_gps(path, float(row["lat"]), float(row["lon"]))
                    content_updates["lat"] = float(row["lat"])
                    content_updates["lon"] = float(row["lon"])
                    content_updates["coord_source"] = "exif"
                    content_updates["gps"] = 1
                if row.get("taken_at"):
                    try:
                        write_taken_at_mtime(path, str(row["taken_at"]))
                    except Exception:
                        pass
                _relink_after_file_change(conn, row, content_updates=content_updates)
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
    return {"updated": updated, "errors": errors, "skipped": skipped}


def reload_exif_coords(photo_ids: list[int]) -> dict[str, Any]:
    """Replace stored lat/lon with GPS from the file's EXIF."""
    if not photo_ids:
        return {"updated": 0, "errors": [], "photos": []}
    updated = 0
    errors: list[dict[str, Any]] = []
    photos: list[dict[str, Any]] = []
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
            path = _local_photo_path(row)
            if not path or not path.is_file():
                errors.append({"photo_id": pid, "error": "File missing"})
                continue
            exif = extract_exif(path)
            file_lat, file_lon = exif.get("lat"), exif.get("lon")
            has_gps = file_lat is not None and file_lon is not None
            if has_gps:
                lat, lon, src, gps = float(file_lat), float(file_lon), "exif", 1
                try:
                    place = reverse_one(lat, lon)
                except Exception:
                    place = None
                fields = _place_payload(place)
                conn.execute(
                    """
                    UPDATE photo_contents
                    SET lat = ?, lon = ?, gps = 1, coord_source = 'exif',
                        venue = ?, city = ?, region = ?, country = ?, place_label = ?
                    WHERE id = ?
                    """,
                    (
                        lat,
                        lon,
                        fields["venue"],
                        fields["city"],
                        fields["region"],
                        fields["country"],
                        fields["place_label"],
                        int(row["content_id"]),
                    ),
                )
            else:
                lat, lon, src, gps = None, None, None, 0
                fields = _place_payload(None)
                conn.execute(
                    """
                    UPDATE photo_contents
                    SET lat = NULL, lon = NULL, gps = 0, coord_source = NULL,
                        venue = NULL, city = NULL, region = NULL, country = NULL,
                        place_label = NULL
                    WHERE id = ?
                    """,
                    (int(row["content_id"]),),
                )
            photos.append(
                {
                    "photo_id": int(pid),
                    "lat": lat,
                    "lon": lon,
                    "coord_source": src,
                    "gps": gps,
                    **fields,
                }
            )
            updated += 1
    return {"updated": updated, "errors": errors, "photos": photos}


def _relink_after_file_change(
    conn,
    row: dict[str, Any],
    *,
    taken_at: str | None = None,
    content_updates: dict[str, Any] | None = None,
) -> None:
    """Rehash file, update/split content. Pixels unchanged → keep faces/embed."""
    path = _local_photo_path(row) or Path(row["path"])
    photo_id = int(row["id"])
    old_content_id = int(row["content_id"])
    new_hash = file_sha1(path)
    st = path.stat()
    size = int(st.st_size)
    mtime = float(st.st_mtime)
    updates = dict(content_updates or {})
    if taken_at is not None:
        updates.setdefault("taken_at", taken_at)
        updates.setdefault("taken_at_source", "manual")

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

    def _apply_updates(content_id: int) -> None:
        allowed = {
            "taken_at",
            "taken_at_source",
            "lat",
            "lon",
            "gps",
            "coord_source",
        }
        sets = ["size = ?"]
        params: list[Any] = [size]
        for key, val in updates.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(val)
        params.append(content_id)
        conn.execute(
            f"UPDATE photo_contents SET {', '.join(sets)} WHERE id = ?",
            params,
        )

    if existing and int(existing["id"]) == old_content_id:
        _apply_updates(old_content_id)
        return

    if existing:
        new_id = int(existing["id"])
        _apply_updates(new_id)
        conn.execute(
            "UPDATE photos SET content_id = ? WHERE id = ?",
            (new_id, photo_id),
        )
        return

    if refcount == 1:
        conn.execute(
            "UPDATE photo_contents SET file_hash = ? WHERE id = ?",
            (new_hash, old_content_id),
        )
        _apply_updates(old_content_id)
        return

    # Shared content: split this location onto a cloned content row.
    old = conn.execute(
        "SELECT * FROM photo_contents WHERE id = ?", (old_content_id,)
    ).fetchone()
    old_d = dict(old)
    old_d.update(updates)
    src = normalize_coord_source(old_d.get("coord_source")) or infer_coord_source(old_d)
    cur = conn.execute(
        """
        INSERT INTO photo_contents(
            file_hash, size, width, height, taken_at, taken_at_source, camera,
            lat, lon, gps, coord_source, city, region, country, place_label,
            orientation, mirrored, fingerprint,
            sharpness, exposure, quality,
            embedding_ready, faces_ready, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_hash,
            size,
            old_d["width"],
            old_d["height"],
            old_d.get("taken_at"),
            old_d.get("taken_at_source"),
            old_d["camera"],
            old_d.get("lat"),
            old_d.get("lon"),
            gps_flag(src) if src else int(old_d.get("gps") or 0),
            src,
            old_d["city"],
            old_d["region"],
            old_d["country"],
            old_d["place_label"],
            int(old_d["orientation"] or 0),
            int(old_d["mirrored"] or 0),
            old_d["fingerprint"],
            old_d["sharpness"],
            old_d["exposure"],
            old_d["quality"],
            old_d["embedding_ready"],
            old_d["faces_ready"],
            old_d["indexed_at"],
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
