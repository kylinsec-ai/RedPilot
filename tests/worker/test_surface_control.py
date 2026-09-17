"""M3 执行通道：会话内预算动作必须能送进 RPC 会话，且 print 回退不假装。

编排层产出动作（`orchestrator._surface_control`），pi_agent 只负责把帧写进
stdio；这里用会说 RPC 的假 pi 验证「帧真的到了对端」，并锁住 print 模式
**不消费** control 回调（无双向通道时不能把动作丢掉还以为发了）。
"""

from __future__ import annotations

import json
import stat
import time

import pytest


def _adapter_cfg(**changes):
    import dataclasses
    from redpilot.worker.adapter.config import SolverConfig as _SC
    return dataclasses.replace(_SC.from_env(), **changes)


_RPC_FAKE = r'''#!/usr/bin/env python3
import sys, json, os
marker = os.environ.get("CTRL_FAKE_MARKER", "")
if "--help" in sys.argv:
    print("Usage: pi [options]")
    print("  --mode <mode>   Output mode: text (default), json, or rpc")
    sys.exit(0)

def out(o):
    print(json.dumps(o), flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        cmd = json.loads(line)
    except ValueError:
        continue
    if marker:
        open(marker, "a").write(json.dumps(cmd) + "\n")
    t = cmd.get("type")
    if t == "prompt":
        out({"type": "tool_execution_start", "toolCallId": "c1",
             "toolName": "bash", "args": {"command": "nmap -sV 10.0.0.5"}})
        out({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
             "result": {"content": [{"type": "text", "text": "10.0.0.5:22 open ssh\n"}]},
             "isError": False})
        out({"type": "agent_end", "messages": [
            {"role": "assistant", "content": [], "stopReason": "stop"}]})
        out({"type": "agent_settled"})
'''

_PRINT_FAKE = r'''#!/usr/bin/env python3
import sys, json
print(json.dumps({"type": "tool_execution_start", "toolCallId": "c1",
                  "toolName": "bash", "args": {"command": "nmap 10.0.0.5"}}), flush=True)
print(json.dumps({"type": "tool_execution_end", "toolCallId": "c1",
                  "toolName": "bash",
                  "result": {"content": [{"type": "text", "text": "ok\n"}]},
                  "isError": False}), flush=True)
'''


def _write_fake(tmp_path, name, body) -> str:
    p = tmp_path / name
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


def test_control_frames_reach_rpc_session(tmp_path, monkeypatch):
    marker = tmp_path / "frames.jsonl"
    fake = _write_fake(tmp_path, "fakepi_rpc.py", _RPC_FAKE)
    monkeypatch.setenv("ADAPTER_PI_TRANSPORT", "rpc")
    monkeypatch.setenv("CTRL_FAKE_MARKER", str(marker))

    from redpilot.worker.adapter.solver import create_solver
    solver = create_solver(model="deepseek/prov-model")
    solver.cmd = fake
    workdir = tmp_path / "wd"
    workdir.mkdir()
    cfg = _adapter_cfg(model="deepseek/prov-model", session_seconds=30)

    calls = []

    def control():
        calls.append(1)
        if len(calls) > 1:
            return []
        return [{"type": "steer", "message": "## surface budget test"}]

    result = solver.solve("p", str(workdir), cfg, control=control,
                          transcript_path=str(tmp_path / "t.jsonl"))
    assert not result.error, f"control 通道引入了错误：{result.error!r}"
    assert calls, "RPC 会话下 control 回调从未被调用"

    frames = [json.loads(l) for l in marker.read_text().splitlines() if l.strip()]
    types = [f.get("type") for f in frames]
    assert "prompt" in types, "prompt 未经 stdin 送达"
    assert "steer" in types, "steer 帧没有到达 RPC 对端"
    steer = next(f for f in frames if f.get("type") == "steer")
    assert "surface budget test" in steer["message"]


def test_print_fallback_does_not_consume_control(tmp_path, monkeypatch):
    fake = _write_fake(tmp_path, "fakepi_print.py", _PRINT_FAKE)
    monkeypatch.setenv("ADAPTER_PI_TRANSPORT", "print")

    from redpilot.worker.adapter.solver import create_solver
    solver = create_solver(model="deepseek/prov-model")
    solver.cmd = fake
    workdir = tmp_path / "wd"
    workdir.mkdir()
    cfg = _adapter_cfg(model="deepseek/prov-model", session_seconds=30)

    calls = []

    def control():
        calls.append(1)
        return [{"type": "steer", "message": "should not be consumed"}]

    t0 = time.monotonic()
    solver.solve("p", str(workdir), cfg, control=control,
                 transcript_path=str(tmp_path / "t.jsonl"))
    assert time.monotonic() - t0 < 20
    assert calls == [], "print 无双向通道，不能消费动作（否则动作既没送达也没记账）"
