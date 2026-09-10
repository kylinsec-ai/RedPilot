"""
配置管理 — 最小求解配置

环境变量驱动。模型经 SOLVER_MODEL 以完整 provider/model 逐字透传
(缺省 DEFAULT_MODEL);provider 由模型前缀决定。
API 凭据由 pi 按官方 env 名(或 ~/.pi/agent/auth.json,优先于 env)自行解析,
仓库代码零映射、零别名、不读不改 key。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 默认模型(完整 provider/id,须存在于 pi 内置目录;以 `pi --list-models` 核对为准)
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def _envs(name: str, default: str = "") -> str:
    """strip 版 _env:WORKER_ID/BASE_URL/TOKEN 类"空白即缺省"读取统一走这里"""
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


@dataclass
class SolverConfig:
    """Pi Agent 求解引擎配置"""

    model: str
    session_seconds: int

    @classmethod
    def from_env(cls) -> "SolverConfig":
        model = _env("SOLVER_MODEL", DEFAULT_MODEL)
        # 早失败: 须为完整 provider/model,否则只是把 typo 推迟到 pi 运行时
        if "/" not in model or model.startswith("/") or model.endswith("/"):
            raise ValueError(
                "SOLVER_MODEL 须为完整 [provider/]model"
                f"(如 deepseek/deepseek-v4-flash),当前: {model!r}"
            )
        return cls(
            model=model,
            session_seconds=_session_seconds(),
        )


def _session_seconds() -> int:
    """SOLVER_SESSION_SECONDS 须为正整数秒数；垃圾值/非正数 fail-fast（driver exit 2）"""
    raw = _env("SOLVER_SESSION_SECONDS", "1500")
    try:
        val = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"SOLVER_SESSION_SECONDS 须为正整数秒数,当前: {raw!r}")
    if val <= 0:
        raise ValueError(f"SOLVER_SESSION_SECONDS 须为正整数秒数,当前: {raw!r}")
    return val
