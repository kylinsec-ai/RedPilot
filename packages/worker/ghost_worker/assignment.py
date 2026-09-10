"""Worker assignment API 客户端。

这是控制面协议的薄客户端，不替代官方 tsec-benchmark SDK。SDK 仍负责
题目 start/submit/close；本模块负责 job 领取、租约和 attempt 结果回报。
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx


class AssignmentError(RuntimeError):
    """控制面返回了不可自动恢复的错误。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class AssignmentClient:
    def __init__(self, base_url: str, token: str, worker_id: str, *, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.worker_id = worker_id
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "AssignmentClient":
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "X-Worker-Token": self.token,
            "X-Worker-Id": self.worker_id,
        }

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("AssignmentClient must be used as an async context manager")
        response = await self._client.request(
            method,
            self.base_url + path,
            headers={**self._headers(), **kwargs.pop("headers", {})},
            **kwargs,
        )
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise AssignmentError(
                response.status_code,
                str(body.get("code") or "platform_error"),
                str(body.get("message") or response.text[:200]),
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def register(self, capabilities: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/workers/{self.worker_id}/register",
            json={"capabilities": dict(capabilities or {})},
        )

    async def heartbeat(self, status: str = "idle") -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/workers/{self.worker_id}/heartbeat",
            json={"status": status},
        )

    async def claim(self, lease_seconds: int = 300) -> dict[str, Any] | None:
        try:
            body = await self._request(
                "POST",
                f"/api/v1/workers/{self.worker_id}/claim",
                json={"lease_seconds": lease_seconds},
            )
        except AssignmentError as exc:
            if exc.code == "assignment_not_found":
                return None
            raise
        return body.get("assignment")

    async def attempt_heartbeat(
        self, attempt_id: str, lease_id: str, lease_seconds: int = 300
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/attempts/{attempt_id}/heartbeat",
            json={"lease_id": lease_id, "lease_seconds": lease_seconds},
        )

    async def append_events(
        self, attempt_id: str, lease_id: str, events: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/attempts/{attempt_id}/events",
            json={"lease_id": lease_id, "events": [dict(event) for event in events]},
        )

    async def complete(
        self,
        attempt_id: str,
        lease_id: str,
        *,
        status: str,
        solved: bool = False,
        flags_found: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/v1/attempts/{attempt_id}/complete",
            json={
                "lease_id": lease_id,
                "status": status,
                "solved": solved,
                "flags_found": flags_found,
                "error": error,
            },
        )
