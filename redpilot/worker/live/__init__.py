"""Live bus + state exports.

展示常量/文本工具/脱敏摘要单源 redpilot.contracts(text/redact),此处转出
以保持 `from redpilot.worker.live import tail_text, ...` 既有拼写稳定。
"""

from .bus import LiveBus
from .state import LiveState
from redpilot.contracts.redact import summarize_args
from redpilot.contracts.text import (
    ASSISTANT_PREVIEW_MAX,
    ARGS_SUMMARY_MAX,
    ERROR_HEAD_MAX,
    OUTPUT_TAIL_MAX,
    head_text,
    tail_text,
)

__all__ = [
    "ARGS_SUMMARY_MAX",
    "OUTPUT_TAIL_MAX",
    "ASSISTANT_PREVIEW_MAX",
    "ERROR_HEAD_MAX",
    "LiveBus",
    "LiveState",
    "head_text",
    "summarize_args",
    "tail_text",
]
