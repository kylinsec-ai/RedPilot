"""脱敏 helpers — 从 adapter/live/state.py 逐字移植(platform 不能 import worker 代码,~60 行有意重复)。

原则:原文 payload 入库(与宿主 transcript.jsonl 同信任域);脱敏只作用于派生展示字段
(timeline 的 cmd 摘要、错误文案等),与旧 dashboard 行为一致。
"""

from __future__ import annotations

import json
import re

ARGS_SUMMARY_MAX = 300
ERROR_HEAD_MAX = 200

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


def head_text(s: str, max_len: int = ERROR_HEAD_MAX) -> str:
    s = s or ""
    return s if len(s) <= max_len else s[:max_len] + "…"


def summarize_args(args, max_len: int = ARGS_SUMMARY_MAX) -> str:
    """Truncate args JSON + redact secret-ish values (never ships full creds)."""
    if isinstance(args, dict):  # 先裁大 value,避免全量 dumps 巨型参数
        args = {k: (v[:1000] + "…" if isinstance(v, str) and len(v) > 1000 else v)
                for k, v in list(args.items())[:50]}
        args = _redact_value(args)
    try:
        s = json.dumps(args, ensure_ascii=False, default=str) if not isinstance(args, str) else args
    except Exception:
        s = str(args)
    if isinstance(args, str):
        # 大字符串先截断再跑正则:含嵌套量词的 _SECRET_PAIR_RX 扫全量 MB 级文本会卡住请求
        if len(s) > 65536:
            s = s[:65536] + "…"
        try:
            s = _SECRET_PAIR_RX.sub(r'\1"***"', s)
        except Exception:
            pass
    return head_text(s, max_len)
