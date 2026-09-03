"""求解器工厂 — 固定使用 Pi Agent"""

from __future__ import annotations

from .base import SolverBackend
from .pi_agent import PiAgentBackend


def create_solver() -> SolverBackend:
    """创建 Pi Agent 求解器后端（模型/API key 在 solve() 时从 SolverConfig 合并）"""
    return PiAgentBackend()
