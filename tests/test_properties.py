"""Lightbox EXIF GPS reload must replace folder-geocoded places."""

from pathlib import Path

from PIL import Image

from fotoman.db import db, init_db
from fotoman.metadata import file_sha1, write_gps
from fotoman import indexer, properties


def _clear() -> None:
    with db() as conn:
        conn.execute("DELETE FROM photos")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM photo_contents")


def _jpeg_with_gps(path: Path, lat: float, lon: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), (8, 16, 24)).save(path, "JPEG")
    write_gps(path, lat, lon)


def _insert_photo(source_id: int, path: Path, *, place: str, lat: float, lon: float) -> tuple[int, int]:
    fhash = file_sha1(path)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO photo_contents(
                file_hash, size, lat, lon, gps, coord_source,
                city, region, country, place_label
            ) VALUES (?, ?, ?, ?, 0, 'geocode', ?, ?, ?, ?)
            """,
            (
                fhash,
                path.stat().st_size,
                lat,
                lon,
                "Yandina",
                "Queensland",
                "Australia",
                place,
            ),
        )
        cid = int(
            conn.execute(
                "SELECT id FROM photo_contents WHERE file_hash = ?", (fhash,)
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO photos(
                content_id, source_id, path, rel_path, mtime, album,
                culled, missing, is_keeper
            ) VALUES (?, ?, ?, ?, ?, '', 0, 0, 1)
            """,
            (cid, source_id, str(path), path.name, path.stat().st_mtime),
        )
        pid = int(
            conn.execute(
                "SELECT id FROM photos WHERE source_id = ? AND path = ?",
                (source_id, str(path)),
            ).fetchone()["id"]
        )
    return pid, cid


def test_reload_exif_coords_replaces_yandina_place(tmp_path: Path, monkeypatch):
    init_db()
    _clear()
    path = tmp_path / "shot.jpg"
    _jpeg_with_gps(path, 33.8889, 35.4954)
    src = indexer.add_source(str(tmp_path), "album")
    pid, cid = _insert_photo(
        int(src["id"]),
        path,
        place="Yandina, Queensland, Australia",
        lat=-26.561,
        lon=152.959,
    )

    monkeypatch.setattr(
        properties,
        "reverse_one",
        lambda _lat, _lon: {
            "place_label": "Beirut, Lebanon",
            "venue": None,
            "city": "Beirut",
            "region": "Beirut Governorate",
            "country": "Lebanon",
        },
    )
    out = properties.reload_exif_coords([pid])
    assert out["updated"] == 1
    assert not out["errors"]
    photo = out["photos"][0]
    assert abs(photo["lat"] - 33.8889) < 0.01
    assert photo["coord_source"] == "exif"
    assert photo["place_label"] == "Beirut, Lebanon"

    with db() as conn:
        row = conn.execute(
            "SELECT lat, coord_source, place_label, country FROM photo_contents WHERE id = ?",
            (cid,),
        ).fetchone()
    assert row["coord_source"] == "exif"
    assert row["place_label"] == "Beirut, Lebanon"
    assert row["country"] == "Lebanon"
