"""观测桥单测：竞技场的 17 字段状态 → LiveState 18 键快照的映射。

本模块是整条观测链上**唯一**没有对手可抄的代码（其余都是搬运），所以它的
断言按"面板会看到什么"来写，而不是按实现细节写。
"""

from __future__ import annotations

import inspect
import json
import threading
import unittest

from redpilot.worker.live import LiveBus, LiveState
from redpilot.worker.observability import StatusBridge


def _status(*, phase: str | None = "idle", **kw) -> dict:
    """一份 status：**键集直接取自 orchestrator._STATUS**，不再手工镜像。

    手工抄一份键集的代价是它会悄悄过期 —— 编排层加一个 status 字段，这里不会
    有任何信号（既不算失败也不算通过），而"生产中永远不会出现的 status"会让
    下面每一条映射断言都失去意义。

    `phase=None` 表示**不带**该键：用来测 StatusBridge 的兜底推导（只有直调
    编排层的老路径才会缺它；竞技场自己每次都写）。
    """
    from redpilot.worker.orchestrator import _STATUS
    base = {**dict(_STATUS), "worker_id": 1, "started_at": 1_700_000_000.0,
            "last_beat": 1_700_000_000.0, "last_activity": 1_700_000_000.0,
            "flags_found": [], "error": ""}
    base["phase"] = phase
    if phase is None:
        base.pop("phase")
    base.update(kw)
    return base


class PhaseMappingTests(unittest.TestCase):
    """phase 由编排层**给出**（status["phase"]），桥只改名不推导。

    这是本模块最容易写错的一处：此前桥用 `last_event` 字符串嗅探去猜阶段，
    结果 `phase` 永远取不到 "starting" → relay 的 run 状态机从不开 run →
    transcript 有路径却没处挂，且全程不报错。
    """

    def test_idle_when_nothing_running(self):
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status())
        self.assertEqual(live.snapshot()["phase"], "idle")

    def test_visit_claim_maps_to_starting(self):
        """认领帧（solving_active 但无活跃会话）必须是 starting。

        relay 靠它开新 run：只写两个布尔而不给 phase 的话，这一帧会落到
        兜底推导的 "closing"，而 closing 走的是**关闭**分支 —— run 根本开不起来。
        """
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(phase="starting", current_code="f2-05",
                       solving_active=True, last_event="visit f2-05"))
        snap = live.snapshot()
        self.assertEqual(snap["phase"], "starting")
        self.assertEqual(snap["challenge_code"], "f2-05")

    def test_session_start_maps_to_solving(self):
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(phase="solving", current_code="f2-05", solving_active=True,
                       session_active=True, session_started_at=1_700_000_100.0,
                       last_event="session start f2-05#0"))
        snap = live.snapshot()
        self.assertEqual(snap["phase"], "solving")
        self.assertEqual(snap["turns"], 0)

    def test_between_sessions_maps_to_closing(self):
        """visit 在跑但没有活跃会话 = 多会话之间的间隙（收尾/复盘/重启靶场）。"""
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(phase="closing", current_code="f2-05", solving_active=True,
                       session_active=False))
        self.assertEqual(live.snapshot()["phase"], "closing")

    def test_phase_is_never_invented_for_unknown_values(self):
        """编排层给了个不在词表里的 phase → 退到兜底推导，**不要**原样透传。

        原样透传的后果是静默：relay/store 按契约词表判活跃，一个拼错的 phase
        会被当成"非活跃"忽略，面板永远不动而不报错。
        """
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(phase="sovling", current_code="f2-05",
                       solving_active=True, session_active=True))
        self.assertEqual(live.snapshot()["phase"], "solving")

    def test_infer_from_booleans_when_orchestrator_omits_phase(self):
        """只有直调编排层的老路径才不带 phase —— 那时从两个布尔兜底推导。"""
        cases = [
            ("idle",    dict(current_code="", solving_active=False, session_active=False)),
            ("solving", dict(current_code="c", solving_active=True, session_active=True)),
            ("closing", dict(current_code="c", solving_active=True, session_active=False)),
        ]
        for expected, kw in cases:
            with self.subTest(expected=expected):
                live = LiveState("worker-1")
                b = StatusBridge(live, None, worker_id="worker-1")
                b.push(_status(phase=None, **kw))
                self.assertEqual(live.snapshot()["phase"], expected)

    def test_session_index_maps_to_turns_and_session_start_does_not_move_started_at(self):
        """started_at 是**进程**启动戳，不能被每场会话的 session_started_at 顶掉。

        顶掉的后果是 elapsed_s 随每场会话归零 —— 面板上"跑了多久"永远显示
        "刚开场"，而硬题恰恰是多会话的。
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
        live.update(transcript_path="/work/f2-05/_transcripts/x.jsonl")
        b.push(_status())
        self.assertEqual(live.snapshot()["transcript_path"], "")

    def test_transcript_path_reaches_the_snapshot_while_solving(self):
        """relay 的字节续读全靠帧里的 transcript_path 锚定起点。

        竞技场一次访问会开**多场**会话，每场一个 `_transcripts/...jsonl` 文件
        （按 trace_scope 隔离）。路径不跟着 status 走的话，run 就只有生命周期、
        一条内容行都进不了 obs —— 而且不报错。
        """
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        path = "/work/f2-05/_transcripts/abc--r000000--s000000--x.jsonl"
        b.push(_status(phase="solving", current_code="f2-05", solving_active=True,
                       session_active=True, last_event="session start f2-05#0",
                       transcript_path=path))
        self.assertEqual(live.snapshot()["transcript_path"], path)


class ToolGranularityTests(unittest.TestCase):
    def test_tool_call_writes_both_halves_in_one_update(self):
        """竞技场 status 只有会话粒度，工具粒度由钩子补 —— 面板要"此刻在跑什么"。

        工具**结束**时写：参数摘要（这一场跑了什么）与输出尾巴（刚看到了什么）
        一次落定。刻意不留 start/end 两个钩子：它们是同一个回调里背靠背调的，
        中间态没有任何观察者看得见，却要多付一次加锁拷贝。
        """
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.tool_call("nmap", {"flags": "-sV", "host": "10.0.0.1"}, "PORT 22/tcp open ssh")
        snap = live.snapshot()
        self.assertEqual(snap["current_tool"], "")      # 已结束，不留"正在跑"
        self.assertEqual(snap["last_tool"], "nmap")
        self.assertIn("sV", snap["current_args_summary"])
        self.assertIn("22/tcp", snap["last_output_tail"])

    def test_tool_call_flushes_the_tool_boundary(self):
        """工具边界是 FLUSH_KINDS 的一员：这一帧必须落盘，否则读者停在上一场尾部。"""
        flushed = []

        class Spy(LiveState):
            def flush(self):
                flushed.append(1)
                super().flush()

        b = StatusBridge(Spy("worker-1"), None, worker_id="worker-1")
        b.tool_call("nmap", {}, "x")
        self.assertEqual(len(flushed), 1)

    def test_null_bridge_is_a_cheap_noop(self):
        """装配层没接桥时（直调编排、单测）钩子必须是无副作用的空操作。

        编排层注入的是 `orchestrator._NULL_BRIDGE` 而不是 None，所以这里锁的是
        "那个占位对象真的什么都不做"——它一旦不小心带了状态，热路径会被拖慢。

        断言刻意**结构性**而非逐次调用检查：`__slots__ = ()` 是"不可能带状态"
        的构造性保证（比"调完 `__dict__` 还是空的"更强 —— 后者要靠每次调用后回看）。
        方法返回值与不抛异常另测，两者合起来才是"廉价空操作"的完整定义。
        """
        from redpilot.worker.orchestrator import _NullBridge, _NULL_BRIDGE

        assert _NullBridge.__slots__ == (), (
            "占位桥有 __slots__ 之外的状态槽 —— 热路径上的空操作不再廉价"
        )
        assert _NULL_BRIDGE.push({"current_code": "x"}) is None
        assert _NULL_BRIDGE.tool_call("nmap", {}, "x") is None
        assert _NULL_BRIDGE.flags_submitted(["flag{a}"]) is None

    def test_hooks_never_raise(self):
        class Boom:
            def update(self, **kw):
                raise RuntimeError("boom")

        b = StatusBridge(Boom(), None)
        b.tool_call("nmap", {}, "x")   # 不抛
        self.assertEqual(b._swallowed, 1)


class BusAndFlushTests(unittest.TestCase):
    def test_publish_carries_passthrough_keys_under_score_prefix(self):
        live = LiveState("worker-1")
        bus = LiveBus()
        q = bus.subscribe()
        try:
            b = StatusBridge(live, bus, worker_id="worker-1")
            b.push(_status(phase="solving", current_code="f2-05", solving_active=True,
                           session_active=True, sessions=2, current_difficulty="hard",
                           flags_submitted=1, total_earned=100, challenges_solved=1,
                           last_event="session start f2-05#1"))
            frame = q.get(timeout=2)
        finally:
            bus.unsubscribe(q)
        self.assertEqual(frame["kind"], "lifecycle")
        self.assertEqual(frame["challenge_code"], "f2-05")
        # phase 现在是快照自身的一列（编排层直接给），不再需要 `_phase` 副本
        self.assertEqual(frame["phase"], "solving")
        # 带外元数据一律下划线前缀（relay 不落库这类键）
        self.assertEqual(frame["_current_difficulty"], "hard")
        self.assertEqual(frame["_flags_submitted"], 1)

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
    def test_structured_error_from_status_reaches_the_snapshot(self):
        """编排层把失败原因**结构化**送过来（`result.error`），桥直传即可。

        此前桥去 `last_log` 里嗅探 "error"/"failed"/"traceback" 关键词来猜 ——
        那是猜一个它本来就能读到的东西，还会把给人看的多语言日志行误判成故障。
        """
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(error="402 Insufficient Balance from provider"))
        self.assertIn("402", live.snapshot()["error"])

    def test_no_error_while_nothing_failed(self):
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(last_log="some chatty human log line"))
        self.assertEqual(live.snapshot()["error"], "")

    def test_error_is_capped(self):
        """面板不该被一条超长 traceback 撑爆（cap 与 last_tool 同一族）。"""
        from redpilot.contracts.text import ERROR_HEAD_MAX
        live = LiveState("worker-1")
        b = StatusBridge(live, None, worker_id="worker-1")
        b.push(_status(error="x" * (ERROR_HEAD_MAX * 3)))
        self.assertLessEqual(len(live.snapshot()["error"]), ERROR_HEAD_MAX)


class AcceptedFlagsChannelTests(unittest.TestCase):
    """eager 提交的明文通道：run 还没收尾时就要把入账的 flag 送进 Runs 历史。

    竞技场在会话进行中就可能投递成功（eager），而 flags_accepted 的常规入口是
    run_close —— 那条要等整场结束。这条窄通道补的就是这个空档。
    """

    def test_accepted_flags_forwarded_to_relay(self):
        class Relay:
            def __init__(self):
                self.sent = []

            def send_accepted_flags(self, flags):
                self.sent.append(list(flags))

        relay = Relay()
        b = StatusBridge(LiveState("worker-1"), None, relay=relay)
        b.flags_submitted(["flag{a}"])
        b.flags_submitted(["flag{b}"])
        # 每次送**单个**刚入账的明文，不是累计列表（累计会让读端反复重写同一批）
        self.assertEqual(relay.sent, [["flag{a}"], ["flag{b}"]])

    def test_empty_flags_is_a_noop(self):
        class Relay:
            def __init__(self):
                self.sent = []

            def send_accepted_flags(self, flags):
                self.sent.append(list(flags))

        relay = Relay()
        b = StatusBridge(LiveState("worker-1"), None, relay=relay)
        b.flags_submitted([])
        self.assertEqual(relay.sent, [])

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


class HeartbeatContractTests(unittest.TestCase):
    """心跳只有一个写者：编排层。

    装配层曾经自己起过第二条心跳线程（每 30s `touch_heartbeat()`）+ 一个
    "心跳不前进就 exit 4"的探针。那个设计是坏的：探针在同一个循环里先写后查，
    它永远看到的是自己刚写下的 mtime，判死分支从构造上不可达 —— 而它的物理
    后果更严重：它每 30s 补写一次心跳，等于替挂死的编排层挡掉了 compose 的
    healthcheck 重启。

    修法是**删掉它**而不是修探针：编排层本来就有心跳线程
    （`orchestrator.main()` 的 `name="heartbeat"`），而它调的是 `_beat()` ——
    那个函数除刷心跳文件外还会调 `_update_status()`，即观测面的数据源。
    装配层再起一条只有坏处。
    """

    def test_driver_starts_no_heartbeat_thread(self):
        """心跳线程归编排层所有（它调 `_beat()`，装配层调不到那个函数）。

        用 AST 而不是源码字符串匹配：注释里提到 `name="heartbeat"` 是说明性文字，
        不该让断言红。
        """
        import ast

        import redpilot.worker.driver as drv
        threads = [
            ast.unparse(node) for node in ast.walk(ast.parse(inspect.getsource(drv)))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) in ("threading.Thread", "Thread")
        ]
        self.assertEqual(threads, [], "装配层不该起脚本线程：心跳归编排层所有")

    def test_orchestrator_is_the_heartbeat_owner(self):
        """编排层的心跳线程仍在，且它走 `_beat()`（刷心跳 + 推状态）。"""
        import inspect

        from redpilot.worker import orchestrator
        src = inspect.getsource(orchestrator.main)
        self.assertIn('name="heartbeat"', src)
        self.assertIn("_beat()", src)


class PassthroughKeyDriftTests(unittest.TestCase):
    """`_PASSTHROUGH_KEYS` 是手工清单 —— 这条用例是它唯一的"会失败"的信号。

    这份清单是照着 orchestrator._STATUS 的键手抄的，而 orchestrator 加一个
    status 字段时**不会**有任何东西失败：新字段静默不进总线（面板少一列，
    不报错）。所以这里断言清单里的每个键都真的能进信封 ——
    幽灵键的代价是它**看起来**被透传了，实际读到的是 KeyError 被吞掉后的 None。

    （反方向——"_STATUS 加了键但忘了加进清单"——不可判定：清单刻意是 _STATUS
    的子集，多数新键本来就不该进总线。所以这里只守住"清单里没有不存在的键"。）
    """

    def test_every_passthrough_key_is_actually_forwarded(self):
        from redpilot.worker.observability import _PASSTHROUGH_KEYS
        live = LiveState("worker-1")
        bus = LiveBus()
        q = bus.subscribe()
        try:
            # 一份"什么都在跑"的 status：让 _push 走"不清空"的那条分支
            st = _status(phase="solving", current_code="f2-05", solving_active=True,
                         session_active=True, sessions=1, transcript_path="/t.jsonl")
            StatusBridge(live, bus, worker_id="worker-1").push(st)
            frame = q.get(timeout=2)
        finally:
            bus.unsubscribe(q)
        rp_keys = [k for k in _PASSTHROUGH_KEYS
                 if f"_{k}" not in frame and k not in frame]
        self.assertEqual(
            rp_keys, [],
            f"observability._PASSTHROUGH_KEYS 里有进不了信封的键: {rp_keys} "
            f"（编排层删了/改了名字 → 总线一直在读 None）")


if __name__ == "__main__":
    unittest.main()
