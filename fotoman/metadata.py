from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import ExifTags, Image, ImageOps

TAG_MAP = {v: k for k, v in ExifTags.TAGS.items()}
GPS_MAP = {v: k for k, v in ExifTags.GPSTAGS.items()}

# Thumb-sized assessment keeps scores comparable across originals.
_QUALITY_MAX_SIDE = 384

# Provenance for photo_contents.taken_at (higher rank wins; manual is sticky).
TAKEN_AT_SOURCE_RANK = {
    "mtime": 10,
    "folder": 20,
    "filename": 30,
    "exif": 40,
    "manual": 100,
}

# Provenance for photo_contents lat/lon. Manual pins are sticky; EXIF GPS
# beats city-center geocode; geocode never overwrites either.
COORD_SOURCE_RANK = {
    "geocode": 10,
    "exif": 40,
    "manual": 100,
}

EXIF_WRITE_SUFFIXES = {".jpg", ".jpeg", ".tif", ".tiff"}
_COORD_EPS = 1e-5

# 20yy[-_.]mm[-_.]dd with optional time; or compact 20yymmdd[HHMMSS].
_DATE_SEP = re.compile(
    r"(?<!\d)(20\d{2}|19\d{2})[-_.](\d{2})[-_.](\d{2})"
    r"(?:[ T_-](\d{2})[:.\-_]?(\d{2})(?:[:.\-_]?(\d{2}))?)?(?!\d)"
)
_DATE_COMPACT = re.compile(
    r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})"
    r"(?:[_-]?(\d{2})(\d{2})(\d{2}))?(?!\d)"
)


def assess_image_quality(im: Image.Image) -> dict[str, float]:
    """Sharpness + exposure → combined quality for relative keeper ranking.

    Scores are for comparing near-duplicates / bursts, not absolute aesthetics.
    """
    rgb = im if im.mode == "RGB" else im.convert("RGB")
    arr = np.asarray(rgb)
    if arr.size == 0:
        return {"sharpness": 0.0, "exposure": 0.0, "quality": 0.0}

    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharpness = float(np.log1p(lap_var))

    g = gray.astype(np.float32) / 255.0
    mean = float(g.mean())
    dark = float((g < 0.05).mean())
    bright = float((g > 0.95).mean())
    clip = dark + bright
    centered = 1.0 - min(1.0, abs(mean - 0.5) * 2.0)
    exposure = float(max(0.0, min(1.0, centered * (1.0 - min(1.0, clip * 1.5)))))

    quality = float(sharpness * (0.35 + 0.65 * exposure))
    return {"sharpness": sharpness, "exposure": exposure, "quality": quality}


def assess_quality_file(path: Path, *, max_side: int = _QUALITY_MAX_SIDE) -> dict[str, float] | None:
    """Score an image or thumb file; returns None on failure."""
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side))
            return assess_image_quality(im)
    except Exception:
        return None


def file_sha1(path: Path, blocksize: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while chunk := f.read(blocksize):
            h.update(chunk)
    return h.hexdigest()


def normalize_taken_at(value: str) -> str:
    """Normalize UI/API date strings to DB form: ``YYYY-MM-DD HH:MM:SS``."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("taken_at is empty")
    raw = raw.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=0, minute=0, second=0)
            elif fmt == "%Y-%m-%d %H:%M":
                dt = dt.replace(second=0)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"Invalid taken_at: {value!r}")


def _ymd_hms_to_taken_at(
    y: str, mo: str, d: str, hh: str | None = None, mm: str | None = None, ss: str | None = None
) -> str | None:
    try:
        dt = datetime(
            int(y),
            int(mo),
            int(d),
            int(hh) if hh is not None else 0,
            int(mm) if mm is not None else 0,
            int(ss) if ss is not None else 0,
        )
    except ValueError:
        return None
    if dt.year < 1900 or dt.year > 2100:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_date_from_text(text: str) -> str | None:
    """First plausible ``20yy[-]mm[-]dd`` (optional time) in a name segment."""
    if not text:
        return None
    for pat in (_DATE_SEP, _DATE_COMPACT):
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        taken = _ymd_hms_to_taken_at(g[0], g[1], g[2], g[3], g[4], g[5])
        if taken:
            return taken
    return None


def parse_date_from_path(path: Path) -> tuple[str | None, str | None]:
    """Date from filename stem, else parent folder names. Returns ``(taken_at, source)``."""
    stem = path.stem
    taken = parse_date_from_text(stem)
    if taken:
        return taken, "filename"
    # Walk parents nearest-first; skip filesystem roots / empty.
    for part in reversed(path.parts[:-1]):
        if not part or part in ("/", ".") or (len(part) == 2 and part[1] == ":"):
            continue
        taken = parse_date_from_text(part)
        if taken:
            return taken, "folder"
    return None, None


def resolve_taken_at(
    path: Path,
    *,
    exif_taken_at: str | None = None,
    mtime: float | None = None,
    allow_mtime: bool = True,
) -> tuple[str | None, str | None]:
    """Pick capture date: EXIF → filename → folder → mtime.

    Returns ``(taken_at, source)`` with source in
    ``exif`` / ``filename`` / ``folder`` / ``mtime``, or ``(None, None)``.
    """
    if exif_taken_at:
        try:
            return normalize_taken_at(str(exif_taken_at)), "exif"
        except ValueError:
            pass
    taken, source = parse_date_from_path(path)
    if taken:
        return taken, source
    if not allow_mtime:
        return None, None
    if mtime is None:
        try:
            mtime = float(path.stat().st_mtime)
        except OSError:
            return None, None
    try:
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"), "mtime"
    except (OSError, OverflowError, ValueError):
        return None, None


def can_replace_taken_at(
    existing_taken: str | None,
    existing_source: str | None,
    new_taken: str | None,
    new_source: str | None,
) -> bool:
    """True when ``new_*`` should overwrite DB date (manual / legacy stick)."""
    if not new_taken or not new_source:
        return False
    if not existing_taken:
        return True
    if (existing_source or "") == "manual":
        return False
    if not existing_source:
        # Legacy rows (pre-provenance): keep existing date.
        return False
    return TAKEN_AT_SOURCE_RANK.get(new_source, 0) > TAKEN_AT_SOURCE_RANK.get(
        existing_source, 0
    )


def normalize_coord_source(value: Any) -> str | None:
    src = str(value or "").strip().lower()
    return src if src in COORD_SOURCE_RANK else None


def infer_coord_source(row: dict[str, Any] | None) -> str | None:
    """Resolve coord provenance from a photo/content row (legacy gps flag)."""
    if not row:
        return None
    src = normalize_coord_source(row.get("coord_source"))
    if src:
        return src
    if int(row.get("gps") or 0):
        return "exif"
    if row.get("lat") is not None and row.get("lon") is not None:
        return "geocode"
    return None


def can_replace_coords(existing_source: str | None, new_source: str | None) -> bool:
    """True when ``new_source`` may overwrite stored lat/lon."""
    new_src = normalize_coord_source(new_source)
    if not new_src:
        return False
    existing = normalize_coord_source(existing_source)
    if not existing:
        return True
    return COORD_SOURCE_RANK.get(new_src, 0) >= COORD_SOURCE_RANK.get(existing, 0)


def gps_flag(coord_source: str | None) -> int:
    """Keep legacy ``gps`` in sync: 1 only for camera EXIF GPS."""
    return 1 if normalize_coord_source(coord_source) == "exif" else 0


def can_write_exif(path: Path) -> bool:
    return path.suffix.lower() in EXIF_WRITE_SUFFIXES


def coords_close(
    lat_a: float | None,
    lon_a: float | None,
    lat_b: float | None,
    lon_b: float | None,
    *,
    eps: float = _COORD_EPS,
) -> bool:
    if lat_a is None or lon_a is None or lat_b is None or lon_b is None:
        return lat_a is None and lon_a is None and lat_b is None and lon_b is None
    try:
        return abs(float(lat_a) - float(lat_b)) < eps and abs(float(lon_a) - float(lon_b)) < eps
    except (TypeError, ValueError):
        return False


def mtime_to_taken_at(mtime: float | None) -> str | None:
    if mtime is None:
        return None
    try:
        return datetime.fromtimestamp(float(mtime)).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def taken_at_to_exif(value: str) -> str:
    """DB/ISO taken_at → EXIF ``YYYY:MM:DD HH:MM:SS``."""
    return normalize_taken_at(value).replace("-", ":", 2)


def _load_exif_dict(path: Path) -> dict[str, Any]:
    import piexif

    try:
        return piexif.load(str(path))
    except Exception:
        return {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}


def _insert_exif_dict(path: Path, exif_dict: dict[str, Any]) -> None:
    import piexif

    piexif.insert(piexif.dump(exif_dict), str(path))


def write_taken_at(path: Path, taken_at: str) -> None:
    """Write DateTimeOriginal / Digitized / DateTime into the image file (lossless JPEG/TIFF)."""
    import piexif

    if not can_write_exif(path):
        raise ValueError(f"Cannot write EXIF date for {path.suffix.lower() or 'unknown'} files")

    exif_str = taken_at_to_exif(taken_at)
    exif_bytes = exif_str.encode("ascii")
    exif_dict = _load_exif_dict(path)
    exif_dict.setdefault("0th", {})
    exif_dict.setdefault("Exif", {})
    exif_dict["0th"][piexif.ImageIFD.DateTime] = exif_bytes
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_bytes
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_bytes
    _insert_exif_dict(path, exif_dict)


def _deg_to_dms_rational(deg: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    deg = abs(float(deg))
    d = int(deg)
    ms = (deg - d) * 60.0
    m = int(ms)
    s = (ms - m) * 60.0
    return ((d, 1), (m, 1), (int(round(s * 10000)), 10000))


def write_gps(path: Path, lat: float, lon: float) -> None:
    """Write GPSLatitude/Longitude into the image file (lossless JPEG/TIFF)."""
    import piexif

    from fotoman.geocode import is_valid_gps

    if not can_write_exif(path):
        raise ValueError(f"Cannot write EXIF GPS for {path.suffix.lower() or 'unknown'} files")
    if not is_valid_gps(lat, lon):
        raise ValueError("Invalid coordinates")

    exif_dict = _load_exif_dict(path)
    gps_ifd = exif_dict.setdefault("GPS", {})
    gps_ifd[piexif.GPSIFD.GPSVersionID] = (2, 0, 0, 0)
    gps_ifd[piexif.GPSIFD.GPSLatitudeRef] = b"N" if lat >= 0 else b"S"
    gps_ifd[piexif.GPSIFD.GPSLatitude] = _deg_to_dms_rational(lat)
    gps_ifd[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
    gps_ifd[piexif.GPSIFD.GPSLongitude] = _deg_to_dms_rational(lon)
    _insert_exif_dict(path, exif_dict)


def write_taken_at_mtime(path: Path, taken_at: str) -> float:
    """Set filesystem atime/mtime to ``taken_at``. Does not change file bytes/hash.

    Returns the unix mtime written.
    """
    dt = datetime.strptime(normalize_taken_at(taken_at), "%Y-%m-%d %H:%M:%S")
    ts = dt.timestamp()
    os.utime(path, (ts, ts))
    return ts


def normalize_orientation(value: Any) -> int:
    """Accept 0/90/180/270 clockwise quarter turns."""
    try:
        deg = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid orientation: {value!r}") from e
    if deg not in (0, 90, 180, 270):
        raise ValueError("orientation must be 0, 90, 180, or 270")
    return deg


def apply_orientation(im: Image.Image, orientation: int = 0) -> Image.Image:
    """Rotate image by a clockwise 90° factor (after EXIF transpose)."""
    deg = int(orientation or 0) % 360
    # PIL ROTATE_* are counter-clockwise; map clockwise turns accordingly.
    if deg == 90:
        return im.transpose(Image.Transpose.ROTATE_270)
    if deg == 180:
        return im.transpose(Image.Transpose.ROTATE_180)
    if deg == 270:
        return im.transpose(Image.Transpose.ROTATE_90)
    return im


def apply_mirror(im: Image.Image, mirrored: bool = False) -> Image.Image:
    """Horizontally flip image when mirrored is set."""
    if mirrored:
        return im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    return im


def pixel_fingerprint(
    path: Path, size: int = 16, orientation: int = 0, mirrored: bool = False
) -> str | None:
    """Content fingerprint resilient to resize/re-encode/format change.

    Downscales to a tiny grayscale grid and hashes the pixels. Same photo saved
    as JPEG vs PNG (or at different resolutions) usually shares this hash.
    ``orientation`` applies a clockwise 90° factor after EXIF transpose.
    ``mirrored`` applies a horizontal flip after orientation.
    """
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = apply_orientation(im, orientation)
            im = apply_mirror(im, mirrored)
            im = im.convert("L").resize((size, size), Image.Resampling.LANCZOS)
            return hashlib.sha1(im.tobytes()).hexdigest()
    except Exception:
        return None


def _rational_to_float(value: Any) -> float | None:
    try:
        if hasattr(value, "numerator"):
            return float(value.numerator) / float(value.denominator or 1)
        if isinstance(value, tuple) and len(value) == 2:
            return float(value[0]) / float(value[1] or 1)
        return float(value)
    except Exception:
        return None


def _ref_letter(ref: Any) -> str | None:
    if ref is None:
        return None
    if isinstance(ref, (bytes, bytearray)):
        ref = ref.decode("ascii", "ignore")
    text = str(ref).strip().upper()
    return text[:1] if text else None


def _dms_to_deg(dms, ref: Any) -> float | None:
    try:
        d = _rational_to_float(dms[0])
        m = _rational_to_float(dms[1])
        s = _rational_to_float(dms[2])
        if d is None or m is None or s is None:
            return None
        deg = d + m / 60.0 + s / 3600.0
        if _ref_letter(ref) in {"S", "W"}:
            deg = -deg
        return deg
    except Exception:
        return None


def _gps_via_piexif(path: Path) -> tuple[float | None, float | None]:
    """Pillow often misses Android GPS IFDs; piexif reads the same JPEG bytes."""
    try:
        import piexif

        gps = piexif.load(str(path)).get("GPS") or {}
        if not gps:
            return None, None
        lat = _dms_to_deg(
            gps.get(piexif.GPSIFD.GPSLatitude),
            gps.get(piexif.GPSIFD.GPSLatitudeRef),
        )
        lon = _dms_to_deg(
            gps.get(piexif.GPSIFD.GPSLongitude),
            gps.get(piexif.GPSIFD.GPSLongitudeRef),
        )
        return _normalize_gps(lat, lon)
    except Exception:
        return None, None


def _normalize_gps(lat: float | None, lon: float | None) -> tuple[float | None, float | None]:
    """Drop Null Island (0,0) placeholders that reverse-geocode as Takoradi, GH."""
    from fotoman.geocode import is_valid_gps

    if is_valid_gps(lat, lon):
        return lat, lon
    return None, None


def extract_exif(path: Path) -> dict[str, Any]:
    """Read size/camera/date/GPS without decoding pixels (exif_transpose is expensive)."""
    out: dict[str, Any] = {
        "width": None,
        "height": None,
        "taken_at": None,
        "camera": None,
        "lat": None,
        "lon": None,
    }
    try:
        with Image.open(path) as im:
            # Lazy header read — do not transpose/decode the full image here.
            w, h = im.size
            raw = im.getexif()
            orientation = raw.get(0x0112) if raw else None
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            out["width"], out["height"] = w, h
            if not raw:
                return out

            # Flatten IFDs when available
            exif: dict[Any, Any] = dict(raw)
            try:
                for ifd_id in (0x8769, 0x8825):  # Exif, GPS
                    ifd = raw.get_ifd(ifd_id)
                    if ifd:
                        exif.update(ifd)
            except Exception:
                pass

            make = exif.get(TAG_MAP.get("Make", -1))
            model = exif.get(TAG_MAP.get("Model", -1))
            parts = [str(x).strip() for x in (make, model) if x]
            if parts:
                out["camera"] = " ".join(parts)

            for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                tag = TAG_MAP.get(key)
                if tag is None:
                    continue
                val = exif.get(tag)
                if not val:
                    continue
                try:
                    out["taken_at"] = datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S").isoformat(sep=" ")
                    break
                except ValueError:
                    out["taken_at"] = str(val)
                    break

            gps = {}
            try:
                gps_ifd = raw.get_ifd(0x8825)
                gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
            except Exception:
                pass
            if gps:
                lat = _dms_to_deg(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
                lon = _dms_to_deg(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
                out["lat"], out["lon"] = _normalize_gps(lat, lon)
    except Exception:
        pass
    if out["lat"] is None or out["lon"] is None:
        lat, lon = _gps_via_piexif(path)
        if lat is not None and lon is not None:
            out["lat"], out["lon"] = lat, lon
    return out


def extract_exif_and_thumbnail(path: Path, thumb_dest: Path, size: int = 384) -> dict[str, Any]:
    """One image open for metadata + thumbnail (avoids double decode)."""
    out: dict[str, Any] = {
        "width": None,
        "height": None,
        "taken_at": None,
        "camera": None,
        "lat": None,
        "lon": None,
        "sharpness": None,
        "exposure": None,
        "quality": None,
    }
    try:
        with Image.open(path) as im:
            raw = im.getexif()
            # Metadata from headers before pixel work
            w, h = im.size
            orientation = raw.get(0x0112) if raw else None
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            out["width"], out["height"] = w, h
            if raw:
                exif: dict[Any, Any] = dict(raw)
                try:
                    for ifd_id in (0x8769, 0x8825):
                        ifd = raw.get_ifd(ifd_id)
                        if ifd:
                            exif.update(ifd)
                except Exception:
                    pass
                make = exif.get(TAG_MAP.get("Make", -1))
                model = exif.get(TAG_MAP.get("Model", -1))
                parts = [str(x).strip() for x in (make, model) if x]
                if parts:
                    out["camera"] = " ".join(parts)
                for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                    tag = TAG_MAP.get(key)
                    if tag is None:
                        continue
                    val = exif.get(tag)
                    if not val:
                        continue
                    try:
                        out["taken_at"] = datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S").isoformat(sep=" ")
                        break
                    except ValueError:
                        out["taken_at"] = str(val)
                        break
                try:
                    gps_ifd = raw.get_ifd(0x8825)
                    gps = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
                    lat = _dms_to_deg(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
                    lon = _dms_to_deg(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
                    out["lat"], out["lon"] = _normalize_gps(lat, lon)
                except Exception:
                    pass

            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            out.update(assess_image_quality(im if im.mode == "RGB" else im.convert("RGB")))
            thumb_dest.parent.mkdir(parents=True, exist_ok=True)
            im.save(thumb_dest, format="JPEG", quality=82, optimize=True)
    except Exception:
        # Fall back to header-only metadata if thumb fails mid-way
        if out["width"] is None:
            return extract_exif(path)
    if out["lat"] is None or out["lon"] is None:
        lat, lon = _gps_via_piexif(path)
        if lat is not None and lon is not None:
            out["lat"], out["lon"] = lat, lon
    return out


def album_from_rel(rel_path: str) -> str:
    parts = Path(rel_path).parts
    if len(parts) >= 2:
        return parts[0] if parts[0] != "." else (parts[1] if len(parts) > 1 else "")
    return ""


# Folder names that are never real places (Takeout, phone albums, camera rolls).
_IGNORE_PLACE_LABELS = frozenset(
    {
        "shared",
        "trash",
        "photos",
        "photo",
        "photos from",
        "android",
        "dcim",
        "camera",
        "download",
        "downloads",
        "pictures",
        "screenshot",
        "screenshots",
        "whatsapp",
        "images",
        "img",
        "movies",
        "videos",
    }
)


def is_ignored_place_label(label: str | None) -> bool:
    """True for non-place folder names (Shared, Trash, Photos, …)."""
    if not label:
        return False
    key = label.strip().lower()
    if key in _IGNORE_PLACE_LABELS:
        return True
    # "Photos 1", "Photos from 2023" → parsed as Photos / Photos from
    return key.startswith("photos ") or key.startswith("photo ")


def place_from_dirname(name: str) -> str | None:
    """Parse a folder name into a Place label.

    From the start: ``_`` → space; insert a space before each uppercase that
    follows a lowercase; stop on a digit or special character (``-`` and
    spaces allowed). Ignores non-place folders such as Shared / Trash.
    """
    if not name:
        return None
    out: list[str] = []
    for ch in name:
        if ch.isdigit():
            break
        if ch == "_":
            out.append(" ")
            continue
        if ch == "-" or ch.isspace():
            out.append("-" if ch == "-" else " ")
            continue
        if not ch.isalnum():
            break
        if out and ch.isupper() and out[-1].islower():
            out.append(" ")
        out.append(ch)
    label = " ".join("".join(out).split())
    if not label or is_ignored_place_label(label):
        return None
    return label


def deepest_dirname(rel_path: str) -> str:
    """Immediate parent folder of the file (empty if file is at archive root)."""
    parent = Path(rel_path).parent
    if str(parent) in (".", ""):
        return ""
    return parent.name


def place_from_rel(rel_path: str) -> str | None:
    """Derive Place from the deepest directory name in a relative path."""
    return place_from_dirname(deepest_dirname(rel_path))


def make_thumbnail(src: Path, dest: Path, size: int = 384) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(dest, format="JPEG", quality=82, optimize=True)