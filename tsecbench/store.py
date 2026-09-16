"""SQLite-backed task, challenge, container, and submission state."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Iterator

from .models import ChallengeDefinition, FlagDefinition, TaskDefinition, flags_json


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


class DuplicateSubmission(Exception):
    """A flag index was already recorded for a task challenge."""


class Store:
    """Small transactional repository using one thread-safe SQLite connection."""

    def __init__(self, database_path: str = "./data/tsecbench.sqlite3") -> None:
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
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE challenges
                   SET container_status = ?, container_addr_json = ?, container_id = ?
                 WHERE task_token = ? AND unique_code = ?
                """,
                (
                    status,
                    json.dumps(addresses, separators=(",", ":")),
                    container_id,
                    token,
                    unique_code,
                ),
            )

    def mark_hint_viewed(self, token: str, unique_code: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE challenges SET hint_viewed = 1 WHERE task_token = ? AND unique_code = ?",
                (token, unique_code),
            )

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
