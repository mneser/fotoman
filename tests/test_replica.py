"""Volume shadow mounts: default path, union fill, offline primary."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from fotoman.config import DATA_DIR
from fotoman.db import db, init_db
from fotoman import duplicates, indexer, replica


def _jpeg(path: Path, color: tuple[int, int, int] = (12, 80, 160)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path, "JPEG")


def _clear() -> None:
    with db() as conn:
        conn.execute("DELETE FROM photos")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM photo_contents")


def _setup() -> None:
    init_db()
    _clear()


def _reload(source_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert row is not None
    return replica.annotate_source(dict(row))


def test_enable_shadow_uses_default_path(tmp_path: Path):
    _setup()
    folder = tmp_path / "album"
    _jpeg(folder / "a.jpg")
    src = indexer.add_source(str(folder), "album")
    out = replica.patch_source(
        int(src["id"]),
        shadow_enabled=True,
        has_shadow_enabled=True,
    )
    assert out["shadow_enabled"] is True
    dest = Path(out["shadow_path"])
    assert dest.is_dir()
    assert dest == DATA_DIR / "replicas" / str(src["id"])


def test_sync_union_fill_never_deletes(tmp_path: Path):
    _setup()
    primary = tmp_path / "work"
    shadow = tmp_path / "usb"
    _jpeg(primary / "only_primary.jpg", (200, 10, 10))
    _jpeg(shadow / "only_shadow.jpg", (10, 200, 10))
    _jpeg(shadow / "extra.jpg", (10, 10, 200))
    src = indexer.add_source(str(primary), "work")
    src = replica.patch_source(
        int(src["id"]),
        shadow_path=str(shadow),
        shadow_enabled=True,
        sync_enabled=True,
        has_shadow_path=True,
        has_shadow_enabled=True,
        has_sync_enabled=True,
    )
    stats = replica.sync_source(src)
    assert stats["copied_to_shadow"] >= 1
    assert stats["copied_to_primary"] >= 1
    assert (shadow / "only_primary.jpg").is_file()
    assert (primary / "only_shadow.jpg").is_file()
    assert (shadow / "extra.jpg").is_file()
    assert (primary / "extra.jpg").is_file()


def test_offline_primary_still_exists_and_lists_shadow(tmp_path: Path):
    _setup()
    primary = tmp_path / "ssd"
    shadow = tmp_path / "backup"
    _jpeg(primary / "shot.jpg")
    src = indexer.add_source(str(primary), "ssd")
    src = replica.patch_source(
        int(src["id"]),
        shadow_path=str(shadow),
        shadow_enabled=True,
        sync_enabled=True,
        has_shadow_path=True,
        has_shadow_enabled=True,
        has_sync_enabled=True,
    )
    replica.sync_source(src)
    assert (shadow / "shot.jpg").is_file()
    primary.rename(tmp_path / "ssd-offline")
    src = _reload(int(src["id"]))
    assert replica.source_exists(src) is True
    listing = indexer._list_source(src)
    assert listing["missing"] is False
    assert listing["listed_count"] == 1
    assert listing["to_index"]
    store_path = listing["to_index"][0][5]
    assert store_path.endswith("shot.jpg")
    assert Path(store_path).is_file() is False
    assert listing["to_index"][0][0].is_file()


def test_delete_files_does_not_remove_shadow_copy(tmp_path: Path):
    _setup()
    primary = tmp_path / "desk"
    shadow = tmp_path / "drive"
    photo = primary / "keep.jpg"
    _jpeg(photo, (90, 40, 20))
    src = indexer.add_source(str(primary), "desk")
    src = replica.patch_source(
        int(src["id"]),
        shadow_path=str(shadow),
        shadow_enabled=True,
        sync_enabled=True,
        has_shadow_path=True,
        has_shadow_enabled=True,
        has_sync_enabled=True,
    )
    replica.sync_source(src)
    listing = indexer._list_source(src)
    indexer._index_source_files(src, listing["to_index"])
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM photos WHERE source_id = ?", (int(src["id"]),)
        ).fetchone()
    assert row is not None
    duplicates.remove_photos([int(row["id"])], delete_files=True)
    assert not photo.is_file()
    assert (shadow / "keep.jpg").is_file()


def test_delete_photos_removes_empty_parent_dirs(tmp_path: Path):
    _setup()
    album = tmp_path / "album"
    nested = album / "trip" / "day1"
    photo = nested / "a.jpg"
    _jpeg(photo)
    src = indexer.add_source(str(album), "album")
    listing = indexer._list_source(src)
    indexer._index_source_files(src, listing["to_index"])
    with db() as conn:
        row = conn.execute("SELECT id FROM photos").fetchone()
    assert row is not None
    pid = int(row["id"])
    duplicates.remove_photos([pid], delete_files=False)
    duplicates.delete_photos([pid])
    assert not photo.exists()
    assert not nested.exists()
    assert not (album / "trip").exists()
    assert album.is_dir()
