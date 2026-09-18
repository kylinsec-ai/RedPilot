"""
求解器工厂 — 固定使用 Pi Agent

本适配器只使用 Pi Agent 作为解题引擎（ADAPTER_SOLVER 不再多选）。
额外配置:
- ADAPTER_SOLVER_MODEL: pi 的模型（如 deepseek/mimo-v2.5，留空用 pi 默认）
- ADAPTER_SKILLS_DIR:   pi 的 skills 目录（可选）
"""

from __future__ import annotations

import logging

from .base import SolverBackend
from .pi_agent import PiAgentBackend

log = logging.getLogger("adapter.solver.factory")


def create_solver(model: str = "", skills_dir: str = "",
                  max_turns: int = 60, thinking: str = "",
                  provider: str = "") -> SolverBackend:
    """创建 Pi Agent 求解器后端。

    provider 可配（缺省读 SOLVER_PROVIDER / ADAPTER_PROVIDER，再回退 deepseek）；
    model 留空则由 SolverConfig.from_env() 的 preset 决定。
    """
    log.info("solver backend: pi-agent (provider=%s model=%s skills=%s thinking=%s)",
             provider or "env/default", model or "default",
             skills_dir or "none", thinking or "-")
    return PiAgentBackend(model=model, skills_dir=skills_dir,
                          max_turns=max_turns, thinking=thinking,
                          provider=provider)