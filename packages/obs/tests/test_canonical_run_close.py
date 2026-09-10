"""canonical 终态权威切片:relay 与 canonical 冲突/乱序/回落场景。"""

from __future__ import annotations

from conftest import TOKEN, make_run_events, rid, session_ev


def _events_body(run_id: str, attempt_id: str | None = None, code: str = "a-05") -> dict:
    return {
        "run_id": run_id,
        "worker_id": "worker-1",
        "challenge_code": code,
        "model": "opencode-go/mimo-v2.5",
        "attempt_id": attempt_id,
        "events": make_run_events([session_ev()]),
    }


def _canonical_started(attempt_id: str, code: str = "a-05") -> dict:
    return {
        "schema_version": 1,
        "event_id": rid(),
        "event_type": "attempt.started",
        "evaluation_id": rid(),
        "job_id": rid(),
        "attempt_id": attempt_id,
        "worker_id": "worker-1",
        "seq": 0,
        "occurred_at": 1000.0,
        "payload": {"unique_code": code, "attempt_no": 1, "model": "gpt-4"},
    }


def _canonical_completed(attempt_id: str, status: str = "solved", **payload_override) -> dict:
    payload = {"status": status}
    payload.update(payload_override)
    return {
        "schema_version": 1,
        "event_id": rid(),
        "event_type": "attempt.completed",
        "evaluation_id": rid(),
        "job_id": rid(),
        "attempt_id": attempt_id,
        "worker_id": "worker-1",
        "seq": 1,
        "occurred_at": 1010.0,
        "payload": payload,
    }


# ── 基础场景:relay 先建 running 行,canonical 后关闭 ──

def test_canonical_closes_relay_run_same_id(client, headers):
    """relay run_id == attempt_id 时,canonical completed 直接命中并关闭。"""
    attempt_id = rid()
    # relay 先建 running 行
    body = _events_body(attempt_id, attempt_id=attempt_id)
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    # canonical 到达
    r = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "done")]},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["processed"] == 1
    run = client.get(f"/api/runs/{attempt_id}").json()
    assert run["status"] == "done"
    assert run["canonical"] is True  # v4 列暴露为 bool


def test_canonical_closes_relay_run_different_id(client, headers):
    """relay run_id != attempt_id 时,canonical 通过 attempt_id 回落关联并关闭。"""
    relay_run_id = rid()
    attempt_id = rid()
    body = _events_body(relay_run_id, attempt_id=attempt_id)
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    r = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "solved", flags_found=1)]},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["processed"] == 1
    # 应关闭 relay 行(因为 run_id=attempt_id 不存在)
    run = client.get(f"/api/runs/{relay_run_id}").json()
    assert run["status"] == "solved"
    assert run["attempt_id"] == attempt_id
    assert run["canonical"] is True


# ── 权威不可覆盖:relay run_close 不能覆盖 canonical 终态 ──

def test_relay_run_close_cannot_override_canonical(client, headers):
    """canonical 已关闭后,relay run_close 只能静默失败,不能改状态。"""
    attempt_id = rid()
    # canonical 直接建并关
    events = [_canonical_started(attempt_id), _canonical_completed(attempt_id, "done")]
    assert client.post("/api/internal/canonical-events", json={"events": events},
                       headers=headers).status_code == 200
    # relay 试图覆盖为 solved
    close_body = {
        "run_id": attempt_id,
        "worker_id": "worker-1",
        "status": "solved",
        "flags_found": 99,
    }
    r = client.post("/api/internal/run_close", json=close_body, headers=headers)
    assert r.status_code == 200  # 静默幂等(不碎)
    run = client.get(f"/api/runs/{attempt_id}").json()
    assert run["status"] == "done"
    assert run["flags_found"] is None  # relay 的 99 未写入


def test_relay_run_close_cannot_override_canonical_different_id(client, headers):
    """run_id != attempt_id 时,relay 对 canonical 终态的覆盖同样被拒绝。"""
    relay_run_id = rid()
    attempt_id = rid()
    # relay 建 running 行
    body = _events_body(relay_run_id, attempt_id=attempt_id)
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    # canonical 关闭(通过 attempt_id 回落)
    r = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "failed", error="boom")]},
        headers=headers,
    )
    assert r.status_code == 200
    # relay 试图通过原 run_id 覆盖
    close_body = {
        "run_id": relay_run_id,
        "worker_id": "worker-1",
        "status": "solved",
    }
    r = client.post("/api/internal/run_close", json=close_body, headers=headers)
    assert r.status_code == 200
    run = client.get(f"/api/runs/{relay_run_id}").json()
    assert run["status"] == "failed"
    assert run["error"] == "boom"


# ── 幂等与乱序 ──

def test_canonical_completed_idempotent(client, headers):
    """同一 attempt.completed 重复投递,状态不变且仍返回 processed=1。"""
    attempt_id = rid()
    events = [_canonical_started(attempt_id), _canonical_completed(attempt_id, "solved")]
    assert client.post("/api/internal/canonical-events", json={"events": events},
                       headers=headers).status_code == 200
    # 再次投递同一 completed
    r = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "solved")]},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["processed"] == 1
    run = client.get(f"/api/runs/{attempt_id}").json()
    assert run["status"] == "solved"


def test_canonical_completed_before_started(client, headers):
    """attempt.completed 先于 attempt.started 到达:同一 attempt 只留一行,
    后补 started 不另起 running 行、不覆盖终态。"""
    relay_run_id = rid()
    attempt_id = rid()
    # relay 先建 running 行
    body = _events_body(relay_run_id, attempt_id=attempt_id)
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    # completed 先到(无 started)
    r = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "done")]},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["processed"] == 1
    # started 后补
    r2 = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_started(attempt_id)]},
        headers=headers,
    )
    assert r2.status_code == 200 and r2.json()["processed"] == 1
    # relay 行应被关闭且标 canonical
    run = client.get(f"/api/runs/{relay_run_id}").json()
    assert run["status"] == "done"
    assert run["canonical"] is True
    # 同一 attempt 只留一行:后补 started 复用 relay 行,不另起 run_id=attempt_id 行
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == relay_run_id
    assert runs[0]["status"] == "done"


def test_canonical_overrides_relay_terminal(client, headers):
    """relay 先关为终态,canonical 后到达时覆盖为权威终态。"""
    relay_run_id = rid()
    attempt_id = rid()
    # relay 建 running 行并关闭为 solved
    body = _events_body(relay_run_id, attempt_id=attempt_id)
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    assert client.post("/api/internal/run_close", json={
        "run_id": relay_run_id,
        "worker_id": "worker-1",
        "status": "solved",
        "flags_found": 1,
    }, headers=headers).status_code == 200
    # canonical 到达,应覆盖为 done
    r = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "done")]},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["processed"] == 1
    run = client.get(f"/api/runs/{relay_run_id}").json()
    assert run["status"] == "done"
    assert run["canonical"] is True


# ── 边缘:无 attempt_id 的 canonical completed ──

def test_canonical_completed_without_attempt_id_skipped(client, headers):
    """canonical completed 缺失 attempt_id 时不应导致 500,应计入 skipped。"""
    bad = _canonical_completed(rid())
    del bad["attempt_id"]
    r = client.post("/api/internal/canonical-events", json={"events": [bad]},
                    headers=headers)
    assert r.status_code == 200 and r.json()["skipped"] == 1


# ── 回归:乱序 completed 无行可关 → 占位终态行(不丢失) ──

def test_canonical_completed_without_any_row_creates_terminal(client, headers):
    """无 relay、无 started 时 completed 独自到达:必须建占位终态行保留终态。"""
    attempt_id = rid()
    r = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "failed", error="boom")]},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["processed"] == 1
    run = client.get(f"/api/runs/{attempt_id}").json()
    assert run["status"] == "failed"
    assert run["error"] == "boom"
    assert run["canonical"] is True
    assert run["attempt_id"] == attempt_id
    # 后补 started 不得覆盖终态、不另起行
    r2 = client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_started(attempt_id)]},
        headers=headers,
    )
    assert r2.status_code == 200
    run2 = client.get(f"/api/runs/{attempt_id}").json()
    assert run2["status"] == "failed"
    assert run2["canonical"] is True
    assert run2["challenge_code"] == "a-05"  # 占位 unknown 被后补 started 纠正
    assert len(client.get("/api/runs").json()["runs"]) == 1


def test_canonical_started_after_completed_same_id_preserves_terminal(client, headers):
    """同 ID 下 completed→started 乱序:started 复用终态行,不重置为 running。"""
    attempt_id = rid()
    assert client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "solved")]},
        headers=headers).status_code == 200
    assert client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_started(attempt_id)]},
        headers=headers).status_code == 200
    run = client.get(f"/api/runs/{attempt_id}").json()
    assert run["status"] == "solved"
    assert run["canonical"] is True
    assert len(client.get("/api/runs").json()["runs"]) == 1


def test_relay_events_after_canonical_started_reuse_same_row(client, headers):
    """canonical started 先建行,relay 后以不同 run_id 同 attempt_id 补事件:
    必须收敛到同一 run,不另起一行。"""
    attempt_id = rid()
    relay_run_id = rid()
    assert client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_started(attempt_id)]},
        headers=headers).status_code == 200
    body = _events_body(relay_run_id, attempt_id=attempt_id)
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run_id"] == attempt_id
    assert runs[0]["event_count"] == 1
    assert runs[0]["status"] == "running"
    # relay 行从未建:按 relay_run_id 查应 404
    assert client.get(f"/api/runs/{relay_run_id}").status_code == 404


def test_relay_run_close_with_attempt_pointing_to_canonical_rejected(client, headers):
    """relay run_close 带 attempt_id 指向已 canonical 的另一行:一律拒写。"""
    attempt_id = rid()
    relay_run_id = rid()
    body = _events_body(relay_run_id, attempt_id=attempt_id)
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    assert client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_completed(attempt_id, "done")]},
        headers=headers).status_code == 200
    # relay 以同 run_id + attempt_id 双字段试图覆盖为 solved
    r = client.post("/api/internal/run_close", json={
        "run_id": relay_run_id,
        "worker_id": "worker-1",
        "status": "solved",
        "attempt_id": attempt_id,
        "flags_found": 9,
    }, headers=headers)
    assert r.status_code == 200
    run = client.get(f"/api/runs/{relay_run_id}").json()
    assert run["status"] == "done"
    assert run["canonical"] is True
    assert run["flags_found"] is None


def test_relay_run_close_repeat_after_canonical_stays_rejected(client, headers):
    """relay 重复投递 run_close 同样不能覆盖 canonical(幂等拒写)。"""
    attempt_id = rid()
    assert client.post(
        "/api/internal/canonical-events",
        json={"events": [_canonical_started(attempt_id),
                           _canonical_completed(attempt_id, "failed", error="x")]},
        headers=headers).status_code == 200
    for _ in range(2):
        r = client.post("/api/internal/run_close", json={
            "run_id": attempt_id, "worker_id": "worker-1", "status": "solved",
        }, headers=headers)
        assert r.status_code == 200
    run = client.get(f"/api/runs/{attempt_id}").json()
    assert run["status"] == "failed"
    assert run["error"] == "x"


def test_canonical_started_idempotent_no_duplicate(client, headers):
    """canonical started 重复投递不另起行、不改终态前状态。"""
    attempt_id = rid()
    for _ in range(2):
        assert client.post(
            "/api/internal/canonical-events",
            json={"events": [_canonical_started(attempt_id)]},
            headers=headers).status_code == 200
    runs = client.get("/api/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "running"
    assert runs[0]["canonical"] is False


def test_legacy_telemetry_without_attempt_unaffected(client, headers):
    """无 attempt_id 的 legacy 链路不受归一影响:建行/关行照旧。"""
    run_id = rid()
    body = _events_body(run_id, attempt_id=None)
    body.pop("attempt_id", None)
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    assert client.post("/api/internal/run_close", json={
        "run_id": run_id, "worker_id": "worker-1", "status": "solved",
    }, headers=headers).status_code == 200
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "solved"
    assert run["canonical"] is False
