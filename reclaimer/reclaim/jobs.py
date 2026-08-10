"""Pending watch/fetch job state (resume after export prepares)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from reclaim.paths import jobs_dir


@dataclass
class PendingJob:
    id: str
    platform: str  # facebook | instagram | google_takeout
    output: str
    watch_dir: str
    created_at: float
    updated_at: float
    status: str = "waiting"  # waiting | ready | done | failed
    message: str = ""
    matched_files: list[str] = field(default_factory=list)
    wait_hours: float = 0.0

    @classmethod
    def create(
        cls,
        platform: str,
        output: Path,
        watch_dir: Path,
        wait_hours: float = 0.0,
        message: str = "",
    ) -> "PendingJob":
        now = time.time()
        return cls(
            id=uuid.uuid4().hex[:12],
            platform=platform,
            output=str(output.resolve()),
            watch_dir=str(watch_dir.resolve()),
            created_at=now,
            updated_at=now,
            wait_hours=wait_hours,
            message=message,
        )


def job_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.json"


def save_job(job: PendingJob) -> Path:
    job.updated_at = time.time()
    path = job_path(job.id)
    path.write_text(json.dumps(asdict(job), indent=2) + "\n", encoding="utf-8")
    return path


def load_job(job_id: str) -> PendingJob | None:
    path = job_path(job_id)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return PendingJob(**raw)


def list_jobs() -> list[PendingJob]:
    jobs: list[PendingJob] = []
    for path in sorted(jobs_dir().glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            jobs.append(PendingJob(**raw))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    return jobs


def delete_job(job_id: str) -> bool:
    path = job_path(job_id)
    if path.exists():
        path.unlink()
        return True
    return False


def find_active_for(platform: str, output: Path) -> PendingJob | None:
    out = str(output.resolve())
    for job in list_jobs():
        if job.platform == platform and job.output == out and job.status in (
            "waiting",
            "ready",
        ):
            return job
    return None
