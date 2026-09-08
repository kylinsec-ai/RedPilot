"""
平台接入层 — 数据模型、异常与抽象基类

设计目标：平台接入层通用化。
上层（driver）只依赖本文件的统一模型与接口，不感知具体平台差异。
新增平台只需实现 PlatformBackend 并注册到 factory。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("tsecbench_worker.platform")


# ── 统一数据模型 ──────────────────────────────────────────────

@dataclass
class Challenge:
    """平台返回的题目信息"""
    unique_code: str
    description: str = ""
    difficulty: str = ""
    level: int = 1
    total_score: int = 0
    flag_count: int = 1
    correct_flag_count: int = 0
    is_completed: bool = False
    container_status: str = "stopped"      # pending/available/stop_pending/stopped
    container_addr: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Challenge":
        return cls(
            unique_code=d.get("unique_code", ""),
            description=d.get("description") or "",
            difficulty=d.get("difficulty", ""),
            level=int(d.get("level", 1) or 1),
            total_score=int(d.get("total_score", 0) or 0),
            flag_count=int(d.get("flag_count", 1) or 1),
            correct_flag_count=int(d.get("correct_flag_count", 0) or 0),
            is_completed=bool(d.get("is_completed", False)),
            container_status=d.get("container_status", "stopped"),
            container_addr=d.get("container_addr") or [],
        )

    @property
    def remaining_flags(self) -> int:
        return max(0, self.flag_count - self.correct_flag_count)


@dataclass
class StartResult:
    """启动容器结果"""
    unique_code: str
    container_addr: list[str] = field(default_factory=list)


@dataclass
class SubmitResult:
    """flag 提交结果"""
    correct: bool = False
    awarded: int = 0
    cumulative_score: int = 0
    correct_flag_count: int = 0
    total_flag_count: int = 0
    matched_flag_index: Optional[int] = None
    duplicate: bool = False
    error: str = ""


@dataclass
class HintResult:
    """提示结果"""
    unique_code: str = ""
    hint: Optional[str] = None


@dataclass
class CloseResult:
    """关闭容器结果"""
    unique_code: str = ""
    closed: bool = False


@dataclass
class VpnCheckResult:
    """VPN 连通性检测结果"""
    status: str = ""
    client_ip: str = ""
    time: str = ""
    ok: bool = False


# ── 统一异常 ──────────────────────────────────────────────

class APIError(Exception):
    """平台 API 业务异常"""
    def __init__(self, code: str, message: str, status: int = 0, detail: dict = None):
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}
        super().__init__(f"[{status}] {code}: {message}")


class TaskNotFound(APIError):
    """token 无效/缺失"""
    pass


class ChallengeNotFound(APIError):
    """unique_code 不属于当前任务用例集"""
    pass


class InvalidState(APIError):
    """任务已结束 或 活跃实例达上限 或 通关后看 hint"""
    pass


class DuplicateSubmit(APIError):
    """flag 已正确提交过（幂等）"""
    pass


class ResourceUnavailable(APIError):
    """靶场资源未就绪/已耗尽"""
    pass


class VpnCheckError(Exception):
    """VPN 联通预检失败"""
    def __init__(self, message: str = "VPN检测未通过,请检查靶场VPN网络配置",
                 reason: str = "network_error"):
        self.reason = reason
        super().__init__(message)


# ── 抽象基类 ──────────────────────────────────────────────

class PlatformBackend(ABC):
    """
    平台接入抽象接口。所有平台适配器实现本接口，
    上层 driver 只依赖此接口，不感知平台差异。
    """

    name: str = "abstract"

    @abstractmethod
    def list_challenges(self) -> list[Challenge]:
        """获取题目列表及作答进度"""

    @abstractmethod
    def start_challenge(self, unique_code: str) -> StartResult:
        """启动题目容器，返回直连地址"""

    @abstractmethod
    def get_hint(self, unique_code: str) -> HintResult:
        """获取提示（会扣分）"""

    @abstractmethod
    def submit_flag(self, unique_code: str, flag: str) -> SubmitResult:
        """提交 flag"""

    @abstractmethod
    def close_challenge(self, unique_code: str) -> CloseResult:
        """关闭容器、释放资源"""

    def check_vpn(self, *, timeout: float = 10.0) -> VpnCheckResult:
        """VPN 联通预检（默认实现：无 VPN 检查则视为通过）"""
        return VpnCheckResult(status="ok", ok=True)

    def health_check(self) -> bool:
        """连通性检查（token/服务是否可用）"""
        try:
            self.list_challenges()
            return True
        except TaskNotFound:
            return False
        except Exception:
            return False