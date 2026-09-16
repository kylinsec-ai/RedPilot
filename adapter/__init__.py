"""
TsecBench 平台接入层适配器
面向 TsecBench 安全基准测试平台的接入适配。
求解引擎固定为 Pi Agent，平台接入层通用化（tsecbench-http / generic）。
"""

from adapter.config import SolverConfig, ControllerConfig, LLMConfig, build_verifier_config
from adapter.task import AgentTask
from adapter.verify import Verifier, Claim
from adapter.solver import SolveResult, create_solver
from adapter.stoploss import StopLoss
from adapter.taskprompt import build_task_prompt

__all__ = [
    "SolverConfig", "ControllerConfig", "LLMConfig",
    "build_verifier_config",
    "AgentTask",
    "Verifier", "Claim",
    "SolveResult", "create_solver",
    "StopLoss",
    "build_task_prompt",
]