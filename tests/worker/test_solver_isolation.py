"""M1/M2 执行点：身份降权参数、控制面目录、预算状态搬家与兼容读取。

设计 `docs/solver-isolation-design.md` §4–§5。非 root / 无 solver 用户的
环境（如本机 Termux）自动跳过需要真降权的用例，但**不需要权限的不变量**
（配置解析、路径单源、Popen 参数透传、legacy 兼容）必须始终生效。
"""

from __future__ import annotations

import json
import os
import pwd
import stat
from pathlib import Path

import pytest

from redpilot.worker.adapter import isolation
from redpilot.worker.adapter.config import IsolationConfig
from redpilot.worker.adapter.solver import pi_transport
from redpilot.worker.adapter.stoploss import StopLoss

_HAS_SOLVER = False
try:
    _HAS_SOLVER = pwd.getpwnam("solver").pw_uid > 0
except KeyError:
    pass

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_CAN_DROP = _HAS_SOLVER and _IS_ROOT


# ── 配置与身份解析 ──

def test_isolation_config_defaults(monkeypatch):
    for key in ("ADAPTER_ISOLATION", "ADAPTER_ISOLATION_STRICT", "ADAPTER_SOLVER_USER"):
        monkeypatch.delenv(key, raising=False)
    cfg = IsolationConfig.from_env()
    assert cfg.enabled is True and cfg.strict is False and cfg.user == "solver"

    monkeypatch.setenv("ADAPTER_ISOLATION", "0")
    monkeypatch.setenv("ADAPTER_ISOLATION_STRICT", "1")
    monkeypatch.setenv("ADAPTER_SOLVER_USER", "runner")
    cfg = IsolationConfig.from_env()
    assert cfg == IsolationConfig(enabled=False, strict=True, user="runner")


def test_resolve_identity_disabled_returns_empty():
    assert isolation.resolve_identity(IsolationConfig(enabled=False)) == {}


def test_resolve_identity_missing_user_returns_empty():
    assert isolation.resolve_identity(
        IsolationConfig(enabled=True, user="definitely-not-a-user-xyz")) == {}


def test_isolation_module_parses_no_env():
    """R7：env 解析只允许在 config/settings 的收集点。"""
    src = Path(isolation.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in src and "getenv" not in src


# ── 控制面目录（M2）──

def test_prepare_control_dirs_creates_and_locks(tmp_path):
    isolation.prepare_control_dirs(str(tmp_path))
    for rel in (".harness", ".harness/stoploss", ".harness/surface",
                ".stoploss-locks", ".live", "status"):
        p = tmp_path / rel
        assert p.is_dir(), f"{rel} missing"
    mode = stat.S_IMODE((tmp_path / ".harness").stat().st_mode)
    assert mode == 0o700, oct(mode)


def test_stoploss_state_lives_in_harness_not_challenge_dir(tmp_path):
    sl = StopLoss(workdir=str(tmp_path), dry_cutoff=3)
    sl.start("code-1")
    sl.record_no_progress("code-1")
    harness = tmp_path / ".harness" / "stoploss" / "code-1.json"
    assert harness.is_file()
    assert not (tmp_path / "code-1" / ".stoploss.json").exists()


def test_stoploss_reads_legacy_state_and_migrates_on_save(tmp_path):
    legacy = tmp_path / "code-1"
    legacy.mkdir()
    (legacy / ".stoploss.json").write_text(
        json.dumps({"sessions": 4, "dry_sessions": 2, "total_facts": 7}),
        encoding="utf-8")
    sl = StopLoss(workdir=str(tmp_path), dry_cutoff=3)
    st = sl._read_state("code-1")
    assert (st.sessions, st.dry_sessions, st.total_facts) == (4, 2, 7)

    sl.record_no_progress("code-1")          # 触发一次保存 → 单向迁移
    harness = tmp_path / ".harness" / "stoploss" / "code-1.json"
    assert harness.is_file()
    assert (legacy / ".stoploss.json").is_file(), "旧文件保留作证据，不删"


def test_stoploss_new_path_wins_over_legacy(tmp_path):
    legacy = tmp_path / "code-1"
    legacy.mkdir()
    (legacy / ".stoploss.json").write_text(json.dumps({"sessions": 1}), encoding="utf-8")
    sl = StopLoss(workdir=str(tmp_path), dry_cutoff=3)
    hdir = tmp_path / ".harness" / "stoploss"
    hdir.mkdir(parents=True)
    (hdir / "code-1.json").write_text(json.dumps({"sessions": 9}), encoding="utf-8")
    assert sl._read_state("code-1").sessions == 9


# ── 传输层身份透传（M1 的执行点）──

def test_print_transport_passes_identity_to_popen(monkeypatch):
    seen: dict = {}

    class _FakeProc:
        pass

    def _fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        seen.update(kw)
        return _FakeProc()

    monkeypatch.setattr(pi_transport.subprocess, "Popen", _fake_popen)
    pi_transport.PrintTransport(
        ["pi"], prompt="hello", workdir="/tmp", env={},
        stop_fn=lambda *a, **k: None,
        identity={"user": 10001, "group": 10001, "extra_groups": []})
    assert seen["user"] == 10001 and seen["group"] == 10001
    assert seen["cmd"] == ["pi", "hello"]


def test_make_transport_keeps_identity_on_print_fallback(monkeypatch):
    seen: dict = {}

    class _FakeProc:
        pass

    def _fake_popen(cmd, **kw):
        seen.update(kw)
        return _FakeProc()

    monkeypatch.setattr(pi_transport.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(pi_transport, "rpc_available", lambda _p: False)
    pi_transport.make_transport(
        transport="rpc", cmd_base=["pi"], prompt="x", workdir="/tmp",
        env={}, stop_fn=lambda *a, **k: None, identity={"user": 7, "group": 7})
    assert seen.get("user") == 7, "print 回退不得丢掉降权身份"


# ── 需要真降权身份的用例（容器/CI 里跑，本机自动跳过）──

@pytest.mark.skipif(not _CAN_DROP, reason="needs root + solver user")
def test_probe_passes_under_solver_identity(tmp_path):
    isolation.prepare_control_dirs(str(tmp_path))
    cfg = IsolationConfig(enabled=True, user="solver")
    report = isolation.probe(str(tmp_path), cfg)
    assert report["identity"] is True
    assert report["writable_tmp"] is True
    assert report["control_readonly"] is True


@pytest.mark.skipif(not _CAN_DROP, reason="needs root + solver user")
def test_challenge_dir_is_writable_only_for_solver(tmp_path):
    cfg = IsolationConfig(enabled=True, user="solver")
    isolation.prepare_control_dirs(str(tmp_path))
    mode = isolation.prepare_challenge_dir(str(tmp_path), "chal", cfg, lambda c: c)
    assert mode in ("acl", "chown")
    pw = pwd.getpwnam("solver")
    own = os.stat(tmp_path / "chal").st_uid
    assert own == pw.pw_uid or mode == "acl"
