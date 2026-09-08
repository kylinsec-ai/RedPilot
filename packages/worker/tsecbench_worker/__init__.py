"""
TsecBench worker 包(解耦版)

求解引擎固定为 Pi Agent(tsecbench_worker.solver);平台接入直调官方
tsec-benchmark SDK(AMAIN list 驱动主循环)并保留 worktree 吸收的
PlatformBackend 抽象(platform/)与队列客户端(queue.py,惰性 —— 默认入口不走)。

可观测性经 obs.localserver(注入式 stdlib 仪表板)与本包 relay(→obs 平台)。
编排入口:
- orchestration.solve_one / driver.amain   list 驱动主循环(默认入口,python -m tsecbench_worker.driver)
- queue.run_queue_worker                    jobs API 队列循环(平台侧实现后启用)
"""

from .config import SolverConfig
from .task import AgentTask
from .taskprompt import build_task_prompt, write_context_md
from .flags import extract_flags, is_valid_flag, normalize_flag_body
from .transcripts import compress_transcript
from .live import LiveBus, LiveState, head_text, summarize_args, tail_text
from .solver import SolveResult, SolverBackend, PiAgentBackend, create_solver, touch_heartbeat
from .settings import WorkerSettings
from .orchestration import solve_one, build_task
from .relay import ObsRelay, maybe_start_relay

__version__ = "0.1.0"

__all__ = [
    # 配置与任务
    "SolverConfig", "AgentTask", "WorkerSettings",
    "build_task_prompt", "write_context_md", "build_task",
    # flag 与 transcript
    "extract_flags", "is_valid_flag", "normalize_flag_body", "compress_transcript",
    # 实时状态
    "LiveBus", "LiveState", "head_text", "summarize_args", "tail_text",
    # 求解器
    "SolveResult", "SolverBackend", "PiAgentBackend", "create_solver", "touch_heartbeat",
    # 编排与中继
    "solve_one", "ObsRelay", "maybe_start_relay",
]
