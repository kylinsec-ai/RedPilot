"""控制面应用服务:把 Store 的事务能力暴露成稳定的 assignment 语义。"""

from __future__ import annotations

from typing import Any, Mapping

from redpilot.contracts.platform import EventEnvelope

from redpilot.control.store import AssignmentRow, Store


class ControlPlaneService:
    """Evaluation/job/worker 服务。

    认证和 HTTP 状态码属于 API 层；本层只编排领域操作，便于后续迁移到
    独立调度器而不让 Worker 依赖 SQLite 实现细节。
    """

    def __init__(self, store: Store, *, public_base_url: str | None = None) -> None:
        self.store = store
        self.public_base_url = (public_base_url or "").rstrip("/")

    def assignment_payload(self, assignment: AssignmentRow) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "evaluation_id": assignment.evaluation_id,
            "job_id": assignment.job_id,
            "attempt_id": assignment.attempt_id,
            "lease_id": assignment.lease_id,
            "lease_expires_at": assignment.lease_expires_at,
            "benchmark_token": assignment.task_token,
            "unique_code": assignment.unique_code,
            "challenge": assignment.challenge,
        }
        if self.public_base_url:
            payload["benchmark_base_url"] = self.public_base_url
        return payload

    def claim(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        assignment = self.store.claim_job(worker_id, lease_seconds)
        return self.assignment_payload(assignment) if assignment is not None else None

    def append_events(
        self,
        attempt_id: str,
        worker_id: str,
        lease_id: str,
        rows: list[Mapping[str, Any]],
    ) -> int:
        context = self.store.attempt_context(attempt_id)
        if context is None:
            raise KeyError("attempt_not_found")
        events: list[EventEnvelope] = []
        for row in rows:
            event_type = str(row.get("event_type") or "").strip()
            if not event_type:
                raise ValueError("event_type_required")
            try:
                seq = int(row.get("seq", 0))
            except (TypeError, ValueError):
                raise ValueError("event_seq_invalid")
            if seq < 0:
                raise ValueError("event_seq_invalid")
            payload = row.get("payload")
            if payload is None:
                payload = {}
            if not isinstance(payload, Mapping):
                # 畸形载荷直接 422,不做 {"value": ...} 包裝(静默 coercion 藏 bug)。
                raise ValueError("event_payload_must_be_object")
            events.append(
                EventEnvelope.create(
                    event_type,
                    event_id=str(row["event_id"]) if row.get("event_id") else None,
                    evaluation_id=context["evaluation_id"],
                    job_id=context["job_id"],
                    attempt_id=attempt_id,
                    worker_id=worker_id,
                    seq=seq,
                    occurred_at=row.get("occurred_at"),
                    payload=payload,
                )
            )
        return self.store.append_attempt_events(attempt_id, worker_id, lease_id, events)
