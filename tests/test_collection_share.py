"""Collections are always on Fotomob; delete to remove a share."""

from fotoman.db import db, init_db
from fotoman import collections
from fotoman.fotomob_link import _handle


def _clear() -> None:
    with db() as conn:
        conn.execute("DELETE FROM collection_members")
        conn.execute("DELETE FROM collections")


def _setup() -> None:
    init_db()
    _clear()


def _stored_share_token(collection_id: int) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT fotomob_share_token FROM collections WHERE id = ?",
            (int(collection_id),),
        ).fetchone()
    assert row
    return str(row["fotomob_share_token"] or "")


def test_collections_are_shared_on_create():
    _setup()
    root = collections.create_collection("Vacation")
    child = collections.create_collection("Italy", parent_id=root["id"])
    assert root["fotomob_shared"] is True
    assert root["share_code"]
    assert child["fotomob_shared"] is True
    assert not child.get("share_code")
    assert _stored_share_token(child["id"])
    tree = collections.list_collections()
    assert tree[0]["fotomob_visible"] is True
    assert tree[0]["children"][0]["id"] == child["id"]
    assert [n["name"] for n in collections.list_collections(fotomob_only=True)] == ["Vacation"]
    assert collections.collection_is_fotomob_visible(root["id"]) is True
    assert collections.collection_is_fotomob_visible(child["id"]) is True
    assert not tree[0]["children"][0].get("share_code")


def test_subfolder_share_codes_when_enabled(monkeypatch):
    monkeypatch.setenv("FOTOMOB_SUBFOLDER_SHARE", "1")
    _setup()
    root = collections.create_collection("Vacation")
    child = collections.create_collection("Italy", parent_id=root["id"])
    assert child["share_code"]
    assert child["share_code"] != root["share_code"]
    tree = collections.list_collections()
    assert tree[0]["children"][0]["share_code"] == child["share_code"]
    opened = _handle(
        {"id": "1", "method": "tree", "params": {"share": child["share_code"]}}
    )
    assert [n["name"] for n in opened["body"]] == ["Italy"]


def test_parent_token_includes_descendants():
    _setup()
    root = collections.create_collection("Vacation")
    italy = collections.create_collection("Italy", parent_id=root["id"])
    collections.create_collection("Hidden")

    shared = collections.list_collections(fotomob_only=True)
    assert [n["name"] for n in shared] == ["Hidden", "Vacation"]
    vacation = next(n for n in shared if n["name"] == "Vacation")
    assert [n["name"] for n in vacation["children"]] == ["Italy"]
    assert collections.collection_is_fotomob_visible(italy["id"]) is True
    assert collections.collection_is_fotomob_visible(root["id"]) is True


def test_fotomob_tree_is_collection_scoped():
    _setup()
    secret = collections.create_collection("Secret")
    opened = collections.create_collection("Open")
    token = opened["share_code"]
    assert token
    assert token in (opened.get("share_url") or "")
    assert token != secret["share_code"]

    denied = _handle({"id": "1", "method": "tree", "params": {}})
    assert denied["ok"] is True
    assert denied["share_ok"] is False
    assert denied["body"] == []

    library = _handle({"id": "2", "method": "tree", "params": {"share": "family"}})
    assert library["share_ok"] is False
    assert library["body"] == []

    allowed = _handle({"id": "3", "method": "tree", "params": {"share": token}})
    assert allowed["share_ok"] is True
    assert [n["name"] for n in allowed["body"]] == ["Open"]


def test_collection_token_hides_siblings_and_delete_revokes():
    _setup()
    vacation = collections.create_collection("Vacation")
    italy = collections.create_collection("Italy", parent_id=vacation["id"])
    work = collections.create_collection("Work")

    tree = _handle({"id": "1", "method": "tree", "params": {"share": vacation["share_code"]}})
    assert [n["name"] for n in tree["body"]] == ["Vacation"]
    assert [n["name"] for n in tree["body"][0]["children"]] == ["Italy"]

    blocked = _handle(
        {
            "id": "2",
            "method": "album",
            "params": {"share": vacation["share_code"], "collection_id": work["id"]},
        }
    )
    assert blocked["ok"] is False

    italy_tree = _handle(
        {
            "id": "3",
            "method": "tree",
            "params": {"share": _stored_share_token(italy["id"])},
        }
    )
    assert [n["name"] for n in italy_tree["body"]] == ["Italy"]

    token = vacation["share_code"]
    collections.delete_collection(vacation["id"])
    dead = _handle({"id": "4", "method": "tree", "params": {"share": token}})
    assert dead["share_ok"] is False
    assert dead["body"] == []


def test_slim_photo_keeps_gps_and_drops_null_island():
    from fotoman.fotomob_link import _slim_photo

    pinned = _slim_photo(
        {"id": 1, "content_id": 2, "lat": 22.3, "lon": 114.2, "city": "HK"}
    )
    assert pinned["lat"] == 22.3
    assert pinned["lon"] == 114.2
    assert pinned["place_label"] == "HK"

    missing = _slim_photo({"id": 1, "content_id": 2})
    assert missing["lat"] is None and missing["lon"] is None

    island = _slim_photo({"id": 1, "content_id": 2, "lat": 0, "lon": 0})
    assert island["lat"] is None and island["lon"] is None

    meta = _slim_photo(
        {
            "id": 1,
            "content_id": 2,
            "lat": 40.7,
            "lon": -74.0,
            "camera": "iPhone",
            "width": 4032,
            "height": 3024,
        }
    )
    assert meta["camera"] == "iPhone"
    assert meta["width"] == 4032
    assert meta["height"] == 3024


def _expect_value_error(fn) -> None:
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_source_subdirs_stay_locked_and_keep_share_codes(monkeypatch):
    _setup()
    monkeypatch.setattr(
        "fotoman.browse.subtree_for_key",
        lambda group_by, key, include_culled=False: {
            "name": "Album",
            "path": "1:Album",
            "children": [
                {"name": "Italy", "path": "1:Album/Italy", "children": []},
            ],
        },
    )
    trip = collections.create_collection("Trip")
    collections.import_browse_branch(trip["id"], "folder", "1:Album")
    tree = collections.list_collections()
    album = tree[0]["children"][0]
    italy = album["children"][0]
    assert album["name"] == "Album"
    assert album["linked"] is True
    assert album["source_subdir"] is False
    assert not album.get("share_code")
    assert italy["linked"] is True
    assert italy["source_subdir"] is True
    assert not italy.get("share_code")
    assert _stored_share_token(album["id"])
    assert _stored_share_token(italy["id"]) != _stored_share_token(album["id"])

    _expect_value_error(lambda: collections.delete_collection(italy["id"]))
    holiday = collections.rename_collection(album["id"], name="Holiday")
    assert holiday["name"] == "Holiday"
    assert holiday["linked"] is True
    _expect_value_error(lambda: collections.rename_collection(italy["id"], name="Nope"))
    _expect_value_error(
        lambda: collections.create_collection("Manual", parent_id=italy["id"])
    )
    _expect_value_error(lambda: collections.add_members(italy["id"], [1]))
    _expect_value_error(
        lambda: collections.import_browse_branch(italy["id"], "folder", "1:Other")
    )

    picks = collections.create_collection("Picks", parent_id=trip["id"])
    assert picks["linked"] is False
    assert picks["source_subdir"] is False
    renamed = collections.rename_collection(picks["id"], name="Keepers")
    assert renamed["name"] == "Keepers"
    collections.delete_collection(picks["id"])

    collections.delete_collection(album["id"])
    tree = collections.list_collections()
    assert tree[0]["children"] == []


def _date_year_tree() -> dict:
    return {
        "name": "2019",
        "path": "2019",
        "count": 10,
        "direct_count": 0,
        "children": [
            {
                "name": "Jan",
                "path": "2019-01",
                "count": 10,
                "direct_count": 0,
                "children": [
                    {"name": "01", "path": "2019-01-01", "count": 4, "direct_count": 4},
                    {"name": "02", "path": "2019-01-02", "count": 6, "direct_count": 6},
                ],
            }
        ],
    }


def test_list_collections_uses_date_tree_counts(monkeypatch):
    _setup()
    year_tree = _date_year_tree()
    monkeypatch.setattr(
        "fotoman.browse.subtree_for_key",
        lambda group_by, key, include_culled=False: year_tree if key == "2019" else None,
    )
    monkeypatch.setattr(
        "fotoman.browse.date_tree",
        lambda include_culled=False, newest_first=True: [year_tree],
    )

    def boom(*_a, **_k):
        raise AssertionError("listing must not fetch live photos per node")

    monkeypatch.setattr("fotoman.browse.photos_for_group", boom)
    collections.import_browse_branch(None, "date", "2019")
    tree = collections.list_collections()
    year = next(n for n in tree if n["name"] == "2019")
    assert year["linked"] is True
    assert year["count"] == 10
    assert year["direct_count"] == 0
    assert year["children"] == []


def test_linked_date_stays_flat_and_keeps_root_alias(monkeypatch):
    _setup()
    year_tree = _date_year_tree()
    monkeypatch.setattr(
        "fotoman.browse.subtree_for_key",
        lambda group_by, key, include_culled=False: year_tree if key == "2019" else None,
    )
    monkeypatch.setattr(
        "fotoman.browse.date_tree",
        lambda include_culled=False, newest_first=True: [year_tree],
    )
    collections.import_browse_branch(None, "date", "2019")
    year_id = collections.resolve_collection_key("2019")
    renamed = collections.rename_collection(year_id, name="Nineteen")
    assert renamed["name"] == "Nineteen"

    year_tree["count"] = 7
    year_tree["children"].append(
        {
            "name": "Feb",
            "path": "2019-02",
            "count": 3,
            "direct_count": 3,
            "children": [],
        }
    )
    year_tree["children"][0]["count"] = 4
    year_tree["children"][0]["direct_count"] = 0
    year_tree["children"][0]["children"] = [
        {"name": "01", "path": "2019-01-01", "count": 4, "direct_count": 4},
    ]

    tree = collections.list_collections()
    year = next(n for n in tree if n["id"] == year_id)
    assert year["name"] == "Nineteen"
    assert year["linked"] is True
    assert year["source_subdir"] is False
    assert year["count"] == 7
    assert year["children"] == []

    collections.import_browse_branch(None, "date", "2019")
    tree = collections.list_collections()
    year = next(n for n in tree if n["id"] == year_id)
    assert year["name"] == "Nineteen"
    assert year["children"] == []


def test_linked_date_purges_legacy_month_children(monkeypatch):
    _setup()
    year_tree = _date_year_tree()
    monkeypatch.setattr(
        "fotoman.browse.subtree_for_key",
        lambda group_by, key, include_culled=False: year_tree if key == "2019" else None,
    )
    monkeypatch.setattr(
        "fotoman.browse.date_tree",
        lambda include_culled=False, newest_first=True: [year_tree],
    )
    collections.import_browse_branch(None, "date", "2019")
    year_id = collections.resolve_collection_key("2019")
    with db() as conn:
        collections._ensure_linked_collection(
            year_id, "Jan", "date", "2019-01", conn=conn
        )
    tree = collections.list_collections()
    year = next(n for n in tree if n["id"] == year_id)
    assert year["children"] == []


def test_linked_date_year_fetches_live_ids_once(monkeypatch):
    _setup()
    year_tree = _date_year_tree()
    monkeypatch.setattr(
        "fotoman.browse.subtree_for_key",
        lambda group_by, key, include_culled=False: year_tree if key == "2019" else None,
    )
    calls: list[tuple] = []

    def fake_photos(group_by, key, limit=200, include_culled=False, include_subfolders=True, labeled=None):
        calls.append((group_by, key, include_subfolders))
        return [{"content_id": 1}, {"content_id": 2}]

    monkeypatch.setattr("fotoman.browse.photos_for_group", fake_photos)
    collections.import_browse_branch(None, "date", "2019")
    year_id = collections.resolve_collection_key("2019")
    ids = collections.content_ids_for_collection(year_id)
    assert calls == [("date", "2019", True)]
    assert ids == [1, 2]


def test_move_members_within_same_collection_tree():
    _setup()
    with db() as conn:
        conn.execute(
            "INSERT INTO photo_contents(file_hash, size) VALUES ('h-move', 1)"
        )
        cid = int(
            conn.execute(
                "SELECT id FROM photo_contents WHERE file_hash = 'h-move'"
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO sources(path, label, kind) VALUES ('/tmp/x', 'x', 'watched')
            """
        )
        sid = int(conn.execute("SELECT id FROM sources ORDER BY id DESC LIMIT 1").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO photos(
                content_id, source_id, path, rel_path, mtime, album,
                culled, missing, is_keeper
            ) VALUES (?, ?, '/tmp/x/a.jpg', 'a.jpg', 0, '', 0, 0, 1)
            """,
            (cid, sid),
        )
        pid = int(
            conn.execute(
                "SELECT id FROM photos WHERE content_id = ?", (cid,)
            ).fetchone()["id"]
        )

    trip = collections.create_collection("Trip")
    italy = collections.create_collection("Italy", parent_id=trip["id"])
    france = collections.create_collection("France", parent_id=trip["id"])
    other = collections.create_collection("Other")
    collections.add_members(italy["id"], [pid])

    moved = collections.move_members(italy["id"], france["id"], [pid])
    assert moved["added"] == 1
    assert moved["removed"] == 1
    assert collections.content_ids_for_collection(italy["id"]) == []
    assert collections.content_ids_for_collection(france["id"]) == [cid]

    # From parent scope (include child members) into a sibling.
    collections.move_members(france["id"], italy["id"], [pid])
    collections.move_members(trip["id"], france["id"], [pid])
    assert collections.content_ids_for_collection(italy["id"]) == []
    assert collections.content_ids_for_collection(france["id"]) == [cid]

    try:
        collections.move_members(france["id"], other["id"], [pid])
        assert False, "expected cross-collection move to fail"
    except ValueError as e:
        assert "same collection" in str(e).lower()
