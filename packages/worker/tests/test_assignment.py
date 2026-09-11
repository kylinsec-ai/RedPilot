"""assignment 客户端协议回归。"""

from __future__ import annotations

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from ghost_worker.assignment import AssignmentClient


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        body = self._body()
        self.server.calls.append((self.path, dict(self.headers), body))  # type: ignore[attr-defined]
        if self.path.endswith("/claim"):
            if self.server.claimed:  # type: ignore[attr-defined]
                self._json(404, {"code": "assignment_not_found", "message": "empty"})
            else:
                self.server.claimed = True  # type: ignore[attr-defined]
                self._json(200, {"assignment": {
                    "evaluation_id": "e1", "job_id": "j1", "attempt_id": "a1",
                    "lease_id": "l1", "unique_code": "c1", "benchmark_token": "t1",
                }})
            return
        self._json(200, {"ok": True})


def test_assignment_client_uses_worker_scope_and_handles_empty_queue():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.calls = []
    server.claimed = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        async with AssignmentClient(
            f"http://127.0.0.1:{server.server_address[1]}", "secret", "worker-1"
        ) as client:
            await client.register({"solver": "pi"})
            assignment = await client.claim(60)
            assert assignment["attempt_id"] == "a1"
            await client.attempt_heartbeat("a1", "l1", 60)
            await client.append_events("a1", "l1", [{"seq": 1, "event_type": "progress"}])
            await client.complete("a1", "l1", status="done")
            assert await client.claim(60) is None

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()

    assert server.calls
    assert all(call[1]["X-Worker-Token"] == "secret" for call in server.calls)
    assert all(call[1]["X-Worker-Id"] == "worker-1" for call in server.calls)


def test_assignment_client_sends_lease_in_body_and_hits_expected_paths():
    """lease 走 JSON body(不在 URL 里);各方法路径/动词可断言,防静默改道。"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.calls = []
    server.claimed = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        async with AssignmentClient(
            f"http://127.0.0.1:{server.server_address[1]}", "secret", "worker-1"
        ) as client:
            await client.register({"solver": "pi"})
            await client.claim(60)
            await client.attempt_heartbeat("a1", "l1", 60)
            await client.append_events("a1", "l1", [{"seq": 1, "event_type": "progress"}])
            await client.complete("a1", "l1", status="done")

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()

    by_path = {call[0].split("?")[0]: call for call in server.calls}
    assert "/api/v1/workers/worker-1/register" in by_path
    assert "/api/v1/workers/worker-1/claim" in by_path
    assert "/api/v1/attempts/a1/heartbeat" in by_path
    assert "/api/v1/attempts/a1/events" in by_path
    assert "/api/v1/attempts/a1/complete" in by_path
    events_call = by_path["/api/v1/attempts/a1/events"]
    assert "lease_id" not in events_call[0]  # query 串里不得出现 bearer
    assert events_call[2]["lease_id"] == "l1"
    assert events_call[2]["events"][0]["event_type"] == "progress"
    assert by_path["/api/v1/attempts/a1/complete"][2]["lease_id"] == "l1"


def test_assignment_client_maps_auth_and_server_errors():
    """401/500 必须抛 AssignmentError(带 code);调用方据此分流 exit/重试。"""
    from ghost_worker.assignment import AssignmentError

    class _ErrHandler(_Handler):
        def do_POST(self):  # noqa: N802
            body = self._body()
            self.server.calls.append((self.path, dict(self.headers), body))  # type: ignore[attr-defined]
            if self.path.endswith("/claim"):
                self._json(401, {"code": "worker_token_required", "message": "bad token"})
            else:
                self._json(500, {"code": "internal_error", "message": "boom"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ErrHandler)
    server.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        async with AssignmentClient(
            f"http://127.0.0.1:{server.server_address[1]}", "bad", "worker-1"
        ) as client:
            try:
                await client.claim(60)
            except AssignmentError as exc:
                assert exc.status_code == 401
                assert exc.code == "worker_token_required"
            else:
                raise AssertionError("claim 401 must raise")
            try:
                await client.attempt_heartbeat("a1", "l1", 60)
            except AssignmentError as exc:
                assert exc.status_code == 500
            else:
                raise AssertionError("heartbeat 500 must raise")

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()


def test_assignment_client_requires_context_manager():
    """未进 async with 即调用 → RuntimeError(防 client 未初始化空转)。"""
    import pytest

    from ghost_worker.assignment import AssignmentError  # noqa: F401

    async def run():
        client = AssignmentClient("http://127.0.0.1:1", "t", "w1")
        with pytest.raises(RuntimeError):
            await client.claim(60)

    asyncio.run(run())
