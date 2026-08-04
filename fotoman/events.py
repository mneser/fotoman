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

# Keep UI snappy on per-image ticks without flooding the browser.
_THROTTLE_S = 0.1
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


async def iter_events(q: queue.Queue):
    """Async generator that yields SSE lines from a thread-safe queue."""
    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                payload = await asyncio.wait_for(
                    loop.run_in_executor(None, q.get, True, 30), timeout=35
                )
                yield f"data: {payload}\n\n"
            except (asyncio.TimeoutError, Exception):
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        return
