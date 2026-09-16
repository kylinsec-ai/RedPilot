"""昨天运行态暴露出的解题链路回归测试。"""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from adapter.solver.base import extract_flags
from adapter.solver.pi_agent import _merge_partial_output, _slim_line, _stop_process_tree
from adapter.config import SolverConfig
from adapter.stoploss import StopLoss
from adapter.blackboard import Blackboard, Fact, goals_for_category
from adapter.task import AgentTask
from adapter.taskprompt import _subagent_scheduling_policy
from adapter.verify import Claim, Verifier, flag_submission_key
from adapter import observability as obs
from drivers import benchmark_driver as driver

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fastapi-console"))
from fastapi_console import agent as console_agent


class FlagSubmissionRegressionTests(unittest.TestCase):
    def test_envelope_and_submission_key_preserve_case(self):
        candidate = "FLAG{CaseSensitive_123}"
        self.assertEqual(driver.normalize_flag_envelope(candidate), candidate)
        self.assertNotEqual(flag_submission_key(candidate), flag_submission_key(candidate.lower()))

    def test_verifier_accepts_uppercase_flag_envelope(self):
        claim = Claim(flag="FLAG{CaseSensitive_123}", grounded=True, confidence=0.95)
        self.assertTrue(Verifier().verify(claim).verified)

    def test_candidates_are_deterministic_and_case_exact(self):
        candidates = driver._clean_flag_candidates(
            ["FLAG{SameBody_1}", "flag{SameBody_1}", "FLAG{SameBody_1}"])
        self.assertEqual(candidates, ["FLAG{SameBody_1}", "flag{SameBody_1}"])

    def test_delivery_queue_keeps_append_order(self):
        """A multi-stage delivery file must not turn into unordered set input."""
        with tempfile.TemporaryDirectory() as workdir:
            first = "flag{StageOne_123}"
            second = "FLAG{StageTwo_456}"
            Path(workdir, "FLAG").write_text(
                f"{first}\n{second}\n{first}\n", encoding="utf-8")
            self.assertEqual(driver._read_flag_file(workdir), [first, second])

    def test_eager_snapshot_recovers_flag_written_between_read_and_stat(self):
        """A FLAG append in the old read/stat gap must still reach verifier."""
        candidate = "flag{SnapshotRaceCandidate_123}"
        old_sig = (("FLAG", 1, 0),)
        new_sig = (("FLAG", 2, len(candidate)),)

        class PollStop:
            def __init__(self):
                self.polls = 0

            def is_set(self):
                return self.polls >= 2

            def wait(self, timeout=None):
                self.polls += 1
                return False

        class Observation:
            def context(self, **_kwargs):
                pass

            def clear_local_context(self):
                pass

        class CountingVerifier:
            def __init__(self):
                self.calls = 0

            def verify(self, claim):
                self.calls += 1
                return claim

        verifier = CountingVerifier()
        transcript_reads = [0]

        def no_evidence(*_args, **_kwargs):
            transcript_reads[0] += 1
            return []

        # First read sees the old empty queue; the first post-read stat already
        # sees the append.  The stable-snapshot helper must reread and deliver
        # the new candidate instead of caching the new signature with old data.
        with (
            patch.object(driver, "_flag_delivery_signature",
                         side_effect=[old_sig, new_sig, new_sig, new_sig]) as sig,
            patch.object(driver, "_read_flag_file",
                         side_effect=[[], [candidate]]) as read_queue,
            patch.object(driver, "_transcript_evidence_signature", return_value=()),
            patch.object(driver, "_tool_outputs_from_current_instance_transcripts",
                         side_effect=no_evidence),
            patch.object(driver, "flag_confidence", return_value=SimpleNamespace(
                flag=candidate, verified=False, reject_reason="not_grounded",
                provenance="")),
        ):
            driver._eager_submit_loop(
                stop_evt=PollStop(), client=None, code="snapshot-race",
                workdir="/tmp", task=SimpleNamespace(
                    category="", targets=(), files=(), workdir="/tmp"),
                verifier=verifier, submitted={}, submitted_lock=threading.Lock(),
                accepted_flags=[], obs=Observation(), solved_flag=[False],
                stoploss=None, trace_scope="",
            )

        self.assertEqual(sig.call_count, 4)
        self.assertEqual(read_queue.call_count, 2)
        self.assertEqual(transcript_reads[0], 1)
        self.assertEqual(verifier.calls, 1)

    def test_confirmed_delivery_lines_are_pruned_without_losing_pending_stage(self):
        """After flag #1 is banked, flag #2 remains in the hand-off queue."""
        with tempfile.TemporaryDirectory() as workdir:
            code = "multi-live"
            confirmed = "flag{ConfirmedStage_123}"
            pending = "flag{PendingStage_456}"
            Path(workdir, "FLAG").write_text(
                f"{confirmed}\n{pending}\n", encoding="utf-8")
            submitted = {code: {driver._candidate_sha256(confirmed)}}
            self.assertEqual(
                driver._prune_confirmed_delivery_candidates(
                    workdir, code, submitted, threading.Lock()), 1)
            self.assertEqual(
                Path(workdir, "FLAG").read_text(encoding="utf-8"), f"{pending}\n")

    def test_eager_pending_candidate_waits_for_new_transcript_evidence(self):
        """An unchanged FLAG must not trigger verifier work every poll tick."""
        empty = ()
        pending = {"candidate-digest"}
        # First sighting of a newly-written FLAG is always processed.
        self.assertTrue(driver._eager_needs_scan(
            ("flag", 1), None, empty, None, pending))
        # Once deferred, an unchanged queue/transcript is quiescent.
        self.assertFalse(driver._eager_needs_scan(
            ("flag", 1), ("flag", 1), empty, empty, pending))
        # A flushed tool event wakes the pending candidate immediately.
        self.assertTrue(driver._eager_needs_scan(
            ("flag", 1), ("flag", 1), (("trace", 2, 10),), empty, pending))
        # A changed FLAG queue wakes even when no transcript exists yet.
        self.assertTrue(driver._eager_needs_scan(
            ("flag", 2), ("flag", 1), empty, empty, pending))

    def test_eager_retries_transient_submit_with_static_delivery_and_evidence(self):
        """A platform blip must not require the agent to rediscover a flag.

        The queue and transcript token remain unchanged for the full test.  A
        fake monotonic clock makes the retry deadline deterministic, proving
        that intermediate polling ticks do not re-verify or re-submit early.
        """
        candidate = "flag{TransientSubmitRetry_9281}"
        clock = [0.0]

        class PollStop:
            def __init__(self):
                self.polls = 0

            def is_set(self):
                return self.polls >= 8

            def wait(self, timeout=None):
                self.polls += 1
                clock[0] += 1.0
                return False

        class Observation:
            def context(self, **_kwargs):
                pass

            def clear_local_context(self):
                pass

            def emit(self, *_args, **_kwargs):
                pass

        class Client:
            def __init__(self):
                self.call_times = []

            def submit_flag(self, _code, _candidate):
                self.call_times.append(clock[0])
                if len(self.call_times) == 1:
                    raise RuntimeError("temporary platform outage")
                return SimpleNamespace(
                    correct=True, duplicate=False, correct_flag_count=1,
                    total_flag_count=1, awarded=1, cumulative_score=1,
                )

        class Verifier:
            def verify(self, claim):
                return claim

        class Stoploss:
            def record_flag(self, *_args):
                return True

            def record_duplicate(self, *_args):
                return False

            def record_platform_progress(self, *_args):
                return True

        transcript_reads = [0]

        def static_tool_output(*_args, **_kwargs):
            transcript_reads[0] += 1
            return [("bash", "curl http://current-target", candidate)]

        with tempfile.TemporaryDirectory() as workdir, \
             patch.dict(os.environ, {
                 "ADAPTER_EAGER_SUBMIT_INTERVAL": "0.5",
                 "ADAPTER_EAGER_SUBMIT_RETRY_LIMIT": "2",
                 "ADAPTER_EAGER_SUBMIT_RETRY_SECONDS": "5",
                 "ADAPTER_EAGER_SUBMIT_RETRY_MAX_SECONDS": "5",
             }, clear=False), \
             patch.object(driver.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(driver, "_transcript_evidence_signature", return_value=()), \
             patch.object(driver, "_tool_outputs_from_current_instance_transcripts",
                          side_effect=static_tool_output), \
             patch.object(driver, "flag_confidence", return_value=SimpleNamespace(
                 flag=candidate, grounded=True, confidence=0.99, verified=True,
                 reject_reason="", provenance="remote")), \
             patch.object(driver, "_skeptic_check", return_value=""), \
             patch.object(driver, "_update_status"):
            client = Client()
            solved = [False]
            driver._eager_submit_loop(
                stop_evt=PollStop(), client=client, code="retry-static",
                workdir=workdir, task=SimpleNamespace(
                    category="", targets=(), files=(), workdir=workdir,
                    flag_count=1, correct_flag_count=0, task_epoch=""),
                verifier=Verifier(), submitted={}, submitted_lock=threading.Lock(),
                accepted_flags=[], obs=Observation(), solved_flag=solved,
                stoploss=Stoploss(), trace_scope="",
            )

        self.assertEqual(client.call_times, [1.0, 6.0])
        self.assertEqual(transcript_reads[0], 2)
        self.assertTrue(solved[0])

    def test_completed_eager_submit_skips_residual_main_submit(self):
        """A completed eager path must not submit Pi's trailing candidate again."""

        class Client:
            def __init__(self, total_flags):
                self.total_flags = total_flags
                self.submits = []

            def list_challenges(self):
                return []

            def start_challenge(self, _code):
                return SimpleNamespace(container_addr=[])

            def close_challenge(self, _code):
                return SimpleNamespace(closed=True)

            def submit_flag(self, code, candidate):
                self.submits.append((code, candidate))
                return SimpleNamespace(
                    correct=True, duplicate=False,
                    correct_flag_count=1, total_flag_count=self.total_flags,
                    awarded=1, cumulative_score=1,
                )

        class Backend:
            def __init__(self, stop_evt, candidate):
                self.stop_evt = stop_evt
                self.candidate = candidate

            def solve(self, *_args, **_kwargs):
                # End this synthetic visit after the first session.  The
                # driver still runs its normal post-session submission path.
                self.stop_evt.set()
                return SimpleNamespace(
                    flags=[self.candidate], observed_output="", tool_outputs=[],
                    duration_s=0.01, turns=1, infra_blocked=False,
                    target_fault=False, error="", handoff="",
                    termination_reason="completed",
                )

        class Verifier:
            def verify(self, claim):
                claim.verified = True
                return claim

        solver = SolverConfig(
            provider="test", base_url="", api_key="", model="",
            small_fast_model="", max_turns=1, session_seconds=60,
            reasoning=False,
        )

        def run_case(*, code, flag_count, eager_complete):
            candidate = f"flag{{SyntheticResidual_{code.replace('-', '_')}}}"
            stop_evt = threading.Event()
            client = Client(flag_count)
            challenge = SimpleNamespace(
                unique_code=code, description="synthetic regression task",
                container_addr=[], flag_count=flag_count, category="reverse",
                difficulty="easy", total_score=1, files=[], correct_flag_count=0,
            )

            def eager_loop(**kwargs):
                if eager_complete:
                    # This models the eager thread only after it has received
                    # a platform response proving all N/N flags are complete.
                    kwargs["solved_flag"][0] = True

            with tempfile.TemporaryDirectory() as workdir, \
                 patch.dict(os.environ, {"ADAPTER_CLOSE_GRACE_SECONDS": "0"}, clear=False), \
                 patch.object(driver, "_other_solver_active_on", return_value=False), \
                 patch.object(driver, "_update_status"), \
                 patch.object(driver, "write_context_md", return_value=""), \
                 patch.object(driver, "_heimdall_observe"), \
                 patch.object(driver, "_persist_session_artifacts", return_value=[]), \
                 patch.object(driver, "_purge_plaintext_artifacts"), \
                 patch.object(driver, "_close_with_retry", return_value=True), \
                 patch.object(driver, "_eager_submit_loop", side_effect=eager_loop), \
                 patch.object(driver, "create_solver", return_value=Backend(stop_evt, candidate)), \
                 patch.object(driver, "_skeptic_check", return_value=""), \
                 patch.object(driver, "_hallu", None), \
                 patch.object(driver, "flag_confidence", return_value=Claim(
                     flag=candidate, grounded=True, confidence=0.95)), \
                 patch("adapter.solver.pi_agent.cleanup_instance_processes", return_value=0):
                result = driver._solve_one_unlocked(
                    client, challenge, 120, 0, solver=solver,
                    ctrl=SimpleNamespace(workdir=workdir), verifier=Verifier(),
                    stoploss=StopLoss(workdir=workdir, per_challenge_seconds=600,
                                      max_sessions=8), stop_event=stop_evt,
                    submitted={}, submitted_lock=threading.Lock(),
                )
            return result, client.submits

        # `result.flags` still has a trailing delivery candidate, but eager
        # already completed the only required flag.  A second submit was the
        # source of post-solve "答题失败" events in the previous run.
        completed, completed_submits = run_case(
            code="eager-completed", flag_count=1, eager_complete=True)
        self.assertTrue(completed["solved"])
        self.assertEqual(completed_submits, [])

        # A partial multi-flag result must remain eligible for normal main-path
        # submission; only a proven complete eager result gets the short-circuit.
        partial, partial_submits = run_case(
            code="eager-partial", flag_count=2, eager_complete=False)
        self.assertFalse(partial["solved"])
        self.assertEqual(len(partial_submits), 1)
        self.assertEqual(partial_submits[0][0], "eager-partial")


class StopLossRegressionTests(unittest.TestCase):
    def test_session_limit_counts_actual_pi_sessions(self):
        with tempfile.TemporaryDirectory() as workdir:
            stoploss = StopLoss(max_sessions=2, workdir=workdir)
            stoploss.start("case-1")
            for _ in range(2):
                stoploss.start_session("case-1")
                self.assertEqual(stoploss.should_stop("case-1"), (False, ""))
            stoploss.start_session("case-1")
            stop, reason = stoploss.should_stop("case-1")
            self.assertTrue(stop)
            self.assertIn("sessions:3>2", reason)

    def test_multiflag_partial_progress_is_not_zero_flag_stopped(self):
        """A partial multi-flag result must keep the challenge eligible."""
        with tempfile.TemporaryDirectory() as workdir:
            stoploss = StopLoss(zero_flag_cutoff=2, workdir=workdir)
            stoploss.start("multi-1", multi_flag=True)
            stoploss.start_session("multi-1")
            stoploss.record_flag("multi-1")
            stoploss.record_zero_flag("multi-1")
            stoploss.record_zero_flag("multi-1")
            self.assertEqual(stoploss.should_stop("multi-1"), (False, ""))

    def test_duplicate_flag_does_not_inflate_stoploss_progress(self):
        with tempfile.TemporaryDirectory() as workdir:
            stoploss = StopLoss(workdir=workdir)
            stoploss.start("multi-1", multi_flag=True)
            stoploss.start_session("multi-1")
            self.assertTrue(stoploss.record_flag("multi-1", "flag{Unique_123}"))
            self.assertFalse(stoploss.record_flag("multi-1", "flag{Unique_123}"))
            self.assertEqual(stoploss.flags_banked("multi-1"), 1)

    def test_platform_duplicate_resets_stall_without_counting_new_flag(self):
        with tempfile.TemporaryDirectory() as workdir:
            stoploss = StopLoss(zero_flag_cutoff=1, workdir=workdir)
            stoploss.start("multi-1", multi_flag=True)
            stoploss.start_session("multi-1")
            stoploss.record_zero_flag("multi-1")
            self.assertTrue(stoploss.record_duplicate("multi-1", "flag{Other_456}"))
            self.assertFalse(stoploss.should_stop("multi-1")[0])
            self.assertEqual(stoploss.flags_banked("multi-1"), 0)

    def test_late_correct_response_cannot_overshoot_platform_progress(self):
        """A delayed response must not turn a known 3/N into 4/N."""
        with tempfile.TemporaryDirectory() as workdir:
            stoploss = StopLoss(workdir=workdir)
            stoploss.start("multi-1", multi_flag=True)
            stoploss.record_platform_progress("multi-1", 3)
            self.assertTrue(stoploss.record_flag("multi-1", "opaque-candidate"))
            self.assertEqual(stoploss._read_state("multi-1").confirmed_flags, 3)

    def test_default_zero_flag_cutoff_is_three_sessions(self):
        with tempfile.TemporaryDirectory() as workdir:
            stoploss = StopLoss(workdir=workdir)
            stoploss.start("case-1")
            for _ in range(3):
                stoploss.start_session("case-1")
                stoploss.record_zero_flag("case-1")
            stopped, reason = stoploss.should_stop("case-1")
            self.assertTrue(stopped)
            self.assertIn("zero_flag_sessions:3>=3", reason)


class SchedulerRegressionTests(unittest.TestCase):
    def test_observability_visit_context_is_thread_local(self):
        """Concurrent visits must not overwrite each other's event metadata."""
        with tempfile.TemporaryDirectory() as workdir:
            path = Path(workdir) / "events.jsonl"
            obs.configure(str(path), run_id="test")
            obs.context(worker_id=1, boot_id="boot")
            barrier = threading.Barrier(2)

            def emit_for(code: str) -> None:
                obs.context(challenge_id=code, attempt_id=code)
                barrier.wait(timeout=2)
                obs.emit("probe", payload={"code": code})

            threads = [threading.Thread(target=emit_for, args=(code,))
                       for code in ("case-a", "case-b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            obs.close()
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual({row.get("challenge_id") for row in rows},
                             {"case-a", "case-b"})
            self.assertEqual({row.get("attempt_id") for row in rows},
                             {"case-a", "case-b"})
            self.assertEqual({row["payload"]["code"] for row in rows},
                             {"case-a", "case-b"})

    def test_asymmetric_capabilities_disable_second_worker_shard(self):
        """Capability owners must not lose half their own category pool.

        ``_capability_filter`` has already removed categories owned by another
        worker.  A subsequent global worker-id shard would therefore discard
        every hash bucket not belonging to the current worker, with no peer
        able to pick those challenges up.
        """
        with patch.dict(os.environ, {
            "ADAPTER_CAPABILITIES": "web,cloud",
            "ADAPTER_WORKER_COUNT": "3",
            "ADAPTER_WORKER_ID": "1",
            "ADAPTER_CAPABILITY_SHARD": "",
        }, clear=False):
            self.assertFalse(driver._capability_sharding_enabled())

    def test_homogeneous_capability_pool_keeps_deterministic_shard(self):
        known = ",".join(sorted(driver._KNOWN_CATEGORIES - {"unknown"}))
        with patch.dict(os.environ, {
            "ADAPTER_CAPABILITIES": known,
            "ADAPTER_WORKER_COUNT": "3",
            "ADAPTER_WORKER_ID": "1",
            "ADAPTER_CAPABILITY_SHARD": "",
        }, clear=False):
            self.assertTrue(driver._capability_sharding_enabled())

    def test_adaptive_session_limits_by_difficulty(self):
        solver = SimpleNamespace(max_turns=60, session_seconds=2400)
        with patch.dict(os.environ, {"ADAPTER_ADAPTIVE_SESSION": "1"}, clear=False):
            easy = driver._adaptive_session_limits(
                SimpleNamespace(difficulty="easy", flag_count=1), solver, 0, 0, 0)
            medium = driver._adaptive_session_limits(
                SimpleNamespace(difficulty="medium", flag_count=1), solver, 0, 0, 0)
            hard = driver._adaptive_session_limits(
                SimpleNamespace(difficulty="hard", flag_count=1), solver, 0, 0, 0)
            multi = driver._adaptive_session_limits(
                SimpleNamespace(difficulty="easy", flag_count=2), solver, 0, 0, 0)
        self.assertEqual(easy[:2], (36, 900))
        self.assertEqual(medium[:2], (48, 1200))
        self.assertEqual(hard[:2], (60, 2400))
        self.assertEqual(multi[:2], (60, 2400))

    def test_multiflag_turn_limit_extends_only_the_chain_session(self):
        with patch.dict(os.environ, {"ADAPTER_MULTIFLAG_MAX_TURNS": "120"}, clear=False):
            self.assertEqual(
                driver._multiflag_session_turn_limit(
                    SimpleNamespace(max_turns=60)), 120)
            self.assertEqual(
                driver._multiflag_session_turn_limit(
                    SimpleNamespace(max_turns=180)), 180)

    def test_subagent_policy_scales_with_difficulty(self):
        easy = _subagent_scheduling_policy(
            AgentTask(objective="x", difficulty="easy", flag_count=1), 0)
        hard = _subagent_scheduling_policy(
            AgentTask(objective="x", difficulty="hard", flag_count=1), 0)
        self.assertIn("不要调用 subagent", easy)
        self.assertIn("最多并行派 2 个", hard)

    def test_efficiency_policy_allows_limited_recheck_then_pivots(self):
        task = AgentTask(objective="x", difficulty="medium", flag_count=1,
                         targets=["127.0.0.1:1"], workdir="/tmp")
        prompt = driver.build_task_prompt(task, None)
        self.assertIn("最多做 2 次验证/参数变体", prompt)
        self.assertIn("连续 2 次验证没有新增事实", prompt)

    def test_prompt_requires_challenge_preflight_before_tools(self):
        task = AgentTask(objective="review", difficulty="easy", flag_count=1,
                         targets=["127.0.0.1:1"], workdir="/tmp")
        prompt = driver.build_task_prompt(task, None)
        self.assertIn("题目信息预读", prompt)
        self.assertIn("禁止调用 bash", prompt)
        self.assertIn("目标/入口地址", prompt)

    def test_multiflag_prompt_does_not_offer_proxychains_or_overwrite_flags(self):
        task = AgentTask(objective="内网多阶段渗透", difficulty="medium", flag_count=3,
                         targets=["10.0.0.1:80"])
        prompt = driver.build_task_prompt(task, None)
        self.assertIn("不要使用环境不兼容的 `proxychains4`", prompt)
        self.assertNotIn("proxychains4 nmap", prompt)
        self.assertIn("禁止用 `>` 覆盖已经找到的 flag", prompt)

    def test_multiflag_prompt_explains_confirmed_queue_cleanup(self):
        task = AgentTask(objective="内网多阶段渗透", difficulty="medium", flag_count=3,
                         targets=["10.0.0.1:80"])
        prompt = driver.build_task_prompt(task, None, flags_submitted=1)
        self.assertIn("已确认的 flag 可能已从 FLAG 投递队列自动移除", prompt)
        self.assertIn("立即沿当前内网链继续寻找下一条", prompt)
        self.assertIn("'flag{...}' >> FLAG", prompt)

    def test_blackboard_goal_moves_past_recon_after_observation(self):
        board = Blackboard()
        board.seed_goals(goals_for_category("pentest"))
        board.add(Fact(kind="network", content="10.0.0.1:80"))
        board.add(Fact(kind="service", content="http"))
        self.assertNotEqual(board.next_open_goal().id, "recon")

    def test_submission_complete_uses_task_count_when_backend_counts_missing(self):
        task = AgentTask(objective="multi", flag_count=3)
        task.correct_flag_count = 2
        self.assertFalse(driver._submission_complete(
            task, SimpleNamespace(correct=True, correct_flag_count=0,
                                  total_flag_count=0), accepted_count=0))
        self.assertTrue(driver._submission_complete(
            task, SimpleNamespace(correct=True, correct_flag_count=0,
                                  total_flag_count=0), accepted_count=1))

    def test_duplicate_is_terminal_only_after_durable_multiflag_count_reaches_total(self):
        """A duplicate can finish a multi-flag task, but never by itself."""
        task = AgentTask(objective="multi", flag_count=3)
        duplicate = SimpleNamespace(correct=False, duplicate=True,
                                    correct_flag_count=0, total_flag_count=0)
        self.assertFalse(driver._submission_complete(task, duplicate, confirmed_count=2))
        self.assertTrue(driver._submission_complete(task, duplicate, confirmed_count=3))

    def test_confirmed_progress_manifest_is_used_after_backend_count_lag(self):
        """Prompt progress must survive a restart/list response that reports zero."""
        with tempfile.TemporaryDirectory() as workdir, \
                patch.object(driver, "_TASK_EPOCH", "epoch-regression"):
            task = AgentTask(objective="multi", flag_count=3, workdir=workdir)
            task.task_epoch = "epoch-regression"
            task.correct_flag_count = 0
            self.assertEqual(
                driver._initialize_confirmed_progress(
                    workdir, task, {"a" * 64, "b" * 64}), 2)
            # Simulate a stale platform list after a worker restart.
            task.correct_flag_count = 0
            self.assertEqual(driver._confirmed_progress_count(workdir, task), 2)

    def test_countless_corrects_extend_partial_platform_baseline_to_completion(self):
        """A count-less backend must still advance an already-partial N-of-M task."""
        with tempfile.TemporaryDirectory() as workdir, \
                patch.object(driver, "_TASK_EPOCH", "epoch-regression"):
            task = AgentTask(objective="multi", flag_count=4, workdir=workdir)
            task.task_epoch = "epoch-regression"
            # The platform says one stage was already accepted before this
            # worker starts, then returns no cumulative counts for new flags.
            task.correct_flag_count = 1
            self.assertEqual(driver._initialize_confirmed_progress(workdir, task), 1)
            response = SimpleNamespace(
                correct=True, duplicate=False,
                correct_flag_count=0, total_flag_count=0,
            )
            counts = []
            for index in range(2, 5):
                count = driver._record_confirmed_submission(
                    workdir, task, f"flag{{SyntheticStage{index}_9281}}", response)
                task.correct_flag_count = max(task.correct_flag_count, count)
                counts.append(count)

            self.assertEqual(counts, [2, 3, 4])
            self.assertTrue(driver._submission_complete(task, response, counts[-1]))

    def test_reordered_correct_responses_cannot_overcount_manifest(self):
        """A late lower count cannot increment a newer platform total again."""
        with tempfile.TemporaryDirectory() as workdir, \
                patch.object(driver, "_TASK_EPOCH", "epoch-regression"):
            task = AgentTask(objective="multi", flag_count=3, workdir=workdir)
            task.task_epoch = "epoch-regression"
            task.correct_flag_count = 1
            newer = SimpleNamespace(correct=True, duplicate=False,
                                    correct_flag_count=3, total_flag_count=3)
            older = SimpleNamespace(correct=True, duplicate=False,
                                    correct_flag_count=2, total_flag_count=3)
            self.assertEqual(
                driver._record_confirmed_submission(workdir, task, "opaque-b", newer), 3)
            self.assertEqual(
                driver._record_confirmed_submission(workdir, task, "opaque-a", older), 3)

    def test_local_receipt_restores_restart_dedupe_without_shared_events(self):
        """A count-less multi-flag response survives a restart in its own directory."""
        with tempfile.TemporaryDirectory() as root, \
                patch.object(driver, "_TASK_EPOCH", "epoch-regression"):
            workdir = Path(root) / "case-a"
            workdir.mkdir()
            # Match the marker written by _purge_stale_solutions so the next
            # same-epoch visit preserves only the opaque receipt.
            (workdir / ".task-epoch.json").write_text(
                json.dumps({"task_epoch": "epoch-regression"}), encoding="utf-8")
            task = AgentTask(objective="multi", flag_count=3, workdir=str(workdir))
            task.task_epoch = "epoch-regression"
            candidate = "FLAG{CurrentOnly_123}"
            response = SimpleNamespace(correct=True, duplicate=False,
                                       correct_flag_count=0, total_flag_count=0)
            self.assertEqual(
                driver._record_confirmed_submission(str(workdir), task, candidate, response), 1)
            receipt = workdir / ".confirmed-progress.json"
            self.assertTrue(receipt.exists())
            self.assertNotIn(candidate, receipt.read_text(encoding="utf-8"))

            # Simulate a later process seeing a stale platform count after the
            # normal per-instance cleanup.  A malformed shared event file must
            # be irrelevant: recovery reads only this challenge's receipt.
            (Path(root) / "_events.jsonl").write_text("not json\n", encoding="utf-8")
            driver._purge_stale_solutions(
                str(workdir), "case-a", [], task_epoch="epoch-regression")
            restarted = AgentTask(objective="multi", flag_count=3, workdir=str(workdir))
            restarted.task_epoch = "epoch-regression"
            restarted.correct_flag_count = 0
            submitted = {}
            restored = driver._hydrate_submitted_from_confirmed_progress(
                submitted, threading.Lock(), "case-a", str(workdir), restarted)
            self.assertEqual(restored, 1)
            self.assertTrue(driver._submitted_contains(submitted, "case-a", candidate))
            self.assertEqual(set(submitted), {"case-a"})

    def test_local_receipt_is_ignored_when_task_epoch_changes(self):
        with tempfile.TemporaryDirectory() as workdir, \
                patch.object(driver, "_TASK_EPOCH", "old-epoch"):
            old = AgentTask(objective="multi", flag_count=2, workdir=workdir)
            old.task_epoch = "old-epoch"
            response = SimpleNamespace(correct=True, duplicate=False,
                                       correct_flag_count=0, total_flag_count=0)
            driver._record_confirmed_submission(workdir, old, "flag{OldOnly_123}", response)

            fresh = AgentTask(objective="multi", flag_count=2, workdir=workdir)
            fresh.task_epoch = "new-epoch"
            submitted = {}
            self.assertEqual(
                driver._hydrate_submitted_from_confirmed_progress(
                    submitted, threading.Lock(), "same-code", workdir, fresh), 0)
            self.assertEqual(submitted, {})

    def test_close_retries_an_active_false_acknowledgement(self):
        class Client:
            def __init__(self):
                self.close_calls = 0

            def close_challenge(self, _code):
                self.close_calls += 1
                return SimpleNamespace(closed=self.close_calls >= 2)

            def list_challenges(self):
                return [SimpleNamespace(unique_code="multi", container_status="available")]

        client = Client()
        with patch.object(driver.time, "sleep"):
            self.assertTrue(driver._close_with_retry(client, "multi", retries=3))
        self.assertEqual(client.close_calls, 2)

    def test_close_accepts_false_acknowledgement_when_platform_is_stopped(self):
        class Client:
            def __init__(self):
                self.close_calls = 0

            def close_challenge(self, _code):
                self.close_calls += 1
                return SimpleNamespace(closed=False)

            def list_challenges(self):
                return [SimpleNamespace(unique_code="multi", container_status="stopped")]

        client = Client()
        self.assertTrue(driver._close_with_retry(client, "multi", retries=3))
        self.assertEqual(client.close_calls, 1)

    def test_api_fault_aborts_unstarted_round_queue(self):
        challenges = [
            SimpleNamespace(unique_code=code, is_completed=False, difficulty="easy",
                            flag_count=1, total_score=1)
            for code in ("first", "second", "third")
        ]

        class Client:
            def list_challenges(self):
                return challenges

        with tempfile.TemporaryDirectory() as workdir:
            ctrl = SimpleNamespace(
                workdir=workdir, total_seconds=120, round_factors=[1.0],
                timebox_easy=60, timebox_medium=60, timebox_hard=60,
                max_concurrency=1, best_of=1,
                timebox_for_difficulty=lambda _difficulty: 60,
            )
            calls = []

            def api_failure(_client, challenge, *_args, **_kwargs):
                calls.append(challenge.unique_code)
                return {"solved": False, "outcome": "api_fault", "turns": 0,
                        "api_error": True}

            with patch.object(driver, "solve_one", side_effect=api_failure), \
                    patch.object(driver, "_worker_concurrency", return_value=1):
                solved, dropped, api_fault, lifecycle_defer = driver.schedule_rounds(
                    challenges, Client(), solver=object(), ctrl=ctrl,
                    verifier=object(), stoploss=StopLoss(workdir=workdir),
                    stop_event=threading.Event(),
                )
            self.assertEqual(calls, ["first"])
            self.assertEqual(solved, set())
            self.assertEqual(dropped, set())
            self.assertTrue(api_fault)
            self.assertFalse(lifecycle_defer)

    def test_infra_outcome_drops_once_and_aborts_remaining_queue(self):
        challenges = [
            SimpleNamespace(unique_code=code, is_completed=False, difficulty="easy",
                            flag_count=1, total_score=1)
            for code in ("first", "second")
        ]

        class Client:
            def list_challenges(self):
                return challenges

        with tempfile.TemporaryDirectory() as workdir:
            ctrl = SimpleNamespace(
                workdir=workdir, total_seconds=120, round_factors=[1.0],
                timebox_easy=60, timebox_medium=60, timebox_hard=60,
                max_concurrency=1, best_of=1,
                timebox_for_difficulty=lambda _difficulty: 60,
            )
            calls = []

            def infra_failure(_client, challenge, *_args, **_kwargs):
                calls.append(challenge.unique_code)
                return {"solved": False, "outcome": "infra_blocked", "turns": 0}

            with patch.object(driver, "solve_one", side_effect=infra_failure), \
                    patch.object(driver, "_worker_concurrency", return_value=1):
                solved, dropped, api_fault, lifecycle_defer = driver.schedule_rounds(
                    challenges, Client(), solver=object(), ctrl=ctrl,
                    verifier=object(), stoploss=StopLoss(workdir=workdir),
                    stop_event=threading.Event(),
                )
            self.assertEqual(calls, ["first"])
            self.assertEqual(solved, set())
            self.assertEqual(dropped, {"first"})
            self.assertFalse(api_fault)
            self.assertTrue(lifecycle_defer)

    def test_start_failure_aborts_unstarted_round_queue_for_dispatch_backoff(self):
        challenges = [
            SimpleNamespace(unique_code=code, is_completed=False, difficulty="easy",
                            flag_count=1, total_score=1)
            for code in ("first", "second")
        ]

        class Client:
            def list_challenges(self):
                return challenges

        with tempfile.TemporaryDirectory() as workdir:
            ctrl = SimpleNamespace(
                workdir=workdir, total_seconds=120, round_factors=[1.0],
                timebox_easy=60, timebox_medium=60, timebox_hard=60,
                max_concurrency=1, best_of=1,
                timebox_for_difficulty=lambda _difficulty: 60,
            )
            calls = []

            def start_failure(_client, challenge, *_args, **_kwargs):
                calls.append(challenge.unique_code)
                return {"solved": False, "outcome": "start_failed", "turns": 0}

            with patch.object(driver, "solve_one", side_effect=start_failure), \
                    patch.object(driver, "_worker_concurrency", return_value=1):
                solved, dropped, api_fault, lifecycle_defer = driver.schedule_rounds(
                    challenges, Client(), solver=object(), ctrl=ctrl,
                    verifier=object(), stoploss=StopLoss(workdir=workdir),
                    stop_event=threading.Event(),
                )
            self.assertEqual(calls, ["first"])
            self.assertEqual(solved, set())
            self.assertEqual(dropped, {"first"})
            self.assertFalse(api_fault)
            self.assertTrue(lifecycle_defer)

    def test_auto_dispatch_waits_before_rebuilding_queue_after_lifecycle_abort(self):
        challenges = [
            SimpleNamespace(unique_code=code, is_completed=False, difficulty="easy",
                            flag_count=1, total_score=1)
            for code in ("first", "second")
        ]

        class Client:
            def list_challenges(self):
                return challenges

        with tempfile.TemporaryDirectory() as workdir:
            ctrl = SimpleNamespace(workdir=workdir, total_seconds=120)
            stop = threading.Event()
            ticks = []

            def idle_tick(duration, reason):
                ticks.append((duration, reason))
                stop.set()

            with patch.dict(os.environ, {
                    "ADAPTER_WORKER_COUNT": "1",
                    "ADAPTER_WORKER_ID": "0",
                    "ADAPTER_LIFECYCLE_BACKOFF": "7",
                    "ADAPTER_DROP_RETRY": "100",
                    "ADAPTER_AUTO_MAX_RETRY": "3",
                }, clear=False), \
                    patch.object(driver, "schedule_rounds",
                                 return_value=(set(), {"first"}, False, True)) as rounds, \
                    patch.object(driver, "_idle_tick", side_effect=idle_tick):
                driver.auto_dispatch_loop(
                    Client(), seed=challenges, ctrl=ctrl, solver=object(),
                    verifier=object(), stoploss=StopLoss(workdir=workdir),
                    stop_event=stop, only="", caps=set(),
                )

            self.assertEqual(rounds.call_count, 1)
            self.assertEqual(ticks, [(7, "transient lifecycle failure backoff")])
            saved = json.loads(Path(driver._retry_state_path(workdir, 0)).read_text())
            self.assertEqual(saved["attempts"], {"first": 1})
            self.assertNotIn("second", saved["attempts"])

    def test_solver_only_fallback_shard_covers_all_codes_without_monitor_overlap(self):
        # Deliberately pick codes in one CRC bucket: every solver must make the
        # same sorted fallback decision, otherwise an empty peer overlaps it.
        codes = [f"fallback-{i}" for i in range(200)
                 if zlib.crc32(f"fallback-{i}".encode()) % 2 == 0][:5]
        self.assertGreaterEqual(len(codes), 3)
        challenges = [SimpleNamespace(unique_code=code) for code in codes]
        assignments = {}
        for wid in (0, 1, 2):
            with patch.dict(os.environ, {
                "ADAPTER_WORKER_COUNT": "3",
                "ADAPTER_WORKER_ID": str(wid),
            }, clear=False):
                assignments[wid] = {row.unique_code for row in driver._solver_shard(challenges)}
        self.assertEqual(assignments[0], set())
        self.assertFalse(assignments[1] & assignments[2])
        self.assertEqual(assignments[1] | assignments[2], set(codes))

    def test_console_priority_never_assigns_monitor_worker(self):
        with tempfile.TemporaryDirectory() as workdir:
            priority = Path(workdir) / "priority.txt"
            with patch.object(console_agent, "PRIORITY_FILE", priority):
                self.assertEqual(console_agent._next_worker(), 1)
                priority.write_text("one|1\n", encoding="utf-8")
                self.assertEqual(console_agent._next_worker(), 2)
                priority.write_text("one|1\ntwo|2\n", encoding="utf-8")
                self.assertEqual(console_agent._next_worker(), 1)

    def test_console_priority_epoch_records_are_balanced_and_atomic_format(self):
        """The third epoch column must not make worker load parsing fail."""
        with tempfile.TemporaryDirectory() as workdir:
            priority = Path(workdir) / "priority.txt"
            with patch.object(console_agent, "PRIORITY_FILE", priority), \
                 patch.object(console_agent, "_priority_task_epoch", return_value="t2-current"), \
                 patch.object(console_agent, "load_agent_env", return_value={
                     "BENCHMARK_TOKEN": "token", "BENCHMARK_BASE_URL": "http://bench"}), \
                 patch.object(console_agent, "fleet_status", return_value={"summary": {"running": 2}}), \
                 patch.object(console_agent, "_platform_challenge", return_value={
                     "is_completed": False, "container_status": "idle"}):
                first = console_agent.solve_one("Case-One")
                second = console_agent.solve_one("case-two")

            self.assertEqual(first["container"], "tsecbench-worker-2")
            self.assertEqual(second["container"], "tsecbench-worker-3")
            self.assertEqual(priority.read_text(encoding="utf-8").splitlines(), [
                "Case-One|1|t2-current",
                "case-two|2|t2-current",
            ])

    def test_completed_priority_is_purged_and_not_reported_as_queued(self):
        with tempfile.TemporaryDirectory() as workdir:
            priority = Path(workdir) / "priority.txt"
            priority.write_text(
                "done|1|t2-current\nACTIVE|1|t2-current\nold|2|t1-old\n",
                encoding="utf-8")
            with patch.object(driver, "_TASK_EPOCH", "t2-current"), \
                 patch.object(driver, "_last_priority_signature", None), \
                 patch.dict(os.environ, {"ADAPTER_WORKDIR": workdir}, clear=False):
                driver._purge_stale_priority({"active"})
                self.assertEqual(priority.read_text(encoding="utf-8"), "ACTIVE|1|t2-current\n")
                driver._purge_stale_priority(set())
                self.assertEqual(priority.read_text(encoding="utf-8"), "")

            priority.write_text("stale|1|t1-old\ncurrent|2|t2-current\n",
                                encoding="utf-8")
            with patch.object(console_agent, "PRIORITY_FILE", priority), \
                 patch.object(console_agent, "_priority_task_epoch", return_value="t2-current"), \
                 patch.object(console_agent, "load_agent_env", return_value={"BENCHMARK_TOKEN": "token"}), \
                 patch.object(console_agent, "_platform_challenge", return_value={"is_completed": False}):
                self.assertFalse(console_agent.single_status("stale")["queued"])
                self.assertTrue(console_agent.single_status("current")["queued"])

            priority.write_text("done|1|t2-current\n", encoding="utf-8")
            with patch.object(console_agent, "PRIORITY_FILE", priority), \
                 patch.object(console_agent, "_priority_task_epoch", return_value="t2-current"), \
                 patch.object(console_agent, "load_agent_env", return_value={"BENCHMARK_TOKEN": "token"}), \
                 patch.object(console_agent, "_platform_challenge", return_value={"is_completed": True}):
                self.assertFalse(console_agent.single_status("done")["queued"])

    def test_event_aggregation_keeps_worker_sessions_separate(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            (root / "work").mkdir()
            events = [
                {"ts": 1, "event": "session_start", "worker_id": 1,
                 "boot_id": "b1", "payload": {"code": "multi"}},
                {"ts": 2, "event": "flag_submit", "worker_id": 1,
                 "boot_id": "b1", "payload": {"code": "multi", "correct": True,
                 "awarded": 100, "flag": "flag{redacted}",
                 "expected_flag_count": 3, "correct_flag_count": 1,
                 "total_flag_count": 3}},
                {"ts": 3, "event": "session_start", "worker_id": 2,
                 "boot_id": "b2", "payload": {"code": "multi"}},
                {"ts": 4, "event": "session_end", "worker_id": 1,
                 "boot_id": "b1", "payload": {"code": "multi"}},
                {"ts": 5, "event": "challenge_solved", "worker_id": 2,
                 "boot_id": "b2", "payload": {"code": "other", "flags": 1}},
            ]
            (root / "work" / "_events.jsonl").write_text(
                "\n".join(json.dumps(item) for item in events), encoding="utf-8")
            with patch.object(console_agent, "PROJECT_ROOT", root):
                out = console_agent._aggregate_events()
            self.assertEqual(out["summary"]["flags_submitted"], 1)
            self.assertEqual(out["summary"]["solved"], 1)
            self.assertEqual(len(out["current"]), 1)
            self.assertEqual(out["current"][0]["worker_id"], "2")

    def test_event_aggregation_marks_multiflag_only_after_all_counts(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            (root / "work").mkdir()
            events = [
                {"ts": 1, "event": "flag_submit", "worker_id": 1,
                 "boot_id": "b1", "payload": {"code": "multi", "correct": True,
                 "awarded": 100, "expected_flag_count": 3,
                 "correct_flag_count": 1, "total_flag_count": 3}},
                {"ts": 2, "event": "flag_submit", "worker_id": 1,
                 "boot_id": "b1", "payload": {"code": "multi", "correct": True,
                 "awarded": 100, "expected_flag_count": 3,
                 "correct_flag_count": 3, "total_flag_count": 3}},
            ]
            (root / "work" / "_events.jsonl").write_text(
                "\n".join(json.dumps(item) for item in events), encoding="utf-8")
            with patch.object(console_agent, "PROJECT_ROOT", root):
                out = console_agent._aggregate_events()
            self.assertEqual(out["summary"]["solved"], 1)

    def test_event_aggregation_clears_legacy_active_sessions_on_new_boot(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            (root / "work").mkdir()
            events = [
                {"ts": 1, "event": "session_start", "payload": {"code": "old"}},
                {"ts": 2, "event": "run_start", "worker_id": 1,
                 "boot_id": "new", "payload": {}},
            ]
            (root / "work" / "_events.jsonl").write_text(
                "\n".join(json.dumps(item) for item in events), encoding="utf-8")
            with patch.object(console_agent, "PROJECT_ROOT", root):
                out = console_agent._aggregate_events()
            self.assertEqual(out["current"], [])

    def test_event_aggregation_matches_synthetic_end_to_target_boot(self):
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            (root / "work").mkdir()
            events = [
                {"ts": 1, "event": "session_start", "worker_id": 1,
                 "boot_id": "old", "payload": {"code": "case"}},
                {"ts": 2, "event": "session_end", "worker_id": 2,
                 "boot_id": "new", "payload": {"code": "case", "synthetic": True,
                 "worker_id": 1, "boot_id": "old"}},
            ]
            (root / "work" / "_events.jsonl").write_text(
                "\n".join(json.dumps(item) for item in events), encoding="utf-8")
            with patch.object(console_agent, "PROJECT_ROOT", root):
                out = console_agent._aggregate_events()
            self.assertEqual(out["current"], [])

    def test_empty_shard_is_a_valid_assignment(self):
        code = next(f"case-{i}" for i in range(1000)
                    if zlib.crc32(f"case-{i}".encode()) % 2 == 0)
        challenge = SimpleNamespace(unique_code=code)
        with patch.dict(os.environ, {"ADAPTER_WORKER_COUNT": "3", "ADAPTER_WORKER_ID": "2"}, clear=False):
            self.assertEqual(driver._solver_shard([challenge]), [])

    def test_task_fingerprint_changes_only_for_material_task_change(self):
        one = SimpleNamespace(unique_code="case-1", is_completed=False,
                              flag_count=1, difficulty="easy", total_score=100)
        same = SimpleNamespace(unique_code="case-1", is_completed=False,
                               flag_count=1, difficulty="easy", total_score=100)
        changed = SimpleNamespace(unique_code="case-1", is_completed=True,
                                  flag_count=1, difficulty="easy", total_score=100)
        self.assertEqual(driver._challenge_fingerprint([one]),
                         driver._challenge_fingerprint([same]))
        self.assertNotEqual(driver._challenge_fingerprint([one]),
                            driver._challenge_fingerprint([changed]))

    def test_task_epoch_identity_rotates_when_benchmark_token_changes(self):
        """Identical catalogues under a new token must not reuse local state."""
        challenge = SimpleNamespace(unique_code="case-1", description="objective",
                                    category="web", tags=[], difficulty="easy",
                                    level=1, total_score=100, flag_count=1)
        with patch.dict(os.environ, {"BENCHMARK_TOKEN": "token-a"}, clear=False):
            first = driver._challenge_stable_identity([challenge])
        with patch.dict(os.environ, {"BENCHMARK_TOKEN": "token-b"}, clear=False):
            second = driver._challenge_stable_identity([challenge])
        self.assertNotEqual(first, second)

    def test_legacy_monitor_priority_is_claimed_by_exactly_one_solver(self):
        code = "queued-from-monitor"
        with tempfile.TemporaryDirectory() as workdir:
            with open(os.path.join(workdir, "priority.txt"), "w", encoding="utf-8") as f:
                f.write(f"{code}|0\n")
            assigned = 1 + (zlib.crc32(code.encode()) % 2)
            env = {"ADAPTER_WORKER_COUNT": "3", "ADAPTER_WORKER_ID": str(assigned)}
            with patch.dict(os.environ, env, clear=False):
                self.assertEqual(driver._load_priority(workdir, assigned), [code])
            other = 1 if assigned == 2 else 2
            with patch.dict(os.environ, {"ADAPTER_WORKER_COUNT": "3", "ADAPTER_WORKER_ID": str(other)}, clear=False):
                self.assertEqual(driver._load_priority(workdir, other), [])

    def test_task_epoch_ignores_in_task_completion_but_rotates_after_terminal(self):
        """Completion changes must not reset cooldown/progress as a new task."""
        row = dict(unique_code="epoch-case", description="dynamic test objective",
                   category="web", tags=[], difficulty="easy", level=1,
                   total_score=100, flag_count=1, is_completed=False,
                   correct_flag_count=0)
        with tempfile.TemporaryDirectory() as workdir:
            with patch.object(driver, "_TASK_EPOCH", ""):
                first = driver._activate_task_epoch([SimpleNamespace(**row)], workdir=workdir)
                completed = dict(row, is_completed=True, correct_flag_count=1)
                same_task = driver._activate_task_epoch(
                    [SimpleNamespace(**completed)], force_new=True, workdir=workdir)
                self.assertEqual(first, same_task)
                driver._mark_task_epoch_terminal(workdir)
                next_task = driver._activate_task_epoch(
                    [SimpleNamespace(**row)], force_new=True, workdir=workdir)
                self.assertNotEqual(first, next_task)

    def test_retry_state_is_scoped_to_task_epoch(self):
        with tempfile.TemporaryDirectory() as workdir:
            driver._save_retry_state(workdir, 1, {"same-code": float("inf")},
                                     {"same-code": 3}, task_epoch="old-task")
            retry_at, attempts = driver._load_retry_state(
                workdir, 1, task_epoch="new-task")
            self.assertEqual(retry_at, {})
            self.assertEqual(attempts, {})

    def test_priority_entries_are_scoped_to_task_epoch(self):
        with tempfile.TemporaryDirectory() as workdir:
            Path(workdir, "priority.txt").write_text(
                "old|1|t1-old\ncurrent|1|t2-current\nlegacy|1\n",
                encoding="utf-8")
            with patch.object(driver, "_TASK_EPOCH", "t2-current"), \
                 patch.dict(os.environ, {"ADAPTER_WORKER_COUNT": "3"}, clear=False):
                self.assertEqual(driver._load_priority(workdir, 1), ["current"])

    def test_challenge_lease_excludes_parallel_lifecycle(self):
        with tempfile.TemporaryDirectory() as workdir:
            first = driver._try_acquire_challenge_lease("lease-case", workdir=workdir)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(
                    driver._try_acquire_challenge_lease("lease-case", workdir=workdir))
            finally:
                driver._release_challenge_lease(first)
            second = driver._try_acquire_challenge_lease("lease-case", workdir=workdir)
            self.assertIsNotNone(second)
            driver._release_challenge_lease(second)

    def test_fleet_best_of_cannot_repeat_a_failed_target_lifecycle(self):
        """A fleet retry must be deferred to dispatch backoff, not immediate."""
        challenge = SimpleNamespace(unique_code="start-fail", is_completed=False,
                                    difficulty="easy", flag_count=1, total_score=1)
        class Client:
            def list_challenges(self):
                return [challenge]
        with tempfile.TemporaryDirectory() as workdir:
            ctrl = SimpleNamespace(
                workdir=workdir, total_seconds=120, round_factors=[1.0],
                timebox_easy=60, timebox_medium=60, timebox_hard=60,
                max_concurrency=1, best_of=3,
                timebox_for_difficulty=lambda _difficulty: 60,
            )
            calls = []
            def failed_visit(*_args, **_kwargs):
                calls.append(1)
                return {"solved": False, "outcome": "start_failed", "turns": 0}
            with patch.object(driver, "solve_one", side_effect=failed_visit), \
                 patch.object(driver, "_worker_concurrency", return_value=1):
                solved, dropped, api_fault, lifecycle_defer = driver.schedule_rounds(
                    [challenge], Client(), solver=object(), ctrl=ctrl,
                    verifier=object(), stoploss=StopLoss(workdir=workdir),
                    stop_event=threading.Event(),
                )
            self.assertEqual(solved, set())
            self.assertEqual(dropped, {"start-fail"})
            self.assertFalse(api_fault)
            self.assertTrue(lifecycle_defer)
            self.assertEqual(len(calls), 1)

    def test_generic_start_failure_is_retryable_not_an_uncaught_fleet_error(self):
        class Client:
            def start_challenge(self, _code):
                raise RuntimeError("temporary gateway failure")
        started, outcome = driver._start_with_retry(
            Client(), "start-fail", stop_event=threading.Event(),
            rate_wait=lambda: None, retries=1)
        self.assertIsNone(started)
        self.assertEqual(outcome, "start_failed")

    def test_await_task_accepts_same_public_shape_after_explicit_terminal(self):
        challenge = SimpleNamespace(unique_code="same", is_completed=False,
                                    flag_count=1, difficulty="easy", total_score=1)
        class Client:
            def list_challenges(self):
                return [challenge]
        fresh = driver._await_task(Client(), poll=0, stop_event=threading.Event(),
                                   known_fingerprint=None)
        self.assertEqual(fresh, [challenge])


class PiRuntimeRegressionTests(unittest.TestCase):
    def test_flag_extraction_preserves_first_observation_order(self):
        text = "FLAG{First_1} then flag{Second_2} then FLAG{First_1}"
        self.assertEqual(extract_flags(text), ["FLAG{First_1}", "flag{Second_2}"])

    def test_partial_merge_handles_delta_and_cumulative_events(self):
        self.assertEqual(_merge_partial_output("hello", "hello world"), "hello world")
        self.assertEqual(_merge_partial_output("hello", " world"), "hello world")

    def test_slim_transcript_retains_capped_tool_partial(self):
        raw = ('{"type":"tool_execution_update","toolCallId":"call-1",'
               '"partialResult":{"content":[{"type":"text","text":"FLAG{Observed_1}"}]}}')
        slim = _slim_line(raw)
        self.assertIn("tool_execution_update", slim)
        self.assertIn("FLAG{Observed_1}", slim)

    def test_process_tree_is_reaped(self):
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 20 & wait"], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            _stop_process_tree(proc, wait_seconds=1)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                _stop_process_tree(proc, force=True, wait_seconds=1)


if __name__ == "__main__":
    unittest.main()
