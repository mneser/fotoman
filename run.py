#!/usr/bin/env python3
from __future__ import annotations

import argparse

import uvicorn

from fotoman.config import load_settings
from fotoman.db import init_db


def main() -> None:
    settings = load_settings()
    init_db()
    parser = argparse.ArgumentParser(description="Fotoman — local photo search")
    parser.add_argument("--host", default=settings.get("host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(settings.get("port", 8000)))
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("fotoman.api:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()