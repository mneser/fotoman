"""Outbound WebSocket provider: Fotoman connects to Fotomob and answers queries."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from fotoman.config import load_settings
from fotoman.db import content_thumb_path, db
from fotoman import collections, device, faces, search

log = logging.getLogger(__name__)

_ws_ref: Any = None
_loop_ref: asyncio.AbstractEventLoop | None = None
_dev_pending: dict[str, asyncio.Future] = {}
_dev_id = 0
_dev_lock = threading.Lock()


def _fotomob_ws_url() -> str | None:
    raw = (os.environ.get("FOTOMOB_URL") or load_settings().get("fotomob_url") or "").strip()
    if not raw:
        return None
    if raw.startswith("https://"):
        raw = "wss://" + raw[len("https://") :]
    elif raw.startswith("http://"):
        raw = "ws://" + raw[len("http://") :]
    elif not raw.startswith(("ws://", "wss://")):
        raw = "ws://" + raw
    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/")
    if path in ("", "/"):
        path = "/provider"
    elif not path.endswith("/provider"):
        path = f"{path}/provider"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _instance_name() -> str:
    raw = (os.environ.get("FOTOMAN_NAME") or load_settings().get("fotoman_name") or "fotoman").strip()
    return raw or "fotoman"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return value


def _slim_collection(n: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": int(n["id"]),
        "name": n["name"],
        "count": int(n.get("count") or 0),
        "linked": bool(n.get("linked")),
        "source_subdir": bool(n.get("source_subdir")),
        "children": [_slim_collection(c) for c in (n.get("children") or [])],
    }
    token = str(n.get("share_code") or "").strip()
    if token:
        out["share_code"] = token
        url = str(n.get("share_url") or "").strip()
        if url:
            out["share_url"] = url
    return out


def _thumb_bytes(photo_id: int) -> tuple[bytes, str] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT content_id, path FROM photos WHERE id = ?", (photo_id,)
        ).fetchone()
    if not row:
        return None
    thumb = content_thumb_path(int(row["content_id"]))
    if thumb.exists():
        return thumb.read_bytes(), "image/jpeg"
    from fotoman.config import THUMBS_DIR

    legacy = THUMBS_DIR / f"{photo_id}.jpg"
    if legacy.exists():
        return legacy.read_bytes(), "image/jpeg"
    src = Path(row["path"])
    if src.exists():
        return src.read_bytes(), "application/octet-stream"
    return None


def _share_tokens(params: dict[str, Any]) -> list[str]:
    raw = params.get("share")
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw).replace(",", " ").split()
    return [p.strip() for p in parts if str(p).strip()]


def _share_scope(params: dict[str, Any]) -> dict[str, Any] | None:
    roots = collections.shared_collection_ids_for_tokens(_share_tokens(params))
    if not roots:
        return None
    return {
        "roots": roots,
        "collection_ids": collections.collection_ids_for_share_roots(roots),
        "content_ids": collections.content_ids_for_share_roots(roots),
    }


def _denied(req_id: Any, status: int = 404, error: str = "Not found") -> dict[str, Any]:
    return {"id": req_id, "op": "res", "ok": False, "status": status, "error": error}


def _photo_is_shared(photo_id: int, content_ids: set[int]) -> bool:
    if not content_ids:
        return False
    with db() as conn:
        row = conn.execute(
            "SELECT content_id FROM photos WHERE id = ?", (int(photo_id),)
        ).fetchone()
    if not row or row["content_id"] is None:
        return False
    return int(row["content_id"]) in content_ids


def _bool_param(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


def _coord(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return n


def _first_coord(*values: Any) -> float | None:
    for value in values:
        n = _coord(value)
        if n is not None:
            return n
    return None


def _pair_coords(p: dict[str, Any]) -> tuple[float | None, float | None]:
    lat = _first_coord(p.get("lat"), p.get("latitude"))
    lon = _first_coord(p.get("lon"), p.get("lng"), p.get("longitude"))
    if lat is None or lon is None:
        return None, None
    if abs(lat) < 1e-5 and abs(lon) < 1e-5:
        return None, None
    return lat, lon


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slim_photo(p: dict[str, Any]) -> dict[str, Any]:
    """Fotomob album/search payload — keep map coords as plain floats."""
    place = p.get("place_label") or p.get("city") or p.get("venue")
    lat, lon = _pair_coords(p)
    return {
        "id": int(p["id"]),
        "content_id": int(p["content_id"]) if p.get("content_id") is not None else None,
        "taken_at": p.get("taken_at"),
        "place_label": place,
        "city": p.get("city"),
        "region": p.get("region"),
        "country": p.get("country"),
        "venue": p.get("venue"),
        "camera": p.get("camera"),
        "width": _int_or_none(p.get("width")),
        "height": _int_or_none(p.get("height")),
        "lat": lat,
        "lon": lon,
        "person_ids": list(p.get("person_ids") or []),
    }


def _slim_photos(photos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_slim_photo(p) for p in _attach_person_ids(photos)]


def _attach_person_ids(photos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cids: list[int] = []
    for p in photos:
        cid = p.get("content_id")
        if cid is not None:
            cids.append(int(cid))
    if not cids:
        for p in photos:
            p.setdefault("person_ids", [])
        return photos
    unique = list(dict.fromkeys(cids))
    by: dict[int, list[int]] = {c: [] for c in unique}
    placeholders = ",".join("?" * len(unique))
    with db() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT content_id, person_id FROM faces "
            f"WHERE person_id IS NOT NULL AND content_id IN ({placeholders})",
            unique,
        )
        for r in rows:
            by.setdefault(int(r["content_id"]), []).append(int(r["person_id"]))
    for p in photos:
        cid = p.get("content_id")
        p["person_ids"] = sorted(set(by.get(int(cid), []))) if cid is not None else []
    return photos


def _cover_faces_in_scope(person_ids: set[int], content_ids: set[int]) -> dict[int, int]:
    """Largest in-scope face per person so Fotomob can show a crop, not initials."""
    best: dict[int, tuple[float, int]] = {}
    if not person_ids or not content_ids:
        return {}
    pids = list(person_ids)
    ids = list(content_ids)
    with db() as conn:
        for i in range(0, len(ids), 800):
            chunk = ids[i : i + 800]
            pph = ",".join("?" * len(pids))
            cph = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""
                SELECT person_id, id, (bbox_w * bbox_h) AS area
                FROM faces
                WHERE person_id IN ({pph}) AND content_id IN ({cph})
                """,
                (*pids, *chunk),
            )
            for r in rows:
                pid = int(r["person_id"])
                area = float(r["area"] or 0)
                cur = best.get(pid)
                if cur is None or area > cur[0]:
                    best[pid] = (area, int(r["id"]))
    return {pid: fid for pid, (_, fid) in best.items()}


def _slim_people(labeled: bool | None, content_ids: set[int]) -> list[dict[str, Any]]:
    visible = content_ids
    if not visible:
        return []
    allowed: set[int] = set()
    ids = list(visible)
    with db() as conn:
        for i in range(0, len(ids), 800):
            chunk = ids[i : i + 800]
            ph = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT DISTINCT person_id FROM faces "
                f"WHERE person_id IS NOT NULL AND content_id IN ({ph})",
                chunk,
            )
            allowed.update(int(r["person_id"]) for r in rows)
    covers = _cover_faces_in_scope(allowed, visible)
    return [
        {
            "id": int(p["id"]),
            "name": p.get("name"),
            "display_name": p["display_name"],
            "photo_count": int(p.get("photo_count") or 0),
            "cover_face_id": covers.get(int(p["id"])),
        }
        for p in faces.people_list(labeled=labeled)
        if int(p["id"]) in allowed
    ]


def _face_is_shared(face_id: int, content_ids: set[int]) -> bool:
    if not content_ids:
        return False
    with db() as conn:
        row = conn.execute(
            "SELECT content_id FROM faces WHERE id = ?", (int(face_id),)
        ).fetchone()
    if not row or row["content_id"] is None:
        return False
    return int(row["content_id"]) in content_ids


def _face_bytes(face_id: int) -> tuple[bytes, str] | None:
    try:
        path = faces.face_thumb_file(int(face_id))
    except FileNotFoundError:
        return None
    if not path.exists():
        return None
    return path.read_bytes(), "image/jpeg"


def _original_bytes(photo_id: int) -> tuple[bytes, str] | None:
    with db() as conn:
        row = conn.execute("SELECT path FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not row:
        return None
    src = Path(row["path"])
    if src.exists():
        return src.read_bytes(), "application/octet-stream"
    local = device.readable_path(row["path"])
    if local:
        return local.read_bytes(), "application/octet-stream"
    return None


async def _device_call(
    pair: str, method: str, params: dict[str, Any] | None, timeout: float
) -> dict[str, Any]:
    global _dev_id
    ws = _ws_ref
    if ws is None:
        raise RuntimeError("Fotomob provider is not connected")
    with _dev_lock:
        _dev_id += 1
        req_id = str(_dev_id)
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _dev_pending[req_id] = fut
    await ws.send(
        json.dumps(
            {
                "op": "devreq",
                "id": req_id,
                "pair": pair,
                "method": method,
                "params": params or {},
            }
        )
    )
    try:
        return await asyncio.wait_for(fut, timeout)
    finally:
        _dev_pending.pop(req_id, None)


def device_call_sync(
    pair: str,
    method: str,
    params: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    loop = _loop_ref
    if loop is None:
        raise RuntimeError("Fotomob provider is not connected")
    fut = asyncio.run_coroutine_threadsafe(
        _device_call(pair, method, params, timeout), loop
    )
    return fut.result(timeout=timeout + 5)


def _handle_device(method: str, params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    pair = str(params.get("pair") or "").strip()
    if method == "device_hello":
        src = device.get_by_pair(pair)
        if not src:
            return _denied(req_id, 404, "Unknown device pair")
        out = device.set_online(pair, True, params.get("label"))
        body = _jsonable(out) or {}
        body["fotoman_name"] = _instance_name()
        return {"id": req_id, "op": "res", "ok": True, "body": body}
    if method == "device_lookup":
        src = device.get_by_pair(pair)
        if not src:
            return _denied(req_id, 404, "Unknown device pair")
        body = _jsonable(device.annotate_source(src)) or {}
        body["fotoman_name"] = _instance_name()
        return {"id": req_id, "op": "res", "ok": True, "body": body}
    if method == "device_put":
        raw = params.get("file") if isinstance(params.get("file"), dict) else params
        out = device.put_upload(
            pair,
            raw,
            folder=params.get("folder"),
            queue_scan=bool(params.get("queue_scan") or params.get("finalize")),
        )
        if not out:
            return _denied(req_id, 404, "Unknown device pair")
        return {"id": req_id, "op": "res", "ok": True, "body": _jsonable(out)}
    if method == "device_catalog":
        files = params.get("files") if isinstance(params.get("files"), list) else []
        out = device.apply_catalog(pair, files, folder=params.get("folder"))
        if not out:
            return _denied(req_id, 404, "Unknown device pair")
        return {"id": req_id, "op": "res", "ok": True, "body": _jsonable(out)}
    if method == "device_bye":
        src = device.set_online(pair, False)
        if not src:
            return _denied(req_id, 404, "Unknown device pair")
        return {"id": req_id, "op": "res", "ok": True, "body": _jsonable(src)}
    return {"id": req_id, "op": "res", "ok": False, "status": 400, "error": f"Unknown method {method}"}


def _handle(msg: dict[str, Any]) -> dict[str, Any]:
    method = msg.get("method")
    params = msg.get("params") or {}
    req_id = msg.get("id")
    try:
        if str(method or "").startswith("device_"):
            return _handle_device(str(method), params, req_id)
        if method == "tree":
            scope = _share_scope(params)
            if not scope:
                return {
                    "id": req_id,
                    "op": "res",
                    "ok": True,
                    "share_ok": False,
                    "body": [],
                }
            tree = collections.list_collections_for_share_tokens(_share_tokens(params))
            return {
                "id": req_id,
                "op": "res",
                "ok": True,
                "share_ok": True,
                "body": [_slim_collection(n) for n in tree],
            }
        if method in ("album", "map"):
            scope = _share_scope(params)
            if not scope:
                return _denied(req_id)
            # Same one-row-per-content path as fotoman Collections browse.
            key = params.get("collection_id")
            if key is None:
                key = params.get("collection")
            if key is None:
                return {
                    "id": req_id,
                    "op": "res",
                    "ok": False,
                    "status": 400,
                    "error": "Collection required",
                }
            fid = (
                int(key)
                if isinstance(key, int) or str(key).isdigit()
                else collections.resolve_collection_key(str(key))
            )
            if int(fid) not in scope["collection_ids"]:
                return _denied(req_id)
            if method == "map":
                try:
                    from fotoman.geocode import fill_missing_place_coords

                    fill_missing_place_coords(fetch_limit=3)
                except Exception:
                    pass
            body = collections.photos_for_collection(
                fid,
                limit=int(params.get("top_k") or 10000),
                include_culled=bool(params.get("include_culled") or False),
                include_subfolders=bool(params.get("include_subfolders", True)),
            )
            slim = _jsonable(_slim_photos(body))
            if method == "map":
                slim = [p for p in slim if p.get("lat") is not None and p.get("lon") is not None]
            return {"id": req_id, "op": "res", "ok": True, "body": slim}
        if method == "people":
            scope = _share_scope(params)
            if not scope:
                return {"id": req_id, "op": "res", "ok": True, "body": []}
            labeled = _bool_param(params.get("labeled"))
            if labeled is None:
                labeled = True
            return {
                "id": req_id,
                "op": "res",
                "ok": True,
                "body": _slim_people(labeled, scope["content_ids"]),
            }
        if method == "search":
            scope = _share_scope(params)
            if not scope:
                return {"id": req_id, "op": "res", "ok": True, "body": []}
            q = str(params.get("q") or "")
            mode = str(params.get("mode") or "meta")
            top_k = int(params.get("top_k") or 48)
            visible = scope["content_ids"]
            if not visible:
                return {"id": req_id, "op": "res", "ok": True, "body": []}
            allow = params.get("content_ids")
            if isinstance(allow, str):
                allow = {int(x) for x in allow.split(",") if x.strip().isdigit()}
            elif isinstance(allow, list):
                allow = {int(x) for x in allow}
            else:
                allow = None
            allow = visible if allow is None else (allow & visible)
            cid = params.get("collection_id")
            ckey = params.get("collection")
            if cid is not None or (ckey and str(ckey).strip()):
                fid = (
                    int(cid)
                    if cid is not None
                    else collections.resolve_collection_key(str(ckey))
                )
                if int(fid) not in scope["collection_ids"]:
                    return _denied(req_id)
            filters = dict(
                taken_from=params.get("taken_from"),
                taken_to=params.get("taken_to"),
                camera=params.get("camera"),
                album=params.get("album"),
                aspect=params.get("aspect"),
                place=params.get("place"),
                lat=params.get("lat"),
                lon=params.get("lon"),
                radius_km=params.get("radius_km"),
                has_gps=params.get("has_gps"),
                source_id=params.get("source_id"),
                keepers_only=bool(params.get("keepers_only") or False),
                include_culled=bool(params.get("include_culled") or False),
                content_ids=allow,
                collection_id=params.get("collection_id"),
                collection=params.get("collection"),
                person_id=params.get("person_id"),
                include_subfolders=params.get("include_subfolders", True),
            )
            if filters["collection_id"] is not None:
                filters["collection_id"] = int(filters["collection_id"])
            if filters["person_id"] is not None:
                filters["person_id"] = int(filters["person_id"])
            if mode == "meta":
                body = search.search_metadata(text=q or None, limit=top_k, **filters)
            elif not q.strip():
                return {"id": req_id, "op": "res", "ok": False, "status": 400, "error": "Query text required"}
            elif mode == "hybrid":
                body = search.hybrid_search(q, top_k=top_k, **filters)
            else:
                body = search.search_visual(q, top_k=top_k, **filters)
            return {"id": req_id, "op": "res", "ok": True, "body": _jsonable(_slim_photos(body))}
        if method == "face":
            scope = _share_scope(params)
            if not scope:
                return _denied(req_id)
            fid = int(params.get("face_id"))
            if not _face_is_shared(fid, scope["content_ids"]):
                return _denied(req_id)
            got = _face_bytes(fid)
            if not got:
                return {"id": req_id, "op": "res", "ok": False, "status": 404, "error": "Not found"}
            data, ctype = got
            return {
                "id": req_id,
                "op": "res",
                "ok": True,
                "contentType": ctype,
                "body": base64.b64encode(data).decode("ascii"),
            }
        if method in ("thumb", "original"):
            scope = _share_scope(params)
            if not scope:
                return _denied(req_id)
            pid = int(params.get("photo_id"))
            if not _photo_is_shared(pid, scope["content_ids"]):
                return _denied(req_id)
            got = _thumb_bytes(pid) if method == "thumb" else _original_bytes(pid)
            if not got:
                return {"id": req_id, "op": "res", "ok": False, "status": 404, "error": "Not found"}
            data, ctype = got
            return {
                "id": req_id,
                "op": "res",
                "ok": True,
                "contentType": ctype,
                "body": base64.b64encode(data).decode("ascii"),
            }
        return {"id": req_id, "op": "res", "ok": False, "status": 400, "error": f"Unknown method {method}"}
    except ValueError as e:
        return {"id": req_id, "op": "res", "ok": False, "status": 400, "error": str(e)}
    except Exception as e:
        return {"id": req_id, "op": "res", "ok": False, "status": 500, "error": str(e)}


async def _session(url: str) -> None:
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        from websockets import connect  # type: ignore[no-redef]

    global _ws_ref, _loop_ref
    name = _instance_name()
    async with connect(url, max_size=80 * 1024 * 1024) as ws:
        _loop_ref = asyncio.get_running_loop()
        _ws_ref = ws
        await ws.send(json.dumps({"op": "hello", "name": name}))
        log.info("Fotomob provider connected as %r at %s", name, url)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                op = msg.get("op")
                if op == "devres":
                    fut = _dev_pending.get(str(msg.get("id")))
                    if fut and not fut.done():
                        if msg.get("ok"):
                            fut.set_result(msg)
                        else:
                            fut.set_exception(
                                RuntimeError(msg.get("error") or "Device error")
                            )
                    continue
                if op != "req":
                    continue

                async def reply(m: dict[str, Any] = msg) -> None:
                    def run() -> str:
                        return json.dumps(_handle(m))

                    payload = await asyncio.to_thread(run)
                    await ws.send(payload)

                asyncio.create_task(reply())
        finally:
            if _ws_ref is ws:
                _ws_ref = None


async def _loop() -> None:
    while True:
        url = _fotomob_ws_url()
        if not url:
            await asyncio.sleep(5)
            continue
        try:
            log.info("Fotomob connecting to %s", url)
            await _session(url)
        except Exception as e:
            log.warning("Fotomob disconnected from %s: %s", url, e)
        await asyncio.sleep(2)


def start_fotomob_link() -> None:
    def run() -> None:
        asyncio.run(_loop())

    threading.Thread(target=run, name="fotomob-link", daemon=True).start()
