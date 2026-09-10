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


def _env_flag(name: str, default: bool) -> bool:
    """显式开关解析:未设走 default;设了按 0/false/no/off=关,其余=开。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


# 心跳过期阈值(秒):读端"在线上"新鲜窗口与房管关 stale run 共用同一把尺,
# store 构造时注入(见 ObsStore.stale_after),避免两处字面量漂移。
DEFAULT_STALE_AFTER = 150.0
# housekeeper 周期(秒)
DEFAULT_HOUSE_INTERVAL = 30.0


@dataclass
class Settings:
    # ingest 鉴权 token;None=未配置 → 摄取端点响亮 503(防静默空转)
    obs_token: str | None = None
    # 读端鉴权 token;None → 回落到 obs_token(compose 已必配,故默认部署即有凭据),
    # 两者皆无 → 读端 503 fail-closed。
    # 为何单独一个:ingest token 分发在每个 worker 容器里,而读端返回**明文 flag 与
    # 完整 agent 实录**;用独立凭据可让"能写遥测"不等于"能读答案"。
    read_token: str | None = None
    # SQLite 文件路径(父目录自动建)
    db_path: str = "./data/obs.sqlite3"
    # SPA 产物目录;None=未配置 → / 返回 404 说明(镜像内由 OBSERVABILITY_WEB=/app/web 提供)
    web_dir: str | None = None
    # 同源控制面代理地址;None 时 /api/v1/* 不启用
    control_url: str | None = None
    # 控制代理显式总开关:默认跟随 control_url(配了 URL 即开,未配即关);
    # OBS_ENABLE_CONTROL_PROXY=0 可在配了 URL 时强制关闭(默认观测 API 不受影响)。
    control_proxy_enabled: bool = False
    # housekeeper 周期 / 心跳过期阈值(秒);live POST 节拍 ~1/s,30s ping ≪ 150s。
    # 注:uvicorn 监听 host/port 由容器 CMD 读 OBSERVABILITY_HOST/PORT(不属本 Settings)。
    stale_after: float = DEFAULT_STALE_AFTER
    house_interval: float = DEFAULT_HOUSE_INTERVAL

    def effective_read_token(self) -> str | None:
        """读端凭据:显式配置优先,否则回落 ingest token。

        刻意用惰性方法而非 __post_init__:app 工厂普遍在构造 Settings 之后
        直接改属性(`settings.obs_token = obs_token`),__post_init__ 早已跑完,
        回落会静默失效 → 读端 503。在 lifespan 装配那一刻求值才不会踩时序。
        """
        return self.read_token or self.obs_token

    @classmethod
    def from_env(cls) -> "Settings":
        control_url = (_env_str("OBS_CONTROL_URL") or "").rstrip("/") or None
        return cls(
            obs_token=_env_str("OBSERVABILITY_TOKEN"),
            read_token=_env_str("OBSERVABILITY_READ_TOKEN"),
            db_path=_env_str("OBSERVABILITY_DB") or cls.db_path,
            web_dir=_env_str("OBSERVABILITY_WEB"),
            control_url=control_url,
            # 默认关闭/不挂载:只有配了 URL 且未被显式开关关闭时才启用。
            control_proxy_enabled=bool(control_url) and _env_flag(
                "OBS_ENABLE_CONTROL_PROXY", True),
        )
