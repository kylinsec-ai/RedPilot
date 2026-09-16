"""
Ghost worker 包(解耦版)

求解引擎固定为 Pi Agent(ghost_worker.solver);legacy 模式直调官方
tsec-benchmark SDK(list 驱动主循环),assignment 模式经 assignment.py 领取
控制面 job/lease 后仍复用同一个 orchestration.solve_one 执行器。

可观测性经 obs.localserver(注入式 stdlib 仪表板)与本包 relay(→obs 平台)。
编排入口:
- orchestration.solve_one / driver.amain   legacy/assignment 双模式主循环(默认入口,python -m ghost_worker.driver)
"""

from .config import SolverConfig
from .assignment import AssignmentClient, AssignmentError
from .task import AgentTask
from .taskprompt import build_task_prompt, write_context_md
from .flags import extract_flags, is_valid_flag
from .transcripts import compress_transcript
from .live import LiveBus, LiveState, head_text, summarize_args, tail_text
from .solver import SolveResult, SolverBackend, create_solver, touch_heartbeat
from .settings import WorkerSettings
from .orchestration import solve_one, build_task
from .relay import ObsRelay, maybe_start_relay

__version__ = "0.1.0"

__all__ = [
    # 配置与任务
    "SolverConfig", "AgentTask", "WorkerSettings",
    "AssignmentClient", "AssignmentError",
    "build_task_prompt", "write_context_md", "build_task",
    # flag 与 transcript
    "extract_flags", "is_valid_flag", "compress_transcript",
    # 实时状态
    "LiveBus", "LiveState", "head_text", "summarize_args", "tail_text",
    # 求解器
    "SolveResult", "SolverBackend", "create_solver", "touch_heartbeat",
    # 编排与中继
    "solve_one", "ObsRelay", "maybe_start_relay",
]
