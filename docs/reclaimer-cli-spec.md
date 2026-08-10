# Social Reclaimer CLI — Requirements Spec

Sibling tool to Fotoman. **Primary job: help get media off social platforms onto disk.** Secondary job: normalize whatever landed into a folder Fotoman can index as an archive.

**Status:** v1 implemented in [`reclaimer/`](../reclaimer/) (`pip install -e reclaimer`)  
**Stack:** Python 3.11+, Typer  
**Repo:** sibling package under this workspace (`reclaimer/`, not inside `fotoman/`)

**One-liner:** *assist/automate getting your photos off Google / Facebook / Instagram → clean folder (+ manifest).*

---

## 0. Why this exists

Unpacking a Takeout zip is the easy part. The real friction is:

- knowing which export to request and which toggles matter,
- waiting days for archives,
- downloading multi-part zips without losing pieces,
- repeating the dance years later,
- and for Google Photos, pulling an incremental library without a full Takeout.

v1 is valuable only if it **automates or materially assists download**. Normalize + exact dedup are the shared backend once bytes exist — not the headline feature.

---

## 1. Purpose

1. **Pull or shepherd** media from social platforms onto local disk (per-platform reality; see §3).
2. Normalize into a stable layout + manifest.
3. Stay out of library intelligence (search, faces, near-dup, vault) — that is Fotoman.

---

## 2. What Fotoman is vs what Reclaimer is

| Concern | Reclaimer | Fotoman |
|--------|-----------|---------|
| Auth to platforms / drive downloads | Yes | No |
| Guide official “download your data” flows | Yes | No |
| Unpack / normalize exports | Yes (shared backend) | Optional later |
| Exact hash skip in output | Yes (light) | Global exact + near-dup |
| Near-dup / faces / search / vault | No | Yes |

### Dedup

Local exact-hash hygiene only (`skip` | `hardlink` | `symlink`). Near-dup stays in Fotoman.

---

## 3. Platform reality (what “download” can mean)

Official live photo APIs are **not equal**. Design around that; do not pretend one OAuth covers all three.

| Platform | Realistic automate? | How auth works | v1 approach |
|----------|---------------------|----------------|-------------|
| **Google Photos** | **Yes (best path)** | OAuth 2 in browser → refresh token stored under `~/.config/reclaim/` | **API pull** via Google Photos Library API: list + download originals into `-o` |
| **Google Takeout** (full export) | Assist, not fully auto | You stay logged into Google in the **browser**; tool does not hold Takeout passwords | **Wizard:** open Takeout URL with Photos pre-hints, checklist, watch download dir for parts, then normalize |
| **Facebook** | Assist only (API nearly useless for personal full-res history) | Browser on facebook.com for DYI export; no app token required for v1 assist | **Wizard:** open DYI URL, instruct “Photos and videos,” watch `~/Downloads` (or `--watch-dir`) for zip, normalize |
| **Instagram** | Assist official export; optional experimental pull later | Browser for accounts-center data download | **Wizard** same as FB; **experimental** unofficial pullers only behind `reclaim instagram experimental-pull` with loud ToS/breakage warnings — not default |

**There is no single “log into Reclaimer”.** Auth is always platform-native:

- **Google API path:** one-time `reclaim google auth` → browser consent screen → token on disk.
- **Export wizards:** you authenticate in the browser session you already use; the CLI never sees your password.
- **Never** ask for Facebook/Instagram passwords in the CLI.

---

## 4. Goals

### In scope (v1) — download-first

**Google Photos (flagship)**

- [ ] `reclaim google auth` — OAuth device/browser flow; store refresh token locally.
- [ ] `reclaim google pull -o OUT` — download library (images; videos flaggable), resume-friendly, exact-hash skip/hardlink against existing `OUT`.
- [ ] Incremental: skip media IDs / hashes already in manifest.
- [ ] Albums best-effort via API where available.
- [ ] Document Google Cloud project / OAuth client setup (user’s own client id, or shipped desktop client with clear quota limits).

**Export assist (Facebook, Instagram, optional Google Takeout)**

- [ ] `reclaim facebook fetch` / `reclaim instagram fetch` / `reclaim google takeout`
  - print short checklist,
  - `xdg-open` (or print) the correct request URL,
  - `--watch-dir` until expected zip(s) appear (and stabilize),
  - hand off to normalize pipeline into `-o`.
- [ ] Multi-part zip detection (Takeout `01`, `02`, …).
- [ ] State file under `.reclaim/` so a run can resume after “export still preparing — come back tomorrow.”

**Normalize backend (all sources)**

- [ ] `reclaim normalize INPUT -o OUT --source …` (also invoked automatically after pull/watch).
- [ ] Manifest JSONL + `run.json`, layout strategies, `--dedup skip|hardlink|symlink`.
- [ ] `reclaim inspect INPUT`.

### Explicit non-goals (v1)

- Scraping facebook.com / instagram.com HTML as the **default** path.
- Storing platform passwords.
- Near-dup / faces / vault / Fotoman DB.
- Automating SD cards / MTP (other tools).
- Guaranteeing Facebook/IG full-res history via official API (not available for this use case).

### Later

- Fotoman “Import from Google Photos…” calling this tool.
- `--skip-hashes-file` / Fotoman probe-by-hash.
- Hardened experimental IG pull if still needed after official export assist.

---

## 5. Users & scenarios

1. **Google, recurring:** `reclaim google auth` once; periodically `reclaim google pull -o ~/reclaimed/google` — new photos only.
2. **Facebook one-shot:** `reclaim facebook fetch -o ~/reclaimed/fb` → browser → request export → wait → tool notices zip → normalize.
3. **Instagram one-shot:** same wizard pattern.
4. **Already have zips:** `reclaim normalize ./export.zip -o OUT` (easy path still supported; not the product pitch).
5. **Then Fotoman:** add `OUT` as archive; use near-dup vs existing library.

---

## 6. Functional requirements

### 6.1 Commands (illustrative)

```text
reclaim google auth
reclaim google pull -o OUT [--since DATE] [--no-videos] [--dedup hardlink]
reclaim google takeout -o OUT [--watch-dir DIR]   # assist + normalize

reclaim facebook fetch -o OUT [--watch-dir DIR]
reclaim instagram fetch -o OUT [--watch-dir DIR]

reclaim normalize INPUT -o OUT --source google_photos|facebook|instagram
reclaim inspect INPUT
reclaim status                                 # pending watches / last runs
```

### 6.2 Auth storage (Google API only in v1)

- Config dir: `~/.config/reclaim/` (XDG).
- Secrets: refresh token + client config; file mode `0600`.
- `reclaim google logout` deletes tokens.
- No secrets in `OUT/.reclaim/` (output may be shared/backed up).

### 6.3 Watch / assist flow

1. Ensure `-o` and watch dir exist.
2. Open platform export URL; print human checklist (format, media quality, date range).
3. Poll watch dir for new matching archives; wait until size stable.
4. Run normalize into `-o`.
5. If export is “preparing,” persist a pending job and exit non-zero with “re-run `reclaim status` / same fetch command later” — do not hang forever by default (`--wait-hours N` optional).

### 6.4 Output layout / manifest / dedup

Same as before:

- Layout: `albums` (default) | `date` | `flat` under `OUT/<source>/…`
- `.reclaim/manifest.jsonl`, `.reclaim/run.json`
- `--dedup skip|hardlink|symlink` (default `hardlink`, fall back to `skip` on cross-device)
- SHA-1 content hash (align with Fotoman later)
- Never modify inputs; atomic writes into `OUT`

### 6.5 Reporting

```text
google pull: 502 downloaded, 12004 skipped_existing, 3 errors
facebook fetch: watched /home/user/Downloads → posts-…zip
normalize: 8800 extracted, 400 hardlinked_exact
output: /data/reclaimed/facebook
```

Exit codes: `0` ok, `1` usage/auth error, `2` partial, `3` fatal I/O, `4` waiting on external export (pending).

---

## 7. Non-functional

- Resume-safe Google pulls (crash mid-album must not corrupt tree).
- Rate-limit / backoff respectful of Google quotas; clear errors when quota exceeded.
- Dependencies: `typer`, Google auth/photos client libs for pull path; stdlib zip for normalize; no heavy browser automation required in v1 (optional later).
- Linux first.

---

## 8. Integration contract (Fotoman)

1. `OUT` is normal media files; Fotoman adds archive path.
2. `.reclaim/` ignorable by Fotoman.
3. Future: Fotoman subprocess to `reclaim google pull` or “import export…” — Fotoman must not require reclaimer to run.

---

## 9. Documentation

- Google: create OAuth client (or use documented desktop client), `auth`, first `pull`, quota notes.
- Facebook / Instagram: step-by-step export settings that maximize original quality; what the watcher expects.
- Security: what tokens are stored; how to revoke in Google Account + `reclaim google logout`.
- Honest limits: FB/IG cannot be fully API-automated like Google Photos.

---

## 10. Success criteria (v1 done when)

- [ ] Author can `google auth` + `google pull` a real library incrementally onto disk.
- [ ] Author can run `facebook fetch` / `instagram fetch`, complete the browser export, and have the tool pick up the zip and normalize without a separate manual unpack step.
- [ ] `normalize` alone still works on a pre-downloaded Takeout.
- [ ] Re-pull / re-normalize does not duplicate exact bytes (skip or hardlink).
- [ ] README gets someone from “photos stuck on Google/FB/IG” to “folder ready for Fotoman,” with auth only where required (Google API).

---

## 11. Open decisions

| Topic | Recommendation |
|-------|----------------|
| Google OAuth client | User-owned Cloud client (safer for distribution); optional shipped desktop client for personal use |
| Browser automation (Playwright) for Takeout/DYI | **Not v1** — brittle; wizard + watch is enough |
| Experimental IG puller | Separate command, documented risk, off by default |
| Hash | SHA-1 |
| Default dedup | `hardlink` |

---

## 12. Summary

| Feature | Verdict |
|---------|---------|
| **Google Photos API pull + OAuth** | **Yes — core v1** |
| **FB/IG/Takeout fetch wizards + zip watch** | **Yes — core v1** |
| Normalize + manifest + exact dedup | Yes — backend |
| Near-dup / vault | No — Fotoman |
| Default scraping / password auth | No |

Zip-only reclaim was undershooting the need. **Download assist/automation is the product; normalize is how it finishes the job.**
