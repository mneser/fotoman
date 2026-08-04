from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps

TAG_MAP = {v: k for k, v in ExifTags.TAGS.items()}
GPS_MAP = {v: k for k, v in ExifTags.GPSTAGS.items()}


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


def taken_at_to_exif(value: str) -> str:
    """DB/ISO taken_at → EXIF ``YYYY:MM:DD HH:MM:SS``."""
    return normalize_taken_at(value).replace("-", ":", 2)


def write_taken_at(path: Path, taken_at: str) -> None:
    """Write DateTimeOriginal / Digitized / DateTime into the image file (lossless JPEG/TIFF)."""
    import piexif

    exif_str = taken_at_to_exif(taken_at)
    exif_bytes = exif_str.encode("ascii")
    suffix = path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".tif", ".tiff"}:
        raise ValueError(f"Cannot write EXIF date for {suffix or 'unknown'} files")

    try:
        exif_dict = piexif.load(str(path))
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    exif_dict.setdefault("0th", {})
    exif_dict.setdefault("Exif", {})
    exif_dict["0th"][piexif.ImageIFD.DateTime] = exif_bytes
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_bytes
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = exif_bytes
    piexif.insert(piexif.dump(exif_dict), str(path))


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


def pixel_fingerprint(
    path: Path, size: int = 16, orientation: int = 0
) -> str | None:
    """Content fingerprint resilient to resize/re-encode/format change.

    Downscales to a tiny grayscale grid and hashes the pixels. Same photo saved
    as JPEG vs PNG (or at different resolutions) usually shares this hash.
    ``orientation`` applies a clockwise 90° factor after EXIF transpose.
    """
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im = apply_orientation(im, orientation)
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


def _dms_to_deg(dms, ref: str | None) -> float | None:
    try:
        d = _rational_to_float(dms[0])
        m = _rational_to_float(dms[1])
        s = _rational_to_float(dms[2])
        if d is None or m is None or s is None:
            return None
        deg = d + m / 60.0 + s / 3600.0
        if ref in {"S", "W"}:
            deg = -deg
        return deg
    except Exception:
        return None


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
            thumb_dest.parent.mkdir(parents=True, exist_ok=True)
            im.save(thumb_dest, format="JPEG", quality=82, optimize=True)
    except Exception:
        # Fall back to header-only metadata if thumb fails mid-way
        if out["width"] is None:
            return extract_exif(path)
    return out


def album_from_rel(rel_path: str) -> str:
    parts = Path(rel_path).parts
    if len(parts) >= 2:
        return parts[0] if parts[0] != "." else (parts[1] if len(parts) > 1 else "")
    return ""


def place_from_dirname(name: str) -> str | None:
    """Parse a folder name into a Place label.

    From the start: ``_`` → space; insert a space before each uppercase that
    follows a lowercase; stop on a digit or special character (``-`` and
    spaces allowed).
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
    return label or None


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