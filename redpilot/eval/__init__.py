"""评估与轨迹面 —— 观测平台的下游消费者。

模块职责:
  eval/replay.py            回放:run_id → 事件 → 紧凑时间线 → Trace（唯一数据入口）
  eval/dataset.py           任务卡与评估集装载
  eval/graders/             判据(确定性优先;模型评分仅用于主观维度)
  eval/report.py            pass^k / 触发率 / 成本
  eval/store.py             评估结果落库(独立于 runs,前缀 eval_)

两条红线:

1. **只读**。评估是观测面的下游 —— 不写 runs/events、不碰控制面、不改任何
   worker 状态。评估结果落自己的表。
2. **不 import redpilot.worker**（与 `redpilot.obs` 同一条纯度约束，由
   `tests/architecture/test_layers.py` 强制）。这带来一个真实后果：
   判据必须**注入**而不是从 worker 侧 import —— 见 `graders/deterministic.py`
   的 `Predicates` 协议。宁可把"判据缺席"报成 skipped，也不在这里复制一份，
   因为复制出来的那份就是判据分叉的起点。
"""

from .dataset import (CATEGORIES, CHECK_IDS, TaskCard, coverage, load_tasks,
                      task_from_dict)
from .graders import CHECKS, CheckResult, GradeReport, Predicates, grade
from .report import (DEFAULT_K, RunRecord, check_coverage, cost, pass_k,
                     summary, trigger_rate)
from .replay import (Replayer, ToolCall, Trace, skill_name_from_cmd,
                     trace_from_rows, trace_from_transcript)
from .store import EvalStore

__all__ = [
    # 数据集
    "CATEGORIES", "CHECK_IDS", "TaskCard", "coverage", "load_tasks", "task_from_dict",
    # 回放（唯一数据入口）
    "Replayer", "ToolCall", "Trace", "skill_name_from_cmd",
    "trace_from_rows", "trace_from_transcript",
    # 判据（确定性优先；模型评分是后续阶段）
    "CHECKS", "CheckResult", "GradeReport", "Predicates", "grade",
    # 报告
    "DEFAULT_K", "RunRecord", "check_coverage", "cost", "pass_k", "summary",
    "trigger_rate",
    # 结果落库（独立于生产观测库）
    "EvalStore",
]
