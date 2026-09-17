"""平台控制面共享契约。

这里只放跨进程的词汇、状态集合和事件 envelope；业务规则仍由 core
负责，contracts 保持标准库零依赖，worker/obs 可以安全复用。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Literal, Mapping
from uuid import uuid4


EVALUATION_STATES: tuple[str, ...] = (
    "queued",
    "running",
    "completed",
    "canceled",
    "expired",
)
JOB_STATES: tuple[str, ...] = (
    "pending",
    "running",
    "completed",
    "failed",
    "canceled",
)
ATTEMPT_STATES: tuple[str, ...] = (
    "starting",
    "solving",
    "submitting",
    "closing",
    "solved",
    "done",
    "failed",
    "interrupted",
)
WORKER_STATES: tuple[str, ...] = ("offline", "idle", "busy", "draining")

# 供类型检查的封闭集合(运行时仍用上方 tuple;新增状态先改 Literal 再同步 tuple)。
EvaluationStatus = Literal["queued", "running", "completed", "canceled", "expired"]
JobStatus = Literal["pending", "running", "completed", "failed", "canceled"]
AttemptStatus = Literal["starting", "solving", "submitting", "closing", "solved", "done",
                        "failed", "interrupted"]
WorkerStatus = Literal["offline", "idle", "busy", "draining"]

# ── canonical vs telemetry 权威语义 ─────────────────────────────
# canonical attempt 事件(attempt.started/completed)是终态唯一权威,只能由
# core 控制面经 outbox 投递,obs 侧只能经 canonical 处理路径写入。
# worker relay 的 telemetry(events/live/run_close/roster/ping)永远是非权威观测,
# relay run_close 永不能覆盖 canonical 终态。source 语义由函数/模块名显式区分:
# obs 侧 canonical_ingest(权威) vs telemetry_ingest(非权威),runs.canonical 列即
# source 标记(1=权威终态已落库,0=仅 telemetry)。
CANONICAL_EVENT_TYPES: tuple[str, ...] = ("attempt.started", "attempt.completed")
CANONICAL_TERMINAL_STATUSES: tuple[str, ...] = ("solved", "done", "failed", "interrupted")


def is_canonical_event_type(event_type: str) -> bool:
    """是否为 canonical attempt 事件(权威源);空/未知一律 False。"""
    return event_type in CANONICAL_EVENT_TYPES


def is_canonical_terminal_status(status: str) -> bool:
    """canonical attempt.completed 允许的终态集合。"""
    return status in CANONICAL_TERMINAL_STATUSES


def new_id() -> str:
    """Return the compact identifier format used by platform records."""

    return uuid4().hex


@dataclass(frozen=True)
class EventEnvelope:
    """Versioned event envelope shared by core, worker and projections."""

    event_id: str
    event_type: str
    evaluation_id: str | None
    job_id: str | None
    attempt_id: str | None
    worker_id: str | None
    seq: int
    occurred_at: float
    payload: dict[str, Any]
    schema_version: int = 1

    def __post_init__(self) -> None:
        # 构造期即守 invariant,不依赖 core/api 二次校验(直构造 envelope 的调用方同样受约)。
        if not self.event_type or not self.event_type.strip():
            raise ValueError("event_type_required")
        if self.seq < 0:
            raise ValueError("event_seq_invalid")
        if not math.isfinite(self.occurred_at):
            raise ValueError("occurred_at must be finite")
        if self.schema_version != 1:
            raise ValueError("unsupported schema_version")


    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        evaluation_id: str | None = None,
        job_id: str | None = None,
        attempt_id: str | None = None,
        worker_id: str | None = None,
        seq: int = 0,
        event_id: str | None = None,
        occurred_at: float | None = None,
    ) -> "EventEnvelope":
        return cls(
            event_id=event_id or new_id(),
            event_type=event_type,
            evaluation_id=evaluation_id,
            job_id=job_id,
            attempt_id=attempt_id,
            worker_id=worker_id,
            seq=seq,
            occurred_at=time.time() if occurred_at is None else float(occurred_at),
            payload=dict(payload or {}),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "evaluation_id": self.evaluation_id,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "worker_id": self.worker_id,
            "seq": self.seq,
            "occurred_at": self.occurred_at,
            "payload": dict(self.payload),
        }
