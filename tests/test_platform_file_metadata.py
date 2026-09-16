"""Focused checks for optional local-material metadata passthrough.

The API may omit this metadata. These tests only verify that an already
present response field reaches ``AgentTask.files``; they deliberately do not
assume a download endpoint, URL convention, or attachment storage layout.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from adapter.platform.base import Challenge
from adapter.platform.generic_openapi import GenericOpenAPIBackend
from adapter.platform.tsecbench_http import TSecBenchHTTPBackend
from adapter.platform.tsecbench_sdk import TSecBenchSDKBackend
from drivers.benchmark_driver import build_task


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _challenge_payload(*, files=None):
    payload = {
        "unique_code": "offline-generic",
        "description": "Analyze supplied material",
        "difficulty": "easy",
        "level": 1,
        "total_score": 10,
        "flag_count": 1,
        "correct_flag_count": 0,
        "is_completed": False,
        "container_status": "stopped",
        "container_addr": [],
    }
    if files is not None:
        payload["files"] = files
    return payload


class PlatformFileMetadataTests(unittest.TestCase):
    def test_from_dict_and_task_builder_preserve_optional_files(self):
        files = ["capture.pcap", {"name": "challenge.bin", "url": "/opaque"}]
        challenge = Challenge.from_dict(_challenge_payload(files=files))

        self.assertEqual(challenge.files, files)
        task = build_task(challenge, "/tmp/platform-file-metadata")
        self.assertEqual(task.files, files)

    def test_absent_files_remains_empty_and_backwards_compatible(self):
        challenge = Challenge.from_dict(_challenge_payload())
        self.assertEqual(challenge.files, [])
        self.assertEqual(build_task(challenge, "/tmp/platform-file-metadata").files, [])

    def test_http_backend_uses_optional_files_from_existing_list_response(self):
        backend = TSecBenchHTTPBackend("http://platform.invalid", "token")
        response = _Response([_challenge_payload(files=["evidence.pcap"])])
        with patch.object(backend._session, "get", return_value=response):
            rows = backend.list_challenges()

        self.assertEqual(rows[0].files, ["evidence.pcap"])

    def test_generic_backend_maps_optional_files_field_without_attachment_protocol(self):
        backend = GenericOpenAPIBackend("http://platform.invalid", "token")
        response = _Response([_challenge_payload(files=[{"filename": "sample.zip"}])])
        with patch.object(backend, "_call", return_value=response):
            rows = backend.list_challenges()

        self.assertEqual(rows[0].files, [{"filename": "sample.zip"}])

    def test_sdk_backend_preserves_optional_files_attribute(self):
        item = SimpleNamespace(
            unique_code="offline-sdk",
            description="Analyze supplied material",
            files=["dump.raw"],
        )
        backend = object.__new__(TSecBenchSDKBackend)
        backend._get_client = lambda: SimpleNamespace(list_challenges=lambda: [item])

        rows = backend.list_challenges()
        self.assertEqual(rows[0].files, ["dump.raw"])


if __name__ == "__main__":
    unittest.main()
