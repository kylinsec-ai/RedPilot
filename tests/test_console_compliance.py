"""Focused regressions for the FastAPI console's evidence boundaries."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fastapi-console"))

from fastapi_console import agent as console_agent
from fastapi_console import main as console_main
from fastapi_console import services as console_services
from fastapi_console.cfg import save_cfg
from fastapi_console.solver import ask_llm, redact_flag_like_text
from tsecbench.errors import APIError


def _brief() -> dict:
    return {
        "code": "case-1",
        "description": "test target",
        "difficulty": "easy",
        "level": 1,
        "flag_count": 1,
        "correct_count": 0,
        "completed": False,
        "container_status": "available",
        "addresses": ["10.0.0.1:80"],
        "hint": None,
        "hint_viewed": False,
    }


class ConsoleComplianceTests(unittest.TestCase):
    def test_plan_round_never_turns_model_output_into_a_submission(self):
        # Compose the envelope to ensure this regression test itself does not
        # seed a literal benchmark-looking answer into source.
        model_text = "先收集响应头，再验证入口。\n" + "f" + "lag{model-secret}"
        with patch.object(console_services, "get_cfg", return_value={}), \
             patch.object(console_services, "challenge_brief", return_value=_brief()), \
             patch.object(console_services, "ask_llm", return_value=model_text), \
             patch.object(console_services, "submit_flag", side_effect=AssertionError("must not submit")):
            result = console_services.run_ai_round({}, "case-1")

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["results"], [])
        self.assertFalse(result["made_progress"])
        self.assertNotIn("model-secret", result["plan"])
        self.assertIn("已脱敏", result["plan"])

    def test_plan_redacts_spacing_variant_of_answer_envelope(self):
        model_text = "请提交 F" + "LAG { spaced-secret }，然后继续。"
        safe = redact_flag_like_text(model_text)
        self.assertNotIn("spaced-secret", safe)
        self.assertIn("已脱敏", safe)

    def test_plan_redacts_multiline_answer_envelope(self):
        model_text = "不要提交 f" + "lag{\nmultiline-secret\n}；先取得靶场证据。"
        safe = redact_flag_like_text(model_text)
        self.assertNotIn("multiline-secret", safe)
        self.assertIn("已脱敏", safe)

    def test_plan_redacts_letter_spaced_answer_envelope(self):
        model_text = "不要提交 f l a g { spaced-letters-secret }。"
        safe = redact_flag_like_text(model_text)
        self.assertNotIn("spaced-letters-secret", safe)
        self.assertIn("已脱敏", safe)

    def test_provider_error_does_not_reflect_answer_shaped_detail(self):
        secret = "provider-secret"
        body = json.dumps({"error": {"message": "f" + "lag{" + secret + "}"}}).encode()
        error = HTTPError("https://provider.invalid/chat/completions", 400, "bad request", {}, BytesIO(body))
        self.addCleanup(error.close)
        cfg = {"llmBaseUrl": "https://provider.invalid", "llmApiKey": "test", "llmModel": "test"}
        with patch("fastapi_console.solver.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(APIError) as caught:
                ask_llm(cfg, [{"role": "user", "content": "plan only"}])

        self.assertNotIn(secret, caught.exception.message)
        self.assertIn("已脱敏", caught.exception.message)

    def test_api_error_renderer_redacts_nested_provider_detail(self):
        secret = "nested-error-secret"
        error = APIError(
            502,
            "provider_error",
            "provider replied f" + "lag{" + secret + "}",
            {"nested": ["f" + "lag{" + secret + "}"]},
        )
        rendered = console_main._error(error).body.decode("utf-8")
        self.assertNotIn(secret, rendered)
        self.assertIn("已脱敏", rendered)

    def test_plan_refuses_mismatched_challenge_context_before_calling_llm(self):
        mismatched = _brief()
        mismatched["code"] = "other-case"
        with patch.object(console_services, "get_cfg", return_value={}), \
             patch.object(console_services, "challenge_brief", return_value=mismatched), \
             patch.object(console_services, "ask_llm") as ask:
            with self.assertRaises(APIError) as caught:
                console_services.run_ai_round({}, "case-1")

        self.assertEqual(caught.exception.code, "challenge_context_mismatch")
        ask.assert_not_called()

    def test_remote_hint_is_not_reused_after_platform_connection_changes(self):
        session = {"console_cfg": {"baseUrl": "https://first.invalid", "token": "first"}}

        def fake_remote(base, _token, path, *_args, **_kwargs):
            if path.startswith("/openapi/v1/challenges/hint"):
                self.assertEqual(base, "https://first.invalid")
                return {"hint": "first-platform-only"}
            self.assertEqual(base, "https://second.invalid")
            return [{
                "unique_code": "reused-code", "description": "new target",
                "difficulty": "easy", "level": 1, "flag_count": 1,
                "correct_flag_count": 0, "is_completed": False,
                "container_status": "stopped", "container_addr": [],
            }]

        with patch.object(console_services, "_remote_request", side_effect=fake_remote):
            console_services.get_hint(session, "reused-code")
            save_cfg(session, {"baseUrl": "https://second.invalid", "token": "second"})
            brief = console_services.challenge_brief(session, "reused-code")

        self.assertFalse(brief["hint_viewed"])
        self.assertIsNone(brief["hint"])

    def test_inflight_old_platform_hint_is_discarded_after_connection_switch(self):
        session = {"console_cfg": {"baseUrl": "https://first.invalid", "token": "first"}}

        def fake_remote(_base, _token, _path, *_args, **_kwargs):
            save_cfg(session, {"baseUrl": "https://second.invalid", "token": "second"})
            return {"hint": "stale-platform-only"}

        with patch.object(console_services, "_remote_request", side_effect=fake_remote):
            with self.assertRaises(APIError) as caught:
                console_services.get_hint(session, "reused-code")

        self.assertEqual(caught.exception.code, "remote_context_changed")
        self.assertEqual(session["console_remote_state"]["hints"], {})

    def test_legacy_auto_endpoint_is_plan_only_and_has_no_platform_side_effects(self):
        round_result = {
            "plan": "先检查当前靶场响应。",
            "candidates": [],
            "results": [],
            "completed": False,
            "flag_count": 1,
            "logs": [{"time": "10:00:00", "type": "info", "text": "计划已生成"}],
            "made_progress": False,
        }
        with patch.object(console_services, "run_ai_round", return_value=round_result), \
             patch.object(console_services, "start_challenge") as start, \
             patch.object(console_services, "get_hint") as hint, \
             patch.object(console_services, "submit_flag") as submit, \
             patch.object(console_services, "close_challenge") as close:
            result = console_services.run_ai_auto({}, "case-1")

        self.assertEqual(result["submission_authorization"], "none")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["results"], [])
        start.assert_not_called()
        hint.assert_not_called()
        submit.assert_not_called()
        close.assert_not_called()

    def test_historical_event_plaintext_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "work").mkdir()
            event = {
                "ts": 1,
                "event": "flag_submit",
                "worker_id": 1,
                "boot_id": "boot",
                "payload": {
                    "code": "case-1",
                    "correct": True,
                    "awarded": 100,
                    "flag": "f" + "lag{old-secret}",
                    "candidate_sha256": "a" * 64,
                    "expected_flag_count": 1,
                    "correct_flag_count": 1,
                    "total_flag_count": 1,
                },
            }
            (root / "work" / "_events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
            with patch.object(console_agent, "PROJECT_ROOT", root):
                result = console_agent._aggregate_events()

        self.assertEqual(result["summary"]["flags_found"], [])
        self.assertEqual(result["summary"]["flags_found_count"], 1)
        self.assertNotIn("old-secret", json.dumps(result))

    def test_log_fallback_is_counter_only(self):
        log = "FLAG CORRECT on case-1: " + "f" + "lag{log-secret} (+100 pts, total 100)\n"
        proc = SimpleNamespace(returncode=0, stdout=log, stderr="")
        with patch.object(console_agent, "_run", return_value=proc):
            result = console_agent._parse_worker_stats_from_logs("tsecbench-worker-2")

        self.assertEqual(result["flags_found_count"], 1)
        self.assertEqual(result["flags_submitted"], 1)
        self.assertNotIn("flags_found", result)
        self.assertNotIn("log-secret", json.dumps(result))

    def test_rotation_deletes_blackboard_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            backup = work / "_blackboard.json.bak"
            backup.write_text("stale", encoding="utf-8")
            (work / "_events.jsonl").write_text("old", encoding="utf-8")
            with patch.object(console_agent, "PROJECT_ROOT", root), \
                 patch.object(console_agent, "WORK_STATUS_DIR", work / "status"):
                console_agent._rotate_stats()

            self.assertFalse(backup.exists())
            self.assertEqual((work / "_events.jsonl").read_text(encoding="utf-8"), "")

    def test_start_is_idempotent_while_a_solver_owns_workdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".agent.env"
            env_file.write_text("BENCHMARK_TOKEN=test\n", encoding="utf-8")
            status = {"workers": [], "summary": {}}
            with patch.object(console_agent, "AGENT_ENV_FILE", env_file), \
                 patch.object(console_agent, "_active_solver_containers", return_value=["tsecbench-worker-2"]), \
                 patch.object(console_agent, "fleet_status", return_value=status.copy()), \
                 patch.object(console_agent, "_rotate_stats") as rotate:
                result = console_agent.fleet_start()

        rotate.assert_not_called()
        self.assertEqual(result["start_action"], "already_running")
        self.assertEqual(result["active_solver_containers"], ["tsecbench-worker-2"])

    def test_start_does_not_rotate_during_monitor_only_compose_startup(self):
        """worker-1 starts before the solver containers and still owns work/."""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".agent.env"
            env_file.write_text("BENCHMARK_TOKEN=test\n", encoding="utf-8")
            status = {"workers": [], "summary": {}}

            def fake_run(args, **_kwargs):
                if args[:2] == ["docker", "inspect"]:
                    name = args[-1]
                    running = name == "tsecbench-worker-1"
                    return SimpleNamespace(
                        returncode=0,
                        stdout=("true|false|running" if running else "false|false|exited"),
                        stderr="",
                    )
                if args[:2] == ["docker", "ps"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {args}")

            with patch.object(console_agent, "AGENT_ENV_FILE", env_file), \
                 patch.object(console_agent, "_run", side_effect=fake_run), \
                 patch.object(console_agent, "fleet_status", return_value=status.copy()), \
                 patch.object(console_agent, "_rotate_stats") as rotate:
                result = console_agent.fleet_start()

        rotate.assert_not_called()
        self.assertEqual(result["start_action"], "already_running")
        self.assertEqual(result["active_solver_containers"], ["tsecbench-worker-1"])


if __name__ == "__main__":
    unittest.main()
