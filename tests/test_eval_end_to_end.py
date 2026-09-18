"""评估面端到端：回放 → 判据 → 报告，跨 ghost/worker 两包跑一遍。

为什么这条测试放在仓库根的 `tests/`（而不是 `packages/ghost/tests/`）：它**必须
同时 import 两个包** —— 评估面在 `ghost.eval`，判据实现在
`redpilot.worker.adapter.eval_bridge`，而生产代码里 `ghost` 不许 import `ghost_worker`
（红线，见 `packages/ghost/tests/test_ghost_purity.py`）。测试是唯一能合法把两边
接起来的地方，接得起来这件事本身就是要验的东西：

    判据注入这条路走得通吗？还是说"注入协议"只是个说法、真接的时候接不上？

这条测试回答的就是它。它跑的是**离线回放**：零 LLM、零网络、零靶场，全是合成
事件，所以能进 CI。
"""

from __future__ import annotations

import json
import unittest

from redpilot.eval import (CHECK_IDS, EvalStore, RunRecord, load_tasks, pass_k, summary,
                        trigger_rate)
from redpilot.eval.dataset import TaskCard
from redpilot.eval.graders import grade
from redpilot.eval.replay import trace_from_rows

# ── 跨包接线：worker 侧的判据实现（生产代码里这是唯一的注入点）──
from redpilot.worker.adapter.eval_bridge import task_predicates


def ev(**kw) -> dict:
    return {"payload": json.dumps(kw)}


def tool(call_id: str, name: str, text: str = "ok", is_error: bool = False,
         **args) -> list[dict]:
    return [
        ev(type="tool_execution_start", toolName=name, toolCallId=call_id, args=args),
        ev(type="tool_execution_end", toolCallId=call_id, isError=is_error,
           result={"content": [{"text": text}]}),
    ]


def run_events(*, skills: tuple[str, ...] = (), commands: tuple[str, ...] = (),
               tokens: int = 5000) -> list[dict]:
    """合成一次执行：开场会话 → 若干技能读取与命令 → 正常收尾。"""
    rows = [ev(type="session", id="sess000001", cwd="/work/demo")]
    for i, skill in enumerate(skills):
        rows.append(ev(type="turn_start"))
        rows += tool(f"r{i}", "read", path=f"/app/skills/{skill}/SKILL.md")
        rows.append(ev(type="turn_end", message={"usage": {"totalTokens": tokens}}))
    for i, cmd in enumerate(commands):
        rows.append(ev(type="turn_start"))
        rows += tool(f"c{i}", "bash", command=cmd, text="...")
        rows.append(ev(type="turn_end", message={"usage": {"totalTokens": tokens}}))
    rows.append(ev(type="agent_end"))
    return rows


TARGET = "http://10.0.0.5:8080"


class EndToEndTests(unittest.TestCase):
    """离线回放的全链路：合成轨迹 → 判据 → 报告。"""

    def _card(self, **kw) -> TaskCard:
        base = dict(id="demo-1", category="explicit", intent="考路由",
                    prompt="……", challenge_code="")
        base.update(kw)
        return TaskCard(**base)

    def test_clean_run_passes_all_enabled_checks(self):
        """干净轨迹：读了期望技能、命令都在目标上 → 全判据通过。

        这是"接线正确"的正面证明 —— 判据 provider 注进去了、`offline` 会真的
        判定（而不是 skipped）、routing 认得出读过的技能。
        """
        card = self._card(expect_skills=("hack",), checks=(
            "budget", "repetition", "tool_errors", "routing", "offline",
            "trace_integrity"))
        trace = trace_from_rows(
            {"run_id": "a" * 32, "challenge_code": "demo"},
            run_events(skills=("hack",),
                       commands=(f"curl -s {TARGET}/api", f"nmap -Pn 10.0.0.5")))
        report = grade(trace, card, predicates=task_predicates(targets=[TARGET]))

        self.assertEqual([c.status for c in report.checks],
                         ["pass"] * 6, [c.detail for c in report.checks])
        self.assertEqual(report.overall, "pass")
        self.assertTrue(report.ok)

    def test_misfire_fails_routing(self):
        """负样本的核心场景：读了不该读的技能 → routing 失败。"""
        card = self._card(id="demo-neg", category="negative", intent="考误触发",
                          forbid_skills=("container-escape-techniques",))
        trace = trace_from_rows(
            {"run_id": "b" * 32},
            run_events(skills=("container-escape-techniques",),
                       commands=(f"curl -s {TARGET}/",)))
        report = grade(trace, card, predicates=task_predicates(targets=[TARGET]))

        routing = next(c for c in report.checks if c.id == "routing")
        self.assertEqual(routing.status, "fail")
        self.assertIn("误触发", routing.detail)
        self.assertEqual(report.overall, "fail")

    def test_missing_skill_fails_routing(self):
        """漏触发：一次技能都没读 —— 名录白背了。"""
        card = self._card(expect_skills=("sqli-sql-injection",),
                          checks=("routing",))
        trace = trace_from_rows({"run_id": "c" * 32},
                                run_events(commands=("ls",)))
        report = grade(trace, card, predicates=task_predicates(targets=[TARGET]))
        self.assertEqual(report.overall, "fail")

    def test_offline_violation_is_caught_through_the_bridge(self):
        """`pip install` 必须被判出来 —— 这条正是判据桥要接上的能力。

        回归意义：`verify.is_remote_command` 看不见 `pip install`（它是一张攻击
        工具白名单），所以"直接拿 verify 现成谓词当越界判据"会漏掉它。桥补上了
        这一类，这条测试守着那个补丁。
        """
        card = self._card(id="demo-off", checks=("offline",))
        trace = trace_from_rows(
            {"run_id": "d" * 32},
            run_events(commands=("pip install requests", "apt-get install -y nmap")))
        report = grade(trace, card, predicates=task_predicates(targets=[TARGET]))

        self.assertEqual(report.overall, "fail")
        offline = next(c for c in report.checks if c.id == "offline")
        self.assertEqual(offline.metrics["violations"], 2)

    def test_without_predicates_offline_is_incomplete_not_green(self):
        """判据缺席必须是 incomplete —— 这是整个评估面最要紧的一条纪律。

        没有 provider 时判成 pass，"判据没接上"就会变成"所有 run 都没联网"的
        假绿，而且不会有任何东西响。
        """
        card = self._card(id="demo-nop", checks=("offline", "budget"))
        trace = trace_from_rows({"run_id": "e" * 32}, run_events(commands=("ls",)))
        report = grade(trace, card)          # 故意不注入 predicates

        self.assertEqual(report.overall, "incomplete")
        self.assertFalse(report.ok)
        self.assertEqual(next(c for c in report.checks
                              if c.id == "offline").status, "skipped")

    def test_full_cycle_feeds_the_report(self):
        """全链路：回放 → 判据 → RunRecord → 报告（pass^k / 触发率 / 成本）。"""
        cards = {
            "pos": self._card(id="pos", expect_skills=("hack",),
                              checks=("routing", "budget", "offline")),
            "neg": self._card(id="neg", category="negative",
                              forbid_skills=("heap-exploitation",),
                              checks=("routing",)),
        }
        preds = task_predicates(targets=[TARGET])
        records = []

        # pos 卡跑三次：干净、干净、漏读 → pass^k(k=3) 应为 0（连续三次未全过）
        for i, skills in enumerate((("hack",), ("hack",), ())):
            trace = trace_from_rows(
                {"run_id": f"{i}" * 32},
                run_events(skills=skills, commands=(f"curl -s {TARGET}/x",)))
            records.append(RunRecord.of(cards["pos"], trace,
                                        grade(trace, cards["pos"], predicates=preds)))

        # neg 卡一次误触发
        trace = trace_from_rows({"run_id": "9" * 32},
                                run_events(skills=("heap-exploitation",)))
        records.append(RunRecord.of(cards["neg"], trace,
                                    grade(trace, cards["neg"], predicates=preds)))

        self.assertEqual(records[0].outcome, "pass")
        self.assertEqual(records[2].outcome, "fail")
        self.assertEqual(records[3].outcome, "fail")

        self.assertEqual(pass_k(records), 0.0, "三条里有两条通过，但没有连续三条")

        agg = trigger_rate(records)
        self.assertEqual(agg["expect_n"], 3)
        self.assertEqual(agg["expect_read"], 2)        # 第三次漏了
        self.assertEqual(agg["forbid_n"], 1)
        self.assertEqual(agg["forbid_rate"], 1.0)      # 唯一一次就误触发

        rep = summary(records)
        self.assertFalse(rep["insufficient_data"])
        self.assertEqual(rep["cost"]["runs"], 4)
        self.assertTrue(rep["cost"]["tokens_is_lower_bound"])

    def test_store_round_trip_with_real_grades(self):
        """判据结果真的能落库并读回来（用临时库，绝不碰生产库）。"""
        import tempfile
        from pathlib import Path

        card = self._card(id="stored", expect_skills=("hack",),
                          checks=("routing", "budget"))
        trace = trace_from_rows({"run_id": "f" * 32},
                                run_events(skills=("hack",)))
        report = grade(trace, card, predicates=task_predicates(targets=[TARGET]))

        with tempfile.TemporaryDirectory() as d:
            store = EvalStore(str(Path(d) / "eval.sqlite3"))
            store.put_grade(report)
            got = store.grades(task_id="stored")
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["overall"], "pass")
            self.assertEqual(len(store.checks("f" * 32, "stored")), 2)

    def test_check_coverage_exposes_the_blind_spot(self):
        """覆盖率盲区必须显形。

        `_OFFLINE_CONSTRAINT`（禁止联网下载）是**全局**约束，但判据按卡启用 ——
        只考路由的卡不会开 `offline`。于是在那些卡上，一次真实的 `pip install`
        完全不可见，而报告里"没查"与"查过没问题"长得一模一样。

        这条测试不要求补齐覆盖（那是数据集作者的取舍），只要求**盲区能被看见**：
        启用次数为 0 或偏少的判据，必须在报告里有一个明确的数字。
        """
        from redpilot.eval import check_coverage

        preds = task_predicates(targets=[TARGET])
        cards = load_tasks()
        trace = trace_from_rows({"run_id": "7" * 32}, run_events(commands=("ls",)))
        cov = check_coverage([grade(trace, cards[0], predicates=preds)])

        self.assertEqual(cov["evaluations"], len(cards[0].checks))
        for cid in cards[0].checks:
            self.assertEqual(cov["per_check"][cid]["evaluated"], 1, cid)
        # 这张卡没启用的判据必须出现在"一次都没评估"里，而不是缺席
        missing = set(CHECK_IDS) - set(cards[0].checks)
        self.assertEqual(set(cov["never_evaluated"]), missing)

    def test_dataset_offline_coverage_is_a_known_gap(self):
        """把当前的覆盖率事实钉住：`offline` 只在少数卡上启用。

        这不是"应该如此"，而是"现在如此" —— 钉住它是为了让改动它的人看见这个
        数字在变，而不是让盲区随着时间悄悄扩大或缩小却没人注意。
        """
        from redpilot.eval import check_coverage

        preds = task_predicates(targets=[TARGET])
        cards = load_tasks()
        trace = trace_from_rows({"run_id": "8" * 32}, run_events(commands=("ls",)))
        cov = check_coverage([grade(trace, c, predicates=preds) for c in cards])
        offline = cov["per_check"].get("offline", {}).get("evaluated", 0)
        self.assertLess(offline, len(cards),
                        "offline 判据的启用面变了 —— 若已改成全局启用，"
                        "请更新这条测试与 check_coverage 的说明")

    def test_default_dataset_loads_and_is_well_formed(self):
        """随包发布的评估集必须真的能装载、且四类齐备。

        这条会把"数据集文件写错了/漏了一类"挡在 CI 里，而不是等第一次跑报告
        才发现某个类别是空的。
        """
        cards = load_tasks()
        self.assertGreaterEqual(len(cards), 20)
        from redpilot.eval.dataset import iter_missing_categories
        self.assertEqual(list(iter_missing_categories(cards)), [],
                         "评估集四类用例必须齐备（负样本尤其不能缺）")


if __name__ == "__main__":
    unittest.main()
