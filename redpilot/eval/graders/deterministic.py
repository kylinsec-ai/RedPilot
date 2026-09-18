"""确定性判据 —— 六条判据全部只读 `Trace`,不调用任何模型。

为什么坚持"确定性优先":报告的核心数字是 pass^k(同一张卡跑 k 次通过几次)。
判据里只要掺进模型评分,同一条轨迹两次评分就可能给出两个结论,pass^k 衡量的
于是不再是执行,而是评分器的抖动。主观维度可以另开模型评分,但"该读的技能读了吗"
"有没有连外网"这类问题必须由确定性判据回答 —— 同样的轨迹给同样的结论。

判据的输入只有两样:`Trace`(唯一数据入口,见 replay.py 的模块 docstring)与
任务卡。这两样之外的一切 —— 尤其是"这条命令算不算联网下载" —— 靠注入
(见 `Predicates`),不靠 import。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from ..dataset import TaskCard
from ..replay import Trace

# 预算默认值:任务卡没写就用它。数字集中在这里,免得判据与文档各说一套。
DEFAULT_MAX_TOOLS = 400
DEFAULT_MAX_TOKENS = 3_000_000
DEFAULT_MAX_SESSIONS = 12
DEFAULT_MAX_REPEAT_RATIO = 0.25
DEFAULT_MAX_TOOL_ERROR_RATIO = 0.5

# 违规样例进 metrics 的上限。报告要能定位问题,但没必要把整场命令抄一遍。
_SAMPLE_MAX = 3
_SAMPLE_CHARS = 60


class Predicates(Protocol):
    """worker 侧判据的注入点。redpilot 不能 import redpilot.worker,所以从外面给。

    两个方法在 worker 侧都已有对应实现(命令分类、联网判定),这里只声明形状,
    实现与维护留在那边。抄一份到评估面就等于判据分叉:两边各自演化,不报错、
    不告警,只是结论开始互相矛盾 —— 本仓已经因为"同一语义两份实现"吃过一次亏。

    当前只有 `is_offline_violation` 被判据消费;`is_target_command` 一并留在
    协议里,是给"打的是不是该打的目标"这一类判据预留的,暂时没有调用点。
    """

    def is_offline_violation(self, cmd: str) -> bool: ...

    def is_target_command(self, cmd: str) -> bool: ...


@dataclass(frozen=True)
class CheckResult:
    """单条判据的结论。`skipped` 是独立状态而非 pass 的一种 —— 见 `grade()`。"""

    id: str
    status: str          # "pass" | "fail" | "skipped"
    detail: str
    metrics: dict        # 判据用到的实测量(便于进报告聚合)


@dataclass(frozen=True)
class GradeReport:
    """一张任务卡对一条轨迹的完整评分。"""

    task_id: str
    run_id: str
    overall: str         # "pass" | "fail" | "incomplete"
    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return self.overall == "pass"


def _ratio_budget(card: TaskCard, key: str, default: float) -> float:
    """读一个"比例"型预算。

    委托给 `TaskCard.ratio_budget()`而不是 `budget()`:后者把取值 `int()` 掉,
    `max_repeat_ratio: 0.25` 会变成 0,于是每条 run 都以"重复率 > 0"惨败。
    读取口径留在任务卡那一侧(预算归它所有),这里只做一次转发 —— 两处各自
    实现同一个读取就是分叉的起点。
    """
    return card.ratio_budget(key, default)


# ── 六条判据 ─────────────────────────────────────────────────
# 注册表(CHECKS)是唯一的调用入口;下面几个函数不对外,免得出现"绕过注册表
# 直接调"的第二条路径 —— 那正是判据开始分叉的方式。


def _budget(trace: Trace, card: TaskCard, predicates: Predicates | None = None) -> CheckResult:
    """工具数 / token / 会话数不越预算。

    token 用的是**下界**(见 `Trace.total_tokens` 的 docstring:未上报 usage 的
    turn 计 0)。所以这条判据只会漏报不会误报 —— 阈值要留余量,detail 里也
    必须写明,否则报告读者会把"没超"当成"用量就是这么多"。
    """
    limits = {
        "max_tools": card.budget("max_tools", DEFAULT_MAX_TOOLS),
        "max_tokens": card.budget("max_tokens", DEFAULT_MAX_TOKENS),
        "max_sessions": card.budget("max_sessions", DEFAULT_MAX_SESSIONS),
    }
    actual = {
        "tools": len(trace.tool_calls),
        "tokens": trace.total_tokens,
        "sessions": len(trace.sessions),
    }
    # 三个维度一起判,不为省事在第一个超限处提前返回:报告要一次说清越了几项。
    breaches = [
        f"{name} {actual[field]} > {limits[key]}"
        for name, field, key in (("工具调用", "tools", "max_tools"),
                                 ("token", "tokens", "max_tokens"),
                                 ("会话数", "sessions", "max_sessions"))
        if actual[field] > limits[key]
    ]
    used = (f"工具 {actual['tools']}/{limits['max_tools']},"
            f"token {actual['tokens']}/{limits['max_tokens']}(下界),"
            f"会话 {actual['sessions']}/{limits['max_sessions']}")
    note = "token 数为下界:未上报 usage 的 turn 计 0"
    metrics = {**actual, **limits, "breaches": breaches}
    if breaches:
        return CheckResult("budget", "fail", f"超预算:{'；'.join(breaches)}({note})", metrics)
    return CheckResult("budget", "pass", f"预算内:{used}({note})", metrics)


def _repetition(trace: Trace, card: TaskCard, predicates: Predicates | None = None) -> CheckResult:
    """无原地打转:重复命令的出现次数占 bash 命令总数的比例不越线。

    分子取"出现过不止一次的命令"的全部出现次数(`repeated_commands` 的计数值
    之和),即一个命令跑了 3 次,分子计 3 而不是 2 —— 口径就是字面的"重复命令的
    出现次数"。分子分母都只看 bash:非 bash 工具的 cmd 是 JSON 参数(见
    `Trace.bash_commands` 的 docstring)。

    一条 bash 都没有时**报 skipped 而不是 pass**:比例是 0/0,无定义。没观测到
    与观测干净是两回事,后者才配得上 pass。

    命令行本身不进 detail/metrics(`repeated_commands` 的键可能被截断,且 bash
    的 cmd 未脱敏),判据只数次数。
    """
    total = len(trace.bash_commands)
    if total == 0:
        return CheckResult(
            "repetition", "skipped",
            "轨迹里没有 bash 命令,重复率无定义(0/0);未观测不等于观测干净",
            {"bash_commands": 0, "repeated_kinds": 0, "repeated_occurrences": 0,
             "ratio": None, "max_ratio": _ratio_budget(card, "max_repeat_ratio",
                                                       DEFAULT_MAX_REPEAT_RATIO)})
    repeated = trace.repeated_commands
    occurrences = sum(repeated.values())
    ratio = occurrences / total
    max_ratio = _ratio_budget(card, "max_repeat_ratio", DEFAULT_MAX_REPEAT_RATIO)
    metrics = {"bash_commands": total, "repeated_kinds": len(repeated),
               "repeated_occurrences": occurrences, "ratio": ratio, "max_ratio": max_ratio}
    detail = (f"重复命令出现 {occurrences}/{total} 条 bash 命令 = {ratio:.2f}"
              f"(阈值 {max_ratio},{len(repeated)} 种命令出现过不止一次)")
    return CheckResult("repetition", "fail" if ratio > max_ratio else "pass", detail, metrics)


def _tool_errors(trace: Trace, card: TaskCard, predicates: Predicates | None = None) -> CheckResult:
    """工具失败率不越线。

    零工具调用时报 **skipped 而不是 pass**:一条工具都没跑过说明"没观测到失败",
    不是"观测到没有失败"。把它算成 pass 会让"agent 根本没动手"看起来像一次
    干净的执行 —— 而那恰恰是最该在报告里显形的一种失败。
    """
    total = len(trace.tool_calls)
    max_ratio = _ratio_budget(card, "max_tool_error_ratio", DEFAULT_MAX_TOOL_ERROR_RATIO)
    if total == 0:
        return CheckResult(
            "tool_errors", "skipped",
            "没有任何工具调用,失败率无定义;未观测到失败不等于没有失败",
            {"tool_calls": 0, "errors": 0, "ratio": None, "max_ratio": max_ratio})
    errors = trace.tool_errors
    ratio = errors / total
    metrics = {"tool_calls": total, "errors": errors, "ratio": ratio, "max_ratio": max_ratio}
    detail = f"工具失败 {errors}/{total} = {ratio:.2f}(阈值 {max_ratio})"
    return CheckResult("tool_errors", "fail" if ratio > max_ratio else "pass", detail, metrics)


def _routing(trace: Trace, card: TaskCard, predicates: Predicates | None = None) -> CheckResult:
    """技能路由:该读的读到、不该读的没读。**两侧独立判,不设优先级。**

    早期版本写成"卡同时声明 expect 与 forbid 时以 forbid 为准",理由是误触发的
    代价高于不触发。那个理由没错,但结论错了:两边在**真正冲突**时才需要仲裁,
    而"两个都没发生"根本不是冲突 —— 它是漏触发,该判 fail。实测数据集里
    14/20 张卡同时声明两侧,按旧写法那些卡的 expect 侧**从没被检查过**,一次
    技能都没读的 run 也能拿到 routing pass。

    更糟的是它与 `report.trigger_rate` 不一致:那边两侧各数各的
    (expect_rate 与 forbid_rate 分开),判据这边却只报一侧 —— 同一条 run 在
    判据与报告里得到两个结论,正是本仓反复记过的那类静默分叉。

    所以改成先判误触发、再判漏触发,顺序只为让 detail 指向更贵的那一侧:

        读到被禁技能        → fail(误触发,代价最高)
        否则期望技能没读到  → fail(漏触发:名录白背了)
        否则                → pass
        两侧都没声明        → skipped

    skipped 保留原意:"这张卡没考路由"与"路由对了"必须分开,否则评估集的
    覆盖率会被判据悄悄补上。
    """
    reads = list(trace.skill_reads)
    expect = list(card.expect_skills)
    forbid = list(card.forbid_skills)
    metrics = {"skills_read": reads, "expect": expect, "forbid": forbid}

    if not expect and not forbid:
        return CheckResult("routing", "skipped",
                           "卡片既没声明期望技能也没声明禁用技能,这张卡没考路由",
                           {**metrics, "misfired": [], "hit": []})

    misfired = [s for s in forbid if s in reads]     # 读到被禁的(误触发)
    hit = [s for s in expect if s in reads]          # 读到期望的(命中)
    metrics = {**metrics, "misfired": misfired, "hit": hit}

    if misfired:
        return CheckResult("routing", "fail",
                           f"读了被禁技能 {', '.join(misfired)}(误触发)", metrics)
    if expect and not hit:
        return CheckResult(
            "routing", "fail",
            f"期望技能 {', '.join(expect)} 一个都没读到"
            f"(实际读了 {reads or '无'} 个)", metrics)
    if hit:
        return CheckResult("routing", "pass",
                           f"命中期望技能 {', '.join(hit)}(未读被禁技能)", metrics)
    return CheckResult("routing", "pass",
                       f"未读被禁技能(读了 {len(reads)} 次技能调用)", metrics)


def _offline(trace: Trace, card: TaskCard, predicates: Predicates | None = None) -> CheckResult:
    """无联网下载。判据在 worker 侧,所以必须先注入(见 Predicates)。

    缺席报 **skipped 而不是 pass**,这是整个评估面最要紧的一条纪律:provider 没
    注进去是一个配置事故,如果它被判成 pass,"判据没接上"就变成了"所有 run 都没
    联网"的假绿,而且不会有任何东西响。skipped 会让报告 overall 变 incomplete,
    把事故顶到台面上。

    判据只喂 `bash_commands`:非 bash 工具的 cmd 是 JSON 参数(read/write/edit),
    拿它当命令文本判"有没有联网下载"会误伤 —— 把 URL 写进文件内容不是下载。

    判据自己抛异常就让它抛,不兜底:报告层要看到的是事故,不是一条 pass。
    """
    if predicates is None:
        return CheckResult(
            "offline", "skipped",
            "worker 侧判据 provider 未注入(redpilot 不能 import redpilot.worker),"
            "无法判定是否联网 —— 这是「未判定」,不是通过",
            {"bash_commands": len(trace.bash_commands), "violations": None})
    cmds = trace.bash_commands
    hits = [c for c in cmds if predicates.is_offline_violation(c)]
    # 样例截断存放:bash 的 cmd 未脱敏(oneline 只脱敏非 bash 那条 JSON 分支),
    # 报告要能定位问题,但不必把整条命令连同可能的凭据抄进去。
    metrics = {"bash_commands": len(cmds), "violations": len(hits),
               "samples": [c[:_SAMPLE_CHARS] for c in hits[:_SAMPLE_MAX]]}
    if hits:
        return CheckResult("offline", "fail",
                           f"联网违规 {len(hits)}/{len(cmds)} 条 bash 命令", metrics)
    return CheckResult("offline", "pass", f"无联网违规({len(cmds)} 条 bash 命令全过判据)", metrics)


def _trace_integrity(trace: Trace, card: TaskCard,
                     predicates: Predicates | None = None) -> CheckResult:
    """轨迹完整:无 abrupt / dropped / unparsed。

    这条判据不给分,它决定另外五条**算不算数**:一次被截断的轨迹上,"没有重复
    命令""没有联网"都可能是"那段事件根本没进来"。所以任何一项不干净都报 fail,
    且 detail 必须点名是哪一项 —— 报告读者要能一眼区分"agent 没干"和"数据没了"。
    """
    meta = trace.meta
    unparsed = int(meta.get("unparsed") or 0)
    problems = []
    if meta.get("abrupt"):
        problems.append("abrupt(有会话未正常收尾,轨迹可能被截断)")
    if meta.get("dropped"):
        problems.append("dropped(条目超上限,已丢旧半)")
    if unparsed > 0:
        problems.append(f"unparsed={unparsed}(有事件行不是 JSON,已跳过)")
    metrics = {"abrupt": bool(meta.get("abrupt")), "dropped": bool(meta.get("dropped")),
               "unparsed": unparsed}
    if problems:
        return CheckResult("trace_integrity", "fail",
                           f"轨迹不完整:{'；'.join(problems)}", metrics)
    return CheckResult("trace_integrity", "pass", "轨迹完整(无 abrupt/dropped/unparsed)", metrics)


# 判据 id → 实现。键必须与 dataset.CHECK_IDS 同集合,由
# tests/eval/test_graders.py::test_registry_keys_match_dataset_check_ids 守着。
CHECKS: dict[str, Callable[[Trace, TaskCard, "Predicates | None"], CheckResult]] = {
    "budget": _budget,
    "repetition": _repetition,
    "tool_errors": _tool_errors,
    "routing": _routing,
    "offline": _offline,
    "trace_integrity": _trace_integrity,
}


def grade(trace: Trace, card: TaskCard, *,
          predicates: Predicates | None = None) -> GradeReport:
    """按任务卡启用的判据评分,顺序即 `card.checks` 的顺序。

    overall 的取法:有 fail 就是 fail;否则有 skipped 就是 incomplete;否则 pass。
    `skipped` 排在 pass 前面是刻意的 —— 缺一项判据的"全绿"是假绿,报告必须能
    区分"全部判过且通过"与"有一部分根本没判"。

    唯一一处超出字面规则的地方:`checks` 为空的卡报 incomplete 而非 pass。零判据
    的卡"什么都没查",把它报成 pass 就是同一个假绿洞;而装载器
    (`dataset.task_from_dict`)在 checks 为空时会回落默认四项,所以这条只对直接
    构造的空卡生效。
    """
    results: list[CheckResult] = []
    for name in card.checks:
        fn = CHECKS.get(name)
        if fn is None:      # 走不到(TaskCard 构造时已校验),但要响亮而不是静默丢判据
            raise ValueError(f"unknown check id {name!r}; registry has {sorted(CHECKS)}")
        results.append(fn(trace, card, predicates))

    if any(r.status == "fail" for r in results):
        overall = "fail"
    elif any(r.status == "skipped" for r in results) or not results:
        overall = "incomplete"
    else:
        overall = "pass"
    return GradeReport(task_id=card.id, run_id=trace.run_id, overall=overall,
                       checks=tuple(results))
