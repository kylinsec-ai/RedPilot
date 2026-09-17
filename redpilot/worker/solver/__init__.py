"""求解引擎契约层。

- `base.py`      `SolveResult`（含 `provider_failure` 判据）+ `touch_heartbeat`
- 实际引擎      `redpilot.worker.adapter.solver.pi_agent.PiAgentBackend`（朋友版，
                1,340 行：四个看门狗、令牌式进程回收、subagent/skills 安装器、
                逐题 `.pi-home` 隔离、provider 配置落地、续接块回捞）
- 引擎工厂      `redpilot.worker.adapter.solver.factory.create_solver`
                —— **竞技场主循环直接用它**，框架侧不再有自己的工厂与桥接

历史：这里曾有一个 517 行的框架自家 pi_agent、一个 `AgentAdapter` 抽象和一个
`friend.py` 桥接层。2026-09 集成把引擎统一到朋友版，桥接层随之作废（它翻译的
`AgentAdapter` 契约只有框架自己的 `orchestration.solve_one` 在用，而那条链路
已整体退位）。
"""

from .base import SolveResult, touch_heartbeat

__all__ = ["SolveResult", "touch_heartbeat"]
