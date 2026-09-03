"""
平台接入层 — TSecBench HTTP 适配器

结构:
- base.py            统一数据模型 / 异常 / PlatformBackend 抽象接口
- generic_openapi.py spec 驱动的通用引擎（TSecBenchHTTPBackend 的父类）
- tsecbench_http.py  TSecBench 平台 HTTP 适配器（唯一实例化后端）
- factory.py         固定直构 TSecBenchHTTPBackend

上层只依赖 base 中的模型与接口。
"""

from .base import (
    APIError, Challenge, ChallengeNotFound, CloseResult, DuplicateSubmit,
    HintResult, InvalidState, PlatformBackend, ResourceUnavailable,
    StartResult, SubmitResult, TaskNotFound, VpnCheckError, VpnCheckResult,
)
from .tsecbench_http import TSecBenchHTTPBackend
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
    # 适配器与工厂
    "TSecBenchHTTPBackend",
    "create_platform",
]
