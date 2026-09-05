"""DB bootstrap — SQLite WAL + 版本化迁移(PRAGMA user_version)。

迁移机制:有序幂等 DDL 列表;migrate() 把 user_version 推进到 len(MIGRATIONS),
将来加表/加列只向 MIGRATIONS 追加条目(每条在单事务内原子执行)。
时间戳约定:全库 REAL epoch 秒(payload 原文里的时间戳保持原样)。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 每次迁移 = 一个语句列表(单事务原子)
_MIGRATION_1 = [
    # ── runs: 每次 solve_one 一条(同 code 可多 run 的历史累积) ──
    """
    CREATE TABLE IF NOT EXISTS runs (
      run_id          TEXT PRIMARY KEY,          -- live: uuid4().hex
      worker_id       TEXT NOT NULL,
      challenge_code  TEXT NOT NULL,
      model           TEXT NOT NULL DEFAULT '',
      status          TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','solved','done','failed','interrupted')),
      started_at      REAL NOT NULL,             -- epoch s
      ended_at        REAL,
      duration_s      REAL,
      error           TEXT,                      -- close 载荷(relay 截断 ≤2000)
      turns           INTEGER,                   -- 可空;close 缺省按 tool_execution_start 计数回填
      sessions        INTEGER,                   -- 可空;close 缺省按 type='session' 回填
      flags_found     INTEGER,
      flags_accepted  TEXT,                      -- JSON list[str](=FLAG 文件同信任域)
      updated_at      REAL NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_challenge ON runs(challenge_code, started_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_started   ON runs(started_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_status    ON runs(status, started_at DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_worker    ON runs(worker_id, started_at DESC);
    """,
    # ── events: 原文事件行全量入库(worker 侧已丢 message_update);id 全局单调=摄取序 ──
    """
    CREATE TABLE IF NOT EXISTS events (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
      seq             INTEGER NOT NULL,          -- run 内单调(过滤流行序号),0 起
      worker_id       TEXT NOT NULL,             -- 冗余:免 join 的 worker/全局检索(单写者不变式)
      challenge_code  TEXT NOT NULL,             -- 同上;transcript-tail/维护 DELETE
      type            TEXT NOT NULL,             -- 摄取时解析;payload 为权威
      ts              REAL,                      -- epoch s(worker ship 时刻;无则 NULL)
      payload         TEXT NOT NULL,             -- 原文 JSON 行(与 transcript.jsonl 同信任域)
      UNIQUE (run_id, seq)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_run      ON events(run_id, seq);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_challenge ON events(challenge_code, id);
    """,
    # ── live_state: 兼作 worker 注册表(PK 即注册;首份 live/首 ping 建档) ──
    """
    CREATE TABLE IF NOT EXISTS live_state (
      worker_id  TEXT PRIMARY KEY,
      updated_at REAL NOT NULL,                  -- put_live 与 ping 都刷新(30s ping ≪ 150s 判定)
      snapshot   TEXT NOT NULL                   -- LiveState 18 键 JSON(原键名不动)
    );
    """,
    # ── roster_snapshot: 每 worker 一行(worker 60s 全量覆盖;读侧合并见 store.roster_merged) ──
    """
    CREATE TABLE IF NOT EXISTS roster_snapshot (
      worker_id         TEXT PRIMARY KEY,
      fetched_at        REAL,
      stale             INTEGER NOT NULL DEFAULT 1,
      platform_error    TEXT,
      platform_disabled INTEGER NOT NULL DEFAULT 0,
      payload           TEXT,                    -- 完整 5 键快照 JSON {fetched_at,stale,...,challenges}
      updated_at        REAL NOT NULL
    );
    """,
]

MIGRATIONS: list[list[str]] = [_MIGRATION_1]


def connect(db_path: str | os.PathLike) -> sqlite3.Connection:
    """打开连接并打全套 pragma;调用方负责 close。"""
    path = str(db_path)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """推进 schema 到最新版本;每条迁移单事务原子,幂等(已到最新则无操作)。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, migration in enumerate(MIGRATIONS[version:], start=version):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for stmt in migration:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {i + 1}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def ensure_parent(db_path: str | os.PathLike) -> None:
    path = Path(str(db_path))
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
