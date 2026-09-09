"""STATUS_BIND 解析回归:默认仅回环,compose 显式 0.0.0.0,垃圾值回退。"""

from __future__ import annotations

from tsecbench_worker.settings import WorkerSettings, _status_bind


def test_status_bind_default_loopback(monkeypatch):
    monkeypatch.delenv("STATUS_BIND", raising=False)
    assert _status_bind() == "127.0.0.1"
    assert WorkerSettings.from_env().status_bind == "127.0.0.1"


def test_status_bind_override(monkeypatch):
    monkeypatch.setenv("STATUS_BIND", "0.0.0.0")
    assert _status_bind() == "0.0.0.0"


def test_status_bind_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("STATUS_BIND", "-evil")
    assert _status_bind() == "127.0.0.1"
    assert WorkerSettings.from_env().status_bind == "127.0.0.1"
