"""Unified Ghost platform FastAPI application.

Combines control plane (challenges, scheduling, VPN) and observability platform
(telemetry ingest, read API, dashboard) into a single application.

Architecture:
- Control routes: /openapi/v1/*, /api/v1/*
- Obs routes: /api/* (ingest, reads, SSE)
- Static files: / (SPA dashboard)

Both subsystems maintain separate SQLite databases with independent schemas.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from .control.api import create_app as create_control_app
from .control.config import Settings as ControlSettings
from .control.maintenance import start_lease_sweeper, stop_lease_sweeper
from .control.outbox import outbox_lifespan
from .obs.bus import LiveBus
from .obs.config import Settings as ObsSettings
from .obs.control_proxy import router as control_proxy_router
from .obs.ingest import router as ingest_router
from .obs.read import router as read_router
from .obs.store import ObsStore

log = logging.getLogger("ghost.app")


async def _housekeep(store: ObsStore, settings: ObsSettings) -> None:
    """Obs housekeeper: close stale runs and sweep dead workers."""
    while True:
        await asyncio.sleep(settings.house_interval)
        try:
            closed = await run_in_threadpool(store.close_stale_runs)
            if closed:
                log.info("housekeeper interrupted stale run(s): %s", closed)
        except Exception:
            log.exception("housekeeper error")
        try:
            swept = await run_in_threadpool(store.sweep_live)
            if swept:
                log.info("housekeeper swept %d stale live row(s)", swept)
        except Exception:
            log.exception("housekeeper sweep error")
        # 原文事件行保留:events 是唯一的无界增长路径。只删已结束 run 的原文行,
        # runs 行保留(审计链不断);0 = 关闭。
        if settings.events_retention_days > 0:
            try:
                removed = await run_in_threadpool(
                    store.prune_events,
                    older_than_days=settings.events_retention_days,
                    batch=settings.events_prune_batch)
                if removed:
                    log.info("housekeeper pruned %d event row(s) older than %.0f day(s)",
                             removed, settings.events_retention_days)
            except Exception:
                log.exception("housekeeper prune error")


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
    control_url: str | None = None,
    enable_control_proxy: bool | None = None,
) -> FastAPI:
    """Create unified Ghost application with control + obs routes.

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
        control_url: Override control URL for proxy
        enable_control_proxy: Override control proxy enable flag

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
    if control_url is not None:
        obs_settings.control_url = control_url
    if enable_control_proxy is not None:
        obs_settings.control_proxy_enabled = bool(enable_control_proxy)
    # Explicit URL injection + no explicit proxy flag → follow production rule
    if control_url is not None and enable_control_proxy is None:
        obs_settings.control_proxy_enabled = bool(obs_settings.control_url)

    # 控制面先于 lifespan 构造:其 store 要被 lifespan 内的 outbox 使用,
    # 先绑定可避免闭包引用后置变量。
    control_app = create_control_app(
        control_settings,
        database_path=database_path,
        tasks=tasks,
        provisioner=provisioner,
        max_active_challenges=max_active_challenges,
    )
    # control_app 自身永不启动(只取其路由),故其 lifespan 不会执行 ——
    # canonical outbox 的启停由下面的统一 lifespan 显式承担。
    control_store = control_app.state.store

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Unified lifespan: obs store + housekeeper + control outbox.

        `app.state.store` 恒为 obs store(观测读端 `read.py` / `ingest_common.py` 的契约);
        控制面 store 挂在 `app.state.control_store`,两者不可混用 —— 见下方 state 命名注释。
        """
        # Initialize obs store and state
        store = ObsStore(obs_settings.db_path, stale_after=obs_settings.stale_after)
        app.state.store = store
        app.state.obs_store = store  # 别名:语义更明确,供新代码使用
        app.state.obs_token = obs_settings.obs_token
        app.state.read_token = obs_settings.effective_read_token()
        app.state.web_dir = obs_settings.web_dir
        app.state.control_url = obs_settings.control_url
        app.state.bus = LiveBus()

        # Start obs housekeeper
        house = asyncio.create_task(_housekeep(store, obs_settings))

        try:
            # canonical 事件投递:与独立控制面共用 outbox_lifespan 单一实现。
            # 合并初期此处遗漏,导致权威事件通道完全断开(见 outbox_lifespan docstring)。
            async with outbox_lifespan(
                control_store,
                control_settings.observability_url,
                control_settings.observability_token,
            ):
                # 过期租约回收:没有它,无人再 claim 时过期 job 会永远钉在 running。
                sweeper = start_lease_sweeper(
                    control_store, control_settings.lease_sweep_interval)
                try:
                    yield
                finally:
                    await stop_lease_sweeper(sweeper)
        finally:
            # Shutdown obs
            house.cancel()
            with suppress(asyncio.CancelledError):
                await house
            store.close()

    # Create unified app
    app = FastAPI(
        title="Ghost Platform",
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

    # Optionally include control proxy
    if obs_settings.control_proxy_enabled and obs_settings.control_url:
        app.include_router(control_proxy_router)
    else:
        log.info(
            "control proxy disabled (control_url=%s enabled=%s)",
            bool(obs_settings.control_url),
            obs_settings.control_proxy_enabled,
        )

    return app


# Module-level app instance for uvicorn
app = create_app()
