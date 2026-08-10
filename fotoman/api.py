from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import StreamingResponse
from pydantic import BaseModel, Field

from fotoman.config import THUMBS_DIR, ensure_dirs, load_settings, public_settings, update_settings
from fotoman.db import content_thumb_path, db, init_db
from fotoman import browse, duplicates, events, faces, indexer, properties, search, vfolders, vqa

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Fotoman", version="0.1.0")


@app.middleware("http")
async def no_cache_ui_assets(request: Request, call_next):
    """Force revalidation of UI assets so HTML/JS cannot drift in the browser cache."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/")
def index_page():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    v = int((STATIC_DIR / "app.js").stat().st_mtime)
    html = html.replace('src="/app.js"', f'src="/app.js?v={v}"', 1)
    html = html.replace('href="/styles.css"', f'href="/styles.css?v={v}"', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/events")
async def api_events():
    q = events.subscribe()

    async def stream():
        try:
            async for chunk in events.iter_events(q):
                yield chunk
        finally:
            events.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


class SourceIn(BaseModel):
    path: str
    label: str | None = None


class SourceLabelIn(BaseModel):
    label: str


class CullIn(BaseModel):
    group_ids: list[int] | None = None
    delete_files: bool = False


class KeeperIn(BaseModel):
    photo_id: int


class PhotoIdsIn(BaseModel):
    photo_ids: list[int]
    delete_files: bool = False


class VirtualFolderIn(BaseModel):
    name: str


class VirtualFolderNameIn(BaseModel):
    name: str


class PhotoPropertiesPatch(BaseModel):
    """Known editable fields. Add new optional fields here as the editor grows."""

    taken_at: str | None = None
    place_label: str | None = None
    orientation: int | None = None
    mirrored: bool | None = None


class PhotoPropertiesIn(BaseModel):
    photo_ids: list[int]
    properties: PhotoPropertiesPatch


class RebuildDupesIn(BaseModel):
    threshold: float | None = Field(default=None, ge=0.8, le=0.999)
    exact: bool = True
    formatted: bool = False
    near: bool = True
    photo_ids: list[int] | None = None
    include_culled: bool = False


class PersonNameIn(BaseModel):
    name: str | None = None
    merge: bool = False


class FaceNameIn(BaseModel):
    name: str | None = None
    merge: bool = False


class ReclusterPeopleIn(BaseModel):
    person_ids: list[int]
    threshold: float | None = Field(default=None, ge=0.2, le=0.9)


class FaceThresholdIn(BaseModel):
    threshold: float | None = Field(default=None, ge=0.2, le=0.9)
    source_id: int | None = None


class SettingsIn(BaseModel):
    model_id: str | None = None
    near_dup_threshold: float | None = Field(default=None, ge=0.8, le=0.999)
    face_match_threshold: float | None = Field(default=None, ge=0.2, le=0.9)
    date_tree_order: str | None = Field(default=None, pattern="^(newest|oldest)$")
    place_edit_nav: str | None = Field(default=None, pattern="^(stay|follow)$")
    vqa_provider: str | None = None
    vqa_api_key: str | None = None
    vqa_base_url: str | None = None
    vqa_model: str | None = None


class VqaIn(BaseModel):
    photo_id: int
    prompt: str | None = Field(default=None, max_length=2000)
    task: str | None = None


def pick_directory(title: str = "Select photo archive") -> str | None:
    """Open a native OS folder dialog; return the path or None if cancelled."""
    if shutil.which("kdialog"):
        proc = subprocess.run(
            ["kdialog", "--getexistingdirectory", str(Path.home()), title],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        return None
    if shutil.which("zenity"):
        proc = subprocess.run(
            ["zenity", "--file-selection", "--directory", f"--title={title}"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        return None
    # tkinter in a subprocess avoids main-thread / server-thread issues
    script = (
        "import tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
        f"path = filedialog.askdirectory(title={title!r}) or ''\n"
        "print(path); root.destroy()\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    path = (proc.stdout or "").strip()
    return path or None


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    init_db()
    load_settings()
    faces.repair_orphaned_ready_flags()
    faces.repair_orphan_person_links()
    faces.purge_tiny_faces()
    faces.refresh_person_covers()
    indexer.resume_if_interrupted()
    from fotoman.geocode import start_geo_worker

    start_geo_worker()
    # Incremental reindex (list + hash/EXIF deltas) before geocode/embed/faces.
    indexer.start_scan_all_enabled(high_priority=True)
    # Auto-queue optional work for archives with face/embed enabled.
    with db() as conn:
        sources = conn.execute(
            "SELECT id, face_enabled, embed_enabled FROM sources WHERE enabled = 1"
        ).fetchall()
    for s in sources:
        if s["embed_enabled"]:
            indexer.start_index_background(source_id=int(s["id"]), mode="embed")
        if s["face_enabled"]:
            faces.start_rebuild_background(source_id=int(s["id"]))


@app.get("/api/stats")
def api_stats():
    return search.stats()


@app.get("/api/sources")
def api_sources():
    with db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT s.*,
                  (SELECT COUNT(*) FROM photos WHERE source_id = s.id) AS photo_count,
                  (SELECT COUNT(*) FROM photos p
                   JOIN photo_contents c ON c.id = p.content_id
                   WHERE p.source_id = s.id AND c.embedding_ready = 1) AS embedded_count,
                  (SELECT COUNT(DISTINCT c.id) FROM photos p
                   JOIN photo_contents c ON c.id = p.content_id
                   WHERE p.source_id = s.id AND c.embedding_ready = 0) AS pending_embed_count,
                  (SELECT COUNT(DISTINCT c.id) FROM photos p
                   JOIN photo_contents c ON c.id = p.content_id
                   WHERE p.source_id = s.id AND c.faces_ready = 1) AS faces_ready_count,
                  (SELECT COUNT(DISTINCT c.id) FROM photos p
                   JOIN photo_contents c ON c.id = p.content_id
                   WHERE p.source_id = s.id AND p.culled = 0 AND p.missing = 0 AND c.faces_ready = 0
                  ) AS pending_faces_count
                FROM sources s
                ORDER BY s.id
                """
            ).fetchall()
        ]
    for r in rows:
        r["exists"] = Path(r["path"]).exists()
    return rows


@app.post("/api/pick-directory")
def api_pick_directory():
    path = pick_directory()
    if not path:
        return {"path": None, "cancelled": True}
    return {"path": path, "cancelled": False}


@app.post("/api/sources")
def api_add_source(body: SourceIn):
    try:
        src = indexer.add_source(body.path, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # List then index deltas (everything is new) ASAP.
    indexer.start_index_background(source_id=int(src["id"]), mode="scan", high_priority=True)
    return {**src, "indexing": True, "status": indexer.get_index_status()}


@app.post("/api/sources/{source_id}/enable")
def api_enable_source(source_id: int, enabled: bool = True):
    indexer.set_source_enabled(source_id, enabled)
    return {"ok": True}


@app.post("/api/sources/{source_id}/face_enabled")
def api_source_face_enabled(source_id: int, enabled: bool = True):
    with db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Source not found")
        conn.execute(
            "UPDATE sources SET face_enabled = ? WHERE id = ?",
            (1 if enabled else 0, source_id),
        )
    if enabled:
        return faces.start_rebuild_background(source_id=source_id)
    try:
        return faces.suspend_faces(source_id=source_id)
    except ValueError:
        return faces.get_status()


@app.post("/api/sources/{source_id}/embed_enabled")
def api_source_embed_enabled(source_id: int, enabled: bool = True):
    with db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Source not found")
        conn.execute(
            "UPDATE sources SET embed_enabled = ? WHERE id = ?",
            (1 if enabled else 0, source_id),
        )
    if enabled:
        return indexer.resume_embedding(source_id=source_id)
    return indexer.suspend_embedding(source_id=source_id)


@app.post("/api/sources/{source_id}/reindex")
def api_reindex_source(source_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Source not found")
    indexer.start_index_background(source_id=source_id, mode="scan", high_priority=True)
    if row["embed_enabled"]:
        indexer.start_index_background(source_id=source_id, mode="embed")
    if row["face_enabled"]:
        faces.start_rebuild_background(source_id=source_id)
    return {"ok": True, "status": indexer.get_index_status()}


@app.patch("/api/sources/{source_id}")
def api_update_source(source_id: int, body: SourceLabelIn):
    try:
        return indexer.set_source_label(source_id, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.delete("/api/sources/{source_id}")
def api_delete_source(source_id: int):
    indexer.remove_source(source_id)
    return {"ok": True}


@app.post("/api/sources/{source_id}/index")
def api_index_source(source_id: int, reembed: bool = False):
    """List this archive and index any new/changed files. Embed follows unless suspended."""
    with db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Source not found")
    indexer.start_index_background(reembed=reembed, source_id=source_id, mode="scan")
    return {"ok": True, "status": indexer.get_index_status()}


@app.post("/api/index")
def api_index(reembed: bool = False):
    indexer.start_index_background(reembed=reembed, mode="scan")
    return {"ok": True, "status": indexer.get_index_status()}


@app.post("/api/index/suspend")
def api_index_suspend(source_id: int | None = None):
    """Pause embedding after the current batch. Listing/indexing can still run."""
    return indexer.suspend_embedding(source_id=source_id)


@app.post("/api/index/resume")
def api_index_resume(source_id: int | None = None):
    """Start embedding photos that are indexed but not yet vectorized (Go)."""
    return indexer.resume_embedding(source_id=source_id)


@app.post("/api/sources/{source_id}/index/suspend")
def api_source_index_suspend(source_id: int):
    with db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Source not found")
    return indexer.suspend_embedding(source_id=source_id)


@app.post("/api/sources/{source_id}/index/resume")
def api_source_index_resume(source_id: int):
    with db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Source not found")
    return indexer.resume_embedding(source_id=source_id)


@app.get("/api/index/status")
def api_index_status():
    indexer.ensure_first_round_scans()
    return indexer.get_index_status()


@app.get("/api/places/suggest")
def api_place_suggest(q: str = Query("", min_length=0), limit: int = Query(12, ge=1, le=40)):
    return search.suggest_places(q, limit=limit)


@app.get("/api/map/photos")
def api_map_photos(include_culled: bool = False):
    return search.map_photos(include_culled=include_culled)


@app.get("/api/search")
def api_search(
    q: str = Query("", min_length=0),
    mode: str = Query("visual", pattern="^(visual|meta|hybrid)$"),
    top_k: int = Query(48, ge=1, le=200),
    place: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_km: float | None = None,
    taken_from: str | None = None,
    taken_to: str | None = None,
    album: str | None = None,
    camera: str | None = None,
    source_id: int | None = None,
    has_gps: bool | None = None,
    keepers_only: bool = False,
    include_culled: bool = False,
    content_ids: str | None = None,
):
    allow: set[int] | None = None
    if content_ids and content_ids.strip():
        allow = {int(x) for x in content_ids.split(",") if x.strip().isdigit()}
    if mode == "meta":
        return search.search_metadata(
            text=q or None,
            taken_from=taken_from,
            taken_to=taken_to,
            camera=camera,
            album=album,
            place=place,
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            has_gps=has_gps,
            source_id=source_id,
            keepers_only=keepers_only,
            include_culled=include_culled,
            content_ids=allow,
            limit=top_k,
        )
    if not q.strip():
        raise HTTPException(400, "Query text required for visual/hybrid search")
    if mode == "hybrid":
        return search.hybrid_search(
            q,
            top_k=top_k,
            place=place,
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            taken_from=taken_from,
            taken_to=taken_to,
            album=album,
            camera=camera,
            source_id=source_id,
            include_culled=include_culled,
            content_ids=allow,
        )
    return search.search_visual(
        q, top_k=top_k, include_culled=include_culled, content_ids=allow
    )


@app.get("/api/search/similar")
def api_search_similar(
    content_id: int | None = None,
    photo_id: int | None = None,
    top_k: int = Query(48, ge=1, le=200),
    include_culled: bool = False,
    content_ids: str | None = None,
):
    """Find photos visually similar to an embedded image (SigLIP / FAISS)."""
    cid = content_id
    if cid is None and photo_id is not None:
        with db() as conn:
            row = conn.execute(
                "SELECT content_id FROM photos WHERE id = ?", (photo_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "Photo not found")
        cid = int(row["content_id"])
    if cid is None:
        raise HTTPException(400, "content_id or photo_id required")
    allow: set[int] | None = None
    if content_ids and content_ids.strip():
        allow = {int(x) for x in content_ids.split(",") if x.strip().isdigit()}
    hits = search.search_similar(
        cid, top_k=top_k, include_culled=include_culled, content_ids=allow
    )
    if hits is None:
        raise HTTPException(404, "No embedding for this photo yet")
    return hits


@app.get("/api/browse/tree")
def api_browse_tree(
    source_id: int | None = None,
    include_files: bool = False,
    include_culled: bool = False,
):
    return browse.folder_tree(
        source_id, include_files=include_files, include_culled=include_culled
    )


@app.get("/api/browse/date-tree")
def api_browse_date_tree(include_culled: bool = False):
    order = str(load_settings().get("date_tree_order") or "newest").lower()
    return browse.date_tree(
        include_culled=include_culled,
        newest_first=order != "oldest",
    )


@app.get("/api/browse/location-tree")
def api_browse_location_tree(include_culled: bool = False):
    return browse.location_tree(include_culled=include_culled)


@app.get("/api/browse/groups")
def api_browse_groups(
    group_by: str = Query("camera", pattern="^(camera|date|location|album)$"),
    include_culled: bool = False,
):
    return browse.group_list(group_by, include_culled=include_culled)


@app.get("/api/browse/photos")
def api_browse_photos(
    group_by: str = Query(
        "folder",
        pattern="^(folder|camera|date|location|album|cluster|person|remove|vfolder)$",
    ),
    key: str = Query(""),
    limit: int = Query(200, ge=1, le=10000),
    include_culled: bool = False,
    include_subfolders: bool = True,
    labeled: bool | None = None,
):
    return browse.photos_for_group(
        group_by,
        key,
        limit,
        include_culled=include_culled,
        include_subfolders=include_subfolders,
        labeled=labeled,
    )


@app.get("/api/virtual-folders")
def api_list_virtual_folders(include_culled: bool = False):
    return vfolders.list_folders(include_culled=include_culled)


@app.post("/api/virtual-folders")
def api_create_virtual_folder(body: VirtualFolderIn):
    try:
        return vfolders.create_folder(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except vfolders.NameConflict as e:
        raise HTTPException(
            409,
            detail={"code": "name_conflict", "existing": e.existing},
        ) from e


@app.patch("/api/virtual-folders/{folder_id}")
def api_rename_virtual_folder(folder_id: int, body: VirtualFolderNameIn):
    try:
        return vfolders.rename_folder(folder_id, body.name)
    except KeyError as e:
        raise HTTPException(404, "Not found") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except vfolders.NameConflict as e:
        raise HTTPException(
            409,
            detail={"code": "name_conflict", "existing": e.existing},
        ) from e


@app.delete("/api/virtual-folders/{folder_id}")
def api_delete_virtual_folder(folder_id: int):
    try:
        return vfolders.delete_folder(folder_id)
    except KeyError as e:
        raise HTTPException(404, "Not found") from e


@app.post("/api/virtual-folders/{folder_id}/members")
def api_add_virtual_folder_members(folder_id: int, body: PhotoIdsIn):
    try:
        return vfolders.add_members(folder_id, body.photo_ids)
    except KeyError as e:
        raise HTTPException(404, "Not found") from e


@app.delete("/api/virtual-folders/{folder_id}/members")
def api_remove_virtual_folder_members(folder_id: int, body: PhotoIdsIn):
    try:
        return vfolders.remove_members(folder_id, body.photo_ids)
    except KeyError as e:
        raise HTTPException(404, "Not found") from e


@app.get("/api/browse/clusters")
def api_browse_clusters(
    threshold: float = Query(0.92, ge=0.8, le=0.999),
    limit: int = Query(50, ge=1, le=200),
    include_culled: bool = False,
):
    return browse.cluster_list(threshold, limit, include_culled=include_culled)


@app.get("/api/browse/people")
def api_browse_people(labeled: bool | None = None):
    return faces.people_list(labeled=labeled)


@app.patch("/api/people/{person_id}")
def api_update_person(person_id: int, body: PersonNameIn):
    try:
        return faces.set_person_name(person_id, body.name, merge=body.merge)
    except faces.NameConflict as e:
        raise HTTPException(
            409,
            detail={"code": "name_conflict", "existing": e.existing},
        ) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.patch("/api/faces/{face_id}")
def api_update_face(face_id: int, body: FaceNameIn):
    try:
        return faces.set_face_name(face_id, body.name, merge=body.merge)
    except faces.NameConflict as e:
        raise HTTPException(
            409,
            detail={"code": "name_conflict", "existing": e.existing},
        ) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/faces/{face_id}/unknown")
def api_assign_face_unknown(face_id: int):
    try:
        return faces.assign_face_unknown(face_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.delete("/api/faces/{face_id}")
def api_delete_face(face_id: int):
    """Dismiss a false-positive face detection."""
    try:
        return faces.delete_face(face_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/api/people/recluster")
def api_recluster_people(body: ReclusterPeopleIn):
    return faces.recluster_people(body.person_ids, threshold=body.threshold)


@app.post("/api/faces/rebuild")
def api_rebuild_faces(body: FaceThresholdIn | None = None):
    thr = body.threshold if body else None
    source_id = body.source_id if body else None
    if source_id is not None:
        with db() as conn:
            row = conn.execute(
                "SELECT id FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        if not row:
            raise HTTPException(404, "Source not found")
    return faces.start_rebuild_background(thr, source_id=source_id)


@app.post("/api/sources/{source_id}/faces")
def api_source_faces(source_id: int):
    with db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Source not found")
    return faces.start_rebuild_background(source_id=source_id)


@app.post("/api/faces/suspend")
def api_faces_suspend(source_id: int | None = None):
    try:
        return faces.suspend_faces(source_id=source_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/faces/resume")
def api_faces_resume(source_id: int | None = None):
    try:
        return faces.resume_faces(source_id=source_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/sources/{source_id}/faces/suspend")
def api_source_faces_suspend(source_id: int):
    with db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Source not found")
    try:
        return faces.suspend_faces(source_id=source_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/sources/{source_id}/faces/resume")
def api_source_faces_resume(source_id: int):
    with db() as conn:
        row = conn.execute("SELECT id FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Source not found")
    try:
        return faces.resume_faces(source_id=source_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/faces/status")
def api_faces_status():
    return faces.get_status()


@app.get("/api/browse/thumb")
def api_browse_thumb(source_id: int, rel: str = Query("")):
    try:
        thumb = browse.browse_thumb_file(source_id, rel)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(thumb, media_type="image/jpeg")


@app.get("/api/browse/original")
def api_browse_original(source_id: int, rel: str = Query("")):
    try:
        path = browse.resolve_source_file(source_id, rel)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(path)


@app.post("/api/duplicates/rebuild")
def api_rebuild_dupes(body: RebuildDupesIn | None = None):
    body = body or RebuildDupesIn()
    return duplicates.rebuild_groups(
        body.threshold,
        exact=body.exact,
        formatted=body.formatted,
        near=body.near,
        photo_ids=body.photo_ids,
        include_culled=body.include_culled,
    )


@app.get("/api/duplicates")
def api_list_dupes(kind: str | None = None, include_culled: bool = False):
    return duplicates.list_groups(kind, include_removed=include_culled)


@app.post("/api/duplicates/keeper")
def api_set_keeper(body: KeeperIn):
    try:
        duplicates.set_keeper(body.photo_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.post("/api/photos/properties")
def api_photo_properties(body: PhotoPropertiesIn):
    props = body.properties.model_dump(exclude_unset=True)
    try:
        return properties.apply_properties(body.photo_ids, props)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/duplicates/remove")
def api_remove(body: PhotoIdsIn):
    return duplicates.remove_photos(body.photo_ids, delete_files=body.delete_files)


@app.post("/api/duplicates/restore")
def api_restore(body: PhotoIdsIn):
    return duplicates.restore_photos(body.photo_ids)


@app.post("/api/duplicates/delete")
def api_delete_photos(body: PhotoIdsIn):
    return duplicates.delete_photos(body.photo_ids)


@app.post("/api/duplicates/cull")
def api_cull(body: CullIn):
    return duplicates.cull_replicas(body.group_ids, delete_files=body.delete_files)


@app.get("/api/photos/{photo_id}/faces")
def api_photo_faces(photo_id: int):
    with db() as conn:
        row = conn.execute("SELECT id FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    return faces.faces_for_photo(photo_id)


@app.get("/api/faces/{face_id}/thumb")
def api_face_thumb(face_id: int):
    try:
        thumb = faces.face_thumb_file(face_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(thumb, media_type="image/jpeg")


@app.get("/api/thumb/{photo_id}")
def api_thumb(photo_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT content_id, path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    thumb = content_thumb_path(int(row["content_id"]))
    if thumb.exists():
        return FileResponse(thumb, media_type="image/jpeg")
    legacy = THUMBS_DIR / f"{photo_id}.jpg"
    if legacy.exists():
        return FileResponse(legacy, media_type="image/jpeg")
    if not Path(row["path"]).exists():
        raise HTTPException(404, "Not found")
    return FileResponse(row["path"])


@app.get("/api/original/{photo_id}")
def api_original(photo_id: int):
    with db() as conn:
        row = conn.execute("SELECT path FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not row or not Path(row["path"]).exists():
        raise HTTPException(404, "Not found")
    return FileResponse(row["path"])


@app.get("/api/settings")
def api_get_settings():
    return public_settings()


@app.post("/api/settings")
def api_post_settings(body: SettingsIn):
    patch = body.model_dump(exclude_unset=True)
    return update_settings(patch)


@app.post("/api/vqa")
def api_vqa(body: VqaIn):
    try:
        return vqa.ask_photo(body.photo_id, body.prompt or "", body.task)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
