"""VPN 管理端点安全回归:admin 凭据隔离 + 上传配置指令校验。

背景:participant 任务 token 原先即可改写/重启平台全局 openvpn(以 root 运行),
配合 script-security/up 等指令即 root RCE(见安全审查 Vuln 1)。修复 =
REDPILOT_ADMIN_TOKEN 独立管理凭据(fail closed)+ 危险指令拒收 + argv 钉死
--script-security 1。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from redpilot.control.api import create_app
from redpilot.control.config import Settings

TASK_TOKEN = "task-tok"
ADMIN_TOKEN = "adm-tok"

BENIGN_CONFIG = """\
# 常规客户端配置(注释/空行/无害指令)
client
remote vpn.example.com 1194 udp
dev tun
proto udp
ca ca.crt
cert client.crt
key client.key
nobind

"""


def _app(tmp_path, *, admin_token: str | None = ADMIN_TOKEN):
    settings = Settings(database_path=str(tmp_path / "db.sqlite3"),
                        config_path=None, inline_config=None,
                        benchmark_token=TASK_TOKEN, admin_token=admin_token)
    return create_app(settings=settings,
                      tasks=[{"token": TASK_TOKEN, "challenges": []}])


@pytest.fixture()
def client(tmp_path):
    return TestClient(_app(tmp_path))


def test_vpn_status_requires_admin_token(client: TestClient):
    # 无凭据 / 仅参与方任务 token → 一律 401(任务 token 不再触达管理端点)
    assert client.get("/openapi/v1/vpn/status").status_code == 401
    assert client.get("/openapi/v1/vpn/status",
                      headers={"BENCHMARK_TOKEN": TASK_TOKEN}).status_code == 401


def test_vpn_config_requires_admin_token(client: TestClient):
    r = client.post("/openapi/v1/vpn/config", json={"content": BENIGN_CONFIG},
                    headers={"BENCHMARK_TOKEN": TASK_TOKEN})
    assert r.status_code == 401


def test_vpn_endpoints_fail_closed_when_admin_token_unset(tmp_path):
    app = _app(tmp_path, admin_token=None)
    with TestClient(app) as c:
        r = c.get("/openapi/v1/vpn/status")
        assert r.status_code == 503
        assert r.json()["code"] == "admin_token_not_configured"


def test_vpn_status_ok_with_admin_token(client: TestClient):
    r = client.get("/openapi/v1/vpn/status",
                   headers={"REDPILOT_ADMIN_TOKEN": ADMIN_TOKEN})
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_vpn_config_accepts_benign_and_rejects_dangerous(tmp_path):
    db = tmp_path / "db.sqlite3"
    with TestClient(_app(tmp_path)) as c:
        h = {"REDPILOT_ADMIN_TOKEN": ADMIN_TOKEN}

        ok = c.post("/openapi/v1/vpn/config", json={"content": BENIGN_CONFIG}, headers=h)
        assert ok.status_code == 200
        assert ok.json()["configured"] is True
        assert (tmp_path / "vpn" / "client.ovpn").read_text(encoding="utf-8") == BENIGN_CONFIG

        # 脚本执行指令族 → 400 拒收,原配置不被覆盖
        for evil in (
            "script-security 3\nup /bin/sh -c 'id'\n",
            "up /bin/sh -c 'id'\n",
            "route-up /etc/openvpn/up.sh\n",
            "down /bin/sh\n",
            "tls-verify /bin/echo pwned\n",
            "plugin /tmp/evil.so\n",
            "config /tmp/evil.ovpn\n",
            "log /etc/pwned\n",
            "log-append /etc/pwned\n",
            "status /etc/pwned.log\n",
            "writepid /etc/pwned.pid\n",
            "chroot /tmp\nuser nobody\n",
            # openvpn 剥行首双横线:-- 前缀写法必须与裸指令同判 400
            "--up /bin/sh -c 'id'\n",
            "--script-security 3\n",
            "--status /etc/pwned.log\n",
            "--config /tmp/evil.ovpn\n",
            "--writepid /etc/pwned.pid\n",
            # 控制通道/环境变量/代理/外部凭据:以 root 身份生效,同样 400
            "management 127.0.0.1 7505\n",
            "management-hold\n",
            "--management /tmp/mgmt.sock unix\n",
            "setenv FOO bar\n",
            "setenv-safe FOO bar\n",
            "http-proxy 10.0.0.1 8080\n",
            "socks-proxy 10.0.0.1 1080\n",
            "auth-user-pass /etc/shadow\n",
            # BOM 头 + 禁指令:首行 FEFF 不得绕过关键字比对
            "﻿up /bin/sh -c 'id'\n",
        ):
            r = c.post("/openapi/v1/vpn/config", json={"content": evil}, headers=h)
            assert r.status_code == 400, f"expected 400 for {evil!r}"
            assert r.json()["code"] == "invalid_vpn_config"
        assert (tmp_path / "vpn" / "client.ovpn").read_text(encoding="utf-8") == BENIGN_CONFIG

def test_vpn_config_allows_routing_and_locks_file_mode(tmp_path):
    """route/redirect-gateway/dhcp-option 是合法路由语义,不得误杀;落盘 0600。"""
    import os

    with TestClient(_app(tmp_path)) as c:
        h = {"REDPILOT_ADMIN_TOKEN": ADMIN_TOKEN}
        ok = c.post("/openapi/v1/vpn/config", json={"content": BENIGN_CONFIG}, headers=h)
        assert ok.status_code == 200
        routed = BENIGN_CONFIG + "route 10.0.0.0 255.0.0.0\nredirect-gateway def1\ndhcp-option DNS 8.8.8.8\n"
        ok = c.post("/openapi/v1/vpn/config", json={"content": routed}, headers=h)
        assert ok.status_code == 200, ok.json()
        mode = os.stat(tmp_path / "vpn" / "client.ovpn").st_mode & 0o777
        assert mode == 0o600


def test_vpn_config_rejects_empty(tmp_path):
    with TestClient(_app(tmp_path)) as c:
        h = {"REDPILOT_ADMIN_TOKEN": ADMIN_TOKEN}
        assert c.post("/openapi/v1/vpn/config", json={"content": "  "},
                      headers=h).status_code == 400
        assert c.post("/openapi/v1/vpn/config", json={"content": "script-security 3"},
                      headers=h).status_code == 400
