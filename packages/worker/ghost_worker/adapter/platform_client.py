"""
平台客户端 — 兼容转发层

旧版平台客户端已重构成 adapter/platform/ 通用适配器体系。
本模块保留原类名与接口，向上兼容 drivers/benchmark_driver.py，
内部按配置转发到具体平台后端。

- PlatformClient      = 工厂创建的后端 + 并发保护
- RateLimitedClient   = 请求频率限制包装
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

from .platform import (
    APIError, Challenge, ChallengeNotFound, CloseResult, DuplicateSubmit,
    HintResult, InvalidState, PlatformBackend, ResourceUnavailable,
    StartResult, SubmitResult, TaskNotFound, VpnCheckError, VpnCheckResult,
)
from .platform.factory import create_platform

log = logging.getLogger("adapter.platform_client")


class PlatformClient:
    """
    平台客户端（兼容旧接口）。

    构造时按 ADAPTER_PLATFORM 环境变量选择后端:
      tsecbench-http / tsecbench-sdk / generic
    其余行为与旧版一致。
    """

    MAX_CONCURRENT = 3  # 平台限制同时启动 3 道题

    def __init__(self, base_url: str, token: str, *,
                 timeout: int = 30, mode: Optional[str] = None,
                 spec: Optional[dict] = None):
        if mode is None:
            mode = os.environ.get("ADAPTER_PLATFORM", "") or "tsecbench-http"
        self._backend: PlatformBackend = create_platform(
            base_url, token, mode=mode, timeout=timeout, spec=spec,
        )
        self.base_url = self._backend.base_url
        self.token = token
        self.timeout = timeout

    # ── 转发到后端 ──

    def list_challenges(self) -> list[Challenge]:
        return self._backend.list_challenges()

    def start_challenge(self, unique_code: str) -> StartResult:
        return self._backend.start_challenge(unique_code)

    def get_hint(self, unique_code: str) -> HintResult:
        return self._backend.get_hint(unique_code)

    def submit_flag(self, unique_code: str, flag: str) -> SubmitResult:
        return self._backend.submit_flag(unique_code, flag)

    def close_challenge(self, unique_code: str) -> CloseResult:
        return self._backend.close_challenge(unique_code)

    def check_vpn(self, *, timeout: float = 10.0) -> VpnCheckResult:
        return self._backend.check_vpn(timeout=timeout)

    def health_check(self) -> bool:
        return self._backend.health_check()

    @property
    def backend(self) -> PlatformBackend:
        return self._backend


class RateLimitedClient:
    """带速率限制的客户端封装，防止 API 请求过密"""

    def __init__(self, client: PlatformClient, min_interval: float = 0.5):
        self._client = client
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0

    def _wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last_call
            if delta < self._min_interval:
                time.sleep(self._min_interval - delta)
            self._last_call = time.monotonic()

    def list_challenges(self) -> list[Challenge]:
        self._wait()
        return self._client.list_challenges()

    def start_challenge(self, unique_code: str) -> StartResult:
        self._wait()
        return self._client.start_challenge(unique_code)

    def get_hint(self, unique_code: str) -> HintResult:
        self._wait()
        return self._client.get_hint(unique_code)

    def submit_flag(self, unique_code: str, flag: str) -> SubmitResult:
        self._wait()
        return self._client.submit_flag(unique_code, flag)

    def close_challenge(self, unique_code: str) -> CloseResult:
        self._wait()
        return self._client.close_challenge(unique_code)

    def check_vpn(self, *, timeout: float = 10.0) -> VpnCheckResult:
        return self._client.check_vpn(timeout=timeout)

    def health_check(self) -> bool:
        return self._client.health_check()