"""
Agent 能力层 — Pi Agent 求解器适配器

只使用 Pi Agent CLI（json print 模式）作为解题引擎。
- base.py          统一 SolveResult / flag 提取 / SolverBackend 抽象
- pi_agent.py      Pi Agent CLI 适配器
- factory.py       创建 Pi Agent 后端

上层只依赖 SolveResult 与 solve() 接口。

注：本文件是合并产物。main 侧定义了 `AgentAdapter`/`touch_heartbeat` 并把
flag 提取收敛到 `ghost_worker.flags`；朋友侧定义了 `CCResult`/`extract_handoff`。
本次合并**包名与实现留在 main**（`solve_one`/`kill_solver_processes`/provider
故障护栏等 main 侧回归测试锁定的契约），因此这里只补上名字对齐，不再搬文件。
跨场交接链（B14）与两个 solver 实现的合一见后续阶段。
"""

from ghost_worker.flags import extract_flags, is_valid_flag
from .base import (
    AgentAdapter,
    SolveResult,
    SolverBackend,
    touch_heartbeat,
)
from .pi_agent import PiAgentBackend
from .factory import create_solver

# 兼容别名：朋友侧的调用点使用 CCResult（= SolveResult 的另一名字）。
CCResult = SolveResult

__all__ = [
    "SolveResult", "CCResult", "SolverBackend", "AgentAdapter",
    "extract_flags", "is_valid_flag",
    "PiAgentBackend", "create_solver", "touch_heartbeat",
]
