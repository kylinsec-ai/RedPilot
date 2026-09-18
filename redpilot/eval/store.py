"""评估结果落库:eval_* 表,**独立于生产观测库**。

为什么另开一个 SQLite 文件(默认 `./data/eval.sqlite3`)而不是复用 obs.sqlite3:
`redpilot/obs/store.py` 的架构契约是**单写者** —— 唯一写连接 + RLock + BEGIN IMMEDIATE,
写锁的持有时间直接等于 ingest 的等待时间,而 ingest 被拖住会经由控制面 outbox 重投
把压力放大回观测面(见该模块 `_read` 的注释)。评估面是**读侧消费者**:observer 每
演进一次判据就要重评历史 run,写入节奏由"重评"决定,与求解过程无关。把 eval_* 表
塞进同一个文件,重评就是去抢那把写锁;库文件层面的隔离是结构性保证,比"调用方记得
轻点写"可靠。

**为什么自带 pragma 而不复用 `redpilot.obs.db.connect`**:那条路径 import
`redpilot.obs.schema`,而 schema 是 pydantic + fastapi 的摄取模型 —— 评估面要能在零
fastapi 的 worker 基线里跑(见 pyproject.toml 的基线依赖说明)。取值
与 obs 侧逐字相同(单写者纪律也照搬),但来源独立,不把 web 依赖拖进来。

判据本身不在这里:本模块只做"存与读",判据在 `graders/`,由 observer 注入。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

# 默认库路径与 obs 侧同形(./data/<面>.sqlite3),但**不同文件** —— 见模块 docstring。
DEFAULT_DB_PATH = "./data/eval.sqlite3"

# 写死的取值集合:overall 的词表由 `graders.GradeReport` 冻结(pass/fail/incomplete),
# 在 schema 上加 CHECK 让写错的值**响亮失败**,而不是落库后让报告静默少算一类。
_OVERALLS = ("pass", "fail", "incomplete")

# 判据明细的列名——读方法给报告用。
_GRADE_FIELDS = "run_id, task_id, overall, graded_at, checks"

# 迁移列表:与 obs/db.MIGRATIONS 同一机制(PRAGMA user_version 推进,每条单事务,
# 幂等)。评估面的表少,先只留一段;将来加列/加表只向列表追加。
_MIGRATION_1 = [
    # 一次评分一行。UNIQUE(run_id, task_id) 是幂等地基:**重评即覆盖,不是追加** ——
    # observer 会随判据演进而重评历史 run,重复评分是常态而非异常,幂等必须落在
    # schema 上,不能指望调用方记得先查后写(两个写者之间还有 TOCTOU 空窗)。
    """
    CREATE TABLE IF NOT EXISTS eval_grades (
      run_id    TEXT NOT NULL,
      task_id   TEXT NOT NULL,
      overall   TEXT NOT NULL CHECK (overall IN (%(overalls)s)),
      graded_at REAL NOT NULL,                   -- epoch 秒(与全库时间戳约定一致)
      checks    TEXT NOT NULL DEFAULT '[]',       -- 判据明细 JSON(归档/人读;查询走 eval_checks)
      UNIQUE (run_id, task_id)
    );
    """ % {"overalls": ", ".join(repr(v) for v in _OVERALLS)},
    # 报告按题目聚合(grades(task_id=…)),取最近重评的那批。
    "CREATE INDEX IF NOT EXISTS idx_eval_grades_task"
    " ON eval_grades(task_id, graded_at DESC);",
    # 判据明细:一行一条。明细单列成表而不是只塞 JSON 列,是因为报告要**按判据
    # 横向聚合**(哪些判据最常失败),JSON 列得全表扫再在 Python 里解。
    """
    CREATE TABLE IF NOT EXISTS eval_checks (
      run_id   TEXT NOT NULL,
      task_id  TEXT NOT NULL,
      ordinal  INTEGER NOT NULL DEFAULT 0,       -- 判据声明顺序:报告里判据的次序是给人读的
      check_id TEXT NOT NULL,
      status   TEXT NOT NULL,                    -- 词表由 graders 定义,此处不设 CHECK
      detail   TEXT NOT NULL DEFAULT '',
      UNIQUE (run_id, task_id, check_id)
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_eval_checks_task ON eval_checks(task_id, check_id);",
]

MIGRATIONS: list[list[str]] = [_MIGRATION_1]


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """按名取字段,对象与映射都认(判据报告是 dataclass,库行是 dict)。

    判据报告的形状由 `graders/` 定义,本模块不复制一份 —— 但字段名要留一点余量
    (见 `_check_triple`),否则两边各改一次名就会**静默丢明细**。
    """
    for name in names:
        val = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if val is not None:
            return val
    return default


def _check_triple(check: Any) -> tuple[str, str, str]:
    """判据对象 -> (check_id, status, detail)。

    缺 id 响亮失败:空串会与 UNIQUE(run_id, task_id, check_id) 撞成"若干条明细
    压成一条",报告里看不出少了东西 —— 静默压扁比抛错难查得多。
    """
    cid = str(_field(check, "id", "check_id", "name", default="") or "")
    if not cid:
        raise ValueError(f"check without id: {check!r}")
    status = _field(check, "status")
    if status is None:
        # 布尔形态(ok/pass)折算成词表:两种写法都收,但落库只有一种形状。
        ok = _field(check, "ok", "pass")
        status = "pass" if ok else ("fail" if ok is not None else "unknown")
    detail = str(_field(check, "detail", "notes", "message", default="") or "")
    return cid, str(status), detail


def _decode_checks(raw: str | None) -> list[dict[str, str]]:
    """eval_grades.checks JSON 列 -> list(坏值 = [])。"""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _connect(db_path: str) -> sqlite3.Connection:
    """连接 + pragma(取值与 `redpilot.obs.db.connect` 逐字相同,来源独立)。"""
    conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if db_path != ":memory:":          # 内存库不支持 WAL(同 obs 侧)
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """推进 schema 到最新版本(逐条单事务,幂等;已最新则无操作)。"""
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


class EvalStore:
    """评估结果库。写 = 单连接串行化;读 = 独立只读连接(WAL 的 1 写 + N 读)。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        path = str(db_path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = _connect(path)
        _migrate(self._conn)
        self._lock = threading.RLock()
        # 内存库的坑与 obs 侧同源:每条连接是**各自独立的私有库**,此时读必须共用
        # 写连接,否则读端看到一张空表(且不报错)。
        self._shared = path == ":memory:" or path.startswith("file::memory:")
        # 只读连接:结构性只读(query_only=ON),不靠调用方自觉。与 obs/store 的差别
        # 只在粒度 —— 那边是每线程一条(要扛 SPA 并发读),评估面的读端只有 observer
        # 一个调用方,故收敛成一条惰性连接:同样的保证,更少的活动件。
        self._reader: sqlite3.Connection | None = None
        self._reader_lock = threading.Lock()

    # ── 基础设施 ──

    def close(self) -> None:
        with self._reader_lock:
            reader, self._reader = self._reader, None
        if reader is not None:
            try:
                reader.close()
            except sqlite3.Error:
                pass
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "EvalStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read_conn(self) -> sqlite3.Connection:
        with self._reader_lock:
            if self._reader is None:
                conn = _connect(self._path)
                conn.execute("PRAGMA query_only = ON")
                self._reader = conn
            return self._reader

    @contextmanager
    def _read(self):
        """读路径:独立只读连接,不与写事务争锁(内存库例外,见 __init__)。"""
        if self._shared:
            with self._lock:
                yield self._conn
        else:
            yield self._read_conn()

    @contextmanager
    def _tx(self):
        """写事务 —— 与 `obs/store._tx` 同一纪律,理由同:

        COMMIT 必须留在 try 内,否则 COMMIT 自身失败(磁盘满/锁冲突)时不会 ROLLBACK,
        事务悬挂;isolation_level=None 下 Python 不跟踪事务状态,之后每次
        BEGIN IMMEDIATE 都报 "cannot start a transaction within a transaction",
        所有写入直到重启全部失败。ROLLBACK 前查 in_transaction:某些触发
        (SQLITE_FULL)SQLite 已自动回滚,再 ROLLBACK 会二次抛错、掩盖原始异常。
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

    # ── 写入(全部幂等) ──

    def put_grade(self, report: Any, *, graded_at: float | None = None) -> float:
        """写入一次评分结果,返回落库时间戳。

        report 是 `graders` 的 GradeReport(鸭子类型:run_id/task_id/overall/checks),
        也可直接给同键的 dict —— 本模块不 import graders,免得评估库里多一条
        "判据"依赖(判据缺席要能报成 skipped,不能把库变成判据的搬运工)。

        (run_id, task_id) 已存在则**覆盖**:overall/graded_at/checks 全量换新,判据
        明细先删后插 —— 上一轮评过的判据若这轮不再产生(判据被移除/改了 id),残留行
        会让报告把已经不存在的判据算进去。
        """
        run_id = str(_field(report, "run_id", default="") or "")
        task_id = str(_field(report, "task_id", default="") or "")
        if not run_id or not task_id:
            raise ValueError(
                f"grade needs run_id and task_id: run_id={run_id!r} task_id={task_id!r}")
        overall = str(_field(report, "overall", default="") or "")
        stamp = float(graded_at if graded_at is not None else time.time())
        triples = [_check_triple(c) for c in (_field(report, "checks", default=()) or ())]
        detail = [{"id": cid, "status": status, "detail": text}
                  for cid, status, text in triples]
        with self._tx():
            self._conn.execute(
                "INSERT INTO eval_grades(run_id, task_id, overall, graded_at, checks)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(run_id, task_id) DO UPDATE SET"
                " overall=excluded.overall, graded_at=excluded.graded_at,"
                " checks=excluded.checks",
                (run_id, task_id, overall, stamp,
                 json.dumps(detail, ensure_ascii=False)))
            self._conn.execute(
                "DELETE FROM eval_checks WHERE run_id=? AND task_id=?", (run_id, task_id))
            self._conn.executemany(
                "INSERT INTO eval_checks(run_id, task_id, ordinal, check_id, status, detail)"
                " VALUES(?,?,?,?,?,?)",
                [(run_id, task_id, i, cid, status, text)
                 for i, (cid, status, text) in enumerate(triples)])
        return stamp

    # ── 只读查询 ──

    def grades(self, task_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """评分行,最近评的在前;task_id 为空则全库。checks 列就地解码成明细。

        上限夹在 [1,500](与 `obs/store.list_runs` 同口径):默认 50 是给报告用的
        一批,不是全史倾倒 —— 全史请显式传 limit 并自己分页。
        """
        limit = min(max(int(limit), 1), 500)
        with self._read() as conn:
            if task_id:
                rows = conn.execute(
                    f"SELECT {_GRADE_FIELDS} FROM eval_grades WHERE task_id=?"
                    " ORDER BY graded_at DESC, rowid DESC LIMIT ?",
                    (str(task_id), limit)).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_GRADE_FIELDS} FROM eval_grades"
                    " ORDER BY graded_at DESC, rowid DESC LIMIT ?",
                    (limit,)).fetchall()
        out = [dict(r) for r in rows]
        for row in out:
            row["checks"] = _decode_checks(row.get("checks"))
        return out

    def checks(self, run_id: str, task_id: str) -> list[dict[str, str]]:
        """一条 run 的判据明细,按判据声明顺序。"""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT check_id, status, detail FROM eval_checks"
                " WHERE run_id=? AND task_id=? ORDER BY ordinal",
                (str(run_id), str(task_id))).fetchall()
        return [dict(r) for r in rows]

    def graded_run_ids(self, task_id: str) -> list[str]:
        """该题已评分的 run_id,按**首次评分**先后(rowid;重评不改 rowid)。

        **顺序语义只保证"稳定",不保证"等于执行先后"**:run 的执行时间在观测库
        (`runs.started_at`),本库刻意不跨库 JOIN(见模块 docstring)。pass^k 需要
        按执行先后排序,那个顺序得由调用方(observer)按 Trace 给 —— 本方法回答的
        只是"哪些 run 已评过"这个集合。
        """
        with self._read() as conn:
            rows = conn.execute(
                "SELECT run_id FROM eval_grades WHERE task_id=? ORDER BY rowid",
                (str(task_id),)).fetchall()
        return [str(r["run_id"]) for r in rows]
