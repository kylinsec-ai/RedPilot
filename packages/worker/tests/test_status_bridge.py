"""观测桥单测：竞技场的 17 字段状态 → LiveState 18 键快照的映射。

本模块是整条观测链上**唯一**没有对手可抄的代码（其余都是搬运），所以它的
断言按"面板会看到什么"来写，而不是按实现细节写。
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest

from ghost_worker.live import LiveBus, LiveState
from ghost_worker.observability import StatusBridge


def _status(**kw) -> dict:
    """一份最小 status：键与 orchestrator._STATUS 对齐。"""
    base = {
        "worker_id": 1,
        "started_at": 1_700_000_000.0,
        "last_beat": 1_700_000_000.0,
        "current_code": "",
        "solving_active": False,
        "current_difficulty": "",
        "current_round": 0,
        "sessions": 0,
        "session_active": False,
        "session_started_at": 0.0,
        "last_activity": 1_700_000_000.0,
        "flags_found": [],
        "flags_submitted": 0,
        "total_earned": 0,
        "challenges_solved": 0,
        "last_event": "",
        "last_log": "",
    }
    base.update(kw)
    return base


class PhaseMappingTests(unittest.TestCase):
    def test_idle_when_nothing_running(self):
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status())
        self.assertEqual(live.snapshot()["phase"], "idle")

    def test_solving_when_session_active(self):
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(current_code="f2-05", solving_active=True, session_active=True,
                       session_started_at=1_700_000_100.0, last_event="session start f2-05#0"))
        snap = live.snapshot()
        self.assertEqual(snap["phase"], "solving")
        self.assertEqual(snap["challenge_code"], "f2-05")
        self.assertEqual(snap["turns"], 0)

    def test_closing_between_sessions_within_a_visit(self):
        """visit 在跑但没有活跃会话 = 多会话之间的间隙（收尾/复盘/重启靶场）。"""
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(current_code="f2-05", solving_active=True, session_active=False))
        self.assertEqual(live.snapshot()["phase"], "closing")

    def test_session_index_maps_to_turns_and_session_start_does_not_move_started_at(self):
        """started_at 是**本题**起点，不能被每场会话的 session_started_at 顶掉。

        顶掉的后果是 elapsed_s 随每场会话归零 —— 面板上"这道题跑了多久"会
        永远显示"刚开场"，而硬题恰恰是多会话的。
        """
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(current_code="f2-05", solving_active=True, session_active=True,
                       sessions=3, started_at=1_700_000_000.0,
                       session_started_at=1_700_009_999.0))
        snap = live.snapshot()
        self.assertEqual(snap["turns"], 3)
        self.assertEqual(snap["started_at"], 1_700_000_000.0)

    def test_flags_found_list_maps_to_count(self):
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(flags_found=["flag{a}", "flag{b}"]))
        self.assertEqual(live.snapshot()["flags_found"], 2)

    def test_idle_clears_transcript_path(self):
        """收尾后 transcript_path 必须清掉，否则面板会一直挂着上一题的实录。"""
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        live.update(transcript_path="/work/f2-05/transcript--s0--x.jsonl")
        b.push(_status())
        self.assertEqual(live.snapshot()["transcript_path"], "")


class ToolGranularityTests(unittest.TestCase):
    def test_tool_start_then_output_clears_current_tool(self):
        """竞技场 status 只有会话粒度，工具粒度由钩子补 —— 面板要"此刻在跑什么"。"""
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.tool_start("nmap", {"flags": "-sV", "host": "10.0.0.1"})
        snap = live.snapshot()
        self.assertEqual(snap["current_tool"], "nmap")
        self.assertIn("nmap", snap["current_args_summary"] + "nmap")  # summarize_args 形如 nmap -sV ...
        b.tool_output("PORT 22/tcp open ssh")
        snap = live.snapshot()
        self.assertEqual(snap["current_tool"], "")
        self.assertIn("22/tcp", snap["last_output_tail"])

    def test_null_bridge_is_a_cheap_noop(self):
        """装配层没接桥时（直调编排、单测）钩子必须是无副作用的空操作。

        编排层注入的是 `orchestrator._NULL_BRIDGE` 而不是 None，所以这里锁的是
        "那个占位对象真的什么都不做"——它一旦不小心带了状态，热路径会被拖慢。
        """
        from ghost_worker.orchestrator import _NULL_BRIDGE
        _NULL_BRIDGE.push({"current_code": "x"})
        _NULL_BRIDGE.tool_start("nmap", {})
        _NULL_BRIDGE.tool_output("x")
        _NULL_BRIDGE.flags_submitted(["flag{a}"])

    def test_note_helpers_never_raise(self):
        class Boom:
            def update(self, **kw):
                raise RuntimeError("boom")

        b = StatusBridge(Boom(), None)
        b.tool_start("nmap", {})   # 不抛
        b.tool_output("x")         # 不抛
        self.assertEqual(b._swallowed, 2)


class BusAndFlushTests(unittest.TestCase):
    def test_publish_carries_passthrough_keys_under_score_prefix(self):
        live = LiveState("worker-1")
        bus = LiveBus()
        q = bus.subscribe()
        try:
            b = StatusBridge(live, bus, worker_id="worker-1")
            b.push(_status(current_code="f2-05", solving_active=True, session_active=True,
                           sessions=2, current_difficulty="hard", flags_submitted=1,
                           total_earned=100, challenges_solved=1,
                           last_event="session start f2-05#1"))
            frame = q.get(timeout=2)
        finally:
            bus.unsubscribe(q)
        self.assertEqual(frame["kind"], "lifecycle")
        self.assertEqual(frame["challenge_code"], "f2-05")
        # 带外元数据一律下划线前缀（relay 不落库这类键）
        self.assertEqual(frame["_current_difficulty"], "hard")
        self.assertEqual(frame["_flags_submitted"], 1)
        self.assertEqual(frame["_phase"], "solving")

    def test_flush_only_on_meaningful_transition(self):
        """热路径每 30s push 一次；每次都强制 flush 会把 LiveState 的节流废掉。"""
        flushed = []

        class Spy(LiveState):
            def flush(self):
                flushed.append(1)
                super().flush()

        live = Spy("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status())                       # 首次：建立基线，不 flush
        b.push(_status())                       # 同签名：不 flush
        b.push(_status())                       # 同签名：不 flush
        self.assertEqual(len(flushed), 0)
        b.push(_status(current_code="f2-05"))   # 跳变：flush 一次
        self.assertEqual(len(flushed), 1)
        b.push(_status(current_code="f2-05"))   # 又同签名
        self.assertEqual(len(flushed), 1)

    def test_push_never_raises_even_when_bus_is_broken(self):
        class BoomBus:
            def has_subscribers(self):
                return True

            def publish(self, payload):
                raise RuntimeError("boom")

        live = LiveState("worker-1")
        b = StatusBridge(live, BoomBus(), worker_id="worker-1")
        b.push(_status(current_code="x"))       # 不抛
        self.assertEqual(b._swallowed, 1)

    def test_publish_skipped_without_subscribers(self):
        """没有订阅者时不构造信封 —— 热路径上不该白白序列化。"""
        calls = []

        class NoSubBus:
            def has_subscribers(self):
                return False

            def publish(self, payload):
                calls.append(payload)

        b = StatusBridge(LiveState("worker-1"), NoSubBus(), worker_id="worker-1")
        b.push(_status(current_code="x"))
        self.assertEqual(calls, [])


class ErrorSurfacingTests(unittest.TestCase):
    def test_failure_text_in_last_log_surfaces_as_error_when_idle(self):
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(last_log="session failed: provider 401"))
        self.assertIn("401", live.snapshot()["error"])

    def test_no_error_noise_while_session_active(self):
        """会话在跑时不该把历史失败文案挂在面板上（那会让红点常亮）。"""
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(solving_active=True, session_active=True,
                       last_log="previous session failed: x"))
        self.assertEqual(live.snapshot()["error"], "")


class AcceptedFlagsChannelTests(unittest.TestCase):
    def test_accepted_flags_forwarded_to_relay(self):
        class Relay:
            def __init__(self):
                self.sent = []

            def send_accepted_flags(self, flags):
                self.sent.append(list(flags))

        relay = Relay()
        b = StatusBridge(LiveState("worker-1"), None, relay=relay)
        b.flags_submitted(["flag{a}", "flag{b}"])
        self.assertEqual(relay.sent, [["flag{a}", "flag{b}"]])

    def test_relay_failure_swallowed(self):
        class BoomRelay:
            def send_accepted_flags(self, flags):
                raise RuntimeError("boom")

        b = StatusBridge(LiveState("worker-1"), None, relay=BoomRelay())
        b.flags_submitted(["flag{a}"])   # 不抛


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_pushes_keep_snapshot_coherent(self):
        """push 会来自心跳线程与求解线程两侧，快照必须始终是合法 JSON。"""
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        errs: list[Exception] = []

        def worker(code: str) -> None:
            try:
                for i in range(200):
                    b.push(_status(current_code=code, sessions=i % 5))
            except Exception as e:  # pragma: no cover
                errs.append(e)

        ts = [threading.Thread(target=worker, args=(c,)) for c in ("a", "b", "c")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        json.dumps(live.snapshot())   # 序列化得动 = 没有半写状态


class HeartbeatProbeTests(unittest.TestCase):
    """挂死探针的判据：只看"别人有没有推进心跳"，不看自己写了什么。

    这一条写错过一次（探针与补心跳在同一个循环里，先写再查 → 判死的分支
    永远不可达）。所以断言按"探针自己写不写"来分组，而不是按时间。
    """

    def test_probe_does_not_penalize_its_own_beat(self):
        """补心跳（装配层的职责）不得让探针以为"编排层还活着"。"""
        import ghost_worker.driver as drv
        from ghost_contracts.paths import HEARTBEAT_PATH

        probe = drv.HeartbeatProbe(stale_after=0.05)
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
        self.assertFalse(probe.observe())        # 建立基线
        time.sleep(0.08)
        self.assertFalse(probe.observe())        # 发现停滞（记起点）
        time.sleep(0.08)
        self.assertTrue(probe.observe())         # 仍无编排层写入 → 判死

    def test_probe_resets_when_orchestrator_advances_heartbeat(self):
        import ghost_worker.driver as drv
        from ghost_contracts.paths import HEARTBEAT_PATH

        probe = drv.HeartbeatProbe(stale_after=0.05)
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
        probe.observe()
        time.sleep(0.08)
        probe.observe()
        time.sleep(0.08)
        with open(HEARTBEAT_PATH, "a"):          # 编排层又跳了一次
            os.utime(HEARTBEAT_PATH, None)
        self.assertFalse(probe.observe())

    def test_probe_survives_missing_heartbeat_file(self):
        """文件不存在（启动早期）不该判死 —— 那时编排层还没开始跑。"""
        import ghost_worker.driver as drv
        probe = drv.HeartbeatProbe(stale_after=0.01)
        self.assertFalse(probe.observe())
        time.sleep(0.03)
        self.assertFalse(probe.observe())


if __name__ == "__main__":
    unittest.main()
