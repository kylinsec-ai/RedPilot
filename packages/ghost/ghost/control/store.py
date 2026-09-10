"""SQLite-backed task, challenge, container, and submission state."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
import time
from typing import Any, Iterator, Mapping

from ghost_contracts.platform import EventEnvelope, new_id

from ghost.control.models import ChallengeDefinition, FlagDefinition, TaskDefinition, flags_json


@dataclass(frozen=True)
class ChallengeRow:
    task_token: str
    definition: ChallengeDefinition
    container_status: str
    container_addresses: tuple[str, ...]
    container_id: str | None
    hint_viewed: bool


@dataclass(frozen=True)
class SubmissionRow:
    flag_index: int
    awarded: int


@dataclass(frozen=True)
class AssignmentRow:
    """A leased job plus the immutable challenge data needed by a worker."""

    evaluation_id: str
    job_id: str
    attempt_id: str
    lease_id: str
    lease_expires_at: float
    task_token: str
    unique_code: str
    challenge: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not math.isfinite(self.lease_expires_at):
            raise ValueError("lease_expires_at must be finite")


class DuplicateSubmission(Exception):
    """A flag index was already recorded for a task challenge."""


class Store:
    """Small transactional repository using one thread-safe SQLite connection."""

    def __init__(self, database_path: str = "./data/ghost.sqlite3") -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        if database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._create_schema()
        self._recover_inflight_containers()

    def _create_schema(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    token TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS challenges (
                    task_token TEXT NOT NULL,
                    unique_code TEXT NOT NULL,
                    description TEXT,
                    difficulty TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    total_score INTEGER NOT NULL,
                    flags_json TEXT NOT NULL,
                    hint TEXT,
                    hint_cost_radio REAL NOT NULL,
                    configured_addr_json TEXT NOT NULL,
                    container_addr_json TEXT NOT NULL,
                    container_status TEXT NOT NULL,
                    container_id TEXT,
                    image TEXT,
                    container_port INTEGER,
                    docker_network TEXT,
                    hint_viewed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (task_token, unique_code),
                    FOREIGN KEY (task_token) REFERENCES tasks(token) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    task_token TEXT NOT NULL,
                    unique_code TEXT NOT NULL,
                    flag_index INTEGER NOT NULL,
                    awarded INTEGER NOT NULL,
                    submitted_at TEXT NOT NULL,
                    PRIMARY KEY (task_token, unique_code, flag_index),
                    FOREIGN KEY (task_token, unique_code)
                        REFERENCES challenges(task_token, unique_code) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS evaluations (
                    evaluation_id TEXT PRIMARY KEY,
                    task_token TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    ended_at REAL,
                    FOREIGN KEY (task_token) REFERENCES tasks(token) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    capabilities_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_seen_at REAL NOT NULL,
                    registered_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    evaluation_id TEXT NOT NULL,
                    task_token TEXT NOT NULL,
                    unique_code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_status TEXT,
                    attempt_no INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_id TEXT,
                    lease_expires_at REAL,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE (evaluation_id, unique_code),
                    FOREIGN KEY (evaluation_id) REFERENCES evaluations(evaluation_id) ON DELETE CASCADE,
                    FOREIGN KEY (task_token, unique_code)
                        REFERENCES challenges(task_token, unique_code) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    solved INTEGER NOT NULL DEFAULT 0,
                    flags_found INTEGER,
                    error TEXT,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS platform_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    evaluation_id TEXT,
                    job_id TEXT,
                    attempt_id TEXT,
                    worker_id TEXT,
                    seq INTEGER NOT NULL,
                    occurred_at REAL NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE (attempt_id, seq)
                );

                CREATE TABLE IF NOT EXISTS outbox_events (
                    event_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    delivered_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_claim
                    ON jobs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_attempts_job
                    ON attempts(job_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_platform_events_attempt
                    ON platform_events(attempt_id, seq);
                """
            )
        with self._transaction() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(challenges)")}
            for name, definition in {
                "image": "TEXT",
                "container_port": "INTEGER",
                "docker_network": "TEXT",
            }.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE challenges ADD COLUMN {name} {definition}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _recover_inflight_containers(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE challenges
                   SET container_status = 'stopped',
                       container_addr_json = '[]',
                       container_id = NULL
                 WHERE container_status IN ('pending', 'stop_pending')
                """
            )

    def insert_task(self, task: TaskDefinition, *, ignore_existing: bool = False) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO tasks(token, state, expires_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (task.token, task.state, task.expires_at, now, now),
                )
            except sqlite3.IntegrityError:
                if not ignore_existing:
                    raise
                return False
            for challenge in task.challenges:
                connection.execute(
                    """
                    INSERT INTO challenges(
                        task_token, unique_code, description, difficulty, level,
                        total_score, flags_json, hint, hint_cost_radio,
                        configured_addr_json, image, container_port, docker_network,
                        container_addr_json, container_status,
                        container_id, hint_viewed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 'stopped', NULL, 0)
                    """,
                    (
                        task.token,
                        challenge.unique_code,
                        challenge.description,
                        challenge.difficulty,
                        challenge.level,
                        challenge.total_score,
                        flags_json(challenge.flags),
                        challenge.hint,
                        challenge.hint_cost_radio,
                        json.dumps(challenge.container_addr, separators=(",", ":")),
                        challenge.image,
                        challenge.container_port,
                        challenge.docker_network,
                    ),
                )
        return cursor.rowcount == 1

    def task_config_drift(self, task: TaskDefinition) -> list[str]:
        """比对**已存在**任务的配置与传入定义,返回差异描述(供 seed 告警)。

        只比"会影响判分或供给"的列:flags(评分密钥,哈希)、分值、hint、题目集合、
        容器配置。**刻意不比** container_status/container_addr/container_id/hint_viewed
        —— 那些是运行时状态,本来就不该由配置覆盖。
        """
        drift: list[str] = []
        with self._lock:
            rows = self._connection.execute(
                "SELECT unique_code, total_score, flags_json, hint, hint_cost_radio,"
                " image, container_port, docker_network"
                " FROM challenges WHERE task_token = ?",
                (task.token,),
            ).fetchall()
        stored = {row["unique_code"]: row for row in rows}
        incoming = {c.unique_code: c for c in task.challenges}

        for code in sorted(set(stored) - set(incoming)):
            drift.append(f"challenge {code} removed from config")
        for code in sorted(set(incoming) - set(stored)):
            drift.append(f"challenge {code} added in config (not materialized)")
        for code in sorted(set(stored) & set(incoming)):
            row, definition = stored[code], incoming[code]
            if row["flags_json"] != flags_json(definition.flags):
                drift.append(f"challenge {code} flags changed")
            if row["total_score"] != definition.total_score:
                drift.append(f"challenge {code} total_score changed")
            if (row["hint"] or "") != (definition.hint or ""):
                drift.append(f"challenge {code} hint changed")
            if row["hint_cost_radio"] != definition.hint_cost_radio:
                drift.append(f"challenge {code} hint_cost_radio changed")
            for column, value in (("image", definition.image),
                                  ("container_port", definition.container_port),
                                  ("docker_network", definition.docker_network)):
                if (row[column] or "") != (value or ""):
                    drift.append(f"challenge {code} {column} changed")
        return drift

    def has_task(self, token: str) -> bool:
        with self._lock:
            row = self._connection.execute("SELECT 1 FROM tasks WHERE token = ?", (token,)).fetchone()
        return row is not None

    @staticmethod
    def _expired(expires_at: str | None) -> bool:
        if not expires_at:
            return False
        text = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
        try:
            expiry = datetime.fromisoformat(text)
        except ValueError:
            return True
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= expiry.astimezone(timezone.utc)

    def task_is_active(self, token: str) -> bool:
        with self._lock:
            row = self._connection.execute("SELECT state, expires_at FROM tasks WHERE token = ?", (token,)).fetchone()
            if row is None:
                return False
            if row["state"] != "active":
                return False
            if self._expired(row["expires_at"]):
                now = datetime.now(timezone.utc).isoformat()
                self._connection.execute("UPDATE tasks SET state = 'expired', updated_at = ? WHERE token = ?", (now, token))
                return False
        return True

    def stop_task(self, token: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET state = 'stopped', updated_at = ? WHERE token = ?",
                (datetime.now(timezone.utc).isoformat(), token),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _decode_addresses(value: str) -> tuple[str, ...]:
        decoded = json.loads(value)
        return tuple(str(item) for item in decoded)

    @staticmethod
    def _decode_flags(value: str) -> tuple[FlagDefinition, ...]:
        decoded = json.loads(value)
        return tuple(FlagDefinition.from_json(item) for item in decoded)

    def _challenge_from_row(self, row: sqlite3.Row) -> ChallengeRow:
        definition = ChallengeDefinition(
            unique_code=row["unique_code"],
            description=row["description"],
            difficulty=row["difficulty"],
            level=row["level"],
            total_score=row["total_score"],
            flags=self._decode_flags(row["flags_json"]),
            hint=row["hint"],
            hint_cost_radio=float(row["hint_cost_radio"]),
            container_addr=self._decode_addresses(row["configured_addr_json"]),
            image=row["image"],
            container_port=row["container_port"],
            docker_network=row["docker_network"],
        )
        return ChallengeRow(
            task_token=row["task_token"],
            definition=definition,
            container_status=row["container_status"],
            container_addresses=self._decode_addresses(row["container_addr_json"]),
            container_id=row["container_id"],
            hint_viewed=bool(row["hint_viewed"]),
        )

    def get_challenge(self, token: str, unique_code: str) -> ChallengeRow | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM challenges WHERE task_token = ? AND unique_code = ?",
                (token, unique_code),
            ).fetchone()
        return self._challenge_from_row(row) if row is not None else None

    def list_challenges(self, token: str) -> tuple[ChallengeRow, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM challenges WHERE task_token = ? ORDER BY rowid",
                (token,),
            ).fetchall()
        return tuple(self._challenge_from_row(row) for row in rows)

    def active_container_count(self, token: str) -> int:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT COUNT(*) AS count FROM challenges
                 WHERE task_token = ? AND container_status IN ('pending', 'available')
                """,
                (token,),
            ).fetchone()
        return int(row["count"])

    def reserve_container(self, token: str, unique_code: str, max_active: int) -> str:
        """Reserve a stopped challenge without exceeding the task limit.

        The return value is one of ``missing``, ``available``,
        ``transitioning``, ``limit``, or ``reserved``.
        """
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT container_status FROM challenges
                 WHERE task_token = ? AND unique_code = ?
                """,
                (token, unique_code),
            ).fetchone()
            if row is None:
                return "missing"
            status = row["container_status"]
            if status == "available":
                return "available"
            if status in {"pending", "stop_pending"}:
                return "transitioning"
            count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM challenges
                 WHERE task_token = ? AND container_status IN ('pending', 'available')
                """,
                (token,),
            ).fetchone()["count"]
            if int(count) >= max_active:
                return "limit"
            updated = connection.execute(
                """
                UPDATE challenges
                   SET container_status = 'pending', container_addr_json = '[]', container_id = NULL
                 WHERE task_token = ? AND unique_code = ? AND container_status = 'stopped'
                """,
                (token, unique_code),
            )
            return "reserved" if updated.rowcount == 1 else "transitioning"

    def set_container(
        self,
        token: str,
        unique_code: str,
        status: str,
        addresses: tuple[str, ...] = (),
        container_id: str | None = None,
        *,
        expect: tuple[str, ...] | None = None,
    ) -> bool:
        """容器状态迁移的**唯一写者**。expect 非空时走 CAS,返回是否迁移成功。

        为何必须 CAS:start/close 都是两段式(锁内改状态 → 锁外调 provisioner
        side effect,秒级 → 回锁内落终态)。没有前置条件时,交错会产出
        "客户端被告知已关闭、行却是 available、容器永不回收" —— 以及失败路径用
        stopped 抹掉并发成功 start 的 addresses/container_id。

        语义约定:调用方**输掉 CAS 即表示状态已被并发推进越过自己**,此时只做
        副作用补偿,绝不写状态(见 service.start/close)。
        """
        sql = ("UPDATE challenges"
               "   SET container_status = ?, container_addr_json = ?, container_id = ?"
               " WHERE task_token = ? AND unique_code = ?")
        params: list[Any] = [
            status,
            json.dumps(addresses, separators=(",", ":")),
            container_id,
            token,
            unique_code,
        ]
        if expect:
            sql += " AND container_status IN (%s)" % ",".join("?" * len(expect))
            params.extend(expect)
        with self._transaction() as connection:
            cursor = connection.execute(sql, params)
        return cursor.rowcount == 1

    def mark_hint_viewed(self, token: str, unique_code: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE challenges SET hint_viewed = 1 WHERE task_token = ? AND unique_code = ?",
                (token, unique_code),
            )

    def delete_challenge(self, token: str, unique_code: str) -> bool:
        """删除题目行（提交记录随外键 ON DELETE CASCADE 级联删除）"""
        with self._lock:
            cur = self._connection.execute(
                "DELETE FROM challenges WHERE task_token = ? AND unique_code = ?",
                (token, unique_code),
            )
        return cur.rowcount > 0

    def submissions(self, token: str, unique_code: str) -> tuple[SubmissionRow, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT flag_index, awarded FROM submissions
                 WHERE task_token = ? AND unique_code = ? ORDER BY flag_index
                """,
                (token, unique_code),
            ).fetchall()
        return tuple(SubmissionRow(flag_index=int(row["flag_index"]), awarded=int(row["awarded"])) for row in rows)

    def record_submission(self, token: str, unique_code: str, flag_index: int, awarded: int) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO submissions(task_token, unique_code, flag_index, awarded, submitted_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (token, unique_code, flag_index, awarded, datetime.now(timezone.utc).isoformat()),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateSubmission from exc

    def task_tokens(self) -> tuple[str, ...]:
        with self._lock:
            rows = self._connection.execute("SELECT token FROM tasks ORDER BY created_at").fetchall()
        return tuple(str(row["token"]) for row in rows)

    # ── 控制面: evaluation / job / attempt / worker ──────────────

    @staticmethod
    def _evaluation_payload_locked(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        counts = {
            str(item["status"]): int(item["count"])
            for item in connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs WHERE evaluation_id = ? GROUP BY status",
                (row["evaluation_id"],),
            )
        }
        total = sum(counts.values())
        # 注意:task_token 是题目鉴权秘密,只经 claim assignment 下发 worker;
        # evaluation 列表/详情面向浏览器,不携带(防批量收割)。
        return {
            "evaluation_id": row["evaluation_id"],
            "project_id": row["project_id"],
            "status": row["status"],
            "job_count": total,
            "pending_count": counts.get("pending", 0),
            "running_count": counts.get("running", 0),
            "completed_count": counts.get("completed", 0),
            "failed_count": counts.get("failed", 0),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }

    def create_evaluation(
        self,
        task_token: str,
        *,
        project_id: str = "default",
        idempotency_key: str | None = None,
    ) -> dict:
        """Create one evaluation and materialize one job per challenge.

        The operation is transactional: a visible evaluation always has a
        complete job set, and retrying with the same idempotency key returns
        the original evaluation.
        """

        now = time.time()
        with self._transaction() as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM evaluations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["task_token"] != task_token:
                        raise ValueError("idempotency_key_reuse")
                    return self._evaluation_payload_locked(connection, existing)

            task = connection.execute(
                "SELECT state FROM tasks WHERE token = ?", (task_token,)
            ).fetchone()
            if task is None:
                raise KeyError("task_not_found")
            if task["state"] != "active":
                raise ValueError("task_not_active")

            evaluation_id = new_id()
            connection.execute(
                """
                INSERT INTO evaluations(
                    evaluation_id, task_token, project_id, status,
                    idempotency_key, created_at
                ) VALUES (?, ?, ?, 'queued', ?, ?)
                """,
                (evaluation_id, task_token, project_id, idempotency_key, now),
            )
            challenges = connection.execute(
                """
                SELECT unique_code FROM challenges
                 WHERE task_token = ? ORDER BY rowid
                """,
                (task_token,),
            ).fetchall()
            for challenge in challenges:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, evaluation_id, task_token, unique_code, status,
                        attempt_no, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                    """,
                    (new_id(), evaluation_id, task_token, challenge["unique_code"], now, now),
                )
            if not challenges:
                connection.execute(
                    "UPDATE evaluations SET status = 'completed', ended_at = ? WHERE evaluation_id = ?",
                    (now, evaluation_id),
                )
            row = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
            ).fetchone()
            assert row is not None
            return self._evaluation_payload_locked(connection, row)

    def get_evaluation(self, evaluation_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
            ).fetchone()
            return (
                self._evaluation_payload_locked(self._connection, row)
                if row is not None
                else None
            )

    def list_evaluations(self, project_id: str | None = None) -> list[dict]:
        with self._lock:
            if project_id:
                rows = self._connection.execute(
                    "SELECT * FROM evaluations WHERE project_id = ? ORDER BY created_at DESC",
                    (project_id,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM evaluations ORDER BY created_at DESC"
                ).fetchall()
            return [self._evaluation_payload_locked(self._connection, row) for row in rows]

    def _reap_expired_leases_locked(self, connection, now: float) -> list[str]:
        """回收过期租约:在飞 attempt 置 interrupted(job 回 pending),并**发 canonical 事件**。

        此前是两段裸 UPDATE,不发事件 —— 契约要求 interrupted 也是 canonical 终态
        (ghost_contracts.CANONICAL_TERMINAL_STATUSES),不发则 obs 侧 run 永挂 running。

        必须在调用方事务内运行(_locked 后缀):claim_job 自己已持有 BEGIN IMMEDIATE,
        嵌套开事务会失败。
        """
        expired = connection.execute(
            """
            SELECT a.attempt_id, a.worker_id, j.job_id, j.evaluation_id
              FROM jobs j JOIN attempts a ON a.job_id = j.job_id
             WHERE j.status = 'running' AND j.lease_expires_at IS NOT NULL
               AND j.lease_expires_at < ?
               AND a.status IN ('starting', 'solving', 'submitting', 'closing')
            """,
            (now,),
        ).fetchall()
        reaped: list[str] = []
        for attempt in expired:
            self._terminate_attempt_locked(
                connection,
                attempt_id=attempt["attempt_id"],
                row=attempt,
                status="interrupted",
                error="lease expired",
                worker_id=attempt["worker_id"],
                reason="lease_expired",
                now=now,
            )
            reaped.append(attempt["attempt_id"])
        return reaped

    def reap_expired_leases(self) -> list[str]:
        """公开入口(lease sweeper 用):自己开事务回收过期租约。

        放在 core 而非 obs housekeeper:lease/attempt/job 都是 core 的表,且事件要写进
        core 的 platform_events + outbox_events;obs 不得 import control。
        """
        with self._transaction() as connection:
            return self._reap_expired_leases_locked(connection, time.time())

    def _terminate_attempt_locked(
        self,
        connection,
        *,
        attempt_id: str,
        row,
        status: str,
        solved: bool = False,
        flags_found: int | None = None,
        error: str | None = None,
        worker_id: str | None = None,
        reason: str | None = None,
        now: float | None = None,
    ) -> None:
        """单题终态写入 + canonical `attempt.completed` —— **唯一终态写者**。

        抽取原因:lease 过期回收与 evaluation 取消此前各自用裸 UPDATE 写
        attempts.status,绕过了事件写入。而 ghost_contracts.CANONICAL_TERMINAL_STATUSES
        含 `interrupted`,即契约要求这些终态也必须发 canonical 事件 —— 不发则 obs 侧
        run 永远停在 running(权威终态缺失),且 core 的 platform_events 序列不完整。

        调用方必须已在同一事务内取好 row(需含 job_id/evaluation_id)。
        reason 进 payload(lease_expired / evaluation_canceled),供观测侧区分来源。
        """
        now = now if now is not None else time.time()
        if status == "interrupted":
            job_status, worker_value = "pending", None
        elif status in {"solved", "done"}:
            job_status, worker_value = "completed", worker_id
        else:
            job_status, worker_value = "failed", worker_id
        connection.execute(
            """
            UPDATE attempts SET status = ?, solved = ?, flags_found = ?,
                error = ?, ended_at = ?, updated_at = ?
             WHERE attempt_id = ?
            """,
            (status, int(solved), flags_found, error, now, now, attempt_id),
        )
        connection.execute(
            """
            UPDATE jobs SET status = ?, result_status = ?, worker_id = ?,
                lease_id = NULL, lease_expires_at = NULL, error = ?, updated_at = ?
             WHERE job_id = ?
            """,
            (job_status, status, worker_value, error, now, row["job_id"]),
        )
        payload: dict = {"status": status, "solved": bool(solved),
                         "flags_found": flags_found, "error": error}
        if reason:
            payload["reason"] = reason
        event = EventEnvelope.create(
            "attempt.completed",
            evaluation_id=row["evaluation_id"],
            job_id=row["job_id"],
            attempt_id=attempt_id,
            worker_id=worker_id or "",
            seq=self._next_event_seq_locked(connection, attempt_id),
            payload=payload,
        )
        self._insert_event_locked(connection, event)
        if worker_id:
            connection.execute(
                "UPDATE workers SET status = 'idle', last_seen_at = ?, updated_at = ?"
                " WHERE worker_id = ?",
                (now, now, worker_id),
            )
        # evaluation 收口:不再有 pending/running job 时置 completed。
        remaining = connection.execute(
            """
            SELECT COUNT(*) AS count FROM jobs
             WHERE evaluation_id = ? AND status IN ('pending', 'running')
            """,
            (row["evaluation_id"],),
        ).fetchone()["count"]
        if int(remaining) == 0:
            connection.execute(
                "UPDATE evaluations SET status = 'completed', ended_at = ?"
                " WHERE evaluation_id = ?",
                (now, row["evaluation_id"]),
            )

    def cancel_evaluation(self, evaluation_id: str) -> dict | None:
        now = time.time()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] not in {"completed", "canceled", "expired"}:
                # 顺序要紧:先把在飞的 attempt 走**统一终态写者**(发 canonical 事件、
                # job 回 pending),再把仍在 pending/running 的 job 置 canceled,
                # 最后置 evaluation。反过来会让终态写入找不到 running job。
                inflight = connection.execute(
                    """
                    SELECT a.attempt_id, a.worker_id, a.status AS attempt_status,
                           j.job_id, j.evaluation_id
                      FROM attempts a JOIN jobs j ON j.job_id = a.job_id
                     WHERE j.evaluation_id = ?
                       AND a.status IN ('starting', 'solving', 'submitting', 'closing')
                    """,
                    (evaluation_id,),
                ).fetchall()
                for attempt in inflight:
                    self._terminate_attempt_locked(
                        connection,
                        attempt_id=attempt["attempt_id"],
                        row=attempt,
                        status="interrupted",
                        error="evaluation canceled",
                        worker_id=attempt["worker_id"],
                        reason="evaluation_canceled",
                        now=now,
                    )
                connection.execute(
                    """
                    UPDATE evaluations SET status = 'canceled', ended_at = ?
                     WHERE evaluation_id = ?
                    """,
                    (now, evaluation_id),
                )
                connection.execute(
                    """
                    UPDATE jobs
                       SET status = 'canceled', lease_id = NULL,
                           lease_expires_at = NULL, updated_at = ?
                     WHERE evaluation_id = ? AND status IN ('pending', 'running')
                    """,
                    (now, evaluation_id),
                )
            row = connection.execute(
                "SELECT * FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)
            ).fetchone()
            assert row is not None
            return self._evaluation_payload_locked(connection, row)

    def register_worker(self, worker_id: str, capabilities: dict | None = None) -> dict:
        now = time.time()
        encoded = json.dumps(capabilities or {}, ensure_ascii=False, sort_keys=True)
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO workers(
                    worker_id, capabilities_json, status, last_seen_at,
                    registered_at, updated_at
                ) VALUES (?, ?, 'idle', ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    capabilities_json = excluded.capabilities_json,
                    status = CASE WHEN workers.status = 'draining' THEN 'draining' ELSE 'idle' END,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (worker_id, encoded, now, now, now),
            )
            row = connection.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        assert row is not None
        return self._worker_payload(row)

    @staticmethod
    def _worker_payload(row: sqlite3.Row) -> dict:
        try:
            capabilities = json.loads(row["capabilities_json"])
        except (TypeError, ValueError):
            capabilities = {}
        return {
            "worker_id": row["worker_id"],
            "capabilities": capabilities if isinstance(capabilities, dict) else {},
            "status": row["status"],
            "last_seen_at": row["last_seen_at"],
            "registered_at": row["registered_at"],
            "updated_at": row["updated_at"],
        }

    def list_workers(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM workers ORDER BY worker_id"
            ).fetchall()
            return [self._worker_payload(row) for row in rows]

    def worker_heartbeat(self, worker_id: str, status: str = "idle") -> bool:
        now = time.time()
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE workers SET status = ?, last_seen_at = ?, updated_at = ?
                 WHERE worker_id = ?
                """,
                (status, now, now, worker_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _next_event_seq_locked(connection: sqlite3.Connection, attempt_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) AS next_seq FROM platform_events WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return int(row["next_seq"])

    @staticmethod
    def _insert_event_locked(connection: sqlite3.Connection, event: EventEnvelope) -> bool:
        encoded = json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO platform_events(
                event_id, event_type, evaluation_id, job_id, attempt_id,
                worker_id, seq, occurred_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.evaluation_id,
                event.job_id,
                event.attempt_id,
                event.worker_id,
                event.seq,
                event.occurred_at,
                encoded,
            ),
        )
        if cursor.rowcount != 1:
            return False
        connection.execute(
            """
            INSERT OR IGNORE INTO outbox_events(event_id, payload, created_at)
            VALUES (?, ?, ?)
            """,
            (event.event_id, encoded, time.time()),
        )
        return True

    def claim_job(self, worker_id: str, lease_seconds: int = 300) -> AssignmentRow | None:
        now = time.time()
        expires = now + lease_seconds
        with self._transaction() as connection:
            worker = connection.execute(
                "SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if worker is None:
                raise KeyError("worker_not_registered")

            self._reap_expired_leases_locked(connection, now)

            row = connection.execute(
                """
                SELECT j.*, e.project_id, c.description, c.difficulty, c.level,
                       c.total_score, c.flags_json,
                       (SELECT COUNT(*) FROM submissions s
                         WHERE s.task_token = j.task_token
                           AND s.unique_code = j.unique_code) AS correct_flag_count
                  FROM jobs j
                  JOIN evaluations e ON e.evaluation_id = j.evaluation_id
                  JOIN tasks t ON t.token = j.task_token
                  JOIN challenges c ON c.task_token = j.task_token
                                   AND c.unique_code = j.unique_code
                 WHERE j.status = 'pending'
                   AND e.status IN ('queued', 'running')
                   AND t.state = 'active'
                 ORDER BY j.created_at, j.job_id
                 LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.execute(
                    "UPDATE workers SET status = 'idle', last_seen_at = ?, updated_at = ? WHERE worker_id = ?",
                    (now, now, worker_id),
                )
                return None

            evaluation_id = row["evaluation_id"]
            job_id = row["job_id"]
            attempt_id = new_id()
            lease_id = new_id()
            attempt_no = int(row["attempt_no"]) + 1
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'running', attempt_no = ?, worker_id = ?,
                    lease_id = ?, lease_expires_at = ?, updated_at = ?
                 WHERE job_id = ? AND status = 'pending'
                """,
                (attempt_no, worker_id, lease_id, expires, now, job_id),
            )
            if cursor.rowcount != 1:
                # 并发 claim 丢了这一行:另一 worker 先行,本次领取作空,调用方按空队
                # 列轮询。
                #
                # 注意 evaluation 置 running **必须放在 CAS 成功之后**:此前它先于 CAS
                # 执行,丢单时也会提交,把 evaluation 无端推到 running —— 若这是它唯一的
                # job 且已被人领走,尚可由对方完成;但"无 challenge 行"的边界下会留下
                # 一个永远无人推进的 running evaluation。
                connection.execute(
                    "UPDATE workers SET status = 'idle', last_seen_at = ?, updated_at = ? WHERE worker_id = ?",
                    (now, now, worker_id),
                )
                return None
            connection.execute(
                """
                UPDATE evaluations SET status = 'running',
                    started_at = COALESCE(started_at, ?)
                 WHERE evaluation_id = ?
                """,
                (now, evaluation_id),
            )
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, job_id, worker_id, lease_id, status,
                    started_at, updated_at
                ) VALUES (?, ?, ?, ?, 'starting', ?, ?)
                """,
                (attempt_id, job_id, worker_id, lease_id, now, now),
            )
            connection.execute(
                "UPDATE workers SET status = 'busy', last_seen_at = ?, updated_at = ? WHERE worker_id = ?",
                (now, now, worker_id),
            )
            event = EventEnvelope.create(
                "attempt.started",
                evaluation_id=evaluation_id,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=worker_id,
                payload={"unique_code": row["unique_code"], "attempt_no": attempt_no},
            )
            self._insert_event_locked(connection, event)
            try:
                flags = json.loads(row["flags_json"])
            except (TypeError, ValueError):
                flags = []
            challenge = {
                "unique_code": row["unique_code"],
                "description": row["description"],
                "difficulty": row["difficulty"],
                "level": row["level"],
                "total_score": row["total_score"],
                "flag_count": len(flags),
                "correct_flag_count": row["correct_flag_count"],
            }
            return AssignmentRow(
                evaluation_id=evaluation_id,
                job_id=job_id,
                attempt_id=attempt_id,
                lease_id=lease_id,
                lease_expires_at=expires,
                task_token=row["task_token"],
                unique_code=row["unique_code"],
                challenge=challenge,
            )

    def heartbeat_assignment(
        self, attempt_id: str, worker_id: str, lease_id: str, lease_seconds: int = 300
    ) -> bool:
        now = time.time()
        expires = now + lease_seconds
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT job_id FROM attempts
                 WHERE attempt_id = ? AND worker_id = ? AND lease_id = ?
                   AND status IN ('starting', 'solving', 'submitting', 'closing')
                """,
                (attempt_id, worker_id, lease_id),
            ).fetchone()
            if row is None:
                return False
            # 先确认 job 租约归属;失败(错 worker/stale lease/已取消)直接返回,不得留下 updated_at/worker 状态变更。
            #
            # 过期校验是必需的:没有它,早已过期的租约只要赶在别人 claim 之前续一次
            # 就能"复活",把 job 永久钉在 running(claim_job 的机会性回收再也回收不到它)。
            cursor = connection.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                 WHERE job_id = ? AND worker_id = ? AND lease_id = ? AND status = 'running'
                   AND lease_expires_at IS NOT NULL AND lease_expires_at >= ?
                """,
                (expires, now, row["job_id"], worker_id, lease_id, now),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "UPDATE attempts SET updated_at = ? WHERE attempt_id = ?",
                (now, attempt_id),
            )
            connection.execute(
                "UPDATE workers SET status = 'busy', last_seen_at = ?, updated_at = ? WHERE worker_id = ?",
                (now, now, worker_id),
            )
        return True

    def append_attempt_events(
        self, attempt_id: str, worker_id: str, lease_id: str,
        events: list[EventEnvelope],
    ) -> int:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT j.evaluation_id, j.job_id, a.status
                  FROM attempts a JOIN jobs j ON j.job_id = a.job_id
                 WHERE a.attempt_id = ? AND a.worker_id = ? AND a.lease_id = ?
                """,
                (attempt_id, worker_id, lease_id),
            ).fetchone()
            if row is None or row["status"] not in {"starting", "solving", "submitting", "closing"}:
                raise PermissionError("lease_conflict")
            inserted = 0
            for event in events:
                if event.attempt_id != attempt_id:
                    raise ValueError("event_attempt_mismatch")
                inserted += int(self._insert_event_locked(connection, event))
            now = time.time()
            connection.execute(
                "UPDATE attempts SET updated_at = ? WHERE attempt_id = ?",
                (now, attempt_id),
            )
            return inserted

    def attempt_context(self, attempt_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT j.evaluation_id, j.job_id, a.worker_id, a.lease_id, a.status
                  FROM attempts a JOIN jobs j ON j.job_id = a.job_id
                 WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def complete_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        lease_id: str,
        *,
        status: str,
        solved: bool = False,
        flags_found: int | None = None,
        error: str | None = None,
    ) -> dict:
        now = time.time()
        if status not in {"solved", "done", "failed", "interrupted"}:
            raise ValueError("invalid_attempt_status")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, j.evaluation_id, j.job_id, j.status AS job_status
                  FROM attempts a JOIN jobs j ON j.job_id = a.job_id
                 WHERE a.attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError("attempt_not_found")
            # 租约校验先于幂等返回:任意持有 worker token 的调用者不得在未知 lease 下重放 terminal attempt 并获知其终态;
            # 同 lease 重试仍走下方幂等分支,错 lease/已取消一律 lease_conflict
            if row["worker_id"] != worker_id or row["lease_id"] != lease_id:
                raise PermissionError("lease_conflict")
            if row["job_status"] == "canceled":
                raise PermissionError("lease_conflict")
            terminal = {"solved", "done", "failed", "interrupted"}
            if row["status"] in terminal:
                return {
                    "attempt_id": attempt_id,
                    "job_id": row["job_id"],
                    "status": row["status"],
                    "solved": bool(row["solved"]),
                    "idempotent": True,
                }

            # 终态写入与 canonical 事件单源在 _terminate_attempt_locked
            # (lease 过期与 evaluation 取消走同一实现,不再各自裸写)。
            self._terminate_attempt_locked(
                connection,
                attempt_id=attempt_id,
                row=row,
                status=status,
                solved=solved,
                flags_found=flags_found,
                error=error,
                worker_id=worker_id,
                now=now,
            )
            return {
                "attempt_id": attempt_id,
                "job_id": row["job_id"],
                "status": status,
                "solved": bool(solved),
                "idempotent": False,
            }

    def attempt_events(self, attempt_id: str) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_id, event_type, evaluation_id, job_id, attempt_id,
                       worker_id, seq, occurred_at, payload
                  FROM platform_events WHERE attempt_id = ? ORDER BY seq
                """,
                (attempt_id,),
            ).fetchall()
            result = []
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError):
                    payload = {}
                result.append(payload)
            return result

    def pending_outbox(self, limit: int = 100) -> list[dict]:
        """读取尚未投递的 canonical events；读取本身不改变投递状态。"""

        limit = min(max(int(limit), 1), 500)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT event_id, payload FROM outbox_events
                 WHERE delivered_at IS NULL ORDER BY created_at, event_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                result.append(payload)
        return result

    def mark_outbox_delivered(self, event_ids: list[str]) -> int:
        """确认投递后直接删除行:outbox 只存待投递,已投递不堆积(防长周期 DB 膨胀)。"""
        if not event_ids:
            return 0
        marks = ",".join("?" for _ in event_ids)
        with self._transaction() as connection:
            cursor = connection.execute(
                f"DELETE FROM outbox_events"
                f" WHERE event_id IN ({marks})",
                [*event_ids],
            )
        return cursor.rowcount
