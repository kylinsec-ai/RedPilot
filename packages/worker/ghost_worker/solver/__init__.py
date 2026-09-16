"""
Agent 能力层 — Pi Agent 求解器适配器

只用 Pi Agent CLI（json print 模式）作为解题引擎，实现分两处：

- base.py       框架契约：SolveResult / AgentAdapter / touch_heartbeat
- friend.py     桥接层：把朋友的引擎（adapter/solver/pi_agent.py，1,340 行）
                翻译成上面的契约（补 on_event、会话中断、配置适配）
- factory.py    构造入口（driver 唯一调用点）
- adapter/      **实际引擎**：ghost_worker.adapter.solver.pi_agent.PiAgentBackend

上层（orchestration.solve_one）只依赖 SolveResult 与 AgentAdapter，不 import
任何具体实现；测试用 FakeAdapter 注入。

历史说明：框架曾有一个 517 行的自家 pi_agent，与朋友的 1,340 行版本并存。
2026-09 的集成把实现统一到朋友版（它的四个看门狗、令牌式进程回收、
subagent/skills 安装器、逐题 HOME 隔离都更强），框架侧只保留契约与桥接。
"""

from ghost_worker.flags import extract_flags, is_valid_flag
from .base import (
    AgentAdapter,
    SolveResult,
    SolverBackend,
    touch_heartbeat,
)
from .friend import FriendSolver, adopt_config
from .factory import create_solver

# 兼容别名：朋友侧的调用点使用 CCResult（= SolveResult 的另一名字）。
CCResult = SolveResult

__all__ = [
    "SolveResult", "CCResult", "SolverBackend", "AgentAdapter",
    "extract_flags", "is_valid_flag",
    "FriendSolver", "create_solver", "adopt_config", "touch_heartbeat",
]
