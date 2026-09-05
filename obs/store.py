"""platform/store.py — 全部 SQL:摄取幂等写入 + 只读查询。

单写者纪律:worker 经 HTTP 进入本进程,进程内唯一连接 + threading.RLock 串行化;
多语句写走 BEGIN IMMEDIATE 事务(_tx)。行字段与 frontend types.ts 逐字对齐,
改键名必须同步。读方法也持锁(与写共享同一连接)。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

from . import db as dbmod

# live 快照 phase 集合(用于崩溃守卫与 active-live 判定;与 worker LiveState 一致)
ACTIVE_PHASES = ("starting", "solving", "submitting", "closing")
# 可被 close 写入的 run 状态(终态幂等)
_CLOSABLE = ("running", "interrupted")


class ObsStore:
    def __init__(self, db_path: str | os.PathLike):
        dbmod.ensure_parent(db_path)
        self._path = str(db_path)
        self._conn = dbmod.connect(db_path)
        dbmod.migrate(self._conn)
        self._lock = threading.RLock()

    # ── 基础设施 ──

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def health(self) -> bool:
        with self._lock:
            try:
                return self._conn.execute("SELECT 1").fetchone()[0] == 1
            except sqlite3.Error:
                return False

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _inserted(self, before: int) -> int:
        return self._conn.total_changes - before

    # ── 摄取(全部幂等) ──

    def ensure_run(self, run_id: str, worker_id: str, challenge_code: str,
                   model: str = "", started_at: float | None = None) -> bool:
        """run 行不存在则建(running)。返回是否新建。"""
        started_at = started_at if started_at is not None else time.time()
        with self._tx():
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO runs(run_id, worker_id, challenge_code, model,"
                " status, started_at, updated_at) VALUES(?,?,?,?, 'running',?,?)",
                (run_id, worker_id, challenge_code, model, started_at, started_at))
        return cur.rowcount > 0

    def insert_events(self, run_id: str, worker_id: str, challenge_code: str,
                      rows: list[tuple[int, str, float | None, str]]) -> int:
        """幂等写入:UNIQUE(run_id, seq) + INSERT OR IGNORE。返回实际新增行数(整批重放=0)。"""
        with self._tx():
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO events(run_id, seq, worker_id, challenge_code,"
                " type, ts, payload) VALUES(?,?,?,?,?,?,?)",
                [(run_id, seq, worker_id, challenge_code, typ, ts, payload)
                 for seq, typ, ts, payload in rows])
        return self._inserted(before)

    def close_run(self, run_id: str, *, status: str, error: str | None = None,
                  turns: int | None = None, sessions: int | None = None,
                  flags_found: int | None = None, flags_accepted: list[str] | None = None,
                  ended_at: float | None = None) -> bool:
        """关闭 run;仅 running/interrupted 可写,终态幂等忽略。turns/sessions 缺省按 events 计数回填。"""
        ended_at = ended_at if ended_at is not None else time.time()
        with self._tx():
            row = self._conn.execute(
                "SELECT status, started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in _CLOSABLE:
                return False
            sets = ["status=?", "ended_at=?", "duration_s=?",
                    "turns=COALESCE(?, (SELECT COUNT(*) FROM events e"
                    " WHERE e.run_id=runs.run_id AND e.type='tool_execution_start'))",
                    "sessions=COALESCE(?, (SELECT COUNT(*) FROM events e"
                    " WHERE e.run_id=runs.run_id AND e.type='session'))",
                    "updated_at=?"]
            params: list[Any] = [status, ended_at,
                                 max(0.0, ended_at - row["started_at"]),
                                 turns, sessions, ended_at]
            if error is not None:
                sets.append("error=?")
                params.append(error)
            if flags_found is not None:
                sets.append("flags_found=?")
                params.append(flags_found)
            if flags_accepted is not None:
                sets.append("flags_accepted=?")
                params.append(json.dumps(flags_accepted, ensure_ascii=False))
            params.append(run_id)
            cur = self._conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", params)
        return cur.rowcount > 0

    def close_runs_for_switch(self, worker_id: str, keep_code: str) -> list[str]:
        """崩溃守卫:worker 从某 code 切到新 code(未关旧 run)——旧 running run 关为 interrupted。"""
        return self._interrupt_runs(
            "WHERE worker_id=? AND status='running' AND challenge_code<>?",
            (worker_id, keep_code), error=f"switched away from {keep_code}")

    def close_running_for_worker(self, worker_id: str, error: str) -> list[str]:
        """空闲守卫:worker 重新上线(phase=idle)却残留 running run → 视为重启残留,关闭。"""
        return self._interrupt_runs(
            "WHERE worker_id=? AND status='running'", (worker_id,), error=error)

    def close_stale_runs(self, now: float | None = None, stale_after: float = 150.0) -> list[str]:
        """housekeeper:心跳过期(无 live 行或 updated_at 超 stale_after)的 running run 关为 interrupted。"""
        now = now if now is not None else time.time()
        return self._interrupt_runs(
            "WHERE r.status='running' AND (l.updated_at IS NULL OR ? - l.updated_at > ?)",
            (now, stale_after), error="no heartbeat", alias=True)

    def _interrupt_runs(self, where: str, params: tuple, *, error: str,
                        alias: bool = False) -> list[str]:
        now = time.time()
        table = "runs r LEFT JOIN live_state l ON l.worker_id=r.worker_id" if alias else "runs"
        with self._tx():
            rows = self._conn.execute(
                f"SELECT run_id FROM {table} {where}", params).fetchall()
            if not rows:
                return []
            ids = [r["run_id"] for r in rows]
            marks = ",".join("?" * len(ids))
            self._conn.execute(
                f"UPDATE runs SET status='interrupted', ended_at=?, updated_at=?, error=?"
                f" WHERE run_id IN ({marks})",
                [now, now, error, *ids])
        return ids

    def put_live(self, worker_id: str, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """upsert live_state;返回该 worker 上一份快照(无则 None)供崩溃守卫比较。"""
        now = time.time()
        with self._tx():
            row = self._conn.execute(
                "SELECT snapshot FROM live_state WHERE worker_id=?", (worker_id,)).fetchone()
            prev = json.loads(row["snapshot"]) if row else None
            self._conn.execute(
                "INSERT INTO live_state(worker_id, updated_at, snapshot) VALUES(?,?,?)"
                " ON CONFLICT(worker_id) DO UPDATE SET updated_at=excluded.updated_at,"
                " snapshot=excluded.snapshot",
                (worker_id, now, json.dumps(snapshot, ensure_ascii=False)))
        return prev

    def ping(self, worker_id: str) -> bool:
        """30s 心跳:仅刷新 updated_at;无行则忽略(首份 live 由 live POST 建)。"""
        with self._tx():
            cur = self._conn.execute(
                "UPDATE live_state SET updated_at=? WHERE worker_id=?", (time.time(), worker_id))
        return cur.rowcount > 0

    def put_roster(self, worker_id: str, snap: dict[str, Any]) -> None:
        """覆盖该 worker 的 roster 轮询快照(60s 节奏);payload 存完整 5 键对象。"""
        now = time.time()
        with self._tx():
            self._conn.execute(
                "INSERT INTO roster_snapshot(worker_id, fetched_at, stale, platform_error,"
                " platform_disabled, payload, updated_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(worker_id) DO UPDATE SET fetched_at=excluded.fetched_at,"
                " stale=excluded.stale, platform_error=excluded.platform_error,"
                " platform_disabled=excluded.platform_disabled, payload=excluded.payload,"
                " updated_at=excluded.updated_at",
                (worker_id,
                 snap.get("fetched_at"),
                 1 if snap.get("stale") else 0,
                 snap.get("platform_error") or None,
                 1 if snap.get("platform_disabled") else 0,
                 json.dumps(snap, ensure_ascii=False), now))

    # ── 只读查询 ──

    def live_rows(self) -> list[dict[str, Any]]:
        """全部 worker 的 live 行(worker_id, updated_at, 已解析 snap)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT worker_id, updated_at, snapshot FROM live_state").fetchall()
        return [{"worker_id": r["worker_id"], "updated_at": r["updated_at"],
                 "snapshot": json.loads(r["snapshot"])} for r in rows]

    def live_latest(self) -> dict[str, Any] | None:
        """最新活 worker 的快照(按 updated_at);无则 None。"""
        rows = self.live_rows()
        if not rows:
            return None
        return max(rows, key=lambda r: r["updated_at"])["snapshot"]

    def active_live_codes(self) -> set[str]:
        """正在活跃求解的 code 集合(用于 timeline meta.live 判定,复刻旧语义)。"""
        return {r["snapshot"].get("challenge_code")
                for r in self.live_rows()
                if r["snapshot"].get("phase") in ACTIVE_PHASES
                and r["snapshot"].get("challenge_code")}

    def roster_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT worker_id, fetched_at, stale, platform_error, platform_disabled,"
                " payload, updated_at FROM roster_snapshot ORDER BY updated_at DESC").fetchall()
        return [{"worker_id": r["worker_id"], "fetched_at": r["fetched_at"],
                 "stale": bool(r["stale"]), "platform_error": r["platform_error"],
                 "platform_disabled": bool(r["platform_disabled"]),
                 "challenges": (json.loads(r["payload"]).get("challenges") or {})
                 if r["payload"] else {},
                 "updated_at": r["updated_at"]} for r in rows]

    def roster_merged(self) -> dict[str, Any]:
        """多 worker roster 读合并;单 worker 时与原快照逐字节同构。

        平台段取 stale=0 且无 platform_error 中 fetched_at 最大者;无则最新行。
        challenges 合并:优先非 local_only(真平台行)于 local_only,同级取 fetched_at 最新者。
        """
        rows = self.roster_rows()
        if not rows:
            return {"fetched_at": 0.0, "stale": True, "platform_error": "",
                    "platform_disabled": True, "challenges": {}}
        good = [r for r in rows if not r["stale"] and not r["platform_error"]]
        seg = max(good, key=lambda r: r["fetched_at"] or 0.0) if good else rows[0]
        merged: dict[str, dict[str, Any]] = {}
        for r in sorted(rows, key=lambda x: x["fetched_at"] or 0.0, reverse=True):
            for code, row in r["challenges"].items():
                prev = merged.get(code)
                if prev is None:
                    merged[code] = row
                elif prev.get("local_only") and not row.get("local_only"):
                    merged[code] = row
        return {"fetched_at": seg["fetched_at"], "stale": seg["stale"],
                "platform_error": seg["platform_error"] or "",
                "platform_disabled": seg["platform_disabled"],
                "challenges": merged}

    def events_for_code(self, code: str) -> list[dict[str, Any]]:
        """某 code 的全部事件,按 (run 起始时间, run rowid, run 内 seq) 全局有序(供 fold/尾部)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.seq, e.type, e.ts, e.payload, e.run_id FROM events e"
                " JOIN runs r ON r.run_id = e.run_id"
                " WHERE e.challenge_code=? ORDER BY r.started_at, r.rowid, e.seq",
                (code,)).fetchall()
        return [dict(r) for r in rows]

    def events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, type, ts, payload FROM events WHERE run_id=? ORDER BY seq",
                (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def transcript_tail(self, code: str, tail: int) -> list[str]:
        """该 code 最近 tail 条 payload(跨 run,时间倒序取尾再正序返回)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM events WHERE challenge_code=?"
                " ORDER BY id DESC LIMIT ?", (code, tail)).fetchall()
        return [r["payload"] for r in reversed(rows)]

    def list_runs(self, status: str | None = None, worker: str | None = None,
                  challenge: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        where, params = [], []
        if status:
            where.append("r.status=?")
            params.append(status)
        if worker:
            where.append("r.worker_id=?")
            params.append(worker)
        if challenge:
            where.append("r.challenge_code=?")
            params.append(challenge)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(min(max(limit, 1), 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.run_id, r.worker_id, r.challenge_code, r.model, r.status,"
                " r.started_at, r.ended_at, r.duration_s, r.error, r.turns, r.sessions,"
                " r.flags_found, r.flags_accepted, r.updated_at,"
                " (SELECT COUNT(*) FROM events e WHERE e.run_id = r.run_id) AS event_count"
                f" FROM runs r{clause} ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?",
                params).fetchall()
        return [dict(r) for r in rows]

    def challenge_flags(self, code: str) -> list[str]:
        """该 code 最近一次携带 flags_accepted 的 run 的 flag 明文(=FLAG 文件同信任域)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT flags_accepted FROM runs WHERE challenge_code=? AND flags_accepted IS NOT NULL"
                " ORDER BY COALESCE(ended_at, updated_at) DESC, rowid DESC LIMIT 1",
                (code,)).fetchone()
        if row is None:
            return []
        try:
            flags = json.loads(row["flags_accepted"])
            return flags if isinstance(flags, list) else []
        except Exception:
            return []

    def run_row(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT r.run_id, r.worker_id, r.challenge_code, r.model, r.status,"
                " r.started_at, r.ended_at, r.duration_s, r.error, r.turns, r.sessions,"
                " r.flags_found, r.flags_accepted, r.updated_at,"
                " (SELECT COUNT(*) FROM events e WHERE e.run_id = r.run_id) AS event_count"
                " FROM runs r WHERE r.run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def run_events(self, run_id: str, after: int = 0, limit: int = 500) -> dict[str, Any]:
        """增量事件页:{events, next_seq, end}。next_seq = 本页末条 seq+1(续拉语义,与 seq 空洞无关)。"""
        limit = min(max(limit, 1), 1000)
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, type, ts, payload FROM events WHERE run_id=? AND seq>=?"
                " ORDER BY seq LIMIT ?", (run_id, after, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [{"seq": r["seq"], "type": r["type"], "ts": r["ts"],
                   "payload": r["payload"]} for r in rows]
        next_seq = (events[-1]["seq"] + 1) if events else after
        return {"events": events, "next_seq": next_seq, "end": not has_more}
