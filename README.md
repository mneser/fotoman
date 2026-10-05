# Fotoman

Local-first photo library. Photos stay in the folders you already have. Fotoman indexes them in place: describe a photo to search it, then browse by person, place, and date, cull duplicate copies, and group shots into collections without moving files.

Share a collection to a phone with [Fotomob](fotomob/README.md). Your computer connects outward, so the house does not need an open port, and the relay does not keep a copy of the library. [Reclaimer](reclaimer/README.md) pulls Google Photos, Facebook, and Instagram exports onto disk for that index.

MIT licensed.

## Run from source (native Linux / Kubuntu)

Python 3.11+. Folder picker uses `kdialog` on KDE (already on Kubuntu). From the repo root.

**conda**

```bash
conda create -n fotoman python=3.11 -y
conda activate fotoman
conda install pytorch cpuonly -c pytorch
pip install -r requirements.txt
python run.py
```

**venv**

```bash
sudo apt install python3-venv python3-pip
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install torch
python run.py
```

Then open http://127.0.0.1:8000/ in a browser. Do not use Docker for this path; photos stay on the host.

The git tree is the install (code + `requirements.txt`). Runtime files are **not** in the repo:

| | Linux (source) | Docker | Windows (frozen) |
|---|---|---|---|
| **Library** (`FOTOMAN_DATA`) — `config.json`, `fotoman.db`, FAISS, thumbs, reclaimed extracts | `~/.local/share/fotoman` | `/data` | `%LOCALAPPDATA%\Fotoman` |
| **Models** (`FOTOMAN_CACHE`) — face ONNX + SigLIP Hugging Face cache | `~/.cache/fotoman` | `/cache` | `%LOCALAPPDATA%\Fotoman\cache` |

Fingerprints live in the database. First run downloads models into the cache dir; they can be deleted and will be fetched again. Copying `FOTOMAN_DATA` is enough to keep a library. If you still have a repo-root `data/` folder from an older layout, the next start moves it to the library dir (and splits `models/` / `hf-cache/` into the cache dir).

## Docker

Build from the repo root (CPU image; several GB because of torch/transformers):

```bash
docker build -t fotoman .
```

Run with port 8000 forwarded to the host. Persist library data in a volume; bind-mount photo folders you want to index (the container only sees those paths):

```bash
docker run --rm -p 8000:8000 \
  -v fotoman-data:/data \
  -v fotoman-cache:/cache \
  -v "$HOME/Pictures:/photos:ro" \
  fotoman
```

Then open http://127.0.0.1:8000/ and add `/photos` as an archive.

Use another host port if 8000 is taken, e.g. `-p 8080:8000` and open http://127.0.0.1:8080/

First run still downloads SigLIP and face ONNX models into the `/cache` volume.

## Windows standalone (PyInstaller)

Build **on Windows** — the spec does not cross-compile. Use the same Python env you use for Fotoman.

From the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
```

That writes `dist\Fotoman\Fotoman.exe` (onedir — keep the whole `Fotoman` folder; zip or Inno Setup that directory if you want an installer).

- First run still downloads SigLIP and face ONNX models into `%LOCALAPPDATA%\Fotoman\cache` (`FOTOMAN_CACHE`).
- Library data (db, thumbs, config, reclaimed extracts) goes to `%LOCALAPPDATA%\Fotoman`. Override with `FOTOMAN_DATA`.
- The frozen app starts the server and opens the browser. Pass `--no-browser` to skip.
- Expect a multi‑GB folder because of torch/transformers.
