"""求解器工厂 — 唯一引擎：朋友的 Pi Agent 实现。"""

from __future__ import annotations

from .base import AgentAdapter
from .friend import FriendSolver


def create_solver() -> AgentAdapter:
    """构造求解引擎。

    参数刻意留空（模型/技能/思考强度都在 solve() 时从配置合并，口径见
    friend.adopt_config 与朋友引擎的 solve 注释）——模型走 `SOLVER_MODEL`，
    完整 provider/id 由 config.py 校验；skills/thinking 走各自的 env。
    """
    return FriendSolver()
