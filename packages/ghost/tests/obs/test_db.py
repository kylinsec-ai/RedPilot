"""迁移/连接基建测试:空库建表、幂等、pragma。"""

from __future__ import annotations

import sqlite3

from ghost.obs import db as dbmod
from ghost.obs.store import ObsStore


def _tables(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def test_migrate_creates_all_tables(tmp_path):
    conn = dbmod.connect(str(tmp_path / "obs.sqlite3"))
    try:
        dbmod.migrate(conn)
        tables = _tables(conn)
        assert {"runs", "events", "live_state", "roster_snapshot"} <= tables
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(dbmod.MIGRATIONS)
        # 关键约束/索引就位(events 只靠 UNIQUE(run_id, seq) 自带索引)
        idxs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert {"idx_runs_challenge", "idx_runs_started", "idx_runs_worker"} <= idxs
        # UNIQUE(run_id, seq) 幂等地基
        cur = conn.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()
        assert "UNIQUE (run_id, seq)" in cur[0]
    finally:
        conn.close()


def test_migrate_idempotent(tmp_path):
    conn = dbmod.connect(str(tmp_path / "obs.sqlite3"))
    try:
        dbmod.migrate(conn)
        dbmod.migrate(conn)  # 二次迁移无异常
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(dbmod.MIGRATIONS)
    finally:
        conn.close()


def test_pragmas(tmp_path):
    conn = dbmod.connect(str(tmp_path / "obs.sqlite3"))
    try:
        dbmod.migrate(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000
    finally:
        conn.close()


def test_events_cascade_on_run_delete(tmp_path):
    store_ = ObsStore(str(tmp_path / "obs.sqlite3"))
    try:
        rid = "a" * 32
        store_.append_events(rid, "worker-1", "a-05", [(0, "session", "{}")])
        assert len(store_.events_for_run(rid)) == 1
        with store_._tx():
            store_._conn.execute("DELETE FROM runs WHERE run_id=?", (rid,))
        assert store_.events_for_run(rid) == []
    finally:
        store_.close()


def test_migrate_rebuilds_drifted_events(tmp_path):
    """v1 迁移前遗留的旧形状 events(extra NOT NULL 列)会被 v2 重建为当前形状,
    数据保留、append_events 不再静默吞行 —— 真实平台 22:03 草稿库踩坑的回归。"""
    path = str(tmp_path / "drifted.sqlite3")
    conn = sqlite3.connect(path)
    try:
        # 只把 runs 建成 v1 形状、events 建成旧草稿形状(user_version 假装已是 1)
        conn.executescript("""
          PRAGMA user_version = 1;
          CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
            challenge_code TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running', started_at REAL NOT NULL,
            ended_at REAL, error TEXT, turns INTEGER, sessions INTEGER,
            flags_found INTEGER, flags_accepted TEXT, updated_at REAL NOT NULL);
          CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            seq INTEGER NOT NULL, worker_id TEXT NOT NULL,
            challenge_code TEXT NOT NULL, type TEXT NOT NULL, ts REAL,
            payload TEXT NOT NULL, UNIQUE (run_id, seq));
        """)
        conn.execute(
            "INSERT INTO runs VALUES(?,?,?,?, 'running', 0, NULL, NULL,"
            " NULL, NULL, NULL, NULL, 0)",
            ("a" * 32, "w", "a-05", "m"))
        # 旧草稿写入路径:5 事件列全给(含 extra NOT NULL 列)
        conn.execute("INSERT INTO events(run_id, seq, worker_id, challenge_code,"
                     " type, ts, payload) VALUES(?,?,?,?,?,?,?)",
                     ("a" * 32, 0, "w", "a-05", "session", 0.0, "{}"))
        conn.commit()
    finally:
        conn.close()

    store_ = ObsStore(path)
    try:
        dbmod.migrate(store_._conn)
        assert store_._conn.execute("PRAGMA user_version").fetchone()[0] == len(
            dbmod.MIGRATIONS)
        cols = {r[1] for r in store_._conn.execute("PRAGMA table_info(events)")}
        assert cols == {"id", "run_id", "seq", "type", "payload"}  # extra 列随重建消失
        # 旧数据 5 列保住了
        assert len(store_.events_for_run("a" * 32)) == 1
        # 新形状下 append_events 真插入(旧形状会因 NOT NULL 违约被 OR IGNORE 吞成 0)
        n, created = store_.append_events("b" * 32, "w", "b-01",
                                          [(0, "agent_message", "{}")])
        assert (n, created) == (1, True)
        assert store_.events_for_run("b" * 32)[0]["seq"] == 0
    finally:
        store_.close()
