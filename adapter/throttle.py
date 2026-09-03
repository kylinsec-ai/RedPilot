"""请求节流 — 线程安全的最小间隔门控

平台 API（RateLimitedClient）与 LLM 客户端共用同一限速语义：
同一实例上的任意两次调用间隔不小于 min_interval（<=0 表示不限速）。
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """线程安全的请求间隔限速器"""

    def __init__(self, min_interval: float = 0.0):
        self._min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        """阻塞直至距上次调用 >= min_interval"""
        interval = self._min_interval
        if interval <= 0:
            return
        with self._lock:
            delta = time.monotonic() - self._last_call
            if delta < interval:
                time.sleep(interval - delta)
            self._last_call = time.monotonic()
