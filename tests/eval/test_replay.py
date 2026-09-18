"""回放层的回归守卫。

回放是评估面唯一的数据入口，所以它的每一条断言都在保护"下游判据看到的
世界是对的"。下面几条是真实踩过的坑，不是凑覆盖率：

- `SKILL.md` 前那个**路径末尾的斜杠**：`/app/skills/hack/SKILL.md` 里，
  若拿最后一个 `/` 当边界，取出来的技能名是空串 —— 整份路由评测会静默
  变成"agent 从没读过任何技能"。
- **非 bash 工具的 `cmd` 是 JSON 参数**而不是命令。拿它去判"有没有联网
  下载"，会把一个把 URL 写进文件内容的 write 调用判成下载。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from redpilot.eval.replay import (Replayer, Trace, skill_name_from_cmd,
                               trace_from_rows, trace_from_transcript)


def ev(**kw) -> dict:
    """一条 pi 原生事件 → 事件行的形状（fold_rows 吃 {payload: json}）。"""
    return {"payload": json.dumps(kw)}


def tool_start(tool: str, call_id: str, **args) -> dict:
    return ev(type="tool_execution_start", toolName=tool,
              toolCallId=call_id, args=args)


def tool_end(call_id: str, text: str = "ok", is_error: bool = False) -> dict:
    return ev(type="tool_execution_end", toolCallId=call_id, isError=is_error,
              result={"content": [{"text": text}]})


def turn_end(tokens: int) -> dict:
    return ev(type="turn_end", message={"usage": {"totalTokens": tokens}})


class SkillNameExtractionTests(unittest.TestCase):
    """从工具参数摘要里认技能名 —— 两种 `cmd` 形态都要认。"""

    def test_json_args_form(self):
        self.assertEqual(
            skill_name_from_cmd('{"path": "/app/skills/hack/SKILL.md"}'), "hack")

    def test_bash_form(self):
        self.assertEqual(
            skill_name_from_cmd("cat /app/skills/rsa-attack-techniques/SKILL.md"),
            "rsa-attack-techniques")

    def test_trailing_slash_regression(self):
        """回归：`/app/skills/hack/` 末尾那个斜杠不是技能名的边界。"""
        for cmd in ("cat /app/skills/hack/SKILL.md",
                    '{"file_path": "/app/skills/hack/SKILL.md"}',
                    "cat /root/.pi/agent/skills/hack/SKILL.md"):
            with self.subTest(cmd=cmd):
                self.assertEqual(skill_name_from_cmd(cmd), "hack")

    def test_relative_and_quoted_forms(self):
        self.assertEqual(skill_name_from_cmd("cat ./skills/web-attack/SKILL.md"),
                         "web-attack")
        self.assertEqual(skill_name_from_cmd("cat '/x/skills/sqli-sql-injection/SKILL.md'"),
                         "sqli-sql-injection")

    def test_non_skill_reads_return_none(self):
        for cmd in ("ls -la /tmp", "", "cat /etc/passwd", "SKILL.md",
                    "grep -r SKILL.md /app"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(skill_name_from_cmd(cmd))


class TraceViewTests(unittest.TestCase):
    """时间线切片的语义 —— 判据全靠它们。"""

    def _trace(self):
        rows = [
            ev(type="session", id="abcdef123456", cwd="/work/pwnbox"),
            ev(type="turn_start"),
            tool_start("bash", "t1", command="nmap -Pn 10.0.0.5"),
            tool_end("t1", text="22/tcp open ssh"),
            turn_end(1000),
            ev(type="turn_start"),
            tool_start("read", "t2", path="/app/skills/hack/SKILL.md"),
            tool_end("t2", text="# HACKING SKILLS"),
            tool_start("bash", "t3", command="nmap -Pn 10.0.0.5"),   # 重复
            tool_end("t3", text="22/tcp open ssh"),
            tool_start("bash", "t4", command="nmap -Pn 10.0.0.6"),
            tool_end("t4", text="connection refused", is_error=True),
            turn_end(2000),
            ev(type="agent_end"),
        ]
        return trace_from_rows({"run_id": "a" * 32, "challenge_code": "pwnbox",
                                "status": "solved", "turns": 2,
                                "flags_accepted": ["flag{x}"]}, rows)

    def test_tool_calls_are_typed(self):
        t = self._trace()
        self.assertEqual(len(t.tool_calls), 4)
        self.assertEqual(t.tool_calls[0].tool, "bash")
        self.assertEqual(t.tool_calls[0].cmd, "nmap -Pn 10.0.0.5")
        self.assertFalse(t.tool_calls[0].err)
        self.assertTrue(t.tool_calls[3].err)

    def test_bash_commands_exclude_non_bash_tools(self):
        """`commands` 含 JSON 参数摘要，`bash_commands` 只含真命令。

        判断"有没有联网下载"必须用后者：read/write 的 `cmd` 是 JSON，
        把 URL 写进文件内容不是下载。
        """
        t = self._trace()
        self.assertEqual(len(t.commands), 4)
        self.assertEqual(len(t.bash_commands), 3)
        self.assertNotIn("/app/skills/hack/SKILL.md", " ".join(t.bash_commands))
        self.assertIn("/app/skills/hack/SKILL.md", " ".join(t.commands))

    def test_skill_reads(self):
        t = self._trace()
        self.assertEqual(t.skill_reads, ("hack",))

    def test_total_tokens_is_sum_of_turns(self):
        self.assertEqual(self._trace().total_tokens, 3000)

    def test_total_tokens_is_a_lower_bound(self):
        """没有 usage 的 turn 计 0 —— 预算是判"超了没有"，下界即够（见属性 docstring）。"""
        t = trace_from_rows({}, [ev(type="turn_start"), ev(type="turn_end")])
        self.assertEqual(t.total_tokens, 0)

    def test_repeated_commands_normalizes_whitespace(self):
        rows = [tool_start("bash", "a", command="nmap  -Pn   10.0.0.5"),
                tool_start("bash", "b", command="nmap -Pn 10.0.0.5"),
                tool_start("bash", "c", command="ls")]
        t = trace_from_rows({}, rows)
        self.assertEqual(t.repeated_commands, {"nmap -Pn 10.0.0.5": 2})

    def test_sessions_and_errors(self):
        t = trace_from_rows({}, [ev(type="session", id="abc"),
                                 ev(type="error", message="boom")])
        self.assertEqual(len(t.sessions), 1)
        self.assertEqual(len(t.errors), 1)

    def test_run_metadata_defaults_are_empty_not_none(self):
        """缺字段一律给"空" —— 判据少写一层防御。"""
        t = trace_from_rows({}, [])
        self.assertEqual(t.run_id, "")
        self.assertEqual(t.flags_accepted, ())
        self.assertEqual(t.duration_s, 0.0)
        self.assertEqual(t.tool_errors, 0)

    def test_flags_accepted_passthrough(self):
        t = trace_from_rows({"flags_accepted": ["flag{a}", "flag{b}"]}, [])
        self.assertEqual(t.flags_accepted, ("flag{a}", "flag{b}"))

    def test_bad_payload_rows_are_counted_not_fatal(self):
        """一条脏行不该废掉整场回放 —— 计数进 meta.unparsed。"""
        t = trace_from_rows({}, [{"payload": "not json"}, ev(type="agent_end")])
        self.assertEqual(t.meta.get("unparsed"), 1)


class TranscriptLoaderTests(unittest.TestCase):
    """从 pi 原生 transcript.jsonl 回放（没有观测库时的那条路）。"""

    def test_loads_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "transcript.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "session", "id": "s1"}) + "\n")
                fh.write("\n")                       # 空行
                fh.write(json.dumps({"type": "turn_start"}) + "\n")
                fh.write(json.dumps({"type": "turn_end",
                                     "message": {"usage": {"totalTokens": 7}}}) + "\n")
            t = trace_from_transcript(path)
            self.assertEqual(len(t.turns), 1)
            self.assertEqual(t.total_tokens, 7)
            self.assertEqual(len(t.sessions), 1)

    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            trace_from_transcript("/nonexistent/transcript.jsonl")


class _FakeStore:
    """按 run_events 的真实分页契约造一个假库（{events,next_seq,end}）。

    `listed` 与 `runs` 刻意分开：真实场景里 `list_runs` 列出的是快照，
    而 `run_row` 读的是当下 —— 两者之间可以发生删除。合成一个 dict
    就模拟不出这个窗口，那条跳过分支也就永远测不到。
    """

    def __init__(self, runs: dict, events: dict, page: int = 2, listed=None):
        self._runs, self._events, self._page = runs, events, page
        self._listed = list(runs.values()) if listed is None else listed
        self.calls: list[int] = []

    def run_row(self, run_id):
        return self._runs.get(run_id)

    def run_events(self, run_id, after=0, limit=500):
        self.calls.append(after)
        rows = self._events.get(run_id, [])
        window = rows[after:after + self._page]
        return {"events": window,
                "next_seq": (window[-1]["seq"] + 1) if window else after,
                "end": len(window) < self._page}

    def list_runs(self, status=None, worker=None, challenge=None, limit=200):
        return self._listed[:limit]


class ReplayerTests(unittest.TestCase):
    """回放器：分页取全、run 不存在要响亮。"""

    def _store(self):
        rows = [{"seq": i, "type": "x", "payload": json.dumps({"type": "turn_start"})}
                for i in range(5)]
        runs = {"a" * 32: {"run_id": "a" * 32, "challenge_code": "c1",
                           "status": "solved"}}
        return _FakeStore(runs, {"a" * 32: rows})

    def test_trace_pages_until_end(self):
        store = self._store()
        t = Replayer(store).trace("a" * 32)
        self.assertEqual(len(t.turns), 5, "分页没有取全")
        self.assertGreater(len(store.calls), 1, "应该发生过分页")

    def test_missing_run_raises(self):
        """不存在必须是 KeyError —— 返回空轨迹会让判据对"没有数据"给出绿灯。"""
        with self.assertRaises(KeyError):
            Replayer(self._store()).trace("deadbeef" * 4)

    def test_traces_skips_runs_deleted_mid_listing(self):
        """列出到回放之间被并发删掉的 run：跳过，不炸整批。"""
        store = self._store()
        store._listed.append({"run_id": "b" * 32})        # 列表里有，run_row 里没有
        out = Replayer(store).traces()
        self.assertEqual([t.run_id for t in out], ["a" * 32])


if __name__ == "__main__":
    unittest.main()
