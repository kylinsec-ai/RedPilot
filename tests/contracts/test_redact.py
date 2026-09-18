"""脱敏单测:key 名抹除 / 递归 / 字符串对抹除 / 截断上限。(自 obs/tests 迁入)"""

from __future__ import annotations

from redpilot.contracts.redact import summarize_args
from redpilot.contracts.text import ARGS_SUMMARY_MAX


def test_redact_key_names():
    args = {"command": "curl -s https://x", "apiKey": "sk-12345",
            "password": "p@ss", "nested": {"token": "t", "keep": "v"}}
    s = summarize_args(args)
    assert '"***"' in s
    assert "sk-12345" not in s and "p@ss" not in s
    assert "curl -s https://x" in s  # 无敏感 key 名的字段不受影响
    assert '"keep": "v"' in s
    assert '"nested"' in s


def test_redact_list_values():
    args = {"auth": ["a", "b"], "urls": ["u1", "u2"]}
    s = summarize_args(args)
    assert '"a"' not in s and '"b"' not in s
    assert '"u1"' in s and '"u2"' in s


def test_redact_string_form_pair():
    s = summarize_args('{"apiKey":"abc","secret":"xyz","cmd":"ls"}')
    assert '"***"' in s
    assert '"abc"' not in s and '"xyz"' not in s
    assert '"ls"' in s


def test_truncation_cap():
    long = {"k": "x" * 2000}
    s = summarize_args(long)
    # head_text:前 max_len 字符 + "…"
    assert len(s) == ARGS_SUMMARY_MAX + 1
    assert s.endswith("…")


def test_plain_string_passthrough():
    assert summarize_args("plain text string") == "plain text string"
    s = summarize_args("token: abc123 " * 500)  # 大字符串先截断再正则(防灾难性回溯)
    assert len(s) == ARGS_SUMMARY_MAX + 1
    assert s.endswith("…")


def test_large_string_truncation_before_regex():
    huge_str = '{"token":"secret_val"}' + "a" * 100000
    s = summarize_args(huge_str, max_len=100)
    assert len(s) <= 101
    assert '"***"' in s
    assert "secret_val" not in s
