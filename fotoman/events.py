"""Simple in-process pub/sub for SSE push to clients."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from typing import Any

_lock = threading.Lock()
_subscribers: list[queue.Queue] = []
_SHUTDOWN = object()
_closing = threading.Event()

# Keep UI snappy on per-image ticks without flooding the browser.
_THROTTLE_S = 0.1
_KEEPALIVE_S = 30.0
_POLL_S = 1.0
_last_push: dict[str, float] = {}


def publish(event: str, data: dict[str, Any] | None = None, *, force: bool = False) -> None:
    """Push an event to all connected SSE clients (called from worker threads)."""
    now = time.monotonic()
    with _lock:
        if not force:
            last = _last_push.get(event, 0.0)
            if now - last < _THROTTLE_S:
                return
        _last_push[event] = now
        payload = json.dumps({"event": event})
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=64)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def close_all() -> None:
    """Wake every SSE subscriber so streams end (needed for clean Ctrl+C shutdown)."""
    _closing.set()
    with _lock:
        subs = list(_subscribers)
        _subscribers.clear()
    for q in subs:
        try:
            q.put_nowait(_SHUTDOWN)
        except queue.Full:
            pass


async def iter_events(q: queue.Queue):
    """Async generator that yields SSE lines from a thread-safe queue."""
    loop = asyncio.get_running_loop()
    last_ka = time.monotonic()
    try:
        while not _closing.is_set():
            try:
                payload = await loop.run_in_executor(
                    None, lambda: q.get(True, _POLL_S)
                )
            except queue.Empty:
                if _closing.is_set():
                    return
                if time.monotonic() - last_ka >= _KEEPALIVE_S:
                    yield ": keepalive\n\n"
                    last_ka = time.monotonic()
                continue
            if payload is _SHUTDOWN or _closing.is_set():
                return
            yield f"data: {payload}\n\n"
    except asyncio.CancelledError:
        return
