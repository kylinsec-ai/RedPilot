"""
求解器工厂 — 固定使用 Pi Agent

本适配器只使用 Pi Agent 作为解题引擎（ADAPTER_SOLVER 不再多选）。
额外配置:
- ADAPTER_SOLVER_MODEL: pi 的模型（如 deepseek-v4-flash，留空用 pi 默认）
- ADAPTER_SKILLS_DIR:   pi 的 skills 目录（可选）
"""

from __future__ import annotations

import logging

from .base import SolverBackend
from .pi_agent import PiAgentBackend

log = logging.getLogger("adapter.solver.factory")


def create_solver(model: str = "", skills_dir: str = "") -> SolverBackend:
    """创建 Pi Agent 求解器后端"""
    log.info("solver backend: pi-agent (model=%s skills=%s)",
             model or "default", skills_dir or "none")
    return PiAgentBackend(model=model, skills_dir=skills_dir)