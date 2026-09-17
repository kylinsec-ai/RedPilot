"""SurfaceLedger（M3，会话内攻击面预算）的确定性单测。

锁四条不变量（设计 docs/solver-isolation-design.md §6）：
  1. 分类确定性 + 防换皮（换工具写法不产生新面）；
  2. 动作阶梯：soft → close+steer → 无视 steer 后 abort；
  3. 救活记账：关闭后出现新事实记 rescued，但不自动重开；
  4. 持久化只认同 task epoch（失效但不丢弃）。
"""

from __future__ import annotations

import json

from redpilot.worker.adapter.config import SurfaceBudgetConfig
from redpilot.worker.adapter.surface import (
    SurfaceLedger, classify, ledger_path, surface_key,
)


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def adv(self, delta: float) -> None:
        self.t += delta


def _cfg(**kw) -> SurfaceBudgetConfig:
    base = dict(enabled=True, soft_seconds=180, hard_seconds=300,
                abs_seconds=900, ignore_seconds=120, ignore_calls=3)
    base.update(kw)
    return SurfaceBudgetConfig(**base)


# ── 分类表 ──

def test_classify_known_tools_and_targets():
    cases = [
        ({"command": "nmap -sV 10.0.0.5 -p 1-1000"}, ("10.0.0.5", "scan")),
        ({"command": "ffuf -u http://10.0.0.7:8080/FUZZ -w wl.txt"},
         ("10.0.0.7:8080", "brute")),
        ({"command": "curl -s http://10.0.0.7:8080/login"},
         ("10.0.0.7:8080", "http")),
        ({"command": "aws s3 ls s3://bucket/x"}, ("", "cloud_enum")),
        ({"command": "strings ./chall.bin"}, ("chall.bin", "analysis")),
        ({"command": "gdb ./a.out"}, ("a.out", "analysis")),
    ]
    for args, expected in cases:
        assert classify("bash", args) == expected


def test_anti_rekey_same_target_same_tactic_is_one_surface():
    a = surface_key("bash", {"command": "nmap -sS 10.0.0.5"})[0]
    b = surface_key("bash", {"command": "masscan 10.0.0.5 -p80"})[0]
    assert a == b, "换个扫描器不算换面（防换皮）"


def test_distinct_targets_are_distinct_surfaces():
    a = surface_key("bash", {"command": "nmap 10.0.0.5"})[0]
    b = surface_key("bash", {"command": "nmap 10.0.0.6"})[0]
    assert a != b


def test_read_tool_classified_by_path():
    assert classify("read", {"file_path": "/work/x/MEMORY.md"}) == ("memory.md", "analysis")


# ── 动作阶梯 ──

def test_ladder_soft_then_close_then_abort():
    clk = Clock()
    led = SurfaceLedger(_cfg(), "chal", epoch="e1", clock=clk)
    for i in range(5):
        led.note_result("bash", {"command": f"nmap -sV 10.0.0.5 -p {1000 + i}"},
                        "noise", now=clk())
    clk.adv(200)
    soft = led.tick()
    assert soft is not None and soft.kind == "soft"
    assert led.tick() is None, "软信号每面只发一次"

    clk.adv(150)                       # 累计无事实 ≥ hard_seconds
    close = led.tick()
    assert close is not None and close.kind == "steer" and close.reason == "stall"
    assert led.tick() is None, "steer 只送一次"

    for i in range(3):                 # 无视 steer，继续同面
        clk.adv(60)
        led.note_result("bash", {"command": f"nmap -Pn 10.0.0.5 -p {2000 + i}"},
                        "", now=clk())
    abort = led.tick()
    assert abort is not None and abort.kind == "abort"
    assert abort.reason == "ignored_steer"
    assert "已被框架关闭" in abort.text


def test_steer_is_delimited_and_evidence_free():
    clk = Clock()
    led = SurfaceLedger(_cfg(soft_seconds=1), "chal", epoch="e", clock=clk)
    for i in range(3):
        led.note_result("bash", {"command": f"nmap 10.0.0.5 -p {i}"}, "x", now=clk())
    clk.adv(1000)
    act = led.tick()
    assert act is not None
    for forbidden in ("flag{", "FLAG=", "http://", "shot from"):
        assert forbidden not in act.text


def test_absolute_cap_closes_even_with_facts():
    clk = Clock()
    led = SurfaceLedger(_cfg(abs_seconds=100, hard_seconds=99999), "chal",
                        epoch="e", clock=clk)
    for i in range(6):
        led.note_result("bash", {"command": f"gobuster dir -u http://h/{i}"},
                        "10.9.9.9:22 open ssh", now=clk())   # 持续产事实
        clk.adv(20)
    act = led.tick()
    assert act is not None and act.reason == "absolute"


def test_disabled_config_never_acts():
    clk = Clock()
    led = SurfaceLedger(_cfg(enabled=False), "chal", epoch="e", clock=clk)
    for i in range(10):
        led.note_result("bash", {"command": f"nmap 10.0.0.5 -p {i}"}, "x", now=clk())
    clk.adv(10000)
    assert led.tick() is None


# ── 救活与误杀记账 ──

def test_rescued_is_recorded_but_surface_stays_closed():
    clk = Clock()
    led = SurfaceLedger(_cfg(), "chal", epoch="e", clock=clk)
    for i in range(3):
        led.note_result("bash", {"command": f"nmap 10.0.0.5 -p {i}"}, "", now=clk())
    clk.adv(400)
    assert led.tick().kind == "steer"

    info = led.note_result("bash", {"command": "nmap -sV 10.0.0.5"},
                           "10.0.0.5:22 open ssh OpenSSH_8.9", now=clk())
    assert info["closed"] is True
    entry = led.entries[info["key"]]
    assert entry.rescued is True and entry.facts >= 1
    assert led.session_summary()["rescued"] == 1


# ── 未试家族 / 跨场注入 ──

def test_untried_tactics_excludes_tried_and_other():
    clk = Clock()
    led = SurfaceLedger(_cfg(), "chal", epoch="e", clock=clk)
    led.note_result("bash", {"command": "nmap 10.0.0.5"}, "x", now=clk())
    led.note_result("bash", {"command": "curl http://10.0.0.5/"}, "x", now=clk())
    untried = led.untried_tactics()
    assert "scan" not in untried and "http" not in untried
    assert "brute" in untried and "other" not in untried


# ── 持久化 ──

def test_persistence_roundtrip_same_epoch_and_epoch_invalidation(tmp_path):
    clk = Clock()
    path = str(tmp_path / ".harness" / "surface" / "chal.json")
    led = SurfaceLedger(_cfg(), "chal", epoch="e1", persist_path=path, clock=clk)
    for i in range(3):
        led.note_result("bash", {"command": f"nmap 10.0.0.5 -p {i}"}, "", now=clk())
    clk.adv(400)
    assert led.tick().kind == "steer"
    led.save()

    same = SurfaceLedger(_cfg(), "chal", epoch="e1", persist_path=path, clock=clk)
    assert len(same.entries) == 1
    assert same.entries[next(iter(same.entries))].closed_reason == "stall"

    other = SurfaceLedger(_cfg(), "chal", epoch="e2", persist_path=path, clock=clk)
    assert other.entries == {}, "旧 epoch 的面预算不得续用"


def test_ledger_path_is_control_plane(tmp_path):
    p = ledger_path(str(tmp_path), "my-code", lambda c: c)
    assert p == str(tmp_path / ".harness" / "surface" / "my-code.json")
    assert "/.harness/" in p


def test_saved_file_contains_no_command_text(tmp_path):
    clk = Clock()
    path = str(tmp_path / ".harness" / "surface" / "chal.json")
    led = SurfaceLedger(_cfg(), "chal", epoch="e", persist_path=path, clock=clk)
    led.note_result("bash", {"command": "nmap -sV 10.0.0.5"}, "10.0.0.5:22 open", now=clk())
    led.save()
    raw = json.loads((tmp_path / ".harness" / "surface" / "chal.json").read_text())
    blob = json.dumps(raw)
    assert "nmap -sV" not in blob and "command" not in blob
