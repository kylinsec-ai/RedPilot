"""回归测试：多段内网题必须先持续推进，再提示，最后才允许弃题。

这些测试只依赖 StopLoss 的无答案状态机，不启动靶场或 LLM。它们刻意把
``max_sessions`` 压到很小，以确保“拿到首个 flag / 达到普通干旱阈值”不会
被误当成整题完成；真正的弃题必须经过一次提示和提示后的复核窗口。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from redpilot_worker.adapter.stoploss import StopLoss


class HintRequestHelperTests(unittest.TestCase):
    """平台提示只走一次、且提示正文不进入共享止损状态。"""

    def test_hint_helper_fetches_once_and_bounds_ephemeral_text(self):
        # Import lazily so the StopLoss state-machine tests remain runnable in
        # a minimal host environment; the project test image has all driver
        # dependencies installed.
        from redpilot_worker import orchestrator as driver

        class Client:
            calls = 0

            def get_hint(self, code):
                self.calls += 1
                self.code = code
                return SimpleNamespace(hint="x" * 5000)

        client = Client()
        ok, hint = driver._request_multiflag_hint(client, "multi-01")
        self.assertTrue(ok)
        self.assertEqual(client.calls, 1)
        self.assertEqual(client.code, "multi-01")
        self.assertEqual(len(hint), 4000)

    def test_hint_review_switch_is_explicitly_configurable(self):
        from redpilot_worker import orchestrator as driver

        with patch.dict(os.environ, {"ADAPTER_MULTIFLAG_HINT_REVIEW": "0"}, clear=False):
            self.assertFalse(driver._multiflag_hint_review_enabled())
        with patch.dict(os.environ, {"ADAPTER_MULTIFLAG_HINT_REVIEW": "1"}, clear=False):
            self.assertTrue(driver._multiflag_hint_review_enabled())

    def test_resumed_post_hint_review_does_not_invent_or_refetch_hint_body(self):
        """重启后只承认已做过提示复核，不能伪称拥有或重拉提示正文。"""
        from redpilot_worker import orchestrator as driver

        note = driver._multiflag_hint_prompt_note(
            hint_reviewed=True,
            hint_text="",
            hint_review_session=None,
            session_idx=0,
            last_session_facts=0,
            last_session_repeats=0,
        )
        self.assertIn("此前已完成一次平台提示复核", note)
        self.assertIn("当前进程不可用", note)
        self.assertIn("不得猜测提示内容", note)
        self.assertNotIn("flag{", note)

    def test_driver_does_not_rearm_an_exhausted_post_hint_window(self):
        """提示后完整复核已耗尽时，入口必须返回切题而不是无限重开窗口。"""
        from redpilot_worker import orchestrator as driver

        with tempfile.TemporaryDirectory() as workdir:
            stoploss = StopLoss(
                workdir=workdir,
                dry_cutoff=1,
                post_hint_dry_cutoff=1,
                max_sessions=99,
            )
            stoploss.start("multi-resume", multi_flag=True)
            stoploss.start_session("multi-resume")
            stoploss.record_no_progress("multi-resume")
            self.assertTrue(stoploss.record_hint_requested("multi-resume"))
            stoploss.start_session("multi-resume")
            stoploss.record_no_progress("multi-resume")
            self.assertTrue(stoploss.should_stop("multi-resume")[0])

            ch = SimpleNamespace(unique_code="multi-resume", flag_count=4)
            ctrl = SimpleNamespace(workdir=workdir)
            with patch.dict(os.environ, {"ADAPTER_REVIVE_COOLDOWN": "999999"}, clear=False):
                result = driver._solve_one_unlocked(
                    object(), ch, 3600, 0,
                    solver=object(), ctrl=ctrl, verifier=object(),
                    stoploss=stoploss, stop_event=threading.Event(),
                    submitted={}, submitted_lock=threading.Lock(),
                )
            self.assertEqual(result["outcome"], "dropped")
            self.assertIn("post_hint_dry_sessions", result["reason"])


class ContinuationCheckpointTests(unittest.TestCase):
    """跨 Pi 会话只传递控制流元数据，不重复已完成入口阶段。"""

    def test_checkpoint_roundtrip_is_epoch_scoped_and_answer_free(self):
        from redpilot_worker import orchestrator as driver

        with tempfile.TemporaryDirectory() as workdir:
            driver._write_continuation_checkpoint(
                workdir,
                task_epoch="epoch-a",
                session=7,
                confirmed_count=2,
                expected_count=6,
                new_facts=0,
                repeat_count=4,
                duplicate_candidates=2,
                termination="max_turns",
                pivot_required=True,
                fact_kinds=["network", "service", "network", "credential"],
            )
            raw = Path(workdir, ".continuation.json").read_text(encoding="utf-8")
            self.assertNotIn("flag{", raw)
            self.assertNotIn("command", raw)
            state = driver._load_continuation_checkpoint(workdir, "epoch-a")
            self.assertEqual(state["session"], 7)
            self.assertEqual(state["confirmed_count"], 2)
            self.assertEqual(state["duplicate_candidates"], 2)
            self.assertEqual(state["fact_kinds"], ["network", "service", "credential"])
            self.assertTrue(state["pivot_required"])
            self.assertEqual(driver._load_continuation_checkpoint(workdir, "epoch-b"), {})

    def test_continuation_prompt_explicitly_pivots_after_duplicate_stage(self):
        from redpilot_worker import orchestrator as driver

        note = driver._continuation_prompt_note(
            {
                "session": 7,
                "new_facts": 0,
                "repeat_count": 4,
                "duplicate_candidates": 2,
                "termination": "max_turns",
                "fact_kinds": ["network", "service"],
                "pivot_required": True,
            },
            session_idx=8,
            confirmed_count=2,
            expected_count=6,
        )
        self.assertIn("连续会话续接", note)
        self.assertIn("重复已确认候选 2 次", note)
        self.assertIn("禁止原样重跑上一场入口", note)
        self.assertNotIn("flag{", note)

    def test_same_epoch_purge_keeps_checkpoint_but_new_epoch_drops_it(self):
        from redpilot_worker import orchestrator as driver

        with tempfile.TemporaryDirectory() as workdir:
            Path(workdir, ".task-epoch.json").write_text(
                json.dumps({"task_epoch": "epoch-a"}), encoding="utf-8")
            driver._write_continuation_checkpoint(
                workdir, task_epoch="epoch-a", session=1,
                confirmed_count=1, expected_count=3, new_facts=1,
                repeat_count=0, termination="completed")
            Path(workdir, "MEMORY.md").write_text("old notes", encoding="utf-8")
            driver._purge_stale_solutions(
                workdir, "multi", [], task_epoch="epoch-a", trace_scope="a" * 32)
            self.assertTrue(Path(workdir, ".continuation.json").exists())
            # Same-epoch purge removes MEMORY.md (compliance: no cross-visit
            # challenge-specific knowledge carry-over).
            self.assertFalse(Path(workdir, "MEMORY.md").exists())

            driver._purge_stale_solutions(
                workdir, "multi", [], task_epoch="epoch-b", trace_scope="b" * 32)
            self.assertFalse(Path(workdir, ".continuation.json").exists())

    def test_same_epoch_target_restart_rotates_trace_and_discards_old_evidence(self):
        """A restarted target keeps only answer-free continuation state."""
        from redpilot_worker import orchestrator as driver

        with tempfile.TemporaryDirectory() as workdir:
            original_scope = "a" * 32
            replacement_scope = "b" * 32
            Path(workdir, "_transcripts").mkdir()
            Path(workdir, "_transcripts", f"{original_scope}--r000000--s000000--boot.jsonl").write_text(
                '{"old":"evidence"}\n', encoding="utf-8")
            Path(workdir, "MEMORY.md").write_text("old target notes\n", encoding="utf-8")
            Path(workdir, ".confirmed-progress.json").write_text(
                json.dumps({"version": 1, "task_epoch": "epoch-a", "candidate_sha256": [],
                            "confirmed_count": 1, "platform_baseline": 1}),
                encoding="utf-8")
            Path(workdir, ".task-epoch.json").write_text(
                json.dumps({"task_epoch": "epoch-a"}), encoding="utf-8")
            # Pre-create _instance.json with the original target to simulate
            # a previous visit that is now being replaced by a target restart.
            Path(workdir, "_instance.json").write_text(
                json.dumps({"code": "multi", "targets": ["10.20.30.40:8080"],
                            "trace_scope": original_scope}),
                encoding="utf-8")

            driver._purge_stale_solutions(
                workdir, "multi", ["10.20.30.41:8080"],
                task_epoch="epoch-a", trace_scope=replacement_scope)

            self.assertFalse(Path(workdir, "MEMORY.md").exists())
            self.assertFalse(Path(workdir, "_transcripts").exists())
            self.assertTrue(Path(workdir, ".confirmed-progress.json").exists())
            stamp = json.loads(Path(workdir, "_instance.json").read_text(encoding="utf-8"))
            self.assertEqual(stamp["trace_scope"], replacement_scope)

    def test_session_boundary_scrubs_flag_text_from_replay_inputs(self):
        """已确认答案留在转录取证链，但不回灌到下一 Pi prompt。"""
        from redpilot_worker import orchestrator as driver

        with tempfile.TemporaryDirectory() as workdir:
            # The value is synthetic test data, not a challenge answer.
            marker = "flag{SyntheticStage_1234}"
            Path(workdir, "MEMORY.md").write_text(
                f"已完成入口，交付 {marker}\n", encoding="utf-8")
            Path(workdir, "tried_commands.md").write_text(
                f"$ printf '%s' '{marker}' >> FLAG\n", encoding="utf-8")
            scrubbed = driver._scrub_flag_plaintext(workdir, "multi")
            self.assertGreaterEqual(scrubbed, 2)
            self.assertNotIn(marker, Path(workdir, "MEMORY.md").read_text())
            self.assertNotIn(marker, Path(workdir, "tried_commands.md").read_text())
            self.assertIn("[REDACTED-FLAG]", Path(workdir, "MEMORY.md").read_text())

    def test_live_blackboard_scrub_survives_a_later_fact_save(self):
        from redpilot_worker.adapter.blackboard import Blackboard, Fact
        from redpilot_worker import orchestrator as driver

        with tempfile.TemporaryDirectory() as workdir:
            marker = "flag{SyntheticStage_5678}"
            board = Blackboard(str(Path(workdir, "_blackboard.json")))
            board.add(Fact(kind="flag", content=marker, source=f"bash: echo {marker}"))
            self.assertGreaterEqual(
                driver._scrub_live_blackboard(board, workdir, "multi"), 1)
            # A later observation causes Blackboard._save(); the old answer
            # must not reappear from the in-memory fact list.
            board.add(Fact(kind="service", content="ssh"))
            raw = Path(workdir, "_blackboard.json").read_text(encoding="utf-8")
            self.assertNotIn(marker, raw)

    def test_tried_command_ledger_redacts_and_deduplicates_flag_writes(self):
        from redpilot_worker import orchestrator as driver

        with tempfile.TemporaryDirectory() as workdir:
            marker = "flag{SyntheticStage_9012}"
            outputs = [("bash", {"command": f"printf '%s' '{marker}' >> FLAG"}, "")]
            driver._persist_tried_commands(workdir, outputs)
            text = Path(workdir, "tried_commands.md").read_text(encoding="utf-8")
            self.assertNotIn(marker, text)
            self.assertIn("[REDACTED-FLAG]", text)
            # The same command in a later session should not append a second
            # line merely because its original candidate was redacted.
            driver._persist_tried_commands(workdir, outputs)
            self.assertEqual(len(Path(workdir, "tried_commands.md").read_text().splitlines()), 1)


class MultiflagProgressionTests(unittest.TestCase):
    CODE = "multi-01"

    def _new_stoploss(self, **kwargs) -> StopLoss:
        # Keep hard limits out of the way while exercising the progression
        # state machine.  A one-session base cap is intentional: multi-flag
        # handling must not use it as an immediate “switch challenge” signal.
        return StopLoss(
            workdir=self.workdir,
            dry_cutoff=3,
            zero_flag_cutoff=3,
            max_sessions=1,
            multi_flag_max_mult=4.0,
            per_challenge_seconds=3600,
            **kwargs,
        )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workdir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _start_partial(self, stoploss: StopLoss) -> None:
        stoploss.start(self.CODE, multi_flag=True)
        stoploss.start_session(self.CODE)
        # A platform-confirmed first stage is the important boundary: all
        # following dry sessions belong to an unfinished chain.
        self.assertTrue(stoploss.record_flag(self.CODE, "flag{stage-one-real}"))

    def test_first_flag_and_session_limit_do_not_switch_multiflag_chain(self):
        """首条 flag 入账后，后续会话仍可继续，不得直接切题。"""
        stoploss = self._new_stoploss()
        self._start_partial(stoploss)

        # A new session may be started after the base cap; the multi-flag
        # multiplier is the bounded lifecycle guard, not a terminal result.
        stoploss.start_session(self.CODE)
        self.assertFalse(stoploss.should_stop(self.CODE)[0])

        # Even at the ordinary dry threshold, the chain is not terminal and a
        # hint is merely requested (once) rather than silently switching.
        for _ in range(stoploss.dry_cutoff):
            stoploss.record_no_progress(self.CODE)
        self.assertTrue(stoploss.should_request_hint(self.CODE))
        stopped, reason = stoploss.should_stop(self.CODE)
        self.assertFalse(stopped, reason)

    def test_hint_is_one_shot_and_persisted_without_hint_text(self):
        stoploss = self._new_stoploss()
        self._start_partial(stoploss)
        for _ in range(stoploss.dry_cutoff):
            stoploss.record_no_progress(self.CODE)

        self.assertTrue(stoploss.should_request_hint(self.CODE))
        self.assertTrue(stoploss.record_hint_requested(self.CODE))
        self.assertTrue(stoploss.hint_requested(self.CODE))
        self.assertFalse(stoploss.record_hint_requested(self.CODE))
        self.assertFalse(stoploss.should_request_hint(self.CODE))

        # The on-disk state is metadata only: no platform hint body or flag
        # candidate may be persisted as part of the stop-loss transition.
        state_path = Path(self.workdir) / self.CODE / ".stoploss.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["hint_requested"])
        self.assertEqual(state["post_hint_dry_sessions"], 0)
        self.assertNotIn("hint", state)

    def test_post_hint_window_must_be_exhausted_before_terminal(self):
        stoploss = self._new_stoploss(post_hint_dry_cutoff=3)
        self._start_partial(stoploss)
        for _ in range(stoploss.dry_cutoff):
            stoploss.record_no_progress(self.CODE)
        self.assertTrue(stoploss.record_hint_requested(self.CODE))

        # Hint is followed by a complete exploration window.  A partial
        # window must never cause a switch.
        for _ in range(stoploss.post_hint_dry_cutoff - 1):
            stoploss.start_session(self.CODE)
            stoploss.record_no_progress(self.CODE)
            stopped, reason = stoploss.should_stop(self.CODE)
            self.assertFalse(stopped, reason)

        stoploss.start_session(self.CODE)
        stoploss.record_no_progress(self.CODE)
        stopped, reason = stoploss.should_stop(self.CODE)
        self.assertTrue(stopped)
        self.assertIn("hint", reason)

        evidence = stoploss.abandonment_evidence(self.CODE)
        self.assertTrue(evidence["terminal"])
        self.assertTrue(evidence["hint_requested"])
        self.assertEqual(
            evidence["post_hint_dry_sessions"], stoploss.post_hint_dry_cutoff
        )
        self.assertEqual(evidence["confirmed_flags"], 1)
        self.assertGreaterEqual(evidence["sessions"], 1)

    def test_post_hint_cutoff_is_independent_from_pre_hint_dry_cutoff(self):
        """提示后的窗口必须使用自身阈值，而不是意外沿用提示前的 3 场。"""
        stoploss = self._new_stoploss(post_hint_dry_cutoff=1)
        self._start_partial(stoploss)
        for _ in range(stoploss.dry_cutoff):
            stoploss.record_no_progress(self.CODE)
        self.assertTrue(stoploss.record_hint_requested(self.CODE))

        stoploss.start_session(self.CODE)
        stoploss.record_no_progress(self.CODE)
        stopped, reason = stoploss.should_stop(self.CODE)
        self.assertTrue(stopped)
        self.assertEqual(reason, "stuck:post_hint_dry_sessions=1")

    def test_new_fact_after_hint_reopens_chain_and_clears_post_hint_dry(self):
        stoploss = self._new_stoploss(post_hint_dry_cutoff=2)
        self._start_partial(stoploss)
        for _ in range(stoploss.dry_cutoff):
            stoploss.record_no_progress(self.CODE)
        self.assertTrue(stoploss.record_hint_requested(self.CODE))

        stoploss.record_no_progress(self.CODE)
        self.assertEqual(stoploss.abandonment_evidence(self.CODE)["post_hint_dry_sessions"], 1)
        stoploss.record_fact(self.CODE)
        self.assertFalse(stoploss.should_stop(self.CODE)[0])
        evidence = stoploss.abandonment_evidence(self.CODE)
        self.assertEqual(evidence["post_hint_dry_sessions"], 0)
        self.assertEqual(evidence["dry_sessions"], 0)

    def test_new_facts_prevent_premature_hint_even_without_a_flag(self):
        """有效的新主机/服务事实不能被“尚无 flag”误判为空转。"""
        stoploss = self._new_stoploss()
        self._start_partial(stoploss)

        # Driver records a zero-flag session separately from the blackboard
        # fact count.  A real new fact must reset the hint dry window, so a
        # chain that is still discovering hosts/services does not spend its
        # one hint or get rotated early merely because no flag was submitted.
        for _ in range(stoploss.dry_cutoff + 2):
            stoploss.record_fact(self.CODE)
            stoploss.record_zero_flag(self.CODE)
            self.assertFalse(stoploss.should_request_hint(self.CODE))
            self.assertFalse(stoploss.should_stop(self.CODE)[0])


if __name__ == "__main__":
    unittest.main()
