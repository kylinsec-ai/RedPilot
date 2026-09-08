"""
Agent 能力层 — 统一结果模型与 SolverBackend 抽象接口

设计目标：解题 Agent（solver）固定使用 Pi Agent 编排网络安全 Agent。
- pi_agent.py      Pi Agent CLI 适配器（唯一求解引擎）
- factory.py       创建 Pi Agent 后端

上层（编排层）只依赖本文件的 SolveResult 与 solve() 接口。
flag 提取/校验单源 tsecbench_worker.flags;心跳路径单源 tsecbench_contracts.paths。
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

from tsecbench_contracts.paths import FLAG_FILES, HEARTBEAT_PATH
from tsecbench_worker.flags import is_valid_flag

log = logging.getLogger("tsecbench_worker.solver")

@dataclass
class SolveResult:
    """Agent 会话执行结果（各后端统一输出）"""
    flags: list[str] = field(default_factory=list)
    tool_outputs: list = field(default_factory=list)
    observed_output: str = ""
    error: str = ""
    turns: int = 0
    duration_s: float = 0.0
    infra_blocked: bool = False

    @property
    def has_flags(self) -> bool:
        return bool(self.flags)

    @property
    def provider_failure(self) -> bool:
        """零回合且末端报错 = provider 失败而非"会话完成"。

        pi 对 provider 400/超限等仅发 stopReason=error 的收尾消息(err=none 表象),
        编排层若当正常完成处理就会静默烧题库(2026-09-08 事故:280 run/0 flag)。
        turns==0 保证真实工作过(哪怕带错误)的会话不误判。
        """
        return self.turns == 0 and bool(self.error)


# ── 心跳文件 ─────────────────────────────────────────────

def touch_heartbeat() -> None:
    """更新心跳文件 mtime（失败静默）— driver 与 solver 会话共用"""
    try:
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
    except Exception:
        pass


class SolverBackend(ABC):
    """
    Agent 求解器抽象接口。
    所有求解器实现本接口，上层 driver 只依赖 solve()。
    """

    name: str = "abstract"

    @abstractmethod
    def solve(
        self,
        prompt: str,
        workdir: str,
        cfg,
        *,
        on_fact: Optional[Callable] = None,
        transcript_path: Optional[str] = None,
        max_retries: int = 2,
        on_event: Optional[Callable] = None,
    ) -> SolveResult:
        """执行一次解题会话，返回统一结果"""

    @staticmethod
    def _read_flag_files(workdir: str, flags: list[str]) -> list[str]:

        """从工作目录的标准 flag 文件补录候选(文件名单源 contracts.FLAG_FILES)"""
        for name in FLAG_FILES:
            p = os.path.join(workdir, name)
            try:
                if os.path.isfile(p):
                    with open(p, encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            v = line.strip()
                            if v and "{" in v and v.endswith("}") and len(v) <= 200:
                                if v not in flags and is_valid_flag(v):
                                    flags.append(v)
            except Exception:
                pass
        return flags