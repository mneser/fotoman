from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from fotoman.config import DB_PATH, DEFAULT_SOURCES, FAISS_PATH, IDS_PATH, THUMBS_DIR, ensure_dirs

# Serialize all SQLite access across FastAPI/worker threads.
_db_lock = threading.RLock()

# Joined location + content row for reads (mirrors the old photos shape).
PHOTO_SELECT = """
SELECT
  p.id, p.source_id, p.path, p.rel_path, p.mtime, p.album,
  p.culled, p.missing, p.group_id, p.is_keeper, p.content_id,
  s.label AS source_label,
  c.file_hash, c.size, c.width, c.height, c.taken_at, c.camera,
  c.lat, c.lon, c.gps, c.venue, c.city, c.region, c.country, c.place_label,
  c.orientation, c.mirrored, c.fingerprint, c.sharpness, c.exposure, c.quality,
  c.embedding_ready, c.faces_ready, c.indexed_at
FROM photos p
JOIN photo_contents c ON c.id = p.content_id
JOIN sources s ON s.id = p.source_id
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    face_enabled INTEGER NOT NULL DEFAULT 0,
    embed_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    scanned_at TEXT,
    listed_at TEXT,
    listed_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS photo_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL UNIQUE,
    size INTEGER,
    width INTEGER,
    height INTEGER,
    taken_at TEXT,
    taken_at_source TEXT,
    camera TEXT,
    lat REAL,
    lon REAL,
    gps INTEGER NOT NULL DEFAULT 0,
    venue TEXT,
    city TEXT,
    region TEXT,
    country TEXT,
    place_label TEXT,
    orientation INTEGER NOT NULL DEFAULT 0,
    mirrored INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT,
    sharpness REAL,
    exposure REAL,
    quality REAL,
    embedding_ready INTEGER NOT NULL DEFAULT 0,
    faces_ready INTEGER NOT NULL DEFAULT 0,
    near_edges_ready INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contents_hash ON photo_contents(file_hash);
CREATE INDEX IF NOT EXISTS idx_contents_taken ON photo_contents(taken_at);
CREATE INDEX IF NOT EXISTS idx_contents_gps ON photo_contents(lat, lon);
CREATE INDEX IF NOT EXISTS idx_contents_place ON photo_contents(place_label);
CREATE INDEX IF NOT EXISTS idx_contents_embed ON photo_contents(embedding_ready);
CREATE INDEX IF NOT EXISTS idx_contents_faces ON photo_contents(faces_ready);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES photo_contents(id),
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    mtime REAL,
    album TEXT,
    culled INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    group_id INTEGER,
    is_keeper INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_id, path)
);

CREATE INDEX IF NOT EXISTS idx_photos_content ON photos(content_id);
CREATE INDEX IF NOT EXISTS idx_photos_source ON photos(source_id);
CREATE INDEX IF NOT EXISTS idx_photos_group ON photos(group_id);
CREATE INDEX IF NOT EXISTS idx_photos_culled ON photos(culled);

CREATE TABLE IF NOT EXISTS photo_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, -- exact | formatted | near
    keeper_id INTEGER,
    member_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Loosest near-dup graph (content-level). Regroup filters by slider threshold.
CREATE TABLE IF NOT EXISTS content_near_edges (
    content_a INTEGER NOT NULL REFERENCES photo_contents(id) ON DELETE CASCADE,
    content_b INTEGER NOT NULL REFERENCES photo_contents(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    PRIMARY KEY (content_a, content_b),
    CHECK (content_a < content_b)
);
CREATE INDEX IF NOT EXISTS idx_near_edges_b ON content_near_edges(content_b);

CREATE TABLE IF NOT EXISTS place_geo_cache (
    query TEXT PRIMARY KEY,
    city TEXT NOT NULL,
    country TEXT NOT NULL,
    region TEXT,
    display_name TEXT,
    lat REAL,
    lon REAL,
    found INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS place_geo_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'city',
    city TEXT,
    region TEXT,
    country TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS index_job (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    running INTEGER NOT NULL DEFAULT 0,
    phase TEXT NOT NULL DEFAULT 'idle',
    current INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error TEXT,
    reembed INTEGER NOT NULL DEFAULT 0,
    source_id INTEGER,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    cover_face_id INTEGER,
    face_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name_unique
ON people(lower(trim(name)))
WHERE name IS NOT NULL AND trim(name) != '';

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id INTEGER NOT NULL REFERENCES photo_contents(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    bbox_x REAL NOT NULL,
    bbox_y REAL NOT NULL,
    bbox_w REAL NOT NULL,
    bbox_h REAL NOT NULL,
    det_score REAL,
    embedding BLOB,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_faces_content ON faces(content_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);

CREATE TABLE IF NOT EXISTS virtual_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_virtual_folders_name_unique
ON virtual_folders(lower(trim(name)));

CREATE TABLE IF NOT EXISTS virtual_folder_members (
    folder_id INTEGER NOT NULL REFERENCES virtual_folders(id) ON DELETE CASCADE,
    content_id INTEGER NOT NULL REFERENCES photo_contents(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (folder_id, content_id)
);

CREATE INDEX IF NOT EXISTS idx_vfolder_members_content ON virtual_folder_members(content_id);
"""


def content_thumb_path(content_id: int) -> Path:
    return THUMBS_DIR / "content" / f"{int(content_id)}.jpg"


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    with _db_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_people_name_unique(conn: sqlite3.Connection) -> None:
    """Merge duplicate person labels, then enforce a case-insensitive unique name."""
    if not _table_exists(conn, "people"):
        return
    conn.execute(
        "UPDATE people SET name = NULL WHERE name IS NOT NULL AND trim(name) = ''"
    )
    dup_keys = conn.execute(
        """
        SELECT lower(trim(name)) AS key
        FROM people
        WHERE name IS NOT NULL AND trim(name) != ''
        GROUP BY lower(trim(name))
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in dup_keys:
        members = conn.execute(
            """
            SELECT p.id,
                   (SELECT COUNT(*) FROM faces f WHERE f.person_id = p.id) AS fc
            FROM people p
            WHERE p.name IS NOT NULL AND lower(trim(p.name)) = ?
            ORDER BY fc DESC, p.id
            """,
            (row["key"],),
        ).fetchall()
        if len(members) < 2:
            continue
        keep_id = int(members[0]["id"])
        for other in members[1:]:
            oid = int(other["id"])
            conn.execute(
                "UPDATE faces SET person_id = ? WHERE person_id = ?",
                (keep_id, oid),
            )
            conn.execute("DELETE FROM people WHERE id = ?", (oid,))
        conn.execute(
            """
            UPDATE people SET
              face_count = (SELECT COUNT(*) FROM faces WHERE person_id = people.id),
              cover_face_id = (
                SELECT id FROM faces WHERE person_id = people.id ORDER BY id LIMIT 1
              )
            WHERE id = ?
            """,
            (keep_id,),
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name_unique
        ON people(lower(trim(name)))
        WHERE name IS NOT NULL AND trim(name) != ''
        """
    )


def _ensure_source_columns(conn: sqlite3.Connection) -> None:
    src_cols = _columns(conn, "sources") if _table_exists(conn, "sources") else set()
    if src_cols and "scanned_at" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN scanned_at TEXT")
        conn.execute(
            """
            UPDATE sources SET scanned_at = datetime('now')
            WHERE scanned_at IS NULL
              AND EXISTS (SELECT 1 FROM photos p WHERE p.source_id = sources.id)
            """
        )
    if src_cols and "face_enabled" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN face_enabled INTEGER NOT NULL DEFAULT 0")
    if src_cols and "embed_enabled" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN embed_enabled INTEGER NOT NULL DEFAULT 0")
    if src_cols and "listed_at" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN listed_at TEXT")
    if src_cols and "listed_count" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN listed_count INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """
            UPDATE sources SET
              listed_count = (SELECT COUNT(*) FROM photos p WHERE p.source_id = sources.id),
              listed_at = COALESCE(scanned_at, datetime('now'))
            WHERE EXISTS (SELECT 1 FROM photos p WHERE p.source_id = sources.id)
            """
        )
    pc_cols = _columns(conn, "photo_contents") if _table_exists(conn, "photo_contents") else set()
    photo_cols = _columns(conn, "photos") if _table_exists(conn, "photos") else set()
    if photo_cols and "missing" not in photo_cols:
        conn.execute(
            "ALTER TABLE photos ADD COLUMN missing INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_photos_missing ON photos(missing)"
        )
    if pc_cols and "fingerprint" not in pc_cols:
        conn.execute("ALTER TABLE photo_contents ADD COLUMN fingerprint TEXT")
    if pc_cols and "near_edges_ready" not in pc_cols:
        conn.execute(
            "ALTER TABLE photo_contents ADD COLUMN near_edges_ready INTEGER NOT NULL DEFAULT 0"
        )
    if pc_cols and "orientation" not in pc_cols:
        conn.execute(
            "ALTER TABLE photo_contents ADD COLUMN orientation INTEGER NOT NULL DEFAULT 0"
        )
    if pc_cols and "mirrored" not in pc_cols:
        conn.execute(
            "ALTER TABLE photo_contents ADD COLUMN mirrored INTEGER NOT NULL DEFAULT 0"
        )
    if pc_cols and "gps" not in pc_cols:
        # One-shot: add provenance flag, drop invented coords/places, force EXIF re-read.
        conn.execute(
            "ALTER TABLE photo_contents ADD COLUMN gps INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute(
            """
            UPDATE photo_contents SET
                lat = NULL, lon = NULL, gps = 0,
                city = NULL, region = NULL, country = NULL, place_label = NULL,
                width = NULL
            """
        )
        conn.execute(
            """
            DELETE FROM place_geo_cache
            WHERE lower(query) LIKE 'freeform|trash%'
               OR lower(query) LIKE 'freeform|shared%'
            """
        )
    pc_cols = _columns(conn, "photo_contents") if _table_exists(conn, "photo_contents") else set()
    if pc_cols and "venue" not in pc_cols:
        conn.execute("ALTER TABLE photo_contents ADD COLUMN venue TEXT")
    if pc_cols and "sharpness" not in pc_cols:
        conn.execute("ALTER TABLE photo_contents ADD COLUMN sharpness REAL")
    if pc_cols and "exposure" not in pc_cols:
        conn.execute("ALTER TABLE photo_contents ADD COLUMN exposure REAL")
    if pc_cols and "quality" not in pc_cols:
        conn.execute("ALTER TABLE photo_contents ADD COLUMN quality REAL")
    if pc_cols and "taken_at_source" not in pc_cols:
        conn.execute("ALTER TABLE photo_contents ADD COLUMN taken_at_source TEXT")
        # Existing dated rows were EXIF (or manual without provenance); leave NULL
        # so backfill does not overwrite them with filename/mtime.
    cache_cols = (
        _columns(conn, "place_geo_cache") if _table_exists(conn, "place_geo_cache") else set()
    )
    if cache_cols and "region" not in cache_cols:
        conn.execute("ALTER TABLE place_geo_cache ADD COLUMN region TEXT")
    if cache_cols and "display_name" not in cache_cols:
        conn.execute("ALTER TABLE place_geo_cache ADD COLUMN display_name TEXT")
    job_cols = _columns(conn, "index_job") if _table_exists(conn, "index_job") else set()
    if job_cols and "source_id" not in job_cols:
        conn.execute("ALTER TABLE index_job ADD COLUMN source_id INTEGER")


def _pick_better_content(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Merge two photo rows into the richer content payload."""
    out = dict(a)
    for key in (
        "size",
        "width",
        "height",
        "taken_at",
        "camera",
        "lat",
        "lon",
        "venue",
        "city",
        "region",
        "country",
        "place_label",
        "indexed_at",
    ):
        if out.get(key) in (None, "") and b.get(key) not in (None, ""):
            out[key] = b[key]
    if int(b.get("embedding_ready") or 0) and not int(out.get("embedding_ready") or 0):
        out["embedding_ready"] = 1
        out["_embed_from_id"] = b["id"]
    if int(b.get("faces_ready") or 0) and not int(out.get("faces_ready") or 0):
        out["faces_ready"] = 1
        out["_faces_from_id"] = b["id"]
    if "_embed_from_id" not in out and int(out.get("embedding_ready") or 0):
        out["_embed_from_id"] = out["id"]
    if "_faces_from_id" not in out and int(out.get("faces_ready") or 0):
        out["_faces_from_id"] = out["id"]
    return out


def _migrate_legacy_photos_to_contents(conn: sqlite3.Connection) -> bool:
    """Convert path-centric photos rows into locations + hash-keyed contents.

    Returns True if a migration ran.
    """
    if not _table_exists(conn, "photos"):
        return False
    cols = _columns(conn, "photos")
    # Already on the new schema.
    if "content_id" in cols and "file_hash" not in cols:
        return False
    # New empty DB created with SCHEMA already has content_id and no legacy cols.
    if "content_id" in cols and _table_exists(conn, "photo_contents"):
        # Partial? Ensure contents exist.
        orphan = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE content_id IS NULL"
        ).fetchone()["c"]
        if orphan == 0 and "file_hash" not in cols:
            return False

    print("Migrating photos → photo_contents (preserving embeddings/faces)…")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS photo_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash TEXT NOT NULL UNIQUE,
            size INTEGER,
            width INTEGER,
            height INTEGER,
            taken_at TEXT,
            camera TEXT,
            lat REAL,
            lon REAL,
            city TEXT,
            region TEXT,
            country TEXT,
            place_label TEXT,
            embedding_ready INTEGER NOT NULL DEFAULT 0,
            faces_ready INTEGER NOT NULL DEFAULT 0,
            indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    legacy = [dict(r) for r in conn.execute("SELECT * FROM photos").fetchall()]
    # hash -> merged content draft (includes legacy photo ids for remaps)
    by_hash: dict[str, dict[str, Any]] = {}
    # photo_id -> hash key used
    photo_hash: dict[int, str] = {}

    for row in legacy:
        pid = int(row["id"])
        fh = (row.get("file_hash") or "").strip()
        if not fh:
            fh = f"legacy-photo-{pid}"
        photo_hash[pid] = fh
        if fh not in by_hash:
            draft = dict(row)
            draft["_embed_from_id"] = pid if int(row.get("embedding_ready") or 0) else None
            draft["_faces_from_id"] = pid if int(row.get("faces_ready") or 0) else None
            draft["file_hash"] = fh
            by_hash[fh] = draft
        else:
            by_hash[fh] = _pick_better_content(by_hash[fh], row)

    hash_to_content: dict[str, int] = {}
    photo_to_content: dict[int, int] = {}
    embed_photo_to_content: dict[int, int] = {}
    faces_photo_to_content: dict[int, int] = {}

    for fh, draft in by_hash.items():
        cur = conn.execute(
            """
            INSERT INTO photo_contents(
                file_hash, size, width, height, taken_at, camera, lat, lon,
                city, region, country, place_label,
                embedding_ready, faces_ready, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
            ON CONFLICT(file_hash) DO UPDATE SET
                size=COALESCE(excluded.size, photo_contents.size),
                width=COALESCE(excluded.width, photo_contents.width),
                height=COALESCE(excluded.height, photo_contents.height),
                taken_at=COALESCE(excluded.taken_at, photo_contents.taken_at),
                camera=COALESCE(excluded.camera, photo_contents.camera),
                lat=COALESCE(excluded.lat, photo_contents.lat),
                lon=COALESCE(excluded.lon, photo_contents.lon),
                city=COALESCE(excluded.city, photo_contents.city),
                region=COALESCE(excluded.region, photo_contents.region),
                country=COALESCE(excluded.country, photo_contents.country),
                place_label=COALESCE(excluded.place_label, photo_contents.place_label),
                embedding_ready=MAX(photo_contents.embedding_ready, excluded.embedding_ready),
                faces_ready=MAX(photo_contents.faces_ready, excluded.faces_ready)
            """,
            (
                fh,
                draft.get("size"),
                draft.get("width"),
                draft.get("height"),
                draft.get("taken_at"),
                draft.get("camera"),
                draft.get("lat"),
                draft.get("lon"),
                draft.get("city"),
                draft.get("region"),
                draft.get("country"),
                draft.get("place_label"),
                int(draft.get("embedding_ready") or 0),
                int(draft.get("faces_ready") or 0),
                draft.get("indexed_at"),
            ),
        )
        if cur.lastrowid:
            cid = int(cur.lastrowid)
        else:
            cid = int(
                conn.execute(
                    "SELECT id FROM photo_contents WHERE file_hash = ?", (fh,)
                ).fetchone()["id"]
            )
        hash_to_content[fh] = cid
        if draft.get("_embed_from_id"):
            embed_photo_to_content[int(draft["_embed_from_id"])] = cid
        if draft.get("_faces_from_id"):
            faces_photo_to_content[int(draft["_faces_from_id"])] = cid

    for pid, fh in photo_hash.items():
        photo_to_content[pid] = hash_to_content[fh]

    # Rebuild locations table without content columns; keep same photo ids.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE photos_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            mtime REAL,
            album TEXT,
            culled INTEGER NOT NULL DEFAULT 0,
            group_id INTEGER,
            is_keeper INTEGER NOT NULL DEFAULT 1,
            UNIQUE(source_id, path)
        )
        """
    )
    for row in legacy:
        pid = int(row["id"])
        conn.execute(
            """
            INSERT INTO photos_new(
                id, content_id, source_id, path, rel_path, mtime, album,
                culled, group_id, is_keeper
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pid,
                photo_to_content[pid],
                int(row["source_id"]),
                row["path"],
                row["rel_path"],
                row.get("mtime"),
                row.get("album"),
                int(row.get("culled") or 0),
                row.get("group_id"),
                int(row.get("is_keeper") if row.get("is_keeper") is not None else 1),
            ),
        )
    conn.execute("DROP TABLE photos")
    conn.execute("ALTER TABLE photos_new RENAME TO photos")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_content ON photos(content_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_source ON photos(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_group ON photos(group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_culled ON photos(culled)")

    # Faces: photo_id → content_id; keep all faces from one canonical photo per content.
    face_cols = _columns(conn, "faces") if _table_exists(conn, "faces") else set()
    if face_cols and "photo_id" in face_cols:
        conn.execute(
            """
            CREATE TABLE faces_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id INTEGER NOT NULL
                    REFERENCES photo_contents(id) ON DELETE CASCADE,
                person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
                bbox_x REAL NOT NULL,
                bbox_y REAL NOT NULL,
                bbox_w REAL NOT NULL,
                bbox_h REAL NOT NULL,
                det_score REAL,
                embedding BLOB,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        canon_photo_for_content = {
            cid: pid for pid, cid in faces_photo_to_content.items()
        }
        rows = [dict(r) for r in conn.execute("SELECT * FROM faces").fetchall()]
        # Fallback: first photo_id seen per content when no faces_ready canonical.
        fallback_photo: dict[int, int] = {}
        for r in rows:
            pid = int(r["photo_id"])
            cid = photo_to_content.get(pid)
            if cid is None:
                continue
            if cid not in canon_photo_for_content and cid not in fallback_photo:
                fallback_photo[cid] = pid

        for r in rows:
            pid = int(r["photo_id"])
            cid = photo_to_content.get(pid)
            if cid is None:
                continue
            canon = canon_photo_for_content.get(cid, fallback_photo.get(cid))
            if canon is None or pid != canon:
                continue
            conn.execute(
                """
                INSERT INTO faces_new(
                    id, content_id, person_id, bbox_x, bbox_y, bbox_w, bbox_h,
                    det_score, embedding, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["id"],
                    cid,
                    r.get("person_id"),
                    r["bbox_x"],
                    r["bbox_y"],
                    r["bbox_w"],
                    r["bbox_h"],
                    r.get("det_score"),
                    r.get("embedding"),
                    r.get("created_at"),
                ),
            )
        conn.execute("DROP TABLE faces")
        conn.execute("ALTER TABLE faces_new RENAME TO faces")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_content ON faces(content_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id)")
        # Mark contents that retained faces.
        conn.execute(
            """
            UPDATE photo_contents SET faces_ready = 1
            WHERE id IN (SELECT DISTINCT content_id FROM faces)
            """
        )

    conn.execute("PRAGMA foreign_keys = ON")

    # Thumbs: photo_id.jpg → content/{content_id}.jpg
    (THUMBS_DIR / "content").mkdir(parents=True, exist_ok=True)
    for pid, cid in photo_to_content.items():
        src = THUMBS_DIR / f"{pid}.jpg"
        dst = content_thumb_path(cid)
        if src.exists() and not dst.exists():
            try:
                src.replace(dst)
            except OSError:
                shutil.copy2(src, dst)
        elif src.exists() and dst.exists():
            src.unlink(missing_ok=True)

    # Stash FAISS remap data for after this transaction commits.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrate_faiss_map (photo_id INTEGER PRIMARY KEY, content_id INTEGER NOT NULL)"
    )
    conn.execute("DELETE FROM _migrate_faiss_map")
    conn.executemany(
        "INSERT INTO _migrate_faiss_map(photo_id, content_id) VALUES (?, ?)",
        list(photo_to_content.items()),
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrate_faiss_embed (photo_id INTEGER PRIMARY KEY, content_id INTEGER NOT NULL)"
    )
    conn.execute("DELETE FROM _migrate_faiss_embed")
    conn.executemany(
        "INSERT INTO _migrate_faiss_embed(photo_id, content_id) VALUES (?, ?)",
        list(embed_photo_to_content.items()),
    )

    print(
        f"Migration done: {len(legacy)} locations → {len(hash_to_content)} unique contents."
    )
    return True


def _finish_faiss_remap() -> None:
    """Remap FAISS ids after the contents migration transaction committed."""
    with db() as conn:
        if not _table_exists(conn, "_migrate_faiss_map"):
            return
        photo_to_content = {
            int(r["photo_id"]): int(r["content_id"])
            for r in conn.execute("SELECT * FROM _migrate_faiss_map")
        }
        embed_photo_to_content = {
            int(r["photo_id"]): int(r["content_id"])
            for r in conn.execute("SELECT * FROM _migrate_faiss_embed")
        }
        conn.execute("DROP TABLE IF EXISTS _migrate_faiss_map")
        conn.execute("DROP TABLE IF EXISTS _migrate_faiss_embed")
    if not photo_to_content:
        return
    _remap_faiss(photo_to_content, embed_photo_to_content)


def _remap_faiss(
    photo_to_content: dict[int, int],
    embed_photo_to_content: dict[int, int],
) -> None:
    if not FAISS_PATH.exists() or not IDS_PATH.exists():
        return
    try:
        import faiss
    except ImportError:
        return

    index = faiss.read_index(str(FAISS_PATH))
    old_ids: list[int] = json.loads(IDS_PATH.read_text())
    if index.ntotal == 0 or not old_ids:
        return

    preferred_photo = {cid: pid for pid, cid in embed_photo_to_content.items()}
    content_vec: dict[int, np.ndarray] = {}

    for i, pid in enumerate(old_ids):
        cid = photo_to_content.get(int(pid))
        if cid is None:
            continue
        vec = index.reconstruct(i).astype(np.float32)
        if cid in preferred_photo and int(pid) == preferred_photo[cid]:
            content_vec[cid] = vec
        elif cid not in content_vec:
            content_vec[cid] = vec

    if not content_vec:
        return

    new_ids = list(content_vec.keys())
    mat = np.vstack([content_vec[cid] for cid in new_ids]).astype(np.float32)
    faiss.normalize_L2(mat)
    new_index = faiss.IndexFlatIP(index.d)
    new_index.add(mat)
    ensure_dirs()
    faiss.write_index(new_index, str(FAISS_PATH))
    IDS_PATH.write_text(json.dumps(new_ids))

    with db() as conn:
        conn.execute("UPDATE photo_contents SET embedding_ready = 0")
        for chunk_start in range(0, len(new_ids), 400):
            chunk = new_ids[chunk_start : chunk_start + 400]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE photo_contents SET embedding_ready = 1 WHERE id IN ({placeholders})",
                chunk,
            )


def _photos_has_global_path_unique(conn: sqlite3.Connection) -> bool:
    """True when photos.path is globally unique (blocks multi-archive membership)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='photos'"
    ).fetchone()
    if not row or not row[0]:
        return False
    sql = " ".join(row[0].split())
    if "UNIQUE(source_id, path)" in sql.replace(" ", ""):
        return False
    # Legacy: `path TEXT NOT NULL UNIQUE` (column-level).
    if "path TEXT NOT NULL UNIQUE" in sql:
        return True
    # Unique index on path alone.
    for idx in conn.execute("PRAGMA index_list(photos)").fetchall():
        if not idx["unique"]:
            continue
        cols = [
            r["name"]
            for r in conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
        ]
        if cols == ["path"]:
            return True
    return False


def _migrate_photos_multi_archive(conn: sqlite3.Connection) -> bool:
    """Allow the same absolute path under multiple archives (UNIQUE source_id+path)."""
    if not _table_exists(conn, "photos"):
        return False
    if not _photos_has_global_path_unique(conn):
        return False
    if "content_id" not in _columns(conn, "photos"):
        return False

    print("Migrating photos: allow same path in multiple archives…")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE photos_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            rel_path TEXT NOT NULL,
            mtime REAL,
            album TEXT,
            culled INTEGER NOT NULL DEFAULT 0,
            group_id INTEGER,
            is_keeper INTEGER NOT NULL DEFAULT 1,
            UNIQUE(source_id, path)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO photos_new(
            id, content_id, source_id, path, rel_path, mtime, album,
            culled, group_id, is_keeper
        )
        SELECT id, content_id, source_id, path, rel_path, mtime, album,
               culled, group_id, is_keeper
        FROM photos
        """
    )
    conn.execute("DROP TABLE photos")
    conn.execute("ALTER TABLE photos_new RENAME TO photos")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_content ON photos(content_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_source ON photos(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_group ON photos(group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_culled ON photos(culled)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_path ON photos(path)")
    conn.execute("PRAGMA foreign_keys = ON")
    return True


def _faces_has_person_fk(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "faces"):
        return True
    for fk in conn.execute("PRAGMA foreign_key_list(faces)"):
        if fk["table"] == "people" and fk["from"] == "person_id":
            return True
    return False


def _repair_orphan_face_people(conn: sqlite3.Connection) -> int:
    """Reattach faces whose person_id is missing (keeps shared ids together)."""
    if not _table_exists(conn, "faces") or not _table_exists(conn, "people"):
        return 0
    orphans = conn.execute(
        """
        SELECT f.id, f.person_id
        FROM faces f
        LEFT JOIN people p ON p.id = f.person_id
        WHERE p.id IS NULL
        """
    ).fetchall()
    if not orphans:
        return 0
    by_old: dict[int | None, list[int]] = {}
    for r in orphans:
        key = int(r["person_id"]) if r["person_id"] is not None else None
        by_old.setdefault(key, []).append(int(r["id"]))
    fixed = 0
    for old_pid, face_ids in by_old.items():
        if old_pid is None:
            for fid in face_ids:
                cur = conn.execute(
                    "INSERT INTO people (name, face_count) VALUES (NULL, 1)"
                )
                conn.execute(
                    "UPDATE faces SET person_id = ? WHERE id = ?",
                    (int(cur.lastrowid), fid),
                )
                fixed += 1
            continue
        cur = conn.execute(
            "INSERT INTO people (name, face_count) VALUES (NULL, ?)",
            (len(face_ids),),
        )
        new_pid = int(cur.lastrowid)
        conn.executemany(
            "UPDATE faces SET person_id = ? WHERE id = ?",
            [(new_pid, fid) for fid in face_ids],
        )
        fixed += len(face_ids)
    return fixed


def _ensure_faces_foreign_keys(conn: sqlite3.Connection) -> None:
    """Restore FKs dropped by the content-migration faces rebuild.

    Without people ON DELETE SET NULL, deleting empty people can leave
    faces pointing at missing person ids (labeling then fails).
    """
    if not _table_exists(conn, "faces") or _faces_has_person_fk(conn):
        return
    _repair_orphan_face_people(conn)
    # Drop faces that reference missing content (cannot satisfy FK).
    conn.execute(
        """
        DELETE FROM faces
        WHERE content_id NOT IN (SELECT id FROM photo_contents)
        """
    )
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        """
        CREATE TABLE faces_fk (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id INTEGER NOT NULL REFERENCES photo_contents(id) ON DELETE CASCADE,
            person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
            bbox_x REAL NOT NULL,
            bbox_y REAL NOT NULL,
            bbox_w REAL NOT NULL,
            bbox_h REAL NOT NULL,
            det_score REAL,
            embedding BLOB,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        INSERT INTO faces_fk(
            id, content_id, person_id, bbox_x, bbox_y, bbox_w, bbox_h,
            det_score, embedding, created_at
        )
        SELECT id, content_id, person_id, bbox_x, bbox_y, bbox_w, bbox_h,
               det_score, embedding, created_at
        FROM faces
        """
    )
    conn.execute("DROP TABLE faces")
    conn.execute("ALTER TABLE faces_fk RENAME TO faces")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_content ON faces(content_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id)")
    conn.execute("PRAGMA foreign_keys = ON")


def _ensure_content_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contents_hash ON photo_contents(file_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contents_taken ON photo_contents(taken_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contents_gps ON photo_contents(lat, lon)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contents_place ON photo_contents(place_label)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contents_embed ON photo_contents(embedding_ready)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_contents_faces ON photo_contents(faces_ready)")
    if "near_edges_ready" in _columns(conn, "photo_contents"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contents_near_edges ON photo_contents(near_edges_ready)"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS content_near_edges (
            content_a INTEGER NOT NULL REFERENCES photo_contents(id) ON DELETE CASCADE,
            content_b INTEGER NOT NULL REFERENCES photo_contents(id) ON DELETE CASCADE,
            score REAL NOT NULL,
            PRIMARY KEY (content_a, content_b),
            CHECK (content_a < content_b)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_near_edges_b ON content_near_edges(content_b)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_content ON photos(content_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_source ON photos(source_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_path ON photos(path)")
    # Only after _ensure_source_columns has added photos.missing on older DBs.
    if "missing" in _columns(conn, "photos"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_missing ON photos(missing)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_content ON faces(content_id)")


def _clear_null_island_gps(conn: sqlite3.Connection) -> int:
    """Strip placeholder GPS (0,0) that reverse-geocoded as Takoradi, Western, GH."""
    cur = conn.execute(
        """
        UPDATE photo_contents
        SET lat = NULL, lon = NULL, gps = 0,
            venue = NULL, city = NULL, region = NULL, country = NULL, place_label = NULL
        WHERE lat IS NOT NULL AND lon IS NOT NULL
          AND ABS(lat) < 1e-5 AND ABS(lon) < 1e-5
        """
    )
    return int(cur.rowcount or 0)


def _purge_orphan_photos(conn: sqlite3.Connection) -> int:
    """Delete location rows whose archive no longer exists.

    Past migrations dropped ``photos.source_id`` FK CASCADE, so removing an
    archive could leave dangling rows that inflate Places counts.
    """
    if not _table_exists(conn, "photos") or not _table_exists(conn, "sources"):
        return 0
    rows = conn.execute(
        """
        SELECT p.id, p.content_id
        FROM photos p
        LEFT JOIN sources s ON s.id = p.source_id
        WHERE s.id IS NULL
        """
    ).fetchall()
    if not rows:
        return 0
    conn.executemany(
        "DELETE FROM photos WHERE id = ?", [(int(r["id"]),) for r in rows]
    )
    # Keep photo_contents / embeddings even with no locations (content vault).
    return len(rows)


def init_db() -> None:
    ensure_dirs()
    (THUMBS_DIR / "content").mkdir(parents=True, exist_ok=True)
    migrated = False
    multi_archive = False
    with db() as conn:
        # Legacy DBs: migrate before applying the fresh SCHEMA creates.
        if _table_exists(conn, "photos") and "file_hash" in _columns(conn, "photos"):
            migrated = _migrate_legacy_photos_to_contents(conn)
        conn.executescript(SCHEMA)
        if _table_exists(conn, "faces") and "photo_id" in _columns(conn, "faces"):
            migrated = _migrate_legacy_photos_to_contents(conn) or migrated
        multi_archive = _migrate_photos_multi_archive(conn)
        _ensure_source_columns(conn)
        _ensure_people_name_unique(conn)
        _ensure_faces_foreign_keys(conn)
        _ensure_content_indexes(conn)
        _clear_null_island_gps(conn)
        _purge_orphan_photos(conn)
        from fotoman.geocode import (
            normalize_freeform_places,
            normalize_place_structure,
            normalize_quebec_province,
        )

        normalize_freeform_places(conn)
        normalize_place_structure(conn)
        normalize_quebec_province(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO index_job(id, running, phase, current, total, message, error, reembed)
            VALUES (1, 0, 'idle', 0, 0, '', NULL, 0)
            """
        )
        count = conn.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"]
        if count == 0:
            for path in DEFAULT_SOURCES:
                p = Path(path)
                if p.exists():
                    conn.execute(
                        "INSERT OR IGNORE INTO sources(path, label) VALUES (?, ?)",
                        (str(p.resolve()), p.name or str(p)),
                    )
    if migrated:
        _finish_faiss_remap()
        try:
            from fotoman.index_store import reload_store

            reload_store()
        except Exception:
            pass
    if multi_archive:
        # Re-list/index every archive so overlapping paths become locations in each.
        try:
            from fotoman import indexer

            indexer.start_scan_all_enabled(high_priority=False)
        except Exception:
            pass


def get_index_job() -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM index_job WHERE id = 1").fetchone()
    if row is None:
        return {
            "running": 0,
            "phase": "idle",
            "current": 0,
            "total": 0,
            "message": "",
            "error": None,
            "reembed": 0,
            "source_id": None,
        }
    return dict(row)


def save_index_job(
    *,
    running: bool,
    phase: str,
    current: int,
    total: int,
    message: str,
    error: str | None,
    reembed: bool,
    source_id: int | None,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO index_job(id, running, phase, current, total, message, error, reembed, source_id, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                running=excluded.running,
                phase=excluded.phase,
                current=excluded.current,
                total=excluded.total,
                message=excluded.message,
                error=excluded.error,
                reembed=excluded.reembed,
                source_id=excluded.source_id,
                updated_at=excluded.updated_at
            """,
            (
                1 if running else 0,
                phase,
                current,
                total,
                message,
                error,
                1 if reembed else 0,
                source_id,
            ),
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def gc_orphaned_content(conn: sqlite3.Connection, content_id: int) -> None:
    """Explicitly delete content (faces/thumb/FAISS) when no locations remain.

    Not used by scan/archive-remove — content is retained as a durable vault
    so moves and remounts can hash-link without re-embedding. Call only from
    intentional purge paths.
    """
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM photos WHERE content_id = ?", (content_id,)
    ).fetchone()["c"]
    if n:
        return
    conn.execute("DELETE FROM faces WHERE content_id = ?", (content_id,))
    conn.execute(
        "DELETE FROM content_near_edges WHERE content_a = ? OR content_b = ?",
        (content_id, content_id),
    )
    conn.execute("DELETE FROM photo_contents WHERE id = ?", (content_id,))
    content_thumb_path(content_id).unlink(missing_ok=True)
    try:
        from fotoman.index_store import get_store

        store = get_store()
        store.remove_ids({int(content_id)})
        store.save()
    except Exception:
        pass


def purge_orphaned_contents() -> int:
    """Drop content rows with zero location references (thumbs, faces, FAISS)."""
    purged = 0
    with db() as conn:
        rows = conn.execute(
            """
            SELECT c.id
            FROM photo_contents c
            WHERE NOT EXISTS (
              SELECT 1 FROM photos p WHERE p.content_id = c.id
            )
            """
        ).fetchall()
        for r in rows:
            gc_orphaned_content(conn, int(r["id"]))
            purged += 1
    return purged
