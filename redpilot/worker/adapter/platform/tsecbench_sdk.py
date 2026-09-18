"""
tsec-benchmark 官方 SDK 适配器

将官方 SDK 的异常与数据类映射为统一模型。
SDK 仅在显式配置 ADAPTER_PLATFORM=tsecbench-sdk 且已安装时使用。
"""

from __future__ import annotations

import logging

from .base import (
    APIError, Challenge, ChallengeNotFound, CloseResult, DuplicateSubmit,
    HintResult, InvalidState, PlatformBackend, ResourceUnavailable,
    StartResult, SubmitResult, TaskNotFound, VpnCheckError, VpnCheckResult,
    _parse_files,
)

log = logging.getLogger("adapter.platform.sdk")

try:
    import tsec_benchmark as sdk
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

# 平台侧自测服务器 tsecbench/（顶层包，仓库根）**不在镜像里**（.dockerignore 排除），
# 静态 import 会在 worker 镜像里 ModuleNotFoundError —— 所以这里不 import 它。
# 本适配器只依赖官方 SDK（上面那个 try/except）。


def sdk_available() -> bool:
    """官方 SDK 是否已安装"""
    return _SDK_AVAILABLE


class TSecBenchSDKBackend(PlatformBackend):
    """基于官方 tsec-benchmark SDK 的适配器"""

    name = "tsecbench-sdk"

    def __init__(self, base_url: str, token: str, *, timeout: int = 30):
        if not _SDK_AVAILABLE:
            raise RuntimeError(
                "tsec-benchmark SDK not installed — run `pip install tsec-benchmark` "
                "or switch ADAPTER_PLATFORM=tsecbench-http"
            )
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        """延迟创建 SDK 客户端"""
        if self._client is None:
            self._client = sdk.TSecBenchmark(
                base_url=self.base_url, token=self.token
            )
        return self._client

    @staticmethod
    def _map_error(e: Exception) -> APIError:
        """将 SDK 异常映射为统一异常"""
        if isinstance(e, sdk.VpnCheckError):
            return VpnCheckError(str(e), reason=getattr(e, "detail", {}).get("reason", "network_error"))
        if isinstance(e, sdk.TaskNotFound):
            return TaskNotFound(code="task_not_found", message=str(e), status=404)
        if isinstance(e, sdk.ChallengeNotFound):
            return ChallengeNotFound(code="challenge_not_found", message=str(e), status=404)
        if isinstance(e, sdk.InvalidState):
            return InvalidState(code="invalid_state", message=str(e), status=409)
        if isinstance(e, sdk.DuplicateSubmit):
            return DuplicateSubmit(code="duplicate", message=str(e), status=409)
        if isinstance(e, sdk.ResourceUnavailable):
            return ResourceUnavailable(code="resource_unavailable", message=str(e), status=503)
        if isinstance(e, sdk.TSecError):
            return APIError(
                code=getattr(e, "code", "unknown"),
                message=str(e),
                status=getattr(e, "status_code", 0),
                detail=getattr(e, "detail", None),
            )
        return e  # 非 SDK 异常原样抛出

    # ── 接口实现 ──

    def list_challenges(self) -> list[Challenge]:
        try:
            items = self._get_client().list_challenges()
        except Exception as e:
            raise self._map_error(e) from e
        return [
            Challenge(
                unique_code=c.unique_code,
                description=getattr(c, "description", "") or "",
                difficulty=getattr(c, "difficulty", "") or "",
                level=int(getattr(c, "level", 1) or 1),
                total_score=int(getattr(c, "total_score", 0) or 0),
                flag_count=int(getattr(c, "flag_count", 1) or 1),
                correct_flag_count=int(getattr(c, "correct_flag_count", 0) or 0),
                is_completed=bool(getattr(c, "is_completed", False)),
                container_status=getattr(c, "container_status", "stopped"),
                container_addr=list(getattr(c, "container_addr", []) or []),
                category=getattr(c, "category", "") or getattr(c, "type", "") or "",
                tags=list(getattr(c, "tags", []) or []),
                files=_parse_files(getattr(c, "files", None)),
            )
            for c in items
        ]

    def start_challenge(self, unique_code: str) -> StartResult:
        try:
            r = self._get_client().start_challenge(unique_code)
        except Exception as e:
            raise self._map_error(e) from e
        return StartResult(
            unique_code=getattr(r, "unique_code", unique_code),
            container_addr=list(getattr(r, "container_addr", []) or []),
        )

    def get_hint(self, unique_code: str) -> HintResult:
        try:
            r = self._get_client().get_hint(unique_code)
        except Exception as e:
            raise self._map_error(e) from e
        return HintResult(
            unique_code=getattr(r, "unique_code", unique_code),
            hint=getattr(r, "hint", None),
        )

    def submit_flag(self, unique_code: str, flag: str) -> SubmitResult:
        try:
            r = self._get_client().submit_flag(unique_code, flag)
        except sdk.DuplicateSubmit as e:
            return SubmitResult(correct=False, duplicate=True, error="duplicate")
        except Exception as e:
            raise self._map_error(e) from e
        return SubmitResult(
            correct=getattr(r, "correct", False),
            awarded=int(getattr(r, "awarded", 0) or 0),
            cumulative_score=int(getattr(r, "cumulative_score", 0) or 0),
            correct_flag_count=int(getattr(r, "correct_flag_count", 0) or 0),
            total_flag_count=int(getattr(r, "total_flag_count", 0) or 0),
            matched_flag_index=getattr(r, "matched_flag_index", None),
        )

    def close_challenge(self, unique_code: str) -> CloseResult:
        try:
            r = self._get_client().close_challenge(unique_code)
        except sdk.InvalidState:
            return CloseResult(unique_code=unique_code, closed=False)
        except sdk.ChallengeNotFound:
            return CloseResult(unique_code=unique_code, closed=False)
        except Exception as e:
            raise self._map_error(e) from e
        return CloseResult(
            unique_code=getattr(r, "unique_code", unique_code),
            closed=bool(getattr(r, "closed", True)),
        )

    def check_vpn(self, *, timeout: float = 10.0) -> VpnCheckResult:
        """SDK 的 VPN 预检（同步 client 也提供 check_vpn）"""
        try:
            r = self._get_client().check_vpn()
        except Exception as e:
            mapped = self._map_error(e)
            if isinstance(mapped, VpnCheckError):
                raise mapped
            raise VpnCheckError(reason="network_error") from e
        return VpnCheckResult(
            status=getattr(r, "status", ""),
            client_ip=getattr(r, "client_ip", ""),
            time=getattr(r, "time", ""),
            ok=bool(getattr(r, "ok", False)),
        )

    def health_check(self) -> bool:
        try:
            self.list_challenges()
            return True
        except Exception:
            return False
