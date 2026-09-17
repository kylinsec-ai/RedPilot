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

from redpilot.obs import db as dbmod
from redpilot.obs.config import DEFAULT_STALE_AFTER
from redpilot.obs.schema import ACTIVE_PHASES, CLOSABLE_STATUSES

# runs 行投影(列表/详情共用;duration 由时间戳导出,不落列避免两处维护)。
#
# event_count 用**相关标量子查询**,不是聚合 LEFT JOIN。曾以为 LEFT JOIN 更省
# ("少 500 次索引扫"),EXPLAIN QUERY PLAN 实测相反:LEFT JOIN 的子查询会在每次调用时
# MATERIALIZE 整张 events(GROUP BY 无法下推),再建 AUTOMATIC COVERING INDEX 回连 ——
# 代价是全库事件量,与 LIMIT 无关,连单条 run 详情(run_row)也一样。改用相关子查询后
# 计划变为 SEARCH events USING COVERING INDEX sqlite_autoindex_events_1 (run_id=?),
# 即每返回行一次索引区间扫,物化消失。
_RUN_FIELDS = (
    "r.run_id, r.worker_id, r.challenge_code, r.model, r.status,"
    " r.evaluation_id, r.job_id, r.attempt_id,"
    " r.started_at, r.ended_at,"
    " CASE WHEN r.ended_at IS NOT NULL THEN MAX(0.0, r.ended_at - r.started_at) END"
    "   AS duration_s,"
    " r.error, r.turns, r.sessions, r.flags_found, r.flags_accepted, r.updated_at,"
    " (SELECT COUNT(*) FROM events e WHERE e.run_id = r.run_id) AS event_count,"
    " r.canonical"
)
_RUN_FROM = "FROM runs r"


def _decode_flags(raw: str | None) -> list[str]:
    """flags_accepted JSON 列 -> list(缺失/坏值 = [],与 challenge_flags 同缺省)。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


class ObsStore:
    def __init__(self, db_path: str | os.PathLike,
                 *, stale_after: float = DEFAULT_STALE_AFTER):
        """stale_after:心跳过期阈值 —— 房管关 stale run 与读端"在线上"判定同源
        (app lifespan 从 Settings.stale_after 注入;测试可显式传)。"""
        dbmod.ensure_parent(db_path)
        self._path = str(db_path)
        self._conn = dbmod.connect(db_path)
        dbmod.migrate(self._conn)
        self._lock = threading.RLock()
        self._stale_after = stale_after
        # 只读连接池(每线程一条):读不再与写抢同一把锁。
        # WAL 天然支持「1 写 + N 读」,故单写者不变量不被破坏 —— 写连接仍唯一。
        # 内存库例外:每个连接是各自独立的私有库,必须共用同一条连接。
        self._shared_conn = str(db_path).startswith("file::memory:") or str(db_path) == ":memory:"
        self._read_local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()

    # ── 基础设施 ──

    def close(self) -> None:
        with self._readers_lock:
            readers, self._read_conns = self._read_conns, []
        for conn in readers:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        with self._lock:
            self._conn.close()

    def health(self) -> bool:
        try:
            with self._read() as conn:
                return conn.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def _read_conn(self) -> sqlite3.Connection:
        """本线程的只读连接(惰性建,进程退出时随 close 统一关闭)。"""
        conn = getattr(self._read_local, "conn", None)
        if conn is None:
            conn = dbmod.connect(self._path)
            # 结构性保证:读连接在 SQLite 层就不可能写(而非靠调用方自觉)。
            conn.execute("PRAGMA query_only = ON")
            self._read_local.conn = conn
            with self._readers_lock:
                self._read_conns.append(conn)
        return conn

    @contextmanager
    def _read(self):
        """读路径:独立只读连接,不与写事务争锁。

        背景:此前读写共用同一把 RLock 与同一条连接,任何慢查询(如无 LIMIT 的
        `/api/timeline` 全史折叠)都会阻塞 ingest —— 而 ingest 被阻塞会让控制面
        outbox 重投堆积,把观测面的读压力放大成控制面的写压力。
        """
        if self._shared_conn:
            with self._lock:  # 内存库单连接:读也必须串行
                yield self._conn
        else:
            yield self._read_conn()

    @contextmanager
    def _tx(self):
        """写事务。COMMIT 必须留在 try 内 —— 否则 COMMIT 自身失败(磁盘满/锁冲突)
        时不会 ROLLBACK,事务悬挂;isolation_level=None 下 Python 不跟踪事务状态,
        之后每次 BEGIN IMMEDIATE 都报 "cannot start a transaction within a
        transaction",所有写入直到重启全部失败。见 test_db 的同名回归。

        ROLLBACK 前查 in_transaction:某些触发(SQLITE_FULL)SQLite 已自动回滚,
        此时再 ROLLBACK 会二次抛错、掩盖原始异常。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    # ── 摄取(全部幂等) ──

    def append_events(self, run_id: str, worker_id: str, challenge_code: str,
                      rows: list[tuple[int, str, str]], model: str = "",
                      started_at: float | None = None,
                      evaluation_id: str | None = None,
                      job_id: str | None = None,
                      attempt_id: str | None = None) -> tuple[int, bool]:
        """run 行不存在则建(running),随后幂等批插事件 —— 单事务。

        rows 为 (seq, type, payload);UNIQUE(run_id, seq) + INSERT OR IGNORE
        保证整批重放新增=0。返回 (实际新增行数, 是否新建 run)。

        attempt 归一:attempt_id 非空时先按 attempt_id 找已有关联行(同一
        attempt 只留一行);命中则复用其 run_id 落事件,不再按传入 run_id
        另起一行 —— canonical started/completed 与 relay telemetry
        (run_id != attempt_id) 因此收敛到同一 run。排序把 canonical 行置顶,
        已分裂的旧库收敛到权威行。复用时仅回填 evaluation/job(权威补全)+
        占位值回填(unknown/空 worker/model → 真实值);绝不用 unknown 覆盖
        真实值、不碰 status(后补 started 不洗终态)。
        """
        started_at = started_at if started_at is not None else time.time()
        with self._tx():
            target = run_id
            if attempt_id:
                existing = self._conn.execute(
                    "SELECT run_id, worker_id, challenge_code, model FROM runs"
                    " WHERE attempt_id=?"
                    " ORDER BY canonical DESC, started_at DESC, rowid DESC LIMIT 1",
                    (attempt_id,)).fetchone()
                if existing is not None:
                    target = existing["run_id"]
                    # 权威补全:只填 evaluation/job(传入非空才写),不动其他列
                    for column, value in (("evaluation_id", evaluation_id),
                                          ("job_id", job_id)):
                        if value is not None:
                            self._conn.execute(
                                f"UPDATE runs SET {column}=? WHERE run_id=?",
                                (value, target))
                    # 占位回填:completed 先建的 unknown/空行被后补 started 纠正;
                    # 反向(unknown 覆盖真实值)禁止。
                    if (existing["challenge_code"] in ("", "unknown")
                            and challenge_code not in ("", "unknown")):
                        self._conn.execute(
                            "UPDATE runs SET challenge_code=? WHERE run_id=?",
                            (challenge_code, target))
                    if not existing["worker_id"] and worker_id:
                        self._conn.execute(
                            "UPDATE runs SET worker_id=? WHERE run_id=?",
                            (worker_id, target))
                    if not existing["model"] and model:
                        self._conn.execute(
                            "UPDATE runs SET model=? WHERE run_id=?", (model, target))
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO runs(run_id, worker_id, challenge_code, model,"
                " evaluation_id, job_id, attempt_id, status, started_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?, 'running',?,?)",
                (target, worker_id, challenge_code, model, evaluation_id, job_id,
                 attempt_id, started_at, started_at))
            created = cur.rowcount > 0
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO events(run_id, seq, type, payload)"
                " VALUES(?,?,?,?)",
                [(target, seq, typ, payload) for seq, typ, payload in rows])
            # 计数必须在锁内读取:锁外读 total_changes 会把并发线程刚提交的
            # 批计入本批(共享同一连接,total_changes 是连接级累计值)
            inserted = self._conn.total_changes - before
        return inserted, created

    def close_run(self, run_id: str, *, status: str, error: str | None = None,
                  turns: int | None = None, sessions: int | None = None,
                  flags_found: int | None = None, flags_accepted: list[str] | None = None,
                  ended_at: float | None = None,
                  evaluation_id: str | None = None,
                  job_id: str | None = None,
                  attempt_id: str | None = None,
                  canonical: bool = False,
                  worker_id: str = "",
                  challenge_code: str = "unknown",
                  model: str = "") -> bool:
        """关闭 run。

        - relay(source=canonical=False): 仅 running/interrupted 可写;
          若已有 canonical 终态,不能覆盖(含 run_id 不同但 attempt_id 相同的
          跨行情形 —— 同一 attempt 任一行 canonical=1 即全局拒写)。
        - canonical(source=canonical=True): 始终可写(覆盖 relay 终态);
          重复投递幂等。run_id 未命中时按 attempt_id 回落关联;两侧均未命中
          时新建占位终态行(乱序 completed 先到不丢失,started 后补复用该行)。
        turns/sessions 缺省按 events 计数回填。
        """
        ended_at = ended_at if ended_at is not None else time.time()
        with self._tx():
            # 先按 run_id 查;未命中且 attempt_id 不一致时按 attempt_id 回落
            # (canonical 置顶:已分裂旧库优先命中权威行,新写收敛)
            row = self._conn.execute(
                "SELECT run_id, status, canonical, attempt_id FROM runs WHERE run_id=?",
                (run_id,)).fetchone()
            if row is None and attempt_id:
                row = self._conn.execute(
                    "SELECT run_id, status, canonical, attempt_id FROM runs"
                    " WHERE attempt_id=?"
                    " ORDER BY canonical DESC, started_at DESC, rowid DESC LIMIT 1",
                    (attempt_id,)).fetchone()
                if row is not None:
                    run_id = row["run_id"]
            if row is None:
                if not canonical:
                    return False
                # 乱序 completed 无行可关:建占位终态行保留权威终态。
                # worker/code 无处可取时用缺省(ingest 透传 worker,code 置 unknown
                # 与 started 缺码回落一致);started_at 取 ended_at 保持 duration>=0。
                self._conn.execute(
                    "INSERT INTO runs(run_id, worker_id, challenge_code, model,"
                    " evaluation_id, job_id, attempt_id, status,"
                    " started_at, ended_at, turns, sessions,"
                    " error, flags_found, flags_accepted, updated_at, canonical)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,1)",
                    (run_id, worker_id, challenge_code, model,
                     evaluation_id, job_id, attempt_id, status,
                     ended_at, ended_at,
                     turns if turns is not None else 0,
                     sessions if sessions is not None else 0,
                     error, flags_found,
                     json.dumps(flags_accepted, ensure_ascii=False)
                     if flags_accepted is not None else None,
                     ended_at))
                return True

            # 权威终态始终允许写入(幂等);以下守卫只约束 relay 侧。
            if not canonical:
                if row["canonical"]:
                    return False  # relay 不能覆盖 canonical 终态
                if row["status"] not in CLOSABLE_STATUSES:
                    return False
                # 跨行守卫:同一 attempt 任一行已 canonical,relay 一律拒写。
                # 覆盖 run_id 与 attempt_id 不一致、relay 未带 attempt_id
                # (取行上 attempt_id)等情形;防分裂库的 relay 行被另行关闭。
                effective_attempt = attempt_id or row["attempt_id"]
                if effective_attempt:
                    guard = self._conn.execute(
                        "SELECT 1 FROM runs WHERE attempt_id=? AND canonical=1 LIMIT 1",
                        (effective_attempt,)).fetchone()
                    if guard is not None:
                        return False

            # turns/sessions 取值优先级:传入 > 既有 > 按事件计数回填。
            # 此前缺中间项(COALESCE(?, 计数)),canonical 关闭传 None 时会用**事件计数
            # 覆盖** relay 报上来的真实轮次 —— canonical 赢下生命周期,却把度量写坏了。
            sets = ["status=?", "ended_at=?",
                    "turns=COALESCE(?, runs.turns, (SELECT COUNT(*) FROM events e"
                    " WHERE e.run_id=runs.run_id AND e.type='tool_execution_start'))",
                    "sessions=COALESCE(?, runs.sessions, (SELECT COUNT(*) FROM events e"
                    " WHERE e.run_id=runs.run_id AND e.type='session'))",
                    "updated_at=?"]
            params: list[Any] = [status, ended_at, turns, sessions, ended_at]
            # error:canonical 的 attempt.completed 载荷**恒含** error 键(core 侧
            # {"status","solved","flags_found","error"}),故 None 确实表示"无错误",
            # 应据此清掉 relay 可能留下的陈旧值;relay 侧则仍是"给了才写",避免
            # 一次未带 error 的关闭擦掉已有信息。
            if error is not None or canonical:
                sets.append("error=?")
                params.append(error)
            if flags_found is not None:
                sets.append("flags_found=?")
                params.append(flags_found)
            if flags_accepted is not None:
                sets.append("flags_accepted=?")
                params.append(json.dumps(flags_accepted, ensure_ascii=False))
            for column, value in (("evaluation_id", evaluation_id),
                                  ("job_id", job_id), ("attempt_id", attempt_id)):
                if value is not None:
                    sets.append(f"{column}=?")
                    params.append(value)
            if canonical:
                sets.append("canonical=1")
            params.append(run_id)
            cur = self._conn.execute(
                f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", params)
        return cur.rowcount > 0

    def close_runs_for_switch(self, worker_id: str, keep_code: str) -> list[str]:
        """崩溃守卫:worker 从某 code 切到新 code(未关旧 run)——旧 running run 关为 interrupted。"""
        return self._interrupt_runs(
            "WHERE r.worker_id=? AND r.status='running' AND r.challenge_code<>?",
            (worker_id, keep_code), error=f"switched away from {keep_code}")

    def close_running_for_worker(self, worker_id: str, error: str) -> list[str]:
        """空闲守卫:worker 重新上线(phase=idle)却残留 running run → 视为重启残留,关闭。"""
        return self._interrupt_runs(
            "WHERE r.worker_id=? AND r.status='running'", (worker_id,), error=error)

    def close_stale_runs(self, now: float | None = None,
                         stale_after: float | None = None) -> list[str]:
        """housekeeper:心跳过期(无 live 行或 updated_at 超 stale_after)的 running run 关为 interrupted。
        stale_after 缺省取构造注入的阈值(与读端新鲜窗口同源)。"""
        if stale_after is None:
            stale_after = self._stale_after
        now = now if now is not None else time.time()
        return self._interrupt_runs(
            "WHERE r.status='running' AND (l.updated_at IS NULL OR ? - l.updated_at > ?)",
            (now, stale_after), error="no heartbeat")

    def _interrupt_runs(self, where: str, params: tuple, *, error: str) -> list[str]:
        """关 running run 为 interrupted。live_state 恒 LEFT JOIN(live_state.worker_id 为
        PK,1:1 无损)——无 live 行的 worker 由 l.updated_at IS NULL 表达。"""
        now = time.time()
        with self._tx():
            rows = self._conn.execute(
                "SELECT r.run_id FROM runs r"
                " LEFT JOIN live_state l ON l.worker_id = r.worker_id " + where,
                params).fetchall()
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
        with self._tx():
            self._conn.execute(
                "INSERT INTO roster_snapshot(worker_id, payload, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(worker_id) DO UPDATE SET payload=excluded.payload,"
                " updated_at=excluded.updated_at",
                (worker_id, json.dumps(snap, ensure_ascii=False), time.time()))

    # ── 只读查询 ──

    def live_rows(self) -> list[dict[str, Any]]:
        """全部 worker 的 live 行(worker_id, updated_at, 已解析 snap)。"""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT worker_id, updated_at, snapshot FROM live_state").fetchall()
        return [{"worker_id": r["worker_id"], "updated_at": r["updated_at"],
                 "snapshot": json.loads(r["snapshot"])} for r in rows]

    def live_latest(self, fresh_after: float | None = None) -> dict[str, Any] | None:
        """最新"在线上"worker 的快照(按 updated_at;无线上行则 None)。

        带新鲜窗口:死 worker 的残留 busy 快照不再被无限期地当作当前状态
        (e2e 观测:worker 退出 23h 后 phase='closing' 行仍在被 /api/status 返回)。
        fresh_after 缺省取构造注入阈值(与房管关 stale run 同源)。
        """
        if fresh_after is None:
            fresh_after = self._stale_after
        now = time.time()
        rows = [r for r in self.live_rows() if now - r["updated_at"] <= fresh_after]
        if not rows:
            return None
        return max(rows, key=lambda r: r["updated_at"])["snapshot"]

    def active_live_codes(self) -> set[str]:
        """活跃求解的 code 集合(用于 timeline meta.live 判定,复刻旧语义)。

        同样只算新鲜窗口内有心跳的 worker:否则死 worker 的残留
        'closing'/'solving' 快照把已结束的 code 永远标成"实时求解中"。
        窗口与 live_latest 同源(self._stale_after)。
        """
        now = time.time()
        return {r["snapshot"].get("challenge_code")
                for r in self.live_rows()
                if now - r["updated_at"] <= self._stale_after
                and r["snapshot"].get("phase") in ACTIVE_PHASES
                and r["snapshot"].get("challenge_code")}

    def sweep_live(self, now: float | None = None,
                   older_than: float | None = None) -> int:
        """房管 GC:删除超 older_than 秒无更新的 live_state 行(已死 worker 残留)。

        安全前提:崩溃残留的 running run 已由 close_stale_runs(150s)先行打断,
        删行只影响"读端无此 worker"的观感,不再承担守卫职责。
        older_than 缺省 = stale 窗口 x4(房管节拍下死 worker 至少要留 4 个窗口)。
        """
        if older_than is None:
            older_than = self._stale_after * 4
        now = now if now is not None else time.time()
        with self._tx():
            cur = self._conn.execute(
                "DELETE FROM live_state WHERE updated_at < ?", (now - older_than,))
        return cur.rowcount

    def roster_rows(self) -> list[dict[str, Any]]:
        """每 worker 一行:payload 快照全键 + worker_id/updated_at(快照 5 键由写入方保证)。
        身份列恒取自 DB 行:payload 若含同名键(脏数据/伪造)一律剥掉,不得覆盖行身份。"""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT worker_id, payload, updated_at FROM roster_snapshot"
                " ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            try:
                snap = json.loads(r["payload"])
            except Exception:
                snap = {}
            if not isinstance(snap, dict):
                snap = {}
            snap.pop("worker_id", None)
            snap.pop("updated_at", None)
            out.append({"worker_id": r["worker_id"], "updated_at": r["updated_at"], **snap})
        return out

    def roster_merged(self) -> dict[str, Any]:
        """多 worker roster 读合并;单 worker 时与原快照逐字节同构。

        平台段取 stale=0 且无 platform_error 中 fetched_at 最大者;无则最新行。
        challenges 合并:优先非 local_only(真平台行)于 local_only,同级取 fetched_at 最新者。
        """
        rows = self.roster_rows()
        if not rows:
            return {"fetched_at": 0.0, "stale": True, "platform_error": "",
                    "platform_disabled": True, "challenges": {}}
        good = [r for r in rows if not r.get("stale") and not r.get("platform_error")]
        seg = max(good, key=lambda r: r.get("fetched_at") or 0.0) if good else rows[0]
        merged: dict[str, dict[str, Any]] = {}
        for r in sorted(rows, key=lambda x: x.get("fetched_at") or 0.0, reverse=True):
            for code, row in (r.get("challenges") or {}).items():
                prev = merged.get(code)
                if prev is None:
                    merged[code] = row
                elif prev.get("local_only") and not row.get("local_only"):
                    merged[code] = row
        return {"fetched_at": seg.get("fetched_at"), "stale": bool(seg.get("stale")),
                "platform_error": seg.get("platform_error") or "",
                "platform_disabled": bool(seg.get("platform_disabled")),
                "challenges": merged}

    def events_for_code(self, code: str) -> list[dict[str, Any]]:
        """某 code 的全部事件,按 (run 起始时间, run rowid, run 内 seq) 全局有序(供 fold/尾部)。"""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT e.seq, e.type, e.payload, e.run_id FROM events e"
                " JOIN runs r ON r.run_id = e.run_id"
                " WHERE r.challenge_code=? ORDER BY r.started_at, r.rowid, e.seq",
                (code,)).fetchall()
        return [dict(r) for r in rows]

    def events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT seq, type, payload FROM events WHERE run_id=? ORDER BY seq",
                (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def transcript_tail(self, code: str, tail: int) -> list[str]:
        """该 code 最近 tail 条 payload(跨 run,时间倒序取尾再正序返回)。"""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT e.payload FROM events e"
                " JOIN runs r ON r.run_id = e.run_id"
                " WHERE r.challenge_code=?"
                " ORDER BY e.id DESC LIMIT ?", (code, tail)).fetchall()
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
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT {_RUN_FIELDS} {_RUN_FROM}{clause}"
                " ORDER BY r.started_at DESC, r.rowid DESC LIMIT ?",
                params).fetchall()
        out = [dict(r) for r in rows]
        for row in out:
            row["flags_accepted"] = _decode_flags(row["flags_accepted"])
            row["canonical"] = bool(row["canonical"])
        return out

    def attach_accepted_flags(self, run_id: str, flags: list[str]) -> bool:
        """只写 runs.flags_accepted 一列 —— **无状态语义**,故可用于 canonical 行。

        与 close_run 的区别:close_run 承载生命周期,受 canonical 守卫约束(relay 不得
        覆盖权威终态);本方法补的是加性观测数据(实测已获得的 flag 明文),不改变
        status/canonical,因此对权威行也允许写入。

        行定位:先按 run_id,再按 attempt_id 回落(与 close_run 同序,canonical 置顶),
        以适配 relay 侧 run_id != attempt_id 的历史数据。无行则返回 False 不建行 ——
        建 running 占位反而可能留下永不关闭的幽灵行。

        幂等:同值重写无副作用;空列表视为"无数据"不覆盖已有值。
        """
        if not flags:
            return False
        with self._tx():
            row = self._conn.execute(
                "SELECT run_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT run_id FROM runs WHERE attempt_id=?"
                    " ORDER BY canonical DESC, started_at DESC, rowid DESC LIMIT 1",
                    (run_id,)).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE runs SET flags_accepted=?, updated_at=? WHERE run_id=?",
                (json.dumps(flags, ensure_ascii=False), time.time(), row["run_id"]))
            return True

    def prune_events(self, *, older_than_days: float, batch: int = 2000) -> int:
        """删除**已结束** run 的原文事件行,返回删除行数;runs 行本身永久保留。

        events 无上界增长是唯一的磁盘增长路径(housekeeper 此前只 GC live_state)。
        注意 events 同时是 transcript 证据链,故:
          - 只动 status<>'running' 的 run(在飞的求解不受影响);
          - 只删原文行,runs 行(含 status/canonical/flags/耗时)留着,审计链不断;
          - 默认阈值取得保守,0 = 关闭(见 Settings.events_retention_days)。

        分批执行:单条大 DELETE 会长时间持写锁阻塞 ingest(单写者)。
        终止性:每批选"尚存事件行的 run"并删光其事件,故不会重复选中同一批。
        """
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400.0
        removed = 0
        while True:
            with self._tx():
                rows = self._conn.execute(
                    "SELECT r.run_id FROM runs r"
                    " WHERE r.status <> 'running'"
                    "   AND COALESCE(r.ended_at, r.updated_at) < ?"
                    "   AND EXISTS(SELECT 1 FROM events e WHERE e.run_id = r.run_id)"
                    " LIMIT ?",
                    (cutoff, batch)).fetchall()
                if not rows:
                    return removed
                ids = [r["run_id"] for r in rows]
                marks = ",".join("?" * len(ids))
                cur = self._conn.execute(
                    f"DELETE FROM events WHERE run_id IN ({marks})", ids)
                removed += cur.rowcount
            if len(ids) < batch:
                return removed

    def challenge_flags(self, code: str) -> list[str]:
        """该 code 最近一次携带 flags_accepted 的 run 的 flag 明文(=FLAG 文件同信任域)。"""
        with self._read() as conn:
            row = conn.execute(
                "SELECT flags_accepted FROM runs WHERE challenge_code=? AND flags_accepted IS NOT NULL"
                " ORDER BY COALESCE(ended_at, updated_at) DESC, rowid DESC LIMIT 1",
                (code,)).fetchone()
        return _decode_flags(row["flags_accepted"]) if row else []

    def run_exists(self, run_id: str) -> bool:
        """存在性探测(详情端点 404 守卫用,免拉整行投影)。"""
        with self._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row is not None

    def run_row(self, run_id: str) -> dict[str, Any] | None:
        with self._read() as conn:
            row = conn.execute(
                f"SELECT {_RUN_FIELDS} {_RUN_FROM} WHERE r.run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["flags_accepted"] = _decode_flags(out["flags_accepted"])
        out["canonical"] = bool(out["canonical"])
        return out

    def run_events(self, run_id: str, after: int = 0, limit: int = 500) -> dict[str, Any]:
        """增量事件页:{events, next_seq, end}。next_seq = 本页末条 seq+1(续拉语义,与 seq 空洞无关)。"""
        limit = min(max(limit, 1), 1000)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT seq, type, payload FROM events WHERE run_id=? AND seq>=?"
                " ORDER BY seq LIMIT ?", (run_id, after, limit + 1)).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = [{"seq": r["seq"], "type": r["type"], "payload": r["payload"]} for r in rows]
        next_seq = (events[-1]["seq"] + 1) if events else after
        return {"events": events, "next_seq": next_seq, "end": not has_more}
