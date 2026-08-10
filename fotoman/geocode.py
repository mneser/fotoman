from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Any

# Ignored when checking that a freeform query matches the geocoded place.
_PLACE_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
        "near",
        "de",
        "la",
        "le",
        "el",
        "los",
        "las",
    }
)

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
    "GI": "Gibraltar", "GR": "Greece", "GT": "Guatemala", "HK": "Hong Kong", "HN": "Honduras", "HR": "Croatia",
    "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IN": "India",
    "IQ": "Iraq", "IR": "Iran", "IS": "Iceland", "IT": "Italy", "JM": "Jamaica", "JO": "Jordan",
    "JP": "Japan", "KE": "Kenya", "KH": "Cambodia", "KR": "South Korea", "KZ": "Kazakhstan",
    "LA": "Laos", "LB": "Lebanon", "LK": "Sri Lanka", "LT": "Lithuania", "LU": "Luxembourg",
    "LV": "Latvia", "MA": "Morocco", "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro",
    "MK": "North Macedonia", "MM": "Myanmar", "MN": "Mongolia", "MT": "Malta",
    "MU": "Mauritius", "MX": "Mexico",
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
    "AW": "Aruba", "BQ": "Bonaire", "CW": "Curaçao", "KY": "Cayman Islands",
    "TC": "Turks and Caicos Islands", "VG": "United States Virgin Islands",
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
    "curacao": "Curaçao",
    "curaçao": "Curaçao",
    "curacau": "Curaçao",  # common misspelling in folder labels
    "aruba": "Aruba",
}

# Bare place names that mean a first-level admin region, not a city.
# Explicit city forms (e.g. "Quebec City") are excluded in known_region().
_REGION_ALIASES: dict[str, tuple[str, str]] = {
    # Canada
    "quebec": ("Quebec", "Canada"),
    "ontario": ("Ontario", "Canada"),
    "british columbia": ("British Columbia", "Canada"),
    "bc": ("British Columbia", "Canada"),
    "alberta": ("Alberta", "Canada"),
    "manitoba": ("Manitoba", "Canada"),
    "saskatchewan": ("Saskatchewan", "Canada"),
    "nova scotia": ("Nova Scotia", "Canada"),
    "new brunswick": ("New Brunswick", "Canada"),
    "newfoundland": ("Newfoundland and Labrador", "Canada"),
    "newfoundland and labrador": ("Newfoundland and Labrador", "Canada"),
    "prince edward island": ("Prince Edward Island", "Canada"),
    "pei": ("Prince Edward Island", "Canada"),
    "yukon": ("Yukon", "Canada"),
    "northwest territories": ("Northwest Territories", "Canada"),
    "nunavut": ("Nunavut", "Canada"),
    # United States (names; abbreviations in _US_STATE_ABBR)
    "alabama": ("Alabama", "United States"),
    "alaska": ("Alaska", "United States"),
    "arizona": ("Arizona", "United States"),
    "arkansas": ("Arkansas", "United States"),
    "california": ("California", "United States"),
    "colorado": ("Colorado", "United States"),
    "connecticut": ("Connecticut", "United States"),
    "delaware": ("Delaware", "United States"),
    "florida": ("Florida", "United States"),
    "georgia": ("Georgia", "United States"),
    "hawaii": ("Hawaii", "United States"),
    "idaho": ("Idaho", "United States"),
    "illinois": ("Illinois", "United States"),
    "indiana": ("Indiana", "United States"),
    "iowa": ("Iowa", "United States"),
    "kansas": ("Kansas", "United States"),
    "kentucky": ("Kentucky", "United States"),
    "louisiana": ("Louisiana", "United States"),
    "maine": ("Maine", "United States"),
    "maryland": ("Maryland", "United States"),
    "massachusetts": ("Massachusetts", "United States"),
    "michigan": ("Michigan", "United States"),
    "minnesota": ("Minnesota", "United States"),
    "mississippi": ("Mississippi", "United States"),
    "missouri": ("Missouri", "United States"),
    "montana": ("Montana", "United States"),
    "nebraska": ("Nebraska", "United States"),
    "nevada": ("Nevada", "United States"),
    "new hampshire": ("New Hampshire", "United States"),
    "new jersey": ("New Jersey", "United States"),
    "new mexico": ("New Mexico", "United States"),
    "new york": ("New York", "United States"),
    "north carolina": ("North Carolina", "United States"),
    "north dakota": ("North Dakota", "United States"),
    "ohio": ("Ohio", "United States"),
    "oklahoma": ("Oklahoma", "United States"),
    "oregon": ("Oregon", "United States"),
    "pennsylvania": ("Pennsylvania", "United States"),
    "rhode island": ("Rhode Island", "United States"),
    "south carolina": ("South Carolina", "United States"),
    "south dakota": ("South Dakota", "United States"),
    "tennessee": ("Tennessee", "United States"),
    "texas": ("Texas", "United States"),
    "utah": ("Utah", "United States"),
    "vermont": ("Vermont", "United States"),
    "virginia": ("Virginia", "United States"),
    "washington": ("Washington", "United States"),
    "west virginia": ("West Virginia", "United States"),
    "wisconsin": ("Wisconsin", "United States"),
    "wyoming": ("Wyoming", "United States"),
    "district of columbia": ("District of Columbia", "United States"),
    "washington dc": ("District of Columbia", "United States"),
    "washington d.c.": ("District of Columbia", "United States"),
}

_US_STATE_ABBR: dict[str, str] = {
    "al": "Alabama", "ak": "Alaska", "az": "Arizona", "ar": "Arkansas",
    "ca": "California", "co": "Colorado", "ct": "Connecticut", "de": "Delaware",
    "fl": "Florida", "ga": "Georgia", "hi": "Hawaii", "id": "Idaho",
    "il": "Illinois", "in": "Indiana", "ia": "Iowa", "ks": "Kansas",
    "ky": "Kentucky", "la": "Louisiana", "me": "Maine", "md": "Maryland",
    "ma": "Massachusetts", "mi": "Michigan", "mn": "Minnesota", "ms": "Mississippi",
    "mo": "Missouri", "mt": "Montana", "ne": "Nebraska", "nv": "Nevada",
    "nh": "New Hampshire", "nj": "New Jersey", "nm": "New Mexico", "ny": "New York",
    "nc": "North Carolina", "nd": "North Dakota", "oh": "Ohio", "ok": "Oklahoma",
    "or": "Oregon", "pa": "Pennsylvania", "ri": "Rhode Island", "sc": "South Carolina",
    "sd": "South Dakota", "tn": "Tennessee", "tx": "Texas", "ut": "Utah",
    "vt": "Vermont", "va": "Virginia", "wa": "Washington", "wv": "West Virginia",
    "wi": "Wisconsin", "wy": "Wyoming", "dc": "District of Columbia",
}

# City spellings that must stay the city (never the province).
_REGION_CITY_FORMS = frozenset(
    {
        "quebec city",
        "ville de quebec",
        "ville de quebec city",
        "new york city",
        "nyc",
        "washington dc",
        "washington d.c.",
    }
)

_NOMINATIM_UA = "fotoman/1.0 (local photo library; contact: local)"
_nominatim_lock = None  # lazy threading.Lock
_nominatim_last = 0.0
_geo_worker_started = False
_geo_worker_stop = False


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


def known_country(name: str | None) -> str | None:
    """Canonical English country if ``name`` is a known country/alias, else None."""
    if not name:
        return None
    raw = name.strip()
    if not raw:
        return None
    return _country_by_lower().get(raw.lower())


def _fold_place(text: str) -> str:
    """Lowercase + strip accents for place-name matching (Québec → quebec)."""
    s = unicodedata.normalize("NFKD", (text or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def known_region(name: str | None) -> dict[str, str | None] | None:
    """If ``name`` is a known province/state alias, return region+country place dict.

    Bare ``Quebec`` / ``Québec`` → Quebec province, Canada. ``Quebec City`` is
    not a region (stays a city for freeform / structured parse).
    """
    if not name:
        return None
    folded = _fold_place(name)
    if not folded or folded in _REGION_CITY_FORMS:
        return None
    hit = _REGION_ALIASES.get(folded)
    if not hit and folded in _US_STATE_ABBR:
        hit = (_US_STATE_ABBR[folded], "United States")
    if not hit:
        return None
    region, country = hit
    return {
        "venue": None,
        "city": None,
        "region": region,
        "country": country,
        "place_label": _format_place(None, region, country),
    }


def _us_state_canonical(token: str | None) -> str | None:
    """Return canonical US state/province name for abbr or full name, else None."""
    if not token:
        return None
    folded = _fold_place(token)
    if folded in _US_STATE_ABBR:
        return _US_STATE_ABBR[folded]
    hit = _REGION_ALIASES.get(folded)
    if hit and hit[1] == "United States":
        return hit[0]
    return None


def _expand_place_parts(parts: list[str]) -> list[str]:
    """Expand ``Miami, FL`` → ``Miami, Florida, United States`` and similar."""
    if len(parts) < 2:
        return parts
    out = [p.strip() for p in parts if p.strip()]
    if len(out) < 2:
        return out
    last = out[-1]
    last_state = _us_state_canonical(last)
    last_country = known_country(last)
    # Prefer USPS / state name over ISO country when last token is a US state
    # (e.g. CA → California, not Canada; GA → Georgia state).
    if last_state and (not last_country or last_state.lower() == last_country.lower() or len(_fold_place(last)) == 2):
        out[-1] = last_state
        out.append("United States")
    elif last_country:
        out[-1] = last_country
    for i in range(len(out) - 1):
        st = _us_state_canonical(out[i])
        # Don't rewrite a token that is the city when it's also a state name
        # unless it's clearly an abbr in a middle slot.
        if st and i == len(out) - 2:
            out[i] = st
        elif st and len(_fold_place(out[i])) == 2:
            out[i] = st
    return out


def _looks_like_venue(name: str | None) -> bool:
    """Heuristic: multi-word or POI-ish tokens are venues, not cities."""
    if not name or not str(name).strip():
        return False
    raw = str(name).strip()
    folded = _fold_place(raw)
    if known_country(raw) or known_region(raw):
        return False
    if " " in raw:
        return True
    markers = (
        "stadium",
        "hotel",
        "airport",
        "museum",
        "temple",
        "church",
        "resort",
        "camp",
        "villa",
        "palace",
        "tower",
        "bridge",
        "helicopter",
        "ventures",
        "studio",
        "gallery",
        "marina",
        "harbor",
        "harbour",
        "park",
    )
    return any(m in folded for m in markers)


def _region_matches_country(region: str | None, country: str | None) -> bool:
    if not region or not country:
        return False
    folded = _fold_place(region)
    hit = _REGION_ALIASES.get(folded)
    if not hit and folded in _US_STATE_ABBR:
        hit = (_US_STATE_ABBR[folded], "United States")
    if not hit:
        return False
    return hit[1] == country


def parse_city_country(label: str | None) -> dict[str, str | None]:
    """Parse a manual place into venue/city/region/country.

    Comma-separated forms:
    - ``City, Country`` / ``City, ST`` / ``City, State, Country``
    - ``Venue, City, Country`` when the middle token is not a known region
    - ``Venue, City, Region, Country`` (4+ parts, last is country)
    - Single token: country, region alias, or freeform
    """
    text = (label or "").strip()
    if not text:
        return empty_place()
    parts = _expand_place_parts([p.strip() for p in text.split(",") if p.strip()])
    if len(parts) == 1:
        country = known_country(parts[0])
        if country:
            return {
                "venue": None,
                "city": None,
                "region": None,
                "country": country,
                "place_label": country,
            }
        region_place = known_region(parts[0])
        if region_place:
            return region_place
        return {
            "venue": None,
            "city": None,
            "region": None,
            "country": None,
            "place_label": parts[0],
        }

    country = known_country(parts[-1])
    if not country:
        # Last token isn't a known country (e.g. "Hard Rock Stadium, Miami").
        # Keep freeform / venue+city for Nominatim to complete.
        if len(parts) == 2 and _looks_like_venue(parts[0]):
            return {
                "venue": parts[0],
                "city": parts[1],
                "region": None,
                "country": None,
                "place_label": text,
            }
        return {
            "venue": None,
            "city": None,
            "region": None,
            "country": None,
            "place_label": text,
        }
    head = parts[:-1]

    if len(head) == 1:
        city = head[0]
        # "Quebec, Canada" → province, not the city (unless "Quebec City, …").
        if country:
            region_place = known_region(city)
            if region_place and region_place.get("country") == country:
                return region_place
        return {
            "venue": None,
            "city": city,
            "region": None,
            "country": country,
            "place_label": _format_place(city, None, country) or text,
        }

    if len(head) == 2:
        first, second = head[0], head[1]
        # Middle is known admin region for this country → city, region, country
        # unless the first token looks like a venue (Helicopter, Hawaii, US).
        if country and _region_matches_country(second, country):
            region = _us_state_canonical(second) or second
            if _looks_like_venue(first):
                return {
                    "venue": first,
                    "city": None,
                    "region": region,
                    "country": country,
                    "place_label": _format_place(None, region, country, venue=first)
                    or text,
                }
            return {
                "venue": None,
                "city": first,
                "region": region,
                "country": country,
                "place_label": _format_place(first, region, country) or text,
            }
        # Venue when first looks like a POI; otherwise city + freeform region
        # (matches reverse-geocode City, Admin1, Country for unknown admins).
        if _looks_like_venue(first):
            return {
                "venue": first,
                "city": second,
                "region": None,
                "country": country,
                "place_label": _format_place(second, None, country, venue=first) or text,
            }
        return {
            "venue": None,
            "city": first,
            "region": second,
            "country": country,
            "place_label": _format_place(first, second, country) or text,
        }

    # 4+ CSV: venue, city, region..., country
    venue = head[0]
    city = head[1]
    region = ", ".join(head[2:]) if len(head) > 2 else None
    if region and country and _region_matches_country(region.split(",")[0].strip(), country):
        # Normalize primary region token when it's a known state.
        reg_parts = [p.strip() for p in region.split(",")]
        if reg_parts:
            canon = _us_state_canonical(reg_parts[0]) or reg_parts[0]
            reg_parts[0] = canon
            region = ", ".join(reg_parts)
    return {
        "venue": venue,
        "city": city,
        "region": region,
        "country": country,
        "place_label": _format_place(city, region, country, venue=venue) or text,
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


def _is_latin_place_text(s: str | None) -> bool:
    """True when text is Latin-script (accents OK); reject CJK / Arabic / etc."""
    if not s or not str(s).strip():
        return False
    for ch in str(s):
        if ch.isascii() or ch.isspace():
            continue
        # Latin-1 supplement / Latin Extended (é, ñ, ü, …)
        o = ord(ch)
        if 0x00C0 <= o <= 0x024F or 0x1E00 <= o <= 0x1EFF:
            continue
        if ch in "-'’./()":
            continue
        if not ch.isalnum() and not ch.isspace():
            # punctuation already handled; other symbols OK if rare
            if ch in ",&":
                continue
            return False
        return False
    return True


def _latin_or_none(s: str | None) -> str | None:
    text = (s or "").strip() or None
    if not text:
        return None
    return text if _is_latin_place_text(text) else None


def _nominatim_headers() -> dict[str, str]:
    # Prefer English labels; local-script names are filtered on write-back anyway.
    return {"User-Agent": _NOMINATIM_UA, "Accept-Language": "en"}


def _nominatim_get(params: dict[str, str]) -> list[dict[str, Any]]:
    """GET Nominatim search; enforces ~1 req/s. Returns raw JSON list."""
    global _nominatim_last
    url = f"https://nominatim.openstreetmap.org/search?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_nominatim_headers())
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
    return data if isinstance(data, list) else []


def _parse_nominatim_hit(hit: dict[str, Any], query: str | None = None) -> dict[str, Any] | None:
    """Extract lat/lon + address fields from a Nominatim hit."""
    try:
        lat = float(hit["lat"])
        lon = float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not is_valid_gps(lat, lon):
        return None
    addr = hit.get("address") or {}
    raw_city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("county")
    )
    raw_region = addr.get("state") or addr.get("region")
    cc = (addr.get("country_code") or "").upper()
    country = country_name(cc) or _latin_or_none(addr.get("country")) or ""
    city = _latin_or_none(raw_city) or ""
    region = _latin_or_none(raw_region)
    display = _latin_or_none(hit.get("display_name")) or ""
    if query is not None:
        if not query_matches_resolved(query, display, city, region, country):
            return None
        if not city and _is_latin_place_text(query) and not known_country(query):
            city = query.strip()
    return {
        "lat": lat,
        "lon": lon,
        "city": city,
        "region": region,
        "country": country,
        "display_name": display,
    }


def _cache_upsert(
    conn,
    key: str,
    *,
    city: str = "",
    country: str = "",
    region: str | None = None,
    display_name: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    found: int = 0,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO place_geo_cache(
            query, city, country, region, display_name, lat, lon, found, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """,
        (key, city, country, region, display_name, lat, lon, found),
    )


def _nominatim_search(
    city: str, country: str, region: str | None = None
) -> dict[str, Any] | None:
    """Forward-geocode via Nominatim structured search. Returns details or None."""
    q: dict[str, str] = {
        "city": city,
        "country": country,
        "format": "json",
        "limit": "1",
        "addressdetails": "1",
    }
    if region:
        q["state"] = region
    data = _nominatim_get(q)
    if not data:
        return None
    return _parse_nominatim_hit(data[0])


def _nominatim_country(country: str) -> dict[str, Any] | None:
    """Country centroid via Nominatim structured country search."""
    data = _nominatim_get(
        {"country": country, "format": "json", "limit": "1", "addressdetails": "1"}
    )
    if not data:
        return None
    return _parse_nominatim_hit(data[0])


def _nominatim_region(region: str, country: str) -> dict[str, Any] | None:
    """State/province centroid via Nominatim structured search."""
    data = _nominatim_get(
        {
            "state": region,
            "country": country,
            "format": "json",
            "limit": "1",
            "addressdetails": "1",
        }
    )
    if not data:
        return None
    return _parse_nominatim_hit(data[0])


def _place_tokens(text: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _significant_place_tokens(text: str | None) -> list[str]:
    return [
        t
        for t in _place_tokens(text)
        if len(t) >= 2 and t not in _PLACE_STOPWORDS
    ]


def query_matches_resolved(query: str, *parts: str | None) -> bool:
    """True when every significant query token appears in the resolved place.

    Rejects loose Nominatim hits (e.g. folder ``Camping with Ali`` → Oman
    ``Jaalan Bani Bu Ali``) where the query is not actually that place.
    """
    needed = _significant_place_tokens(query)
    if not needed:
        return False
    hay: set[str] = set()
    for p in parts:
        hay.update(_place_tokens(p))
    return all(t in hay for t in needed)


def _nominatim_freeform(query: str) -> dict[str, Any] | None:
    """Free-text Nominatim search. Returns {lat, lon, city, region, country} or None."""
    data = _nominatim_get(
        {"q": query, "format": "json", "limit": "1", "addressdetails": "1"}
    )
    if not data:
        return None
    return _parse_nominatim_hit(data[0], query=query)


def forward_geocode_label(label: str, *, fetch: bool = True) -> dict[str, Any] | None:
    """Forward-geocode a freeform place_label. Returns structured place dict or None.

    Checks place_geo_cache first; on miss, queries Nominatim and caches the result.
    Bare region aliases (e.g. ``Quebec`` → province) resolve without freeform search.
    """
    label = label.strip()
    if not label:
        return None
    region_place = known_region(label)
    if region_place and region_place.get("region") and region_place.get("country"):
        coords = lookup_region_coords(
            region_place["region"], region_place["country"], fetch=fetch
        )
        if not coords:
            return None
        return {
            "lat": coords[0],
            "lon": coords[1],
            "city": "",
            "region": region_place["region"],
            "country": region_place["country"],
        }
    key = f"freeform|{label.lower()}"
    from fotoman.db import db

    with db() as conn:
        row = conn.execute(
            """
            SELECT lat, lon, found, city, country, region, display_name
            FROM place_geo_cache WHERE query = ?
            """,
            (key,),
        ).fetchone()
        if row is not None:
            if int(row["found"] or 0) and is_valid_gps(row["lat"], row["lon"]):
                city = _latin_or_none(row["city"]) or ""
                country = known_country(row["country"]) or _latin_or_none(row["country"]) or ""
                region = _latin_or_none(row["region"]) if "region" in row.keys() else None
                display = _latin_or_none(row["display_name"]) if "display_name" in row.keys() else ""
                match_city = "" if city.lower() == label.lower() else city
                if not query_matches_resolved(label, display, match_city, region, country):
                    conn.execute(
                        """
                        UPDATE place_geo_cache
                        SET found = 0, lat = NULL, lon = NULL, updated_at = datetime('now')
                        WHERE query = ?
                        """,
                        (key,),
                    )
                    return None
                if not city and _is_latin_place_text(label) and not known_country(label):
                    city = label.strip()
                return {
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                    "city": city,
                    "region": region,
                    "country": country,
                    "display_name": display or "",
                }
            return None
    if not fetch:
        return None
    result = _nominatim_freeform(label)
    with db() as conn:
        if result:
            _cache_upsert(
                conn,
                key,
                city=result.get("city") or "",
                country=result.get("country") or "",
                region=result.get("region"),
                display_name=result.get("display_name"),
                lat=result["lat"],
                lon=result["lon"],
                found=1,
            )
        else:
            _cache_upsert(conn, key, city=label, country="", found=0)
    return result


def lookup_city_details(
    city: str | None,
    country: str | None,
    region: str | None = None,
    *,
    fetch: bool = True,
) -> dict[str, Any] | None:
    """Cached city lookup returning lat/lon plus address fields when available."""
    city = (city or "").strip()
    country = normalize_country(country) or ""
    region = (region or "").strip() or None
    if not city or not country:
        return None
    key = _cache_key(city, country, region)
    from fotoman.db import db

    with db() as conn:
        row = conn.execute(
            """
            SELECT lat, lon, found, city, country, region, display_name
            FROM place_geo_cache WHERE query = ?
            """,
            (key,),
        ).fetchone()
        if row is not None:
            if int(row["found"] or 0) and is_valid_gps(row["lat"], row["lon"]):
                return {
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                    "city": _latin_or_none(row["city"]) or city,
                    "country": known_country(row["country"])
                    or _latin_or_none(row["country"])
                    or country,
                    "region": _latin_or_none(row["region"]) or region,
                    "display_name": _latin_or_none(row["display_name"]) or "",
                }
            return None
    if not fetch:
        return None
    details = _nominatim_search(city, country, region)
    with db() as conn:
        if details:
            _cache_upsert(
                conn,
                key,
                city=city,
                country=country,
                region=details.get("region") or region,
                display_name=details.get("display_name"),
                lat=details["lat"],
                lon=details["lon"],
                found=1,
            )
            return {
                "lat": details["lat"],
                "lon": details["lon"],
                "city": details.get("city") or city,
                "country": details.get("country") or country,
                "region": details.get("region") or region,
                "display_name": details.get("display_name") or "",
            }
        _cache_upsert(conn, key, city=city, country=country, region=region, found=0)
    return None


def lookup_city_coords(
    city: str | None,
    country: str | None,
    region: str | None = None,
    *,
    fetch: bool = True,
) -> tuple[float, float] | None:
    """Cached city/[state/]country → (lat, lon). Uses SQLite ``place_geo_cache``."""
    details = lookup_city_details(city, country, region, fetch=fetch)
    if not details:
        return None
    return float(details["lat"]), float(details["lon"])


def lookup_country_coords(
    country: str | None, *, fetch: bool = True
) -> tuple[float, float] | None:
    """Cached country centroid → (lat, lon). Uses SQLite ``place_geo_cache``."""
    country = known_country(country) or (normalize_country(country) or "")
    country = country.strip()
    if not country:
        return None
    key = f"country|{country.lower()}"
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
    details = _nominatim_country(country)
    with db() as conn:
        if details:
            _cache_upsert(
                conn,
                key,
                city="",
                country=country,
                region=details.get("region"),
                display_name=details.get("display_name"),
                lat=details["lat"],
                lon=details["lon"],
                found=1,
            )
            return float(details["lat"]), float(details["lon"])
        _cache_upsert(conn, key, city="", country=country, found=0)
    return None


def lookup_region_coords(
    region: str | None, country: str | None, *, fetch: bool = True
) -> tuple[float, float] | None:
    """Cached state/province centroid → (lat, lon). Uses SQLite ``place_geo_cache``."""
    region = (region or "").strip()
    country = known_country(country) or (normalize_country(country) or "")
    country = country.strip()
    if not region or not country:
        return None
    key = f"region|{region.lower()}|{country.lower()}"
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
    details = _nominatim_region(region, country)
    with db() as conn:
        if details:
            _cache_upsert(
                conn,
                key,
                city="",
                country=country,
                region=region,
                display_name=details.get("display_name"),
                lat=details["lat"],
                lon=details["lon"],
                found=1,
            )
            return float(details["lat"]), float(details["lon"])
        _cache_upsert(conn, key, city="", country=country, region=region, found=0)
    return None


def enqueue_place_geocode(
    *,
    kind: str,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
    label: str | None = None,
) -> None:
    """Queue a Nominatim lookup for the background worker (1 req/s)."""
    from fotoman.db import db

    if kind == "city" and city and country:
        query = _cache_key(city, country, region)
    elif kind == "region" and region and country:
        query = f"region|{region.lower()}|{country.lower()}"
    elif kind == "country" and country:
        query = f"country|{country.lower()}"
    elif kind == "freeform" and label:
        query = f"freeform|{label.strip().lower()}"
    else:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO place_geo_queue(query, kind, city, region, country)
            VALUES (?, ?, ?, ?, ?)
            """,
            (query, kind, city, region, country),
        )


def fill_missing_place_coords(*, fetch_limit: int = 0) -> int:
    """Write city/region-center lat/lon onto contents that have place but no GPS.

    Applies cached lookups immediately. Uncached places are enqueued for the
    background Nominatim worker (public API ~1 req/s). ``fetch_limit`` still
    allows a small synchronous burst when callers opt in.
    """
    from fotoman.db import db

    updated = 0
    with db() as conn:
        pairs = conn.execute(
            """
            SELECT DISTINCT city, region, country FROM photo_contents
            WHERE country IS NOT NULL AND country != ''
              AND (
                    (city IS NOT NULL AND city != '')
                 OR (region IS NOT NULL AND region != '')
              )
              AND IFNULL(gps, 0) = 0
              AND (lat IS NULL OR lon IS NULL)
            """
        ).fetchall()

    pending_city: list[tuple[str, str | None, str]] = []
    pending_region: list[tuple[str, str]] = []
    for r in pairs:
        city, region, country = r["city"], r["region"], r["country"]
        if city:
            coords = lookup_city_coords(city, country, region, fetch=False)
            if coords:
                lat, lon = coords
                with db() as conn:
                    cur = conn.execute(
                        """
                        UPDATE photo_contents
                        SET lat = ?, lon = ?, gps = 0
                        WHERE city = ? AND country = ?
                          AND IFNULL(region, '') = IFNULL(?, '')
                          AND IFNULL(gps, 0) = 0
                          AND (lat IS NULL OR lon IS NULL)
                        """,
                        (lat, lon, city, country, region),
                    )
                    updated += int(cur.rowcount or 0)
            else:
                pending_city.append((city, region, country))
        elif region:
            coords = lookup_region_coords(region, country, fetch=False)
            if coords:
                lat, lon = coords
                with db() as conn:
                    cur = conn.execute(
                        """
                        UPDATE photo_contents
                        SET lat = ?, lon = ?, gps = 0
                        WHERE (city IS NULL OR city = '')
                          AND region = ? AND country = ?
                          AND IFNULL(gps, 0) = 0
                          AND (lat IS NULL OR lon IS NULL)
                        """,
                        (lat, lon, region, country),
                    )
                    updated += int(cur.rowcount or 0)
            else:
                pending_region.append((region, country))

    for city, region, country in pending_city:
        enqueue_place_geocode(
            kind="city", city=city, region=region, country=country
        )
    for region, country in pending_region:
        enqueue_place_geocode(kind="region", region=region, country=country)

    fetches = 0
    limit = max(0, fetch_limit)
    for city, region, country in pending_city:
        if fetches >= limit:
            break
        coords = lookup_city_coords(city, country, region, fetch=True)
        fetches += 1
        if not coords:
            continue
        lat, lon = coords
        with db() as conn:
            cur = conn.execute(
                """
                UPDATE photo_contents
                SET lat = ?, lon = ?, gps = 0
                WHERE city = ? AND country = ?
                  AND IFNULL(region, '') = IFNULL(?, '')
                  AND IFNULL(gps, 0) = 0
                  AND (lat IS NULL OR lon IS NULL)
                """,
                (lat, lon, city, country, region),
            )
            updated += int(cur.rowcount or 0)
    for region, country in pending_region:
        if fetches >= limit:
            break
        coords = lookup_region_coords(region, country, fetch=True)
        fetches += 1
        if not coords:
            continue
        lat, lon = coords
        with db() as conn:
            cur = conn.execute(
                """
                UPDATE photo_contents
                SET lat = ?, lon = ?, gps = 0
                WHERE (city IS NULL OR city = '')
                  AND region = ? AND country = ?
                  AND IFNULL(gps, 0) = 0
                  AND (lat IS NULL OR lon IS NULL)
                """,
                (lat, lon, region, country),
            )
            updated += int(cur.rowcount or 0)
    return updated


def _apply_cached_coords_for_queue_item(kind: str, city, region, country) -> None:
    from fotoman.db import db

    if kind == "city" and city and country:
        coords = lookup_city_coords(city, country, region, fetch=False)
        if not coords:
            return
        lat, lon = coords
        with db() as conn:
            conn.execute(
                """
                UPDATE photo_contents
                SET lat = ?, lon = ?, gps = 0
                WHERE city = ? AND country = ?
                  AND IFNULL(region, '') = IFNULL(?, '')
                  AND IFNULL(gps, 0) = 0
                  AND (lat IS NULL OR lon IS NULL)
                """,
                (lat, lon, city, country, region),
            )
    elif kind == "region" and region and country:
        coords = lookup_region_coords(region, country, fetch=False)
        if not coords:
            return
        lat, lon = coords
        with db() as conn:
            conn.execute(
                """
                UPDATE photo_contents
                SET lat = ?, lon = ?, gps = 0
                WHERE (city IS NULL OR city = '')
                  AND region = ? AND country = ?
                  AND IFNULL(gps, 0) = 0
                  AND (lat IS NULL OR lon IS NULL)
                """,
                (lat, lon, region, country),
            )
    elif kind == "country" and country:
        coords = lookup_country_coords(country, fetch=False)
        if not coords:
            return
        lat, lon = coords
        with db() as conn:
            conn.execute(
                """
                UPDATE photo_contents
                SET lat = ?, lon = ?, gps = 0
                WHERE country = ?
                  AND (city IS NULL OR city = '')
                  AND (region IS NULL OR region = '')
                  AND IFNULL(gps, 0) = 0
                  AND (lat IS NULL OR lon IS NULL)
                """,
                (lat, lon, country),
            )


def _geo_worker_loop() -> None:
    from fotoman.db import db

    while not _geo_worker_stop:
        with db() as conn:
            row = conn.execute(
                """
                SELECT id, query, kind, city, region, country
                FROM place_geo_queue
                ORDER BY id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                item = None
            else:
                item = dict(row)
                conn.execute("DELETE FROM place_geo_queue WHERE id = ?", (item["id"],))
        if not item:
            time.sleep(2.0)
            continue
        kind = item["kind"]
        try:
            if kind == "city":
                lookup_city_coords(
                    item["city"], item["country"], item["region"], fetch=True
                )
            elif kind == "region":
                lookup_region_coords(item["region"], item["country"], fetch=True)
            elif kind == "country":
                lookup_country_coords(item["country"], fetch=True)
            elif kind == "freeform" and item["query"].startswith("freeform|"):
                label = item["query"][len("freeform|") :]
                forward_geocode_label(label, fetch=True)
            _apply_cached_coords_for_queue_item(
                kind, item.get("city"), item.get("region"), item.get("country")
            )
        except Exception:
            pass


def start_geo_worker() -> None:
    """Start the background Nominatim drain thread (idempotent)."""
    global _geo_worker_started
    if _geo_worker_started:
        return
    import threading

    _geo_worker_started = True
    threading.Thread(target=_geo_worker_loop, name="place-geo", daemon=True).start()


def _merge_place_from_details(
    place: dict[str, str | None], details: dict[str, Any]
) -> dict[str, str | None]:
    """Fill missing region/country from Nominatim; keep venue and parsed city."""
    out = dict(place)
    if not out.get("region") and details.get("region"):
        out["region"] = str(details["region"]).strip() or None
    if details.get("country"):
        canon = known_country(details["country"]) or normalize_country(details["country"])
        if canon:
            out["country"] = canon
    if not out.get("city") and details.get("city"):
        out["city"] = str(details["city"]).strip() or None
    out["place_label"] = (
        _format_place(
            out.get("city"),
            out.get("region"),
            out.get("country"),
            venue=out.get("venue"),
        )
        or out.get("place_label")
    )
    return out


def resolve_manual_place(
    label: str, *, fetch: bool = True
) -> tuple[dict[str, str | None], tuple[float, float] | None]:
    """Parse a Place edit and resolve coords + complete region via Nominatim."""
    place = parse_city_country(label)
    coords: tuple[float, float] | None = None

    def _coords_of(details: dict[str, Any] | None) -> tuple[float, float] | None:
        if not details or details.get("lat") is None or details.get("lon") is None:
            return None
        return float(details["lat"]), float(details["lon"])

    # Venue labels: freeform first (POI-aware), then structured city fallback.
    if place.get("venue") and place.get("country"):
        queries = [label.strip()]
        geo_bits = _format_place(
            place.get("city"), place.get("region"), place.get("country")
        )
        if geo_bits and geo_bits.lower() != label.strip().lower():
            queries.append(geo_bits)
        venue_city = _format_place(place.get("city"), None, place.get("country"), venue=place.get("venue"))
        if venue_city and venue_city not in queries:
            queries.insert(0, venue_city)
        for q in queries:
            try:
                result = forward_geocode_label(q, fetch=fetch)
            except Exception:
                result = None
            if result and result.get("country"):
                place = _merge_place_from_details(place, result)
                # Keep parsed venue/city when Nominatim returns a locality.
                if place.get("venue"):
                    pass
                coords = _coords_of(result)
                if coords:
                    break
        if not coords and place.get("city") and place.get("country"):
            details = lookup_city_details(
                place["city"], place["country"], place.get("region"), fetch=fetch
            )
            if details:
                place = _merge_place_from_details(place, details)
                coords = _coords_of(details)
            elif not fetch:
                enqueue_place_geocode(
                    kind="city",
                    city=place["city"],
                    region=place.get("region"),
                    country=place["country"],
                )
    elif place.get("city") and place.get("country"):
        details = lookup_city_details(
            place["city"], place["country"], place.get("region"), fetch=fetch
        )
        if details:
            place = _merge_place_from_details(place, details)
            coords = _coords_of(details)
        else:
            # Structured miss → freeform fallback (typos / shorthands).
            try:
                result = forward_geocode_label(
                    place.get("place_label") or label, fetch=fetch
                )
            except Exception:
                result = None
            if result and result.get("country"):
                place = _merge_place_from_details(place, result)
                coords = _coords_of(result)
            elif not fetch:
                enqueue_place_geocode(
                    kind="city",
                    city=place["city"],
                    region=place.get("region"),
                    country=place["country"],
                )
    elif place.get("region") and place.get("country") and not place.get("city"):
        coords = lookup_region_coords(place["region"], place["country"], fetch=fetch)
        if not coords and not fetch:
            enqueue_place_geocode(
                kind="region", region=place["region"], country=place["country"]
            )
    elif place.get("country") and not place.get("city"):
        coords = lookup_country_coords(place["country"], fetch=fetch)
        if not coords and not fetch:
            enqueue_place_geocode(kind="country", country=place["country"])
    elif not place.get("country"):
        try:
            result = forward_geocode_label(label, fetch=fetch)
        except Exception:
            result = None
        if result and result.get("country"):
            venue = place.get("venue")
            city = (place.get("city") or "").strip() or None
            if not city:
                city = (result.get("city") or "").strip() or None
            # When venue was parsed without country, prefer Nominatim locality as city
            # if we only had a loose city hint that matches the venue query.
            if venue and not place.get("city"):
                city = (result.get("city") or "").strip() or city
            country = known_country(result["country"]) or result["country"]
            region = place.get("region") or result.get("region")
            place = {
                "venue": venue,
                "city": city,
                "region": region,
                "country": country,
                "place_label": _format_place(city, region, country, venue=venue)
                or label,
            }
            coords = _coords_of(result)
        elif not fetch:
            enqueue_place_geocode(kind="freeform", label=label)

    place["place_label"] = (
        _format_place(
            place.get("city"),
            place.get("region"),
            place.get("country"),
            venue=place.get("venue"),
        )
        or place.get("place_label")
        or label
    )
    return place, coords


def normalize_freeform_places(conn) -> int:
    """Parse place_label-only rows as City/District, [State,] Country (or country-only)."""
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
        if not parsed.get("country"):
            continue
        if not parsed.get("city") and not parsed.get("region"):
            if known_country(r["place_label"]) is None:
                continue
        conn.execute(
            """
            UPDATE photo_contents
            SET venue = ?, city = ?, region = ?, country = ?, place_label = ?
            WHERE id = ?
            """,
            (
                parsed.get("venue"),
                parsed["city"],
                parsed["region"],
                parsed["country"],
                parsed["place_label"],
                r["id"],
            ),
        )
        n += 1
    return n


def normalize_place_structure(conn) -> int:
    """Re-parse existing comma labels into venue/city/region (idempotent cleanup)."""
    rows = conn.execute(
        """
        SELECT id, place_label, venue, city, region, country
        FROM photo_contents
        WHERE place_label IS NOT NULL AND place_label != ''
          AND instr(place_label, ',') > 0
        """
    ).fetchall()
    n = 0
    for r in rows:
        parsed = parse_city_country(r["place_label"])
        if not parsed.get("country"):
            continue
        new_vals = (
            parsed.get("venue"),
            parsed.get("city"),
            parsed.get("region"),
            parsed.get("country"),
            parsed.get("place_label"),
        )
        old_vals = (
            r["venue"] if "venue" in r.keys() else None,
            r["city"],
            r["region"],
            r["country"],
            r["place_label"],
        )
        if new_vals == old_vals:
            continue
        conn.execute(
            """
            UPDATE photo_contents
            SET venue = ?, city = ?, region = ?, country = ?, place_label = ?
            WHERE id = ?
            """,
            (*new_vals, r["id"]),
        )
        n += 1
    return n


def _region_coords_on_conn(
    conn, region: str, country: str, *, fetch: bool
) -> tuple[float, float] | None:
    """Lookup/cache region centroid using an existing DB connection."""
    key = f"region|{region.lower()}|{country.lower()}"
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
    details = _nominatim_region(region, country)
    if details:
        _cache_upsert(
            conn,
            key,
            city="",
            country=country,
            region=region,
            display_name=details.get("display_name"),
            lat=details["lat"],
            lon=details["lon"],
            found=1,
        )
        return float(details["lat"]), float(details["lon"])
    _cache_upsert(conn, key, city="", country=country, region=region, found=0)
    return None


def normalize_quebec_province(conn) -> int:
    """Rewrite bare Quebec city pins (gps=0) to Quebec province; clear bad cache.

    ``Quebec`` / ``Québec`` means the province unless the city is explicitly
    ``Quebec City``. Real EXIF GPS (gps=1) is left untouched.
    """
    # Drop freeform/city cache entries that resolved bare Quebec to the city.
    conn.execute(
        """
        DELETE FROM place_geo_cache
        WHERE lower(query) IN (
            'freeform|quebec',
            'freeform|québec',
            'quebec||canada',
            'québec||canada'
        )
        """
    )
    coords = _region_coords_on_conn(conn, "Quebec", "Canada", fetch=True)
    place_label = _format_place(None, "Quebec", "Canada")
    n = 0
    rows = conn.execute(
        """
        SELECT id, city, region, country, place_label
        FROM photo_contents
        WHERE IFNULL(gps, 0) = 0
          AND country IS NOT NULL AND country != ''
          AND city IS NOT NULL AND city != ''
        """
    ).fetchall()
    for r in rows:
        if _fold_place(r["country"] or "") != "canada":
            continue
        city_fold = _fold_place(r["city"] or "")
        if city_fold in _REGION_CITY_FORMS or city_fold != "quebec":
            continue
        region_fold = _fold_place(r["region"] or "")
        if region_fold and region_fold != "quebec":
            continue
        if coords:
            conn.execute(
                """
                UPDATE photo_contents
                SET venue = NULL, city = NULL, region = ?, country = ?, place_label = ?,
                    lat = ?, lon = ?, gps = 0
                WHERE id = ?
                """,
                ("Quebec", "Canada", place_label, coords[0], coords[1], r["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE photo_contents
                SET venue = NULL, city = NULL, region = ?, country = ?, place_label = ?,
                    lat = NULL, lon = NULL, gps = 0
                WHERE id = ?
                """,
                ("Quebec", "Canada", place_label, r["id"]),
            )
        n += 1
    # Bare freeform labels still waiting for country.
    for r in conn.execute(
        """
        SELECT id, place_label FROM photo_contents
        WHERE place_label IS NOT NULL AND place_label != ''
          AND (country IS NULL OR country = '')
        """
    ).fetchall():
        parsed = known_region(r["place_label"])
        if not parsed:
            continue
        if coords:
            conn.execute(
                """
                UPDATE photo_contents
                SET venue = NULL, city = NULL, region = ?, country = ?, place_label = ?,
                    lat = ?, lon = ?, gps = 0
                WHERE id = ?
                """,
                (
                    parsed["region"],
                    parsed["country"],
                    parsed["place_label"],
                    coords[0],
                    coords[1],
                    r["id"],
                ),
            )
        else:
            conn.execute(
                """
                UPDATE photo_contents
                SET venue = NULL, city = NULL, region = ?, country = ?, place_label = ?
                WHERE id = ?
                """,
                (
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


def _format_place(
    city: str | None,
    region: str | None,
    country: str | None,
    *,
    venue: str | None = None,
) -> str | None:
    parts = [p for p in (venue, city, region, country) if p]
    # Drop duplicate adjacent names (e.g. city == admin2)
    deduped: list[str] = []
    for p in parts:
        if not deduped or deduped[-1].lower() != p.lower():
            deduped.append(p)
    return ", ".join(deduped) if deduped else None


def empty_place() -> dict[str, Any]:
    return {
        "venue": None,
        "city": None,
        "region": None,
        "country": None,
        "place_label": None,
    }


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
            "venue": None,
            "city": city,
            "region": region,
            "country": country,
            "place_label": _format_place(city, region, country),
        }
    return out


def reverse_one(lat: float, lon: float) -> dict[str, str | None]:
    return reverse_places([(lat, lon)])[0]