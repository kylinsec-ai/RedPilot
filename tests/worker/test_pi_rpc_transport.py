"""RPC 传输回归：argv 形状 / 能力探测 / agent_settled 收尾 / 扩展弹窗自动回应。

背景（`docs/pi-rpc-migration-research.md`）：求解引擎从 `--mode json --print`
（一次性）换成 `--mode rpc`（常驻 JSONL）。三个语义差异由 `pi_transport.py` 吸收：

1. RPC 进程**不自己退出** → 必须靠 `agent_settled` 收尾并显式关停；
2. stdout 混入 `response` / `extension_ui_request` → 前者丢弃、后者自动应答
   （不答就整场挂死）；
3. prompt 走 stdin 而非 argv。

`test_provider_failure_guard.py` 的假 pi **不认** `--mode rpc`，因此它走的是
print 回退分支 —— RPC 分支需要本文件这组用**会说 RPC 的假 pi** 覆盖。
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
marker = os.environ.get("FAKE_RPC_MARKER", "")
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
    t = cmd.get("type")
    if t == "prompt":
        # 协议帧：response 必须被传输层丢掉，不能进事件流
        out({"type": "response", "id": cmd.get("id"), "command": "prompt", "success": True})
        if os.environ.get("FAKE_RPC_SILENT"):
            # 只接受 prompt 然后永远静默：复现真实 pi 在模型/凭据不可解析时的形态
            import time as _t
            while True:
                _t.sleep(3600)
        out({"type": "tool_execution_start", "toolCallId": "c1",
             "toolName": "bash", "args": {"command": "echo hi"}})
        out({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
             "result": {"content": [{"type": "text", "text": "hi\n"}]}, "isError": False})
        # 扩展弹窗：客户端不回 = 本进程永远不推进（模拟挂死）
        out({"type": "extension_ui_request", "id": "u1", "method": "confirm",
             "title": "ok?", "message": "?"})
    elif t == "extension_ui_response":
        if marker:
            open(marker, "w").write(json.dumps(cmd))
        out({"type": "agent_end",
             "messages": [{"role": "assistant", "content": [], "stopReason": "stop"}]})
        out({"type": "agent_settled"})
        # 刻意**不退出**：常驻进程由 transport.shutdown() 关停
    elif t == "abort":
        sys.exit(0)
'''


def _write_rpc_fake(tmp_path, marker_path) -> str:
    p = tmp_path / "fakepi_rpc.py"
    p.write_text(_RPC_FAKE)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# ── 1. argv 形状（纯函数，最便宜的一条）──

def test_build_cmd_rpc_omits_prompt_and_print_flag():
    from redpilot.worker.adapter.solver.pi_agent import PiAgentBackend
    b = PiAgentBackend(cmd="/bin/true")
    cmd = b._build_cmd("PROMPT_TEXT", "", transport="rpc")
    assert cmd[1:4] == ["--mode", "rpc", "--no-session"]
    assert "--print" not in cmd
    assert "PROMPT_TEXT" not in cmd, "rpc 的 prompt 必须走 stdin，不能拼进 argv"


def test_build_cmd_print_keeps_original_shape():
    from redpilot.worker.adapter.solver.pi_agent import PiAgentBackend
    b = PiAgentBackend(cmd="/bin/true")
    cmd = b._build_cmd("PROMPT_TEXT", "", transport="print")
    assert cmd[1:5] == ["--mode", "json", "--print", "--no-session"]
    assert cmd[-1] == "PROMPT_TEXT"


# ── 2. 能力探测：老 pi 无 rpc 时安全回退 ──

def test_rpc_available_true_for_rpc_capable_pi(tmp_path):
    from redpilot.worker.adapter.solver.pi_transport import rpc_available
    fake = tmp_path / "hasrpc.sh"
    fake.write_text('#!/bin/sh\necho "  --mode <mode>  Output mode: text, json, or rpc"\n')
    fake.chmod(0o755)
    assert rpc_available(str(fake)) is True


def test_rpc_available_false_for_old_pi(tmp_path):
    from redpilot.worker.adapter.solver.pi_transport import rpc_available
    fake = tmp_path / "oldpi.sh"
    fake.write_text('#!/bin/sh\necho "  --mode <mode>  Output mode: text or json"\n')
    fake.chmod(0o755)
    assert rpc_available(str(fake)) is False


# ── 3. RPC 全链路：settled 收尾 + 关停 + 弹窗自动回应 ──

def test_rpc_solve_settles_shuts_down_and_answers_ui(tmp_path, monkeypatch):
    """假 RPC pi 只在收到 extension_ui_response 后才发 agent_settled。

    因此这条同时证明三件事：prompt 经 stdin 送达（turns=1）、
    扩展弹窗被自动拒绝（否则永远等不到 settled）、
    solve 靠 agent_settled 收尾并关停常驻进程（否则会耗到会话 deadline）。
    """
    marker = tmp_path / "ui_response.json"
    fake = _write_rpc_fake(tmp_path, marker)
    monkeypatch.setenv("ADAPTER_PI_TRANSPORT", "rpc")
    monkeypatch.setenv("FAKE_RPC_MARKER", str(marker))

    from redpilot.worker.adapter.solver import create_solver
    solver = create_solver(model="deepseek/prov-model")
    solver.cmd = fake

    workdir = tmp_path / "wd"
    workdir.mkdir()
    cfg = _adapter_cfg(model="deepseek/prov-model", session_seconds=30)

    t0 = time.monotonic()
    result = solver.solve("p", str(workdir), cfg,
                          transcript_path=str(tmp_path / "t.jsonl"))
    elapsed = time.monotonic() - t0

    assert result.turns == 1, "prompt 未经 stdin 送达 RPC 进程"
    assert not result.error, f"RPC 会话留下了错误：{result.error!r}"
    assert elapsed < 15, (
        f"RPC 未按 agent_settled 收尾（{elapsed:.1f}s）—— "
        "常驻进程没被关停，白等到会话 deadline")
    assert marker.exists(), "扩展弹窗未被自动回应（无人值守会整场挂死）"
    assert json.loads(marker.read_text()).get("confirmed") is False, \
        "无人值守时扩展弹窗应一律回 confirmed=false"


def test_rpc_startup_guard_fails_fast_on_silent_agent(tmp_path, monkeypatch):
    """prompt 被接受但一个事件都没有 → 提前判死，而不是等会话 deadline。

    实测形态（真实 pi 0.85.1 + 不可解析的 --model）：RPC 只回 prompt 的
    `response success`，之后**不发 agent_start、不报错、也不退出**。print 模式下
    这种错会非零退出被 0-turn 护栏接住；RPC 常驻所以只能靠本护栏。
    """
    marker = tmp_path / "ui.json"
    fake = _write_rpc_fake(tmp_path, marker)
    monkeypatch.setenv("ADAPTER_PI_TRANSPORT", "rpc")
    monkeypatch.setenv("FAKE_RPC_SILENT", "1")
    monkeypatch.setenv("ADAPTER_RPC_STARTUP_GRACE", "2")

    from redpilot.worker.adapter.solver import create_solver
    solver = create_solver(model="deepseek/prov-model")
    solver.cmd = fake
    workdir = tmp_path / "wd"
    workdir.mkdir()
    # 会话预算给得足够大：若护栏失效，本用例会耗到 deadline 才失败
    cfg = _adapter_cfg(model="deepseek/prov-model", session_seconds=600)

    t0 = time.monotonic()
    result = solver.solve("p", str(workdir), cfg)
    elapsed = time.monotonic() - t0

    assert result.termination_reason == "error"
    assert result.error == "rpc_no_agent_start"
    assert elapsed < 30, f"启动护栏未生效，白等了 {elapsed:.1f}s"


def test_rpc_transcript_excludes_protocol_frames(tmp_path, monkeypatch):
    """转录里不得出现 response / extension_ui_request —— 它们是协议帧不是事件。"""
    marker = tmp_path / "ui.json"
    fake = _write_rpc_fake(tmp_path, marker)
    monkeypatch.setenv("ADAPTER_PI_TRANSPORT", "rpc")
    monkeypatch.setenv("FAKE_RPC_MARKER", str(marker))

    from redpilot.worker.adapter.solver import create_solver
    solver = create_solver(model="deepseek/prov-model")
    solver.cmd = fake
    workdir = tmp_path / "wd"
    workdir.mkdir()
    tpath = tmp_path / "t.jsonl"
    cfg = _adapter_cfg(model="deepseek/prov-model", session_seconds=30)
    solver.solve("p", str(workdir), cfg, transcript_path=str(tpath))

    text = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
    assert '"response"' not in text
    assert "extension_ui_request" not in text
    assert "agent_settled" in text or "tool_execution" in text, \
        "转录里应当留下真实事件"
