"""Device archives: pairing, catalog index, vault-visible offline."""

from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image

from fotoman.db import db, init_db
from fotoman.fotomob_link import _handle
from fotoman import browse, collections, device, indexer, replica, search


def _jpeg(path: Path, color: tuple[int, int, int] = (20, 80, 160)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, "JPEG")


def _clear() -> None:
    device.reset_runtime()
    with db() as conn:
        conn.execute("DELETE FROM photos")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM photo_contents")
        conn.execute("DELETE FROM collection_members")
        conn.execute("DELETE FROM collections")


def _setup() -> None:
    init_db()
    _clear()


def test_create_pair_is_device_source_not_a_directory():
    _setup()
    src = device.create_pair("Pixel 8")
    assert src["kind"] == device.KIND_DEVICE
    assert src["label"] == "Pixel 8"
    assert device.is_device_path(src["path"])
    assert src["pair_id"]
    assert src["pair_id"] in src["path"]
    assert Path(src["path"]).exists() is False
    assert src["shadow_enabled"] is True
    assert Path(src["shadow_path"]).is_dir()
    assert src["exists"] is True
    assert device.source_exists(src) is True
    assert src["sync_enabled"] is True
    assert replica.live_root(src) is None


def test_heic_is_a_listed_image_type():
    from fotoman.config import IMAGE_EXTS

    assert ".heic" in IMAGE_EXTS
    assert ".heif" in IMAGE_EXTS


def test_online_catalog_indexes_and_stays_searchable_offline(tmp_path: Path):
    _setup()
    src = device.create_pair("Phone")
    pair = src["pair_id"]
    photo = tmp_path / "IMG_1.jpg"
    _jpeg(photo)
    data = photo.read_bytes()
    asset = "IMG_1.jpg"
    device.set_online(pair, True)
    device.put_cache(pair, asset, data)
    device.apply_catalog(
        pair,
        [
            {
                "id": asset,
                "name": "IMG_1.jpg",
                "rel": "Camera/IMG_1.jpg",
                "size": len(data),
                "mtime": photo.stat().st_mtime,
            }
        ],
        queue_scan=False,
    )
    row = device.get_by_pair(pair)
    listing = indexer._list_source(row)
    assert listing["missing"] is False
    assert listing["listed_count"] == 1
    assert listing["to_index"]

    discovered, _embeds = indexer._index_source_files(row, listing["to_index"])
    assert discovered == 1

    with db() as conn:
        loc = conn.execute("SELECT * FROM photos WHERE source_id = ?", (src["id"],)).fetchone()
        content = conn.execute(
            "SELECT * FROM photo_contents WHERE id = ?", (loc["content_id"],)
        ).fetchone()
    assert loc["path"] == device.asset_path(pair, asset)
    assert loc["rel_path"] == "Camera/IMG_1.jpg"
    assert loc["missing"] == 0
    assert content["file_hash"]
    assert content["width"] == 32

    hits = search.search_metadata(limit=20)
    assert any(int(p["id"]) == int(loc["id"]) for p in hits)

    device.set_online(pair, False)
    with db() as conn:
        missing = conn.execute(
            "SELECT missing FROM photos WHERE id = ?", (loc["id"],)
        ).fetchone()["missing"]
    assert int(missing) == 0
    assert device.cache_file(pair, asset).is_file()
    assert device.readable_path(loc["path"]) is not None
    assert device.source_exists(device.get_by_pair(pair))

    hits = search.search_metadata(limit=20)
    assert any(int(p["id"]) == int(loc["id"]) for p in hits)

    tree = browse.folder_tree()
    assert any(n["name"] == "Phone" for n in tree)
    phone = next(n for n in tree if n["name"] == "Phone")
    assert phone["offline"] is False
    assert phone["count"] >= 1

    dates = browse.date_tree()
    assert isinstance(dates, list)


def test_device_hello_unknown_pair_is_denied():
    _setup()
    out = _handle({"id": 1, "method": "device_hello", "params": {"pair": "nope"}})
    assert out["ok"] is False
    assert out["status"] == 404


def test_device_hello_and_catalog_roundtrip(tmp_path: Path):
    _setup()
    src = device.create_pair("Desk phone")
    pair = src["pair_id"]
    hello = _handle(
        {"id": 2, "method": "device_hello", "params": {"pair": pair, "label": "Pixel"}}
    )
    assert hello["ok"] is True
    assert device.is_online(pair)
    assert hello["body"]["label"] == "Pixel"
    assert hello["body"]["fotoman_name"]

    photo = tmp_path / "x.jpg"
    _jpeg(photo, (9, 9, 9))
    catalog = _handle(
        {
            "id": 3,
            "method": "device_catalog",
            "params": {
                "pair": pair,
                "files": [
                    {
                        "id": "x",
                        "name": "x.jpg",
                        "rel": "x.jpg",
                        "size": photo.stat().st_size,
                        "mtime": photo.stat().st_mtime,
                    }
                ],
            },
        }
    )
    assert catalog["ok"] is True
    assert catalog["body"]["catalog_count"] == 1
    assert device.get_catalog(pair)[0]["id"] == "x"

    bye = _handle({"id": 4, "method": "device_bye", "params": {"pair": pair}})
    assert bye["ok"] is True
    assert device.is_online(pair) is False


def test_put_upload_folder_indexes_after_disconnect(tmp_path: Path):
    _setup()
    src = device.create_pair("Phone")
    pair = src["pair_id"]
    photo = tmp_path / "beach.jpg"
    _jpeg(photo, (40, 120, 20))
    data = photo.read_bytes()
    device.set_online(pair, True)
    out = device.put_upload(
        pair,
        {
            "id": "beach",
            "name": "beach.jpg",
            "size": len(data),
            "mtime": photo.stat().st_mtime,
            "body": base64.b64encode(data).decode("ascii"),
            "folder": "Vacation",
        },
        queue_scan=False,
    )
    assert out["rel"] == "Vacation/beach.jpg"
    assert device.get_catalog(pair)[0]["rel"] == "Vacation/beach.jpg"
    device.set_online(pair, False)
    assert device.has_shadow(pair)
    row = device.get_by_pair(pair)
    listing = indexer._list_source(row)
    assert listing["missing"] is False
    assert listing["to_index"]
    discovered, _ = indexer._index_source_files(row, listing["to_index"])
    assert discovered == 1
    with db() as conn:
        loc = conn.execute("SELECT * FROM photos WHERE source_id = ?", (src["id"],)).fetchone()
    assert loc["rel_path"] == "Vacation/beach.jpg"
    assert loc["album"] == "Vacation"
    assert int(loc["missing"]) == 0
    assert device.readable_path(loc["path"]).read_bytes() == data


def test_catalog_merges_successive_uploads(tmp_path: Path):
    _setup()
    src = device.create_pair("Phone")
    pair = src["pair_id"]
    a = tmp_path / "a.jpg"
    b = tmp_path / "b.jpg"
    _jpeg(a)
    _jpeg(b, (1, 2, 3))
    device.put_cache(pair, "a", a.read_bytes())
    device.apply_catalog(
        pair,
        [{"id": "a", "name": "a.jpg", "rel": "a.jpg", "size": a.stat().st_size}],
        queue_scan=False,
        folder="One",
    )
    device.put_cache(pair, "b", b.read_bytes())
    device.apply_catalog(
        pair,
        [{"id": "b", "name": "b.jpg", "rel": "b.jpg", "size": b.stat().st_size}],
        queue_scan=False,
        folder="Two",
    )
    ids = {item["id"]: item["rel"] for item in device.get_catalog(pair)}
    assert ids["a"] == "One/a.jpg"
    assert ids["b"] == "Two/b.jpg"


def test_device_put_roundtrip(tmp_path: Path):
    _setup()
    src = device.create_pair("Desk")
    pair = src["pair_id"]
    photo = tmp_path / "x.jpg"
    _jpeg(photo)
    data = photo.read_bytes()
    put = _handle(
        {
            "id": 8,
            "method": "device_put",
            "params": {
                "pair": pair,
                "folder": "Inbox",
                "file": {
                    "id": "x",
                    "name": "x.jpg",
                    "size": len(data),
                    "mtime": photo.stat().st_mtime,
                    "body": base64.b64encode(data).decode("ascii"),
                },
            },
        }
    )
    assert put["ok"] is True
    assert put["body"]["rel"] == "Inbox/x.jpg"
    assert device.cache_file(pair, "x").read_bytes() == data


def test_shadow_catalog_reloads_after_runtime_reset(tmp_path: Path):
    _setup()
    src = device.create_pair("Phone")
    pair = src["pair_id"]
    photo = tmp_path / "keep.jpg"
    _jpeg(photo)
    device.put_cache(pair, "keep", photo.read_bytes())
    device.apply_catalog(
        pair,
        [{"id": "keep", "name": "keep.jpg", "rel": "keep.jpg", "size": photo.stat().st_size}],
        queue_scan=False,
    )
    device.reset_runtime()
    loaded = device.get_catalog(pair)
    assert len(loaded) == 1
    assert loaded[0]["id"] == "keep"
    assert device.has_shadow(pair)


def test_create_pair_with_shadow_path_writes_named_copy(tmp_path: Path):
    _setup()
    shadow = tmp_path / "pixel"
    src = device.create_pair("Pixel", shadow_path=str(shadow))
    pair = src["pair_id"]
    assert src["shadow_enabled"] is True
    assert Path(src["shadow_path"]) == shadow.resolve()
    assert src["exists"] is True
    photo = tmp_path / "shot.jpg"
    _jpeg(photo)
    data = photo.read_bytes()
    device.put_upload(
        pair,
        {
            "id": "shot",
            "name": "shot.jpg",
            "size": len(data),
            "mtime": photo.stat().st_mtime,
            "body": base64.b64encode(data).decode("ascii"),
            "folder": "Camera",
        },
        queue_scan=False,
    )
    named = shadow / "Camera" / "shot.jpg"
    assert named.is_file()
    assert named.read_bytes() == data
    loc = device.asset_path(pair, "shot")
    assert device.readable_path(loc) is not None
    assert device.readable_path(loc).read_bytes() == data


def test_patch_shadow_and_sync_flags(tmp_path: Path):
    _setup()
    src = device.create_pair("Phone")
    shadow = tmp_path / "copies"
    out = device.patch_source(
        src["id"],
        shadow_path=str(shadow),
        has_shadow_path=True,
    )
    assert out["shadow_enabled"] is True
    assert Path(out["shadow_path"]) == shadow.resolve()
    off = device.patch_source(
        src["id"],
        shadow_enabled=False,
        has_shadow_enabled=True,
    )
    assert off["shadow_enabled"] is False
    assert off["shadow_path"]
    synced = device.patch_source(
        src["id"],
        sync_enabled=False,
        has_sync_enabled=True,
    )
    assert synced["sync_enabled"] is False
    defaulted = device.patch_source(
        src["id"],
        shadow_path="",
        shadow_enabled=True,
        has_shadow_path=True,
        has_shadow_enabled=True,
    )
    assert defaulted["shadow_enabled"] is True
    assert Path(defaulted["shadow_path"]).is_dir()


def test_create_pair_shares_volume_as_collection():
    _setup()
    src = device.create_pair("Pixel 8")
    tree = collections.list_collections()
    phone = next(n for n in tree if n["name"] == "Pixel 8")
    assert phone["fotomob_shared"] is True
    assert phone.get("share_code")
    assert str(phone.get("link_key") or "") == f"{src['id']}:"


def test_device_lookup_known_pair():
    _setup()
    src = device.create_pair("Desk")
    out = _handle({"id": 9, "method": "device_lookup", "params": {"pair": src["pair_id"]}})
    assert out["ok"] is True
    assert out["body"]["fotoman_name"]
    missing = _handle({"id": 10, "method": "device_lookup", "params": {"pair": "nope"}})
    assert missing["ok"] is False


def test_put_upload_appears_in_volume_collection(tmp_path: Path):
    _setup()
    src = device.create_pair("Phone")
    pair = src["pair_id"]
    phone = next(n for n in collections.list_collections() if n["name"] == "Phone")
    photo = tmp_path / "shot.jpg"
    _jpeg(photo)
    data = photo.read_bytes()
    device.put_upload(
        pair,
        {
            "id": "shot",
            "name": "shot.jpg",
            "size": len(data),
            "mtime": photo.stat().st_mtime,
            "body": base64.b64encode(data).decode("ascii"),
            "folder": "Camera",
        },
        queue_scan=False,
    )
    row = device.get_by_pair(pair)
    listing = indexer._list_source(row)
    indexer._index_source_files(row, listing["to_index"])
    collections.invalidate_fotomob_share_cache()
    photos = collections.photos_for_collection(phone["id"])
    assert len(photos) == 1
    assert "Camera" in str(photos[0].get("rel_path") or photos[0].get("album") or "")
    tree = collections.list_collections()
    phone_node = next(n for n in tree if n["name"] == "Phone")
    child_names = [c["name"] for c in phone_node.get("children") or []]
    assert "Camera" in child_names
    camera = next(c for c in phone_node["children"] if c["name"] == "Camera")
    assert camera["source_subdir"] is True
    assert not camera.get("share_code")


def test_put_upload_folder_creates_collection_and_returns_share_code(tmp_path: Path):
    _setup()
    src = device.create_pair("Phone")
    pair = src["pair_id"]
    photo = tmp_path / "shot.jpg"
    _jpeg(photo)
    data = photo.read_bytes()
    payload = {
        "id": "shot",
        "name": "shot.jpg",
        "size": len(data),
        "mtime": photo.stat().st_mtime,
        "body": base64.b64encode(data).decode("ascii"),
        "folder": "Vacation",
    }
    out = device.put_upload(pair, payload, queue_scan=False)
    assert out["collection_name"] == "Vacation"
    assert out["share_code"]
    assert out["collection_id"]
    tree = collections.list_collections()
    vacation = next(n for n in tree if n["name"] == "Vacation")
    assert vacation["parent_id"] is None
    assert vacation["linked"] is True
    assert vacation["source_subdir"] is False
    assert vacation["share_code"] == out["share_code"]
    assert str(vacation.get("link_key") or "") == f"{src['id']}:Vacation"

    extra = tmp_path / "shot2.jpg"
    _jpeg(extra, (200, 10, 10))
    extra_data = extra.read_bytes()
    again = device.put_upload(
        pair,
        {
            **payload,
            "id": "shot2",
            "name": "shot2.jpg",
            "size": len(extra_data),
            "mtime": extra.stat().st_mtime,
            "body": base64.b64encode(extra_data).decode("ascii"),
        },
        queue_scan=False,
    )
    assert again["share_code"] == out["share_code"]
    assert again["collection_id"] == out["collection_id"]
    assert sum(1 for n in collections.list_collections() if n["name"] == "Vacation") == 1

    row = device.get_by_pair(pair)
    listing = indexer._list_source(row)
    indexer._index_source_files(row, listing["to_index"])
    photos = collections.photos_for_collection(int(vacation["id"]))
    assert len(photos) == 2

    via_rpc = _handle(
        {
            "id": 11,
            "method": "device_put",
            "params": {
                "pair": pair,
                "folder": "Vacation",
                "file": {**payload, "id": "shot3", "name": "shot3.jpg"},
            },
        }
    )
    assert via_rpc["ok"] is True
    assert via_rpc["body"]["share_code"] == out["share_code"]


def test_put_upload_without_folder_does_not_mint_collection_code(tmp_path: Path):
    _setup()
    src = device.create_pair("Phone")
    pair = src["pair_id"]
    photo = tmp_path / "shot.jpg"
    _jpeg(photo)
    data = photo.read_bytes()
    out = device.put_upload(
        pair,
        {
            "id": "shot",
            "name": "shot.jpg",
            "size": len(data),
            "mtime": photo.stat().st_mtime,
            "body": base64.b64encode(data).decode("ascii"),
        },
        queue_scan=False,
    )
    assert not out.get("share_code")
    assert [n["name"] for n in collections.list_collections()] == ["Phone"]


def test_delete_photos_removes_named_shadow_folder(tmp_path: Path):
    from fotoman import duplicates

    _setup()
    shadow = tmp_path / "android"
    src = device.create_pair("Android", shadow_path=str(shadow))
    pair = src["pair_id"]
    photo = tmp_path / "shot.jpg"
    _jpeg(photo)
    data = photo.read_bytes()
    device.put_upload(
        pair,
        {
            "id": "shot",
            "name": "shot.jpg",
            "size": len(data),
            "mtime": photo.stat().st_mtime,
            "body": base64.b64encode(data).decode("ascii"),
            "folder": "verdun",
        },
        queue_scan=False,
    )
    assert (shadow / "verdun" / "shot.jpg").is_file()
    row = device.get_by_pair(pair)
    listing = indexer._list_source(row)
    indexer._index_source_files(row, listing["to_index"])
    with db() as conn:
        pid = int(conn.execute("SELECT id FROM photos").fetchone()["id"])
    duplicates.remove_photos([pid], delete_files=False)
    duplicates.delete_photos([pid])
    assert not (shadow / "verdun" / "shot.jpg").exists()
    assert not (shadow / "verdun").exists()


def test_device_album_folder_is_not_used_as_place(tmp_path: Path):
    _setup()
    src = device.create_pair("Android")
    pair = src["pair_id"]
    photo = tmp_path / "shot.jpg"
    _jpeg(photo)
    data = photo.read_bytes()
    device.put_upload(
        pair,
        {
            "id": "shot",
            "name": "shot.jpg",
            "size": len(data),
            "mtime": photo.stat().st_mtime,
            "body": base64.b64encode(data).decode("ascii"),
            "folder": "verdun",
        },
        queue_scan=False,
    )
    row = device.get_by_pair(pair)
    listing = indexer._list_source(row)
    indexer._index_source_files(row, listing["to_index"])
    indexer._backfill_places_from_paths()
    with db() as conn:
        label = conn.execute("SELECT place_label FROM photo_contents").fetchone()["place_label"]
    assert not label


def test_reload_exif_on_device_path_updates_place(tmp_path: Path, monkeypatch):
    from fotoman.metadata import write_gps
    from fotoman import properties

    _setup()
    src = device.create_pair("Android")
    pair = src["pair_id"]
    photo = tmp_path / "shot.jpg"
    _jpeg(photo)
    write_gps(photo, 33.8889, 35.4954)
    data = photo.read_bytes()
    device.put_upload(
        pair,
        {
            "id": "shot",
            "name": "shot.jpg",
            "size": len(data),
            "mtime": photo.stat().st_mtime,
            "body": base64.b64encode(data).decode("ascii"),
            "folder": "verdun",
        },
        queue_scan=False,
    )
    row = device.get_by_pair(pair)
    listing = indexer._list_source(row)
    indexer._index_source_files(row, listing["to_index"])
    with db() as conn:
        pid = int(conn.execute("SELECT id FROM photos").fetchone()["id"])
        cid = int(conn.execute("SELECT content_id FROM photos WHERE id = ?", (pid,)).fetchone()["content_id"])
        conn.execute(
            """
            UPDATE photo_contents
            SET lat = ?, lon = ?, gps = 0, coord_source = 'geocode',
                city = 'Yandina', region = 'Queensland', country = 'Australia',
                place_label = 'Yandina, Queensland, Australia'
            WHERE id = ?
            """,
            (-26.561, 152.959, cid),
        )
    monkeypatch.setattr(
        properties,
        "reverse_one",
        lambda _lat, _lon: {
            "place_label": "Beirut, Lebanon",
            "venue": None,
            "city": "Beirut",
            "region": None,
            "country": "Lebanon",
        },
    )
    out = properties.reload_exif_coords([pid])
    assert out["updated"] == 1
    assert not out["errors"]
    assert out["photos"][0]["place_label"] == "Beirut, Lebanon"
    assert abs(out["photos"][0]["lat"] - 33.8889) < 0.01
