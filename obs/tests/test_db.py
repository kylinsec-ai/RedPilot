"""迁移/连接基建测试:空库建表、幂等、pragma。"""

from __future__ import annotations

from obs import db as dbmod
from obs.store import ObsStore


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
        # 关键约束/索引就位
        idxs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert {"idx_events_run", "idx_events_challenge", "idx_runs_worker"} <= idxs
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
        store_.ensure_run(rid, "worker-1", "a-05")
        store_.insert_events(rid, "worker-1", "a-05", [(0, "session", None, "{}")])
        assert len(store_.events_for_run(rid)) == 1
        with store_._tx():
            store_._conn.execute("DELETE FROM runs WHERE run_id=?", (rid,))
        assert store_.events_for_run(rid) == []
    finally:
        store_.close()
