"""摄取端点测试:token 鉴权 / 幂等 / 崩溃守卫 / roster。"""

from __future__ import annotations

from conftest import (attempt_ev, live_body, make_run_events, rid, session_ev,
                      turn_start_ev)


def _events_body(run_id: str | None = None, code: str = "a-05") -> dict:
    return {"run_id": run_id or rid(), "worker_id": "worker-1",
            "challenge_code": code, "model": "opencode-go/mimo-v2.5",
            "events": make_run_events([attempt_ev(1), session_ev(), turn_start_ev()])}


def test_token_unconfigured_loud(client_no_token, headers):
    r = client_no_token.post("/api/internal/ping", json={"worker_id": "worker-1"},
                             headers=headers)
    assert r.status_code == 503


def test_token_auth(client, headers):
    body = {"worker_id": "worker-1"}
    assert client.post("/api/internal/ping", json=body).status_code == 401          # 无头
    assert client.post("/api/internal/ping", json=body,
                       headers={"X-Observability-Token": "wrong"}).status_code == 401
    assert client.post("/api/internal/ping", json=body, headers=headers).status_code == 200


def test_ingest_roundtrip(client, headers):
    body = _events_body()
    r = client.post("/api/internal/events", json=body, headers=headers)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "inserted": 3}
    # 幂等重放
    r = client.post("/api/internal/events", json=body, headers=headers)
    assert r.json() == {"ok": True, "inserted": 0}
    # 空批不进不去
    empty = dict(body, events=[])
    assert client.post("/api/internal/events", json=empty,
                       headers=headers).json()["inserted"] == 0
    # bad run_id
    bad = dict(body, run_id="not-a-run-id")
    assert client.post("/api/internal/events", json=bad,
                       headers=headers).status_code == 400


def test_canonical_attempt_events_project_to_stable_run(client, headers):
    attempt_id = rid()
    base = {
        "schema_version": 1,
        "evaluation_id": rid(),
        "job_id": rid(),
        "attempt_id": attempt_id,
        "worker_id": "worker-1",
    }
    started = dict(base, event_id=rid(), event_type="attempt.started", seq=0,
                   occurred_at=1000.0,
                   payload={"unique_code": "a-05", "attempt_no": 1})
    completed = dict(base, event_id=rid(), event_type="attempt.completed", seq=1,
                     occurred_at=1010.0,
                     payload={"status": "solved", "solved": True, "flags_found": 1})
    response = client.post("/api/internal/canonical-events",
                           json={"events": [started, completed]}, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": 2, "skipped": 0}
    rows = client.get("/api/runs").json()["runs"]
    assert len(rows) == 1
    assert rows[0]["run_id"] == attempt_id
    assert rows[0]["attempt_id"] == attempt_id
    assert rows[0]["status"] == "solved"

def test_canonical_events_count_skipped_not_silent(client, headers):
    """未知类型/空 attempt_id/非法终态计入 skipped,不再 ok 绿灯丢数据。"""
    attempt_id = rid()
    base = {
        "schema_version": 1, "evaluation_id": rid(), "job_id": rid(),
        "attempt_id": attempt_id, "worker_id": "worker-1",
    }
    good = dict(base, event_id=rid(), event_type="attempt.started", seq=0,
                occurred_at=1000.0, payload={"unique_code": "a-05"})
    unknown_type = dict(base, event_id=rid(), event_type="attempt.frobnicate", seq=1,
                        occurred_at=1001.0, payload={})
    no_attempt = dict(base, event_id=rid(), event_type="attempt.started", seq=2,
                      occurred_at=1002.0, payload={"unique_code": "a-05"})
    del no_attempt["attempt_id"]
    bad_status = dict(base, event_id=rid(), event_type="attempt.completed", seq=3,
                      occurred_at=1003.0, payload={"status": "running"})
    response = client.post("/api/internal/canonical-events",
                           json={"events": [good, unknown_type, no_attempt, bad_status]},
                           headers=headers)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": 1, "skipped": 3}


def test_run_close_unknown_run_ok_with_trace(client, headers):
    """未知 run 的 close 仍 ok(幂等),不断言日志,只 pin 语义不碎。"""
    body = {"run_id": rid(), "worker_id": "worker-1", "status": "done"}
    assert client.post("/api/internal/run_close", json=body,
                       headers=headers).json() == {"ok": True}


def test_run_close_lifecycle(client, headers):
    body = _events_body()
    client.post("/api/internal/events", json=body, headers=headers)
    close = {"run_id": body["run_id"], "worker_id": "worker-1", "status": "solved",
             "flags_found": 1, "flags_accepted": ["flag{x}"], "turns": 5}
    assert client.post("/api/internal/run_close", json=close,
                       headers=headers).status_code == 200
    rows = client.get("/api/runs?worker=worker-1").json()["runs"]
    assert len(rows) == 1
    assert rows[0]["status"] == "solved"
    assert rows[0]["flags_accepted"] == ["flag{x}"]  # 归一化 list
    assert rows[0]["event_count"] == 3
    # 未知 run 的 close:静默 ok(幂等)
    ghost = dict(close, run_id=rid(), status="failed")
    assert client.post("/api/internal/run_close", json=ghost,
                       headers=headers).status_code == 200


def test_switch_guard_via_http(client, headers):
    r1 = _events_body(code="a-05")
    client.post("/api/internal/events", json=r1, headers=headers)
    # 同一 run 的 live;随后直接切到 b-01(未关旧 run)
    live = dict(live_body("solving", "a-05"), worker_id="worker-1")
    client.post("/api/internal/live", json=live, headers=headers)
    live2 = dict(live_body("solving", "b-01"))
    client.post("/api/internal/live", json=live2, headers=headers)
    runs = client.get("/api/runs?challenge=a-05").json()["runs"]
    assert runs[0]["status"] == "interrupted"


def test_idle_guard_via_http(client, headers):
    # 真实重启流:旧进程 live 行在(solving),events 落在 DB,随后新进程以 idle 上线
    client.post("/api/internal/live", json=live_body("solving", "a-05"), headers=headers)
    r1 = _events_body()
    client.post("/api/internal/events", json=r1, headers=headers)
    # 新进程首包 idle(prev phase=solving,ACTIVE) → 残留 running run 关为 interrupted
    client.post("/api/internal/live", json=live_body("idle", ""), headers=headers)
    runs = client.get("/api/runs?worker=worker-1").json()["runs"]
    assert runs[0]["status"] == "interrupted"
    # 纯 idle→idle(非重启)不误杀新 run
    r2 = _events_body()
    client.post("/api/internal/events", json=r2, headers=headers)
    client.post("/api/internal/live", json=live_body("idle", ""), headers=headers)
    runs = client.get("/api/runs?worker=worker-1").json()["runs"]
    assert {r["status"] for r in runs} == {"interrupted", "running"}


def test_roster_post_and_read(client, headers):
    snap = {"fetched_at": 1_700.0, "stale": False, "platform_error": "",
            "platform_disabled": False,
            "challenges": {"a-05": {"unique_code": "a-05", "difficulty": "easy",
                                    "total_score": 100}}}
    r = client.post("/api/internal/roster",
                    json={"worker_id": "worker-1", "snapshot": snap}, headers=headers)
    assert r.status_code == 200
    assert client.get("/api/roster").json() == snap
    # 平台段/字段镜像到列
    snap2 = dict(snap, fetched_at=1_800.0, stale=True, platform_error="boom")
    client.post("/api/internal/roster",
                json={"worker_id": "worker-1", "snapshot": snap2}, headers=headers)
    out = client.get("/api/roster").json()
    assert out["fetched_at"] == 1_800.0 and out["stale"] is True
    assert out["platform_error"] == "boom"


def test_live_and_ping_http(client, headers):
    live = dict(live_body("solving", "a-05"), snapshot={
        **dict(live_body("solving", "a-05"))["snapshot"], "transcript_path": "/work/a-05/x"})
    r = client.post("/api/internal/live", json=live, headers=headers)
    assert r.json() == {"ok": True}
    assert client.post("/api/internal/ping", json={"worker_id": "worker-1"},
                       headers=headers).json() == {"ok": True}
    assert client.get("/api/status").json()["phase"] == "solving"
