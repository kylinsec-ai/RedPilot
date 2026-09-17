"""回放：把一次 run 折叠成判据可吃的轨迹视图。**评估面唯一的数据入口。**

为什么要"唯一入口"：判据分叉是这类系统最典型的静默失败 —— 同一件事被两处
各自实现，一处改了另一处没跟上，不报错、不告警，只是结论开始互相矛盾。本仓
已经吃过一次同名的亏（`ghost_worker/taskprompt.py` 与 `adapter/taskprompt.py`
双份并存，见 `packages/worker/tests/test_taskprompt_single_source.py`）。所以
「run_id → 事件 → 时间线」这条链只允许有一个实现，就是本模块；判据只消费它。

数据来源有三条，互不依赖，都产出同一个 `Trace`：

    1. ObsStore（生产）      run_id → run_events 分页 → fold_rows
    2. 事件行（测试/离线）    rows  → fold_rows
    3. transcript.jsonl     文件 → 逐行 JSON → fold_rows

第 3 条是给没有观测库的场景用的（靶场自测、CI 夹具）。它读的是 pi 原生
transcript，与 relay 摄取进库的是同一批事件，所以三者语义一致。

**只读**：本模块不写入任何东西，也不碰控制面。评估是观测面的下游消费者。
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Iterator, Sequence

from ghost_contracts.digest import fold_rows

# 技能正文文件名。判据靠它识别"这一条工具调用是在读技能"。
SKILL_MD = "SKILL.md"

# 事件分页上限（store.run_events 自身也夹在 [1,1000]）。
_PAGE = 500


def skill_name_from_cmd(cmd: str) -> str | None:
    """从工具参数摘要里抽出被读取的技能名；不是读技能则返回 None。

    为什么要认两种形态：`cmd` 字段是 `digest.oneline()` 的产物 —— bash 工具
    给的是**裸命令**，其余工具（read/write/edit）给的是**截断到 300 字符的
    JSON 参数**（`contracts/text.ARGS_SUMMARY_MAX`）。两条路都要能认出来：
    技能是只读 300 字符的 JSON 里那个 path，还是 `cat /app/skills/x/SKILL.md`。
    """
    if not cmd or SKILL_MD not in cmd:
        return None
    # 取 SKILL.md 前的那一段路径，最后一段就是技能目录名。
    head = cmd[: cmd.index(SKILL_MD)]
    # 先吃掉路径末尾的分隔符：`/app/skills/hack/` 里最后那个 `/` 是路径的一部分，
    # 若拿它当边界，取出来的技能名会是空串（这条踩过 —— 见 test_replay 的回归用例）。
    head = head.rstrip("/\\")
    # 再以分隔符/引号/空白回溯最近的一段（cmd 可能是被截断的 JSON 参数摘要）。
    cut = max(head.rfind(c) for c in ('/', '"', "'", ' ', ',', ':')) if head else -1
    name = (head[cut + 1:] if cut >= 0 else head).strip()
    # 取不到就是取不到：宁可返回 None 让判据报"没读到技能"，也不要返回残片。
    return name or None


class ToolCall:
    """一次工具调用（时间线 `kind=tool` 条目的强类型视图）。"""

    __slots__ = ("seq", "turn", "tool", "cmd", "err", "out", "out_len", "id")

    def __init__(self, entry: dict):
        self.seq: int = int(entry.get("seq") or 0)
        self.turn: int | None = entry.get("turn")
        self.tool: str = str(entry.get("tool") or "")
        self.cmd: str = str(entry.get("cmd") or "")
        self.err: bool = bool(entry.get("err"))
        self.out: str = str(entry.get("out") or "")
        self.out_len: int = int(entry.get("out_len") or 0)
        self.id: str = str(entry.get("id") or "")

    @property
    def skill(self) -> str | None:
        """这条调用是在读哪个技能；不是读技能则为 None。"""
        return skill_name_from_cmd(self.cmd)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ToolCall #{self.seq} {self.tool} {self.cmd[:60]!r}>"


class Trace:
    """一次执行的轨迹视图：run 元信息 + 折叠时间线 + 预抽取的常用切片。

    构造即冻结：所有派生视图在这里算一次，判据只管读。
    """

    def __init__(self, run: dict, entries: Sequence[dict], meta: dict | None = None):
        self.run: dict = dict(run or {})
        self.entries: tuple[dict, ...] = tuple(entries or ())
        self.meta: dict = dict(meta or {})
        self.tool_calls: tuple[ToolCall, ...] = tuple(
            ToolCall(e) for e in self.entries if e.get("kind") == "tool")
        self.turns: tuple[dict, ...] = tuple(
            e for e in self.entries if e.get("kind") == "turn")
        self.sessions: tuple[dict, ...] = tuple(
            e for e in self.entries if e.get("kind") == "session")
        self.errors: tuple[dict, ...] = tuple(
            e for e in self.entries if e.get("kind") == "error")

    # ── run 元信息（缺省一律给"空"而不是 None，判据少写一层防御）──

    @property
    def run_id(self) -> str:
        return str(self.run.get("run_id") or "")

    @property
    def challenge_code(self) -> str:
        return str(self.run.get("challenge_code") or "")

    @property
    def worker_id(self) -> str:
        return str(self.run.get("worker_id") or "")

    @property
    def status(self) -> str:
        return str(self.run.get("status") or "")

    @property
    def duration_s(self) -> float:
        return float(self.run.get("duration_s") or 0.0)

    @property
    def started_at(self) -> float:
        """这次执行的**开始时刻**（epoch 秒）。

        存在的理由只有一个：`pass^k` 数的是"连续 k 次"，所以它要的是**执行顺序**，
        而评估库天然只会给出"评分顺序"（`EvalStore.graded_run_ids` 就是后者）。
        两者在重评历史 run 之后会分叉 —— 那时按评分顺序算出来的 pass^k 是个
        看起来正常的错数。把执行时刻挂在记录上，顺序就不再依赖调用方记不记得。
        """
        return float(self.run.get("started_at") or 0.0)

    @property
    def flags_accepted(self) -> tuple[str, ...]:
        return tuple(str(f) for f in (self.run.get("flags_accepted") or ()))

    # ── 派生量 ──

    @property
    def commands(self) -> tuple[str, ...]:
        """所有工具调用的 `cmd`（含非 bash 工具的 JSON 参数摘要）。"""
        return tuple(t.cmd for t in self.tool_calls)

    @property
    def bash_commands(self) -> tuple[str, ...]:
        """只取 bash 工具的裸命令 —— 命令类判据应当只看这一批。

        非 bash 工具的 `cmd` 是 JSON 参数（read/write/edit），拿它当命令文本
        判"有没有联网下载"会误伤：一个把 URL 写进文件内容的 write 调用不是下载。
        """
        return tuple(t.cmd for t in self.tool_calls if t.tool == "bash")

    @property
    def skill_reads(self) -> tuple[str, ...]:
        """按发生顺序列出被读过的技能名（可重复 —— 重复本身就是信号）。"""
        return tuple(s for s in (t.skill for t in self.tool_calls) if s)

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(t.tool for t in self.tool_calls)

    @property
    def total_tokens(self) -> int:
        """折叠时间线里各 turn 的 totalTokens 之和；缺 usage 的 turn 不计。

        这是**下界**：pi 未上报 usage 的 turn 不贡献，所以拿它做预算判据时
        阈值要留余量（见 graders.deterministic 的 BUDGET 检查）。
        """
        return sum(int(t.get("tokens") or 0) for t in self.turns)

    @property
    def tool_errors(self) -> int:
        return sum(1 for t in self.tool_calls if t.err)

    @property
    def repeated_commands(self) -> dict[str, int]:
        """出现过不止一次的 bash 命令 → 次数（空转/原地打转的确定性信号）。

        归一：折叠空白再比。命令里的凭据由 `digest.oneline` 上游的
        `summarize_args` 脱敏与截断，所以这里的键可能被截断 —— 判据只数
        次数，不复现内容。
        """
        counts: dict[str, int] = {}
        for cmd in self.bash_commands:
            key = " ".join(cmd.split())
            if key:
                counts[key] = counts.get(key, 0) + 1
        return {k: v for k, v in counts.items() if v > 1}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"<Trace {self.run_id[:8]} {self.challenge_code} "
                f"{len(self.tool_calls)} tools {len(self.sessions)} sessions>")


# ── 三条数据来源 ──────────────────────────────────────────────


def trace_from_rows(run: dict, rows: Iterable[dict], *,
                    live: bool = False) -> Trace:
    """从事件行折叠出 Trace。rows 每项须含 `payload`（原文 JSON 字符串）。

    这是 `store.run_events()["events"]` 的形状，也是 DB `events` 表的形状。
    """
    folded = fold_rows(list(rows), live=live)
    return Trace(run, folded["entries"], folded["meta"])


def trace_from_transcript(path: str, run: dict | None = None, *,
                          live: bool = False) -> Trace:
    """从 pi 原生 transcript.jsonl 折叠出 Trace（无观测库时用）。

    坏行/非 JSON 行**跳过并计数**（`meta.unparsed`），不让一条脏行废掉整场
    回放 —— 与 `fold_rows` 自身的容错口径一致。
    """
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append({"payload": line})
    meta_run = dict(run or {})
    meta_run.setdefault("run_id", os.path.basename(os.path.dirname(path)) or path)
    return trace_from_rows(meta_run, rows, live=live)


class Replayer:
    """从 ObsStore 读 run。**只读** —— 不写库、不碰控制面。"""

    def __init__(self, store):
        self._store = store

    def trace(self, run_id: str) -> Trace:
        """一条 run 的完整轨迹。run 不存在 → KeyError（响亮，不返回空轨迹）。"""
        run = self._store.run_row(run_id)
        if run is None:
            raise KeyError(f"run not found: {run_id}")
        rows: list[dict] = []
        after = 0
        while True:
            page = self._store.run_events(run_id, after=after, limit=_PAGE)
            rows.extend(page["events"])
            if page["end"]:
                break
            after = page["next_seq"]
        return trace_from_rows(run, rows)

    def traces(self, *, status: str | None = None, worker: str | None = None,
               challenge: str | None = None, limit: int = 200) -> list[Trace]:
        """按过滤条件回放多条 run（`list_runs` 的薄包装）。"""
        out: list[Trace] = []
        for row in self._store.list_runs(status=status, worker=worker,
                                         challenge=challenge, limit=limit):
            try:
                out.append(self.trace(str(row["run_id"])))
            except KeyError:      # 列出后被并发删除：跳过而不是炸掉整批
                continue
        return out
