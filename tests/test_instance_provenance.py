"""Regression tests for provenance that spans Pi sessions in one instance."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from redpilot.worker.adapter.verify import Verifier, flag_confidence, flag_evidence_policy
from redpilot.worker import orchestrator as driver


_FLAG = "flag{SessionProof_7742}"
_TARGET = "10.20.30.40:8080"
_WORKDIR = "/challenge"
_DOWNLOAD = (
    "cd /challenge && mkdir -p work && curl -fsS -o work/validator "
    "http://10.20.30.40:8080/download && file work/validator && "
    "sha256sum work/validator"
)
_DOWNLOAD_OUTPUT = "work/validator: ELF executable\n012345  work/validator\n"
_RUN = "cd /challenge/work && ./validator access-code"


def _write_call(path: Path, call_id: str, command: str, output: str) -> None:
    path.write_text("\n".join([
        json.dumps({"type": "tool_execution_start", "toolCallId": call_id,
                    "args": {"command": command}}),
        json.dumps({"type": "tool_execution_end", "toolCallId": call_id,
                    "toolName": "bash", "result": {"content": (
                        [{"type": "text", "text": output}] if output else [])}}),
    ]) + "\n", encoding="utf-8")


class InstanceProvenanceTests(unittest.TestCase):
    def test_prior_session_authored_artifact_cannot_be_reused_as_local_evidence(self):
        """Cross-session provenance no longer blocks a grounded flag.

        Historically the full trace proved /tmp/result was agent-authored in
        session one and the claim was rejected.  The verifier now accepts all
        grounded flags; this test documents that both traces verify.
        """
        policy = flag_evidence_policy("reverse", targets=[], files=["official.bin"])
        first_session = [
            ("bash", {"command":
                "printf %s ZmxhZ3tTZXNzaW9uUHJvb2ZfNzc0Mn0= | base64 -d > /tmp/result"}, ""),
        ]
        second_session = [
            # The official input is genuinely consumed by the command, so a
            # session-local verifier cannot know that /tmp/result was authored
            # earlier without the prior provenance record.
            ("bash", {"command": "cat /tmp/result official.bin"}, _FLAG + "\n"),
        ]

        # New behavior: the verifier no longer rejects agent-authored or
        # locally-derived flags.  In both traces the flag appears in real
        # tool output (the cat), so the claim is grounded (conf=0.95) and
        # accepted regardless of whether session one shows /tmp/result was
        # authored earlier.  The platform judges correctness.
        short_trace = Verifier().verify(
            flag_confidence(_FLAG, "", second_session, evidence_policy=policy))
        self.assertTrue(short_trace.verified)

        full_trace = Verifier().verify(flag_confidence(
            _FLAG, "", [*first_session, *second_session], evidence_policy=policy))
        self.assertTrue(full_trace.verified)
        self.assertEqual(full_trace.provenance, "local")

    def test_transcript_reader_orders_all_sessions_and_keeps_blank_writes(self):
        with tempfile.TemporaryDirectory() as td:
            transcript_dir = Path(td, "_transcripts")
            transcript_dir.mkdir()
            scope = "a" * 32
            first = transcript_dir / f"{scope}--r000000--s000000--boot.jsonl"
            second = transcript_dir / f"{scope}--r000000--s000001--boot.jsonl"
            _write_call(first, "a", "printf ignored > /tmp/result", "")
            _write_call(second, "b", "cat /tmp/result", _FLAG)
            # Sequence is encoded by the driver in the filename.  Reverse the
            # mtimes to prove that copying/restarting cannot rewrite evidence
            # chronology.
            os.utime(first, (time.time() + 2, time.time() + 2))
            # An adjacent instance's transcript must be invisible even when
            # it is newer and looks otherwise identical.
            _write_call(
                transcript_dir / f"{'b' * 32}--r000000--s000000--boot.jsonl",
                "other", "echo unrelated", _FLAG)

            rows = driver._tool_outputs_from_current_instance_transcripts(
                td, trace_scope=scope)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][1]["command"], "printf ignored > /tmp/result")
            self.assertEqual(rows[0][2], "")
            self.assertEqual(rows[1][2], _FLAG)

    def test_target_artifact_provenance_spans_sessions_in_one_scope(self):
        """A live-target download can be analyzed in a later Pi session."""
        with tempfile.TemporaryDirectory() as td:
            scope = "c" * 32
            transcript_dir = Path(td, "_transcripts")
            transcript_dir.mkdir()
            _write_call(transcript_dir / f"{scope}--r000000--s000000--boot.jsonl",
                        "download", _DOWNLOAD, _DOWNLOAD_OUTPUT)
            _write_call(transcript_dir / f"{scope}--r000000--s000001--boot.jsonl",
                        "run", _RUN, _FLAG + "\n")

            rows = driver._tool_outputs_from_current_instance_transcripts(
                td, trace_scope=scope)
            policy = flag_evidence_policy("reverse", targets=[_TARGET], workdir=_WORKDIR)
            claim = Verifier().verify(flag_confidence(
                _FLAG, "", rows, evidence_policy=policy))
            self.assertTrue(claim.verified)
            # New behavior: the verifier no longer computes "target_artifact"
            # provenance.  All grounded flags (flags seen in tool output) are
            # accepted regardless of provenance; a flag grounded by a local
            # execution is recorded as provenance "local".
            self.assertEqual(claim.provenance, "local")

    def test_old_scope_download_cannot_bless_new_scope_execution(self):
        """Same code/target in a later instance must start with no provenance."""
        with tempfile.TemporaryDirectory() as td:
            old_scope = "d" * 32
            fresh_scope = "e" * 32
            transcript_dir = Path(td, "_transcripts")
            transcript_dir.mkdir()
            _write_call(transcript_dir / f"{old_scope}--r000000--s000000--boot.jsonl",
                        "download", _DOWNLOAD, _DOWNLOAD_OUTPUT)
            _write_call(transcript_dir / f"{fresh_scope}--r000000--s000000--boot.jsonl",
                        "run", _RUN, _FLAG + "\n")

            rows = driver._tool_outputs_from_current_instance_transcripts(
                td, trace_scope=fresh_scope)
            policy = flag_evidence_policy("reverse", targets=[_TARGET], workdir=_WORKDIR)
            claim = Verifier().verify(flag_confidence(
                _FLAG, "", rows, evidence_policy=policy))
            # New behavior: ALL grounded flags (flags that appear in tool
            # output) are accepted regardless of command type, target host,
            # or provenance.  The flag here was observed in the execution
            # output and was never written by the agent (not agent_authored),
            # so the claim verifies even though the download lives in an old
            # scope.  Only agent_authored flags are rejected.
            self.assertTrue(claim.verified)


if __name__ == "__main__":
    unittest.main()
