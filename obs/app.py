"""FastAPI 装配:DB + SSE bus + housekeeper。

lifespan:建 ObsStore(建库/DDL/WAL pragma)、起 housekeeper
(每 house_interval 秒关掉心跳过期的 running run -> interrupted)。
env 见 obs/config.py(OBSERVABILITY_TOKEN/DB/WEB/HOST/PORT)。
容器 CMD:uvicorn obs.app:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from . import __version__
from .bus import LiveBus
from .config import Settings
from .ingest import router as ingest_router
from .read import router as read_router
from .store import ObsStore

log = logging.getLogger("obs.app")


async def _housekeep(store: ObsStore, settings: Settings) -> None:
    while True:
        await asyncio.sleep(settings.house_interval)
        try:
            closed = await run_in_threadpool(store.close_stale_runs,
                                             None, settings.stale_after)
            if closed:
                log.info("housekeeper interrupted stale run(s): %s", closed)
        except Exception:
            log.exception("housekeeper error")


def create_app(db_path: str | None = None, web_dir: str | None = None,
               obs_token: str | None = None, stale_after: float | None = None,
               house_interval: float | None = None) -> FastAPI:
    settings = Settings.from_env()
    if db_path is not None:
        settings.db_path = db_path
    if web_dir is not None:
        settings.web_dir = web_dir
    if obs_token is not None:
        settings.obs_token = obs_token
    if stale_after is not None:
        settings.stale_after = stale_after
    if house_interval is not None:
        settings.house_interval = house_interval

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ObsStore(settings.db_path)
        app.state.store = store
        app.state.obs_token = settings.obs_token
        app.state.web_dir = settings.web_dir
        app.state.bus = LiveBus()
        house = asyncio.create_task(_housekeep(store, settings))
        try:
            yield
        finally:
            house.cancel()
            with suppress(asyncio.CancelledError):
                await house
            store.close()

    app = FastAPI(title="TSecBench 观测平台", version=__version__, lifespan=lifespan)
    app.include_router(ingest_router)
    app.include_router(read_router)
    return app


app = create_app()
