"""Runtime settings and task configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from redpilot.control.models import ConfigurationError, TaskDefinition, parse_task_config


def _float_env(name: str, default: float) -> float:
    """数值型 env:未设/坏值 → 报错(配置错误应显式暴露,而非静默用默认值)。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    database_path: str = "./data/redpilot.sqlite3"
    config_path: str | None = None
    inline_config: str | None = None
    benchmark_token: str | None = None
    # 管理端点(openvpn 生命周期等平台全局特权操作)独立凭据;None → 端点 503 拒用(fail closed)
    admin_token: str | None = None
    # Worker 控制面 API 凭据;None → assignment API 503 拒用(fail closed)
    worker_token: str | None = None
    # assignment 返回给 Worker 的平台地址;为空时 Worker 使用自身配置的地址
    public_base_url: str | None = None
    # canonical event outbox 的下游观测平台;未配置则只在 core 本地持久化
    observability_url: str | None = None
    observability_token: str | None = None
    max_active_challenges: int = 3
    # 过期租约扫描周期(秒);0 = 关闭(退回"仅在 claim 时机会性回收")。
    # 没有 sweeper 时,若无人再 claim,过期 job 会永远钉在 running。
    lease_sweep_interval: float = 30.0
    provisioner: str = "static"
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> Settings:
        dotenv_path = Path(env_file) if env_file is not None else Path(__file__).resolve().parent.parent / ".env"
        if dotenv_path.is_file():
            load_dotenv(dotenv_path=dotenv_path, override=False)

        database_path = str(Path(os.getenv("REDPILOT_DB_PATH", os.getenv("REDPILOT_DATABASE", cls.database_path))).expanduser())
        config_path = os.getenv("REDPILOT_CONFIG")
        inline_config = os.getenv("REDPILOT_TASKS_JSON")
        token_value = os.getenv("BENCHMARK_TOKEN", "").strip()
        token = token_value or None
        admin_value = os.getenv("REDPILOT_ADMIN_TOKEN", "").strip()
        admin_token = admin_value or None
        worker_value = os.getenv("REDPILOT_WORKER_TOKEN", "").strip()
        worker_token = worker_value or None
        public_value = os.getenv("REDPILOT_PUBLIC_BASE_URL", "").strip()
        public_base_url = public_value.rstrip("/") or None
        obs_url_value = os.getenv("OBSERVABILITY_URL", "").strip()
        observability_url = obs_url_value.rstrip("/") or None
        obs_token_value = os.getenv("OBSERVABILITY_TOKEN", "").strip()
        observability_token = obs_token_value or None
        try:
            max_active = int(os.getenv("REDPILOT_MAX_ACTIVE_CHALLENGES", "3"))
            port = int(os.getenv("PORT", "8000"))
        except ValueError as exc:
            raise ConfigurationError("numeric environment settings are invalid") from exc
        if max_active < 1:
            raise ConfigurationError("REDPILOT_MAX_ACTIVE_CHALLENGES must be at least 1")
        if port < 1 or port > 65535:
            raise ConfigurationError("PORT must be between 1 and 65535")
        return cls(
            database_path=database_path,
            config_path=config_path,
            inline_config=inline_config,
            benchmark_token=token,
            admin_token=admin_token,
            worker_token=worker_token,
            public_base_url=public_base_url,
            observability_url=observability_url,
            observability_token=observability_token,
            max_active_challenges=max_active,
            lease_sweep_interval=_float_env("REDPILOT_LEASE_SWEEP_INTERVAL", cls.lease_sweep_interval),
            provisioner=os.getenv("REDPILOT_PROVISIONER", "static").lower(),
            host=os.getenv("HOST", "0.0.0.0"),
            port=port,
        )

    def load_tasks(self) -> tuple[TaskDefinition, ...]:
        raw: Any = None
        if self.config_path:
            raw = json.loads(Path(self.config_path).read_text(encoding="utf-8"))
        elif self.inline_config:
            raw = json.loads(self.inline_config)
        if self.benchmark_token and raw is not None:
            if isinstance(raw, dict) and "tasks" not in raw and "token" not in raw and "benchmark_token" not in raw and "challenges" in raw:
                raw = {**raw, "token": self.benchmark_token}
            elif isinstance(raw, list) and all(isinstance(item, dict) and "token" not in item and "benchmark_token" not in item for item in raw):
                raw = {"token": self.benchmark_token, "challenges": raw}
        return parse_task_config(raw)
