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

Requires Python 3.11+.

## Quick paths

| Goal | Command |
|------|---------|
| Recurring Google library sync | `reclaim google auth` once, then `reclaim google pull -o ~/reclaimed/google` |
| Facebook one-shot | `reclaim facebook fetch -o ~/reclaimed/fb` |
| Instagram one-shot | `reclaim instagram fetch -o ~/reclaimed/ig` |
| Already have a Takeout zip | `reclaim normalize ./takeout.zip -o ~/reclaimed/google --source google_takeout` |
| Then Fotoman | Add `~/reclaimed/...` as an archive |

There is **no** single “log into Reclaimer.” Google API uses OAuth; Facebook/Instagram use **your browser** session. The CLI never asks for FB/IG passwords.

---

## Google Photos API (flagship)

### 1. Create an OAuth client (your GCP project)

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project (or pick one).
3. Enable **Photos Library API**.
4. Configure OAuth consent screen (External is fine for personal use; add yourself as a test user).
5. Credentials → Create credentials → **OAuth client ID** → Application type **Desktop app**.
6. Download the JSON and save it as:

```text
~/.config/reclaim/client_secret.json
```

(or pass `--client-secrets PATH` to `reclaim google auth`).

### 2. Authenticate

```bash
reclaim google auth
```

Browser consent → refresh token stored at `~/.config/reclaim/token.json` (mode `0600`).

```bash
reclaim google logout   # delete local token
# Also revoke at https://myaccount.google.com/permissions
```

### 3. Pull

```bash
reclaim google pull -o ~/reclaimed/google
reclaim google pull -o ~/reclaimed/google --since 2024-01-01 --no-videos
reclaim google pull -o ~/reclaimed/google --dedup hardlink   # default
```

- Resume-friendly: media IDs and SHA-1 hashes already in `.reclaim/manifest.jsonl` are skipped.
- Albums are mapped best-effort via the API.
- Rate limits: the client backs off on 429/5xx. If quota is exhausted, wait and retry.

**API access note:** Google has tightened Photos Library API access over time. You must use **your own** OAuth client with the API enabled. If you get 403, check API enablement, consent screen test users, and whether Google still grants library read for your project type.

### Takeout assist (full export)

Prefer API pull for recurring sync. For a full Takeout:

```bash
reclaim google takeout -o ~/reclaimed/google --wait-hours 2
```

Opens Takeout with Photos hints, watches `~/Downloads` (or `--watch-dir`) for multi-part zips, then normalizes.

---

## Facebook / Instagram (export wizards)

Official personal full-history APIs are not usable for this. Reclaimer **assists** the official download flows:

```bash
reclaim facebook fetch -o ~/reclaimed/fb
reclaim instagram fetch -o ~/reclaimed/ig
# optional:
reclaim facebook fetch -o ~/reclaimed/fb --watch-dir ~/Downloads --wait-hours 48
```

What happens:

1. Prints a short checklist (format ZIP, high/original quality, Photos and videos).
2. Opens the DYI / Accounts Center URL (`xdg-open`).
3. Watches the download dir until matching zip(s) appear and size stabilizes.
4. Runs normalize into `-o`.

If the export is still preparing, the tool exits with code **4**, saves a pending job, and you re-run the same command later (`reclaim status`).

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

Secrets are **never** written under `OUT/.reclaim/` (only under `~/.config/reclaim/`).

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

- Google: refresh token + client secret live in `~/.config/reclaim/` with mode `0600`.
- Facebook / Instagram: no tokens; you authenticate in the browser you already use.
- Never put platform passwords into the CLI.
- Revoke Google access via Account permissions + `reclaim google logout`.

## Fotoman

`OUT` is ordinary media files. Point Fotoman at it as an archive. Ignore `.reclaim/` if desired. Fotoman does not need Reclaimer installed to run.

## Honest limits

| Platform | Reality |
|----------|---------|
| Google Photos | Best automation path via API |
| Google Takeout | Assist + watch + normalize |
| Facebook / Instagram | Assist official export only in v1 |
