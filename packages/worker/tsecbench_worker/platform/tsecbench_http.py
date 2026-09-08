"""
TSecBench 平台 HTTP 适配器 — DEFAULT_SPEC 之上的薄封装

平台 REST API（CHALLENGES_API.md）与 generic_openapi.py 的 DEFAULT_SPEC
完全对齐（认证头 / 路径 / 参数位置 / 错误码映射 / 裸数组列表），
本类只补充差异点:
- VPN 预检地址（spec.vpn_check_url，供 check_vpn 使用）
- 统一发送 json Content-Type 头
"""

from __future__ import annotations

from typing import Optional

from ..config import _env
from .generic_openapi import GenericOpenAPIBackend

_FALLBACK_VPN_CHECK_URL = "http://10.0.100.58"


def default_vpn_check_url() -> str:
    """调用期解析 ADAPTER_VPN_CHECK_URL(无 import/def 期冻结,改 env 即生效)"""
    return _env("ADAPTER_VPN_CHECK_URL", _FALLBACK_VPN_CHECK_URL) or _FALLBACK_VPN_CHECK_URL


class TSecBenchHTTPBackend(GenericOpenAPIBackend):
    """TSecBench 平台 HTTP API 适配器（spec 驱动，不重复实现各端点）。"""

    name = "tsecbench-http"

    def __init__(self, base_url: str, token: str, *,
                 timeout: int = 30,
                 vpn_check_url: Optional[str] = None):
        super().__init__(base_url, token, timeout=timeout,
                         spec={"vpn_check_url": vpn_check_url or default_vpn_check_url()})
        self._session.headers["Content-Type"] = "application/json"
