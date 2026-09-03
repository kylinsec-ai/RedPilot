"""Playwright end-to-end fixtures for the TSecBench Range Console.

A session-scoped fixture boots the real FastAPI server (main.py) against a
temporary SQLite database and a seeded task catalog, so the browser drives the
actual running API rather than a mock.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "e2e-token"
CHALLENGES = [
    {
        "unique_code": "web_sql_injection_01",
        "description": "通过 SQL 注入获取管理员凭证并读取 flag",
        "difficulty": "easy",
        "level": 1,
        "total_score": 100,
        "hint": "尝试在登录表单的用户名字段使用单引号测试注入点",
        "hint_cost_radio": 0.3,
        "container_addr": ["10.0.1.5:8080"],
        "flags": [
            {"value": "flag{admin_sql}", "score": 40},
            {"value": "flag{second_sql}", "score": 60},
        ],
    },
    {
        "unique_code": "crypto_rsa_02",
        "description": "RSA 参数不当导致私钥可恢复",
        "difficulty": "hard",
        "level": 3,
        "total_score": 200,
        "hint": "检查模数是否可分解",
        "hint_cost_radio": 0.2,
        "container_addr": ["10.0.2.10:443"],
        "flags": ["flag{rsa_priv}"],
    },
    {
        "unique_code": "rev_maze_03",
        "description": "逆向迷宫程序找到出口 flag",
        "difficulty": "medium",
        "level": 2,
        "total_score": 150,
        "hint": "跟踪出口分支的条件",
        "hint_cost_radio": 0.5,
        "container_addr": [],
        "flags": ["flag{maze_end}"],
    },
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_ready(url: str, proc: subprocess.Popen[bytes], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            pytest.fail(f"server exited early with code {proc.returncode}:\n{output}")
        try:
            if requests.get(url + "/", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.2)
    proc.terminate()
    pytest.fail("server did not become ready in time")


@pytest.fixture
def server_url(tmp_path) -> str:
    """Boot a fresh server per test (fresh DB) and yield its base URL.

    Function scope gives each test an isolated database, so container state and
    submitted flags from one test never leak into the next.
    """
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps({"token": TOKEN, "challenges": CHALLENGES}), encoding="utf-8")
    db_path = tmp_path / "e2e.sqlite3"

    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "TSECBENCH_DB_PATH": str(db_path),
            "TSECBENCH_CONFIG": str(tasks_file),
            "HOST": "127.0.0.1",
            "PORT": str(port),
            # 隔离本地 e2e：禁用远程 .env 配置，让前端走本地 API；
            # 同时钉死 provisioner 与活跃上限，防止宿主机导出值（如
            # TSECBENCH_PROVISIONER=docker）泄漏进被测服务器
            "BENCHMARK_BASE_URL": "",
            "BENCHMARK_TOKEN": "",
            "TSECBENCH_PROVISIONER": "static",
            "TSECBENCH_MAX_ACTIVE_CHALLENGES": "3",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "main.py")],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    url = f"http://127.0.0.1:{port}"
    _wait_until_ready(url, proc)
    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
