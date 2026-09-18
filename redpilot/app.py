"""Unified RedPilot platform FastAPI application.

Combines control plane (challenges, provisioning, VPN, scoring) and observability
(telemetry ingest, read API, dashboard) into a single application.

Architecture:
- Control routes: /openapi/v1/* (challenges / vpn)
- Obs routes: /api/* (ingest, reads, SSE)
- Static files: / (SPA dashboard)

Both subsystems maintain separate SQLite databases with independent schemas.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from .control.api import create_app as create_control_app
from .control.config import Settings as ControlSettings
from .obs.bus import LiveBus
from .obs.config import Settings as ObsSettings
from .obs.ingest import router as ingest_router
from .obs.maintenance import housekeep
from .obs.read import router as read_router
from .obs.store import ObsStore

log = logging.getLogger("redpilot.app")


def create_app(
    control_settings: ControlSettings | None = None,
    obs_settings: ObsSettings | None = None,
    *,
    # Control plane overrides
    database_path: str | None = None,
    tasks: Any = None,
    provisioner: Any = None,
    max_active_challenges: int | None = None,
    # Obs overrides
    obs_db_path: str | None = None,
    obs_web_dir: str | None = None,
    obs_token: str | None = None,
) -> FastAPI:
    """Create unified RedPilot application with control + obs routes.

    Args:
        control_settings: Control plane settings (loaded from env if None)
        obs_settings: Obs settings (loaded from env if None)
        database_path: Override control DB path
        tasks: Override task definitions
        provisioner: Override container provisioner
        max_active_challenges: Override max active challenges
        obs_db_path: Override obs DB path
        obs_web_dir: Override SPA web directory
        obs_token: Override observability token

    Returns:
        Unified FastAPI application
    """
    control_settings = control_settings or ControlSettings.from_env()
    obs_settings = obs_settings or ObsSettings.from_env()

    # Apply overrides
    if obs_db_path is not None:
        obs_settings.db_path = obs_db_path
    if obs_web_dir is not None:
        obs_settings.web_dir = obs_web_dir
    if obs_token is not None:
        obs_settings.obs_token = obs_token
    # 沿革（2026-09 死码清扫）：此处原有的 `control_url` / `enable_control_proxy`
    # 两个注入参数与配套的 `obs.control_proxy` 路由一并删除 —— 理由是两个独立的
    # 死因：① 打开它的环境变量 `OBS_CONTROL_URL` **不在任何部署文件里**
    # （.env.example / compose / entrypoint / Dockerfile 全无），恒为关闭；
    # ② 即便打开也转发到 `/api/v1/*`，而那条前缀的路由已随派发控制面于 `fb96614`
    # 拆除（现存的只有 `/openapi/v1/*`）—— 按构造就不可能工作。

    # 控制面先于 lifespan 构造:路由闭包持有其 store。
    control_app = create_control_app(
        control_settings,
        database_path=database_path,
        tasks=tasks,
        provisioner=provisioner,
        max_active_challenges=max_active_challenges,
    )
    # control_app 自身永不启动(只取其路由),故其 lifespan 不会执行。
    control_store = control_app.state.store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Unified lifespan: obs store + housekeeper.

        `app.state.store` 恒为 obs store(观测读端 `read.py` / `ingest_common.py` 的契约);
        控制面 store 挂在 `app.state.control_store`,两者不可混用 —— 见下方 state 命名注释。

        此前这里还启停控制面的 canonical outbox;那套随 evaluation/job/attempt
        派发协议于 2026-09 一并拆除,控制面已无后台任务。
        """
        # Initialize obs store and state
        store = ObsStore(obs_settings.db_path, stale_after=obs_settings.stale_after)
        app.state.store = store
        app.state.obs_store = store  # 别名:语义更明确,供新代码使用
        app.state.obs_token = obs_settings.obs_token
        app.state.read_token = obs_settings.effective_read_token()
        app.state.web_dir = obs_settings.web_dir
        app.state.bus = LiveBus()

        # Start obs housekeeper
        house = asyncio.create_task(housekeep(store, obs_settings))

        try:
            yield
        finally:
            # Shutdown obs
            house.cancel()
            with suppress(asyncio.CancelledError):
                await house
            store.close()

    # Create unified app
    app = FastAPI(
        title="RedPilot Platform",
        version="1.0.0",
        description="Unified control plane + observability platform",
        lifespan=lifespan,
    )

    # 控制面路由直接并入主 app(而非 mount 子应用:mount 会与观测的 /api/* 及
    # 根路径 SPA 抢前缀)。只取 APIRoute,跳过 FastAPI 自带的 openapi/docs/redoc
    # 等默认路由,避免与主 app 的同名路由重复。
    app.router.routes.extend(
        route for route in control_app.router.routes if isinstance(route, APIRoute)
    )

    # state 命名契约:
    #   app.state.store         = obs store(观测读端既有契约,勿改)
    #   app.state.control_store = 控制面 store(避开同名冲突)
    # 控制面的 service/facades 已由各自路由闭包持有,全仓无 app.state 读者,故不再复制。
    app.state.control_store = control_store
    app.state.vpn = control_app.state.vpn

    # Copy exception handlers
    for exc_class, handler in control_app.exception_handlers.items():
        app.add_exception_handler(exc_class, handler)

    # Include obs routes
    app.include_router(ingest_router)
    app.include_router(read_router)

    return app


# Module-level app instance for uvicorn
app = create_app()
