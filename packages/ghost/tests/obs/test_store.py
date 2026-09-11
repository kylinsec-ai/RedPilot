"""store 单测:幂等写入 / 状态机 / 守卫 / 查询。"""

from __future__ import annotations

import time

from conftest import (attempt_ev, rid, roster_snap, seed_run, session_ev,
                      tool_start_ev, turn_start_ev)


# ── 幂等 ──

def test_insert_idempotent(store):
    run_id = rid()
    inserted, created = store.append_events(run_id, "worker-1", "a-05",
                                            [(0, "session", '{"type":"session"}'),
                                             (1, "message_start", "{}")])
    assert (inserted, created) == (2, True)
    assert store.append_events(run_id, "worker-1", "a-05",
                               [(0, "session", '{"type":"session"}'),
                                (1, "message_start", "{}")]) == (0, False)  # 全重放
    assert store.append_events(run_id, "worker-1", "a-05",
                               [(2, "agent_start", "{}")])[0] == 1


def test_close_run_state_machine(store):
    rid = seed_run(store, events=[session_ev(), turn_start_ev(),
                                  tool_start_ev(cmd="curl x"), tool_start_ev(cmd="curl y"),
                                  attempt_ev(1)])
    # 缺省计数回填:turns=tool_execution_start 数, sessions=session 数
    assert store.close_run(rid, status="solved", flags_found=1,
                           flags_accepted=["flag{a}"])
    row = store.run_row(rid)
    assert row["status"] == "solved"
    assert row["turns"] == 2
    assert row["sessions"] == 1
    assert row["flags_found"] == 1
    assert row["flags_accepted"] == ["flag{a}"]
    assert row["ended_at"] is not None and row["duration_s"] >= 0
    # 终态幂等:再关一律忽略
    assert store.close_run(rid, status="failed") is False
    assert store.run_row(rid)["status"] == "solved"


def test_interrupted_can_be_repaired(store):
    rid = seed_run(store)
    closed = store.close_stale_runs(now=10_000.0, stale_after=150.0)  # 无 live 行=心跳缺失
    assert closed == [rid]
    row = store.run_row(rid)
    assert row["status"] == "interrupted"
    assert "heartbeat" in (row["error"] or "")
    # 迟到的真实 close 可修复(interrupted 可写)
    assert store.close_run(rid, status="solved")
    assert store.run_row(rid)["status"] == "solved"


# ── 查询排序 ──

def test_events_for_code_ordered(store):
    # run B 先插入但 started_at 更晚 → 查询序仍按 started_at
    rb = seed_run(store, code="a-05", started_at=2_000.0,
                  events=[attempt_ev(1), session_ev(cwd="/work/a-05")])
    ra = seed_run(store, code="a-05", started_at=1_000.0,
                  events=[session_ev(cwd="/work/a-05"), turn_start_ev()])
    rows = store.events_for_code("a-05")
    seqs = [r["seq"] for r in rows]
    run_ids = [r["run_id"] for r in rows]
    assert run_ids[:2] == [ra, ra] and run_ids[2:] == [rb, rb]  # ra(run_id) 先于 rb
    assert seqs == [0, 1, 0, 1]
    assert rows[0]["payload"].startswith('{"type": "session",')


def test_transcript_tail_across_runs(store):
    ra = seed_run(store, code="a-05", started_at=1_000.0,
                  events=[attempt_ev(1), session_ev()])
    rb = seed_run(store, code="a-05", started_at=2_000.0,
                  events=[attempt_ev(2), session_ev(), turn_start_ev()])
    tail = store.transcript_tail("a-05", 3)
    assert len(tail) == 3
    # 新 run 在前,倒序取尾再正序
    assert tail == [r["payload"] for r in store.events_for_run(rb)]
    assert store.events_for_run(ra)[0]["payload"] not in tail


# ── roster 每 worker 行 + 读合并 ──

def test_roster_per_worker_and_merge(store):
    w1 = roster_snap({"a-05": {"unique_code": "a-05", "difficulty": "easy"},
                      "b-01": {"unique_code": "b-01", "local_only": True,
                               "local": {"dir": "b-01", "flag": True}}})
    store.put_roster("worker-1", w1)
    # 单 worker 读 = 原样
    assert store.roster_merged() == w1

    stale_w2 = roster_snap({"c-01": {"unique_code": "c-01", "difficulty": "hard"}},
                           fetched_at=50.0, stale=True)
    stale_w2["platform_error"] = "boom"
    store.put_roster("worker-2", stale_w2)
    merged = store.roster_merged()
    # 平台段取非 stale 最新(worker-1);challenges 为并集
    assert merged["fetched_at"] == 100.0 and merged["stale"] is False
    assert set(merged["challenges"]) == {"a-05", "b-01", "c-01"}
    # 同一 code 多 worker:非 local_only 优先于 local_only
    w2_fresh = roster_snap({"b-01": {"unique_code": "b-01",
                                     "difficulty": "medium", "local_only": False}},
                           fetched_at=200.0)
    store.put_roster("worker-2", w2_fresh)
    merged = store.roster_merged()
    assert merged["fetched_at"] == 200.0
    assert merged["challenges"]["b-01"]["local_only"] is False
    assert merged["challenges"]["a-05"]["difficulty"] == "easy"


def test_roster_empty_state(store):
    assert store.roster_merged() == {"fetched_at": 0.0, "stale": True,
                                     "platform_error": "", "platform_disabled": True,
                                     "challenges": {}}


# ── runs 列表/分页 ──

def test_runs_listing_and_paging(store):
    ids = []
    for i, st in enumerate(["running", "done", "solved", "failed"]):
        rid = seed_run(store, code=f"a-0{i + 1}", started_at=1_000.0 + i,
                       events=[attempt_ev(1), session_ev()])
        ids.append(rid)
        if st != "running":
            store.close_run(rid, status=st, turns=2, sessions=1)
    all_rows = store.list_runs()
    assert len(all_rows) == 4
    assert {r["status"] for r in all_rows} == {"running", "done", "solved", "failed"}
    assert all(r["event_count"] == 2 for r in all_rows)
    by_status = store.list_runs(status="done")
    assert len(by_status) == 1 and by_status[0]["status"] == "done"
    by_worker = store.list_runs(worker="worker-1", challenge="a-02")
    assert len(by_worker) == 1
    assert store.list_runs(limit=2) == all_rows[:2]  # started_at DESC
    assert store.run_row("nope") is None


# ── 崩溃/心跳守卫 ──

def test_switch_guard_and_stale_guard(store):
    ra = seed_run(store, code="a-05")
    rb = seed_run(store, code="b-01")
    # 换题守卫:keep b-01 → a-05 被关
    closed = store.close_runs_for_switch("worker-1", "b-01")
    assert closed == [ra]
    assert store.run_row(ra)["status"] == "interrupted"
    # 心跳新鲜 → 不被 housekeeper 杀
    store.put_live("worker-1", {"worker_id": "worker-1", "phase": "solving",
                                "challenge_code": "b-01"})
    assert store.close_stale_runs(now=time.time(), stale_after=150.0) == []
    # 心跳过期 → running run 被关
    store._conn.execute("UPDATE live_state SET updated_at=?", (time.time() - 400.0,))
    assert store.close_stale_runs(now=time.time(), stale_after=150.0) == [rb]


def test_idle_guard_closes_running(store):
    rid = seed_run(store, code="a-05")
    closed = store.close_running_for_worker("worker-1", "worker restarted idle")
    assert closed == [rid]
    assert store.run_row(rid)["status"] == "interrupted"


# ── live / ping ──

def test_live_and_ping(store):
    assert store.put_live("worker-1", {"worker_id": "worker-1", "phase": "idle"}) is None
    prev = store.put_live("worker-1", {"worker_id": "worker-1", "phase": "solving",
                                       "challenge_code": "a-05"})
    assert prev == {"worker_id": "worker-1", "phase": "idle"}
    assert store.live_latest()["phase"] == "solving"
    assert store.active_live_codes() == {"a-05"}
    store.put_live("worker-2", {"worker_id": "worker-2", "phase": "idle"})
    # 后写的 worker-2 更新更晚 → /api/status 的最新活语义;active 集与"最新"无关
    assert store.live_latest()["worker_id"] == "worker-2"
    assert store.active_live_codes() == {"a-05"}  # worker-1 仍在 solving
    assert store.ping("worker-1") is True
    assert store.ping("ghost") is False


def test_challenge_flags_latest_run(store):
    assert store.challenge_flags("a-05") == []
    ra = seed_run(store, code="a-05", started_at=1_000.0)
    store.close_run(ra, status="done", flags_accepted=["flag{old}"])
    assert store.challenge_flags("a-05") == ["flag{old}"]
    rb = seed_run(store, code="a-05", started_at=2_000.0)
    store.close_run(rb, status="done", flags_accepted=["flag{new}"])
    assert store.challenge_flags("a-05") == ["flag{new}"]
    assert store.challenge_flags("never") == []


# ── 读写分离(单写者约束下的并发读) ──

def test_read_does_not_block_on_write_lock(store):
    """读不持写锁 —— 否则慢查询(如无 LIMIT 的 /api/timeline)会阻塞 ingest。

    直接持有写锁模拟"有写事务在飞":读若仍走同一把锁就取不到结果。
    join 必须带超时:旧实现下读线程会永久阻塞在锁上,无超时会让整个测试进程
    挂死(表现为 CI 超时而非用例失败),比断言失败更难诊断。
    """
    import threading

    store.append_events(rid(), "worker-1", "a-05", [(0, "session", "{}")])
    done = threading.Event()
    seen: dict = {}

    def reader() -> None:
        seen["rows"] = store.list_runs()
        done.set()

    t = threading.Thread(target=reader, daemon=True)
    with store._lock:  # 写锁被持有
        t.start()
        finished = done.wait(5.0)
    t.join(timeout=5.0)  # 不阻塞测试进程
    assert finished, "读被写锁阻塞:读写仍共用同一把锁"
    assert len(seen["rows"]) == 1


def test_read_connection_is_query_only(store):
    """只读连接在 SQLite 层就不能写(结构性保证,非调用方自觉)。"""
    import sqlite3

    import pytest

    with store._read() as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO runs(run_id, worker_id, challenge_code,"
                         " status, started_at, updated_at) VALUES('x','y','z','running',0,0)")


def test_memory_store_falls_back_to_shared_connection(tmp_path):
    """内存库每个连接是独立私有库,必须回落单连接,否则读不到写入。"""
    from ghost.obs.store import ObsStore

    mem = ObsStore(":memory:")
    try:
        assert mem._shared_conn is True
        mem.append_events("r1", "worker-1", "a-05", [(0, "session", "{}")])
        assert len(mem.list_runs()) == 1  # 读得到的正是刚写的那条
    finally:
        mem.close()


def test_reads_see_committed_writes(tmp_path):
    """读连接必须看得到已提交的写(WAL 下每次读取最新已提交快照)。"""
    from ghost.obs.store import ObsStore

    st = ObsStore(tmp_path / "obs.sqlite3")
    try:
        assert st.list_runs() == []
        st.append_events("r1", "worker-1", "a-05", [(0, "session", "{}")])
        assert len(st.list_runs()) == 1, "读连接未看到写入"
        st.close_run("r1", status="solved", canonical=True, challenge_code="a-05",
                     worker_id="worker-1")
        assert st.run_row("r1")["status"] == "solved"
    finally:
        st.close()


# ── 已接受 flag 的窄通道(assignment 模式的补写) ──

def test_attach_accepted_flags_writes_canonical_row(store):
    """关键性质:加性观测数据可写入 canonical 行(close_run 会拒,本方法不会)。

    assignment 模式下 relay 不关 run,而 flags_accepted 只经 run_close 写入 ——
    若本方法也受 canonical 守卫约束,平台主推模式就永远看不到已获得的 flag。
    """
    run_id = rid()
    store.append_events(run_id, "worker-1", "a-05", [(0, "session", "{}")],
                        attempt_id=run_id)
    store.close_run(run_id, status="solved", canonical=True, attempt_id=run_id,
                    challenge_code="a-05", worker_id="worker-1")
    row = store.run_row(run_id)
    assert row["status"] == "solved" and row["canonical"] is True

    assert store.attach_accepted_flags(run_id, ["flag{a}", "flag{b}"]) is True
    row = store.run_row(run_id)
    assert row["flags_accepted"] == ["flag{a}", "flag{b}"]
    # 只动一列:生命周期字段不受影响
    assert row["status"] == "solved" and row["canonical"] is True
    assert store.challenge_flags("a-05") == ["flag{a}", "flag{b}"]


def test_attach_accepted_flags_falls_back_to_attempt_id(store):
    """relay 侧 run_id != attempt_id 的历史数据也要能补上。"""
    attempt_id = rid()
    store.append_events("relay-run-id", "worker-1", "a-05", [(0, "session", "{}")],
                        attempt_id=attempt_id)
    assert store.attach_accepted_flags(attempt_id, ["flag{x}"]) is True
    assert store.run_row("relay-run-id")["flags_accepted"] == ["flag{x}"]


def test_attach_accepted_flags_no_row_and_empty_are_noops(store):
    """无行不建行(避免幽灵 running 行);空列表不覆盖已有值。"""
    assert store.attach_accepted_flags("never-seen", ["flag{x}"]) is False
    run_id = rid()
    store.append_events(run_id, "worker-1", "a-05", [(0, "session", "{}")])
    assert store.attach_accepted_flags(run_id, []) is False
    assert store.run_row(run_id)["flags_accepted"] == []  # 列 NULL 经投影解码为空表
    assert store.attach_accepted_flags(run_id, ["flag{y}"]) is True
    assert store.attach_accepted_flags(run_id, []) is False
    assert store.run_row(run_id)["flags_accepted"] == ["flag{y}"]


# ── canonical 关闭的字段归属(权威赢下生命周期,不写坏度量) ──

def test_canonical_close_clears_stale_relay_error(store):
    """relay 先写 error,canonical 以 solved 关闭(载荷恒含 error 键) → 陈旧错误被清掉。

    canonical 的 attempt.completed 载荷由 core 恒带 error 键,故 None 表示"无错误";
    若沿用 relay 的"非 None 才写",权威终态下会残留一条早已不成立的错误。
    """
    rid = seed_run(store, events=[attempt_ev(1), session_ev()])
    store.close_run(rid, status="failed", error="relay boom")
    assert store.run_row(rid)["error"] == "relay boom"
    store.close_run(rid, status="solved", error=None, canonical=True)
    row = store.run_row(rid)
    assert row["status"] == "solved" and row["canonical"] is True
    assert row["error"] is None, "权威终态下残留了 relay 的陈旧 error"


def test_canonical_close_does_not_clobber_relay_turns(store):
    """canonical 关闭不传 turns 时,保留 relay 报的真实轮次,而非用事件计数覆盖。"""
    rid = seed_run(store, events=[attempt_ev(1), session_ev()])  # 事件计数:turns=0
    store.close_run(rid, status="done", turns=7, sessions=2)
    store.close_run(rid, status="solved", canonical=True)  # 不传 turns/sessions
    row = store.run_row(rid)
    assert row["turns"] == 7, "canonical 用事件计数覆盖了 relay 的真实轮次"
    assert row["sessions"] == 2
    # 传入值仍可显式覆盖(权威确实知道时)
    store.close_run(rid, status="solved", turns=9, canonical=True)
    assert store.run_row(rid)["turns"] == 9


def test_relay_close_still_keeps_existing_error_when_absent(store):
    """relay 侧语义不变:未带 error 的关闭不擦掉已有信息。"""
    rid = seed_run(store, events=[attempt_ev(1)])
    store.close_run(rid, status="done", error="first")
    # relay 二次关闭会被终态幂等挡住;换一条 running 行验证写入分支本身
    rid2 = seed_run(store, code="b-01", events=[attempt_ev(1)])
    store.close_run(rid2, status="failed", error="kept")
    assert store.run_row(rid2)["error"] == "kept"


# ── 原文事件行保留策略 ──

def test_prune_events_keeps_runs_and_running(store):
    """只删已结束 run 的原文行;runs 行保留(审计链),running 不受影响。"""
    old_done = seed_run(store, code="a-01", started_at=1_000.0,
                        events=[attempt_ev(1), session_ev()])
    store.close_run(old_done, status="solved", ended_at=1_000.0)
    live = seed_run(store, code="a-02", started_at=time.time(),
                    events=[attempt_ev(1), session_ev()])  # 仍 running

    removed = store.prune_events(older_than_days=1.0)
    assert removed == 2, "应删掉已结束 run 的两条原文行"
    # runs 行都还在(含终态与 canonical 标记),审计链不断
    assert store.run_row(old_done)["status"] == "solved"
    assert store.run_row(old_done)["event_count"] == 0
    assert store.run_row(live) is not None
    assert store.run_row(live)["event_count"] == 2, "running run 的原文行不得被清"


def test_prune_events_disabled_and_idempotent(store):
    rid = seed_run(store, code="a-01", events=[attempt_ev(1)])
    store.close_run(rid, status="done", ended_at=1_000.0)
    assert store.prune_events(older_than_days=0) == 0          # 0 = 关闭
    assert store.prune_events(older_than_days=1.0) == 1
    assert store.prune_events(older_than_days=1.0) == 0        # 再跑无残留(不重复选中)
