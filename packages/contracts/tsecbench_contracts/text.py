"""文本截断工具 — head/tail 截断 + 各展示层长度上限(单一来源)。

此前 adapter/live/state.py 与 drivers(pi_agent/driver 经 import)及 obs/redact.py
各持一份;含大字符串先截断再跑正则的防回溯约定。
"""

from __future__ import annotations

ARGS_SUMMARY_MAX = 300
OUTPUT_TAIL_MAX = 2048
ASSISTANT_PREVIEW_MAX = 500
ERROR_HEAD_MAX = 200


def tail_text(s: str, max_len: int) -> str:
    s = s or ""
    return s if len(s) <= max_len else "…" + s[-max_len:]


def head_text(s: str, max_len: int = ERROR_HEAD_MAX) -> str:
    s = s or ""
    return s if len(s) <= max_len else s[:max_len] + "…"
