"""obs 中继测试:本地 mock HTTP 服务器按序记录 POST → 直接 import drivers.obs_relay
起真 relay(不经 driver)→ 断言过滤/定序/状态映射/压缩收缩幂等/平台 down 恢复/未配禁用。

工作目录:repo 根(pytest 由此起,adapter/drivers 可导入)。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adapter.live import LiveBus, LiveState


# ── mock 服务器:按到达序记录 (path, body) ──

class _Recorder(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: N802
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.records.append((self.path, body))  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')


class MockServer:
    def __init__(self, port: int = 0):
        self.records: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self._srv = ThreadingHTTPServer(("127.0.0.1", port), _Recorder)
        self._srv.lock = self.lock
        self._srv.records = self.records
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def by_path(self, path: str) -> list[dict]:
        with self.lock:
            return [b for p, b in self.records if p == path]

    def close(self) -> None:
        self._srv.shutdown()


@pytest.fixture
def mock_server():
    s = MockServer()
    yield s
    s.close()


def _wait(pred, timeout: float = 10.0, what: str = "condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting {what}")


def _frame(phase: str, code: str = "a-05", **extra) -> dict:
    snap = {"worker_id": "worker-1", "phase": phase, "challenge_code": code,
            "model": "m/m", "updated_at": time.time(),
            "turns": 0, "accepted": 0, "flags_found": 0, "error": ""}
    snap.update(extra)
    return {**snap, "kind": "lifecycle"}


def _relay(tmp_path, url: str, token: str = "tok", workdir: str | None = None):
    live = LiveState(worker_id="worker-1", state_path=None)
    bus = LiveBus()
    from drivers.obs_relay import ObsRelay
    r = ObsRelay(live, bus, workdir or str(tmp_path), url, token, worker_id="worker-1")
    r.start()
    return live, bus, r


def _events_of(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for b in records:
        out.extend(b.get("events", []))
    return out


# ── 未配置零副作用 ──

def test_disabled_when_url_unset(monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_URL", raising=False)
    from drivers.obs_relay import maybe_start_relay
    assert maybe_start_relay(None, None, "/tmp/x") is None


# ── 端到端:starting→solving→closing 全序 + 过滤 + 状态映射 ──

def test_lifecycle_order_filter_and_solved(tmp_path, mock_server):
    wd = tmp_path / "work"
    wd.mkdir()
    path = wd / "a-05" / "transcript.jsonl"
    path.parent.mkdir(parents=True)
    (path.parent / "FLAG").write_text("flag{solved}\n", encoding="utf-8")

    def line(typ: str, **extra) -> str:
        ev = {"type": typ, **extra}
        return json.dumps(ev, ensure_ascii=False)

    live, bus, relay = _relay(tmp_path, mock_server.url())
    # run 开始(与 driver 一致:starting 无 path,base 在 solving 帧锚定)
    bus.publish(_frame("starting", "a-05"))
    bus.publish(_frame("solving", "a-05", transcript_path=str(path),
                       model="m/m", accepted=1, flags_found=1))
    _wait(lambda: bool(mock_server.records), what="idle live 注册")
    time.sleep(0.4)  # 引擎已处理 solving 帧并锚定 base
    # 解题中写入:message_update 应被丢弃
    with open(path, "a", encoding="utf-8") as f:
        f.write(line("_attempt", attempt=0) + "\n")
        f.write(line("session", version=3, id="ab12cd-uuid", timestamp="2026-09-04T11:52:03.942Z",
                     cwd="/work/a-05") + "\n")
        f.write(line("message_update", message={},
                     assistantMessageEvent={"type": "text_delta", "delta": "x" * 50}) + "\n")
        f.write(line("turn_start") + "\n")
    # 收尾:submit→done→closing(accepted==flags_found → solved)
    bus.publish(_frame("done", "a-05", accepted=1, flags_found=1))
    bus.publish(_frame("closing", "a-05", accepted=1, flags_found=1, turns=5, updated_at=time.time()))

    _wait(lambda: any(p == "/api/internal/run_close" for p, _ in mock_server.records),
          what="run_close 到达")
    closes = mock_server.by_path("/api/internal/run_close")
    assert closes and closes[0]["status"] == "solved"
    assert closes[0]["flags_accepted"] == ["flag{solved}"]
    assert closes[0]["turns"] is not None
    # run_close 必须晚于该 run 的所有事件 POST
    last_ev_idx = max(i for i, (p, b) in enumerate(mock_server.records)
                      if p == "/api/internal/events" and b.get("events"))
    close_idx = next(i for i, (p, _) in enumerate(mock_server.records)
                     if p == "/api/internal/run_close")
    assert close_idx > last_ev_idx
    # 事件内容:过滤 message_update;payload 原文行;seq 单调
    evs = _events_of([b for p, b in mock_server.records if p == "/api/internal/events"])
    assert evs and [e["seq"] for e in evs] == list(range(len(evs)))
    types = [json.loads(e["payload"])["type"] for e in evs]
    assert types == ["_attempt", "session", "turn_start"]
    assert all("message_update" not in e["payload"] for e in evs)


def test_status_mapping_failed_and_done(tmp_path, mock_server):
    live, bus, relay = _relay(tmp_path, mock_server.url())
    wd = tmp_path / "w"
    wd.mkdir()
    path = wd / "transcript.jsonl"

    # failed:error 非空(stall/timeout)
    bus.publish(_frame("starting", "b-01"))
    bus.publish(_frame("solving", "b-01", transcript_path=str(path)))
    time.sleep(0.4)
    open(path, "a", encoding="utf-8").write('{"type": "session"}\n')
    bus.publish(_frame("error", "b-01", error="stall timeout"))
    bus.publish(_frame("closing", "b-01", error="stall timeout"))
    _wait(lambda: bool(mock_server.by_path("/api/internal/run_close")), what="failed close")
    assert mock_server.by_path("/api/internal/run_close")[0]["status"] == "failed"
    assert mock_server.by_path("/api/internal/run_close")[0]["error"] == "stall timeout"

    # done:部分接受(found=2, accepted=1)
    bus.publish(_frame("starting", "c-01"))
    bus.publish(_frame("solving", "c-01", transcript_path=str(path)))
    time.sleep(0.4)
    open(path, "a", encoding="utf-8").write('{"type": "session"}\n')
    bus.publish(_frame("done", "c-01", accepted=1, flags_found=2))
    bus.publish(_frame("closing", "c-01", accepted=1, flags_found=2))
    _wait(lambda: len(mock_server.by_path("/api/internal/run_close")) >= 2, what="done close")
    assert mock_server.by_path("/api/internal/run_close")[1]["status"] == "done"


def test_shrink_compress_no_duplicate_loss(tmp_path, mock_server):
    """压缩重写(只删 message_update,文件变小):offset>size 且已送行 → 对齐新 EOF,
    后续追加零丢失;已发行同内容由平台 UNIQUE 幂等兜底。"""
    live, bus, relay = _relay(tmp_path, mock_server.url())
    wd = tmp_path / "w"
    wd.mkdir()
    path = wd / "transcript.jsonl"

    def wline(typ: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": typ}) + "\n")

    bus.publish(_frame("starting", "d-01"))
    bus.publish(_frame("solving", "d-01", transcript_path=str(path)))
    time.sleep(0.4)
    wline("_attempt")
    wline("message_update")  # 会被过滤,但占据字节 → 压缩后文件缩小
    _wait(lambda: _events_of(mock_server.by_path("/api/internal/events")) != [],
          what="首批事件")
    # 模拟 compress_transcript:原子重写,去掉 message_update
    kept = [json.dumps({"type": t}) for t in ("_attempt", "turn_start")]
    tmpf = path.with_suffix(".jsonl.tmp")
    tmpf.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(tmpf, path)
    relay.flush_run()  # driver 压缩前必调
    time.sleep(0.5)    # 引擎 shrink 检测走 offset=size
    wline("turn_end")
    _wait(lambda: any(
        json.loads(e["payload"]).get("type") == "turn_end"
        for e in _events_of(mock_server.by_path("/api/internal/events"))),
        what="压缩后追加行到达")
    bus.publish(_frame("closing", "d-01"))
    _wait(lambda: bool(mock_server.by_path("/api/internal/run_close")), what="close")


def test_truncate_at_run_start(tmp_path, mock_server):
    """>5MB 截断(本 run 起点):base 尚未送行时 size<base → 从头读,新内容零丢失。"""
    live, bus, relay = _relay(tmp_path, mock_server.url())
    wd = tmp_path / "w"
    wd.mkdir()
    path = wd / "transcript.jsonl"
    # 旧 run 残留(大文件,引擎 base 锚在其 EOF;截断后必须 size<base 才走从头读)
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(200):
            f.write(json.dumps({"type": "session"}) + "\n")
    bus.publish(_frame("starting", "e-01"))
    bus.publish(_frame("solving", "e-01", transcript_path=str(path)))
    time.sleep(0.4)  # base=旧 EOF
    # 截断 + 新 run 内容
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type": "_attempt", "attempt": 0}\n{"type": "session"}\n')
    _wait(lambda: any(
        json.loads(e["payload"]).get("type") == "_attempt"
        for e in _events_of(mock_server.by_path("/api/internal/events"))),
        what="截断后内容从头送达")


def test_platform_down_then_recover(tmp_path):
    """平台 down:事件留在 FIFO(零丢失);平台上线后按序送达(close 恒在事件后)。"""
    wd = tmp_path / "w"
    wd.mkdir()
    path = wd / "transcript.jsonl"

    # 占一个端口后释放:relay 先指向无监听者,随后服务器在同一端口上线
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    live, bus, relay = _relay(tmp_path, f"http://127.0.0.1:{port}")
    bus.publish(_frame("starting", "f-01"))
    bus.publish(_frame("solving", "f-01", transcript_path=str(path)))
    time.sleep(0.4)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type": "_attempt"}\n{"type": "session"}\n')
    bus.publish(_frame("done", "f-01"))
    bus.publish(_frame("closing", "f-01"))
    time.sleep(1.2)  # down 期多轮失败退避;事件与 close 全部留在 FIFO

    srv = MockServer(port=port)
    try:
        _wait(lambda: bool(srv.by_path("/api/internal/run_close")), timeout=15.0,
              what="恢复后 run_close 送达")
        closes = srv.by_path("/api/internal/run_close")
        assert closes[0]["status"] == "done"
        last_ev = max((i for i, (p, b) in enumerate(srv.records)
                       if p == "/api/internal/events" and b.get("events")), default=-1)
        close_idx = next(i for i, (p, _) in enumerate(srv.records)
                         if p == "/api/internal/run_close")
        assert close_idx > last_ev  # 断网期积压的事件先于 close 送达
        evs = _events_of([b for p, b in srv.records if p == "/api/internal/events"])
        types = [json.loads(e["payload"])["type"] for e in evs]
        assert types == ["_attempt", "session"]  # 零丢失
    finally:
        srv.close()
        relay._stop.set()
