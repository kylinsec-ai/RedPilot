"""core outbox 投递循环:把 Store 事务内写入的 canonical events 至少一次 POST 给 obs。

从 api.py lifespan 内嵌逻辑抽出,投递语义不变:
- 200/2xx → mark_outbox_delivered(删行,已投递不堆积);
- 429/5xx → 退避重试(留行,下轮重投);
- 其他 4xx → dead-letter(删行+error 日志,重试永不成功,防无界堆积);
- 传输异常 → warn,下轮重投。
半配置(url/token 只配其一)时调用方负责告警,本模块只在两者齐备时被装配。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging

import httpx

log = logging.getLogger("ghost.outbox")


async def dispatch_outbox_loop(store, url: str, token: str, *, client_factory=None,
                               max_rounds: int | None = None) -> None:
    """常驻投递循环(调用方以 asyncio Task 装配,取消即停)。

    client_factory: 测试缝合点 —— 传 `lambda: httpx.AsyncClient(transport=ASGITransport(app))`
    即可在进程内跑通 outbox → obs 全链路,无需起真实服务。默认走真实网络。
    max_rounds: 仅测试用 —— 跑满轮数后返回(默认 None = 常驻)。
    """

    endpoint = url.rstrip("/") + "/api/internal/canonical-events"
    make_client = client_factory or (lambda: httpx.AsyncClient(timeout=5.0))
    rounds = 0
    async with make_client() as client:
        while True:
            try:
                events = await asyncio.to_thread(store.pending_outbox, 100)
                if events:
                    response = await client.post(
                        endpoint,
                        headers={"X-Observability-Token": token},
                        json={"events": events},
                    )
                    if response.status_code < 400:
                        await asyncio.to_thread(
                            store.mark_outbox_delivered,
                            [str(event["event_id"]) for event in events if event.get("event_id")],
                        )
                    elif response.status_code == 429:
                        log.debug("canonical event delivery throttled (429), retrying")
                    elif response.status_code >= 500:
                        log.warning("canonical event delivery returned HTTP %s, retrying",
                                    response.status_code)
                    else:
                        # 4xx = obs 侧永久拒绝(鉴权/载荷):重试永不成功,直接死信
                        # 删行避免无界堆积,行数告警保留现场。
                        ids = [str(event["event_id"]) for event in events if event.get("event_id")]
                        await asyncio.to_thread(store.mark_outbox_delivered, ids)
                        log.error("canonical event delivery returned HTTP %s, %d event(s) dead-lettered",
                                  response.status_code, len(ids))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("canonical event delivery failed", exc_info=True)
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                return
            await asyncio.sleep(1.0)


def start_dispatch_task(store, url: str, token: str, *, client_factory=None):
    """装配辅助:起投递 Task;调用方在 lifespan finally 里 cancel/await。"""

    return asyncio.create_task(
        dispatch_outbox_loop(store, url, token, client_factory=client_factory)
    )


async def stop_dispatch_task(task) -> None:
    """装配辅助:取消投递 Task(幂等,None 可入)。"""

    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@asynccontextmanager
async def outbox_lifespan(store, url: str | None, token: str | None):
    """canonical outbox 的 lifespan 片段 —— **所有** app 工厂的唯一启停实现。

    半配置(url/token 只配其一)时响亮告警而非静默堆积(见模块 docstring 末句)。

    历史:这段逻辑原先内嵌在 control/api.py 的 lifespan 里。合并出统一 app 时,
    app.py 用 `app.routes.append()` 复制控制面路由而未 mount 子应用,Starlette
    不会对未挂载的子应用执行 lifespan —— 于是投递任务从未启动,canonical 事件
    全部滞留 outbox,obs 侧的权威终态永久缺失。抽到这里后,统一 app 与独立控制面
    共用同一实现,不会再出现"某条装配路径漏了这一步"。
    """

    task = None
    if url and token:
        task = start_dispatch_task(store, url, token)
        log.info("canonical outbox dispatch started (url=%s)", url)
    elif url or token:
        log.error("observability half-configured (url=%s token=%s): outbox will pile up, "
                  "set both OBSERVABILITY_URL and OBSERVABILITY_TOKEN",
                  bool(url), bool(token))
    try:
        yield
    finally:
        await stop_dispatch_task(task)
