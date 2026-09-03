"""
TSecBench 平台 HTTP 适配器 — DEFAULT_SPEC 之上的薄封装

平台 REST API（CHALLENGES_API.md）与 generic_openapi.py 的 DEFAULT_SPEC
完全对齐（认证头 / 路径 / 参数位置 / 错误码映射 / 裸数组列表），
本类只补充差异点:
- VPN 预检地址（spec.vpn_check_url，供 check_vpn 使用）
- 统一发送 json Content-Type 头
"""

from __future__ import annotations

from .generic_openapi import GenericOpenAPIBackend

DEFAULT_VPN_CHECK_URL = "http://10.0.100.58"


class TSecBenchHTTPBackend(GenericOpenAPIBackend):
    """TSecBench 平台 HTTP API 适配器（spec 驱动，不重复实现各端点）。"""

    name = "tsecbench-http"

    def __init__(self, base_url: str, token: str, *,
                 timeout: int = 30,
                 vpn_check_url: str = DEFAULT_VPN_CHECK_URL):
        super().__init__(base_url, token, timeout=timeout,
                         spec={"vpn_check_url": vpn_check_url})
        self._session.headers["Content-Type"] = "application/json"
