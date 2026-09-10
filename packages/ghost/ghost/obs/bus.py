"""进程内 asyncio 订阅/发布;同一事件循环使用(摄取端与 SSE 生成器都在本进程)。

无 replay、无持久化 —— SSE 首帧由 handler 从 store 取最新快照;此后每帧 =
一次 live POST 的 {**snapshot, kind, ts}。订阅者队列有界(maxsize,丢最老):
慢订阅者绝不阻塞发布(与 worker bus.py 同策略)。
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("obs.bus")


class LiveBus:
    def __init__(self, maxsize: int = 200):
        self._subs: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    async def publish(self, event: dict) -> None:
        ev = dict(event)  # 拷贝:发布后调用方可继续改
        ev.setdefault("ts", time.time())
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # 满则丢最老(progress 类先丢),绝不阻塞发布
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)
