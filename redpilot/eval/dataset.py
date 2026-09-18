"""任务卡：评估集的最小单元与其装载。

一张任务卡 = 一条可判定的用例。报告要求四类齐备，缺负样本会出事：

    explicit  题面直接点名技能       —— 验证技能名与指令可用
    implicit  只描述现象，不点名      —— 验证 `description` 是否够精确（路由主力）
    noisy     带业务噪声/无关上下文   —— 模拟真实题面
    negative  不该触发某技能          —— **捕捉误触发**

负样本为什么必须单列：报告 §5.4 引 Glean 的生产数据 —— 提供 skill 反而会**先降低**
正确触发率约 20%，补上"不要在……时调用"才恢复。误触发比不触发更贵：它会改工作目录、
耗回合、污染后续判断。只有正样本的评估集看不见这一类失败。

**凭什么叫"评估集"**：每张卡都要能被评分器判对错。所以 `checks` 列出启用的判据 id，
`budgets` 给出预算，`reference` 至少给一个能通过的参考解 —— 没有参考解的用例无法
分辨"题目太难怪 agent"还是"评分器写错了"。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Iterator

# 四类用例。判定卡是否齐备用它，别在别处再写一份字面量。
CATEGORIES: tuple[str, ...] = ("explicit", "implicit", "noisy", "negative")

# 判据 id 的词表 —— 与 graders.deterministic 的注册表**同名同集合**。
# 各写一份会漂移，所以这里只列名，实现方在 deterministic 里注册；
# 装载时校验（见 _CHECKS 与 load_tasks 的校验），拼错立刻响亮失败。
CHECK_IDS: frozenset[str] = frozenset({
    "budget",            # 工具数 / token / 会话数不越预算
    "repetition",        # 无原地打转（重复命令占比）
    "tool_errors",       # 工具失败率不越线
    "routing",           # 技能路由：期望的技能被读到、被禁的技能没被读
    "offline",           # 无联网下载（需注入判据provider，缺则 skipped）
    "trace_integrity",   # 轨迹完整（无 abrupt / dropped / unparsed）
})


@dataclass(frozen=True)
class TaskCard:
    """一条评估用例。不可变 —— 任务卡是数据集，不是运行态。"""

    id: str
    category: str
    intent: str                      # 这张卡在考什么（人读）
    prompt: str                      # 题面
    expect_skills: tuple[str, ...] = ()     # 期望被路由到的技能（任一命中即算）
    forbid_skills: tuple[str, ...] = ()     # 不该被路由到的技能（负样本核心）
    challenge_code: str = ""         # 绑定的真实 run 的题号（回放用；空=纯题面卡）
    checks: tuple[str, ...] = ("budget", "repetition", "tool_errors", "routing")
    budgets: dict = field(default_factory=dict)
    reference: str = ""              # 参考解 / 判定依据（人读）
    note: str = ""

    def __post_init__(self):
        if self.category not in CATEGORIES:
            raise ValueError(
                f"task {self.id}: unknown category {self.category!r}; "
                f"must be one of {CATEGORIES}")
        bad = set(self.checks) - CHECK_IDS
        if bad:
            raise ValueError(
                f"task {self.id}: unknown check id(s) {sorted(bad)}; "
                f"must be in grader registry {sorted(CHECK_IDS)}")
        if self.category == "negative" and not self.forbid_skills:
            raise ValueError(
                f"task {self.id}: negative card must declare forbid_skills —— "
                "负样本的全部意义就是点名'不该走哪里'")

    @property
    def is_negative(self) -> bool:
        return self.category == "negative"

    def budget(self, key: str, default: int) -> int:
        """**计数**型预算（工具数 / token / 会话数）。

        不要拿它读比例型预算：`int(0.25)` 是 0，于是 `max_repeat_ratio: 0.25`
        会变成"重复率必须为 0"，每一条正常轨迹都以惨败收场 —— 而失败信息里
        看不出是单位错配。比例走 `ratio_budget()`。这两个方法分开，就是因为
        这个坑唯一的防法是不给同一个入口同时喂两种单位。
        """
        try:
            return int(self.budgets.get(key, default))
        except (TypeError, ValueError):
            return default

    def ratio_budget(self, key: str, default: float) -> float:
        """**比例**型预算（0–1 之间，如 `max_repeat_ratio`）。"""
        try:
            return float(self.budgets.get(key, default))
        except (TypeError, ValueError):
            return default


def _as_tuple(v) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(str(x) for x in v)


def task_from_dict(d: dict) -> TaskCard:
    """从 JSON 对象建卡。未知键**响亮失败** —— 拼错字段名会静默丢整条用例。"""
    known = {f for f in TaskCard.__dataclass_fields__}
    unknown = set(d) - known
    if unknown:
        raise ValueError(f"task {d.get('id', '?')}: unknown field(s) {sorted(unknown)}")
    return TaskCard(
        id=str(d["id"]),
        category=str(d.get("category", "")),
        intent=str(d.get("intent", "")),
        prompt=str(d.get("prompt", "")),
        expect_skills=_as_tuple(d.get("expect_skills")),
        forbid_skills=_as_tuple(d.get("forbid_skills")),
        challenge_code=str(d.get("challenge_code", "")),
        checks=_as_tuple(d.get("checks")) or TaskCard.checks,
        budgets=dict(d.get("budgets") or {}),
        reference=str(d.get("reference", "")),
        note=str(d.get("note", "")),
    )


# 数据集就放在本模块旁边。用**同目录相对**而不是"上溯 N 层"：后者在目录布局
# 变动后会静默指错（`redpilot/contracts/paths.py` 的 docstring 记了同一个坑）。
_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datasets")


def load_tasks(path: str = "") -> list[TaskCard]:
    """装载任务卡。path 可以是单个 JSON 文件，也可以是目录（收 *.json，按名排序）。

    重复 id 响亮失败：数据集里两条同 id 的卡会让报告里的 pass^k 互相覆盖。
    """
    files: list[str] = []
    if not path:
        path = _DEFAULT_DIR
    if os.path.isdir(path):
        files = [os.path.join(path, n) for n in sorted(os.listdir(path))
                 if n.endswith(".json")]
    elif os.path.isfile(path):
        files = [path]
    else:
        raise FileNotFoundError(f"dataset path not found: {path}")

    cards: list[TaskCard] = []
    seen: dict[str, str] = {}
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise ValueError(f"{f}: expected a list of tasks or {{'tasks': [...]}}")
        for d in items:
            card = task_from_dict(d)
            if card.id in seen:
                raise ValueError(
                    f"duplicate task id {card.id!r}: {seen[card.id]} and {f}")
            seen[card.id] = f
            cards.append(card)
    return cards


def coverage(cards: list[TaskCard]) -> dict[str, int]:
    """四类用例的条数（报告要求四类齐备；缺哪类在报告里要显式说）。"""
    out = {c: 0 for c in CATEGORIES}
    for card in cards:
        out[card.category] = out.get(card.category, 0) + 1
    return out


def iter_missing_categories(cards: list[TaskCard]) -> Iterator[str]:
    """返回条数为 0 的类别名 —— 调用方负责把它变成警告或失败。"""
    cov = coverage(cards)
    for cat in CATEGORIES:
        if not cov.get(cat):
            yield cat
