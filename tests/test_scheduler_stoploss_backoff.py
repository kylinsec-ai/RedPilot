"""Regression coverage for terminal zero-progress scheduler behavior."""

from __future__ import annotations

import os
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from redpilot_worker.adapter.stoploss import StopLoss
from redpilot_worker import orchestrator as driver


class SchedulerStopLossBackoffTests(unittest.TestCase):
    def test_new_task_epoch_reused_code_does_not_inherit_terminal_stoploss(self):
        """A reused public code must reach start, not be dropped by old state."""
        with tempfile.TemporaryDirectory() as workdir:
            code = "reused-public-code"
            stoploss = StopLoss(
                workdir=workdir,
                dry_cutoff=3,
                zero_flag_cutoff=3,
                max_sessions=20,
            )
            stoploss.start(code, multi_flag=False)
            for _ in range(3):
                stoploss.start_session(code)
                stoploss.record_no_progress(code)
            self.assertTrue(stoploss.should_stop(code)[0])

            code_dir = os.path.join(workdir, driver._safe_code(code))
            with open(driver._workdir_epoch_path(code_dir), "w", encoding="utf-8") as fh:
                json.dump({"task_epoch": "old-task"}, fh)

            challenge = SimpleNamespace(
                unique_code=code,
                flag_count=1,
                difficulty="medium",
            )
            controller = SimpleNamespace(workdir=workdir)

            class Client:
                def list_challenges(self):
                    return []

            # Returning a start failure lets the test stop immediately after
            # the pre-start path.  A call proves the old terminal StopLoss
            # state was cleared before the entry guard was consulted.
            with patch.object(driver, "_TASK_EPOCH", "new-task"), \
                    patch.object(driver, "_CLAIM_VERIFY_DELAY", 0), \
                    patch.object(driver, "_start_with_retry",
                                 return_value=(None, "start_failed")) as starter:
                result = driver._solve_one_unlocked(
                    Client(), challenge, 300, 0,
                    solver=object(), ctrl=controller, verifier=object(),
                    stoploss=stoploss, stop_event=threading.Event(),
                    submitted={}, submitted_lock=threading.Lock(),
                )

            self.assertTrue(starter.called)
            self.assertEqual(result["outcome"], "start_failed")
            self.assertFalse(stoploss.should_stop(code)[0])
            with open(driver._workdir_epoch_path(code_dir), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["task_epoch"], "new-task")

    def test_single_flag_zero_progress_stop_does_not_reopen_target(self):
        """Three dry sessions must switch away before another lifecycle starts."""
        with tempfile.TemporaryDirectory() as workdir:
            code = "generic-single-flag"
            stoploss = StopLoss(
                workdir=workdir,
                dry_cutoff=3,
                zero_flag_cutoff=3,
                max_sessions=20,
            )
            stoploss.start(code, multi_flag=False)
            for _ in range(3):
                stoploss.start_session(code)
                stoploss.record_no_progress(code)
                stoploss.record_zero_flag(code)

            stopped, reason = stoploss.should_stop(code)
            self.assertTrue(stopped)
            self.assertIn("stuck:dry_sessions=3", reason)

            class Client:
                start_calls = 0

                def start_challenge(self, _code):
                    self.start_calls += 1
                    raise AssertionError("terminal zero-progress state must not start a target")

            client = Client()
            challenge = SimpleNamespace(unique_code=code, flag_count=1)
            controller = SimpleNamespace(workdir=workdir)
            with patch.dict(os.environ, {"ADAPTER_REVIVE_COOLDOWN": "86400"}, clear=False):
                result = driver._solve_one_unlocked(
                    client,
                    challenge,
                    300,
                    0,
                    solver=object(),
                    ctrl=controller,
                    verifier=object(),
                    stoploss=stoploss,
                    stop_event=threading.Event(),
                    submitted={},
                    submitted_lock=threading.Lock(),
                )

            self.assertEqual(result["outcome"], "dropped")
            self.assertEqual(client.start_calls, 0)
            self.assertTrue(stoploss.should_stop(code)[0])

    def test_terminal_zero_progress_uses_revive_cooldown_not_retry_budget(self):
        """The dispatcher must not make three empty retry entries before revive."""
        with tempfile.TemporaryDirectory() as workdir:
            code = "generic-terminal-dry"
            stoploss = StopLoss(workdir=workdir, dry_cutoff=3, max_sessions=20)
            stoploss.start(code, multi_flag=False)
            for _ in range(3):
                stoploss.start_session(code)
                stoploss.record_no_progress(code)
            self.assertTrue(stoploss.should_stop(code)[0])

            challenge = SimpleNamespace(
                unique_code=code,
                is_completed=False,
                difficulty="easy",
                flag_count=1,
                total_score=1,
            )

            class Client:
                def list_challenges(self):
                    return [challenge]

            controller = SimpleNamespace(workdir=workdir, total_seconds=120)
            stop_event = threading.Event()

            def idle_once(_seconds, **_kwargs):
                stop_event.set()

            started = time.time()
            with patch.object(driver, "_TASK_EPOCH", ""), \
                    patch.dict(os.environ, {
                    "ADAPTER_WORKER_COUNT": "1",
                    "ADAPTER_WORKER_ID": "0",
                    "ADAPTER_AUTO_POLL": "0",
                    "ADAPTER_DROP_RETRY": "2",
                    "ADAPTER_AUTO_MAX_RETRY": "3",
                    "ADAPTER_REVIVE_COOLDOWN": "300",
                }, clear=False), \
                    patch.object(driver, "schedule_rounds",
                                 return_value=(set(), {code}, False, False)) as rounds, \
                    patch.object(driver, "_idle_tick", side_effect=idle_once):
                driver._activate_task_epoch([challenge], workdir=workdir)
                driver.auto_dispatch_loop(
                    Client(),
                    seed=[challenge],
                    ctrl=controller,
                    solver=object(),
                    verifier=object(),
                    stoploss=stoploss,
                    stop_event=stop_event,
                    only="",
                    caps=set(),
                )

            self.assertEqual(rounds.call_count, 1)
            saved = json.loads(
                open(driver._retry_state_path(workdir, 0), encoding="utf-8").read())
            self.assertEqual(saved["attempts"], {})
            self.assertGreaterEqual(saved["retry_at"][code], started + 290)


if __name__ == "__main__":
    unittest.main()
