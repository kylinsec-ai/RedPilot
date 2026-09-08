"""
TsecBench 平台接入层适配器(精简包)

求解引擎固定为 Pi Agent(adapter.solver);平台接入直调官方 tsec-benchmark SDK
(平台侧协议见 CHALLENGES_API.md / SDK_API.md)。driver 经本包顶层符号完成装配。
"""

from adapter.config import SolverConfig
from adapter.task import AgentTask
from adapter.solver import SolveResult, create_solver, normalize_flag_body, touch_heartbeat
from adapter.taskprompt import build_task_prompt, write_context_md

__all__ = [
    "SolverConfig",
    "AgentTask",
    "SolveResult", "create_solver", "normalize_flag_body", "touch_heartbeat",
    "build_task_prompt", "write_context_md",
]
