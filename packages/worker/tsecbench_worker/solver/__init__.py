"""
Agent 能力层 — Pi Agent 求解器适配器

只使用 Pi Agent CLI（json print 模式）作为解题引擎。
- base.py          统一 SolveResult / flag 提取 / SolverBackend 抽象
- pi_agent.py      Pi Agent CLI 适配器
- factory.py       创建 Pi Agent 后端

上层只依赖 SolveResult 与 solve() 接口。
"""

from tsecbench_worker.flags import extract_flags, is_valid_flag, normalize_flag_body
from .base import SolveResult, SolverBackend, touch_heartbeat
from .pi_agent import PiAgentBackend
from .factory import create_solver

__all__ = [
    "SolveResult", "SolverBackend", "extract_flags", "is_valid_flag",
    "normalize_flag_body", "PiAgentBackend", "create_solver", "touch_heartbeat",
]