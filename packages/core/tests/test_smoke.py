"""core 冒烟测试:应用工厂可构建、配置可解析(无网络/无 DB 副作用)。"""

from __future__ import annotations


def test_create_app():
    from tsecbench.api import create_app
    app = create_app()
    assert app is not None


def test_settings_from_env(monkeypatch):
    from tsecbench.config import Settings
    monkeypatch.setenv("TSECBENCH_DB_PATH", "/tmp/tsecbench-smoke.sqlite3")
    s = Settings.from_env()
    assert s is not None
