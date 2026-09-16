"""RedPilot worker 包。

形态: 一个容器（worker-1 持 VPN 并向外共享 netns，worker-2/3 复用它）。
主循环是**朋友的竞技场**（`orchestrator`，多会话/时间盒/止损/eager 提交/
能力分片/舰队监督/热重载），求解引擎是朋友的 Pi Agent（`adapter.solver`）。

装配入口: `python -m redpilot_worker.driver`
  driver.main()  = WorkerSettings 校验 + 观测面（LiveState/LiveBus/relay/
                   roster/:8080）+ StatusBridge 注入
  orchestrator.main() = 主循环本体（本包不再有第二套编排）

可观测性经 obs.localserver（注入式 stdlib 仪表板）与本包 relay（→obs 平台）。
编排侧状态另落 `<workdir>/status/worker-<N>.json`（supervisor 与只读控制台读）。
"""

from .config import SolverConfig
from .task import AgentTask
from .taskprompt import build_task_prompt, write_context_md
from .flags import extract_flags, is_valid_flag
from .transcripts import compress_transcript
from .live import LiveBus, LiveState, head_text, summarize_args, tail_text
from .observability import StatusBridge
from .solver import SolveResult, touch_heartbeat
from .settings import WorkerSettings
from .relay import ObsRelay, maybe_start_relay

__version__ = "0.1.0"

__all__ = [
    # 配置与任务
    "SolverConfig", "AgentTask", "WorkerSettings",
    "build_task_prompt", "write_context_md",
    # flag 与 transcript
    "extract_flags", "is_valid_flag", "compress_transcript",
    # 实时状态与观测桥
    "LiveBus", "LiveState", "head_text", "summarize_args", "tail_text",
    "StatusBridge",
    # 求解引擎契约
    "SolveResult", "touch_heartbeat",
    # 观测中继
    "ObsRelay", "maybe_start_relay",
]
