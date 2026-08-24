"""Face detection DB: YuNet detect + SFace embed + cluster into people."""

from __future__ import annotations

import re
import sqlite3
import threading
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import cv2
import faiss
import numpy as np

from fotoman.config import (
    FACE_MATCH_THRESHOLD,
    SFACE_MODEL,
    SFACE_URL,
    THUMBS_DIR,
    YUNET_MODEL,
    YUNET_URL,
    ensure_dirs,
    load_settings,
)
from fotoman.db import content_thumb_path, db
from fotoman.events import publish

_state: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "current": 0,
    "total": 0,
    "message": "",
    "error": None,
    "people": 0,
    "faces": 0,
    "source_id": None,
    "queued": [],
    "queued_count": 0,
}
_lock = threading.RLock()
_detector = None
_recognizer = None
# Cleared while paused; set to allow the detect loop to continue.
_pause_gate = threading.Event()
_pause_gate.set()
_suspend_requested = False
# Per-archive face jobs waiting to run: {source_id, threshold}.
_pending: deque[dict[str, Any]] = deque()

# Skip detections smaller than this on the original image (px on the short side).
# Tiny crops produce noisy embeddings and unrealistically small boxes in the UI.
MIN_FACE_SIDE_PX = 32
# Also skip faces whose short side is below this fraction of the image dimension.
# Absolute px alone still lets flecks through on very large photos.
MIN_FACE_REL = 0.02
# Unlabeled people need at least one face this large to appear in the People tree.
TREE_FACE_SIDE_PX = 64


def get_status() -> dict[str, Any]:
    with _lock:
        snap = dict(_state)
        queued = [int(j["source_id"]) for j in _pending if j.get("source_id") is not None]
        snap["queued"] = queued
        snap["queued_count"] = len(queued)
        return snap


def _set(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)
        snap = dict(_state)
    publish("faces", snap)


def count_pending_faces(source_id: int | None = None) -> int:
    """Contents not yet facematched (faces_ready=0), optionally scoped to an archive."""
    with db() as conn:
        if source_id is None:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT c.id) AS c
                FROM photo_contents c
                JOIN photos p ON p.content_id = c.id
                WHERE p.culled = 0 AND p.missing = 0 AND c.faces_ready = 0
                """
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT c.id) AS c
                FROM photo_contents c
                JOIN photos p ON p.content_id = c.id
                WHERE p.culled = 0 AND p.missing = 0 AND c.faces_ready = 0 AND p.source_id = ?
                """,
                (source_id,),
            ).fetchone()
    return int(row["c"] if row else 0)


def sources_with_pending_faces() -> list[int]:
    """Enabled, mounted archives that still have unscanned contents."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.path
            FROM sources s
            WHERE s.enabled = 1
              AND IFNULL(s.kind, 'watched') != 'backup'
              AND EXISTS (
                SELECT 1 FROM photos p
                JOIN photo_contents c ON c.id = p.content_id
                WHERE p.source_id = s.id AND p.culled = 0 AND p.missing = 0 AND c.faces_ready = 0
              )
            ORDER BY s.id
            """
        ).fetchall()
    from fotoman import device

    return [int(r["id"]) for r in rows if device.source_exists(dict(r))]


def repair_orphan_person_links() -> int:
    """Reattach faces pointing at missing people (e.g. deletes with FKs off)."""
    with db() as conn:
        n = _repair_orphan_faces(conn)
        if n:
            _recalc_people(conn)
        return n


def repair_orphaned_ready_flags() -> int:
    """One-shot: clear faces_ready left set after an interrupted wipe-rebuild.

    Old rebuilds deleted face rows then marked ready during detect before
    clustering committed. Restart looked like facematched>0 with empty People.
    Safe only once — a finished library with zero faces is also faces=0+ready>0.
    """
    from fotoman.config import load_settings, save_settings

    settings = load_settings()
    if settings.get("faces_ready_repaired_v1"):
        return 0
    with db() as conn:
        n_faces = int(conn.execute("SELECT COUNT(*) AS c FROM faces").fetchone()["c"])
        n_ready = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM photo_contents WHERE faces_ready = 1"
            ).fetchone()["c"]
        )
        cleared = 0
        if n_faces == 0 and n_ready > 0:
            conn.execute("UPDATE photo_contents SET faces_ready = 0 WHERE faces_ready = 1")
            cleared = n_ready
    settings["faces_ready_repaired_v1"] = True
    save_settings(settings)
    return cleared


def purge_tiny_faces() -> int:
    """One-shot: drop face rows whose bbox is unrealistically small on the image."""
    from fotoman.config import load_settings, save_settings

    settings = load_settings()
    if settings.get("tiny_faces_purged_v2"):
        return 0
    deleted = 0
    with db() as conn:
        rows = conn.execute(
            """
            SELECT f.id
            FROM faces f
            JOIN photo_contents c ON c.id = f.content_id
            WHERE MIN(f.bbox_w * c.width, f.bbox_h * c.height) < ?
               OR MIN(f.bbox_w, f.bbox_h) < ?
            """,
            (MIN_FACE_SIDE_PX, MIN_FACE_REL),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        face_dir = THUMBS_DIR / "faces"
        for fid in ids:
            if face_dir.exists():
                face_thumb_path(fid).unlink(missing_ok=True)
        for chunk in _chunked_ids(ids):
            placeholders = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM faces WHERE id IN ({placeholders})", chunk)
            deleted += len(chunk)
        if deleted:
            _recalc_people(conn)
    settings["tiny_faces_purged_v2"] = True
    save_settings(settings)
    return deleted


def _enqueue_face_job_unlocked(job: dict[str, Any]) -> None:
    """Queue a per-archive face job (dedupe by source_id). Caller holds _lock."""
    sid = job.get("source_id")
    if sid is None:
        return
    kept: deque[dict[str, Any]] = deque()
    for existing in _pending:
        if existing.get("source_id") == sid:
            continue
        kept.append(existing)
    kept.append({"source_id": int(sid), "threshold": job.get("threshold")})
    _pending.clear()
    _pending.extend(kept)


def _dequeue_face_job_unlocked(source_id: int) -> bool:
    """Remove a queued face job for this archive. Caller holds _lock."""
    before = len(_pending)
    kept: deque[dict[str, Any]] = deque()
    for existing in _pending:
        if existing.get("source_id") is not None and int(existing["source_id"]) == int(
            source_id
        ):
            continue
        kept.append(existing)
    _pending.clear()
    _pending.extend(kept)
    return len(_pending) < before


def _checkpoint_pause(*, resume_phase: str) -> None:
    """Block here if suspend was requested; keeps running=True with phase=suspended."""
    global _suspend_requested
    if not _suspend_requested:
        return
    _set(phase="suspended", message="Paused — face detection")
    while True:
        with _lock:
            if not _suspend_requested:
                break
        _pause_gate.wait(timeout=0.5)
    with _lock:
        if _state["phase"] == "suspended":
            _state["phase"] = resume_phase
            _state["message"] = "Resuming face detection…"


def suspend_faces(source_id: int | None = None) -> dict[str, Any]:
    """Pause active face scan for this archive, or remove it from the queue."""
    global _suspend_requested
    with _lock:
        if source_id is not None and _dequeue_face_job_unlocked(int(source_id)):
            return get_status()
        if not _state["running"]:
            return get_status()
        cur = _state.get("source_id")
        if source_id is not None and cur is not None and int(cur) != int(source_id):
            # Another archive is active — only queue removal applies (already tried).
            return get_status()
        phase = str(_state.get("phase") or "")
        if phase in ("done", "error", "idle"):
            return get_status()
        _suspend_requested = True
        if phase != "suspended":
            _state["message"] = "Pausing face detection…"
    _pause_gate.clear()
    return get_status()


def resume_faces(source_id: int | None = None) -> dict[str, Any]:
    """Resume a paused in-process job, or enqueue/start face scan for an archive."""
    global _suspend_requested
    with _lock:
        running = bool(_state["running"])
        phase = str(_state.get("phase") or "")
        cur = _state.get("source_id")
        if running and phase == "suspended":
            if source_id is not None and cur is not None and int(cur) != int(source_id):
                # Paused job is for another archive — queue this one instead.
                pass
            else:
                _suspend_requested = False
                _state["message"] = "Resuming face detection…"
                _pause_gate.set()
                return get_status()

    if source_id is not None:
        return start_rebuild_background(source_id=int(source_id))
    with _lock:
        if not _state["running"] and _state.get("phase") != "suspended":
            return get_status()
        _suspend_requested = False
        if _state["phase"] == "suspended":
            _state["message"] = "Resuming face detection…"
    _pause_gate.set()
    return get_status()


def _ensure_models() -> None:
    ensure_dirs()
    for path, url in ((YUNET_MODEL, YUNET_URL), (SFACE_MODEL, SFACE_URL)):
        if path.exists() and path.stat().st_size > 0:
            continue
        tmp = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(path)


def _get_models():
    global _detector, _recognizer
    if _detector is not None and _recognizer is not None:
        return _detector, _recognizer
    _ensure_models()
    _detector = cv2.FaceDetectorYN.create(
        str(YUNET_MODEL), "", (320, 320), 0.7, 0.3, 5000
    )
    _recognizer = cv2.FaceRecognizerSF.create(str(SFACE_MODEL), "")
    return _detector, _recognizer


def _load_bgr(path: Path) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return img


def _detect_and_embed(path: Path) -> list[dict[str, Any]]:
    detector, recognizer = _get_models()
    img = _load_bgr(path)
    if img is None:
        return []
    h, w = img.shape[:2]
    scale = 1.0
    max_side = 1280
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
        h, w = img.shape[:2]

    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return []

    out: list[dict[str, Any]] = []
    for row in faces:
        bx, by, bw, bh = float(row[0]), float(row[1]), float(row[2]), float(row[3])
        if scale != 1.0:
            obx, oby, obw, obh = bx / scale, by / scale, bw / scale, bh / scale
            ow, oh = w / scale, h / scale
        else:
            obx, oby, obw, obh = bx, by, bw, bh
            ow, oh = float(w), float(h)
        if min(obw, obh) < MIN_FACE_SIDE_PX:
            continue
        if min(obw / ow, obh / oh) < MIN_FACE_REL:
            continue
        face_row = row.reshape(1, -1)
        try:
            aligned = recognizer.alignCrop(img, face_row)
            feat = recognizer.feature(aligned)
        except cv2.error:
            continue
        vec = np.asarray(feat, dtype=np.float32).reshape(-1)
        if vec.size == 0:
            continue
        out.append(
            {
                "bbox_x": max(0.0, obx / ow),
                "bbox_y": max(0.0, oby / oh),
                "bbox_w": min(1.0, obw / ow),
                "bbox_h": min(1.0, obh / oh),
                "det_score": float(row[14]) if len(row) > 14 else None,
                "embedding": vec,
            }
        )
    return out


def _cluster(embeddings: np.ndarray, threshold: float) -> list[int]:
    """Union-find clustering by cosine similarity. Returns cluster id per row."""
    n = embeddings.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [0]

    mat = np.ascontiguousarray(embeddings.astype(np.float32))
    faiss.normalize_L2(mat)
    index = faiss.IndexFlatIP(mat.shape[1])
    index.add(mat)

    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    k = min(32, n)
    scores, neighbors = index.search(mat, k)
    for i in range(n):
        for score, j in zip(scores[i], neighbors[i]):
            if j < 0 or int(j) <= i or float(score) < threshold:
                continue
            union(i, int(j))

    roots = [find(i) for i in range(n)]
    remap: dict[int, int] = {}
    labels: list[int] = []
    for r in roots:
        if r not in remap:
            remap[r] = len(remap)
        labels.append(remap[r])
    return labels


# Face belongs to at least one non-removed photo location.
_LIVE_FACE_SQL = """
EXISTS (
  SELECT 1 FROM photos ph
  WHERE ph.content_id = f.content_id AND ph.culled = 0 AND ph.missing = 0
)
"""


def _existing_person_centroids() -> list[tuple[int, str | None, np.ndarray]]:
    """person_id, name, L2-normalized mean embedding (live photos only)."""
    with db() as conn:
        rows = conn.execute(
            f"""
            SELECT p.id, p.name, f.embedding
            FROM people p
            JOIN faces f ON f.person_id = p.id
            WHERE f.embedding IS NOT NULL
              AND {_LIVE_FACE_SQL}
            """
        ).fetchall()
    by_id: dict[int, dict[str, Any]] = {}
    for r in rows:
        entry = by_id.setdefault(r["id"], {"name": r["name"], "vecs": []})
        vec = np.frombuffer(r["embedding"], dtype=np.float32)
        if vec.size:
            entry["vecs"].append(vec)
    out: list[tuple[int, str | None, np.ndarray]] = []
    for pid, data in by_id.items():
        if not data["vecs"]:
            continue
        mean = np.mean(np.stack(data["vecs"]), axis=0).astype(np.float32)
        faiss.normalize_L2(mean.reshape(1, -1))
        out.append((pid, data["name"], mean))
    return out


def _pick_cover_face_id(face_rows: list[Any]) -> int | None:
    """Face closest to the group mean embedding; else lowest id."""
    if not face_rows:
        return None
    vecs: list[np.ndarray] = []
    ids: list[int] = []
    for r in face_rows:
        emb = r["embedding"]
        if emb is None:
            continue
        v = np.frombuffer(emb, dtype=np.float32)
        if v.size:
            vecs.append(v)
            ids.append(int(r["id"]))
    if vecs:
        mean = np.mean(np.stack(vecs), axis=0).astype(np.float32)
        mean_n = float(np.linalg.norm(mean)) + 1e-9
        mean = mean / mean_n
        best_id = ids[0]
        best_sim = -1.0
        for fid, v in zip(ids, vecs):
            vn = v / (float(np.linalg.norm(v)) + 1e-9)
            sim = float(vn @ mean)
            if sim > best_sim:
                best_sim = sim
                best_id = fid
        return best_id
    return min(int(r["id"]) for r in face_rows)


def _repair_orphan_faces(conn) -> int:
    """Reattach faces whose person_id is NULL or points at a deleted person.

    Faces that shared a missing person_id stay together on one new person.
    """
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
            # Unassigned: each face gets its own person shell.
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


def _recalc_people(conn, person_ids: list[int] | None = None) -> None:
    """Refresh face_count, drop empties, refresh covers.

    When ``person_ids`` is set, only those people are touched (label/merge hot path).
    Full recalc (``person_ids is None``) still repairs orphans and rebuilds everyone.
    """
    if person_ids is not None:
        ids = sorted({int(p) for p in person_ids if p is not None})
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"""
            UPDATE people SET
              face_count = (SELECT COUNT(*) FROM faces WHERE person_id = people.id)
            WHERE id IN ({placeholders})
            """,
            ids,
        )
        conn.execute(
            f"""
            DELETE FROM people
            WHERE id IN ({placeholders})
              AND NOT EXISTS (
                SELECT 1 FROM faces f WHERE f.person_id = people.id
              )
            """,
            ids,
        )
        surviving = [
            int(r["id"])
            for r in conn.execute(
                f"SELECT id FROM people WHERE id IN ({placeholders})", ids
            ).fetchall()
        ]
        if not surviving:
            return
        sph = ",".join("?" * len(surviving))
        rows = conn.execute(
            f"""
            SELECT f.person_id, f.id, f.embedding
            FROM faces f
            WHERE f.person_id IN ({sph})
              AND {_LIVE_FACE_SQL}
            """,
            surviving,
        ).fetchall()
        by_person: dict[int, list[Any]] = {pid: [] for pid in surviving}
        for r in rows:
            by_person[int(r["person_id"])].append(r)
        conn.executemany(
            "UPDATE people SET cover_face_id = ? WHERE id = ?",
            [
                (_pick_cover_face_id(face_rows) if face_rows else None, pid)
                for pid, face_rows in by_person.items()
            ],
        )
        return

    _repair_orphan_faces(conn)
    conn.execute(
        """
        UPDATE people SET
          face_count = (SELECT COUNT(*) FROM faces WHERE person_id = people.id)
        """
    )
    # Subquery delete: never drop a person that still has face rows
    # (stale face_count + missing FK used to create orphans).
    conn.execute(
        """
        DELETE FROM people
        WHERE NOT EXISTS (
          SELECT 1 FROM faces f WHERE f.person_id = people.id
        )
        """
    )
    # Covers / centers from live (non-removed) photos only.
    conn.execute("UPDATE people SET cover_face_id = NULL")
    rows = conn.execute(
        f"""
        SELECT f.person_id, f.id, f.embedding
        FROM faces f
        WHERE f.person_id IS NOT NULL
          AND {_LIVE_FACE_SQL}
        """
    ).fetchall()
    by_person: dict[int, list[Any]] = {}
    for r in rows:
        by_person.setdefault(int(r["person_id"]), []).append(r)
    updates: list[tuple[int | None, int]] = []
    for pid, face_rows in by_person.items():
        updates.append((_pick_cover_face_id(face_rows), pid))
    if updates:
        conn.executemany(
            "UPDATE people SET cover_face_id = ? WHERE id = ?",
            updates,
        )


def refresh_person_covers() -> None:
    """One-shot: replace oldest-face covers with centroid-nearest faces."""
    from fotoman.config import load_settings, save_settings

    settings = load_settings()
    if settings.get("person_covers_centroid_v1"):
        return
    with db() as conn:
        _recalc_people(conn)
    settings["person_covers_centroid_v1"] = True
    save_settings(settings)


def _chunked_ids(ids: list[int], size: int = 500):
    for i in range(0, len(ids), size):
        yield ids[i : i + size]


# Commit faces + faces_ready this often so restarts resume mid-archive.
_FACE_COMMIT_EVERY = 32


def _commit_detected_batch(
    *,
    ready_ids: list[int],
    detected: list[dict[str, Any]],
    thr: float,
    existing: list[tuple[int, str | None, np.ndarray]],
) -> tuple[int, list[tuple[int, str | None, np.ndarray]]]:
    """Cluster one batch, write faces, mark ready. Returns (n_people, refreshed centroids)."""
    if not detected:
        with db() as conn:
            conn.executemany(
                "UPDATE photo_contents SET faces_ready = 1 WHERE id = ?",
                [(cid,) for cid in ready_ids],
            )
            n_people = conn.execute("SELECT COUNT(*) AS c FROM people").fetchone()["c"]
        return int(n_people), existing

    emb = np.stack([d["embedding"] for d in detected]).astype(np.float32)
    labels = _cluster(emb, thr)
    cluster_ids = sorted(set(labels))

    centroids: dict[int, np.ndarray] = {}
    for cid in cluster_ids:
        idxs = [i for i, lab in enumerate(labels) if lab == cid]
        mean = np.mean(emb[idxs], axis=0).astype(np.float32)
        faiss.normalize_L2(mean.reshape(1, -1))
        centroids[cid] = mean

    # Match new clusters to people from prior commits / other archives.
    used_person_ids: set[int] = set()
    person_for_cluster: dict[int, int] = {}
    for cid, cent in centroids.items():
        best_pid = None
        best_score = thr
        for pid, _name, pcent in existing:
            if pid in used_person_ids:
                continue
            score = float(np.dot(cent, pcent))
            if score > best_score:
                best_score = score
                best_pid = pid
        if best_pid is not None:
            person_for_cluster[cid] = best_pid
            used_person_ids.add(best_pid)

    with db() as conn:
        for cid in cluster_ids:
            if cid in person_for_cluster:
                continue
            cur = conn.execute(
                "INSERT INTO people(name, face_count) VALUES (NULL, 0)"
            )
            person_for_cluster[cid] = int(cur.lastrowid)

        for i, face in enumerate(detected):
            pid = person_for_cluster[labels[i]]
            blob = face["embedding"].astype(np.float32).tobytes()
            conn.execute(
                """
                INSERT INTO faces(
                    content_id, person_id, bbox_x, bbox_y, bbox_w, bbox_h,
                    det_score, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    face["content_id"],
                    pid,
                    face["bbox_x"],
                    face["bbox_y"],
                    face["bbox_w"],
                    face["bbox_h"],
                    face["det_score"],
                    blob,
                ),
            )

        # Mark ready only after face rows are committed — survives restart.
        conn.executemany(
            "UPDATE photo_contents SET faces_ready = 1 WHERE id = ?",
            [(cid,) for cid in ready_ids],
        )
        _recalc_people(conn, list(person_for_cluster.values()))
        n_people = conn.execute("SELECT COUNT(*) AS c FROM people").fetchone()["c"]

    return int(n_people), _existing_person_centroids()


def start_rebuild_background(
    threshold: float | None = None,
    source_id: int | None = None,
) -> dict[str, Any]:
    """Start or enqueue face detection. Null source_id expands to pending archives."""
    global _suspend_requested

    if source_id is None:
        ids = sources_with_pending_faces()
        if not ids:
            _set(phase="done", message="All images already facematched", running=False)
            return get_status()
        start_rebuild_background(threshold=threshold, source_id=ids[0])
        for sid in ids[1:]:
            start_rebuild_background(threshold=threshold, source_id=sid)
        return get_status()

    sid = int(source_id)
    with _lock:
        # Resume in-place if this archive is the suspended active job.
        if (
            _state["running"]
            and _state.get("phase") == "suspended"
            and _state.get("source_id") is not None
            and int(_state["source_id"]) == sid
        ):
            _suspend_requested = False
            _state["message"] = "Resuming face detection…"
            _pause_gate.set()
            return get_status()

    # Preference may stay on, but do not start/queue when nothing is pending.
    if count_pending_faces(sid) == 0:
        return get_status()

    with _lock:
        if (
            _state["running"]
            and _state.get("phase") == "suspended"
            and _state.get("source_id") is not None
            and int(_state["source_id"]) == sid
        ):
            _suspend_requested = False
            _state["message"] = "Resuming face detection…"
            _pause_gate.set()
            return get_status()

        if _state["running"]:
            _enqueue_face_job_unlocked({"source_id": sid, "threshold": threshold})
            return get_status()

        _suspend_requested = False
        _pause_gate.set()
        _state.update(
            {
                "running": True,
                "phase": "starting",
                "current": 0,
                "total": 0,
                "message": "Starting…",
                "error": None,
                "people": 0,
                "faces": 0,
                "source_id": sid,
            }
        )
        first_threshold = threshold

    def run() -> None:
        # Face detect + SigLIP embed thrash the same CPU/RAM; pause heavy
        # indexer work for the duration and resume when we finish.
        global _suspend_requested
        from fotoman import indexer

        should_resume = False
        thr = first_threshold
        try:
            _set(
                phase="starting",
                message="Pausing embedding so face detection can run…",
            )
            should_resume = indexer.yield_for_faces()
            while True:
                with _lock:
                    cur_sid = _state.get("source_id")
                _rebuild_inner(thr, source_id=cur_sid)
                with _lock:
                    if not _pending:
                        break
                    nxt = _pending.popleft()
                    thr = nxt.get("threshold")
                    _suspend_requested = False
                    _pause_gate.set()
                    _state.update(
                        {
                            "phase": "starting",
                            "current": 0,
                            "total": 0,
                            "message": "Starting…",
                            "error": None,
                            "source_id": int(nxt["source_id"]),
                        }
                    )
        except Exception as e:
            _set(phase="error", error=str(e), message=str(e))
        finally:
            _suspend_requested = False
            _pause_gate.set()
            if should_resume:
                try:
                    indexer.resume_after_faces()
                except Exception:
                    pass
            _set(running=False)
            with _lock:
                if _state["phase"] not in ("error", "done"):
                    _state["phase"] = "done"

    threading.Thread(target=run, daemon=True).start()
    return get_status()


def rebuild(
    threshold: float | None = None,
    source_id: int | None = None,
) -> dict[str, Any]:
    """Detect faces on unscanned photos, cluster into people. Blocks until done."""
    with _lock:
        if _state["running"]:
            raise RuntimeError("Face detection already running")
        _state.update(
            {
                "running": True,
                "phase": "detect",
                "current": 0,
                "total": 0,
                "message": "Starting…",
                "error": None,
                "people": 0,
                "faces": 0,
                "source_id": source_id,
            }
        )
    try:
        return _rebuild_inner(threshold, source_id=source_id)
    except Exception as e:
        _set(phase="error", error=str(e), message=str(e))
        raise
    finally:
        _set(running=False)
        with _lock:
            if _state["phase"] not in ("error", "done"):
                _state["phase"] = "done"


def _rebuild_inner(
    threshold: float | None,
    source_id: int | None = None,
) -> dict[str, Any]:
    """Incremental face scan: only contents with faces_ready=0. Never wipe ready work.

    Detect → cluster → commit in batches of ``_FACE_COMMIT_EVERY`` so a restart
    resumes from remaining ``faces_ready=0`` instead of redoing the archive.
    """
    settings = load_settings()
    thr = float(
        threshold
        if threshold is not None
        else settings.get("face_match_threshold", FACE_MATCH_THRESHOLD)
    )
    _set(source_id=source_id, phase="detect")

    with db() as conn:
        if source_id is not None:
            contents = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT c.id AS content_id, MIN(p.path) AS path, c.faces_ready
                    FROM photo_contents c
                    JOIN photos p ON p.content_id = c.id
                    WHERE p.culled = 0 AND p.missing = 0 AND p.source_id = ? AND c.faces_ready = 0
                    GROUP BY c.id
                    ORDER BY c.id
                    """,
                    (source_id,),
                ).fetchall()
            ]
        else:
            contents = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT c.id AS content_id, MIN(p.path) AS path, c.faces_ready
                    FROM photo_contents c
                    JOIN photos p ON p.content_id = c.id
                    WHERE p.culled = 0 AND p.missing = 0 AND c.faces_ready = 0
                    GROUP BY c.id
                    ORDER BY c.id
                    """
                ).fetchall()
            ]
        # Drop stray face rows for unscanned contents only (never touch ready images).
        content_ids = [int(c["content_id"]) for c in contents]
        face_dir = THUMBS_DIR / "faces"
        for chunk in _chunked_ids(content_ids):
            placeholders = ",".join("?" * len(chunk))
            if face_dir.exists():
                for (fid,) in conn.execute(
                    f"SELECT id FROM faces WHERE content_id IN ({placeholders})", chunk
                ):
                    face_thumb_path(int(fid)).unlink(missing_ok=True)
            conn.execute(
                f"DELETE FROM faces WHERE content_id IN ({placeholders})", chunk
            )
        if content_ids:
            _recalc_people(conn)

    if not contents:
        with db() as conn:
            n_people = conn.execute("SELECT COUNT(*) AS c FROM people").fetchone()["c"]
            n_faces = conn.execute("SELECT COUNT(*) AS c FROM faces").fetchone()["c"]
        _set(
            phase="done",
            current=0,
            total=0,
            people=n_people,
            faces=n_faces,
            message="All images already facematched",
        )
        return {
            "people": n_people,
            "faces": n_faces,
            "threshold": thr,
            "source_id": source_id,
        }

    existing = _existing_person_centroids()

    _set(
        current=0,
        total=len(contents),
        message=f"Detecting faces in {len(contents)} images",
    )
    n_faces = 0
    n_people = 0

    for batch_start in range(0, len(contents), _FACE_COMMIT_EVERY):
        batch = contents[batch_start : batch_start + _FACE_COMMIT_EVERY]
        detected: list[dict[str, Any]] = []
        _set(phase="detect")

        for i, item in enumerate(batch):
            _checkpoint_pause(resume_phase="detect")
            from fotoman import device

            path = device.readable_path(item["path"]) or Path(item["path"])
            cid = int(item["content_id"])
            if path.exists():
                for face in _detect_and_embed(path):
                    detected.append({"content_id": cid, **face})
            _set(
                current=batch_start + i + 1,
                faces=n_faces + len(detected),
                message=f"Detected {n_faces + len(detected)} faces",
            )

        _checkpoint_pause(resume_phase="cluster")
        _set(phase="cluster", message="Clustering faces…")

        ready_ids = [int(c["content_id"]) for c in batch]
        n_people, existing = _commit_detected_batch(
            ready_ids=ready_ids,
            detected=detected,
            thr=thr,
            existing=existing,
        )
        n_faces += len(detected)
        _set(
            current=min(batch_start + len(batch), len(contents)),
            faces=n_faces,
            people=n_people,
            phase="detect",
            message=f"Detected {n_faces} faces",
        )

    if n_faces == 0:
        _set(phase="done", people=n_people, faces=0, message="No faces found")
        return {"people": n_people, "faces": 0, "threshold": thr, "source_id": source_id}

    _set(
        phase="done",
        people=n_people,
        faces=n_faces,
        message=f"{n_people} people, {n_faces} faces",
    )
    return {
        "people": n_people,
        "faces": n_faces,
        "threshold": thr,
        "source_id": source_id,
    }


_PERSON_SELECT = """
    SELECT p.id, p.name, p.face_count, p.cover_face_id,
           (SELECT COUNT(DISTINCT f.content_id)
              FROM faces f
              JOIN photos ph ON ph.content_id = f.content_id
               AND ph.culled = 0 AND ph.missing = 0 AND ph.is_keeper = 1
             WHERE f.person_id = p.id) AS photo_count
    FROM people p
"""


class NameConflict(Exception):
    """Raised when renaming to a label already used by another person."""

    def __init__(self, existing: dict[str, Any]):
        self.existing = existing
        super().__init__("name_conflict")


_UNKNOWN_LABEL_RE = re.compile(r"^unknown\s+(\d+)$", re.IGNORECASE)


def is_person_labeled(name: str | None) -> bool:
    """True when the person has a real name (not empty / ``Unknown N``)."""
    label = (name or "").strip()
    return bool(label) and not _UNKNOWN_LABEL_RE.match(label)


def _person_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["display_name"] = (d["name"] or "").strip() or f"Person {d['id']}"
    d["labeled"] = is_person_labeled(d.get("name"))
    return d


def people_list(*, labeled: bool | None = None) -> list[dict[str, Any]]:
    # Count keeper photos only — matches thumbs/cards gallery (non-keepers hidden).
    # Unlabeled clusters need a significant face; named people always show.
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.name, p.face_count, p.cover_face_id,
                   COALESCE(pc.photo_count, 0) AS photo_count
            FROM people p
            LEFT JOIN (
              SELECT f.person_id, COUNT(DISTINCT f.content_id) AS photo_count
              FROM faces f
              JOIN photos ph ON ph.content_id = f.content_id
                AND ph.culled = 0 AND ph.missing = 0 AND ph.is_keeper = 1
              JOIN sources s ON s.id = ph.source_id
              GROUP BY f.person_id
            ) pc ON pc.person_id = p.id
            WHERE COALESCE(pc.photo_count, 0) > 0
              AND (
                (p.name IS NOT NULL AND trim(p.name) != '')
                OR EXISTS (
                  SELECT 1
                  FROM faces f2
                  JOIN photo_contents c ON c.id = f2.content_id
                  JOIN photos ph2 ON ph2.content_id = f2.content_id
                    AND ph2.culled = 0 AND ph2.missing = 0 AND ph2.is_keeper = 1
                  WHERE f2.person_id = p.id
                    AND MIN(f2.bbox_w * c.width, f2.bbox_h * c.height) >= ?
                )
              )
            ORDER BY
              CASE WHEN p.name IS NULL OR trim(p.name) = '' THEN 1 ELSE 0 END,
              lower(trim(COALESCE(p.name, ''))),
              p.id
            """,
            (TREE_FACE_SIDE_PX,),
        ).fetchall()
    people = [_person_dict(r) for r in rows]
    if labeled is None:
        return people
    return [p for p in people if bool(p["labeled"]) is labeled]


def merge_people(
    keep_id: int, absorb_id: int, name: str | None = None
) -> dict[str, Any]:
    """Move all faces from absorb_id onto keep_id, then drop the empty person."""
    if keep_id == absorb_id:
        raise ValueError("Cannot merge a person into themselves")
    label = None if name is None else ((name or "").strip() or None)
    with db() as conn:
        keep = conn.execute(
            f"{_PERSON_SELECT} WHERE p.id = ?", (keep_id,)
        ).fetchone()
        absorb = conn.execute(
            f"{_PERSON_SELECT} WHERE p.id = ?", (absorb_id,)
        ).fetchone()
        if not keep:
            raise ValueError(f"Person not found: {keep_id}")
        if not absorb:
            raise ValueError(f"Person not found: {absorb_id}")
        conn.execute(
            "UPDATE faces SET person_id = ? WHERE person_id = ?",
            (keep_id, absorb_id),
        )
        if name is not None:
            conn.execute(
                "UPDATE people SET name = ? WHERE id = ?", (label, keep_id)
            )
        _recalc_people(conn, [keep_id, absorb_id])
        row = conn.execute(
            f"{_PERSON_SELECT} WHERE p.id = ?", (keep_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"Person not found after merge: {keep_id}")
    return _person_dict(row)


def set_person_name(
    person_id: int, name: str | None, *, merge: bool = False
) -> dict[str, Any]:
    label = (name or "").strip() or None
    absorb_into: int | None = None
    with db() as conn:
        row = conn.execute(
            f"{_PERSON_SELECT} WHERE p.id = ?", (person_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Person not found: {person_id}")

        if label is not None:
            existing = conn.execute(
                f"""
                {_PERSON_SELECT}
                WHERE p.id != ?
                  AND p.name IS NOT NULL AND trim(p.name) != ''
                  AND lower(trim(p.name)) = lower(?)
                  AND p.face_count > 0
                ORDER BY photo_count DESC, p.id
                LIMIT 1
                """,
                (person_id, label),
            ).fetchone()
            if existing is not None:
                if not merge:
                    raise NameConflict(_person_dict(existing))
                absorb_into = int(existing["id"])

        if absorb_into is None:
            try:
                cur = conn.execute(
                    "UPDATE people SET name = ? WHERE id = ?", (label, person_id)
                )
            except sqlite3.IntegrityError as e:
                # Unique index race: another person claimed this label.
                if label is None:
                    raise
                existing = conn.execute(
                    f"""
                    {_PERSON_SELECT}
                    WHERE p.id != ?
                      AND p.name IS NOT NULL AND trim(p.name) != ''
                      AND lower(trim(p.name)) = lower(?)
                    ORDER BY photo_count DESC, p.id
                    LIMIT 1
                    """,
                    (person_id, label),
                ).fetchone()
                if existing is not None:
                    raise NameConflict(_person_dict(existing)) from e
                raise
            if cur.rowcount == 0:
                raise ValueError(f"Person not found: {person_id}")
            row = conn.execute(
                f"{_PERSON_SELECT} WHERE p.id = ?", (person_id,)
            ).fetchone()
            return _person_dict(row)

    # Absorb this person into the existing named one (new connection).
    return merge_people(absorb_into, person_id, name=label)


def _find_named_person(
    conn, label: str, *, exclude_id: int | None = None
) -> Any | None:
    sql = f"""
        {_PERSON_SELECT}
        WHERE p.name IS NOT NULL AND trim(p.name) != ''
          AND lower(trim(p.name)) = lower(?)
          AND p.face_count > 0
    """
    args: list[Any] = [label]
    if exclude_id is not None:
        sql += " AND p.id != ?"
        args.append(exclude_id)
    sql += " ORDER BY photo_count DESC, p.id LIMIT 1"
    return conn.execute(sql, args).fetchone()


def next_unknown_label(conn: Any | None = None) -> str:
    """Return the next unused ``Unknown N`` label (N = max existing + 1)."""

    def _scan(c: Any) -> str:
        rows = c.execute(
            """
            SELECT name FROM people
            WHERE name IS NOT NULL AND trim(name) != ''
            """
        ).fetchall()
        max_n = 0
        for r in rows:
            m = _UNKNOWN_LABEL_RE.match((r["name"] or "").strip())
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f"Unknown {max_n + 1}"

    if conn is not None:
        return _scan(conn)
    with db() as c:
        return _scan(c)


def assign_face_unknown(face_id: int) -> dict[str, Any]:
    """Label one face with the next ``Unknown N`` (instance only)."""
    for _ in range(8):
        label = next_unknown_label()
        try:
            return set_face_name(face_id, label, merge=False)
        except NameConflict:
            continue
    raise ValueError("Could not allocate Unknown label")


def set_face_name(
    face_id: int, name: str | None, *, merge: bool = False
) -> dict[str, Any]:
    """Label or correct one face instance only (never the whole group).

    Returns person dict plus face_id and scope ("face").
    """
    label = (name or "").strip() or None
    with db() as conn:
        face = conn.execute(
            """
            SELECT f.id, f.person_id, p.id AS person_row_id,
                   p.name AS person_name, p.face_count
            FROM faces f
            LEFT JOIN people p ON p.id = f.person_id
            WHERE f.id = ?
            """,
            (face_id,),
        ).fetchone()
        if not face:
            raise ValueError(f"Face not found: {face_id}")
        # Orphaned face (missing/deleted person): create a shell person.
        if face["person_row_id"] is None:
            cur = conn.execute(
                "INSERT INTO people (name, face_count) VALUES (NULL, 1)"
            )
            person_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE faces SET person_id = ? WHERE id = ?",
                (person_id, face_id),
            )
            face_count = 1
            current = None
        else:
            person_id = int(face["person_id"])
            face_count = int(face["face_count"] or 0)
            current = (face["person_name"] or "").strip() or None
        if label == current:
            row = conn.execute(
                f"{_PERSON_SELECT} WHERE p.id = ?", (person_id,)
            ).fetchone()
            return {**_person_dict(row), "face_id": face_id, "scope": "face"}

        if label is not None:
            existing = _find_named_person(conn, label, exclude_id=person_id)
            if existing is not None:
                if not merge:
                    raise NameConflict(_person_dict(existing))
                keep_id = int(existing["id"])
                conn.execute(
                    "UPDATE faces SET person_id = ? WHERE id = ?",
                    (keep_id, face_id),
                )
                _recalc_people(conn, [keep_id, person_id])
                row = conn.execute(
                    f"{_PERSON_SELECT} WHERE p.id = ?", (keep_id,)
                ).fetchone()
                return {**_person_dict(row), "face_id": face_id, "scope": "face"}

        # Sole face: rename/clear in place.
        if face_count <= 1:
            try:
                conn.execute(
                    "UPDATE people SET name = ? WHERE id = ?", (label, person_id)
                )
            except sqlite3.IntegrityError as e:
                if label is None:
                    raise
                existing = _find_named_person(conn, label, exclude_id=person_id)
                if existing is not None:
                    raise NameConflict(_person_dict(existing)) from e
                raise
            row = conn.execute(
                f"{_PERSON_SELECT} WHERE p.id = ?", (person_id,)
            ).fetchone()
            return {**_person_dict(row), "face_id": face_id, "scope": "face"}

        # Split face onto a new person (named or unlabeled).
        try:
            cur = conn.execute(
                "INSERT INTO people (name, face_count) VALUES (?, 1)", (label,)
            )
        except sqlite3.IntegrityError as e:
            if label is None:
                raise
            existing = _find_named_person(conn, label, exclude_id=person_id)
            if existing is not None:
                raise NameConflict(_person_dict(existing)) from e
            raise
        new_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE faces SET person_id = ? WHERE id = ?", (new_id, face_id)
        )
        _recalc_people(conn, [person_id, new_id])
        row = conn.execute(
            f"{_PERSON_SELECT} WHERE p.id = ?", (new_id,)
        ).fetchone()
        return {**_person_dict(row), "face_id": face_id, "scope": "face"}


def assign_face_to_person(face_id: int, person_id: int) -> dict[str, Any]:
    """Move one face onto an existing person without renaming that person."""
    keep_id = int(person_id)
    with db() as conn:
        face = conn.execute(
            """
            SELECT f.id, f.person_id, p.id AS person_row_id
            FROM faces f
            LEFT JOIN people p ON p.id = f.person_id
            WHERE f.id = ?
            """,
            (face_id,),
        ).fetchone()
        if not face:
            raise ValueError(f"Face not found: {face_id}")
        keep = conn.execute(
            f"{_PERSON_SELECT} WHERE p.id = ?", (keep_id,)
        ).fetchone()
        if not keep:
            raise ValueError(f"Person not found: {keep_id}")
        old_pid = int(face["person_id"]) if face["person_id"] is not None else None
        if face["person_row_id"] is not None and old_pid == keep_id:
            return {**_person_dict(keep), "face_id": face_id, "scope": "face"}
        conn.execute(
            "UPDATE faces SET person_id = ? WHERE id = ?",
            (keep_id, face_id),
        )
        touched = [keep_id]
        if old_pid and old_pid != keep_id:
            touched.append(old_pid)
        _recalc_people(conn, touched)
        row = conn.execute(
            f"{_PERSON_SELECT} WHERE p.id = ?", (keep_id,)
        ).fetchone()
        return {**_person_dict(row), "face_id": face_id, "scope": "face"}


def recluster_people(
    person_ids: list[int], threshold: float | None = None
) -> dict[str, Any]:
    """Recluster faces belonging to the given person ids only.

    Unlabeled faces whose embedding is directly similar to a named person's
    centroid join that person. Remaining unlabeled faces are regrouped among
    themselves (no transitive absorb of the whole old blob). Named faces
    never move. Does not touch faces outside this id set.
    """
    ids = sorted({int(p) for p in person_ids if p is not None})
    if not ids:
        return {"faces": 0, "moved": 0, "person_ids": []}

    settings = load_settings()
    thr = (
        float(threshold)
        if threshold is not None
        else float(settings.get("face_match_threshold", FACE_MATCH_THRESHOLD))
    )

    with db() as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT f.id, f.person_id, f.embedding, p.name AS person_name
            FROM faces f
            JOIN people p ON p.id = f.person_id
            WHERE f.person_id IN ({placeholders})
              AND f.embedding IS NOT NULL
              AND {_LIVE_FACE_SQL}
            ORDER BY f.id
            """,
            ids,
        ).fetchall()

        named_rows = [r for r in rows if (r["person_name"] or "").strip()]
        unlabeled = [r for r in rows if not (r["person_name"] or "").strip()]
        touched: set[int] = set(ids)

        # Centroids for named people in the pool.
        named_vecs: dict[int, list[np.ndarray]] = {}
        for r in named_rows:
            pid = int(r["person_id"])
            vec = np.frombuffer(r["embedding"], dtype=np.float32)
            if vec.size:
                named_vecs.setdefault(pid, []).append(vec)
        named_cent: dict[int, np.ndarray] = {}
        for pid, vecs in named_vecs.items():
            mean = np.mean(np.stack(vecs), axis=0).astype(np.float32)
            faiss.normalize_L2(mean.reshape(1, -1))
            named_cent[pid] = mean

        moved = 0
        still_unlabeled: list[Any] = []

        # Direct match only: join nearest named centroid if score >= thr.
        named_pids = list(named_cent.keys())
        for r in unlabeled:
            fid = int(r["id"])
            old_pid = int(r["person_id"])
            vec = np.frombuffer(r["embedding"], dtype=np.float32).astype(np.float32)
            if not named_pids or not vec.size:
                still_unlabeled.append(r)
                continue
            faiss.normalize_L2(vec.reshape(1, -1))
            best_pid = None
            best = -1.0
            for pid in named_pids:
                score = float(np.dot(vec, named_cent[pid]))
                if score > best:
                    best = score
                    best_pid = pid
            if best_pid is not None and best >= thr and best_pid != old_pid:
                conn.execute(
                    "UPDATE faces SET person_id = ? WHERE id = ?",
                    (best_pid, fid),
                )
                touched.add(best_pid)
                touched.add(old_pid)
                moved += 1
            elif best_pid is not None and best >= thr:
                # Already on that person.
                pass
            else:
                still_unlabeled.append(r)

        # Regroup leftovers that did not join any named person.
        if len(still_unlabeled) >= 2:
            face_ids = [int(r["id"]) for r in still_unlabeled]
            old_pids = [int(r["person_id"]) for r in still_unlabeled]
            old_by_face = {fid: old_pids[i] for i, fid in enumerate(face_ids)}
            emb = np.stack(
                [
                    np.frombuffer(r["embedding"], dtype=np.float32)
                    for r in still_unlabeled
                ]
            ).astype(np.float32)
            labels = _cluster(emb, thr)
            by_cluster: dict[int, list[int]] = {}
            for i, lab in enumerate(labels):
                by_cluster.setdefault(int(lab), []).append(i)

            claimed_pids = set(named_cent.keys())
            for idxs in by_cluster.values():
                candidates = sorted({old_pids[i] for i in idxs})
                target = next((p for p in candidates if p not in claimed_pids), None)
                if target is None:
                    cur = conn.execute(
                        "INSERT INTO people (name, face_count) VALUES (NULL, 0)"
                    )
                    target = int(cur.lastrowid)
                claimed_pids.add(target)
                touched.add(target)
                for i in idxs:
                    fid = face_ids[i]
                    if old_by_face[fid] == target:
                        continue
                    conn.execute(
                        "UPDATE faces SET person_id = ? WHERE id = ?",
                        (target, fid),
                    )
                    touched.add(old_by_face[fid])
                    moved += 1
        elif len(still_unlabeled) == 1:
            # Sole leftover: ensure not stuck on a now-named-only person id.
            pass

        _recalc_people(conn, list(touched))

    return {"faces": len(rows), "moved": moved, "person_ids": ids}


def photos_for_person(
    person_id: int, limit: int = 200, include_culled: bool = False
) -> list[dict[str, Any]]:
    # Aggregate first (one face + one photo per content), then join — avoids
    # correlated MIN() subqueries that scan the whole photos table per row.
    culled_filter = "p.missing = 0" if include_culled else "p.culled = 0 AND p.missing = 0"
    with db() as conn:
        rows = conn.execute(
            f"""
            WITH person_faces AS (
              SELECT content_id, MIN(id) AS face_id
              FROM faces
              WHERE person_id = ?
              GROUP BY content_id
            ),
            keepers AS (
              SELECT p.content_id, MIN(p.id) AS photo_id
              FROM photos p
              JOIN person_faces pf ON pf.content_id = p.content_id
              JOIN sources s ON s.id = p.source_id
              WHERE {culled_filter}
              GROUP BY p.content_id
            )
            SELECT
              p.id, p.source_id, p.path, p.rel_path, p.mtime, p.album,
              p.culled, p.group_id, p.is_keeper, p.content_id,
              s.label AS source_label,
              c.file_hash, c.size, c.width, c.height, c.taken_at, c.taken_at_source, c.camera,
              c.lat, c.lon, c.gps, c.coord_source, c.venue, c.city, c.region, c.country, c.place_label,
              c.orientation, c.mirrored, c.embedding_ready, c.faces_ready, c.indexed_at,
              f.id AS face_id, f.person_id, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,
              pe.name AS person_name
            FROM keepers k
            JOIN photos p ON p.id = k.photo_id
            JOIN photo_contents c ON c.id = p.content_id
            JOIN sources s ON s.id = p.source_id
            JOIN person_faces pf ON pf.content_id = k.content_id
            JOIN faces f ON f.id = pf.face_id
            LEFT JOIN people pe ON pe.id = f.person_id
            ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
            LIMIT ?
            """,
            (person_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        name = (d.get("person_name") or "").strip() or None
        d["labeled"] = is_person_labeled(name)
        d["display_name"] = name or (
            f"Person {d['person_id']}" if d.get("person_id") else "Unassigned"
        )
        out.append(d)
    return out


def photos_with_faces(
    limit: int = 200,
    include_culled: bool = False,
    *,
    labeled: bool | None = None,
) -> list[dict[str, Any]]:
    """All photos that have at least one face (primary People branch).

    When ``labeled`` is True/False, only photos with a face belonging to a
    labeled person (or unlabeled/Unknown) are included.

    Each photo includes a ``faces`` list with every face bbox on that content.
    """
    culled_filter = "p.missing = 0" if include_culled else "p.culled = 0 AND p.missing = 0"
    person_filter = ""
    params: list[Any] = []
    if labeled is not None:
        ids = [int(p["id"]) for p in people_list(labeled=labeled)]
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        person_filter = f"AND f.person_id IN ({placeholders})"
        params.extend(ids)
    params.append(limit)
    with db() as conn:
        rows = conn.execute(
            f"""
            WITH faced AS (
              SELECT DISTINCT f.content_id
              FROM faces f
              WHERE 1=1 {person_filter}
            ),
            keepers AS (
              SELECT p.content_id, MIN(p.id) AS photo_id
              FROM photos p
              JOIN faced fd ON fd.content_id = p.content_id
              WHERE {culled_filter}
              GROUP BY p.content_id
            )
            SELECT
              p.id, p.source_id, p.path, p.rel_path, p.mtime, p.album,
              p.culled, p.group_id, p.is_keeper, p.content_id,
              s.label AS source_label,
              c.file_hash, c.size, c.width, c.height, c.taken_at, c.taken_at_source, c.camera,
              c.lat, c.lon, c.gps, c.coord_source, c.venue, c.city, c.region, c.country, c.place_label,
              c.orientation, c.mirrored, c.embedding_ready, c.faces_ready, c.indexed_at
            FROM keepers k
            JOIN photos p ON p.id = k.photo_id
            JOIN photo_contents c ON c.id = p.content_id
            JOIN sources s ON s.id = p.source_id
            ORDER BY COALESCE(c.taken_at, '') DESC, p.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        photos = [dict(r) for r in rows]
        if not photos:
            return []
        content_ids = [int(p["content_id"]) for p in photos if p.get("content_id") is not None]
        faces_by_content: dict[int, list[dict[str, Any]]] = {cid: [] for cid in content_ids}
        if content_ids:
            ph = ",".join("?" * len(content_ids))
            for fr in conn.execute(
                f"""
                SELECT f.id, f.content_id, f.person_id,
                       f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,
                       p.name AS person_name
                FROM faces f
                LEFT JOIN people p ON p.id = f.person_id
                WHERE f.content_id IN ({ph})
                ORDER BY f.id
                """,
                content_ids,
            ).fetchall():
                cid = int(fr["content_id"])
                if cid not in faces_by_content:
                    continue
                name = (fr["person_name"] or "").strip() or None
                # People-all gallery: only box named people (skip unlabeled / Unknown N).
                if not is_person_labeled(name):
                    continue
                faces_by_content[cid].append(
                    {
                        "id": fr["id"],
                        "person_id": fr["person_id"],
                        "bbox_x": fr["bbox_x"],
                        "bbox_y": fr["bbox_y"],
                        "bbox_w": fr["bbox_w"],
                        "bbox_h": fr["bbox_h"],
                        "person_name": name,
                        "display_name": name,
                        "labeled": True,
                    }
                )
        for p in photos:
            cid = p.get("content_id")
            p["faces"] = faces_by_content.get(int(cid), []) if cid is not None else []
        return photos


def faces_for_photo(photo_id: int) -> list[dict[str, Any]]:
    """All faces on the content behind a photo location, with person labels."""
    with db() as conn:
        row = conn.execute(
            "SELECT content_id FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            """
            SELECT f.id, f.content_id, f.person_id,
                   f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,
                   p.name AS person_name
            FROM faces f
            LEFT JOIN people p ON p.id = f.person_id
            WHERE f.content_id = ?
            ORDER BY f.id
            """,
            (int(row["content_id"]),),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        name = (d.get("person_name") or "").strip() or None
        d["labeled"] = is_person_labeled(name)
        if name:
            d["display_name"] = name
        elif d.get("person_id"):
            d["display_name"] = f"Person {d['person_id']}"
        else:
            d["display_name"] = "Unassigned"
        out.append(d)
    return out


def delete_face(face_id: int) -> dict[str, Any]:
    """Remove a false-positive detection (dog, statue, cartoon, etc.)."""
    fid = int(face_id)
    with db() as conn:
        row = conn.execute(
            "SELECT id, person_id, content_id FROM faces WHERE id = ?", (fid,)
        ).fetchone()
        if not row:
            raise ValueError(f"Face not found: {fid}")
        person_id = row["person_id"]
        conn.execute("DELETE FROM faces WHERE id = ?", (fid,))
        if person_id is not None:
            _recalc_people(conn, [int(person_id)])
    try:
        face_thumb_path(fid).unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "ok": True,
        "id": fid,
        "person_id": int(person_id) if person_id is not None else None,
    }


def face_thumb_path(face_id: int) -> Path:
    return THUMBS_DIR / "faces" / f"{int(face_id)}.jpg"


def face_thumb_file(face_id: int, size: int = 256) -> Path:
    """Crop a padded face region from the content thumb (or original) and cache it."""
    from PIL import Image

    dest = face_thumb_path(face_id)
    if dest.exists():
        return dest

    with db() as conn:
        row = conn.execute(
            """
            SELECT f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, f.content_id,
                   (SELECT MIN(p.path) FROM photos p WHERE p.content_id = f.content_id) AS path
            FROM faces f WHERE f.id = ?
            """,
            (face_id,),
        ).fetchone()
    if not row:
        raise FileNotFoundError(f"Face not found: {face_id}")

    src = content_thumb_path(int(row["content_id"]))
    if not src.exists():
        src = Path(row["path"] or "")
    if not src.exists():
        raise FileNotFoundError(f"No image for face {face_id}")

    with Image.open(src) as im:
        im = im.convert("RGB")
        w, h = im.size
        # Slight padding around the detected box so crops look natural.
        pad = 0.35
        bx = float(row["bbox_x"])
        by = float(row["bbox_y"])
        bw = float(row["bbox_w"])
        bh = float(row["bbox_h"])
        cx = bx + bw / 2
        cy = by + bh / 2
        side = max(bw, bh) * (1 + pad)
        x0 = max(0, int((cx - side / 2) * w))
        y0 = max(0, int((cy - side / 2) * h))
        x1 = min(w, int((cx + side / 2) * w))
        y1 = min(h, int((cy + side / 2) * h))
        if x1 <= x0 or y1 <= y0:
            crop = im
        else:
            crop = im.crop((x0, y0, x1, y1))
        crop.thumbnail((size, size))
        dest.parent.mkdir(parents=True, exist_ok=True)
        crop.save(dest, format="JPEG", quality=85, optimize=True)
    return dest
