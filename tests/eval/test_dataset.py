"""评估集守卫:默认数据集可装载、四类齐备、技能名真实存在。

最有价值的一条是 `test_skill_refs_exist` —— 任务卡里的 `expect_skills` /
`forbid_skills` 是**字符串**,写错一个名字不会有任何运行时症状:装载成功、
判据照跑,只是那条判据永远判不出"期望命中"。所以这里把每个名字拿回
`skills/` 目录去核对(逐目录含 SKILL.md 才算技能,与 `is_skill_dir` 同一判据)。

`skills/` 的定位走 `redpilot.contracts.paths.skills_root`,不数层数:本仓已被
"上溯 N 层"的写法坑过一次(搬目录后技能面静默归零),测试里再写一份就是
把这个坑复制到第二处。
"""

from __future__ import annotations

import json

import pytest
from redpilot.eval.dataset import CATEGORIES, CHECK_IDS, TaskCard, coverage, load_tasks, task_from_dict
from redpilot.contracts.paths import is_skill_dir, skills_root

# 仓库里的技能库根。定位失败(空串)时下面的测试必须响亮失败,而不是把所有
# 技能名都判成"不存在"——后者会把定位失败伪装成数据集错误。
SKILLS_ROOT = skills_root(__file__)

# 模块级装载:数据集本身坏了(JSON 语法、重复 id、未知字段)就在**收集期**炸掉,
# 而不是在某个断言里以"条数不对"的面目出现。
CARDS = load_tasks()

ALL_SKILL_REFS = [
    pytest.param(skill, id=f"{card.id}:{field}:{skill}")
    for card in CARDS
    for field in ("expect_skills", "forbid_skills")
    for skill in getattr(card, field)
]


# ── 定位与齐备 ──

def test_skills_root_found():
    """先证明技能库能被定位 —— 否则下面的"技能名不存在"全是假的。"""
    assert SKILLS_ROOT, "skills/ 定位失败:技能名核对会退化成全不存在的假阳性"
    assert is_skill_dir(SKILLS_ROOT + "/hack"), "skills/ 下应存在入口技能 hack"


def test_default_dataset_has_20_cards():
    assert len(CARDS) == 20


def test_all_four_categories_present():
    cov = coverage(CARDS)
    assert not [c for c, n in cov.items() if not n], f"有类别为空:{cov}"
    assert set(cov) == set(CATEGORIES)
    assert sum(cov.values()) == len(CARDS)


# ── 技能名核对(本文件的主要目的)──

@pytest.mark.parametrize("skill", ALL_SKILL_REFS)
def test_skill_refs_exist(skill):
    """每个期望/禁止的技能名都必须是真实技能:skills/<name>/ 且含 SKILL.md。"""
    path = f"{SKILLS_ROOT}/{skill}"
    assert is_skill_dir(path), f"技能名 {skill!r} 在 skills/ 下不存在或没有 SKILL.md"


def test_no_card_expects_and_forbids_same_skill():
    """同一张卡里既期望又禁止同一技能 = 恒不可满足的卡,装载期看不出来。"""
    for card in CARDS:
        both = set(card.expect_skills) & set(card.forbid_skills)
        assert not both, f"task {card.id}: {sorted(both)} 同时出现在 expect 与 forbid"


# ── 判据与预算 ──

def test_checks_are_in_grader_registry():
    for card in CARDS:
        assert card.checks, f"task {card.id}: checks 为空(装载期会回落默认值,别依赖)"
        assert set(card.checks) <= CHECK_IDS, f"task {card.id}: 未知判据 {sorted(set(card.checks) - CHECK_IDS)}"


def test_every_card_is_scorable():
    """卡必须能判对错:有题面、有意图、有参考解、有计数预算。"""
    for card in CARDS:
        assert card.prompt.strip(), f"task {card.id}: 无题面"
        assert card.intent.strip(), f"task {card.id}: 无 intent"
        assert card.reference.strip(), f"task {card.id}: 无参考解,无法分辨'题难'与'判据错'"
        assert card.budget("max_tools", 0) > 0, f"task {card.id}: 无 max_tools 预算"
        assert card.budget("max_sessions", 0) > 0, f"task {card.id}: 无 max_sessions 预算"


def test_negative_cards_declare_forbid_skills():
    negatives = [c for c in CARDS if c.category == "negative"]
    assert negatives, "负样本为零 —— 误触发这一类失败将完全不可见"
    for card in negatives:
        assert card.forbid_skills, f"task {card.id}: 负样本未点名禁用的技能"


def test_challenge_code_deferred():
    """题号一律留空:本机没有真实 run 数据,填进去就是编造绑定,后续串真实 run 再补。"""
    assert [c.id for c in CARDS if c.challenge_code] == []


# ── 装载期的响亮失败 ──

def _write(tmp_path, tasks) -> str:
    p = tmp_path / "ds.json"
    p.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_duplicate_id_raises(tmp_path):
    card = {"id": "dup", "category": "explicit", "prompt": "x"}
    with pytest.raises(ValueError, match="duplicate task id"):
        load_tasks(_write(tmp_path, [card, dict(card)]))


def test_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_tasks(str(tmp_path / "nope"))


@pytest.mark.parametrize("bad, match", [
    ({"id": "u", "category": "explicit", "skillz": ["ssti-server-side-template-injection"]},
     "unknown field"),
    ({"id": "c", "category": "unknown-cat"}, "unknown category"),
    ({"id": "k", "category": "explicit", "checks": ["budget", "vibes"]}, "unknown check"),
    ({"id": "n", "category": "negative"}, "forbid_skills"),
])
def test_bad_card_raises(bad, match):
    with pytest.raises(ValueError, match=match):
        task_from_dict(bad)


def test_task_card_is_frozen():
    """任务卡是数据集不是运行态:能改就会有人改,报告里的 pass^k 随之失去可比性。"""
    with pytest.raises(Exception):
        TaskCard(id="x", category="explicit", intent="", prompt="").id = "y"
