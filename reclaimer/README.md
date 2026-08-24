# Reclaimer (`reclaim`)

Sibling tool to [Fotoman](../). **Primary job: get media off Google / Facebook / Instagram onto disk.** Secondary job: normalize exports into a folder Fotoman can index.

```text
assist/automate getting your photos off Google / Facebook / Instagram → clean folder (+ manifest)
```

## Install

```bash
cd reclaimer
pip install -e .
# or: pip install -e ".[dev]"
```

Requires Python 3.11+. Prefer Fotoman’s `.venv` so deps stay isolated from conda/system packages.

## Quick paths

| Goal | Command |
|------|---------|
| Google Photos → disk | `reclaim google takeout -o ~/reclaimed/google` |
| Already have a Takeout zip | `reclaim normalize ./takeout.zip -o ~/reclaimed/google --source google_takeout` |
| Facebook one-shot | `reclaim facebook fetch -o ~/reclaimed/fb` |
| Instagram one-shot | `reclaim instagram fetch -o ~/reclaimed/ig` |
| Then Fotoman | Add `~/reclaimed/...` as an archive |

There is **no** single “log into Reclaimer.” Export wizards use **your browser** session. The CLI never asks for platform passwords.

---

## Google Photos (Takeout — the real path)

As of **31 March 2025**, Google removed library-wide read access from the Photos Library API (`photoslibrary.readonly` and related scopes). The API now only manages media **uploaded by your own app**. The Photos Picker API is for interactive selection, not bulk reclaim.

**Use Google Takeout** to get your library onto disk:

```bash
reclaim google takeout -o ~/reclaimed/google --wait-hours 2
```

What happens:

1. Prints a checklist (Photos-only export, zip parts, quality).
2. Opens Takeout with Photos pre-hints (`xdg-open`).
3. Watches `~/Downloads` (or `--watch-dir`) until matching zip(s) appear and size stabilizes.
4. Runs normalize into `-o` (multi-part `…-001.zip`, `…-002.zip`, … supported).

If the export is still preparing, exit code **4** — re-run later or check `reclaim status`.

Already downloaded:

```bash
reclaim normalize ./takeout-….zip -o ~/reclaimed/google --source google_takeout
```

No Google Cloud project or OAuth client is required for Takeout.

### `reclaim google pull` (legacy / limited)

`google auth` + `google pull` remain for experiments against the Library API, but **they cannot download your existing Photos library** after Google’s 2025 policy change. Prefer Takeout. If you still try pull and get 403 / empty library, that is expected.

---

## Facebook / Instagram (export wizards)

Official personal full-history APIs are not usable for this. Reclaimer **assists** the official download flows:

```bash
reclaim facebook fetch -o ~/reclaimed/fb
reclaim instagram fetch -o ~/reclaimed/ig
# optional:
reclaim facebook fetch -o ~/reclaimed/fb --watch-dir ~/Downloads --wait-hours 48
```

Same pattern as Takeout: checklist → open DYI URL → watch for zip → normalize.

`reclaim instagram experimental-pull` is intentionally **not** implemented — unofficial scrapers stay off by default.

---

## Normalize / inspect / status

```bash
reclaim inspect ./export.zip
reclaim normalize ./export.zip -o ~/reclaimed/fb --source facebook
reclaim normalize ./Takeout -o ~/reclaimed/google --source google_takeout --layout date
reclaim status
reclaim clear-job JOBID
```

### Output layout

```text
OUT/<source>/…          # albums (default) | date | flat
OUT/.reclaim/manifest.jsonl
OUT/.reclaim/run.json
```

Secrets (if any) live under `~/.config/reclaim/`, never under `OUT/.reclaim/`.

### Dedup

Exact SHA-1 only (`--dedup skip|hardlink|symlink`, default `hardlink`). Cross-device hardlink falls back to skip. Near-dup / faces / search belong in Fotoman.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | OK |
| 1 | Usage / auth error |
| 2 | Partial (some errors) |
| 3 | Fatal I/O |
| 4 | Waiting on external export (pending) |

---

## Security

- Takeout / Facebook / Instagram: no tokens; you authenticate in the browser you already use.
- Legacy Google API path only: refresh token + client secret in `~/.config/reclaim/` (`0600`); revoke at Account permissions + `reclaim google logout`.
- Never put platform passwords into the CLI.

## Fotoman

`OUT` is ordinary media files. Point Fotoman at it as an archive. Ignore `.reclaim/` if desired. Fotoman does not need Reclaimer installed to run.

## Honest limits

| Platform | Reality |
|----------|---------|
| Google Photos | **Takeout** (API can no longer read your full library) |
| Facebook / Instagram | Assist official export only in v1 |
