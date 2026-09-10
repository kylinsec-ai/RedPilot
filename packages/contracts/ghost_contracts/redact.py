"""凭据脱敏 + 参数摘要 — 单一来源。

此前 adapter/live/state.py 与 obs/redact.py 逐字各持一份("platform 不能 import
worker 代码"时代的有意重复);contracts 消除该根因 —— 两侧都依赖本模块。
含大字符串先截断再跑正则的防灾难性回溯约定(嵌套量词扫 MB 级文本会卡住求解线程)。
"""

from __future__ import annotations

import json
import re

from .text import ARGS_SUMMARY_MAX, head_text

_SECRET_KEY_RX = re.compile(r"(key|token|secret|auth|password|passwd)", re.IGNORECASE)
# 字符串形态 args 中的 "secretKey": value 对 —— 只抹 value,保留 key 名
_SECRET_PAIR_RX = re.compile(
    r'("(?:[^"\\]|\\.)*?(?:key|token|secret|auth|password|passwd)(?:[^"\\]|\\.)*?"\s*:\s*)'
    r'("(?:[^"\\]|\\.)*"|[^\s,}]+)',
    re.IGNORECASE,
)


def _redact_value(v):
    """按 key 名抹掉疑似凭据的 value(递归进 dict/list);非容器原样返回"""
    if isinstance(v, dict):
        return {k: ("***" if _SECRET_KEY_RX.search(str(k)) else _redact_value(x))
                for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_redact_value(x) for x in v]
    return v


def summarize_args(args, max_len: int = ARGS_SUMMARY_MAX) -> str:
    """Truncate args JSON + redact secret-ish values (never ships full creds)."""
    if isinstance(args, dict):  # 先裁大 value，避免全量 dumps 巨型参数
        args = {k: (v[:1000] + "…" if isinstance(v, str) and len(v) > 1000 else v)
                for k, v in list(args.items())[:50]}
        args = _redact_value(args)
    try:
        s = json.dumps(args, ensure_ascii=False, default=str) if not isinstance(args, str) else args
    except Exception:
        s = str(args)
    if isinstance(args, str):
        # 大字符串先截断再跑正则:含嵌套量词的 _SECRET_PAIR_RX 扫全量 MB 级文本会卡住求解线程。
        # 注意必须裁 s 本身 —— args 与 s 是同一字符串(str 不可变,截 args 是死代码)
        if len(s) > 65536:
            s = s[:65536] + "…"
        try:
            s = _SECRET_PAIR_RX.sub(r'\1"***"', s)
        except Exception:
            pass
    return head_text(s, max_len)
