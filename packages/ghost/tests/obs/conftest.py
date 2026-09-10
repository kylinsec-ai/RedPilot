"""platform 测试夹具:临时 sqlite + 应用工厂 + 合成 transcript 事件工具。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from ghost.obs.app import create_app
from ghost.obs.store import ObsStore

TOKEN = "tok"


# ── 合成 transcript 事件(line/tool/… → dict;make_run_events → 摄取行) ──

def _j(d: dict) -> str:
    return json.dumps(d, ensure_ascii=False)


def rid() -> str:
    """新 run_id(uuid4().hex 形态,平台 RUN_ID_RX 校验)。"""
    import uuid
    return uuid.uuid4().hex


def roster_snap(rows: dict, *, fetched_at: float = 100.0, stale: bool = False,
                error: str = "") -> dict:
    """完整 5 键 roster 快照(与 worker RosterPoller.snapshot 同构)。"""
    return {"fetched_at": fetched_at, "stale": stale, "platform_error": error,
            "platform_disabled": False, "challenges": rows}


def live_body(phase: str, code: str = "a-05") -> dict:
    """一次 internal/live POST 请求体(快照为 LiveState 子集,键与 worker 侧一致)。"""
    return {"worker_id": "worker-1", "kind": "lifecycle",
            "snapshot": {"worker_id": "worker-1", "phase": phase,
                         "challenge_code": code, "turns": 1}}


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
    return [{"seq": i, "type": e.get("type", ""), "payload": _j(e)}
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
def client_no_token(tmp_path, web_dir, monkeypatch):
    # obs.config import 时 load_dotenv() 会把 .env 的 OBSERVABILITY_TOKEN 带进进程;
    # "无 token 应用" 语义要求真无 token → 显式摘掉 env(否则 401 顶替 503,测试失真)
    monkeypatch.delenv("OBSERVABILITY_TOKEN", raising=False)
    with TestClient(create_app(db_path=str(tmp_path / "obs.sqlite3"),
                               web_dir=web_dir, obs_token=None)) as c:
        yield c


@pytest.fixture
def headers() -> dict:
    return {"X-Observability-Token": TOKEN}


def seed_run(store: ObsStore, code: str = "a-05", worker: str = "worker-1",
             started_at: float = 1_000.0, events: list[dict] | None = None) -> str:
    """开一个 run 并(可选)插入合成事件;返回 run_id。"""
    run_id = rid()
    rows = [(e["seq"], e["type"], e["payload"]) for e in make_run_events(events or [])]
    store.append_events(run_id, worker, code, rows, "opencode-go/mimo-v2.5", started_at)
    return run_id
