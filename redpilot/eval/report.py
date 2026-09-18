"""评估报告:pass^k / 技能触发率 / 每任务成本 —— **确定性聚合,零 LLM**。

本模块只做聚合:输入是一批 `RunRecord`(任务卡 + Trace + 评分结果拼成的记录),
输出是方法论要求的那几个数。模型评分(主观维度)不在这一层,它归 `graders/rubric.py`
—— 报告要能在 CI 里对同一批记录重算出**逐位相同**的数,掺进模型调用就没这个性质了。

一条贯穿全模块的取舍:**"没测到"与"测出来是坏"必须长得不一样**。
- 观测不足 k 次的 pass^k 返回 `None`,不返回 `0.0`(0.0 在报告里就是"连续全败")。
- 分母为 0 的比率返回 `None`,同时**每个比率旁边都放它的样本量** —— n=1 的
  比率要一眼看出它薄,而不是被当成基线。
- `summary` 顶层给 `insufficient_data` 标记:空输入的 0 是"没数据",不是"测得 0"。

把这个区分做丢,评估面就从"证据"退化成"噪声",而且是最坏的一类:看起来像结论。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .dataset import CHECK_IDS

# 结局词表 —— 与 graders.GradeReport.overall 同一集合(pass/fail/incomplete)。
PASS = "pass"

# 默认 k:报告 §7.2 的"连续三次"。三次是起步门槛,不是终值。
DEFAULT_K = 3


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """按名取字段,对象(dataclass/Trace)与映射(库行 dict)都认;多给几个名字就是
    别名(取第一个非 None 的)—— 卡/Trace/评分报告来自三个模块,名字不总是在一处定。
    """
    for name in names:
        val = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if val is not None:
            return val
    return default


def _outcome(rec: Any) -> str:
    """记录 -> 结局字符串。RunRecord 与裸字符串("pass"/"fail"/"incomplete")都收:
    前者是正常路径,后者是回放脚本与测试用的最小输入。"""
    if isinstance(rec, str):
        return rec
    return str(_field(rec, "outcome", default="") or "")


@dataclass(frozen=True)
class RunRecord:
    """一次运行的评估记录 = 任务卡(期望)+ Trace(实测)+ 评分结果(结局)。

    为什么要有这个拼装层:`report` 的三个度量各自需要不同来源的字段 —— pass^k 要
    结局、触发率要"期望/禁读/实读的技能名"、成本要 token 与工具调用数。若让每个
    度量各自去 Trace/卡/报告里挖,同一份字段映射会被抄三遍,而这正是本仓记过的
    那类失败(两处各自实现同一件事,一处改了另一处没跟上,不报错、只是结论开始
    互相矛盾)。所以拼装只在这里做一次。
    """

    task_id: str
    run_id: str
    outcome: str                          # pass / fail / incomplete
    expect_skills: tuple[str, ...] = ()   # 期望被路由到的技能(来自任务卡)
    forbid_skills: tuple[str, ...] = ()   # 不该被读到的技能(负样本)
    skills_read: tuple[str, ...] = ()     # Trace.skill_reads(实读,可重复)
    tokens: int = 0                       # Trace.total_tokens(**下界**)
    tool_calls: int = 0
    # 执行开始时刻(epoch 秒;拿不到就是 0)。存在的理由是 pass^k 要**执行顺序**,
    # 而评估库给的是**评分顺序**(重评历史 run 之后两者会分叉)。挂在这里,
    # `summary()` 就能自己排对,不必指望调用方记得。
    started_at: float = 0.0

    @classmethod
    def of(cls, card: Any, trace: Any, grade: Any) -> "RunRecord":
        """卡 + Trace + 评分结果 -> 一条记录。

        task_id 取**任务卡**(数据集是任务 id 的权威);判据报告里的 task_id 只用于
        对账,不拿它当键 —— 否则卡与判据各说一个名字时,报告会静默分成两组。
        """
        return cls(
            task_id=str(_field(card, "id", default="") or _field(grade, "task_id", default="") or ""),
            run_id=str(_field(trace, "run_id", default="") or _field(grade, "run_id", default="") or ""),
            outcome=str(_field(grade, "overall", default="") or ""),
            expect_skills=tuple(str(s) for s in (_field(card, "expect_skills", default=()) or ())),
            forbid_skills=tuple(str(s) for s in (_field(card, "forbid_skills", default=()) or ())),
            skills_read=tuple(str(s) for s in (_field(trace, "skill_reads", default=()) or ())),
            tokens=int(_field(trace, "total_tokens", default=0) or 0),
            tool_calls=len(_field(trace, "tool_calls", default=()) or ()),
            started_at=float(_field(trace, "started_at", default=0.0) or 0.0),
        )


def pass_k(results: Iterable[Any], k: int = DEFAULT_K) -> float | None:
    """pass^k:**连续 k 次全部成功**的次数占比;观测不足 k 次返回 None。

    为什么是 pass^k 不是 pass@k(报告 §7.2):单次成功率 75% 时,"三次里至少成功
    一次"是 1 - 0.25³ ≈ **98.4%**,而"连续三次全部成功"是 0.75³ ≈ **42.2%**。
    同一个系统,同一个成功率,两个指标一个像"接近可用",一个像"根本不能上线"。
    一次成功演示证明不了上线资质,所以报告只认后者。

    口径:长度为 k 的窗口里"全 pass"的窗口占比。窗口重叠会让各窗口不独立(方差
    偏大),但期望仍是 p^k —— 即"单次成功率^k"的无偏估计,与上面那组算术同一个数。

    **不足 k 次返回 None,不返回 0.0**:0.0 在报告里读作"连续 k 次全败",而真相是
    "还没测够"。把未测与失败混在一个数里,就是拿噪声当结论。
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    outcomes = [_outcome(r) for r in results]
    if len(outcomes) < k:
        return None
    windows = len(outcomes) - k + 1
    good = sum(1 for i in range(windows)
               if all(outcomes[i + j] == PASS for j in range(k)))
    return good / windows


def trigger_rate(records: Iterable[Any]) -> dict[str, Any]:
    """技能路由的两侧:该读的读到没有、不该读的读没读(**误触发率**)。

    两类错必须分开数(dataset.py 的负样本专治第二类):
    - **漏触发**:期望的技能没被读到 —— 技能没起作用,代价是白背一份名录。
    - **误触发**:禁读的技能被读到 —— 改工作目录、耗回合、污染后续判断,报告 §5.4
      引 Glean 的生产数据说它比漏触发更贵,且提供 skill 反而会**先降低**正确触发率
      约 20%,只有补上负样本才恢复。

    只统计**声明了期望/禁读的**记录(分母各自独立):一张没写 expect_skills 的卡
    不该被算成"漏触发",它只是没在这个维度上被考核。每个比率旁边都给它的样本量
    与分子 —— n=1 的比率要能一眼看出薄,而不是被当成基线读走。
    """
    recs = list(records)
    expect_n = expect_hit = 0
    forbid_n = forbid_hit = 0
    for rec in recs:
        read = set(str(s) for s in (_field(rec, "skills_read", default=()) or ()))
        expect = set(str(s) for s in (_field(rec, "expect_skills", default=()) or ()))
        forbid = set(str(s) for s in (_field(rec, "forbid_skills", default=()) or ()))
        if expect:
            expect_n += 1
            expect_hit += 1 if read & expect else 0
        if forbid:
            forbid_n += 1
            forbid_hit += 1 if read & forbid else 0
    return {
        "records": len(recs),
        # 漏触发一侧
        "expect_n": expect_n,
        "expect_read": expect_hit,
        "expect_missed": expect_n - expect_hit,
        "expect_rate": (expect_hit / expect_n) if expect_n else None,
        # 误触发一侧
        "forbid_n": forbid_n,
        "forbid_triggered": forbid_hit,
        "forbid_rate": (forbid_hit / forbid_n) if forbid_n else None,
    }


def cost(records: Iterable[Any]) -> dict[str, Any]:
    """每任务的 token 与工具调用量:总量 + 均值(分母是 run 数)。

    **token 数是下界,不是实测上界**:`Trace.total_tokens` 由折叠时间线各 `turn` 的
    `totalTokens` 求和而来,pi 未上报 usage 的 turn 贡献 0(见 replay.py 的同名
    文档)。所以这个数只能回答"至少花了多少" —— 拿它做预算判据要留余量,拿它做
    "等 token 比较"要写明是下界。返回值里的 `tokens_is_lower_bound` 就是这句话的
    机器可读版本,免得数字单独流传出去时把限定语丢了。
    """
    per_task: dict[str, dict[str, Any]] = {}
    for rec in records:
        task = str(_field(rec, "task_id", default="") or "")
        entry = per_task.setdefault(task, {"runs": 0, "tokens": 0, "tool_calls": 0})
        entry["runs"] += 1
        entry["tokens"] += int(_field(rec, "tokens", default=0) or 0)
        entry["tool_calls"] += int(_field(rec, "tool_calls", default=0) or 0)
    for entry in per_task.values():
        # 均值为 None(而不是 0.0)在 runs=0 时才出现,与全模块"未测 ≠ 测得坏"一致。
        entry["mean_tokens"] = (entry["tokens"] / entry["runs"]) if entry["runs"] else None
        entry["mean_tool_calls"] = (entry["tool_calls"] / entry["runs"]) if entry["runs"] else None
    runs = sum(e["runs"] for e in per_task.values())
    tokens = sum(e["tokens"] for e in per_task.values())
    calls = sum(e["tool_calls"] for e in per_task.values())
    return {
        "per_task": per_task,
        "tasks": len(per_task),
        "runs": runs,
        "tokens": tokens,
        "tool_calls": calls,
        "mean_tokens": (tokens / runs) if runs else None,
        "mean_tool_calls": (calls / runs) if runs else None,
        "tokens_is_lower_bound": True,
    }


def summary(records: Iterable[Any], *, k: int = DEFAULT_K) -> dict[str, Any]:
    """把上面三块聚成一份报告。

    `insufficient_data` 是**先看**的那个键:空输入时下面那些 0 是"没有数据",不是
    "测得 0"。渲染方若把这个标记丢掉,报告会把"还没跑过"画成"全军覆没" —— 这就是
    本模块开头那条取舍要防的事。

    `insufficient_pass_k` 列出观测不足 k 次的任务:它们的 pass_k 是 None,不能与
    "连续全败(0.0)"混排在一列里比较。
    """
    recs = list(records)
    by_task: dict[str, list[Any]] = {}
    for rec in recs:
        by_task.setdefault(str(_field(rec, "task_id", default="") or ""), []).append(rec)
    # 每个任务的记录按**执行时刻**重排后再算 pass^k —— pass^k 数的是"连续 k 次",
    # 顺序错了它就是个看起来正常的错数。评估库给的是评分顺序(重评历史 run 之后
    # 与执行顺序分叉),所以顺序不能靠调用方传对。sorted 是稳定的:全为默认 0
    # 的记录(合成轨迹/没有 started_at)保持传入顺序,行为与改动前一致。
    for rs in by_task.values():
        rs.sort(key=lambda r: float(_field(r, "started_at", default=0.0) or 0.0))
    tasks: dict[str, dict[str, Any]] = {}
    thin: list[str] = []
    for task, rs in by_task.items():
        pk = pass_k(rs, k)
        if pk is None:
            thin.append(task)
        outcomes: dict[str, int] = {}
        for rec in rs:
            key = _outcome(rec)
            outcomes[key] = outcomes.get(key, 0) + 1
        tasks[task] = {"runs": len(rs), "outcomes": outcomes, "pass_k": pk}
    return {
        "insufficient_data": not recs,
        "k": k,
        "records": len(recs),
        "tasks": tasks,
        "pass_k": {t: v["pass_k"] for t, v in tasks.items()},
        "insufficient_pass_k": thin,
        "trigger": trigger_rate(recs),
        "cost": cost(recs),
    }


def check_coverage(grades: Iterable[Any]) -> dict[str, Any]:
    """每一条判据**实际被评估过多少次** —— 评估集覆盖率不能靠判据悄悄补上。

    为什么需要这个数：判据是按卡启用的（`TaskCard.checks`），而有些约束是**全局**
    的 —— `_OFFLINE_CONSTRAINT`（禁止联网下载）对每一道题都成立，但一张只考技能
    路由的卡不会去开 `offline` 判据。于是一次真实发生的 `pip install` 在那些卡上
    完全不会显形：判据没被启用，报告里就一个字都没有，而**没有任何地方会说明
    "这项根本没查"**。

    实测（2026-09-17 首版数据集）：20 张卡里只有 3 张启用了 `offline`。也就是说
    联网违规在另外 17 张卡上是不可见的。这不是判据写错了，是**覆盖率的盲区**，
    而盲区与"测过且通过"在报告里长得一样 —— 那是最坏的一类。

    所以把每个判据的启用次数与状态分布摆出来：启用次数为 0 的判据、或者被
    `skipped` 吃掉的判据（比如 `offline` 没有注入 provider），一眼就能看见。
    """
    per_check: dict[str, dict[str, Any]] = {}
    total = 0
    for g in grades:
        checks = _field(g, "checks", default=()) or ()
        for c in checks:
            cid = str(_field(c, "id", default="") or "")
            status = str(_field(c, "status", default="") or "")
            if not cid:
                continue
            entry = per_check.setdefault(
                cid, {"evaluated": 0, "pass": 0, "fail": 0, "skipped": 0})
            entry["evaluated"] += 1
            if status in entry:
                entry[status] += 1
            total += 1
    absent = sorted(CHECK_IDS - set(per_check))
    return {
        "per_check": per_check,
        "evaluations": total,
        # 一次都没被启用的判据。非空即为覆盖盲区 —— 别把它读成"都过了"。
        "never_evaluated": absent,
        # 被判据吃掉的那部分（enabled 但 skipped）：这些卡上是"没查"而不是"查过没问题"。
        "skipped_total": sum(e["skipped"] for e in per_check.values()),
    }
