"""Regression tests for challenge-scoped Pi background-process cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from redpilot.worker.adapter.solver.pi_agent import cleanup_instance_processes


class PiProcessCleanupTests(unittest.TestCase):
    def test_cleanup_reaps_only_processes_from_the_matching_instance(self):
        scope = "1" * 32
        other_scope = "2" * 32
        env = dict(os.environ)
        tagged = None
        other = None
        try:
            with tempfile.TemporaryDirectory() as td:
                Path(td, "_instance.json").write_text(
                    json.dumps({"trace_scope": scope}), encoding="utf-8")
                tagged = subprocess.Popen(
                    ["sh", "-c", "exec sleep 30"],
                    env={**env, "TSECBENCH_PI_INSTANCE_TOKEN": scope},
                )
                other = subprocess.Popen(
                    ["sh", "-c", "exec sleep 30"],
                    env={**env, "TSECBENCH_PI_INSTANCE_TOKEN": other_scope},
                )
                time.sleep(0.05)
                self.assertGreaterEqual(cleanup_instance_processes(td, grace_seconds=0.1), 1)
                self.assertIsNotNone(tagged.wait(timeout=3))
                self.assertIsNone(other.poll())
        finally:
            for proc in (tagged, other):
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
