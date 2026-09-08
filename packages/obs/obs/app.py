"""FastAPI 装配:DB + SSE bus + housekeeper。

lifespan:建 ObsStore(建库/DDL/WAL pragma;stale_after 注入 Settings 阈值)、
起 housekeeper(每 house_interval 秒关掉心跳过期的 running run -> interrupted)。
env 见 obs/config.py(OBSERVABILITY_TOKEN/DB/WEB;监听 HOST/PORT 属 uvicorn CMD)。
容器 CMD:uvicorn obs.app:app --host $OBSERVABILITY_HOST --port $OBSERVABILITY_PORT
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
    # 窗口阈值已由 lifespan 注入 store(ObsStore.stale_after = Settings.stale_after),
    # 房管直接吃 store 缺省 —— 关 stale run 与读端"在线上"判定永同一把尺。
    while True:
        await asyncio.sleep(settings.house_interval)
        try:
            closed = await run_in_threadpool(store.close_stale_runs)
            if closed:
                log.info("housekeeper interrupted stale run(s): %s", closed)
        except Exception:
            log.exception("housekeeper error")
        try:
            # GC 死 worker 的 live 残留(关闭其 running run 之后 4x 窗口)
            swept = await run_in_threadpool(store.sweep_live)
            if swept:
                log.info("housekeeper swept %d stale live row(s)", swept)
        except Exception:
            log.exception("housekeeper sweep error")


def create_app(db_path: str | None = None, web_dir: str | None = None,
               obs_token: str | None = None) -> FastAPI:
    """装配 App;三个参数仅为测试/宿主直跑注入,生产全走 Settings.from_env()。"""
    settings = Settings.from_env()
    if db_path is not None:
        settings.db_path = db_path
    if web_dir is not None:
        settings.web_dir = web_dir
    if obs_token is not None:
        settings.obs_token = obs_token

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ObsStore(settings.db_path, stale_after=settings.stale_after)
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
