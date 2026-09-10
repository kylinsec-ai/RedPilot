"""HTTP 读端测试:静态/健康 / roster-challenge 契约 / runs 分页 / SSE。"""

from __future__ import annotations

import json
import threading

import pytest

from conftest import (TOKEN, assistant_msg_ev, live_body, make_run_events, rid,
                      roster_snap, session_ev, tool_end_ev, tool_start_ev,
                      turn_end_ev, turn_start_ev, user_msg_ev)
from ghost.obs.app import create_app
from fastapi.testclient import TestClient
from ghost.obs.bus import LiveBus


def _ingest(client, headers, events, code="a-05", model=""):
    body = {"run_id": rid(), "worker_id": "worker-1", "challenge_code": code,
            "model": model, "events": make_run_events(events)}
    assert client.post("/api/internal/events", json=body, headers=headers).status_code == 200
    return body["run_id"]


def _close(client, headers, rid, status="solved", flags=None):
    body = {"run_id": rid, "worker_id": "worker-1", "status": status}
    if flags:
        body.update({"flags_found": len(flags), "flags_accepted": flags})
    assert client.post("/api/internal/run_close", json=body, headers=headers).status_code == 200


# ── 静态 + 健康 ──

def test_static_and_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.text == "ok"
    r = client.get("/")
    assert r.status_code == 200 and "spa" in r.text
    assert r.headers["cache-control"] == "no-store"  # index 热更:恒不缓存
    assert client.get("/index.html").status_code == 200
    r = client.get("/assets/index-x.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    # vite 产物名带内容 hash → 可安全不可变缓存;只有 index.html 走 no-store
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_asset_guards(web_dir):
    """穿越守卫(直调:httpx 客户端会先归一化 ../,HTTP 层测不到真形态)。"""
    from types import SimpleNamespace
    from starlette.requests import Request
    from ghost.obs.read import asset

    def req(name: str) -> dict:
        app = SimpleNamespace(state=SimpleNamespace(web_dir=web_dir))
        r = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                     "query_string": b"", "app": app})
        return asset(name, r)

    for bad in ("../../index.html", "a/b.js", "a\\b.js", "x\x00.js", "",
                "..", "x.sh", "missing.js"):
        with pytest.raises(Exception) as ei:
            req(bad)
        assert getattr(ei.value, "status_code", None) == 404, bad
    ok = req("index-x.js")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/javascript")


def test_index_requires_web_dir(tmp_path):
    with TestClient(create_app(db_path=str(tmp_path / "obs.sqlite3"),
                               web_dir=None, obs_token="tok")) as c:
        assert c.get("/").status_code == 404


# ── roster / challenge 契约 ──

def test_roster_challenge_contracts(client, headers):
    snap = roster_snap({"a-05": {"unique_code": "a-05", "difficulty": "easy",
                                 "description": "desc", "total_score": 100,
                                 "flag_count": 2, "correct_flag_count": 1,
                                 "is_completed": False, "container_status": "running",
                                 "container_addr": ["10.0.0.5:80"], "level": 1,
                                 "local": {"dir": "a-05", "flag": True, "crashed": False}}})
    assert client.post("/api/internal/roster",
                       json={"worker_id": "worker-1", "snapshot": snap},
                       headers=headers).status_code == 200
    # /api/roster = 存储快照逐字
    assert client.get("/api/roster").json() == snap
    # /api/challenge 含平台行 + local + flags
    rid = _ingest(client, headers, [session_ev(), user_msg_ev()])
    _close(client, headers, rid, flags=["flag{a}"])
    detail = client.get("/api/challenge?code=a-05").json()
    assert detail["unique_code"] == "a-05"
    assert detail["description"] == "desc"
    assert detail["local"]["flag"] is True
    assert detail["flags"] == ["flag{a}"]
    assert client.get("/api/challenge?code=never").json()["local_only"] is True
    assert client.get("/api/challenge?code=bad code!").status_code == 400


# ── transcript / timeline / status ──

def test_transcript_and_timeline(client, headers):
    events = [session_ev(), user_msg_ev(), turn_start_ev(),
              tool_start_ev("c1", "bash", "ls"), tool_end_ev("c1", "out"),
              assistant_msg_ev("完成")]
    rid = _ingest(client, headers, events)
    _close(client, headers, rid, status="done")
    lines = client.get("/api/transcript?code=a-05").json()["lines"]
    assert len(lines) == 6
    assert json.loads(lines[0])["type"] == "session"
    assert client.get("/api/transcript?code=a-05&tail=2").json()["lines"] == lines[-2:]
    tl = client.get("/api/timeline?code=a-05").json()
    assert tl["next_seq"] > 0
    assert tl["meta"]["sessions"] == 1
    assert tl["entries"][0]["kind"] == "session"
    inc = client.get("/api/timeline?code=a-05&after=1").json()
    assert [e["seq"] for e in inc["entries"]] == list(range(1, tl["next_seq"]))
    assert client.get("/api/timeline?code=a 05").status_code == 400
    assert client.get("/api/status").json() == {}


def test_timeline_live_flag(client, headers):
    _ingest(client, headers, [session_ev()])
    client.post("/api/internal/live", json=live_body("solving", "a-05"), headers=headers)
    tl = client.get("/api/timeline?code=a-05").json()
    assert tl["meta"]["live"] is True and tl["meta"]["abrupt"] is False
    client.post("/api/internal/live", json=live_body("idle", ""), headers=headers)
    assert client.get("/api/timeline?code=a-05").json()["meta"]["live"] is False


# ── runs 历史 ──

def test_runs_filters_and_bad_params(client, headers):
    for i, code in enumerate(["a-01", "a-02", "b-01"]):
        rid = _ingest(client, headers, [session_ev()], code=code)
        _close(client, headers, rid, status="solved" if i == 0 else "done")
    all_runs = client.get("/api/runs").json()["runs"]
    assert len(all_runs) == 3
    assert all("event_count" in r for r in all_runs)
    by_code = client.get("/api/runs?challenge=a-01").json()["runs"]
    assert len(by_code) == 1 and by_code[0]["status"] == "solved"
    assert client.get("/api/runs?status=running").json()["runs"] == []
    assert client.get("/api/runs?status=bogus").status_code == 400
    assert client.get("/api/runs?worker=a/b").status_code == 400
    assert client.get("/api/runs?limit=1000").status_code == 422  # 参数上界


def test_runs_detail_events_and_timeline(client, headers):
    events = [session_ev(), user_msg_ev(), turn_start_ev(),
              tool_start_ev("c1", "bash", "curl http://x"),
              tool_end_ev("c1", "done-out"),
              assistant_msg_ev("看到"), turn_end_ev(tokens=9, stop="end_turn")]
    rid = _ingest(client, headers, events)
    _close(client, headers, rid, status="solved", flags=["flag{ok}"])
    d = client.get(f"/api/runs/{rid}").json()
    assert d["status"] == "solved"
    assert d["flags_accepted"] == ["flag{ok}"]
    assert d["event_count"] == 7
    page1 = client.get(f"/api/runs/{rid}/events?limit=3").json()
    assert len(page1["events"]) == 3
    assert page1["next_seq"] == 3 and page1["end"] is False
    page2 = client.get(f"/api/runs/{rid}/events?after=3&limit=3").json()
    assert len(page2["events"]) == 3 and page2["end"] is False   # 剩 1 条(seq 6)
    assert page2["events"][0]["seq"] == 3
    page3 = client.get(f"/api/runs/{rid}/events?after=6&limit=3").json()
    assert len(page3["events"]) == 1 and page3["end"] is True
    assert page3["next_seq"] == 7
    tl = client.get(f"/api/runs/{rid}/timeline").json()
    assert tl["meta"]["sessions"] == 1
    kinds = [e["kind"] for e in tl["entries"]]
    assert "tool" in kinds and "text" in kinds
    assert client.get("/api/runs/nope").status_code == 400
    assert client.get(f"/api/runs/{'f' * 32}").status_code == 404
    assert client.get(f"/api/runs/{'f' * 32}/events").status_code == 404


# ── SSE(真实 TCP:TestClient/ASGI 传输会整体缓冲流式响应,测不了推送)──

def _start_server(tmp_path, web_dir):
    import time as _t

    import uvicorn

    app = create_app(db_path=str(tmp_path / "obs.sqlite3"), web_dir=web_dir, obs_token=TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                           log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = _t.time() + 10
    while not getattr(server, "started", False):
        assert _t.time() < deadline, "uvicorn start timeout"
        _t.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, t, f"http://127.0.0.1:{port}"


@pytest.fixture
def live_server(tmp_path, web_dir):
    import time as _t

    server, thread, base = _start_server(tmp_path, web_dir)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(5)
        assert not thread.is_alive() or _t.sleep(0.1) is None  # pragma: no cover


def _sse_frames(base: str, headers: dict, live_body: dict | None):
    """开一条 SSE:首帧 = snapshot;随后若给 live POST,断言推帧到达。"""
    import httpx

    with httpx.Client(base_url=base, timeout=10) as c:
        with c.stream("GET", "/api/events", headers=headers) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            it = r.iter_lines()
            first = None
            for raw in it:
                if raw.startswith("data: "):
                    first = json.loads(raw[6:])
                    break
        assert first is not None and first["kind"] == "snapshot"
        if live_body is None:
            return first, None
        with c.stream("GET", "/api/events", headers=headers) as r:
            it = r.iter_lines()
            for raw in it:  # 消费首帧(订阅已建立)
                if raw.startswith("data: "):
                    break
            assert c.post("/api/internal/live", json=live_body,
                          headers=headers).status_code == 200
            pushed = None
            for raw in it:
                if raw.startswith("data: "):
                    pushed = json.loads(raw[6:])
                    break
        return first, pushed


def test_sse_push_after_subscribe(live_server, headers):
    first, pushed = _sse_frames(live_server, headers, live_body("solving", "b-01"))
    assert pushed is not None, "live POST 后推帧未达"
    assert pushed["phase"] == "solving"
    assert pushed["challenge_code"] == "b-01"
    assert pushed["kind"] == "lifecycle"
    assert "ts" in pushed


def test_snapshot_first_frame_not_empty_after_live(live_server, headers):
    import httpx
    with httpx.Client(base_url=live_server, timeout=10) as c:
        assert c.post("/api/internal/live", json=live_body("idle"),
                      headers=headers).status_code == 200
    first, _ = _sse_frames(live_server, headers, None)
    assert first["phase"] == "idle"


# ── bus 单元语义 ──

def test_bus_drops_slow_subscriber():
    import asyncio

    async def scenario():
        bus = LiveBus(maxsize=4)
        q = bus.subscribe()
        for i in range(10):
            await bus.publish({"seq": i})
        # 订阅者没消费:满则丢最老,绝不阻塞发布
        qsize = q.qsize()
        assert qsize == 4
        last = None
        while not q.empty():
            last = q.get_nowait()
        assert last["seq"] == 9  # 最新到达

    asyncio.run(scenario())


# ── 已接受 flag 的窄通道(HTTP 层) ──

def test_accepted_flags_endpoint_attaches_to_run(client, headers):
    """assignment 模式补写:POST /api/internal/accepted_flags 后 /api/challenge 可见。"""
    run_id = rid()
    client.post("/api/internal/events", headers=headers, json={
        "run_id": run_id, "worker_id": "worker-1", "challenge_code": "a-05",
        "events": make_run_events([session_ev()]),
    })
    r = client.post("/api/internal/accepted_flags", headers=headers,
                    json={"run_id": run_id, "flags": ["flag{found}"]})
    assert r.status_code == 200 and r.json() == {"ok": True, "attached": True}
    assert client.get("/api/challenge?code=a-05").json()["flags"] == ["flag{found}"]


def test_accepted_flags_endpoint_rejects_anonymous(anon_client):
    """明文 flag 通道与其它 ingest 同凭据,不得匿名写入(用无默认 header 的 client)。"""
    r = anon_client.post("/api/internal/accepted_flags",
                         json={"run_id": rid(), "flags": ["flag{x}"]})
    assert r.status_code == 401
