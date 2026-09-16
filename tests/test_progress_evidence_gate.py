"""Regression coverage for StopLoss fact-source qualification.

The blackboard's extraction regexes intentionally recognize broad operational
signals.  The driver must therefore gate *where* an output came from before
letting such a signal reset the no-progress window.
"""

from __future__ import annotations

import tempfile
import unittest

from ghost_worker.adapter.blackboard import Blackboard
from ghost_worker.adapter.stoploss import StopLoss
from ghost_worker.adapter.verify import flag_evidence_policy
from drivers import benchmark_driver as driver


_TARGET = "10.20.30.40:8080"
_FACT_OUTPUT = "10.20.30.40:8080 nginx ssh\n"


def _call(command: str, *, ok: bool = True):
    return {"command": command, "__tsecbench_execution_ok": ok}


class ProgressEvidenceGateTests(unittest.TestCase):
    def _observe(self, board, gate, policy, command, output, *, ok=True):
        return driver._observe_qualified_tool_facts(
            board, gate, policy.task_workdir, "bash", _call(command, ok=ok),
            output, iter=0)

    def test_local_state_and_generated_work_output_do_not_become_facts(self):
        policy = flag_evidence_policy(
            "web", targets=[_TARGET], files=[], workdir="/challenge")
        board = Blackboard()
        gate = driver._ProgressEvidenceGate(policy)

        self.assertEqual(self._observe(
            board, gate, policy, "cat MEMORY.md", _FACT_OUTPUT), 0)
        self.assertEqual(self._observe(
            board, gate, policy, "cat /challenge/work/scan.log", _FACT_OUTPUT), 0)
        self.assertEqual(board.query(), [])

    def test_current_target_response_and_direct_response_file_read_count(self):
        policy = flag_evidence_policy(
            "web", targets=[_TARGET], files=[], workdir="/challenge")
        board = Blackboard()
        gate = driver._ProgressEvidenceGate(policy)

        self.assertGreater(self._observe(
            board, gate, policy,
            "curl -fsS http://10.20.30.40:8080/status", _FACT_OUTPUT), 0)

        # A quiet downloader is still retained by the gate; its following
        # direct `cat` is valid only because the download completed successfully.
        self.assertEqual(self._observe(
            board, gate, policy,
            "cd /challenge && curl -fsS -o response http://10.20.30.40:8080/hosts",
            ""), 0)
        self.assertGreater(self._observe(
            board, gate, policy, "cd /challenge && cat response",
            "10.20.30.41:2222 redis\\n"), 0)

    def test_failed_response_download_cannot_reset_stoploss_from_stale_readback(self):
        policy = flag_evidence_policy(
            "web", targets=[_TARGET], files=[], workdir="/challenge")
        board = Blackboard()
        gate = driver._ProgressEvidenceGate(policy)

        self.assertEqual(self._observe(
            board, gate, policy,
            "cd /challenge && curl -fsS -o response http://10.20.30.40:8080/hosts",
            "", ok=False), 0)
        self.assertEqual(self._observe(
            board, gate, policy, "cd /challenge && cat response", _FACT_OUTPUT), 0)
        self.assertEqual(board.query(), [])

    def test_declared_local_artifact_analysis_counts_but_authored_helper_does_not(self):
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["official.bin"], workdir="/challenge")
        board = Blackboard()
        gate = driver._ProgressEvidenceGate(policy)

        self.assertGreater(self._observe(
            board, gate, policy, "python3 solve.py official.bin", _FACT_OUTPUT), 0)

        blocked_board = Blackboard()
        blocked_gate = driver._ProgressEvidenceGate(policy)
        self.assertEqual(self._observe(
            blocked_board, blocked_gate, policy, "cat > solve.py <<'PY'\nprint('constant')\nPY", ""), 0)
        self.assertEqual(self._observe(
            blocked_board, blocked_gate, policy,
            "python3 solve.py official.bin", _FACT_OUTPUT), 0)

    def test_stoploss_only_reopens_for_qualified_fact(self):
        with tempfile.TemporaryDirectory() as workdir:
            code = "progress-source"
            stoploss = StopLoss(workdir=workdir, dry_cutoff=1)
            stoploss.start(code)
            stoploss.start_session(code)

            remote_policy = flag_evidence_policy(
                "web", targets=[_TARGET], files=[], workdir="/challenge")
            board = Blackboard()
            gate = driver._ProgressEvidenceGate(remote_policy)
            local_added = self._observe(
                board, gate, remote_policy, "cat MEMORY.md", _FACT_OUTPUT)
            self.assertEqual(local_added, 0)
            stoploss.record_no_progress(code)
            self.assertTrue(stoploss.should_stop(code)[0])

            stoploss.revive(code)
            stoploss.start_session(code)
            remote_added = self._observe(
                board, gate, remote_policy,
                "curl -fsS http://10.20.30.40:8080/status", _FACT_OUTPUT)
            self.assertGreater(remote_added, 0)
            stoploss.record_fact(code)
            self.assertFalse(stoploss.should_stop(code)[0])


if __name__ == "__main__":
    unittest.main()
