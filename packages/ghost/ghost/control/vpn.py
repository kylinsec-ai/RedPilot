"""Local OpenVPN lifecycle management for the benchmark environment.

All state lives under ``<db_parent>/vpn/``: the uploaded config, the daemon
PID file, and the openvpn log. The openvpn binary is required on the host.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ghost.control.errors import APIError

# 上传配置中禁止的 OpenVPN 指令。openvpn 以平台进程用户(典型部署为 root)运行,
# 配置本身即代码:script-security≥2 + up/down/tls-verify 等可执行任意命令,
# log/status/writepid 等可按任意路径写文件,chroot/user/group/cd 可改变运行上下文。
# config(文件包含)可绕过本校验加载外部危险指令;其余经 --script-security 1 兜底。
# 命中任一指令 → 400 拒收(fail loud),而不是剥离后静默放行。
_FORBIDDEN_DIRECTIVES = frozenset({
    # 脚本执行
    "script-security", "up", "down", "route-up", "route-pre-down",
    "ipchange", "tls-verify", "learn-address", "auth-user-pass-verify",
    "client-connect", "client-disconnect", "plugin", "iproute",
    # 控制通道/环境变量(以平台用户、典型 root 身份生效)
    "management", "management-hold", "management-query-passwords",
    "setenv", "setenv-safe",
    # 出站代理重定向(流量劫持)与外部凭据文件外发
    "http-proxy", "http-proxy-option", "http-proxy-retry", "http-proxy-timeout",
    "socks-proxy", "socks-proxy-retry",
    "auth-user-pass",
    # 任意路径文件写
    "log", "log-append", "status", "writepid",
    # 文件包含(绕过逐行校验)
    "config",
    # 运行上下文/提权语义
    "chroot", "user", "group", "cd", "tmp-dir",
    # 注意:route/redirect-gateway/dhcp-option 是合法 VPN 路由语义,不禁
    # (禁掉会导致正常配置无法上线);威胁模型止于"拿到 admin token 才能上传"。
})


def _validate_config(content: str) -> None:
    """逐行检查禁止指令黑名单;命中 → APIError(400)。注释/空行跳过。

    openvpn 配置解析会剥掉行首 ``--``(--up 等价于 up),因此关键字比对前
    统一 lstrip("-"),否则 ``--status`` 等带双横线写法可绕过黑名单。
    """
    for raw_line in content.splitlines():
        # 先剥 BOM:Windows 导出的 .ovpn 首行带 FEFF,否则首行禁指令可绕过。
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith(("#", ";")):
            continue
        keyword = line.split(None, 1)[0].lower().lstrip("-")
        if keyword in _FORBIDDEN_DIRECTIVES:
            raise APIError(
                400, "invalid_vpn_config",
                f"VPN 配置含被禁指令 '{keyword}'"
                " (script-security/up/down/plugin/log/status 等执行与文件写指令一律拒绝)")


@dataclass(frozen=True)
class VPNStatus:
    configured: bool
    running: bool
    tun_up: bool
    config_name: str | None = None
    pid: int | None = None


class VPNManager:
    def __init__(self, vpn_dir: str | Path) -> None:
        self.vpn_dir = Path(vpn_dir)
        self.config_path = self.vpn_dir / "client.ovpn"
        self.pid_path = self.vpn_dir / "openvpn.pid"
        self.log_path = self.vpn_dir / "openvpn.log"

    def _ensure_dir(self) -> None:
        self.vpn_dir.mkdir(parents=True, exist_ok=True)

    def _read_pid(self) -> int | None:
        try:
            raw = self.pid_path.read_text(encoding="utf-8").strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _tun_up() -> bool:
        try:
            result = subprocess.run(
                ["ip", "-o", "link", "show"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return any("tun" in line for line in result.stdout.splitlines())

    def status(self) -> VPNStatus:
        pid = self._read_pid()
        running = self._pid_alive(pid)
        return VPNStatus(
            configured=self.config_path.exists(),
            running=running,
            tun_up=self._tun_up(),
            config_name=self.config_path.name if self.config_path.exists() else None,
            pid=pid if running else None,
        )

    def save_config(self, content: str) -> VPNStatus:
        if not content or not content.strip():
            raise APIError(400, "invalid_vpn_config", "VPN 配置内容为空")
        _validate_config(content)
        self._ensure_dir()
        was_running = self.status().running
        self.config_path.write_text(content, encoding="utf-8")
        # 配置常含内嵌私钥:host bind-mount 下默认 umask 可被同机其它 UID 读取 —— 钉死 0600。
        try:
            os.chmod(self.config_path, 0o600)
        except OSError:
            pass
        if was_running:
            self.stop()
            self.start()
        return self.status()

    def start(self) -> VPNStatus:
        if shutil.which("openvpn") is None:
            raise APIError(503, "openvpn_missing", "服务器未安装 openvpn")
        current = self.status()
        if current.running:
            return current
        if not self.config_path.exists():
            raise APIError(400, "vpn_config_missing", "尚未上传 VPN 配置文件")
        self._ensure_dir()
        try:
            # 注意 argv 顺序:--config 之后追加的选项晚于配置解析,覆盖配置内的
            # script-security 设定(双重防线:上传校验拒绝 + 启动参数钉死为 1,
            # 即仅允许内置 ip/route 等,用户脚本一律不执行)。
            subprocess.run(
                [
                    "openvpn",
                    "--config",
                    self.config_path.name,
                    "--daemon",
                    "--log",
                    self.log_path.name,
                    "--writepid",
                    self.pid_path.name,
                    "--script-security",
                    "1",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.vpn_dir),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise APIError(500, "vpn_start_failed", f"openvpn 启动失败: {exc}") from exc
        return self.status()

    def stop(self) -> VPNStatus:
        pid = self._read_pid()
        if pid is not None and self._pid_alive(pid) and self._pid_is_openvpn(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            self.pid_path.unlink()
        except OSError:
            pass
        return self.status()

    @staticmethod
    def _pid_is_openvpn(pid: int) -> bool:
        """pidfile 在 host 可写的 bind-mount 上:kill 前确认仍是 openvpn 进程,
        防 PID 复用误杀。无 /proc 时无法确认,沿用旧行为(容器内单进程命名空间)。"""
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore")
        except OSError:
            return True
        return "openvpn" in cmdline.split("\x00")[0]

    def as_dict(self, status: VPNStatus | None = None) -> dict[str, Any]:
        value = status or self.status()
        return {
            "configured": value.configured,
            "running": value.running,
            "tun_up": value.tun_up,
            "config_name": value.config_name,
            "pid": value.pid,
        }