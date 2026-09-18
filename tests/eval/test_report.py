"""评估报告与评估库的回归:pass^k 的口径、触发率的两侧、成本的下界、落库幂等。

判据报告(`redpilot.eval.graders`)由并行任务实现,本文件**不 import 它** —— 跨任务的
进度依赖会让这里的红/绿取决于别人的提交时间,而不是这里的代码。改用与简报同形的
替身(见下面的 _Check/_Grade):字段名与
`GradeReport(task_id, run_id, overall, checks)` / `.ok` 一致。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from redpilot.eval.report import RunRecord, cost, pass_k, summary, trigger_rate
from redpilot.eval.store import DEFAULT_DB_PATH, EvalStore

_SEQ = itertools.count(1)


# ── 判据报告替身(just enough:存储与聚合只认这几个字段)──

@dataclass(frozen=True)
class _Check:
    id: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class _Grade:
    task_id: str
    run_id: str
    overall: str
    checks: tuple[_Check, ...] = ()

    @property
    def ok(self) -> bool:
        return self.overall == "pass"


def _rec(outcome: str, *, task_id: str = "t1", expect_skills: tuple = (),
         forbid_skills: tuple = (), skills_read: tuple = (), tokens: int = 0,
         tool_calls: int = 0) -> RunRecord:
    """一条记录(run_id 自动编号,报告侧不关心它的取值)。"""
    return RunRecord(task_id=task_id, run_id=f"r{next(_SEQ)}", outcome=outcome,
                     expect_skills=tuple(expect_skills),
                     forbid_skills=tuple(forbid_skills),
                     skills_read=tuple(skills_read),
                     tokens=tokens, tool_calls=tool_calls)


# ── pass^k ──

def test_pass_k_counts_consecutive_successes_only():
    """口径:长度为 k 的窗口里"全 pass"的占比 —— 断一次的代价是窗口,不是单次。"""
    assert pass_k(["pass", "pass", "pass"], k=3) == 1.0
    # 总体成功率 2/3,但没有任何一个窗口是"连续三次":pass@3 会算它 100%,pass^3 是 0
    assert pass_k(["pass", "fail", "pass"], k=3) == 0.0
    # 断层之后窗口重新计数:4 个窗口里只有最后一个全 pass
    seq = ["pass", "pass", "fail", "pass", "pass", "pass"]
    assert pass_k(seq, k=3) == 1 / 4
    # k=1 退化为单次成功率(5/6),与 k=3 的 25% 判若两物 —— 报告 §7.2:单次 75% 时
    # "三次里至少一次"约 98.4%,而"连续三次"只有 42.2%。差的就是"连续"两个字。
    assert pass_k(seq, k=1) == 5 / 6


def test_pass_k_accepts_records_and_plain_outcomes():
    assert pass_k([_rec("pass"), _rec("pass"), _rec("pass")], k=3) == 1.0
    assert pass_k([_rec("pass"), _rec("fail"), _rec("pass")], k=3) == 0.0


def test_pass_k_returns_none_when_observations_are_thin():
    """观测不足 k 次 → None(不是 0.0):未测 ≠ 连败。"""
    assert pass_k(["pass", "pass"], k=3) is None
    assert pass_k([], k=1) is None
    assert pass_k([_rec("pass")], k=3) is None
    with pytest.raises(ValueError):
        pass_k(["pass"], k=0)


# ── 技能路由:漏触发 vs 误触发 ──

def test_trigger_rate_separates_miss_from_misfire():
    recs = [
        # 正样本:期望的技能读到了 → 命中
        _rec("pass", skills_read=("web-attack",), expect_skills=("web-attack",)),
        # 正样本:该读没读,读了别的 → 漏触发(没声明禁读,故不算误触发)
        _rec("fail", skills_read=("crypto",), expect_skills=("web-attack",)),
        # 负样本:禁读的技能被读了 → 误触发
        _rec("fail", skills_read=("crypto",), forbid_skills=("crypto",)),
        # 负样本:干净
        _rec("pass", skills_read=("web-recon",), forbid_skills=("crypto",)),
    ]
    rate = trigger_rate(recs)
    assert (rate["expect_n"], rate["expect_read"], rate["expect_missed"]) == (2, 1, 1)
    assert rate["expect_rate"] == 0.5
    assert (rate["forbid_n"], rate["forbid_triggered"]) == (2, 1)
    assert rate["forbid_rate"] == 0.5
    assert rate["records"] == 4


def test_trigger_rate_without_declarations_is_none_not_zero():
    """没声明期望/禁读的卡不进分母 —— 分母为 0 时比率是 None,不是 0%。"""
    rate = trigger_rate([_rec("pass", skills_read=("web-attack",))])
    assert (rate["expect_n"], rate["forbid_n"]) == (0, 0)
    assert rate["expect_rate"] is None and rate["forbid_rate"] is None


# ── 成本 ──

def test_cost_totals_per_task_and_flags_lower_bound():
    recs = [_rec("pass", task_id="t1", tokens=100, tool_calls=3),
            _rec("fail", task_id="t1", tokens=50, tool_calls=1),
            _rec("pass", task_id="t2", tokens=7, tool_calls=2)]
    c = cost(recs)
    assert c["per_task"]["t1"] == {"runs": 2, "tokens": 150, "tool_calls": 4,
                                   "mean_tokens": 75.0, "mean_tool_calls": 2.0}
    assert c["per_task"]["t2"]["mean_tokens"] == 7.0
    assert (c["runs"], c["tokens"], c["tool_calls"]) == (3, 157, 6)
    assert c["mean_tokens"] == pytest.approx(157 / 3)
    # 缺 usage 的 turn 贡献 0 → 这是下界,不是实测总量(数字单独流传时别丢了限定语)
    assert c["tokens_is_lower_bound"] is True


def test_cost_of_empty_input_has_no_mean():
    c = cost([])
    assert c["runs"] == 0 and c["tokens"] == 0
    assert c["mean_tokens"] is None and c["mean_tool_calls"] is None


# ── summary ──

def test_summary_marks_empty_input_as_insufficient():
    """空输入:0 是"没数据"而不是"测得 0",故比率缺席、顶层带标记。"""
    s = summary([])
    assert s["insufficient_data"] is True
    assert s["records"] == 0 and s["tasks"] == {}
    assert s["trigger"]["expect_rate"] is None
    assert s["trigger"]["forbid_rate"] is None
    assert s["cost"]["mean_tokens"] is None


def test_summary_flags_thin_tasks_instead_of_reporting_zero():
    recs = [_rec("pass", task_id="thin") for _ in range(2)]
    recs += [_rec("pass", task_id="ok") for _ in range(3)]
    s = summary(recs, k=3)
    assert s["insufficient_data"] is False
    assert s["pass_k"]["ok"] == 1.0
    assert s["pass_k"]["thin"] is None          # 不是 0.0
    assert s["insufficient_pass_k"] == ["thin"]
    assert s["tasks"]["ok"]["outcomes"] == {"pass": 3}


def test_record_of_joins_card_trace_and_grade():
    """拼装层:期望来自任务卡,实测来自 Trace,结局来自评分结果。"""
    card = SimpleNamespace(id="t1", expect_skills=("web-attack",),
                           forbid_skills=("crypto",))
    trace = SimpleNamespace(run_id="r1", skill_reads=("web-attack",),
                            total_tokens=123, tool_calls=[1, 2, 3])
    rec = RunRecord.of(card, trace, _Grade("t1", "r1", "pass"))
    assert (rec.task_id, rec.run_id, rec.outcome) == ("t1", "r1", "pass")
    assert rec.expect_skills == ("web-attack",) and rec.forbid_skills == ("crypto",)
    assert rec.skills_read == ("web-attack",)
    assert (rec.tokens, rec.tool_calls) == (123, 3)


# ── EvalStore ──

def test_default_db_is_not_the_production_db():
    """评估结果落自己的库:Glean 那种"重评历史 run"的写节奏不许去抢 ingest 的写锁。"""
    assert DEFAULT_DB_PATH.endswith("eval.sqlite3")
    assert "obs" not in DEFAULT_DB_PATH


def test_store_round_trip_and_regrade_replaces(tmp_path):
    with EvalStore(tmp_path / "eval.sqlite3") as store:
        store.put_grade(_Grade("t1", "r1", "fail",
                               (_Check("budget", "fail", "工具数 41 > 30"),)),
                        graded_at=1.0)
        rows = store.grades(task_id="t1")
        assert len(rows) == 1
        assert (rows[0]["run_id"], rows[0]["overall"], rows[0]["graded_at"]) == ("r1", "fail", 1.0)
        assert rows[0]["checks"] == [{"id": "budget", "status": "fail",
                                      "detail": "工具数 41 > 30"}]
        assert [c["check_id"] for c in store.checks("r1", "t1")] == ["budget"]
        assert store.graded_run_ids("t1") == ["r1"]

        # 重评(判据演进后 observer 会做):覆盖,不是追加
        store.put_grade(_Grade("t1", "r1", "pass",
                               (_Check("budget", "pass"), _Check("routing", "pass"))),
                        graded_at=2.0)
        rows = store.grades(task_id="t1")
        assert len(rows) == 1                                    # 一行,不是两行
        assert (rows[0]["overall"], rows[0]["graded_at"]) == ("pass", 2.0)
        assert [c["check_id"] for c in store.checks("r1", "t1")] == ["budget", "routing"]

        # 判据变少:上一轮的明细整体换掉,不留残行(否则报告会算上已不存在的判据)
        store.put_grade(_Grade("t1", "r1", "pass", (_Check("budget", "pass"),)),
                        graded_at=3.0)
        assert [c["check_id"] for c in store.checks("r1", "t1")] == ["budget"]
        assert store.graded_run_ids("t1") == ["r1"]


def test_store_filters_by_task_and_takes_dict_grades(tmp_path):
    with EvalStore(tmp_path / "eval.sqlite3") as store:
        store.put_grade(_Grade("t1", "r1", "pass"), graded_at=1.0)
        store.put_grade(_Grade("t2", "r2", "fail"), graded_at=2.0)
        # 库行是 dict,put 也该吃 dict(observer 从旧库转抄时不用先造对象)
        store.put_grade({"run_id": "r3", "task_id": "t1", "overall": "incomplete",
                         "checks": [{"id": "offline", "status": "skipped"}]},
                        graded_at=3.0)
        assert len(store.grades()) == 3
        assert len(store.grades(task_id="t1")) == 2
        assert store.grades(task_id="t1")[0]["run_id"] == "r3"    # 最近评的在前
        assert store.graded_run_ids("t1") == ["r1", "r3"]         # 首次评分先后
        assert store.checks("r3", "t1")[0]["status"] == "skipped"


def test_store_rejects_grade_without_ids(tmp_path):
    """缺 id 响亮失败:空 id 会让两份评分撞成一份,报告里看不出来。"""
    with EvalStore(tmp_path / "eval.sqlite3") as store:
        with pytest.raises(ValueError):
            store.put_grade(_Grade("", "r1", "pass"))
        with pytest.raises(ValueError):
            store.put_grade({"run_id": "r1", "task_id": "t1",
                             "overall": "pass", "checks": [{"status": "pass"}]})


def test_summary_sorts_by_execution_time_not_caller_order():
    """回归：`pass^k` 要**执行顺序**，而评估库给的是**评分顺序**。

    两者在重评历史 run 之后会分叉。若照调用方传进来的顺序算，"连续 k 次"数的
    就是另一条时间线 —— 得到一个看起来完全正常的错数，不报错、不告警。
    `RunRecord` 因此带上 `started_at`，由 `summary()` 自己排。

    要看出顺序的影响，窗口必须多于一个（n > k）—— n == k 时只有一个窗口，
    顺序无从体现。这里刻意构造：真实执行顺序得 0.0，而按调用方传入的顺序
    得 0.5。两个数都"合理"，只有一个是真话。
    """
    from redpilot.eval.report import RunRecord, summary

    # 真实执行顺序（按 started_at）：pass, pass, fail, pass → 0/2 个窗口全过
    # 调用方传入顺序：              fail, pass, pass, pass → 1/2 个窗口全过
    exec_order = [(100.0, "pass"), (200.0, "pass"), (300.0, "fail"), (400.0, "pass")]
    recs = [RunRecord(task_id="t", run_id=f"r{i}", outcome=o, started_at=ts)
            for i, (ts, o) in enumerate(exec_order)]
    shuffled = [recs[2], recs[0], recs[1], recs[3]]

    assert pass_k(shuffled, 3) == 0.5, "直接喂 pass_k 就是按传入顺序算（调用方负责排）"
    assert summary(shuffled, k=3)["pass_k"]["t"] == 0.0, (
        "summary 必须按执行时刻重排 —— 否则重评过的历史会让 pass^k 悄悄算错")


def test_summary_keeps_caller_order_when_no_timestamps():
    """没有 started_at（合成轨迹）时保持传入顺序 —— 改动前后行为一致。"""
    from redpilot.eval.report import RunRecord, summary

    recs = [RunRecord(task_id="t", run_id=str(i), outcome=o)
            for i, o in enumerate(("pass", "pass", "fail"))]
    assert summary(recs, k=3)["pass_k"]["t"] == 0.0
