"""平台后端工厂 — 固定直构 TSecBench HTTP 后端"""

from __future__ import annotations

from .base import PlatformBackend
from .tsecbench_http import TSecBenchHTTPBackend


def create_platform(base_url: str, token: str, *, timeout: int = 30) -> PlatformBackend:
    """创建 TSecBench 平台 HTTP 后端（唯一接入方式）"""
    return TSecBenchHTTPBackend(base_url, token, timeout=timeout)
