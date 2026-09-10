"""控制面 assignment API 的端到端回归。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from ghost.api import create_app
from ghost.config import Settings


TASK_TOKEN = "task-control"
ADMIN_TOKEN = "admin-control"
WORKER_TOKEN = "worker-control"


def _client(tmp_path) -> TestClient:
    settings = Settings(
        database_path=str(tmp_path / "control.sqlite3"),
        benchmark_token=TASK_TOKEN,
        admin_token=ADMIN_TOKEN,
        worker_token=WORKER_TOKEN,
        public_base_url="http://platform:8000",
    )
    app = create_app(
        settings=settings,
        tasks={
            "token": TASK_TOKEN,
            "challenges": [
                {
                    "unique_code": "web-01",
                    "description": "control-plane test",
                    "flags": ["flag{control}"],
                    "container_addr": ["10.0.0.1:80"],
                }
            ],
        },
    )
    return TestClient(app)


def test_evaluation_claim_heartbeat_events_and_complete(tmp_path):
    with _client(tmp_path) as client:
        admin = {"GHOST_ADMIN_TOKEN": ADMIN_TOKEN}
        worker = {"X-Worker-Token": WORKER_TOKEN}

        created = client.post(
            "/api/v1/evaluations",
            headers=admin,
            json={"task_token": TASK_TOKEN, "idempotency_key": "once"},
        )
        assert created.status_code == 200
        evaluation = created.json()
        assert evaluation["job_count"] == 1
        assert evaluation["status"] == "queued"

        retry = client.post(
            "/api/v1/evaluations",
            headers=admin,
            json={"task_token": TASK_TOKEN, "idempotency_key": "once"},
        )
        assert retry.json()["evaluation_id"] == evaluation["evaluation_id"]

        assert client.post(
            "/api/v1/workers/worker-1/register",
            headers=worker,
            json={"capabilities": {"solver": "pi"}},
        ).status_code == 200
        claimed = client.post(
            "/api/v1/workers/worker-1/claim",
            headers=worker,
            json={"lease_seconds": 60},
        )
        assert claimed.status_code == 200
        assignment = claimed.json()["assignment"]
        assert assignment["unique_code"] == "web-01"
        assert assignment["benchmark_token"] == TASK_TOKEN
        assert assignment["benchmark_base_url"] == "http://platform:8000"

        worker_with_id = {**worker, "X-Worker-Id": "worker-1"}
        heartbeat = client.post(
            f"/api/v1/attempts/{assignment['attempt_id']}/heartbeat",
            headers=worker_with_id,
            json={"lease_id": assignment["lease_id"], "lease_seconds": 60},
        )
        assert heartbeat.status_code == 200

        event = client.post(
            f"/api/v1/attempts/{assignment['attempt_id']}/events",
            headers=worker_with_id,
            json={
                "lease_id": assignment["lease_id"],
                "events": [
                    {
                        "event_id": "worker-event-1",
                        "event_type": "attempt.progress",
                        "seq": 1,
                        "payload": {"phase": "solving"},
                    }
                ],
            },
        )
        assert event.status_code == 200
        assert event.json()["inserted"] == 1

        completed = client.post(
            f"/api/v1/attempts/{assignment['attempt_id']}/complete",
            headers=worker_with_id,
            json={
                "lease_id": assignment["lease_id"],
                "status": "solved",
                "solved": True,
                "flags_found": 1,
            },
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "solved"

        final = client.get(
            f"/api/v1/evaluations/{evaluation['evaluation_id']}", headers=admin
        )
        assert final.json()["status"] == "completed"
        assert final.json()["completed_count"] == 1

        events = client.get(
            f"/api/v1/attempts/{assignment['attempt_id']}/events", headers=admin
        )
        assert [event["event_type"] for event in events.json()["events"]] == [
            "attempt.started",
            "attempt.progress",
            "attempt.completed",
        ]


def test_worker_api_fails_closed_without_worker_token(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "control.sqlite3"),
        benchmark_token=TASK_TOKEN,
        admin_token=ADMIN_TOKEN,
        worker_token=None,
    )
    client = TestClient(create_app(settings=settings, tasks={"token": TASK_TOKEN, "challenges": []}))
    response = client.post(
        "/api/v1/workers/worker-1/register",
        json={"capabilities": {}},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "worker_token_not_configured"

def test_second_claim_gets_empty_while_job_running(tmp_path):
    """lost-update 回归:job 被 worker-1 领走后,worker-2 的 claim 必须为空
    (UPDATE …WHERE status='pending' 命中 0 行 → None → 404,不产生第二个 attempt)。"""
    with _client(tmp_path) as client:
        admin = {"GHOST_ADMIN_TOKEN": ADMIN_TOKEN}
        worker = {"X-Worker-Token": WORKER_TOKEN}
        client.post("/api/v1/evaluations", headers=admin, json={"task_token": TASK_TOKEN})
        client.post("/api/v1/workers/worker-1/register", headers=worker,
                    json={"capabilities": {}})
        client.post("/api/v1/workers/worker-2/register", headers=worker,
                    json={"capabilities": {}})
        first = client.post("/api/v1/workers/worker-1/claim", headers=worker,
                            json={"lease_seconds": 60})
        assert first.status_code == 200
        second = client.post("/api/v1/workers/worker-2/claim", headers=worker,
                             json={"lease_seconds": 60})
        assert second.status_code == 404
        assert second.json()["code"] == "assignment_not_found"


def test_attempt_endpoints_reject_wrong_lease_and_bad_status(tmp_path):
    """错 lease → 409;非法终态 → 422;非对象 payload → 422(不静默包裝)。"""
    with _client(tmp_path) as client:
        admin = {"GHOST_ADMIN_TOKEN": ADMIN_TOKEN}
        worker = {"X-Worker-Token": WORKER_TOKEN}
        evaluation = client.post(
            "/api/v1/evaluations", headers=admin, json={"task_token": TASK_TOKEN}
        ).json()
        client.post("/api/v1/workers/worker-1/register", headers=worker,
                    json={"capabilities": {}})
        claimed = client.post("/api/v1/workers/worker-1/claim", headers=worker,
                              json={"lease_seconds": 60}).json()["assignment"]
        attempt_id = claimed["attempt_id"]
        wid = {"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-1"}

        bad_lease = client.post(
            f"/api/v1/attempts/{attempt_id}/heartbeat", headers=wid,
            json={"lease_id": "wrong-lease", "lease_seconds": 60},
        )
        assert bad_lease.status_code == 409

        bad_status = client.post(
            f"/api/v1/attempts/{attempt_id}/complete", headers=wid,
            json={"lease_id": claimed["lease_id"], "status": "whatever"},
        )
        assert bad_status.status_code == 422

        bad_payload = client.post(
            f"/api/v1/attempts/{attempt_id}/events", headers=wid,
            json={"lease_id": claimed["lease_id"],
                  "events": [{"event_type": "attempt.progress", "seq": 1,
                              "payload": "not-an-object"}]},
        )
        # pydantic 层即拒非对象 payload(422),到不了存储层。
        assert bad_payload.status_code == 422


def test_evaluation_payload_omits_task_token(tmp_path):
    """evaluation 面向浏览器:task_token 不得下发(只经 claim 给 worker)。"""
    with _client(tmp_path) as client:
        admin = {"GHOST_ADMIN_TOKEN": ADMIN_TOKEN}
        created = client.post(
            "/api/v1/evaluations", headers=admin, json={"task_token": TASK_TOKEN}
        )
        assert created.status_code == 200
        assert "task_token" not in created.json()
        listed = client.get("/api/v1/evaluations", headers=admin)
        assert listed.status_code == 200
        assert all("task_token" not in row for row in listed.json()["evaluations"])
