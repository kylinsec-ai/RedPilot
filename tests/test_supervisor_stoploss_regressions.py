"""Regression coverage for shared stop-loss state and monitor replacement."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from ghost_worker.adapter.stoploss import StopLoss
from drivers import w1_supervisor as supervisor


class _Log:
    """Small logger compatible with the supervisor's fail-safe log calls."""

    def debug(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class StopLossSharedStateTests(unittest.TestCase):
    def test_independent_workers_do_not_lose_concurrent_counter_updates(self):
        """Each worker must reload/mutate/save while holding the same file lock."""
        with tempfile.TemporaryDirectory() as workdir:
            code = "shared-state-case"
            left = StopLoss(workdir=workdir)
            right = StopLoss(workdir=workdir)
            left.start(code, multi_flag=True)
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def update(stoploss: StopLoss, token: str) -> None:
                try:
                    barrier.wait(timeout=3)
                    for _ in range(20):
                        stoploss.start_session(code)
                    self.assertTrue(stoploss.record_flag(code, token))
                except BaseException as exc:  # propagate failures from worker threads
                    errors.append(exc)

            threads = [
                threading.Thread(target=update, args=(left, "opaque-left")),
                threading.Thread(target=update, args=(right, "opaque-right")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(errors)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            state = left._read_state(code)
            self.assertEqual(state.sessions, 40)
            self.assertEqual(state.sessions_total, 40)
            self.assertEqual(state.flags_found, 2)
            self.assertEqual(state.confirmed_flags, 2)


class SupervisorBaselineTests(unittest.TestCase):
    def setUp(self):
        self._old_workdir = supervisor._DRIVER_BASELINE_WORKDIR
        supervisor._DRIVER_BASELINE_WORKDIR = None

    def tearDown(self):
        supervisor._DRIVER_BASELINE_WORKDIR = self._old_workdir

    def test_new_driver_side_resets_stale_baseline_once_then_keeps_grace(self):
        """A rebuilt worker-1 must not reload workers from prior-run status."""
        with tempfile.TemporaryDirectory() as workdir:
            root = Path(workdir)
            status_dir = root / "status"
            status_dir.mkdir()
            for wid in (1, 2):
                (status_dir / f"worker-{wid}.json").write_text(
                    json.dumps({"last_beat": 1, "solving_active": False,
                                "current_code": "old", "sessions": 9,
                                "challenges_solved": 1}),
                    encoding="utf-8",
                )
            old_action = {"ts": 2, "wid": 1, "reason": "old", "detail": "old action"}
            (root / "_supervise.json").write_text(
                json.dumps({
                    "first_tick_ts": 1,
                    "driver_side_ts": 1,
                    "workers": {
                        "1": {"sig": "old", "sig_ts": 1, "sig_change_ts": 1,
                              "last_action_ts": 1},
                        "2": {"sig": "old", "sig_ts": 1, "sig_change_ts": 1},
                    },
                    "actions": [old_action],
                }),
                encoding="utf-8",
            )

            supervisor.mark_driver_side(workdir, now=1000)
            state = json.loads((root / "_supervise.json").read_text(encoding="utf-8"))
            self.assertEqual(state["first_tick_ts"], 1000)
            self.assertEqual(state["driver_side_ts"], 1000)
            self.assertEqual(state["workers"], {})
            self.assertEqual(state["actions"], [old_action])

            # This is the first supervisor tick of the replacement process.
            # The old stale heartbeats must be observed only, never acted on.
            self.assertEqual(supervisor.supervise_tick(workdir, _Log(), now=1001), [])
            self.assertFalse((root / ".reload.wid1").exists())
            self.assertFalse((root / ".reload.wid2").exists())

            # Later heartbeat publications do not continuously restart grace.
            supervisor.mark_driver_side(workdir, now=1100)
            state = json.loads((root / "_supervise.json").read_text(encoding="utf-8"))
            self.assertEqual(state["first_tick_ts"], 1000)
            self.assertEqual(state["driver_side_ts"], 1100)


if __name__ == "__main__":
    unittest.main()
