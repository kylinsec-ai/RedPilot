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
