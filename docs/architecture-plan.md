# Architecture plan

Fotoman is a **local-first** alternative to PhotoPrism / Immich. The edge is not another cloud library. It is **archiving, organizing, and sharing** while photos stay on disk you already have.

Two machines, four runtimes, one sibling CLI:

| Piece | Where | Job |
|-------|--------|-----|
| **Fotoman server** | Home (`python run.py`) | Index, vault, search, faces, collections, share ACL |
| **Fotoman UI** | Same origin (`fotoman/static/`) | Organizer: cull, collections, map, people, archives |
| **Fotomob server** | Public host (colligator.com or self-host) | Relay only. No photo store, no ML |
| **Fotomob PWA** | Same public origin | Phone gallery + “this phone as an archive” |
| **Reclaimer** | CLI (`reclaimer/`) | Get bytes off Google / Facebook / Instagram onto disk |

```text
  HOME                                              PUBLIC
  ┌─────────────────────────────┐                   ┌──────────────────────────────┐
  │  Fotoman (Python / FastAPI) │  outbound WS      │  Fotomob (Node / Express)    │
  │  SQLite + FAISS + thumbs    │ ───────────────►  │  named providers + devices   │
  │  + desktop UI               │  collections      │  + PWA (gallery / pair)      │
  │  Reclaimer → folder archive │  phone catalog    │  no library copy             │
  └─────────────────────────────┘                   └──────────────────────────────┘
```

Local opens the tunnel. The house never needs inbound ports. Public never holds originals.

---

## What to keep (do not reinvent)

These are the product edges. Do not flatten them into “yet another Immich.”

1. **Content vault** — `photo_contents` (hash → embed / faces / thumb) vs `photos` (locations). Files can move, unplug, or duplicate without re-embedding. See [content-vault-todo.md](content-vault-todo.md).
2. **Collections without moving files** — export is optional. Share is a flag on a collection, not a second library.
3. **Cull is copy-level** — reject a duplicate without forgetting the content.
4. **Phone is an archive, not a sync client** — Fotomob PWA pairs as a `sources.kind = device` location. Fotoman still owns the vault.
5. **Reclaimer stays a CLI** — unzip import in Fotoman is enough UI. Do not fold OAuth / Takeout wizards into the server.
6. **Two stacks on purpose** — Python at home (torch, FAISS, OpenCV). Tiny Node on the VPS. Do not ship Fotoman Python to the public host.

Non-goals for this plan:

- Native mobile apps
- In-app camera
- Feature-parity of Fotoman onto Fotomob
- Immich-style always-on phone backup
- A cloud replica of the library
- React / SPA rewrite of either UI
- Splitting local Fotoman into microservices
- Merging Fotoman and Fotomob into one process

---

## Product decisions

Four forks that would turn Fotomob into a second Immich. Default is **no** unless a later workflow proves otherwise.

### Keep a deliberate functional gap

**Maintain the distinction.** Do not replicate the organizer onto the phone.

| On Fotoman (mutate) | On Fotomob (consume + offer) |
|---------------------|------------------------------|
| Cull, collections, face names, EXIF, archives, export | Shared collections, search, map, people filter, offline cache |
| Job queue, vault, backups | Pair phone as a *location*; pick photos while the page is open |

Viewer overlap that already exists (gallery, lightbox, search, map) is enough. Adding cull / folder edits / face labeling through the tunnel means conflict handling, a much larger PWA, and Fotoman-must-be-online mutation — a worse organizer, not a better share client.

Rule: if a feature changes the library, it lives in Fotoman. If it only *reads a share* or *offers bytes from this device*, it can live in Fotomob.

### In-app camera: no

The OS camera is better than anything a PWA (or a thin native wrapper) will ship: HDR, HEIC, night mode, Photos integration. Fotomob should keep **Choose photos** (and later, if needed, the share-sheet / library picker). A custom viewfinder is reinventing a worse wheel and does not help archiving — the file still has to enter the vault as a device location.

### Native app: no, unless background ingest is the product

The PWA already covers install, offline shares, and pairing. Native pays off only for **background photo-library access** (the current “keep this page open while Fotoman indexes” limit). That is an Immich problem, not an archive/share problem.

Stay PWA. If that keep-open loop becomes a real complaint, wrap the same PWA (Capacitor) rather than rewriting. Do not start a Swift/Kotlin client.

### Auto-archiving: yes at home, no as phone daemon

Two different features:

| Kind | Verdict |
|------|---------|
| Watched folders + remount + backup archives (`sources.kind = backup`, collection export catch-up) | **Yes** — this is the archiving edge. Polish when a workflow needs it (see [content-vault-todo.md](content-vault-todo.md)). |
| Phone silently uploads the camera roll whenever Fotomob can reach Fotoman | **No** — requires native + background, and turns the phone into a sync client. |

Allowed later, still user-initiated: on pair, “new since last catalog” (file picker pre-filtered or a prompt). Not a daemon. Bytes still land in the vault as a device location, not as a second library on the VPS.

### Two-way collection QR: yes, scoped — do not merge with pair

Today there are two QRs that look similar and do different jobs:

| QR | URL | Direction |
|----|-----|-----------|
| Share collection | `/s/{folder-token}` | **Read** that collection and its branches only |
| Pair phone | `/d/{pair}` | **Offer** camera-roll files as a `kind=device` archive |

Each collection has its own token (`collections.fotomob_share_token`). Unshare clears it. A library-wide code must not unlock the provider.

**Do this:** make the *collection* QR an envelope — take photos out, drop photos in — without turning Fotomob into the organizer.

1. **Deep link that collection**, not the whole shared tree. Token lives on the folder (or folder + subtree). Scanning “Vacation” opens Vacation.
2. **Split view vs contribute.** `fotomob_shared` stays read. A separate contribute flag (or a second capability on the same token) opts into “Add photos.” Read-only family albums stay read-only.
3. **Upload is membership, not editing.** File picker → Fotoman pulls bytes (reuse the device fetch path) → vault hash → `collection_members`. No new folders, cull, faces, or EXIF from the phone.
4. **Download is UX, not a new pipe.** Originals already exist (`/original/:id`). Add Save / share-sheet on the album (and maybe a zip of the current folder). Cached thumbs stay for offline viewing.
5. **Keep pair QR.** “This phone is an archive” is a source. “Add to this collection” is a drop on one folder. One code that does both gives every viewer a write path into the house library.

Do **not** grant write on a library-wide share code — there isn’t one for collections anymore. Contribute, if added, stays on the folder token.

This is on-strategy: sharing without a cloud replica. It is not auto-backup and not native camera. It still needs Fotoman online (relay pull), same as pair. Binary frames (Phase 3) matter more once albums move originals both ways.

### Sharing

Indexing stays on the machine that has the photos. Fotomob, in this repo, is the public relay: phones open a link, the relay asks the house for that collection, and bytes pass through without being stored. Self-host it, or point Fotoman at another relay (`fotomob_url`; the default is `https://colligator.com/fotomob`).

Ask AI (VQA) and geocoding run from the house today (your own API keys / Nominatim). A hosted box for prompts, caching, and quota can come later. Do not upload embeddings or originals so a server can index them in the cloud.

### What else to add on a hosted Fotomob

Default Fotomob stays a **relay + QR**. Extra hosted features that justify a DB without becoming a photo cloud:

| Add | Why it is yours | Store on Fotomob |
|-----|-----------------|------------------|
| **Public directory** | Discover providers / public albums without a pasted QR | Small DB: display name, slug, live?, public collection title + share token, cover thumb URL (or one cached cover), last seen |
| **Opt-in listing** | Private shares stay capability URLs; only “List publicly” publishes | Flag from Fotoman when sharing |
| **Boxed VQA / geocode** | Billing, prompts, quota | Keys + cache; stills in flight only |
| **Contribute-to-collection** (later) | Phone drop into a shared folder | Protocol only; bytes still pull through to house |

**Public directory shape (good):** “mini · live · Vacation (120)” → opens that collection’s share link. Bytes still stream from the house when online; offline shows “house asleep” or last-known cover only.

**Do not build on Fotomob:** full gallery cache, search index, faces, embeddings, or a copy of every shared album. That is a second library and kills the open-core story.

`/api/status` already knows who is connected (ephemeral). A directory is that list plus **opt-in metadata** and stable public links — not a re-index of people’s disks.

### Cross-provider / public search

Want: from Fotomob, search across public collections (and maybe several share links), not only one album.

| Approach | Verdict | Why |
|----------|---------|-----|
| **Fan-out to live providers** | **Yes for v1**, tightly limited | Fotomob asks each *relevant* house `search` with that collection’s share token. No library copy. Abuse risk is real (turn every home box into a free search API) — mitigate, don’t skip. |
| **Upload embeddings to Fotomob** | **Only opt-in public**, later | Hosted vectors = a real search index (sticky IP). Also a content leak and a sync job. Never for private shares. |
| **Upload originals / thumbs for search** | **No** | Second photo cloud. |

**Fan-out rules (required):**

1. Search only providers/collections the client is allowed to see (pasted share tokens and/or **public** directory rows).
2. Cap fan-out (e.g. ≤5 live providers, ≤3 public collections each), short timeout, coalesce results on Fotomob.
3. Rate-limit by IP / share token / provider; reject anonymous “search the whole directory” with no auth surface.
4. Public listings only accept search when the house is online; offline → skip or “asleep,” don’t queue forever.

**Vector upload (optional hosted IP later):**

- Opt-in per **public** collection: Fotoman pushes content-id + embedding (+ maybe tiny cover) to Fotomob when listed.
- Fotomob runs ANN over that public set; open album still streams bytes from the house (or fails offline).
- Private / capability-URL shares **never** sync vectors — keep “secret link” meaningful.
- Treat vectors as sensitive: TLS, tenant isolation, delete on unlist, no bulk scrape API.

Do **not** start by “upload the whole library’s FAISS to Fotomob.” That collapses the open-core split. Start with scoped fan-out; add a **public-only** vector index if directory search is slow or houses are often asleep.

---

## Current pain

### 1. Fotomob static layout is wrong

`fotomob/server.js` serves `fotomob/public/`, but the PWA files live next to `server.js` (`index.html`, `app.js`, `styles.css`, `sw.js`, icons). As written, the public gallery has nothing to serve.

**Fix:** move client assets into `fotomob/public/` (do not point Express at `__dirname` — that would also serve `server.js` / `package.json`). Keep `server.js` at the package root.

### 2. Public host is a deploy target, not a second tree

Default `fotomob_url` is `https://colligator.com/fotomob`. That is fine as a hosted relay. The repo must have **one** Fotomob. Do not copy `fotoman/` Python or nested `fotomob/` trees into a `colligator/` folder for deploy. If those copies return, delete them; deploy from `fotomob/` as-is.

### 3. The two clients are different products (keep them split)

| | Fotoman UI | Fotomob PWA |
|--|------------|-------------|
| Size | ~desktop organizer (`app.js` is a large single file) | ~1.5k lines, cache + share codes |
| Job | Mutate the library | Read shared collections; offer the phone as an archive |
| Offline | Not required | IndexedDB + service worker |

Do **not** share a UI kit or merge the HTML. Optional later: a tiny shared helper for lat/lon / orientation if duplication actually hurts. CSS look-alike is enough.

### 4. God files on the local side

| File | Role | Issue |
|------|------|--------|
| `fotoman/api.py` | Every HTTP route | ~1.3k lines, FastAPI `@app.on_event("startup")` (deprecated) |
| `fotoman/indexer.py` | Scan + geocode + embed + job queue | ~2.5k lines, several jobs in one module |
| `fotoman/static/app.js` | Whole organizer | One file, no modules |
| `fotoman/browse.py` | Trees + gallery | DB path **and** live filesystem walks (`_fs_photos_*`, date groups from disk) |

Filesystem browse is useful *before* an archive is indexed. After the vault is current, the FS fallback is a second source of truth.

### 5. Tunnel protocol is implicit

`fotoman/fotomob_link.py` and `fotomob/server.js` share a JSON-over-WS dialect (`hello` / `req` / `res` / `devreq` / `devres`) with methods `tree`, `album`, `people`, `search`, `thumb`, `original`, `device_*`. There is no spec. Thumbs and originals go as **base64 in JSON** (`maxPayload` 80 MB). That is 33% bloat and a hard cap on large originals.

Share auth is one library-wide code in the query string (`?share=`). Collections are the ACL; the code is the capability. Fine for v1; document the threat model (logs, Referer) rather than inventing OAuth.

### 6. Docs and packaging lag the product

- Root `README.md` only covers local Fotoman + Docker + Windows freeze.
- Fotomob has no Dockerfile, no run notes, no reverse-proxy example (`/fotomob` subpath is already in the PWA).
- `packaging/fotoman.spec` lists an outdated hiddenimport set; `collect_submodules("fotoman")` probably saves it, but new modules (`device`, `fotomob_link`, `backup`, …) should be explicit.
- Cache-bust: Fotoman injects `app.js` mtime; Fotomob hard-codes `?v=10` in HTML, CSS, and `sw.js`.

---

## Target layout

```text
fotoman/                 # local server + organizer UI
  api.py                 # app factory + mounts (thin)
  routers/               # HTTP only; keep URL paths stable
  static/                # organizer client (vanilla JS)
fotomob/
  server.js              # relay
  public/                # PWA only
  Dockerfile
reclaimer/               # unchanged sibling
docs/
  architecture-plan.md   # this file
  fotomob-protocol.md    # WS methods (when the tunnel is touched)
tests/                   # local Python
fotomob/test/            # later: protocol fixtures, not a browser suite
```

URLs stay stable: `/api/...` on Fotoman, `/api/...` + `/thumb/:id` + `/ws /provider|/device` on Fotomob.

---

## Phases

### Phase 0 — Cleanup (do first, no behavior change)

1. Move Fotomob assets into `fotomob/public/` so `server.js` matches the tree.
2. Document the four runtimes in the root README (one diagram, how to run Fotomob locally, how Fotoman connects).
3. Make `fotomob_url` obviously overridable; treat colligator.com as *a* relay, not *the* product.
4. One cache-bust scheme for Fotomob (`?v=` derived once, same in HTML + `sw.js` shell list).
5. Shrink committed PNG assets (especially `fotomob.png` / `icon.png`) or generate web-sized copies; keep one source icon.
6. Guardrail: never copy `fotoman/` onto the public host. `.dockerignore` already excludes `fotomob/` from the **Fotoman** image — keep that split.

### Phase 1 — Readable local server (same API)

Split `api.py` with `APIRouter` by area, no path changes:

- `sources` / index / zip import / device pair
- `browse` / search / map
- `collections`
- `faces` / people
- `duplicates` / properties / cull
- `settings` / vqa / events

Replace `@app.on_event("startup")` with a lifespan context.

Leave `indexer.py` as one worker until a concrete bug needs a queue type. If split: `scan` vs `embed` vs `job_queue` — not three processes.

Do **not** split `fotoman/static/app.js` yet. Vanilla JS is the right “don’t reinvent.” Split into ES modules only when a change regularly requires scrolling past unrelated lightbox / archive / map code.

### Phase 2 — Fotomob as a first-class deploy

1. `fotomob/Dockerfile` (Node 18+, `PORT`, no ML).
2. Short reverse-proxy note: `/fotomob` prefix, WebSocket `/provider` and `/device`, `FOTOMOB_URL` / prefs on the home box.
3. Write `docs/fotomob-protocol.md`: ops, methods, share check, device pair. Tests already hit `_handle` in `tests/test_collection_share.py` / `test_device.py` — keep that as the contract, not a mock HTTP layer on the VPS.
4. Add a Fotomob smoke test (Node): share-less `/api/tree` → 503; hello + tree round-trip with a fake provider socket.

### Phase 3 — Tunnel efficiency (when sharing large originals hurts)

Keep JSON for metadata. Switch `thumb` / `original` (and device file pull) to **binary WebSocket frames** or a short-lived HTTP GET the relay streams through. Drop the 80 MB JSON ceiling as the design limit.

Do not add a public object store. The relay may buffer one response; it must not persist the library.

### Phase 4 — One browse source of truth (when it pays)

Today Archives/Dates can walk the filesystem so a new folder is visible before hash/embed. Keep that for **listing**, not for gallery queries once `listed_count` / `photo_count` say the archive is caught up.

- Prefer `PHOTO_SELECT` for photos-in-folder / date / place.
- Keep `_iter_images` for the indexer list pass and “not yet in DB” empty-folder UI.
- Finish vault UX from [content-vault-todo.md](content-vault-todo.md) only when a real workflow needs it (purge UI, offline “last seen”, backup roles). Do not expand the data model speculatively.

---

## Suggested order of work

| Order | Change | Risk | Why |
|-------|--------|------|-----|
| 1 | Fotomob `public/` layout | Low | Relay currently cannot serve the PWA |
| 2 | README: four pieces + run Fotomob | None | Product is undocumented as a pair |
| 3 | FastAPI routers + lifespan | Low | `api.py` is the local choke point |
| 4 | Fotomob Dockerfile + protocol doc | Low | Public deploy is tribal knowledge |
| 5 | Binary image tunnel | Medium | Only if share/originals feel slow or hit 80 MB |
| 6 | Browse DB-first | Medium | Dual FS/DB paths cause “why is this photo missing” bugs |
| 7 | Split organizer `app.js` | Medium | Last; no framework |

Reclaimer and the vault schema stay as they are unless a phase above needs a hook.

---

## Checks that a change is on-strategy

- Photos still live only on the home box (or a paired phone, pulled into the vault).
- Public process stays small: HTTP + WebSocket, no SQLite photo DB, no torch.
- Sharing is collection-scoped; default remains private.
- Organizer UI can still cull, nest collections, and export without Fotomob.
- A second Fotoman instance can connect to the same relay under a different `fotoman_name`.
