"""Unreadable files must leave the embed queue instead of retrying forever."""

from __future__ import annotations

from pathlib import Path

from PIL import UnidentifiedImageError

from fotoman.db import db, init_db
from fotoman import indexer


class _FakeStore:
    ids: list[int] = []

    def remove_ids(self, _ids):
        return None

    def save(self):
        return None

    def add(self, _ids, _vecs):
        return None


def _add_junk_content(path: Path) -> int:
    path.write_bytes(b"not an image")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO sources(path, label, enabled, embed_enabled) VALUES (?, ?, 1, 1)",
            (str(path.parent), "test"),
        )
        sid = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO photo_contents(file_hash, size, embedding_ready) VALUES ('junk', 12, 0)"
        )
        cid = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO photos(
                content_id, source_id, path, rel_path, mtime, album,
                culled, missing, is_keeper
            ) VALUES (?, ?, ?, ?, 0, '', 0, 0, 1)
            """,
            (cid, sid, str(path), path.name),
        )
    return cid


def test_unreadable_embed_is_marked_and_not_pending(tmp_path, monkeypatch):
    init_db()
    cid = _add_junk_content(tmp_path / "bad.jpg")
    monkeypatch.setattr(indexer, "get_store", lambda: _FakeStore())

    def _boom(*_a, **_k):
        raise UnidentifiedImageError("cannot identify image file")

    monkeypatch.setattr(indexer, "embed_images", _boom)

    remaining, skipped = indexer._embed_pending([(cid, tmp_path / "bad.jpg")])
    assert remaining == []
    assert skipped == 1
    with db() as conn:
        ready = int(
            conn.execute(
                "SELECT embedding_ready FROM photo_contents WHERE id = ?", (cid,)
            ).fetchone()["embedding_ready"]
        )
    assert ready == indexer.EMBED_UNUSABLE
    assert indexer.count_pending_embeds() == 0
