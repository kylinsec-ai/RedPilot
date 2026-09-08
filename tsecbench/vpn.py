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

from .errors import APIError


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
        self._ensure_dir()
        was_running = self.status().running
        self.config_path.write_text(content, encoding="utf-8")
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
        if pid is not None and self._pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            self.pid_path.unlink()
        except OSError:
            pass
        return self.status()

    def as_dict(self, status: VPNStatus | None = None) -> dict[str, Any]:
        value = status or self.status()
        return {
            "configured": value.configured,
            "running": value.running,
            "tun_up": value.tun_up,
            "config_name": value.config_name,
            "pid": value.pid,
        }