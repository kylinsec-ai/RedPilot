"""求解引擎契约 — 竞技场主循环与 Pi 引擎之间的那层薄接口。

## 这一层为什么还在

竞技场主循环（`ghost_worker.orchestrator`）用**朋友自己的** `SolverBackend`
接口调引擎（`flag_format=` / `on_fact=` / `stop_check=` / `transcript_path=`），
而这里保留的是框架侧的两个东西：

- `SolveResult`：框架读的那个结果形状。**注意它与朋友
  `ghost_worker.adapter.solver.SolveResult` 不是同一个类** —— 朋友版没有
  `provider_failure` 判据，而"0-turn + 报错"是识别"引擎压根没跑起来"的唯一信号
  （2026-09-08 事故：pi 对 provider 400 只发 stopReason=error，漏读 → err=none
  → 编排层当成"正常跑完没解出来" → 280 run/0 flag/63 题静默烧库）。
  它与编排侧的 `_is_api_fault`（认报错文本里的余额/认证 token）是**互补**关系，
  不是重复：前者认"没跑起来"，后者认"跑到一半账号挂了"。分工写在
  `orchestrator._is_api_fault` 的 docstring 里 —— 合并会丢一半语义，
  所以这个类里的属性看着像冗余也**不能删**。
- `touch_heartbeat()`：compose healthcheck 读的那个心跳文件（路径单源在
  `ghost_contracts.paths`）。

## 已退役的部分

- `AgentAdapter` 抽象与 `solver/friend.py` 桥接层：它们服务的是框架自己的
  `orchestration.solve_one`，而那条链路已整体退位（竞技场主循环直接调朋友引擎）。
- `solver/factory.create_solver()`：竞技场用它那边的工厂
  （`ghost_worker.adapter.solver.factory.create_solver`），不再需要框架这一份。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ghost_contracts.paths import HEARTBEAT_PATH


@dataclass
class SolveResult:
    """一次 Pi 会话的结果（框架侧形状；字段与朋友结果模型的并集）。

    前 7 个字段是框架编排原有的；后 5 个是朋友引擎独有、为过渡期排查保留
    （handoff 目前全仓无消费者，朋友自己的注释里也这么写）。
    """
    flags: list[str] = field(default_factory=list)
    tool_outputs: list = field(default_factory=list)
    observed_output: str = ""
    error: str = ""
    turns: int = 0
    duration_s: float = 0.0
    infra_blocked: bool = False
    # ── 朋友引擎独有 ──
    final_answer: str = ""
    final_text: str = ""
    handoff: str = ""
    termination_reason: str = ""
    target_fault: bool = False

    @property
    def has_flags(self) -> bool:
        return bool(self.flags)

    @property
    def provider_failure(self) -> bool:
        """0-turn 且带报错 = 引擎压根没跑起来，不是"这题没解出来"。

        判否 = 把一次没发生的求解当成"正常跑完没解出来"，静默烧题（见模块头的事故）。
        注意它**只管"没跑"**：跑到一半的账号级故障（余额/认证）由编排侧的
        `orchestrator._is_api_fault` 认，那个认的是报错文本而非 turns。两条判据互补。
        """
        return self.turns == 0 and bool(self.error)


def touch_heartbeat() -> None:
    """刷新心跳文件 mtime（compose healthcheck 依据）。失败静默 —— 心跳写不动
    时该报的警由 healthcheck 那一侧报，这里抛异常只会把启动流程带崩。"""
    try:
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
    except Exception:
        pass
