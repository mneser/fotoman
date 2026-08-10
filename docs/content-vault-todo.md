# Content vault / multi-root TODO

Goal: many watched roots (USB, SD, NAS, backup), heavy embeddings, free file moves
in/out of indexed dirs — without re-embedding when paths change.

Model: **watched roots** (where we scan) ≠ **content vault** (hash → embed / faces / thumb).
Paths are locations; `culled` remains “unwanted copy,” not “forget content.”

---

## Done

- [x] Split `photo_contents` (hash) vs `photos` (locations)
- [x] Hash-link on index (reuse embed by content)
- [x] Soft `culled` / removed for duplicate copies (keep content)
- [x] Incremental people recalc on face label/merge (perf)
- [x] Durable vault on path loss:
  - [x] `photos.missing` instead of delete+GC on scan
  - [x] Revive path clears `missing`; upsert sets `missing=0`
  - [x] Remove archive drops locations only; keep content/FAISS
  - [x] Live browse/search/faces/dupes ignore `missing`
  - [x] `purge_orphaned_contents()` helper (no UI yet)

---

## Crucial — none left for the move/remount story

Vault retention above is enough to protect embeddings when files move or drives unplug.

---

## Defer until a real workflow needs it

- [ ] **Location roles** — `primary` / `backup` / `remembered` (+ `last_seen`) on `photos`
  - Needed when a slow backup drive should share the index as a second copy with clear UX
- [ ] **Probe-by-hash API** — hash external file/folder → vault hit or enqueue embed
  - Convenience; watched root + scan still works
- [ ] **Offline UX** — “only on backup / last seen at …” when search hits content with no live path
- [ ] **Purge UI** — wire explicit purge of orphan content / “forget this photo”
  - Do when vault/thumb/FAISS growth becomes noticeable
- [ ] **Source kinds** — watched vs backup vs remembered (not always continuously walked)

---

## Optional / polish

- [ ] Gate or defer auto-recluster on lightbox close after naming unlabeled faces
- [ ] Soft-mark all locations missing when an entire root is unmounted (today: reconcile on remount)
- [ ] Drop redundant missing locations once another live location exists for the same content under the same source

---

## Non-goals / keep

- Keep explicit **roots** (do not unbounded whole-disk scan)
- Keep **`culled`** as copy-level judgment, separate from missing/offline/vault retention
- Do not GC embeddings on scan or archive remove; only on intentional purge
