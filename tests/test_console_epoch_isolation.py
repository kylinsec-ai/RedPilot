"""Regression coverage for task-epoch isolation in the web control plane."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fastapi-console"))

from fastapi_console import agent as console_agent


class ConsoleEpochIsolationTests(unittest.TestCase):
    def _root_with_epoch(self, epoch: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        status = root / "work" / "status"
        status.mkdir(parents=True)
        (status / "task-epoch.json").write_text(
            json.dumps({"generation": 2, "epoch": epoch, "terminal": False}),
            encoding="utf-8",
        )
        return root

    def test_aggregate_excludes_prior_task_epoch(self):
        root = self._root_with_epoch("t2-current")
        events = [
            {"ts": 1, "event": "session_start", "worker_id": 1,
             "boot_id": "old", "task_epoch": "t1-old",
             "payload": {"code": "reused-code"}},
            {"ts": 2, "event": "flag_submit", "worker_id": 1,
             "boot_id": "old", "task_epoch": "t1-old",
             "payload": {"code": "reused-code", "correct": True,
                         "awarded": 100, "expected_flag_count": 1,
                         "correct_flag_count": 1, "total_flag_count": 1}},
            {"ts": 3, "event": "challenge_solved", "worker_id": 1,
             "boot_id": "old", "task_epoch": "t1-old",
             "payload": {"code": "reused-code"}},
            {"ts": 4, "event": "session_start", "worker_id": 2,
             "boot_id": "fresh", "task_epoch": "t2-current",
             "payload": {"code": "reused-code"}},
        ]
        (root / "work" / "_events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events), encoding="utf-8")

        with patch.object(console_agent, "PROJECT_ROOT", root):
            result = console_agent._aggregate_events()

        self.assertTrue(result["epoch_scoped"])
        self.assertEqual(result["task_epoch"], "t2-current")
        self.assertEqual(result["summary"]["flags_submitted"], 0)
        self.assertEqual(result["summary"]["solved"], 0)
        self.assertEqual(result["current"], [{
            "code": "reused-code", "since": 4,
            "worker_id": "2", "boot_id": "fresh",
        }])

    def test_bootstrap_synthetic_end_can_close_current_epoch_session(self):
        root = self._root_with_epoch("t2-current")
        events = [
            {"ts": 1, "event": "session_start", "worker_id": 1,
             "boot_id": "departed", "task_epoch": "t2-current",
             "payload": {"code": "case-1"}},
            # The driver emits this repair before it restores its epoch context.
            {"ts": 2, "event": "session_end", "worker_id": 2,
             "boot_id": "new", "payload": {"code": "case-1", "synthetic": True,
                                              "worker_id": 1, "boot_id": "departed"}},
        ]
        (root / "work" / "_events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events), encoding="utf-8")

        with patch.object(console_agent, "PROJECT_ROOT", root):
            result = console_agent._aggregate_events()

        self.assertEqual(result["current"], [])
        self.assertEqual(result["summary"]["flags_submitted"], 0)

    def test_explicit_rotation_marks_epoch_terminal(self):
        root = self._root_with_epoch("t7-same-task")
        status = root / "work" / "status"
        with patch.object(console_agent, "PROJECT_ROOT", root), \
             patch.object(console_agent, "WORK_STATUS_DIR", status):
            console_agent._rotate_stats()

        saved = json.loads((status / "task-epoch.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["terminal"])
        self.assertEqual(saved["epoch"], "t7-same-task")
        self.assertEqual(saved["generation"], 2)

    def test_rotation_discards_malformed_epoch_metadata(self):
        root = self._root_with_epoch("t7-same-task")
        status = root / "work" / "status"
        epoch_path = status / "task-epoch.json"
        epoch_path.write_text("[]", encoding="utf-8")
        with patch.object(console_agent, "PROJECT_ROOT", root), \
             patch.object(console_agent, "WORK_STATUS_DIR", status):
            console_agent._rotate_stats()

        self.assertFalse(epoch_path.exists())

    def test_epoch_scoped_events_override_stale_worker_counters_even_when_zero(self):
        root = self._root_with_epoch("t2-current")
        stale_state = {
            "flags_submitted": 3,
            "flags_found_count": 3,
            "total_earned": 300,
            "challenges_solved": 3,
        }

        def fake_run(args, **_kwargs):
            if args[:2] == ["docker", "inspect"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="running|healthy|0|true|ADAPTER_WORKER_ID=1 ",
                    stderr="",
                )
            if args[:3] == ["docker", "ps", "-a"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {args}")

        current_events = {
            "summary": {"flags_submitted": 0, "flags_found_count": 0,
                        "total_earned": 0, "solved": 0},
            "current": [], "last_events": [], "task_epoch": "t2-current",
            "epoch_scoped": True,
        }
        with patch.object(console_agent, "PROJECT_ROOT", root), \
             patch.object(console_agent, "WORKER_NAMES", ["tsecbench-worker-2"]), \
             patch.object(console_agent, "_run", side_effect=fake_run), \
             patch.object(console_agent, "_read_worker_status", return_value=stale_state), \
             patch.object(console_agent, "_aggregate_events", return_value=current_events):
            result = console_agent.fleet_status()

        summary = result["summary"]
        self.assertEqual(summary["flags_submitted"], 0)
        self.assertEqual(summary["flags_found_count"], 0)
        self.assertEqual(summary["total_earned"], 0)
        self.assertEqual(summary["solved"], 0)


if __name__ == "__main__":
    unittest.main()
