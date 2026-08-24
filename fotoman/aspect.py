"""Display aspect class from stored pixel size and user rotation.

Stored width/height already match EXIF-transposed pixels. 90°/270° user
rotation swaps the displayed frame, so classification uses that display size.
Panorama is any displayed ratio ≥ 2.35:1; square allows 5% slack.
"""

from __future__ import annotations

SQUARE_MAX_RATIO = 1.05
PANORAMA_MIN_RATIO = 2.35

ASPECT_CLASSES = ("landscape", "portrait", "square", "panorama")
ASPECT_LABELS = {
    "landscape": "Landscape",
    "portrait": "Portrait",
    "square": "Square",
    "panorama": "Panorama",
}

# Table alias ``c`` (photo_contents), same as PHOTO_SELECT.
_DW = "CASE WHEN (IFNULL(c.orientation, 0) % 180) = 90 THEN c.height ELSE c.width END"
_DH = "CASE WHEN (IFNULL(c.orientation, 0) % 180) = 90 THEN c.width ELSE c.height END"
_RATIO = f"(CAST(MAX({_DW}, {_DH}) AS REAL) / MIN({_DW}, {_DH}))"


def aspect_class_sql() -> str:
    r = PANORAMA_MIN_RATIO
    return f"""
CASE
  WHEN c.width IS NULL OR c.height IS NULL OR c.width <= 0 OR c.height <= 0 THEN NULL
  WHEN {_RATIO} >= {r} THEN 'panorama'
  WHEN {_RATIO} <= {SQUARE_MAX_RATIO} THEN 'square'
  WHEN ({_DW}) > ({_DH}) THEN 'landscape'
  ELSE 'portrait'
END
""".strip()


ASPECT_CLASS_SQL = aspect_class_sql()


def display_size(
    width: int | None, height: int | None, orientation: int = 0
) -> tuple[int, int] | None:
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    if int(orientation or 0) % 180 == 90:
        return int(height), int(width)
    return int(width), int(height)


def classify(
    width: int | None, height: int | None, orientation: int = 0
) -> str | None:
    size = display_size(width, height, orientation)
    if size is None:
        return None
    dw, dh = size
    ratio = max(dw, dh) / min(dw, dh)
    if ratio >= PANORAMA_MIN_RATIO:
        return "panorama"
    if ratio <= SQUARE_MAX_RATIO:
        return "square"
    return "landscape" if dw > dh else "portrait"
