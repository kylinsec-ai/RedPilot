"""
配置管理 — 最小求解配置

环境变量驱动，支持 deepseek / glm 预设。
只保留 Pi Agent 求解引擎所需；调度/止损/验证器配置已随机制移除。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_SOLVER_PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-flash",
    },
    "deepseek-1m": {
        "base_url": "https://api.deepseek.com/anthropic",
        "model": "deepseek-v4-pro[1m]",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "model": "glm-5.3",
    },
    "glm-1m": {
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "model": "glm-5.3",
    },
}


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


@dataclass
class SolverConfig:
    """Pi Agent 求解引擎配置"""

    provider: str
    base_url: str
    api_key: str
    model: str
    session_seconds: int

    @classmethod
    def from_env(cls) -> "SolverConfig":
        provider = (_env("SOLVER_PROVIDER") or "deepseek").lower()
        preset = _SOLVER_PRESETS.get(provider, _SOLVER_PRESETS["deepseek"])
        base = _env("SOLVER_BASE_URL", preset["base_url"]) or preset["base_url"]
        key = (_env("SOLVER_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN")
               or _env("ANTHROPIC_API_KEY") or "")
        return cls(
            provider=provider,
            base_url=base.rstrip("/"),
            api_key=key,
            model=_env("SOLVER_MODEL", preset["model"]) or preset["model"],
            session_seconds=int(_env("SOLVER_SESSION_SECONDS", "1500") or "1500"),
        )
