# Geocoding & places: evaluation

How Place labels are parsed, when reverse vs forward geocoding run, what Nominatim can (and cannot) do, and options to support venues like `Hard Rock Stadium, Miami`.

---

## Glossary

| Term | Meaning |
|------|---------|
| **Reverse geocode** | Coordinates → place name (`lat/lon` → city/region/country) |
| **Forward geocode** | Place name → coordinates (and sometimes structured address parts) |
| **Nominatim** | OpenStreetMap’s public geocoding HTTP API (`nominatim.openstreetmap.org`). Free, attribution required, **max ~1 request/second** on the public instance. |
| **Structured search** | Nominatim params like `city=`, `state=`, `country=` (fields specified separately) |
| **Freeform search** | Nominatim param `q=` (one open string; Nominatim parses it) |
| **Venue** | POI / site that is *not* a city (stadium, hotel, neighborhood nickname, “Helicopter”, “Sky Ventures”) |
| **Gazetteer** | A geographic dictionary / place-name index (city → country, admin1, often lat/lon). Offline datasets like GeoNames or Who’s On First are gazetteers; they resolve common names without calling Nominatim. Fotoman does **not** ship a full gazetteer yet — only small alias tables (countries, US/CA states) plus Nominatim/cache. |

---

## Current state

### Schema & Places tree

Stored fields on `photo_contents`:

- `city`, `region` (state/province), `country`, `place_label`
- `lat` / `lon` / `gps` (`gps=1` = real EXIF; `gps=0` = city/region centroid from forward geocode)

Places sidebar tree is only:

**continent → country → city**

Province/`region` is stored and shown in the label, but is **not** a tree level. There is **no venue** field or child-under-city node.

### Label parse (`parse_city_country`)

Comma-separated rules today:

1. **1 token** — known country → country-only; known region alias (`Quebec`) → province+country; else freeform (Nominatim later).
2. **2 tokens** — `city, country` (e.g. `Montreal, Canada`). Special case: `Quebec, Canada` → province, not city.
3. **3+ tokens** — `first = city`, `middle… = region`, `last = country`.

So:

| Input | city | region | country | Problem |
|-------|------|--------|---------|---------|
| `Miami` | *(freeform)* | — | — | OK path → Nominatim may fill city/region/country |
| `Miami, FL` | Miami | — | *(normalize FL?)* | Country must be a known country name/alias; `FL` is **not** treated as Florida-in-US today unless it maps via country helpers (it won’t). Likely freeform/bad parse. |
| `Miami, US` | Miami | — | United States | OK as city+country; no Florida |
| `Miami, Florida, United States` | Miami | Florida | United States | OK |
| `Hard Rock Stadium, Miami` | Hard Rock Stadium | — | *(Miami as country?)* | Broken: last token must be a country |
| `Hard Rock Stadium, Miami, US` | **Hard Rock Stadium** | Miami | United States | Venue wrongly becomes **city**; Miami becomes “state” |
| `Sky Ventures, Montreal, Canada` | Sky Ventures | Montreal | Canada | Same venue-as-city bug |
| `Sky Ventures, Montreal, Quebec, Canada` | Sky Ventures | Montreal, Quebec | Canada | Still venue-as-city |
| `Helicopter, Hawaii, United States` | Helicopter | Hawaii | United States | Fake city |

**4 CSV “first is venue?”** — Not today. First token is always **city**. Treating 4-part labels as `venue, city, region, country` (and 3-part ambiguous cases carefully) would be a deliberate parse change, plus a new `venue` column (or stuffing venue only into `place_label` while city stays Montreal).

### Reverse geocode (offline)

- Library: `reverse_geocoder` (local dataset, no network).
- When: indexing / Geocoding phase, contents with valid GPS and empty `place_label`.
- Output: `city` ≈ nearest place name, `region` = admin1, `country` from country code, `place_label` = `City, Region, Country`.
- Does **not** run over manual/folder labels that already have a label.
- Coarse: nearest gazetteer point, not a street/POI; no venue names.

### Forward geocode (Nominatim)

Two styles in code:

| Path | How called | What we keep |
|------|------------|--------------|
| Structured | `city` + `country` (+ optional `state`) | **lat/lon only** — response address parts discarded |
| Freeform | `q=…` + `addressdetails=1` | lat/lon + city/region/country when token-match check passes |

Triggers:

1. **Place edit** (`properties.apply_properties`) — structured if parse yields city+country (etc.); freeform only for single-token / no-country labels.
2. **Indexer** — folder-derived labels still missing country/coords.
3. **Map load** — `fill_missing_place_coords(fetch_limit=3)` for pins missing coords (cache first, then up to 3 live Nominatim calls).

Cache: SQLite `place_geo_cache` keyed by query string (`city|region|country`, `freeform|…`, `region|…`, `country|…`). Stores lat/lon + found flag; freeform cache also stores a city/country snapshot (incomplete vs full address JSON).

### What Nominatim actually offers

**Fields vs open input**

- Both are supported. We already use both.
- Structured is better when we *know* roles (`city=Miami&state=Florida&country=United States`).
- Freeform is better for venues and messy strings (`q=Hard Rock Stadium, Miami`).

**Near matches / typos**

- Nominatim is **not** a typo engine. Mild variants sometimes hit; `Maimi` / `Montral` often miss or hit wrong.
- Google Places / Geocoding APIs are stronger at autocomplete, aliases, and fuzzy match (paid, ToS limits on storage).
- We take `limit=1` and do not rank alternatives for the user.

**Structured correction / completion**

- Nominatim **can** return rich `address` (`city`, `town`, `state`, `country_code`, sometimes `tourism`/`amenity` for POIs) when `addressdetails=1`.
- Freeform path **already reads** city/region/country from that.
- Structured city search path **throws away** everything except lat/lon — so `Miami, US` stays `Miami, United States` and never gains `Florida` from the API.
- Google similarly returns structured components (`locality`, `administrative_area_level_1`, `country`, plus place name for POIs). Same opportunity; different license/cost.

**Infer roles from API responses?**

Yes — that’s the right long-term approach for ambiguous CSV:

1. Parse commas as an **ordered hint list**, not hard roles.
2. Forward-geocode (prefer freeform for 2+ ambiguous tokens, or structured trials).
3. Compare Nominatim `address` + `type`/`class` (city vs state vs country vs amenity/tourism) to tokens.
4. Assign: matching country → country; matching state → region; matching city/town → city; leftover leading token that matches POI/`display_name` → **venue**.

Example: `Hard Rock Stadium, Miami` → freeform → amenity/stadium in Miami-Dade, Florida, US → `venue=Hard Rock Stadium`, `city=Miami`, `region=Florida`, `country=United States`.

Heuristic-only “4 CSV = venue first” helps *some* labels but fails on `Miami, Florida, United States` (4 parts, no venue) and on `District, City, Country` without a POI. **API-grounded assignment beats fixed CSV arity.**

### Rate limit: is 1/sec a bottleneck?

On the **public** Nominatim instance, yes for bulk, no for interactive edit:

| Scenario | Impact |
|----------|--------|
| Edit one Place in UI | One lookup; 1/sec irrelevant |
| Map load | Capped at 3 live fetches; rest wait for later loads / cache |
| Folder backfill of N unique labels | ~N seconds serial (lock + sleep) — can stall Geocoding phase |
| Re-geocode whole library | Painful on public API |

**Parallelism against public Nominatim is not allowed** (policy is ~1 req/s). Parallelizing our client would get us blocked. Options that *do* help:

- Rely more on **cache** (already partially done)
- **Background queue** with 1/s worker (UX: don’t block indexer UI; drain gradually)
- Self-hosted Nominatim / Photon / Com Nominal (then raise concurrency)
- Offline forward gazetteer (GeoNames, Who’s On First) for city/country; Nominatim only for leftovers
- Reverse path is already offline and batch-friendly — not the bottleneck

So: **background yes; parallel to public API no.** Parallel only after self-hosting or switching providers that allow it.

---

## Gaps vs desired behaviour

User wants e.g. **Sky Ventures under Montreal**, **Helicopter under Hawaii**, **Hard Rock Stadium under Miami**.

Today:

- No venue field → cannot nest under city in Places.
- CSV arity mis-labels venues as cities.
- Structured Nominatim results are underused for completing province / correcting spelling.
- Album (or folder name) is the only practical “venue” today.

---

## Quick improvements

**Status: implemented** (see `fotoman/geocode.py`, `venue` column, Places tree venue children, `place_geo_queue` worker).

1. **Venue-aware parse** — 4-part → `venue, city, region, country`; 3-part with non-region middle → venue+city; POI-ish first + known region → venue+region (e.g. Helicopter, Hawaii).
2. **`addressdetails=1`** on all Nominatim searches; merge missing region/country into the place.
3. **Freeform fallback** when structured miss or venue/no-country labels (`Hard Rock Stadium, Miami`).
4. **Aliases** — US state abbr/names + Canadian provinces; `Miami, FL` → Florida, United States.
5. **Places tree** — venues nest under city (or under admin region when city empty).
6. **Background worker** — map/cache misses enqueue to `place_geo_queue`; daemon drains at ~1/s.
7. **Richer cache** — `region` + `display_name` on `place_geo_cache`.
8. **Startup re-parse** — `normalize_place_structure` rewrites existing comma labels into venue/city/region.

---

## Elaborate improvements

Larger product / architecture bets.

### A. Resolve-then-assign pipeline

```
raw label
  → tokenize / CSV hints
  → candidate queries (structured + freeform)
  → provider response(s)
  → score role assignment (venue/city/region/country)
  → write fields + centroid (POI coords when OSM point exists)
  → place_label = format(venue, city, region, country)
```

Confidence threshold: below threshold, keep freeform label and flag “needs review” instead of inventing a fake city.

### B. Local gazetteer first, network second

- Ship or download GeoNames / Who’s On First / Geopy datasets for cities, admin1, countries.
- Resolve `Miami`, `Miami, FL`, `Montreal, Canada` with **zero** network calls.
- Use Nominatim/Google only for POIs and misses.
- Makes 1/s irrelevant for the common case; cache becomes a write-through of network hits only.

### C. Smarter providers

| Provider | Strengths | Tradeoffs |
|----------|-----------|-----------|
| Nominatim public | Free, OSM POIs, addressdetails | 1/s, soft fuzzy, soft autocomplete |
| Self-hosted Nominatim / Photon | Higher QPS, same data | Ops cost, disk, updates |
| Google Geocoding / Places | Excellent aliases, typos, stadiums | $$; caching/ToS; API key |
| Mapbox / HERE / Pelias | Similar middle ground | $$ / self-host Pelias |

Hybrid: local gazetteer → Nominatim POI → optional Google on explicit user “Search place…” in UI.

### D. Chatbot / LLM interpretation

Use an LLM only as a **parser/normalizer**, not as the source of truth for coordinates:

- Input: `Helicopter, Hawaii, United States` + optional EXIF country hint.
- Output JSON: `{venue, city, region, country, query_for_geocoder}`.
- Then geocode `query_for_geocoder` or structured fields via Nominatim/Google.
- Good for messy folder names; bad as sole geo oracle (hallucinated places). Always verify with a geocoder + `query_matches_resolved`-style checks.

Batch offline: “normalize all freeform labels” job with local model or API, writing proposed patches for accept/reject.

### E. UX: Place picker

- Autocomplete against cache + gazetteer + (debounced) Nominatim/Google.
- Show structured preview: venue / city / region / country + map pin before save.
- User confirms → no silent mis-parse of Sky Ventures as a city.

### F. Hierarchy & browse

Full tree: `continent → country → [region] → city → venue`.

Filter keys like `venue::Canada::Montreal::Sky Ventures`. Map pins: prefer venue coords when OSM returns a POI point; else city centroid.

### G. Chatbot as librarian (optional)

Natural language: “put these under Montreal as Sky Ventures” → applies venue+city patch to selection. Still backed by the same structured fields.

---

## Recommended direction (pragmatic)

1. **Now:** addressdetails backfill; state-code aliases; freeform fallback when structured fails; background 1/s queue.  
2. **Next:** add `venue` (or equivalent) + parse/API role assignment; Places child under city.  
3. **Later:** local city gazetteer to kill most Nominatim traffic; optional Google/Photon for POI/typo hard cases; Place picker UI.  
4. **LLM:** optional normalizer for folder-quality strings, always followed by geocoder verification — not a replacement for Nominatim/Google.

---

## Direct answers (checklist)

| Question | Answer |
|----------|--------|
| Assume first of 4 CSV is venue? | Reasonable heuristic, but incomplete alone; prefer API/gazetteer role inference. Needs a venue field + tree support to matter in Places. |
| Nominatim: fields or open? | Both; we use structured for city/country pins and freeform for single-token / folder labels. |
| Typos / near matches? | Weak on Nominatim; better on Google; local aliases help known shorthands. |
| Structured fields to fix/complete Place? | Yes from Nominatim/Google; we only fully use them on the freeform path today. |
| Infer interpretation from API? | Yes — best fix for venues vs cities vs states. |
| Is 1/s a bottleneck? | For bulk folder backfill yes; for UI edits no. Parallelize only with self-host/other provider; otherwise background + cache + local gazetteer. |

---

## Example outcomes (target after improvements)

| Input | Target fields |
|-------|----------------|
| `Miami` | city=Miami, region=Florida, country=United States |
| `Miami, FL` | same |
| `Miami, US` | same (region completed from API/gazetteer) |
| `Miami, Florida, United States` | same (no venue) |
| `Hard Rock Stadium, Miami` | venue=Hard Rock Stadium, city=Miami, region=Florida, country=United States |
| `Sky Ventures, Montreal, Canada` | venue=Sky Ventures, city=Montreal, region=Quebec, country=Canada |
| `Helicopter, Hawaii, United States` | venue=Helicopter, city=*(optional)*, region=Hawaii, country=United States — or venue + region-only if no city |
