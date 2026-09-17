"""obs 中继测试:本地 mock HTTP 服务器按序记录 POST → 直接起真 relay(不经 driver)
→ 断言过滤/定序/状态映射/压缩收缩幂等/平台 down 恢复/未配禁用。

pytest 由仓库根起(仓库根在 pythonpath,redpilot.worker 可导入)。
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from redpilot.worker.live import LiveBus, LiveState


# ── mock 服务器:按到达序记录 (path, body) ──

class _Recorder(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: N802
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.records.append((self.path, body))  # type: ignore[attr-defined]
        fail = getattr(self.server, "fail_paths", ())  # type: ignore[attr-defined]
        if self.path in fail:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail": "boom"}')
            return
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
        self._srv.fail_paths = set()
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def fail(self, *paths: str) -> None:
        """令这些路径恒定 500(其余照常 200),模拟平台侧持续拒绝某类载荷。"""
        self._srv.fail_paths = set(paths)

    def by_path(self, path: str) -> list[dict]:
        with self.lock:
            return [b for p, b in self.records if p == path]

    def close(self) -> None:
        # shutdown() 只停 serve_forever,必须 server_close() 释放监听 socket,
        # 否则每个用例泄漏一个已绑定端口(后续用例复用端口会撞上残留 backlog)
        self._srv.shutdown()
        self._srv.server_close()


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
    from redpilot.worker.relay import ObsRelay
    r = ObsRelay(live, bus, workdir or str(tmp_path), url, token, worker_id="worker-1")
    r.start()
    return live, bus, r


def _relay_unstarted(tmp_path, url: str, token: str = "tok"):
    """只构造、不 start():单测直接驱动 _sender,不经引擎与节拍线程。"""
    live = LiveState(worker_id="worker-1", state_path=None)
    bus = LiveBus()
    from redpilot.worker.relay import ObsRelay
    return live, bus, ObsRelay(live, bus, str(tmp_path), url, token,
                               worker_id="worker-1")


def _events_of(records: list[dict]) -> list[dict]:
    out: list[dict] = []
    for b in records:
        out.extend(b.get("events", []))
    return out


# ── 未配置零副作用 ──

def test_disabled_when_url_unset(monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_URL", raising=False)
    from redpilot.worker.relay import maybe_start_relay
    assert maybe_start_relay(None, None) is None


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
    relay.stop()


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

    # done:零接受但发现候选(found=2, accepted=0)
    bus.publish(_frame("starting", "c-01"))
    bus.publish(_frame("solving", "c-01", transcript_path=str(path)))
    time.sleep(0.4)
    open(path, "a", encoding="utf-8").write('{"type": "session"}\n')
    bus.publish(_frame("done", "c-01", accepted=0, flags_found=2))
    bus.publish(_frame("closing", "c-01", accepted=0, flags_found=2))
    _wait(lambda: len(mock_server.by_path("/api/internal/run_close")) >= 2, what="done close")
    assert mock_server.by_path("/api/internal/run_close")[1]["status"] == "done"

    # solved:接受且候选数大于接受数(found=3, accepted=1)
    bus.publish(_frame("starting", "c-02"))
    bus.publish(_frame("solving", "c-02", transcript_path=str(path)))
    time.sleep(0.4)
    open(path, "a", encoding="utf-8").write('{"type": "session"}\n')
    bus.publish(_frame("done", "c-02", accepted=1, flags_found=3))
    bus.publish(_frame("closing", "c-02", accepted=1, flags_found=3))
    _wait(lambda: len(mock_server.by_path("/api/internal/run_close")) >= 3, what="solved close with decoys")
    assert mock_server.by_path("/api/internal/run_close")[2]["status"] == "solved"
    relay.stop()


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
    relay.stop()


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
    relay.stop()


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
        relay.stop()


@pytest.fixture
def sender_backoff_noop(monkeypatch):
    """把 sender 线程的退避 sleep 变成 no-op。

    只对 obs-sender 线程生效:redpilot.worker.relay 里的 `time` 是共享模块,
    直接 patch time.sleep 会让本模块 _wait 的 50ms 轮询变成忙等。
    """
    import redpilot.worker.relay as relay_mod
    real_sleep = relay_mod.time.sleep

    def fake_sleep(seconds):
        if threading.current_thread().name == "obs-sender":
            return
        real_sleep(seconds)

    monkeypatch.setattr(relay_mod.time, "sleep", fake_sleep)


def test_permanent_500_does_not_block_later_messages(tmp_path, mock_server,
                                                     sender_backoff_noop):
    """被平台持续 500 的消息不得永久占住队首 —— 重试必须有终点。

    回归:此前失败消息经 requeue 放回**队首**且无尝试上限、无截止时间,一条毒
    消息会把它后面的 live/events/ping 永久堵死;平台据此在 stale_after=150s 后
    判定 worker 离线,而 worker 其实还在正常解题,且没有任何恢复路径(只能重启进程)。
    """
    mock_server.fail("/api/internal/events")
    live, bus, relay = _relay_unstarted(tmp_path, mock_server.url())

    run = {"run_id": "a" * 32, "worker_id": "worker-1", "code": "a-05"}
    relay._fifo.put({"t": "events", "run": run,
                     "rows": [{"seq": 0, "type": "session", "payload": "{}"}]})
    relay._fifo.put({"t": "ping", "worker_id": "worker-1"})

    threading.Thread(target=relay._sender, daemon=True, name="obs-sender").start()
    try:
        _wait(lambda: bool(mock_server.by_path("/api/internal/ping")), timeout=10.0,
              what="毒消息之后的 ping 送达(队首未被永久占住)")
    finally:
        relay.stop()


def test_transport_failure_never_counts_as_poison(tmp_path, monkeypatch,
                                                  sender_backoff_noop):
    """平台 down(连接失败)无论重试多少次都不许丢 —— 重试终点只认"回了话的失败"。

    毒消息上限(_POISON_TRIES)只对 HTTP 5xx/429 这类**有响应**的失败计数。若日后
    有人把连接异常一并计入,长 down 就会静默吃掉积压事件 —— 直接破掉 relay 的
    零丢失不变量,而这条不变量恰是「失败放回队首」重试存在的唯一理由。
    """
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # 端口空着:连接必被拒(HTTP 无响应)

    live, bus, relay = _relay_unstarted(tmp_path, f"http://127.0.0.1:{port}")
    failed_tries: list[int] = []
    real_requeue = relay._fifo.requeue

    def spy(msg):
        failed_tries.append(msg.get("_tries", 0))
        real_requeue(msg)

    monkeypatch.setattr(relay._fifo, "requeue", spy)

    run = {"run_id": "a" * 32, "worker_id": "worker-1", "code": "a-05"}
    relay._fifo.put({"t": "events", "run": run,
                     "rows": [{"seq": 0, "type": "session", "payload": "{}"}]})

    threading.Thread(target=relay._sender, daemon=True, name="obs-sender").start()
    srv = None
    try:
        _wait(lambda: len(failed_tries) > 6, timeout=10.0,
              what="连接失败重试次数超过毒消息上限")
        assert not any(failed_tries[-1:]), \
            "连接失败被当毒消息计数(平台 down 将静默丢事件)"
        srv = MockServer(port=port)
        _wait(lambda: bool(srv.by_path("/api/internal/events")), timeout=15.0,
              what="平台恢复后积压事件仍送达(零丢失)")
    finally:
        if srv:
            srv.close()
        relay.stop()
