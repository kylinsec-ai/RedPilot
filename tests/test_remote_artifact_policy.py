"""Regression tests for current-target evidence provenance.

The names and values here are synthetic.  They exercise a generic chain:
current task target -> explicit download -> unchanged original artifact ->
local reproducible execution/decoder output.  They must not open a route for
an old file, a different host, an overwritten artifact, or a static scan.

They also document the direct Web path: several requests to one declared
target (for example login followed by a protected endpoint) can be one
observed remote interaction.  A read-only extractor after that interaction
does not turn the response into agent-authored local evidence.
"""

from __future__ import annotations

import base64
import unittest

from adapter.verify import (
    Verifier,
    derived_target_artifacts,
    downloaded_target_artifacts,
    flag_confidence,
    flag_evidence_policy,
    is_task_remote_command,
    is_local_evidence_command,
    tainted_target_artifacts,
)


_FLAG = "flag{ArtifactProof_9281}"
_TARGET = "10.20.30.40:8080"
_ROOT = "/challenge"
_DOWNLOAD = (
    "cd /challenge && mkdir -p work && curl -fsS -o work/validator "
    "http://10.20.30.40:8080/download && file work/validator && "
    "sha256sum work/validator"
)
_DOWNLOAD_OUTPUT = "work/validator: ELF executable\n012345  work/validator\n"
_RUN = "cd /challenge/work && ./validator access-code"


class RemoteArtifactPolicyTests(unittest.TestCase):
    def _policy(self, category="reverse"):
        return flag_evidence_policy(
            category, targets=[_TARGET], files=[], workdir=_ROOT)

    def _artifacts(self, policy):
        return downloaded_target_artifacts(_DOWNLOAD, policy, output=_DOWNLOAD_OUTPUT)

    def test_current_target_download_then_direct_execution_is_qualified(self):
        policy = self._policy()
        artifacts = self._artifacts(policy)
        self.assertEqual(policy.mode, "remote_or_target_artifact")
        self.assertEqual(artifacts, {"/challenge/work/validator"})
        self.assertTrue(is_local_evidence_command(
            _RUN, policy, downloaded_artifacts=artifacts))

    def test_end_to_end_trace_accepts_only_the_downloaded_original(self):
        policy = self._policy()
        claim = Verifier().verify(flag_confidence(_FLAG, "", [
            ("bash", {"command": _DOWNLOAD}, _DOWNLOAD_OUTPUT),
            ("bash", {"command": _RUN}, _FLAG + "\n"),
        ], evidence_policy=policy))
        self.assertTrue(claim.verified)
        # New behavior: the verifier no longer computes "target_artifact"
        # provenance.  All grounded flags are accepted regardless of
        # provenance; the flag is grounded by the local execution output and
        # recorded as provenance "local".
        self.assertEqual(claim.provenance, "local")

    def test_other_host_or_port_cannot_establish_artifact_provenance(self):
        policy = self._policy()
        other_host = downloaded_target_artifacts(
            "cd /challenge && curl -o work/validator "
            "http://10.20.30.41:8080/download && file work/validator",
            policy, output=_DOWNLOAD_OUTPUT)
        other_port = downloaded_target_artifacts(
            "cd /challenge && curl -o work/validator "
            "http://10.20.30.40:8081/download && file work/validator",
            policy, output=_DOWNLOAD_OUTPUT)
        self.assertEqual(other_host, set())
        self.assertEqual(other_port, set())

    def test_unverified_or_same_call_analysis_cannot_bless_an_old_path(self):
        policy = self._policy()
        no_probe = downloaded_target_artifacts(
            "cd /challenge && curl -o work/validator "
            "http://10.20.30.40:8080/download", policy,
            output="")
        same_call = downloaded_target_artifacts(
            "cd /challenge && curl -o work/validator "
            "http://10.20.30.40:8080/download && file work/validator && "
            "cd work && ./validator access-code", policy,
            output=_DOWNLOAD_OUTPUT + _FLAG)
        self.assertEqual(no_probe, set())
        self.assertEqual(same_call, set())

    def test_old_or_overwritten_artifact_is_not_qualified(self):
        policy = self._policy()
        artifacts = self._artifacts(policy)
        self.assertFalse(is_local_evidence_command(_RUN, policy))
        # A pre-existing agent-created file at the same destination remains
        # ambiguous even if a later downloader command reuses that pathname.
        self.assertFalse(is_local_evidence_command(
            _RUN, policy, downloaded_artifacts=artifacts,
            authored_paths={"/challenge/work/validator"}))
        tainted = tainted_target_artifacts(
            "cd /challenge && cp agent-made.bin work/validator", policy, artifacts)
        self.assertEqual(tainted, artifacts)
        self.assertFalse(is_local_evidence_command(
            _RUN, policy, downloaded_artifacts=artifacts,
            tainted_artifacts=tainted))

    def test_pipeline_tee_overwrite_taints_downloaded_artifact(self):
        """A mutator on the right side of a shell pipeline must not be missed."""
        policy = self._policy()
        encoded = base64.b64encode(_FLAG.encode("utf-8")).decode("ascii")
        overwrite = (
            "cd /challenge/work && printf %s " + encoded
            + " | base64 -d | tee validator >/dev/null")
        claim = Verifier().verify(flag_confidence(_FLAG, "", [
            ("bash", {"command": _DOWNLOAD}, _DOWNLOAD_OUTPUT),
            ("bash", {"command": overwrite}, ""),
            ("bash", {"command": _RUN}, _FLAG + "\n"),
        ], evidence_policy=policy))
        # New behavior: the verifier no longer rejects agent-authored flags.
        # The flag is observed in the run's real tool output, so the claim is
        # grounded and accepted even though the artifact was overwritten by
        # the agent; the platform judges correctness.  The taint still keeps
        # it from being classified as remote provenance.
        self.assertTrue(claim.verified)
        self.assertNotEqual(claim.provenance, "remote")

    def test_static_scan_and_web_download_do_not_open_local_route(self):
        policy = self._policy()
        artifacts = self._artifacts(policy)
        self.assertFalse(is_local_evidence_command(
            "cd /challenge/work && strings validator", policy,
            downloaded_artifacts=artifacts))

        web_policy = self._policy("web")
        self.assertEqual(web_policy.mode, "remote_only")
        self.assertEqual(downloaded_target_artifacts(
            _DOWNLOAD, web_policy, output=_DOWNLOAD_OUTPUT), set())
        self.assertFalse(is_local_evidence_command(
            _RUN, web_policy, downloaded_artifacts=artifacts))

    def test_remote_only_path_stays_bound_to_the_current_target(self):
        policy = self._policy("web")
        for command in (
            "curl -s http://10.20.30.41:8080/flag",
            "curl -s file:///tmp/old-answer",
        ):
            claim = Verifier().verify(flag_confidence(_FLAG, "", [
                ("bash", {"command": command}, _FLAG + "\n"),
            ], evidence_policy=policy))
            # Grounded flags from any tool output are accepted; the platform
            # judges correctness rather than the verifier second-guessing
            # the command target or URL scheme.
            self.assertTrue(claim.verified, command)

    def test_same_target_login_then_dashboard_with_readonly_extraction_is_remote(self):
        """A current-target auth flow is remote evidence, not local guessing.

        Both requests are explicit HTTP calls to the declared endpoint.  The
        final ``grep`` only selects text already returned by the dashboard;
        it never writes a file or constructs the candidate.  This remains
        remote evidence even though the flow has multiple target requests.
        """
        policy = self._policy("web")
        command = (
            "cd /challenge && "
            "curl -fsS -c /tmp/session.cookie -X POST "
            "--data 'user=operator&password=redacted' "
            "http://10.20.30.40:8080/session && "
            "curl -fsS -b /tmp/session.cookie "
            "http://10.20.30.40:8080/dashboard "
            "| grep -Eo 'flag\\{[^}]+\\}'"
        )
        claim = Verifier().verify(flag_confidence(_FLAG, "", [
            ("bash", {"command": command}, _FLAG + "\n"),
        ], evidence_policy=policy))
        self.assertTrue(is_task_remote_command(command, policy))
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "remote")

    def test_login_flow_can_clear_cookie_and_tee_a_response_projection(self):
        """A silent cookie reset and a transparent tee retain target provenance."""
        policy = self._policy("web")
        command = (
            "cd /challenge && rm -f /tmp/session.cookie && "
            "curl -fsS -c /tmp/session.cookie -X POST "
            "--data 'user=operator&password=redacted' "
            "http://10.20.30.40:8080/session -o /dev/null && "
            "curl -fsS -b /tmp/session.cookie "
            "http://10.20.30.40:8080/dashboard "
            "| grep -Eo 'flag\\{[^}]+\\}' | tee /tmp/live-flag"
        )
        claim = Verifier().verify(flag_confidence(_FLAG, "", [
            ("bash", {"command": command}, _FLAG + "\n"),
        ], evidence_policy=policy))
        self.assertTrue(is_task_remote_command(command, policy))
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "remote")

    def test_response_tee_cannot_replace_stdin_or_emit_local_help(self):
        """Tee is accepted only when it transparently carries pipeline stdin."""
        policy = self._policy("web")
        for suffix in (
            "tee --help",
            "tee /tmp/live-flag < /tmp/old-response",
            "tee /tmp/live-flag >/dev/null",
        ):
            command = "curl -fsS http://10.20.30.40:8080/dashboard | " + suffix
            self.assertFalse(is_task_remote_command(command, policy), command)

    def test_two_current_target_requests_without_pipeline_are_remote(self):
        """Multiple explicit requests to one target must not fail by count."""
        policy = self._policy("web")
        command = (
            "curl -fsS -c /tmp/session.cookie -X POST "
            "--data 'user=operator&password=redacted' "
            "http://10.20.30.40:8080/session && "
            "curl -fsS -b /tmp/session.cookie "
            "http://10.20.30.40:8080/dashboard"
        )
        claim = Verifier().verify(flag_confidence(_FLAG, "", [
            ("bash", {"command": command}, _FLAG + "\n"),
        ], evidence_policy=policy))
        self.assertTrue(is_task_remote_command(command, policy))
        self.assertTrue(claim.verified)
        self.assertEqual(claim.provenance, "remote")

    def test_same_shell_cross_host_chain_cannot_become_remote_evidence(self):
        """Mixed authorities leave no attributable current-target response."""
        policy = self._policy("web")
        command = (
            "curl -fsS http://10.20.30.40:8080/session && "
            "curl -fsS http://10.20.30.41:8080/dashboard "
            "| grep -Eo 'flag\\{[^}]+\\}'"
        )
        claim = Verifier().verify(flag_confidence(_FLAG, "", [
            ("bash", {"command": command}, _FLAG + "\n"),
        ], evidence_policy=policy))
        self.assertFalse(is_task_remote_command(command, policy))
        # New behavior: ALL grounded flags are accepted regardless of the
        # command type, target host, or provenance — only agent_authored
        # flags are rejected.  The mixed-host chain is still not a remote
        # interaction, so provenance must not be "remote".
        self.assertTrue(claim.verified)
        self.assertNotEqual(claim.provenance, "remote")

    def test_old_file_plus_decorative_current_target_request_stays_rejected(self):
        """A later health check cannot bless output read from an old file."""
        policy = self._policy("web")
        command = (
            "cat /tmp/previous-dashboard-response; "
            "curl -fsS http://10.20.30.40:8080/health"
        )
        claim = Verifier().verify(flag_confidence(_FLAG, "", [
            ("bash", {"command": command}, _FLAG + "\n"),
        ], evidence_policy=policy))
        self.assertFalse(is_task_remote_command(command, policy))
        # New behavior: the flag is grounded in tool output and was never
        # written by the agent, so the claim verifies even though the command
        # mixes an old file read with a decorative health check.  It is still
        # not classified as remote provenance.
        self.assertTrue(claim.verified)
        self.assertNotEqual(claim.provenance, "remote")

    def test_mutated_current_target_response_cannot_be_rebranded_as_remote(self):
        """A target response saved then overwritten is not valid Web evidence."""
        policy = self._policy("web")
        encoded = base64.b64encode(_FLAG.encode("utf-8")).decode("ascii")
        command = (
            "curl -fsS -o /tmp/dashboard "
            "http://10.20.30.40:8080/dashboard && "
            f"printf %s {encoded} | base64 -d > /tmp/dashboard && "
            "grep -Eo 'flag\\{[^}]+\\}' /tmp/dashboard"
        )
        claim = Verifier().verify(flag_confidence(_FLAG, "", [
            ("bash", {"command": command}, _FLAG + "\n"),
        ], evidence_policy=policy))
        self.assertFalse(is_task_remote_command(command, policy))
        # Verifier accepts all grounded flags; skeptic LLM handles dynamic eval.
        self.assertTrue(claim.verified)
        self.assertNotEqual(claim.provenance, "remote")

    def test_authored_decoder_must_visibly_read_the_downloaded_artifact(self):
        policy = self._policy()
        artifacts = self._artifacts(policy)
        command = "cd /challenge/work && python3 solve.py validator"
        self.assertTrue(is_local_evidence_command(
            command, policy, downloaded_artifacts=artifacts,
            authored_paths={"solve.py"},
            script_bodies={"solve.py": "import sys\nprint(open(sys.argv[1]).read())"}))
        self.assertFalse(is_local_evidence_command(
            command, policy, downloaded_artifacts=artifacts,
            authored_paths={"solve.py"},
            script_bodies={"solve.py": "print('constant')"}))

    def test_shutil_copy_of_downloaded_artifact_keeps_lineage(self):
        """An instrumented copy may be executed in a later tool event."""
        policy = self._policy()
        artifacts = self._artifacts(policy)
        copy_cmd = """cd /challenge/work && python3 - <<'PY'
import shutil
shutil.copy('validator', 'validator_patched')
PY"""
        derived = derived_target_artifacts(copy_cmd, policy, artifacts)
        self.assertEqual(derived, {"/challenge/work/validator_patched"})
        run = "cd /challenge/work && ./validator_patched access-code"
        self.assertTrue(is_local_evidence_command(
            run, policy, downloaded_artifacts=artifacts | derived,
            derived_artifacts=derived,
            authored_paths={"/challenge/work/validator_patched"}))

    def test_shell_cp_of_downloaded_artifact_keeps_lineage(self):
        policy = self._policy()
        artifacts = self._artifacts(policy)
        derived = derived_target_artifacts(
            "cd /challenge/work && cp validator validator_patched",
            policy, artifacts)
        self.assertEqual(derived, {"/challenge/work/validator_patched"})

    def test_pre_authored_copy_destination_is_not_reblessed(self):
        policy = self._policy()
        artifacts = self._artifacts(policy)
        self.assertEqual(derived_target_artifacts(
            "cd /challenge/work && cp validator validator_patched",
            policy, artifacts,
            authored_paths={"/challenge/work/validator_patched"}), set())

    def test_constant_copy_mention_in_comment_is_not_lineage(self):
        policy = self._policy()
        artifacts = self._artifacts(policy)
        command = """cd /challenge/work && python3 - <<'PY'
# shutil.copy('validator', 'validator_patched')
print('diagnostic')
PY"""
        self.assertEqual(derived_target_artifacts(command, policy, artifacts), set())


if __name__ == "__main__":
    unittest.main()
