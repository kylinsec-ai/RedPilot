"""canonical 事件端到端:控制面 outbox → 观测平台 → runs 终态。

这是合并架构最核心的不变量 —— canonical 事件是唯一权威终态。合并初期统一 app
的 outbox 从未启动(见 test_unified_app.py),该链路整体断裂却无任何测试发现:
obs 侧 run 永远停在 running,而所有单元测试照样全绿。

本文件用 httpx ASGITransport 在进程内跑通全链路(不需起真实服务):
    create_evaluation → claim → complete → outbox 投递 → obs runs 终态可查
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
import httpx
import pytest

from ghost.app import create_app
from ghost.control.config import Settings as ControlSettings
from ghost.control.outbox import dispatch_outbox_loop
from ghost.obs.config import Settings as ObsSettings

TASK_TOKEN = "task-canonical"
ADMIN_TOKEN = "admin-canonical"
WORKER_TOKEN = "worker-canonical"
OBS_TOKEN = "obs-canonical"

# 观测读端凭据(回落 ingest token)
READ = {"X-Observability-Token": OBS_TOKEN}


def _app(tmp_path, web_dir):
    return create_app(
        control_settings=ControlSettings(
            database_path=str(tmp_path / "control.sqlite3"),
            benchmark_token=TASK_TOKEN,
            admin_token=ADMIN_TOKEN,
            worker_token=WORKER_TOKEN,
        ),
        obs_settings=ObsSettings(
            obs_token=OBS_TOKEN,
            db_path=str(tmp_path / "obs.sqlite3"),
            web_dir=str(web_dir),
        ),
        tasks={
            "token": TASK_TOKEN,
            "challenges": [
                {
                    "unique_code": "web-01",
                    "description": "canonical e2e",
                    "flags": ["flag{e2e}"],
                    "container_addr": ["10.0.0.1:80"],
                }
            ],
        },
    )


@pytest.fixture()
def web_dir(tmp_path):
    d = tmp_path / "web"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return d


async def _drain_outbox(app) -> None:
    """用指向本 app 的 ASGI transport 跑满两轮投递(排空 outbox)。"""
    transport = httpx.ASGITransport(app=app)
    await dispatch_outbox_loop(
        app.state.control_store,
        "http://obs.internal",
        OBS_TOKEN,
        client_factory=lambda: httpx.AsyncClient(timeout=5.0, transport=transport),
        max_rounds=2,
    )


def test_canonical_completed_reaches_obs(tmp_path, web_dir):
    """attempt.completed 必须经 outbox 抵达 obs,并把 run 关成 canonical 终态。"""
    app = _app(tmp_path, web_dir)
    with TestClient(app, headers=READ) as client:
        admin = {"GHOST_ADMIN_TOKEN": ADMIN_TOKEN}
        worker = {"X-Worker-Token": WORKER_TOKEN}

        created = client.post("/api/v1/evaluations", headers=admin,
                              json={"task_token": TASK_TOKEN, "idempotency_key": "e2e"})
        assert created.status_code == 200
        assert created.json()["job_count"] == 1

        assert client.post("/api/v1/workers/worker-1/register", headers=worker,
                           json={"capabilities": {}}).status_code == 200
        claimed = client.post("/api/v1/workers/worker-1/claim", headers=worker,
                              json={"lease_seconds": 60})
        assignment = claimed.json()["assignment"]
        attempt_id = assignment["attempt_id"]

        # 尚未投递:控制面 outbox 里已有 attempt.started 待发
        assert app.state.control_store.pending_outbox(100), "attempt.started 未入 outbox"

        worker_with_id = {**worker, "X-Worker-Id": "worker-1"}
        done = client.post(
            f"/api/v1/attempts/{attempt_id}/complete",
            headers=worker_with_id,
            json={"lease_id": assignment["lease_id"], "status": "solved",
                  "solved": True, "flags_found": 1},
        )
        assert done.status_code == 200

        # 投递前:obs 侧一片空白(权威终态尚未送达)
        assert client.get("/api/runs").json()["runs"] == []

        asyncio.run(_drain_outbox(app))

        runs = client.get("/api/runs").json()["runs"]
        assert len(runs) == 1, f"期望恰好一条 run,实得 {len(runs)}"
        run = runs[0]
        assert run["status"] == "solved", "canonical 终态未落库"
        assert run["canonical"] is True, "该行未标记为权威"
        assert run["attempt_id"] == attempt_id, "run 未按 attempt_id 归一"
        assert run["run_id"] == attempt_id

        # outbox 已排空(投递成功即删行)
        assert app.state.control_store.pending_outbox(100) == []


def test_canonical_started_creates_running_run(tmp_path, web_dir):
    """attempt.started 单独投递时先建 running 行(乱序/中途态场景)。"""
    app = _app(tmp_path, web_dir)
    with TestClient(app, headers=READ) as client:
        admin = {"GHOST_ADMIN_TOKEN": ADMIN_TOKEN}
        worker = {"X-Worker-Token": WORKER_TOKEN}
        client.post("/api/v1/evaluations", headers=admin,
                    json={"task_token": TASK_TOKEN, "idempotency_key": "start"})
        client.post("/api/v1/workers/worker-1/register", headers=worker,
                    json={"capabilities": {}})
        assignment = client.post("/api/v1/workers/worker-1/claim", headers=worker,
                                 json={"lease_seconds": 60}).json()["assignment"]

        asyncio.run(_drain_outbox(app))

        runs = client.get("/api/runs").json()["runs"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == assignment["attempt_id"]
        assert runs[0]["status"] == "running"
        assert runs[0]["challenge_code"] == "web-01"
