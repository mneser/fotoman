"""Manifest + run metadata under OUT/.reclaim/."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from reclaim.paths import reclaim_meta_dir


@dataclass
class ManifestEntry:
    sha1: str
    relpath: str
    source: str
    media_id: str | None = None
    album: str | None = None
    original_name: str | None = None
    bytes: int | None = None
    action: str | None = None  # copied | hardlinked | symlinked | skipped


@dataclass
class RunStats:
    downloaded: int = 0
    extracted: int = 0
    skipped_existing: int = 0
    hardlinked_exact: int = 0
    symlinked_exact: int = 0
    errors: int = 0
    albums: int = 0

    def merge(self, other: "RunStats") -> None:
        self.downloaded += other.downloaded
        self.extracted += other.extracted
        self.skipped_existing += other.skipped_existing
        self.hardlinked_exact += other.hardlinked_exact
        self.symlinked_exact += other.symlinked_exact
        self.errors += other.errors
        self.albums += other.albums


@dataclass
class RunRecord:
    command: str
    source: str
    started_at: float
    finished_at: float | None = None
    output: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    message: str = ""


class ManifestStore:
    """JSONL manifest + hash/id indexes for incremental runs."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.meta = reclaim_meta_dir(out)
        self.manifest_path = self.meta / "manifest.jsonl"
        self.run_path = self.meta / "run.json"
        self._by_sha1: dict[str, ManifestEntry] = {}
        self._by_media_id: dict[str, ManifestEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.manifest_path.exists():
            return
        with self.manifest_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry = ManifestEntry(
                    sha1=raw["sha1"],
                    relpath=raw["relpath"],
                    source=raw.get("source", ""),
                    media_id=raw.get("media_id"),
                    album=raw.get("album"),
                    original_name=raw.get("original_name"),
                    bytes=raw.get("bytes"),
                    action=raw.get("action"),
                )
                self._index(entry)

    def _index(self, entry: ManifestEntry) -> None:
        self._by_sha1[entry.sha1] = entry
        if entry.media_id:
            self._by_media_id[entry.media_id] = entry

    def has_sha1(self, sha1: str) -> bool:
        return sha1 in self._by_sha1

    def has_media_id(self, media_id: str) -> bool:
        return media_id in self._by_media_id

    def path_for_sha1(self, sha1: str) -> Path | None:
        entry = self._by_sha1.get(sha1)
        if not entry:
            return None
        p = self.out / entry.relpath
        return p if p.exists() else None

    def append(self, entry: ManifestEntry) -> None:
        self._index(entry)
        with self.manifest_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def write_run(self, record: RunRecord) -> None:
        self.run_path.write_text(
            json.dumps(asdict(record), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def entries(self) -> Iterator[ManifestEntry]:
        yield from self._by_sha1.values()

    def count(self) -> int:
        return len(self._by_sha1)


def load_run(out: Path) -> dict[str, Any] | None:
    path = reclaim_meta_dir(out) / "run.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def now() -> float:
    return time.time()
