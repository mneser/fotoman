#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from uvicorn import Config, Server

from fotoman import events
from fotoman.config import load_settings
from fotoman.db import init_db


def _pick_dialog_and_exit(*, directory: bool) -> None:
    import tkinter as tk
    from tkinter import filedialog

    flag = "--pick-folder" if directory else "--pick-file"
    title = "Select photo volume" if directory else "Select export zip"
    argv = sys.argv
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            title = argv[i + 1]
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if directory:
        path = filedialog.askdirectory(title=title) or ""
    else:
        path = (
            filedialog.askopenfilename(
                title=title,
                filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")],
            )
            or ""
        )
    print(path)
    root.destroy()
    raise SystemExit(0)


def main() -> None:
    if "--pick-folder" in sys.argv:
        _pick_dialog_and_exit(directory=True)
    if "--pick-file" in sys.argv:
        _pick_dialog_and_exit(directory=False)

    frozen = bool(getattr(sys, "frozen", False))
    settings = load_settings()
    init_db()
    parser = argparse.ArgumentParser(description="Fotoman — local photo search")
    parser.add_argument("--host", default=settings.get("host", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(settings.get("port", 8000)))
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if frozen:
        from fotoman.api import app

        app_target = app
        reload = False
    else:
        app_target = "fotoman.api:app"
        reload = args.reload

    config = Config(
        app_target,
        host=args.host,
        port=args.port,
        reload=reload,
        # Open SSE (/api/events) otherwise blocks graceful shutdown forever.
        timeout_graceful_shutdown=3,
    )
    server = Server(config)
    _prev_exit = server.handle_exit

    def handle_exit(sig, frame):  # type: ignore[no-untyped-def]
        events.close_all()
        _prev_exit(sig, frame)

    server.handle_exit = handle_exit

    if frozen and not args.no_browser:
        url = f"http://{args.host}:{args.port}/"

        def _open() -> None:
            time.sleep(1.2)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    if config.should_reload:
        sock = config.bind_socket()
        from uvicorn.supervisors import ChangeReload

        ChangeReload(config, target=server.run, sockets=[sock]).run()
    else:
        server.run()


if __name__ == "__main__":
    main()
