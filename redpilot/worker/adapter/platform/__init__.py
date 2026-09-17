"""
平台接入层 — 通用化平台适配器

结构:
- base.py            统一数据模型 / 异常 / PlatformBackend 抽象接口
- tsecbench_http.py  TSecBench 平台 HTTP 适配器（无第三方依赖）
- tsecbench_sdk.py   tsec-benchmark 官方 SDK 适配器（可选安装）
- generic_openapi.py 通用 OpenAPI 适配器（JSON spec 描述平台差异）
- factory.py         按 ADAPTER_PLATFORM 选择后端

上层只依赖 base 中的模型与接口，新增平台只需实现 PlatformBackend。
"""

from .base import (
    APIError, Challenge, ChallengeNotFound, CloseResult, DuplicateSubmit,
    HintResult, InvalidState, PlatformBackend, ResourceUnavailable,
    StartResult, SubmitResult, TaskNotFound, VpnCheckError, VpnCheckResult,
)
from .tsecbench_http import TSecBenchHTTPBackend
from .tsecbench_sdk import TSecBenchSDKBackend, sdk_available
from .generic_openapi import GenericOpenAPIBackend
from .factory import create_platform

__all__ = [
    # 模型
    "Challenge", "StartResult", "SubmitResult", "HintResult",
    "CloseResult", "VpnCheckResult",
    # 异常
    "APIError", "TaskNotFound", "ChallengeNotFound", "InvalidState",
    "DuplicateSubmit", "ResourceUnavailable", "VpnCheckError",
    # 接口
    "PlatformBackend",
    # 适配器
    "TSecBenchHTTPBackend", "TSecBenchSDKBackend", "sdk_available",
    "GenericOpenAPIBackend",
    # 工厂
    "create_platform",
]