"""Live worker state — summary-first snapshot + redaction helpers."""

from __future__ import annotations

import json
import os
import re
import threading
import time

ARGS_SUMMARY_MAX = 300
OUTPUT_TAIL_MAX = 2048
ASSISTANT_PREVIEW_MAX = 500
ERROR_HEAD_MAX = 200
SAVE_MIN_INTERVAL = 1.0  # 热路径节流：高频 delta 只写内存，落地最多 1/s

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


def ensure_dir(path: str) -> None:
    """dirname 存在则建目录；空 dirname 不做事（os.makedirs('') 会抛错）"""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass


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
        # 大字符串先截断再跑正则:含嵌套量词的 _SECRET_PAIR_RX 扫全量 MB 级文本会卡住求解线程
        if len(args) > 65536:
            args = args[:65536] + "…"
        try:
            s = _SECRET_PAIR_RX.sub(r'\1"***"', s)
        except Exception:
            pass
    return head_text(s, max_len)


def tail_text(s: str, max_len: int) -> str:
    s = s or ""
    return s if len(s) <= max_len else "…" + s[-max_len:]


def head_text(s: str, max_len: int = ERROR_HEAD_MAX) -> str:
    s = s or ""
    return s if len(s) <= max_len else s[:max_len] + "…"


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

    def update(self, _turns_inc: int = 0, _immediate: bool = False, **fields) -> dict:
        with self._lock:
            if _turns_inc:
                self._data["turns"] = int(self._data.get("turns", 0)) + _turns_inc
            self._data.update(fields)
            self._data["updated_at"] = time.time()
            if self._data.get("started_at"):
                self._data["elapsed_s"] = int(self._data["updated_at"] - self._data["started_at"])
            snap = dict(self._data)
        self.save(force=_immediate)
        return snap

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def save(self, force: bool = False) -> None:
        """节流落地；force=True 用于 tool_end/lifecycle/error 等边界事件"""
        if not self._path:
            return
        now = time.monotonic()
        if not force and now - self._last_save < SAVE_MIN_INTERVAL:
            return
        try:
            with self._lock:
                snap = dict(self._data)
            # tmp 名带线程 + 纳秒(同进程并发 save 不互踩);失败不推进 _last_save,
            # 下次 throttled save 仍可重试,不至于静默冻结一个节流窗口
            tmp = f"{self._path}.tmp.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
            os.replace(tmp, self._path)
            self._last_save = time.monotonic()
        except Exception:
            pass

    def flush(self) -> None:
        self.save(force=True)

    # ── pi event helpers（tool_end/lifecycle 类边界事件立即落地）──
    def on_tool_start(self, tool_name: str, args) -> dict:
        return self.update(
            _turns_inc=1,
            phase="solving",
            current_tool=tool_name or "",
            current_args_summary=summarize_args(args or {}),
        )

    def on_tool_progress(self, preview: str) -> dict:
        return self.update(last_output_tail=tail_text(preview, OUTPUT_TAIL_MAX))

    def on_tool_end(self, tool_name: str, out: str, flags: int = 0) -> dict:
        return self.update(
            _immediate=True,
            last_tool=tool_name or "",
            last_output_tail=tail_text(out or "", OUTPUT_TAIL_MAX),
            current_tool="",
            flags_found=flags,
        )

    def on_text(self, preview: str, thinking_len: int = 0) -> dict:
        fields: dict = {"assistant_preview": tail_text(preview, ASSISTANT_PREVIEW_MAX)}
        if thinking_len:
            fields["thinking_len"] = thinking_len
        return self.update(**fields)
