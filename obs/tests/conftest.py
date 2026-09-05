"""platform 测试夹具:临时 sqlite + 应用工厂 + 合成 transcript 事件工具。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from obs.app import create_app
from obs.store import ObsStore

TOKEN = "tok"


# ── 合成 transcript 事件(line/tool/… → dict;make_run_events → 摄取行) ──

def _j(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)


def session_ev(sid: str = "ab12cd", ts: str = "2026-09-04T11:52:03.942Z",
               cwd: str = "/work/a-05") -> dict:
    return {"type": "session", "version": 3, "id": sid, "timestamp": ts, "cwd": cwd}


def attempt_ev(n: int = 1) -> dict:
    return {"type": "_attempt", "attempt": n}


def user_msg_ev(text: str = "解题 a-05", ts: str = "2026-09-04T11:52:04.100Z") -> dict:
    return {"type": "message_start",
            "message": {"role": "user", "content": [{"type": "text", "text": text}],
                        "timestamp": ts}}


def turn_start_ev() -> dict:
    return {"type": "turn_start"}


def turn_end_ev(tokens: int | None = None, stop: str = "tool_use") -> dict:
    msg: dict = {"stopReason": stop}
    if tokens is not None:
        msg["usage"] = {"totalTokens": tokens}
    return {"type": "turn_end", "message": msg}


def tool_start_ev(call: str = "call_1", name: str = "bash", cmd: str = "ls -la /tmp") -> dict:
    return {"type": "tool_execution_start", "toolCallId": call, "toolName": name,
            "args": {"command": cmd}}


def tool_end_ev(call: str = "call_1", out: str = "", err: bool = False) -> dict:
    return {"type": "tool_execution_end", "toolCallId": call, "toolName": "bash",
            "isError": err, "result": {"content": [{"type": "text", "text": out}]}}


def assistant_msg_ev(text: str, ts: str = "2026-09-04T11:52:05.200Z") -> dict:
    return {"type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                        "timestamp": ts}}


def agent_end_ev() -> dict:
    return {"type": "agent_end"}


def make_run_events(events: list[dict]) -> list[dict]:
    """把原始事件 dict 列表转成摄取行(seq 单调,payload 为原文行)。"""
    return [{"seq": i, "type": e.get("type", ""), "ts": None, "payload": _j(e)}
            for i, e in enumerate(events)]


# ── fixtures ──

@pytest.fixture
def store(tmp_path) -> ObsStore:
    s = ObsStore(str(tmp_path / "obs.sqlite3"))
    yield s
    s.close()


@pytest.fixture
def web_dir(tmp_path) -> str:
    d = tmp_path / "web"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<html>spa</html>", encoding="utf-8")
    (d / "assets" / "index-x.js").write_text("console.log(1)", encoding="utf-8")
    return str(d)


@pytest.fixture
def client(tmp_path, web_dir):
    with TestClient(create_app(db_path=str(tmp_path / "obs.sqlite3"),
                               web_dir=web_dir, obs_token=TOKEN)) as c:
        yield c


@pytest.fixture
def client_no_token(tmp_path, web_dir):
    with TestClient(create_app(db_path=str(tmp_path / "obs.sqlite3"),
                               web_dir=web_dir, obs_token=None)) as c:
        yield c


@pytest.fixture
def headers() -> dict:
    return {"X-Observability-Token": TOKEN}


def seed_run(store: ObsStore, code: str = "a-05", worker: str = "worker-1",
             started_at: float = 1_000.0, events: list[dict] | None = None) -> str:
    """开一个 run 并(可选)插入合成事件;返回 run_id。"""
    import uuid
    run_id = uuid.uuid4().hex
    store.ensure_run(run_id, worker, code, "opencode-go/mimo-v2.5", started_at)
    if events:
        store.insert_events(run_id, worker, code,
                            [(e["seq"], e["type"], e.get("ts"), e["payload"])
                             for e in make_run_events(events)])
    return run_id
