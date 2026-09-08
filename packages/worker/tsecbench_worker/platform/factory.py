"""平台后端工厂 — 固定直构 TSecBench HTTP 后端"""

from __future__ import annotations

from typing import Optional

from .base import PlatformBackend
from .tsecbench_http import TSecBenchHTTPBackend, default_vpn_check_url


def create_platform(base_url: str, token: str, *,
                    timeout: int = 30,
                    vpn_check_url: Optional[str] = None) -> PlatformBackend:
    """创建 TSecBench 平台 HTTP 后端（唯一接入方式）"""
    return TSecBenchHTTPBackend(base_url, token, timeout=timeout,
                                vpn_check_url=vpn_check_url or default_vpn_check_url())
