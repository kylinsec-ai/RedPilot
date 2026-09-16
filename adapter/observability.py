"""
结构化事件日志
用于记录运行中的关键事件，便于事后分析和调试。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger("adapter.obs")

_log_path: Optional[str] = None
_run_id: str = ""
_context: dict = {}
# Challenge/attempt metadata is set from parallel solver threads. Keep these
# fields thread-local so events from one challenge cannot inherit the identity
# of another challenge when a thread-pool worker is reused.
_local_context = threading.local()
_LOCAL_KEYS = frozenset(("challenge_id", "attempt_id"))
_lock = threading.Lock()
_file = None


def configure(path: str, *, run_id: str = ""):
    """配置事件日志输出文件"""
    global _log_path, _run_id, _file, _context
    _log_path = path
    _run_id = run_id
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _lock:
        # Reconfiguration is used by reloads/tests; close the previous handle
        # so descriptors and stale streams do not accumulate.
        old_file = _file
        _file = open(path, "a", encoding="utf-8")
        _context = {}
    if old_file is not None and old_file is not _file:
        try:
            old_file.close()
        except Exception:
            pass
    clear_local_context()


def context(**kwargs):
    """设置当前上下文 (线程安全)"""
    global _context
    local_values = {k: v for k, v in kwargs.items() if k in _LOCAL_KEYS}
    process_values = {k: v for k, v in kwargs.items() if k not in _LOCAL_KEYS}
    with _lock:
        _context.update(process_values)
    if local_values:
        values = getattr(_local_context, "values", None)
        if values is None:
            values = {}
            _local_context.values = values
        values.update(local_values)


def clear_local_context():
    """清理当前线程的题目上下文，避免线程池复用时沿用旧题目。"""
    try:
        _local_context.values = {}
    except Exception:
        pass


def emit(event: str, *, layer: str = "adapter", payload: dict = None):
    """
    记录一条结构化事件。

    Args:
        event: 事件名称
        layer: 来源层 (adapter/driver/verify/etc)
        payload: 事件载荷
    """
    if _file is None:
        return

    entry = {
        "ts": time.time(),
        "run_id": _run_id,
        "event": event,
        "layer": layer,
    }

    with _lock:
        if _context:
            entry.update(_context)
        local_values = getattr(_local_context, "values", None) or {}
        if local_values:
            entry.update(local_values)

    if payload:
        entry["payload"] = payload

    try:
        with _lock:
            _file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _file.flush()
    except Exception as e:
        log.warning("event emit failed: %s", e)


def close():
    """关闭日志文件"""
    global _file
    if _file:
        try:
            _file.close()
        except Exception:
            pass
        _file = None
