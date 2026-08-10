#!/usr/bin/env python3
from __future__ import annotations

import argparse

import uvicorn
from uvicorn import Config, Server

from fotoman import events
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

    config = Config(
        "fotoman.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # Open SSE (/api/events) otherwise blocks graceful shutdown forever.
        timeout_graceful_shutdown=3,
    )
    server = Server(config)
    _prev_exit = server.handle_exit

    def handle_exit(sig, frame):  # type: ignore[no-untyped-def]
        events.close_all()
        _prev_exit(sig, frame)

    server.handle_exit = handle_exit

    if config.should_reload:
        sock = config.bind_socket()
        from uvicorn.supervisors import ChangeReload

        ChangeReload(config, target=server.run, sockets=[sock]).run()
    else:
        server.run()


if __name__ == "__main__":
    main()
