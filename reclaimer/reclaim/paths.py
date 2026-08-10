"""Config, output, and job paths."""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".config"
    d = base / "reclaim"
    d.mkdir(parents=True, exist_ok=True)
    return d


def jobs_dir() -> Path:
    d = config_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def client_secrets_path() -> Path:
    return config_dir() / "client_secret.json"


def token_path() -> Path:
    return config_dir() / "token.json"


def reclaim_meta_dir(out: Path) -> Path:
    d = out / ".reclaim"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_watch_dir() -> Path:
    return Path.home() / "Downloads"
