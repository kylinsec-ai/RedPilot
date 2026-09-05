"""平台进程配置 — env OBSERVABILITY_*(宿主裸跑与容器双适配)。"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 容器外也允许从仓库 .env 读(缺省文件不存在时为 no-op)
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover - 无 python-dotenv 时降级为纯 env
    pass


def _env_str(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else None


@dataclass
class Settings:
    # ingest 鉴权 token;None=未配置 → 摄取端点响亮 503(防静默空转)
    obs_token: str | None = None
    # SQLite 文件路径(父目录自动建)
    db_path: str = "./data/obs.sqlite3"
    # SPA 产物目录;None=未配置 → / 返回 404 说明(镜像内由 OBSERVABILITY_WEB=/app/web 提供)
    web_dir: str | None = None
    host: str = "0.0.0.0"
    port: int = 8090
    # housekeeper 周期 / 心跳过期阈值(秒);live POST 节拍 ~1/s,30s ping ≪ 150s
    stale_after: float = 150.0
    house_interval: float = 30.0

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            obs_token=_env_str("OBSERVABILITY_TOKEN"),
            db_path=_env_str("OBSERVABILITY_DB") or cls.db_path,
            web_dir=_env_str("OBSERVABILITY_WEB"),
            host=_env_str("OBSERVABILITY_HOST") or cls.host,
        )
        raw_port = _env_str("OBSERVABILITY_PORT")
        if raw_port is not None:
            s.port = int(raw_port)
        return s
