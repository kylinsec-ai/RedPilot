"""DB bootstrap — SQLite WAL + 版本化迁移(PRAGMA user_version)。

迁移机制:有序幂等 DDL 列表;migrate() 把 user_version 推进到 len(MIGRATIONS),
将来加表/加列只向 MIGRATIONS 追加条目(每条在单事务内原子执行)。
时间戳约定:全库 REAL epoch 秒(payload 原文里的时间戳保持原样)。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from ghost.obs.schema import RUN_STATUSES

_MIGRATION_1 = [
    # ── runs: 每次 solve_one 一条(同 code 可多 run 的历史累积) ──
    """
    CREATE TABLE IF NOT EXISTS runs (
      run_id          TEXT PRIMARY KEY,          -- live: uuid4().hex
      worker_id       TEXT NOT NULL,
      challenge_code  TEXT NOT NULL,
      model           TEXT NOT NULL DEFAULT '',
      status          TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN (%(statuses)s)),
      started_at      REAL NOT NULL,             -- epoch s
      ended_at        REAL,
      error           TEXT,                      -- close 载荷(relay 截断 ≤2000)
      turns           INTEGER,                   -- 可空;close 缺省按 tool_execution_start 计数回填
      sessions        INTEGER,                   -- 可空;close 缺省按 type='session' 回填
      flags_found     INTEGER,
      flags_accepted  TEXT,                      -- JSON list[str](=FLAG 文件同信任域)
      updated_at      REAL NOT NULL
    );
    """ % {"statuses": ", ".join(repr(s) for s in RUN_STATUSES)},
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
    # ── events: 原文事件行全量入库(worker 侧已丢 message_update);id 全局单调=摄取序。
    #    run 级信息(code/worker)只存 runs 行,events 经 run_id 关联 —— 避免每行冗余副本漂移。 ──
    """
    CREATE TABLE IF NOT EXISTS events (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
      seq             INTEGER NOT NULL,          -- run 内单调(过滤流行序号),0 起
      type            TEXT NOT NULL,             -- 摄取时解析;payload 为权威
      payload         TEXT NOT NULL,             -- 原文 JSON 行(与 transcript.jsonl 同信任域)
      UNIQUE (run_id, seq)                       -- 幂等地基;其索引同时服务 run 内 seq 检索
    );
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
      worker_id  TEXT PRIMARY KEY,
      payload    TEXT NOT NULL,                  -- 完整 5 键快照 JSON {fetched_at,stale,...,challenges}
      updated_at REAL NOT NULL
    );
    """,
]

# ── v2: 重建 events 至 v1 形状 ──
# 历史背景:早于本迁移框架的 22:03 草稿库带着 extra 列 worker_id/challenge_code(NOT NULL
# 无默认)与当前 INSERT 四列相撞 → INSERT OR IGNORE 每行静默吞(NOT NULL 违约),post_events
# 恒回 200 + inserted:0,事件"入库成功"实为零行。迁移幂等:对 v1 新形状的库同样安全
# (空拷贝后换名);version≥2 自动跳过。载入数据只取 5 列,旧冗余列随 DROP 消失。
_MIGRATION_2 = [
    """
    CREATE TABLE events_v2 (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
      seq             INTEGER NOT NULL,
      type            TEXT NOT NULL,
      payload         TEXT NOT NULL,
      UNIQUE (run_id, seq)
    );
    """,
    """
    INSERT INTO events_v2(run_id, seq, type, payload)
      SELECT run_id, seq, type, payload FROM events ORDER BY id;
    """,
    "DROP TABLE events;",
    "ALTER TABLE events_v2 RENAME TO events;",
    # 无需再建 idx_events_run:UNIQUE(run_id, seq) 约束自带同键 autoindex(见 v1 注释)
]

# ── v3: 将观测 run 与控制面 evaluation/job/attempt 关联 ──
_MIGRATION_3 = [
    "ALTER TABLE runs ADD COLUMN evaluation_id TEXT",
    "ALTER TABLE runs ADD COLUMN job_id TEXT",
    "ALTER TABLE runs ADD COLUMN attempt_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_runs_attempt ON runs(attempt_id)",
    "CREATE INDEX IF NOT EXISTS idx_runs_evaluation ON runs(evaluation_id, started_at DESC)",
]

# ── v4: canonical 终态标记 ──
# 权威事件(attempt.completed)写入时置 1;relay run_close 不能覆盖已存在的 canonical 终态。
_MIGRATION_4 = [
    "ALTER TABLE runs ADD COLUMN canonical INTEGER NOT NULL DEFAULT 0",
]

MIGRATIONS: list[list[str]] = [_MIGRATION_1, _MIGRATION_2, _MIGRATION_3, _MIGRATION_4]


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
