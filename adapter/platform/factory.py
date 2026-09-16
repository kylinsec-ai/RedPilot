"""
平台后端工厂 — 按配置选择适配器

ADAPTER_PLATFORM:
- tsecbench-http  TSecBench 直接 HTTP（默认，无依赖）
- tsecbench-sdk   tsec-benchmark 官方 SDK（需 pip install tsec-benchmark）
- generic         通用 OpenAPI 适配器（PLATFORM_SPEC_FILE 描述平台差异）
"""

from __future__ import annotations

import logging
from typing import Optional

from .base import PlatformBackend
from .generic_openapi import GenericOpenAPIBackend
from .tsecbench_http import TSecBenchHTTPBackend
from .tsecbench_sdk import TSecBenchSDKBackend, sdk_available

log = logging.getLogger("adapter.platform.factory")

BACKENDS = {
    "tsecbench-http": TSecBenchHTTPBackend,
    "tsecbench-sdk": TSecBenchSDKBackend,
    "generic": GenericOpenAPIBackend,
}


def create_platform(base_url: str, token: str, *,
                    mode: Optional[str] = None,
                    timeout: int = 30,
                    spec: Optional[dict] = None) -> PlatformBackend:
    """
    创建平台后端实例。
    mode 为空时按可用性自动选择: sdk(已安装) > http(默认)
    """
    mode = (mode or "tsecbench-http").lower()

    if mode == "tsecbench-sdk" and not sdk_available():
        log.warning("tsec-benchmark SDK not installed, falling back to HTTP backend")
        mode = "tsecbench-http"

    if mode not in BACKENDS:
        log.warning("unknown platform mode %r, using tsecbench-http", mode)
        mode = "tsecbench-http"

    backend = BACKENDS[mode]
    log.info("platform backend: %s (%s)", backend.name, mode)

    if mode == "generic":
        return GenericOpenAPIBackend(base_url, token, timeout=timeout, spec=spec)
    return backend(base_url, token, timeout=timeout)