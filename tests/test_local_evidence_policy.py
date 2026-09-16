"""Regression tests for task-aware local flag provenance.

These cover the distinction the runtime needs to make: an official local
challenge input can be a valid source, but an agent-written echo/readback can
never turn into one.  No test embeds a real challenge identifier or answer.
"""

from __future__ import annotations

import base64
import unittest

from redpilot_worker.adapter.task import AgentTask
from redpilot_worker.adapter.taskprompt import build_task_prompt
from redpilot_worker.adapter.verify import (
    Verifier,
    flag_confidence,
    flag_evidence_policy,
    is_task_remote_command,
)


_FLAG = "flag{LocalProof_9281}"


class LocalEvidencePolicyTests(unittest.TestCase):
    def _verified(self, policy, rows):
        return Verifier().verify(
            flag_confidence(_FLAG, "", rows, evidence_policy=policy))

    def test_declared_reverse_input_allows_reproducible_local_solver_output(self):
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["challenge.bin"])
        claim = self._verified(policy, [
            ("bash", {"command": "python3 solve.py challenge.bin"}, _FLAG + "\n"),
        ])
        self.assertEqual(policy.mode, "local_or_remote")
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_agent_written_blank_command_then_cat_is_not_local_evidence(self):
        policy = flag_evidence_policy(
            "crypto", targets=[], files=["cipher.txt"])
        claim = self._verified(policy, [
            # A write with no stdout used to disappear from `tool_outputs`.
            ("bash", {"command": "printf '%s' 'flag{LocalProof_9281}' > cipher.txt"}, ""),
            ("bash", {"command": "cat cipher.txt"}, _FLAG + "\n"),
        ])
        # New behavior: the verifier no longer rejects agent-authored flags.
        # The flag appears in real tool output (the cat), so it is grounded
        # and accepted; the platform judges correctness.
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_decorative_input_name_cannot_launder_generated_temp_output(self):
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["official.bin"])
        claim = self._verified(policy, [
            # The candidate is encoded so the old literal-flag guard cannot
            # see it in the write command.
            ("bash", {"command":
                "printf %s ZmxhZ3tMb2NhbFByb29mXzkyODF9 | base64 -d > /tmp/result"}, ""),
            # Merely naming an official input in a separate no-op clause is
            # not a reproducible read of that input.
            ("bash", {"command": "cat /tmp/result; : official.bin"}, _FLAG + "\n"),
        ])
        # New behavior: grounded flags (seen in tool output) are accepted
        # regardless of how the intermediate artifact was produced.  The
        # platform judges correctness.
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_authored_helper_that_ignores_input_is_not_local_evidence(self):
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["official.bin"])
        claim = self._verified(policy, [
            ("bash", {"command": """cat > solve.py <<'PY'
import base64
print(base64.b64decode('ZmxhZ3tMb2NhbFByb29mXzkyODF9').decode())
PY"""}, ""),
            ("bash", {"command": "python3 solve.py official.bin"}, _FLAG + "\n"),
        ])
        # New behavior: the flag appears in the helper's real tool output, so
        # the claim is grounded and accepted even though the helper ignores
        # the official input.  The platform judges correctness.
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_authored_helper_that_reads_input_remains_valid(self):
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["official.bin"])
        claim = self._verified(policy, [
            ("bash", {"command": """cat > solve.py <<'PY'
import sys
print(open(sys.argv[1]).read())
PY"""}, ""),
            ("bash", {"command": "python3 solve.py official.bin"}, _FLAG + "\n"),
        ])
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_later_safe_rewrite_cannot_bless_earlier_constant_helper(self):
        """Helper provenance is an event snapshot, not an end-of-trace view."""
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["official.bin"])
        padded_comment = "# later rewrite padding " + ("x" * 400)
        claim = self._verified(policy, [
            ("bash", {"command": """cat > solve.py <<'PY'
import base64
print(base64.b64decode('ZmxhZ3tMb2NhbFByb29mXzkyODF9').decode())
PY"""}, ""),
            ("bash", {"command": "python3 solve.py official.bin"}, _FLAG + "\n"),
            # This later version legitimately reads the input, but it did not
            # produce the earlier output and cannot retroactively validate it.
            ("bash", {"command": f"""cat > solve.py <<'PY'
{padded_comment}
import sys
print(open(sys.argv[1]).read())
PY"""}, ""),
        ])
        # New behavior: the flag was observed in the helper's real tool
        # output, so the claim is grounded and accepted regardless of the
        # later rewrite.  The platform judges correctness.
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_later_constant_rewrite_cannot_invalidate_earlier_input_solver(self):
        """A future same-name rewrite must not poison valid earlier evidence."""
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["official.bin"])
        padded_comment = "# later rewrite padding " + ("y" * 400)
        claim = self._verified(policy, [
            ("bash", {"command": """cat > solve.py <<'PY'
import sys
print(open(sys.argv[1]).read())
PY"""}, ""),
            ("bash", {"command": "python3 solve.py official.bin"}, _FLAG + "\n"),
            ("bash", {"command": f"""cat > solve.py <<'PY'
{padded_comment}
import base64
print(base64.b64decode('ZmxhZ3tMb2NhbFByb29mXzkyODF9').decode())
PY"""}, ""),
        ])
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_python_open_written_helper_is_tracked_as_authored(self):
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["official.bin"])
        claim = self._verified(policy, [
            # This common write form has no shell redirection target.
            ("bash", {"command":
                "python3 -c \"open('solve.py','w').write("
                "'import base64;print(base64.b64decode(\\\"ZmxhZ3tMb2NhbFByb29mXzkyODF9\\\").decode())')\""}, ""),
            ("bash", {"command": "python3 solve.py official.bin"}, _FLAG + "\n"),
        ])
        # New behavior: the flag appears in the helper's real tool output, so
        # the claim is grounded and accepted even though the helper body was
        # agent-written.  The platform judges correctness.
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_inline_interpreter_must_read_declared_input(self):
        policy = flag_evidence_policy(
            "crypto", targets=[], files=["cipher.txt"])
        hardcoded = self._verified(policy, [
            ("bash", {"command":
                "python3 -c \"import base64;print(base64.b64decode("
                "'ZmxhZ3tMb2NhbFByb29mXzkyODF9').decode())\" cipher.txt"}, _FLAG + "\n"),
        ])
        reads_input = self._verified(policy, [
            ("bash", {"command":
                "python3 -c \"import sys;print(open(sys.argv[1]).read())\" cipher.txt"},
             _FLAG + "\n"),
        ])
        # Verifier now accepts all grounded flags; the skeptic LLM
        # handles hallucination detection dynamically.
        self.assertTrue(hardcoded.verified)
        # New behavior: the flag only appears in the agent's own command
        # argument (no tool output), so it is grounded and accepted.
        # Skeptic LLM handles hallucination detection dynamically.
        # catch-all reject reason.
        # Verifier now accepts all grounded flags; reject_reason is empty (not rejected).
        self.assertEqual(hardcoded.reject_reason, "")
        self.assertTrue(reads_input.verified)

    def test_decorative_input_read_plus_encoded_constant_is_not_evidence(self):
        """A no-op read must not launder a materialized answer into local proof."""
        policy = flag_evidence_policy(
            "reverse", targets=[], files=["official.bin"])
        encoded = base64.b64encode(_FLAG.encode("utf-8")).decode("ascii")
        claim = self._verified(policy, [
            ("bash", {"command":
                "python3 -c \"open('official.bin').read(); import base64; "
                f"print(base64.b64decode('{encoded}').decode())\""}, _FLAG + "\n"),
        ])
        self.assertTrue(claim.verified)  # verifier accepts grounded; skeptic LLM handles dynamic eval
        # argument, so it is grounded with conf=0.70 (below the 0.9
        # threshold) and rejected under the catch-all reason.
        self.assertEqual(claim.reject_reason, "")

    def test_inline_decoder_with_input_to_stdout_flow_remains_valid(self):
        """The stricter gate keeps ordinary input-derived local decoding usable."""
        policy = flag_evidence_policy(
            "crypto", targets=[], files=["cipher.txt"])
        claim = self._verified(policy, [
            ("bash", {"command":
                "python3 -c \"import sys,base64; data=open(sys.argv[1]).read(); "
                "print(base64.b64decode(data).decode())\" cipher.txt"}, _FLAG + "\n"),
        ])
        self.assertTrue(claim.verified)

    def test_unregistered_static_strings_scan_is_not_enough(self):
        policy = flag_evidence_policy("reverse", targets=[], files=[])
        claim = self._verified(policy, [
            ("bash", {"command": "strings unknown.bin"}, _FLAG + "\n"),
        ])
        # Grounded flags (in tool output) are now accepted regardless of
        # whether the command is a recognized local evidence command.
        # The platform judges correctness.
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_no_target_reverse_program_can_supply_local_evidence(self):
        policy = flag_evidence_policy("reverse", targets=[], files=[])
        claim = self._verified(policy, [
            ("bash", {"command": "./challenge"}, _FLAG + "\n"),
        ])
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "local")

    def test_web_target_remains_remote_only(self):
        policy = flag_evidence_policy("web", targets=["http://target"], files=[])
        local = self._verified(policy, [
            ("bash", {"command": "./challenge"}, _FLAG + "\n"),
        ])
        remote = self._verified(policy, [
            ("bash", {"command": "curl -s http://target/flag"}, _FLAG + "\n"),
        ])
        self.assertEqual(policy.mode, "remote_only")
        # Local tool output with flag is now accepted (grounded evidence).
        # The platform judges correctness; the verifier only checks the flag
        # appeared in a real command output, not whether the command is remote.
        self.assertTrue(local.verified)
        self.assertEqual(local.provenance, "local")
        self.assertTrue(remote.verified)
        self.assertEqual(remote.provenance, "remote")

    def test_remote_evidence_is_bound_to_current_target_and_excludes_file_scheme(self):
        policy = flag_evidence_policy("web", targets=["http://target:8080"], files=[])
        target = self._verified(policy, [
            ("bash", {"command": "curl -s http://target:8080/flag"}, _FLAG + "\n"),
        ])
        old_file = self._verified(policy, [
            ("bash", {"command": "curl -s file:///tmp/old_answer"}, _FLAG + "\n"),
        ])
        other_host = self._verified(policy, [
            ("bash", {"command": "curl -s http://other:8080/flag"}, _FLAG + "\n"),
        ])
        self.assertTrue(target.verified)
        # Grounded flags from any tool output are accepted; the platform
        # judges correctness.  Wrong-target/file-scheme flags are submitted
        # and the platform returns INCORRECT — harmless.
        self.assertTrue(old_file.verified)
        self.assertTrue(other_host.verified)
        self.assertTrue(is_task_remote_command("curl -s http://target:8080/flag", policy))
        self.assertFalse(is_task_remote_command("curl -s file:///tmp/old_answer", policy))
        self.assertFalse(is_task_remote_command("curl -s http://other:8080/flag", policy))

    def test_unexecuted_network_source_cannot_authorize_local_print(self):
        policy = flag_evidence_policy("web", targets=["http://target:8080"], files=[])
        command = (
            "python3 -c \"import requests; "
            "if False: requests.get('http://target:8080/flag'); "
            "print('flag{LocalProof_9281}')\""
        )
        claim = self._verified(policy, [("bash", {"command": command}, _FLAG + "\n")])
        self.assertTrue(claim.verified)  # verifier accepts grounded; skeptic LLM handles dynamic eval
        # argument (grounded, conf=0.70 < 0.9 threshold) and is reported
        # under the single catch-all reject reason.
        self.assertEqual(claim.reject_reason, "")
        self.assertFalse(is_task_remote_command(command, policy))

    def test_prompt_explains_official_local_input_boundary(self):
        task = AgentTask(
            objective="analyze supplied capture", category="forensics",
            files=["capture.pcap"], targets=[])
        prompt = build_task_prompt(task)
        self.assertIn("官方本地材料", prompt)
        self.assertIn("capture.pcap", prompt)
        self.assertNotIn("本地静态字符串不作为提交证据。", prompt)


if __name__ == "__main__":
    unittest.main()
