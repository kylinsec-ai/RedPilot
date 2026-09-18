"""FastAPI 装配:DB + SSE bus + housekeeper。

lifespan:建 ObsStore(建库/DDL/WAL pragma;stale_after 注入 Settings 阈值)、
起 housekeeper(每 house_interval 秒关掉心跳过期的 running run -> interrupted)。
env 见 obs/config.py(OBSERVABILITY_TOKEN/DB/WEB;监听 HOST/PORT 属 uvicorn CMD)。

**仅测试可达**：容器启的是 `redpilot.app`（`Dockerfile.redpilot:30`、`main.py:17`），它
自己重装观测面各件（`redpilot/app.py:26-32`）而**不调用本工厂** —— 唯一 importers 是
`tests/obs/conftest.py:10` 与 `tests/obs/test_api.py:13`。故本工厂设的是**旧**的
`app.state` 名字：装配缺陷在 `tests/obs/` 全绿、在线上全废
（`tests/app/test_unified_app.py:10-12` 就是为此写的）。
按本仓「生产消费者为零、唯一调用方是测试」的判据，它与已拆的评估面同类。

沿革：此处原写「容器 CMD:uvicorn obs.app:app --host … --port …」，2026-09 死代码清扫时
修正 —— 那是一句与部署现实脱节的断言。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from redpilot import __version__
from redpilot.obs.bus import LiveBus
from redpilot.obs.config import Settings
from redpilot.obs.control_proxy import router as control_proxy_router
from redpilot.obs.ingest import router as ingest_router
from redpilot.obs.maintenance import housekeep
from redpilot.obs.read import router as read_router
from redpilot.obs.store import ObsStore

log = logging.getLogger("obs.app")


def create_app(db_path: str | None = None, web_dir: str | None = None,
               obs_token: str | None = None,
               control_url: str | None = None,
               enable_control_proxy: bool | None = None) -> FastAPI:
    """装配 App;参数仅为测试/宿主直跑注入,生产全走 Settings.from_env()。

    control proxy 默认不挂载:只有 control_url 非空且 enable_control_proxy
    未显式关闭时才挂载(读端观测 API 不受开关影响)。
    """
    settings = Settings.from_env()
    if db_path is not None:
        settings.db_path = db_path
    if web_dir is not None:
        settings.web_dir = web_dir
    if obs_token is not None:
        settings.obs_token = obs_token
    if control_url is not None:
        settings.control_url = control_url
    if enable_control_proxy is not None:
        settings.control_proxy_enabled = bool(enable_control_proxy)
    # 显式 URL 注入(测试)且未显式指定开关时,跟随生产规则:有 URL 即开。
    if control_url is not None and enable_control_proxy is None:
        settings.control_proxy_enabled = bool(settings.control_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ObsStore(settings.db_path, stale_after=settings.stale_after)
        app.state.store = store
        app.state.obs_token = settings.obs_token
        app.state.read_token = settings.effective_read_token()
        app.state.web_dir = settings.web_dir
        app.state.control_url = settings.control_url
        app.state.bus = LiveBus()
        house = asyncio.create_task(housekeep(store, settings))
        try:
            yield
        finally:
            house.cancel()
            with suppress(asyncio.CancelledError):
                await house
            store.close()

    app = FastAPI(title="RedPilot 观测平台", version=__version__, lifespan=lifespan)
    app.include_router(ingest_router)
    app.include_router(read_router)
    if settings.control_proxy_enabled and settings.control_url:
        app.include_router(control_proxy_router)
    else:
        log.info("control proxy disabled (control_url=%s enabled=%s)",
                 bool(settings.control_url), settings.control_proxy_enabled)
    return app


app = create_app()
