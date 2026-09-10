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
from starlette.concurrency import run_in_threadpool

from .control.api import create_app as create_control_app
from .control.config import Settings as ControlSettings
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Unified lifespan: control outbox + obs store + housekeeper."""
        # Initialize obs store and state
        store = ObsStore(obs_settings.db_path, stale_after=obs_settings.stale_after)
        app.state.obs_store = store
        app.state.obs_token = obs_settings.obs_token
        app.state.obs_web_dir = obs_settings.web_dir
        app.state.obs_control_url = obs_settings.control_url
        app.state.bus = LiveBus()

        # Start obs housekeeper
        house = asyncio.create_task(_housekeep(store, obs_settings))

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
        title="Ghost Platform",
        version="1.0.0",
        description="Unified control plane + observability platform",
        lifespan=lifespan,
    )

    # Mount control routes (creates control app internally with its own lifespan)
    # Control app has its own outbox dispatch task in its lifespan
    control_app = create_control_app(
        control_settings,
        database_path=database_path,
        tasks=tasks,
        provisioner=provisioner,
        max_active_challenges=max_active_challenges,
    )

    # Include all control routes directly (flatten into main app)
    for route in control_app.routes:
        app.routes.append(route)

    # Copy control app state to unified app
    app.state.store = control_app.state.store
    app.state.service = control_app.state.service
    app.state.control = control_app.state.control
    app.state.challenges = control_app.state.challenges
    app.state.scheduling = control_app.state.scheduling
    app.state.settings = control_app.state.settings
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
