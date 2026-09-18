"""RedPilot worker 模块。

形态: 一个容器（worker-1 持 VPN 并向外共享 netns，worker-2/3 复用它）。
主循环是**竞技场**（`orchestrator`，多会话/时间盒/止损/eager 提交/
能力分片/舰队监督/热重载），求解引擎是 Pi Agent（`adapter.solver`）。

装配入口: `python -m redpilot.worker.driver`
  driver.main()  = WorkerSettings 校验 + 观测面（LiveState/LiveBus/relay/
                   roster/:8080）+ StatusBridge 注入
  orchestrator.main() = 主循环本体（本包不再有第二套编排）

本 `__init__` 是 worker 的公共 API（façade）：跨模块只能 import 这里
`__all__` 中的名字。实现全在子模块，重复件（旧 task/taskprompt/flags）已删除，
AgentTask / build_task_prompt / extract_flags 各有且只有一份定义。
"""

from .adapter.solver.base import extract_flags, is_valid_flag
from .adapter.task import AgentTask
from .adapter.taskprompt import build_task_prompt, write_context_md
from .config import SolverConfig
from .live import LiveBus, LiveState, head_text, summarize_args, tail_text
from .observability import StatusBridge
from .relay import ObsRelay, maybe_start_relay
from .settings import WorkerSettings
from .solver import touch_heartbeat

__all__ = [
    # 配置与任务
    "SolverConfig", "AgentTask", "WorkerSettings",
    "build_task_prompt", "write_context_md",
    # flag
    "extract_flags", "is_valid_flag",
    # 实时状态与观测桥
    "LiveBus", "LiveState", "head_text", "summarize_args", "tail_text",
    "StatusBridge",
    # 求解引擎契约（只剩心跳；框架侧 SolveResult 已随死码清扫删除，见 solver/base.py）
    "touch_heartbeat",
    # 观测中继
    "ObsRelay", "maybe_start_relay",
]
