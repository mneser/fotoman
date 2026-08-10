# Archive banners & job queueing

## Toggle behaviour

Embed and Faces toggles fire the POST, save the returned status, and call `loadSources()`. No polling loop.

| Toggle | Enable | Disable |
|--------|--------|---------|
| **Embed** | `POST /api/sources/:id/embed_enabled?enabled=true` → save status → `loadSources()` | same with `=false` |
| **Faces** | `POST /api/sources/:id/face_enabled?enabled=true` → save status → `loadSources()` | same with `=false` |

## Stage chips

Always shown in the progress header, in order:
- **Indexing / Geocoding / Fingerprinting**: live `total - current` while that phase is running
- **Facematch**: sum of `pending_faces_count` across sources with `face_enabled=1` (always visible; before Embedding)
- **Embedding**: sum of `pending_embed_count` across sources with `embed_enabled=1`

Zero remaining → count hidden. The archive row currently being processed gets an `is-active` highlight.

## Per-archive count line

Under each archive path, e.g. `8099 listed · indexed ✓ · face-scanned ✓ · 4936 embedded`:

- **listed**: file count from the last list pass (omitted while still 0)
- **indexed / face-scanned / embedded**: checkmark when caught up; otherwise the live count in accent (orange)
  - indexed done when `photo_count >= listed_count`
  - face-scanned done when `pending_faces_count == 0` (and photos exist)
  - embedded done when `pending_embed_count == 0` (and photos exist)

## Live updates (SSE)

`GET /api/events` — Server-Sent Events stream. Backend publishes on every progress tick (indexer file updates, face progress, phase changes). Frontend `EventSource` refreshes index status + faces status + source counts on each event (patches rows in place; full list rebuild only if archives are added/removed). No polling loops anywhere.

Throttled to ≤1 push per 100 ms per event type to avoid flooding; phase / source changes force an immediate push.
