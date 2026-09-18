"""Live worker state — summary-first snapshot。

截断常量/文本工具/脱敏摘要/原子 IO 单源 redpilot.contracts(redact/text/fsio),
本模块只剩 LiveState 本体与热路径节流常量。
"""

from __future__ import annotations

import threading
import time

from redpilot.contracts.fsio import atomic_write_json, ensure_dir

SAVE_MIN_INTERVAL = 1.0  # 热路径节流：高频 delta 只写内存，落地最多 1/s


class LiveState:
    """Thread-safe summary snapshot, atomically persisted as JSON (throttled)."""

    def __init__(self, worker_id: str = "worker-1", state_path: str | None = None):
        self._lock = threading.Lock()
        self._data = {
            "worker_id": worker_id,
            "phase": "idle",
            "challenge_code": "",
            "model": "",
            "started_at": 0.0,
            "updated_at": time.time(),
            "elapsed_s": 0,
            "turns": 0,
            "current_tool": "",
            "current_args_summary": "",
            "last_tool": "",
            "last_output_tail": "",
            "assistant_preview": "",
            "thinking_len": 0,
            "flags_found": 0,
            "accepted": 0,
            "error": "",
            "transcript_path": "",
        }
        self._path = state_path
        self._last_save = 0.0
        if state_path:
            ensure_dir(state_path)

    def update(self, _turns_inc: int = 0, **fields) -> dict:
        """合并字段并节流落地(每次调用刷新 updated_at/elapsed_s;不保证落盘 ——
        边界事件的强制落盘由调用方随 flush() 完成,见 contracts.vocabulary.FLUSH_KINDS)。"""
        with self._lock:
            if _turns_inc:
                self._data["turns"] = int(self._data.get("turns", 0)) + _turns_inc
            self._data.update(fields)
            self._data["updated_at"] = time.time()
            if self._data.get("started_at"):
                self._data["elapsed_s"] = int(self._data["updated_at"] - self._data["started_at"])
            snap = dict(self._data)
        self.save()
        return snap

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def save(self, force: bool = False) -> None:
        """节流落地；force=True 用于 tool_end/lifecycle/error 等边界事件。
        失败(如目录不可写)不推进 _last_save —— 下次 throttled save 仍可重试,
        不至于静默冻结一个节流窗口。"""
        if not self._path:
            return
        now = time.monotonic()
        if not force and now - self._last_save < SAVE_MIN_INTERVAL:
            return
        with self._lock:
            snap = dict(self._data)
        if atomic_write_json(self._path, snap):
            self._last_save = time.monotonic()

    def flush(self) -> None:
        self.save(force=True)
