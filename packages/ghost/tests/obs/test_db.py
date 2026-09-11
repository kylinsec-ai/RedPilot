"""迁移/连接基建测试:空库建表、幂等、pragma。"""

from __future__ import annotations

import sqlite3

import pytest

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


class _CommitBomb:
    """委托真连接,但让 COMMIT 抛错 —— 真事务因此确实留在打开状态。

    为何用代理而非 monkeypatch.execute:sqlite3.Connection 是 C 类型,
    `monkeypatch.setattr(conn, "execute", …)` 直接 AttributeError(read-only)。
    为何不用 `PRAGMA max_page_count` 造 SQLITE_FULL:那种触发下 SQLite 会**自动
    回滚**(in_transaction 已是 False),在旧代码上照样通过,钉不住这个 bug。
    """

    def __init__(self, conn, fails: int = 1):
        self._conn = conn
        self._left = fails

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        if sql == "COMMIT" and self._left:
            self._left -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)


def test_tx_recovers_from_failed_commit(tmp_path, monkeypatch):
    """COMMIT 自身失败后写路径必须仍然可用(磁盘满是现实触发条件)。

    回归:此前 _tx 的 COMMIT 写在 try **之外**,抛错时不会 ROLLBACK,事务悬挂;
    isolation_level=None 下 Python 不跟踪事务状态,于是之后每次 BEGIN IMMEDIATE
    都报 "cannot start a transaction within a transaction" —— 所有写入直到重启
    全部失败。
    """
    store_ = ObsStore(str(tmp_path / "obs.sqlite3"))
    try:
        bomb = _CommitBomb(store_._conn, fails=1)
        monkeypatch.setattr(store_, "_conn", bomb)

        rid = "a" * 32
        with pytest.raises(sqlite3.OperationalError):
            store_.append_events(rid, "worker-1", "a-05", [(0, "session", "{}")])

        assert bomb.in_transaction is False, "失败后事务仍悬挂,写路径已被锁死"

        # 写路径必须自愈(旧代码在这行炸 BEGIN IMMEDIATE)
        n, created = store_.append_events(rid, "worker-1", "a-05", [(0, "session", "{}")])
        assert (n, created) == (1, True)
        # 读路径同样要能看到
        assert store_.run_row(rid) is not None
        assert [e["seq"] for e in store_.events_for_run(rid)] == [0]
    finally:
        store_.close()
