"""
TsecBench 平台接入层适配器(精简包)

求解引擎固定为 Pi Agent(adapter.solver);平台接入固定 TSecBench HTTP
(adapter.platform)。driver 经本包顶层符号即可完成装配。
"""

from adapter.config import SolverConfig
from adapter.task import AgentTask
from adapter.solver import SolveResult, create_solver, normalize_flag_body, touch_heartbeat
from adapter.taskprompt import build_task_prompt, write_context_md
from adapter.platform import Challenge, SubmitResult, create_platform

__all__ = [
    "SolverConfig",
    "AgentTask",
    "SolveResult", "create_solver", "normalize_flag_body", "touch_heartbeat",
    "build_task_prompt", "write_context_md",
    "Challenge", "SubmitResult", "create_platform",
]
