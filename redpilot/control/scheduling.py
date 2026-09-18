"""Evaluation/Job/Attempt 调度外观:control-plane 状态机的唯一入口。

只委托 ControlPlaneService + Store 调度切面,不触碰 challenges/submissions
业务表;业务侧入口见 challenges.py。api.py control/worker 路由组只经本外观调用。
"""

from __future__ import annotations

from typing import Any, Mapping

from redpilot.control.control import ControlPlaneService
from redpilot.control.store import Store


class SchedulingFacade:
    """调度外观(assignment 语义 + lease/事件/终态,不含评分规则)。"""

    def __init__(self, store: Store, control: ControlPlaneService) -> None:
        self._store = store
        self._control = control

    @property
    def store(self) -> Store:
        return self._store

    @property
    def control(self) -> ControlPlaneService:
        return self._control

    # — evaluation —
    def create_evaluation(self, task_token: str, *, project_id: str = "default",
                          idempotency_key: str | None = None) -> dict:
        return self._store.create_evaluation(
            task_token, project_id=project_id, idempotency_key=idempotency_key)

    def get_evaluation(self, evaluation_id: str) -> dict | None:
        return self._store.get_evaluation(evaluation_id)

    def list_evaluations(self, project_id: str | None = None) -> list[dict]:
        return self._store.list_evaluations(project_id)

    def cancel_evaluation(self, evaluation_id: str) -> dict | None:
        return self._store.cancel_evaluation(evaluation_id)

    # — worker —
    def register_worker(self, worker_id: str, capabilities: dict | None = None) -> dict:
        return self._store.register_worker(worker_id, capabilities)

    def list_workers(self) -> list[dict]:
        return self._store.list_workers()

    def worker_heartbeat(self, worker_id: str, status: str = "idle") -> bool:
        return self._store.worker_heartbeat(worker_id, status)

    def claim(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        return self._control.claim(worker_id, lease_seconds)

    # — attempt —
    def heartbeat_assignment(self, attempt_id: str, worker_id: str,
                             lease_id: str, lease_seconds: int = 300) -> bool:
        return self._store.heartbeat_assignment(
            attempt_id, worker_id, lease_id, lease_seconds)

    def append_events(self, attempt_id: str, worker_id: str, lease_id: str,
                      rows: list[Mapping[str, Any]]) -> int:
        return self._control.append_events(attempt_id, worker_id, lease_id, rows)

    def complete_attempt(self, attempt_id: str, worker_id: str, lease_id: str, *,
                         status: str, solved: bool = False,
                         flags_found: int | None = None,
                         error: str | None = None) -> dict:
        return self._store.complete_attempt(
            attempt_id, worker_id, lease_id, status=status, solved=solved,
            flags_found=flags_found, error=error)

    def attempt_events(self, attempt_id: str) -> list[dict]:
        return self._store.attempt_events(attempt_id)
