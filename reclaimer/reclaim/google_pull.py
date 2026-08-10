"""Google Photos Library API pull into OUT."""

from __future__ import annotations

import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from reclaim.google_auth import load_credentials
from reclaim.manifest import ManifestStore, RunRecord, RunStats, now
from reclaim.place import DedupMode, Layout, apply_place_to_stats, place_file
from reclaim.util import ExitCode, file_sha1

PHOTOS_BASE = "https://photoslibrary.googleapis.com/v1"


def _authed_client(creds) -> httpx.Client:
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
    return httpx.Client(
        base_url=PHOTOS_BASE,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=120.0,
    )


def _request_with_backoff(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    max_retries: int = 6,
    **kwargs,
) -> httpx.Response:
    delay = 1.0
    last: httpx.Response | None = None
    for attempt in range(max_retries):
        resp = client.request(method, url, **kwargs)
        last = resp
        if resp.status_code == 429 or resp.status_code >= 500:
            # quota / transient
            ra = resp.headers.get("Retry-After")
            sleep_for = float(ra) if ra and ra.isdigit() else delay
            time.sleep(sleep_for)
            delay = min(delay * 2, 60.0)
            continue
        return resp
    assert last is not None
    return last


def _list_albums(client: httpx.Client) -> dict[str, str]:
    """mediaItemId -> album title (best-effort; first album wins)."""
    mapping: dict[str, str] = {}
    page_token: str | None = None
    while True:
        params: dict = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = _request_with_backoff(client, "GET", "/albums", params=params)
        if resp.status_code == 403:
            raise PermissionError(
                "Photos Library API denied (403). Enable the API and ensure "
                "your OAuth client can access the library. "
                f"Body: {resp.text[:400]}"
            )
        if resp.status_code != 200:
            break
        data = resp.json()
        for album in data.get("albums", []):
            album_id = album.get("id")
            title = album.get("title") or "Untitled"
            if not album_id:
                continue
            # search media in album
            a_token: str | None = None
            while True:
                body: dict = {"albumId": album_id, "pageSize": 100}
                if a_token:
                    body["pageToken"] = a_token
                r2 = _request_with_backoff(client, "POST", "/mediaItems:search", json=body)
                if r2.status_code != 200:
                    break
                d2 = r2.json()
                for item in d2.get("mediaItems", []):
                    mid = item.get("id")
                    if mid and mid not in mapping:
                        mapping[mid] = title
                a_token = d2.get("nextPageToken")
                if not a_token:
                    break
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return mapping


def _iter_media_items(
    client: httpx.Client,
    *,
    since: datetime | None,
    include_videos: bool,
):
    page_token: str | None = None
    filters: dict | None = None
    if since is not None:
        filters = {
            "dateFilter": {
                "ranges": [
                    {
                        "startDate": {
                            "year": since.year,
                            "month": since.month,
                            "day": since.day,
                        },
                        "endDate": {
                            "year": 9999,
                            "month": 12,
                            "day": 31,
                        },
                    }
                ]
            }
        }
    while True:
        if filters:
            body: dict = {"pageSize": 100, "filters": filters}
            if page_token:
                body["pageToken"] = page_token
            resp = _request_with_backoff(client, "POST", "/mediaItems:search", json=body)
        else:
            params: dict = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = _request_with_backoff(client, "GET", "/mediaItems", params=params)

        if resp.status_code == 403:
            raise PermissionError(
                "Photos Library API denied (403). "
                f"Body: {resp.text[:500]}"
            )
        if resp.status_code == 429:
            raise RuntimeError(
                "Google Photos quota exceeded (429). Wait and retry; "
                "see README for quota notes."
            )
        if resp.status_code != 200:
            raise RuntimeError(f"List failed ({resp.status_code}): {resp.text[:500]}")

        data = resp.json()
        for item in data.get("mediaItems", []):
            mime = (item.get("mimeType") or "").lower()
            is_video = mime.startswith("video/")
            if is_video and not include_videos:
                continue
            yield item
        page_token = data.get("nextPageToken")
        if not page_token:
            break


def _download_original(
    client: httpx.Client,
    item: dict,
    dest: Path,
) -> None:
    base = item.get("baseUrl")
    if not base:
        raise RuntimeError("media item missing baseUrl")
    mime = (item.get("mimeType") or "").lower()
    # =d original for images; =dv for videos
    url = base + ("=dv" if mime.startswith("video/") else "=d")
    # baseUrl downloads go to lh3 — use absolute URL without Photos API host
    with httpx.stream(
        "GET",
        url,
        headers={"Authorization": client.headers["Authorization"]},
        timeout=300.0,
        follow_redirects=True,
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"download {resp.status_code}: {resp.text[:200]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as f:
            for chunk in resp.iter_bytes(1 << 16):
                f.write(chunk)


def _date_prefix(item: dict) -> str | None:
    meta = item.get("mediaMetadata") or {}
    created = meta.get("creationTime")
    if not created:
        return None
    # 2014-10-02T15:01:23Z
    return created[:10]


def pull(
    out: Path,
    *,
    since: datetime | None = None,
    include_videos: bool = True,
    dedup: DedupMode = DedupMode.HARDLINK,
    layout: Layout = Layout.ALBUMS,
    progress: Callable[[str], None] | None = None,
) -> tuple[RunStats, ExitCode]:
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    log = progress or (lambda _m: None)
    stats = RunStats()
    store = ManifestStore(out)
    started = now()
    source = "google_photos"

    try:
        creds = load_credentials()
    except (FileNotFoundError, RuntimeError) as e:
        log(str(e))
        return stats, ExitCode.USAGE

    with _authed_client(creds) as client:
        log("loading album membership (best-effort)…")
        try:
            album_map = _list_albums(client)
            stats.albums = len(set(album_map.values()))
            log(f"mapped {len(album_map)} items across {stats.albums} album(s)")
        except PermissionError as e:
            log(str(e))
            return stats, ExitCode.USAGE
        except Exception as e:  # noqa: BLE001
            log(f"album map skipped: {e}")
            album_map = {}

        log("listing media…")
        try:
            items = list(
                _iter_media_items(client, since=since, include_videos=include_videos)
            )
        except (PermissionError, RuntimeError) as e:
            log(str(e))
            return stats, ExitCode.USAGE

        log(f"{len(items)} media item(s) to consider")
        tmp_dir = Path(tempfile.mkdtemp(prefix="reclaim-gpull-"))
        try:
            for i, item in enumerate(items, 1):
                mid = item.get("id") or ""
                filename = item.get("filename") or f"{mid}.bin"
                if mid and store.has_media_id(mid):
                    stats.skipped_existing += 1
                    continue
                tmp = tmp_dir / filename
                try:
                    _download_original(client, item, tmp)
                    sha1 = file_sha1(tmp)
                    album = album_map.get(mid)
                    result = place_file(
                        tmp,
                        store=store,
                        source=source,
                        layout=layout,
                        dedup=dedup,
                        album=album,
                        date_prefix=_date_prefix(item),
                        media_id=mid or None,
                        original_name=filename,
                        known_sha1=sha1,
                    )
                    if result.action == "copied":
                        stats.downloaded += 1
                    elif result.action == "skipped":
                        stats.skipped_existing += 1
                    elif result.action == "hardlinked":
                        stats.hardlinked_exact += 1
                        stats.downloaded += 1  # still "got" it into tree
                    elif result.action == "symlinked":
                        stats.symlinked_exact += 1
                        stats.downloaded += 1
                    if i % 25 == 0:
                        log(f"…{i}/{len(items)} ({stats.downloaded} new)")
                except Exception as e:  # noqa: BLE001
                    stats.errors += 1
                    log(f"error {filename}: {e}")
                finally:
                    if tmp.exists():
                        tmp.unlink(missing_ok=True)
        finally:
            try:
                tmp_dir.rmdir()
            except OSError:
                import shutil

                shutil.rmtree(tmp_dir, ignore_errors=True)

    code = ExitCode.OK if stats.errors == 0 else ExitCode.PARTIAL
    store.write_run(
        RunRecord(
            command="google pull",
            source=source,
            started_at=started,
            finished_at=now(),
            output=str(out),
            stats=stats.__dict__.copy(),
            ok=code == ExitCode.OK,
            message=(
                f"{stats.downloaded} downloaded, "
                f"{stats.skipped_existing} skipped_existing, "
                f"{stats.errors} errors"
            ),
        )
    )
    return stats, code
