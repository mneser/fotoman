"""Country aliases, Place parse, and country-centroid fallback."""

from fotoman.db import db, init_db
from fotoman.geocode import (
    known_country,
    lookup_country_coords,
    parse_city_country,
    query_matches_resolved,
    resolve_manual_place,
)


def test_uae_alias_parses_like_usa():
    assert known_country("UAE") == "United Arab Emirates"
    assert known_country("USA") == "United States"
    uae = parse_city_country("UAE")
    usa = parse_city_country("USA")
    assert uae["country"] == "United Arab Emirates"
    assert uae["place_label"] == "United Arab Emirates"
    assert usa["country"] == "United States"
    assert usa["place_label"] == "United States"


def test_dubai_uae_expands_country():
    parsed = parse_city_country("Dubai, UAE")
    assert parsed["city"] == "Dubai"
    assert parsed["country"] == "United Arab Emirates"
    assert parsed["place_label"] == "Dubai, United Arab Emirates"


def test_hong_kong_is_a_country():
    assert known_country("Hong Kong") == "Hong Kong"
    assert known_country("HK") == "Hong Kong"
    assert known_country("Hongkong") == "Hong Kong"
    parsed = parse_city_country("Hong Kong")
    assert parsed["country"] == "Hong Kong"
    assert parsed["city"] is None
    assert parsed["place_label"] == "Hong Kong"


def test_uae_alias_matches_resolved_name():
    assert query_matches_resolved("UAE", "United Arab Emirates")
    assert query_matches_resolved("USA", "United States")


def test_venue_plus_province_stays_without_city():
    parsed = parse_city_country("Buller Pass, Alberta, Canada")
    assert parsed["venue"] == "Buller Pass"
    assert parsed["city"] is None
    assert parsed["region"] == "Alberta"
    assert parsed["country"] == "Canada"
    assert parsed["place_label"] == "Buller Pass, Alberta, Canada"


def test_resolve_does_not_inject_nominatim_city(monkeypatch):
    monkeypatch.setattr(
        "fotoman.geocode.forward_geocode_label",
        lambda *_a, **_k: {
            "lat": 50.89,
            "lon": -115.32,
            "city": "Kananaskis Improvement District",
            "region": "Alberta",
            "country": "Canada",
        },
    )
    place, coords = resolve_manual_place("Buller Pass, Alberta, Canada", fetch=True)
    assert place["venue"] == "Buller Pass"
    assert place["city"] is None
    assert place["region"] == "Alberta"
    assert place["country"] == "Canada"
    assert place["place_label"] == "Buller Pass, Alberta, Canada"
    assert coords == (50.89, -115.32)


def test_resolve_still_fills_missing_region(monkeypatch):
    monkeypatch.setattr(
        "fotoman.geocode.forward_geocode_label",
        lambda *_a, **_k: {
            "lat": 45.50,
            "lon": -73.57,
            "city": "Montreal",
            "region": "Quebec",
            "country": "Canada",
        },
    )
    place, _coords = resolve_manual_place("Sky Ventures, Montreal, Canada", fetch=True)
    assert place["venue"] == "Sky Ventures"
    assert place["city"] == "Montreal"
    assert place["region"] == "Quebec"
    assert place["place_label"] == "Sky Ventures, Montreal, Quebec, Canada"


def test_resolve_uae_writes_canonical_label(monkeypatch):
    monkeypatch.setattr(
        "fotoman.geocode.lookup_country_coords", lambda *a, **k: (24.0, 54.0)
    )
    place, coords = resolve_manual_place("UAE", fetch=True)
    assert place["country"] == "United Arab Emirates"
    assert place["place_label"] == "United Arab Emirates"
    assert coords == (24.0, 54.0)


def test_country_lookup_falls_back_to_freeform(monkeypatch):
    init_db()
    with db() as conn:
        conn.execute("DELETE FROM place_geo_cache")
    monkeypatch.setattr("fotoman.geocode._nominatim_country", lambda *_: None)
    monkeypatch.setattr(
        "fotoman.geocode._nominatim_freeform",
        lambda *_: {
            "lat": 22.35,
            "lon": 114.19,
            "city": "",
            "region": None,
            "country": "Hong Kong",
            "display_name": "Hong Kong, China",
        },
    )
    coords = lookup_country_coords("Hong Kong", fetch=True)
    assert coords == (22.35, 114.19)


def test_android_folder_is_not_a_place():
    from fotoman.metadata import place_from_dirname, place_from_rel

    assert place_from_dirname("Android") is None
    assert place_from_dirname("DCIM") is None
    assert place_from_dirname("Camera") is None
    assert place_from_rel("Android/verdun/shot.jpg") == "verdun"


def test_extract_exif_reads_written_gps(tmp_path):
    from PIL import Image

    from fotoman.metadata import extract_exif, write_gps

    path = tmp_path / "geo.jpg"
    Image.new("RGB", (16, 16), (8, 16, 24)).save(path, "JPEG")
    write_gps(path, 33.8833, 35.5)
    meta = extract_exif(path)
    assert meta["lat"] is not None and meta["lon"] is not None
    assert abs(meta["lat"] - 33.8833) < 0.001
    assert abs(meta["lon"] - 35.5) < 0.001
