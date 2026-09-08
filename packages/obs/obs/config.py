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


# 心跳过期阈值(秒):读端"在线上"新鲜窗口与房管关 stale run 共用同一把尺,
# store 构造时注入(见 ObsStore.stale_after),避免两处字面量漂移。
DEFAULT_STALE_AFTER = 150.0
# housekeeper 周期(秒)
DEFAULT_HOUSE_INTERVAL = 30.0


@dataclass
class Settings:
    # ingest 鉴权 token;None=未配置 → 摄取端点响亮 503(防静默空转)
    obs_token: str | None = None
    # SQLite 文件路径(父目录自动建)
    db_path: str = "./data/obs.sqlite3"
    # SPA 产物目录;None=未配置 → / 返回 404 说明(镜像内由 OBSERVABILITY_WEB=/app/web 提供)
    web_dir: str | None = None
    # housekeeper 周期 / 心跳过期阈值(秒);live POST 节拍 ~1/s,30s ping ≪ 150s。
    # 注:uvicorn 监听 host/port 由容器 CMD 读 OBSERVABILITY_HOST/PORT(不属本 Settings)。
    stale_after: float = DEFAULT_STALE_AFTER
    house_interval: float = DEFAULT_HOUSE_INTERVAL

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            obs_token=_env_str("OBSERVABILITY_TOKEN"),
            db_path=_env_str("OBSERVABILITY_DB") or cls.db_path,
            web_dir=_env_str("OBSERVABILITY_WEB"),
        )
