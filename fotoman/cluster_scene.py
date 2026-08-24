"""SCENE_MATCH clustering: time + perceptual fingerprint + color + homography.

HASH_VECTOR unions on SigLIP cosine. Similar architecture (temples, palaces)
then chains into one giant cluster, while same-subject shots at different
angles stay ungrouped because embeddings fall below the slider.

SCENE_MATCH:
- joins a short capture burst unless visuals strongly disagree
- uses 16×16 grayscale cosine, color histograms, and ORB homography inside
  a visit window (the slider) to catch viewpoint changes
- does not chain high embeddings across hours just because they "look like a temple"
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import cv2
import numpy as np

from fotoman.db import content_thumb_path
from fotoman.index_store import get_store


CLUSTER_SCENE_MATCH = "SCENE_MATCH"

# City-level reverse-geocode pins are shared by a whole place; ignore them.
_GEOCODE_SOURCES = {"geocode"}


def _parse_taken_at(value: str | None) -> datetime | None:
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return None


def dt_seconds(ta: str | None, tb: str | None) -> float | None:
    da, db = _parse_taken_at(ta), _parse_taken_at(tb)
    if da is None or db is None:
        return None
    return abs((da - db).total_seconds())


def gps_meters(
    lat_a: float | None,
    lon_a: float | None,
    lat_b: float | None,
    lon_b: float | None,
    *,
    source_a: str | None = None,
    source_b: str | None = None,
) -> float | None:
    """Haversine meters, or None when GPS is missing / city-level geocode."""
    if source_a in _GEOCODE_SOURCES or source_b in _GEOCODE_SOURCES:
        return None
    if None in (lat_a, lon_a, lat_b, lon_b):
        return None
    rlat1, rlat2 = math.radians(float(lat_a)), math.radians(float(lat_b))
    dlat = rlat2 - rlat1
    dlon = math.radians(float(lon_b) - float(lon_a))
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(h)))


@dataclass(frozen=True)
class SceneParams:
    window_s: float
    burst_s: float
    emb_floor: float
    color_floor: float
    fp_floor: float
    homo_inliers: int
    gps_max_m: float = 400.0
    fp_exact: float = 0.99
    veto_emb: float = 0.42
    veto_color: float = 0.22
    veto_fp: float = 0.86


def scene_params_from_threshold(thr: float) -> SceneParams:
    """Map HASH_VECTOR's 0.86–0.98 slider onto visit-window / cue floors."""
    t = max(0.86, min(0.98, float(thr)))
    span = (0.98 - t) / 0.12  # 0 = Burst, 1 = Familiar
    window_s = 20.0 + span * 460.0  # 20s … ~8 min
    burst_s = min(90.0, window_s)
    return SceneParams(
        window_s=window_s,
        burst_s=burst_s,
        emb_floor=0.90 - span * 0.16,  # 0.90 … 0.74
        color_floor=0.92 - span * 0.18,  # 0.92 … 0.74
        fp_floor=0.985 - span * 0.04,  # 0.985 … 0.945
        homo_inliers=max(12, 18 - int(span * 6)),
    )


@dataclass(frozen=True)
class SceneCues:
    embedding: float | None = None
    fingerprint: float | None = None
    color: float | None = None
    homography_inliers: int | None = None
    dt_seconds: float | None = None
    gps_meters: float | None = None


def scene_pair_joins(cues: SceneCues, params: SceneParams) -> bool:
    """Whether two photos are the same scene under SCENE_MATCH."""
    fp = float(cues.fingerprint or 0.0)
    if fp >= params.fp_exact:
        return True
    if cues.gps_meters is not None and cues.gps_meters > params.gps_max_m:
        return False

    emb = float(cues.embedding or 0.0)
    col = float(cues.color or 0.0)
    homo = int(cues.homography_inliers or 0)
    dt = cues.dt_seconds
    in_window = dt is not None and dt <= params.window_s
    in_burst = dt is not None and dt <= params.burst_s

    if in_burst:
        if (
            emb >= params.veto_emb
            or col >= params.veto_color
            or fp >= params.veto_fp
            or homo >= 8
        ):
            return True
        return False

    if in_window:
        if (
            emb >= params.emb_floor
            or col >= params.color_floor
            or fp >= params.fp_floor
            or homo >= params.homo_inliers
        ):
            return True
        # Viewpoint change: a bit below floors but still the same visit.
        if emb >= params.emb_floor - 0.05 and (
            col >= params.color_floor - 0.12 or fp >= params.fp_floor - 0.03
        ):
            return True

    # Strong geometry without a clock (undated files / long revisits).
    if homo >= params.homo_inliers + 8 and emb >= 0.70:
        return True
    return False


def _gray16_and_hist(bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32)
    n = float(np.linalg.norm(small))
    fp = (small / n).reshape(-1) if n else small.reshape(-1)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).flatten()
    hist = hist.astype(np.float32)
    hn = float(np.linalg.norm(hist))
    if hn:
        hist = hist / hn
    return fp, hist


def _load_thumb_bgr(content_id: int) -> np.ndarray | None:
    path = content_thumb_path(content_id)
    if not path.is_file():
        return None
    im = cv2.imread(str(path), cv2.IMREAD_COLOR)
    return im if im is not None and im.size else None


def _cosine(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None:
        return None
    return float(np.clip(np.dot(a, b), 0.0, 1.0))


class _HomoCache:
    def __init__(self) -> None:
        self.orb = cv2.ORB_create(nfeatures=400)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        self._kd: dict[int, tuple[Any, Any]] = {}

    def _kd_for(self, cid: int, bgr: np.ndarray | None) -> tuple[Any, Any] | None:
        if cid in self._kd:
            return self._kd[cid]
        if bgr is None:
            self._kd[cid] = (None, None)
            return None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        kps, desc = self.orb.detectAndCompute(gray, None)
        self._kd[cid] = (kps, desc)
        return kps, desc

    def inliers(
        self,
        cid_a: int,
        bgr_a: np.ndarray | None,
        cid_b: int,
        bgr_b: np.ndarray | None,
    ) -> int:
        ka = self._kd_for(cid_a, bgr_a)
        kb = self._kd_for(cid_b, bgr_b)
        if not ka or not kb:
            return 0
        k1, d1 = ka
        k2, d2 = kb
        if d1 is None or d2 is None or len(k1) < 8 or len(k2) < 8:
            return 0
        try:
            knn = self.matcher.knnMatch(d1, d2, k=2)
        except cv2.error:
            return 0
        good = []
        for pair in knn:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good.append(m)
        if len(good) < 8:
            return 0
        src = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        if mask is None:
            return 0
        return int(mask.ravel().sum())


def _embedding_lookup(content_ids: list[int]) -> dict[int, np.ndarray]:
    store = get_store()
    id_to_pos = {int(cid): i for i, cid in enumerate(store.ids)}
    out: dict[int, np.ndarray] = {}
    for cid in content_ids:
        pos = id_to_pos.get(int(cid))
        if pos is None:
            continue
        out[int(cid)] = store.index.reconstruct(pos).astype(np.float32)
    return out


def cluster_scene_contents(
    by_content: dict[int, list[dict[str, Any]]],
    active_ids: list[int],
    params: SceneParams,
) -> list[list[int]]:
    """Union-find content ids that SCENE_MATCH considers the same scene."""
    if len(active_ids) < 2:
        return []

    id_to_i = {cid: i for i, cid in enumerate(active_ids)}
    parent = list(range(len(active_ids)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    vecs = _embedding_lookup(active_ids)
    feats: dict[int, tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]] = {}
    for cid in active_ids:
        bgr = _load_thumb_bgr(cid)
        if bgr is None:
            feats[cid] = (None, None, None)
            continue
        fp, hist = _gray16_and_hist(bgr)
        feats[cid] = (fp, hist, bgr)

    homo = _HomoCache()
    reps = {cid: by_content[cid][0] for cid in active_ids}

    # Time-ordered sweep: only pairs inside the visit window (or undated).
    ordered = sorted(
        active_ids,
        key=lambda cid: (
            reps[cid].get("taken_at") or "",
            cid,
        ),
    )
    dated = [cid for cid in ordered if _parse_taken_at(reps[cid].get("taken_at"))]
    undated = [cid for cid in ordered if cid not in set(dated)]

    def consider(cid_a: int, cid_b: int, *, need_homo: bool) -> None:
        ia, ib = id_to_i[cid_a], id_to_i[cid_b]
        if find(ia) == find(ib):
            return
        a, b = reps[cid_a], reps[cid_b]
        fa, fb = feats[cid_a], feats[cid_b]
        va, vb = vecs.get(cid_a), vecs.get(cid_b)
        emb = float(np.dot(va, vb)) if va is not None and vb is not None else None
        fp_s = _cosine(fa[0], fb[0])
        col_s = _cosine(fa[1], fb[1])
        dt = dt_seconds(a.get("taken_at"), b.get("taken_at"))
        dist = gps_meters(
            a.get("lat"),
            a.get("lon"),
            b.get("lat"),
            b.get("lon"),
            source_a=a.get("coord_source"),
            source_b=b.get("coord_source"),
        )
        cues = SceneCues(
            embedding=emb,
            fingerprint=fp_s,
            color=col_s,
            homography_inliers=None,
            dt_seconds=dt,
            gps_meters=dist,
        )
        if scene_pair_joins(cues, params):
            union(ia, ib)
            return
        if not need_homo:
            return
        # Homography is the expensive cue — only if time says maybe.
        in_window = dt is not None and dt <= params.window_s
        if not in_window and dt is not None:
            return
        inn = homo.inliers(cid_a, fa[2], cid_b, fb[2])
        if inn <= 0:
            return
        cues = SceneCues(
            embedding=emb,
            fingerprint=fp_s,
            color=col_s,
            homography_inliers=inn,
            dt_seconds=dt,
            gps_meters=dist,
        )
        if scene_pair_joins(cues, params):
            union(ia, ib)

    for i, cid_a in enumerate(dated):
        ta = _parse_taken_at(reps[cid_a].get("taken_at"))
        for cid_b in dated[i + 1 :]:
            tb = _parse_taken_at(reps[cid_b].get("taken_at"))
            if ta is None or tb is None:
                break
            if abs((tb - ta).total_seconds()) > params.window_s:
                break
            consider(cid_a, cid_b, need_homo=True)

    # Undated: fingerprint / strong geometry only (no time chaining).
    undated_params = SceneParams(
        window_s=0.0,
        burst_s=0.0,
        emb_floor=1.1,
        color_floor=1.1,
        fp_floor=1.1,
        homo_inliers=params.homo_inliers,
        gps_max_m=params.gps_max_m,
        fp_exact=params.fp_exact,
    )
    for i, cid_a in enumerate(undated):
        for cid_b in undated[i + 1 :]:
            ia, ib = id_to_i[cid_a], id_to_i[cid_b]
            fa, fb = feats[cid_a], feats[cid_b]
            va, vb = vecs.get(cid_a), vecs.get(cid_b)
            emb = float(np.dot(va, vb)) if va is not None and vb is not None else None
            fp_s = _cosine(fa[0], fb[0])
            inn = 0
            cues = SceneCues(
                embedding=emb,
                fingerprint=fp_s,
                color=_cosine(fa[1], fb[1]),
                homography_inliers=None,
                dt_seconds=None,
                gps_meters=gps_meters(
                    reps[cid_a].get("lat"),
                    reps[cid_a].get("lon"),
                    reps[cid_b].get("lat"),
                    reps[cid_b].get("lon"),
                    source_a=reps[cid_a].get("coord_source"),
                    source_b=reps[cid_b].get("coord_source"),
                ),
            )
            if scene_pair_joins(cues, undated_params):
                union(ia, ib)
                continue
            inn = homo.inliers(cid_a, fa[2], cid_b, fb[2])
            cues = SceneCues(
                embedding=emb,
                fingerprint=fp_s,
                color=cues.color,
                homography_inliers=inn,
                dt_seconds=None,
                gps_meters=cues.gps_meters,
            )
            if scene_pair_joins(cues, undated_params):
                union(ia, ib)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i, cid in enumerate(active_ids):
        clusters[find(i)].append(cid)
    return [cids for cids in clusters.values() if len(cids) >= 2]
