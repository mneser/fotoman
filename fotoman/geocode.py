from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Any

# ISO 3166-1 alpha-2 → English short name (common photo destinations + fallbacks)
_COUNTRY_NAMES: dict[str, str] = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan", "AL": "Albania",
    "AM": "Armenia", "AR": "Argentina", "AT": "Austria", "AU": "Australia", "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina", "BD": "Bangladesh", "BE": "Belgium", "BG": "Bulgaria",
    "BO": "Bolivia", "BR": "Brazil", "BT": "Bhutan", "CA": "Canada", "CH": "Switzerland",
    "CL": "Chile", "CN": "China", "CO": "Colombia", "CR": "Costa Rica", "CU": "Cuba",
    "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "DO": "Dominican Republic",
    "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt", "ES": "Spain", "ET": "Ethiopia",
    "FI": "Finland", "FJ": "Fiji", "FR": "France", "GB": "United Kingdom", "GE": "Georgia",
    "GR": "Greece", "GT": "Guatemala", "HK": "Hong Kong", "HN": "Honduras", "HR": "Croatia",
    "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IN": "India",
    "IQ": "Iraq", "IR": "Iran", "IS": "Iceland", "IT": "Italy", "JM": "Jamaica", "JO": "Jordan",
    "JP": "Japan", "KE": "Kenya", "KH": "Cambodia", "KR": "South Korea", "KZ": "Kazakhstan",
    "LA": "Laos", "LB": "Lebanon", "LK": "Sri Lanka", "LT": "Lithuania", "LU": "Luxembourg",
    "LV": "Latvia", "MA": "Morocco", "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro",
    "MK": "North Macedonia", "MM": "Myanmar", "MN": "Mongolia", "MT": "Malta", "MX": "Mexico",
    "MY": "Malaysia", "MZ": "Mozambique", "NA": "Namibia", "NG": "Nigeria", "NI": "Nicaragua",
    "NL": "Netherlands", "NO": "Norway", "NP": "Nepal", "NZ": "New Zealand", "OM": "Oman",
    "PA": "Panama", "PE": "Peru", "PH": "Philippines", "PK": "Pakistan", "PL": "Poland",
    "PR": "Puerto Rico", "PT": "Portugal", "PY": "Paraguay", "QA": "Qatar", "RO": "Romania",
    "RS": "Serbia", "RU": "Russia", "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore",
    "SI": "Slovenia", "SK": "Slovakia", "SV": "El Salvador", "TH": "Thailand", "TJ": "Tajikistan",
    "TN": "Tunisia", "TR": "Turkey", "TW": "Taiwan", "TZ": "Tanzania", "UA": "Ukraine",
    "UG": "Uganda", "US": "United States", "UY": "Uruguay", "UZ": "Uzbekistan", "VE": "Venezuela",
    "VN": "Vietnam", "XK": "Kosovo", "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
    # Often missing from older maps / overseas territories seen in EXIF
    "GH": "Ghana", "GP": "Guadeloupe", "MF": "Saint Martin", "SX": "Sint Maarten",
}

# Common aliases → canonical English short name (matches reverse-geocode labels).
_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "u.s.a.": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "great britain": "United Kingdom",
    "england": "United Kingdom",
    "holland": "Netherlands",
    "czechia": "Czechia",
    "czech republic": "Czechia",
    "russia": "Russia",
    "south korea": "South Korea",
    "korea": "South Korea",
    "viet nam": "Vietnam",
}

_NOMINATIM_UA = "fotoman/1.0 (local photo library; contact: local)"
_nominatim_lock = None  # lazy threading.Lock
_nominatim_last = 0.0


@lru_cache(maxsize=1)
def _rg():
    import reverse_geocoder as rg

    return rg


def country_name(cc: str | None) -> str | None:
    if not cc:
        return None
    return _COUNTRY_NAMES.get(cc.upper(), cc.upper())


@lru_cache(maxsize=1)
def _country_by_lower() -> dict[str, str]:
    out = {v.lower(): v for v in _COUNTRY_NAMES.values()}
    out.update(_COUNTRY_ALIASES)
    for cc, name in _COUNTRY_NAMES.items():
        out[cc.lower()] = name
    return out


def normalize_country(name: str | None) -> str | None:
    """Map freeform / alias country text to the reverse-geocode English name."""
    if not name:
        return None
    raw = name.strip()
    if not raw:
        return None
    return _country_by_lower().get(raw.lower(), raw)


def parse_city_country(label: str | None) -> dict[str, str | None]:
    """Parse a manual place as ``City/District, [State/Province,] Country``.

    Comma-separated: first token is city or district, last is country, optional
    middle token(s) are state/province (stored as ``region``). Single-token
    labels stay freeform (city/country None, place_label set).
    """
    text = (label or "").strip()
    if not text:
        return empty_place()
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) == 1:
        return {
            "city": None,
            "region": None,
            "country": None,
            "place_label": parts[0],
        }
    city = parts[0]
    country = normalize_country(parts[-1])
    region = ", ".join(parts[1:-1]) if len(parts) > 2 else None
    return {
        "city": city,
        "region": region,
        "country": country,
        "place_label": _format_place(city, region, country) or text,
    }


def _cache_key(city: str, country: str, region: str | None = None) -> str:
    reg = (region or "").strip().lower()
    return f"{city.strip().lower()}|{reg}|{country.strip().lower()}"


def _nominatim_lock_get():
    global _nominatim_lock
    if _nominatim_lock is None:
        import threading

        _nominatim_lock = threading.Lock()
    return _nominatim_lock


def _nominatim_search(
    city: str, country: str, region: str | None = None
) -> tuple[float, float] | None:
    """Forward-geocode via Nominatim (1 req/s). Returns (lat, lon) or None."""
    global _nominatim_last
    q: dict[str, str] = {
        "city": city,
        "country": country,
        "format": "json",
        "limit": "1",
    }
    if region:
        q["state"] = region
    params = urllib.parse.urlencode(q)
    url = f"https://nominatim.openstreetmap.org/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _NOMINATIM_UA})
    with _nominatim_lock_get():
        elapsed = time.monotonic() - _nominatim_last
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            data = []
        _nominatim_last = time.monotonic()
    if not data:
        return None
    try:
        lat = float(data[0]["lat"])
        lon = float(data[0]["lon"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    return (lat, lon) if is_valid_gps(lat, lon) else None


def _nominatim_freeform(query: str) -> dict[str, Any] | None:
    """Free-text Nominatim search. Returns {lat, lon, city, region, country} or None."""
    global _nominatim_last
    params = urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": "1", "addressdetails": "1"}
    )
    url = f"https://nominatim.openstreetmap.org/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _NOMINATIM_UA})
    with _nominatim_lock_get():
        elapsed = time.monotonic() - _nominatim_last
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            data = []
        _nominatim_last = time.monotonic()
    if not data:
        return None
    hit = data[0]
    try:
        lat = float(hit["lat"])
        lon = float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not is_valid_gps(lat, lon):
        return None
    addr = hit.get("address") or {}
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("county")
    )
    region = addr.get("state") or addr.get("region")
    cc = addr.get("country_code", "").upper()
    country = country_name(cc) or addr.get("country")
    return {"lat": lat, "lon": lon, "city": city, "region": region, "country": country}


def forward_geocode_label(label: str, *, fetch: bool = True) -> dict[str, Any] | None:
    """Forward-geocode a freeform place_label. Returns structured place dict or None.

    Checks place_geo_cache first; on miss, queries Nominatim and caches the result.
    """
    label = label.strip()
    if not label:
        return None
    key = f"freeform|{label.lower()}"
    from fotoman.db import db

    with db() as conn:
        row = conn.execute(
            "SELECT lat, lon, found, city, country FROM place_geo_cache WHERE query = ?",
            (key,),
        ).fetchone()
        if row is not None:
            if int(row["found"] or 0) and is_valid_gps(row["lat"], row["lon"]):
                return {
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                    "city": row["city"],
                    "country": row["country"],
                }
            return None
    if not fetch:
        return None
    result = _nominatim_freeform(label)
    with db() as conn:
        if result:
            conn.execute(
                """
                INSERT OR REPLACE INTO place_geo_cache(query, city, country, lat, lon, found, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, datetime('now'))
                """,
                (key, result.get("city"), result.get("country"), result["lat"], result["lon"]),
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO place_geo_cache(query, city, country, lat, lon, found, updated_at)
                VALUES (?, ?, ?, NULL, NULL, 0, datetime('now'))
                """,
                (key, label, None),
            )
    return result


def lookup_city_coords(
    city: str | None,
    country: str | None,
    region: str | None = None,
    *,
    fetch: bool = True,
) -> tuple[float, float] | None:
    """Cached city/[state/]country → (lat, lon). Uses SQLite ``place_geo_cache``."""
    city = (city or "").strip()
    country = normalize_country(country) or ""
    region = (region or "").strip() or None
    if not city or not country:
        return None
    key = _cache_key(city, country, region)
    from fotoman.db import db

    with db() as conn:
        row = conn.execute(
            "SELECT lat, lon, found FROM place_geo_cache WHERE query = ?",
            (key,),
        ).fetchone()
        if row is not None:
            if int(row["found"] or 0) and is_valid_gps(row["lat"], row["lon"]):
                return float(row["lat"]), float(row["lon"])
            return None
    if not fetch:
        return None
    coords = _nominatim_search(city, country, region)
    with db() as conn:
        if coords:
            conn.execute(
                """
                INSERT OR REPLACE INTO place_geo_cache(query, city, country, lat, lon, found, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, datetime('now'))
                """,
                (key, city, country, coords[0], coords[1]),
            )
            return coords
        conn.execute(
            """
            INSERT OR REPLACE INTO place_geo_cache(query, city, country, lat, lon, found, updated_at)
            VALUES (?, ?, ?, NULL, NULL, 0, datetime('now'))
            """,
            (key, city, country),
        )
    return None


def fill_missing_place_coords(*, fetch_limit: int = 3) -> int:
    """Write city-center lat/lon onto contents that have city+country but no GPS.

    Applies cached lookups immediately; at most ``fetch_limit`` Nominatim
    requests for uncached cities (so map load stays responsive).
    """
    from fotoman.db import db

    updated = 0
    with db() as conn:
        pairs = conn.execute(
            """
            SELECT DISTINCT city, region, country FROM photo_contents
            WHERE city IS NOT NULL AND city != ''
              AND country IS NOT NULL AND country != ''
              AND (lat IS NULL OR lon IS NULL)
            """
        ).fetchall()

    pending: list[tuple[str, str | None, str]] = []
    for r in pairs:
        city, region, country = r["city"], r["region"], r["country"]
        coords = lookup_city_coords(city, country, region, fetch=False)
        if coords:
            lat, lon = coords
            with db() as conn:
                cur = conn.execute(
                    """
                    UPDATE photo_contents
                    SET lat = ?, lon = ?
                    WHERE city = ? AND country = ?
                      AND IFNULL(region, '') = IFNULL(?, '')
                      AND (lat IS NULL OR lon IS NULL)
                    """,
                    (lat, lon, city, country, region),
                )
                updated += int(cur.rowcount or 0)
        else:
            pending.append((city, region, country))

    for city, region, country in pending[: max(0, fetch_limit)]:
        coords = lookup_city_coords(city, country, region, fetch=True)
        if not coords:
            continue
        lat, lon = coords
        with db() as conn:
            cur = conn.execute(
                """
                UPDATE photo_contents
                SET lat = ?, lon = ?
                WHERE city = ? AND country = ?
                  AND IFNULL(region, '') = IFNULL(?, '')
                  AND (lat IS NULL OR lon IS NULL)
                """,
                (lat, lon, city, country, region),
            )
            updated += int(cur.rowcount or 0)
    return updated


def normalize_freeform_places(conn) -> int:
    """Parse place_label-only rows as City/District, [State,] Country."""
    rows = conn.execute(
        """
        SELECT id, place_label FROM photo_contents
        WHERE place_label IS NOT NULL AND place_label != ''
          AND (country IS NULL OR country = '')
        """
    ).fetchall()
    n = 0
    for r in rows:
        parsed = parse_city_country(r["place_label"])
        if not parsed.get("country") or not parsed.get("city"):
            continue
        conn.execute(
            """
            UPDATE photo_contents
            SET city = ?, region = ?, country = ?, place_label = ?
            WHERE id = ?
            """,
            (
                parsed["city"],
                parsed["region"],
                parsed["country"],
                parsed["place_label"],
                r["id"],
            ),
        )
        n += 1
    return n


def is_valid_gps(lat: float | None, lon: float | None) -> bool:
    """Reject missing coords and Null Island placeholders (0,0 → Takoradi)."""
    if lat is None or lon is None:
        return False
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        return False
    # Cameras / Google Takeout often write empty GPS as exact zeros.
    return abs(la) >= 1e-5 or abs(lo) >= 1e-5


def _format_place(city: str | None, region: str | None, country: str | None) -> str | None:
    parts = [p for p in (city, region, country) if p]
    # Drop duplicate adjacent names (e.g. city == admin2)
    deduped: list[str] = []
    for p in parts:
        if not deduped or deduped[-1].lower() != p.lower():
            deduped.append(p)
    return ", ".join(deduped) if deduped else None


def empty_place() -> dict[str, Any]:
    return {"city": None, "region": None, "country": None, "place_label": None}


def reverse_places(coords: list[tuple[float, float]]) -> list[dict[str, str | None]]:
    """Offline reverse geocode a batch of (lat, lon) pairs."""
    if not coords:
        return []
    # Preserve order: geocode only valid points, fill empties for Null Island / bad GPS.
    valid_idx = [i for i, (la, lo) in enumerate(coords) if is_valid_gps(la, lo)]
    out: list[dict[str, str | None]] = [empty_place() for _ in coords]
    if not valid_idx:
        return out
    rg = _rg()
    # mode=1 → single-threaded; avoids multiprocessing pickling issues under uvicorn
    raw = rg.search([coords[i] for i in valid_idx], mode=1)
    if isinstance(raw, dict):
        raw = [raw]
    for i, row in zip(valid_idx, raw):
        city = (row.get("name") or "").strip() or None
        region = (row.get("admin1") or "").strip() or None
        country = country_name(row.get("cc"))
        out[i] = {
            "city": city,
            "region": region,
            "country": country,
            "place_label": _format_place(city, region, country),
        }
    return out


def reverse_one(lat: float, lon: float) -> dict[str, str | None]:
    return reverse_places([(lat, lon)])[0]