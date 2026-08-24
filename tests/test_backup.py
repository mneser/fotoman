"""Backup archive kind + incremental collection sync."""

from __future__ import annotations

import time
from pathlib import Path

from PIL import Image

from fotoman.db import db, init_db
from fotoman.metadata import file_sha1
from fotoman import backup, indexer, collections


def _jpeg(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path, "JPEG")


def _clear_default_sources() -> None:
    with db() as conn:
        conn.execute("DELETE FROM photos")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM collection_members")
        conn.execute("DELETE FROM collections")
        conn.execute("DELETE FROM photo_contents")


def _add_photo(source_id: int, path: Path) -> tuple[int, int]:
    fhash = file_sha1(path)
    size = path.stat().st_size
    mtime = path.stat().st_mtime
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO photo_contents(file_hash, size) VALUES (?, ?)",
            (fhash, size),
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
            (cid, int(source_id), str(path), path.name, mtime),
        )
        pid = int(
            conn.execute(
                "SELECT id FROM photos WHERE source_id = ? AND path = ?",
                (int(source_id), str(path)),
            ).fetchone()["id"]
        )
    return pid, cid


def _setup_library(tmp_path: Path) -> tuple[int, int, int, Path]:
    init_db()
    _clear_default_sources()
    watched = tmp_path / "watched"
    a = watched / "a.jpg"
    b = watched / "b.jpg"
    _jpeg(a, (200, 30, 30))
    _jpeg(b, (30, 200, 30))
    src = indexer.add_source(str(watched), "watched")
    pid_a, _ = _add_photo(int(src["id"]), a)
    pid_b, _ = _add_photo(int(src["id"]), b)
    return int(src["id"]), pid_a, pid_b, watched


def _wait_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def test_export_creates_backup_kind_and_sync_copies_new_members(tmp_path: Path):
    _sid, pid_a, pid_b, _watched = _setup_library(tmp_path)
    folder = collections.create_collection("Keepers")
    fid = int(folder["id"])
    collections.add_members(fid, [pid_a])
    dest = tmp_path / "backup-root"
    dest.mkdir()
    result = collections.export_collection(fid, dest)
    archive = result["archive"]
    assert archive is not None
    assert archive["kind"] == "backup"
    assert int(archive["backup_collection_id"]) == fid
    assert result["exported"] == 1
    root = Path(result["dest"])
    assert (root / "a.jpg").is_file()
    assert not (root / "b.jpg").exists()
    assert int(src_id := archive["id"]) not in indexer.enabled_source_ids()

    collections.add_members(fid, [pid_b])
    assert src_id in backup.covering_backup_ids(fid)
    _wait_file(root / "b.jpg")
    st = backup.status_for_source(backup.get_source(src_id))
    assert st["backup_pending_count"] == 0
    again = backup.sync_backup(src_id)
    assert again["exported"] == 0
    assert again["skipped"] >= 2


def test_backup_skips_indexer_and_browse_roots(tmp_path: Path):
    _setup_library(tmp_path)
    folder = collections.create_collection("Keepers")
    with db() as conn:
        pid = int(conn.execute("SELECT id FROM photos ORDER BY id LIMIT 1").fetchone()["id"])
    collections.add_members(int(folder["id"]), [pid])
    dest = tmp_path / "backup-root"
    dest.mkdir()
    result = collections.export_collection(int(folder["id"]), dest)
    bid = int(result["archive"]["id"])
    assert bid not in indexer.enabled_source_ids()
    from fotoman import browse

    labels = {n["name"] for n in browse.folder_tree()}
    assert result["archive"]["label"] not in labels
