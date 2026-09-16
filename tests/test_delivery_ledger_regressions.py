"""Regression coverage for delivery-ledger idempotency and live queue safety."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from redpilot_worker.adapter import hallucination
from redpilot_worker import orchestrator as driver


class DeliveryLedgerRegressionTests(unittest.TestCase):
    def test_concurrent_unverified_records_write_one_ledger_key_and_one_mark(self):
        candidate = "flag{LedgerUnique_9281}"
        with tempfile.TemporaryDirectory() as workdir:
            barrier = threading.Barrier(8)

            def reject() -> None:
                barrier.wait()
                driver._add_unverified_flag(
                    workdir, candidate, "local_computed_only")

            workers = [threading.Thread(target=reject) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            rows = Path(workdir, ".unverified_flags").read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(rows, [candidate])
            self.assertEqual(hallucination.state(workdir)["derivations"], 1)

    def test_rejected_ledger_is_exactly_idempotent(self):
        candidate = "flag{RejectedUnique_9281}"
        with tempfile.TemporaryDirectory() as workdir:
            driver._add_rejected_flag(workdir, candidate)
            driver._add_rejected_flag(workdir, candidate)
            rows = Path(workdir, ".rejected_flags").read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(rows, [candidate])

    def test_live_rejection_defers_queue_rewrite_until_session_boundary(self):
        stale = "flag{DeferredStale_9281}"
        later_stage = "flag{LiveStage_9282}"
        with tempfile.TemporaryDirectory() as workdir:
            queue = Path(workdir, "FLAG")
            queue.write_text(f"{stale}\n{later_stage}\n", encoding="utf-8")
            driver._add_unverified_flag(
                workdir, stale, "local_computed_only", prune_delivery=False)
            self.assertEqual(queue.read_text(encoding="utf-8"),
                             f"{stale}\n{later_stage}\n")

            driver._prune_suppressed_delivery_candidates(workdir)
            self.assertEqual(queue.read_text(encoding="utf-8"),
                             f"{later_stage}\n")

    def test_blackboard_input_excludes_unconfirmed_candidate_but_keeps_facts(self):
        from redpilot_worker.adapter.blackboard import Blackboard

        candidate = "flag{NoFalseProgress_9281}"
        with tempfile.TemporaryDirectory() as workdir:
            board = Blackboard(str(Path(workdir, "_blackboard.json")))
            output = (
                "target 10.20.30.40:8080 serves nginx\n"
                + candidate + "\n")
            added = board.observe(
                "bash", {"command": "curl current-target"},
                driver._without_flag_candidates_for_blackboard(workdir, output))
            self.assertGreater(added, 0)
            self.assertFalse(board.query("flag"))
            self.assertNotIn(candidate, Path(workdir, "_blackboard.json").read_text(
                encoding="utf-8"))

    def test_blackboard_never_persists_a_flag_candidate_directly(self):
        from redpilot_worker.adapter.blackboard import Blackboard

        candidate = "flag{DirectCandidate_9281}"
        with tempfile.TemporaryDirectory() as workdir:
            path = Path(workdir, "_blackboard.json")
            board = Blackboard(str(path))
            board.observe("bash", {"command": "cat response"}, candidate + "\n")
            self.assertFalse(board.query("flag"))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
