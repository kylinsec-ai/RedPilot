"""LiveState 快照 schema — worker→obs live 通道的 18 键 wire 契约。

此前只以 dict 字面量(adapter/live/state.py)与注释(obs/schema.py LiveIn)存在;
此处为类型化单一声明。obs 入口仍是 dict[str, Any](字节兼容,不加运行时拒绝);
本模块供 worker 侧类型标注与文档化。
"""

from __future__ import annotations

from typing import TypedDict


class LiveSnapshot(TypedDict, total=False):
    """LiveState 快照(total=False:字段随求解阶段渐进填充)。

    18 键,键名与 worker LiveState._data 逐字对齐 —— 改名必须同步
    adapter(→worker)LiveState 与前端 frontend-vue/src/types/index.ts。
    """
    worker_id: str
    phase: str
    challenge_code: str
    model: str
    started_at: float
    updated_at: float
    elapsed_s: int
    turns: int
    current_tool: str
    current_args_summary: str
    last_tool: str
    last_output_tail: str
    assistant_preview: str
    thinking_len: int
    flags_found: int
    accepted: int
    error: str
    transcript_path: str


# 快照键全集(测试用:LiveState 产出键 ⊆ LIVE_SNAPSHOT_KEYS 断言)
LIVE_SNAPSHOT_KEYS: tuple[str, ...] = (
    "worker_id", "phase", "challenge_code", "model", "started_at", "updated_at",
    "elapsed_s", "turns", "current_tool", "current_args_summary", "last_tool",
    "last_output_tail", "assistant_preview", "thinking_len", "flags_found",
    "accepted", "error", "transcript_path",
)
