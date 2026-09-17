"""In-memory fan-out bus for SSE (bounded, drop-oldest progress)."""

from __future__ import annotations

import queue
import threading
import time


class LiveBus:
    """Stateless fan-out: each subscriber gets its own Queue; publish copies to all.

    首帧快照由 SSE handler 显式写（live.snapshot），bus 只做扇出，不存 replay。
    """

    def __init__(self, maxsize: int = 200):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def has_subscribers(self) -> bool:
        with self._lock:
            return bool(self._subs)

    def publish(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("ts", time.time())
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                if q.full():
                    try:
                        q.get_nowait()  # drop oldest (progress first in practice)
                    except queue.Empty:
                        pass
                q.put_nowait(event)
            except Exception:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass
