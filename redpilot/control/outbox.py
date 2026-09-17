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
import random

import httpx

log = logging.getLogger("redpilot.outbox")

# 空转节拍与退避参数。此前固定 1s 无退避:obs 宕机时既每秒重试一次(无效压力),
# 又每秒刷一条 exc_info(噪声淹没真实故障)。
_IDLE_INTERVAL = 1.0
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 60.0


def _backoff(failures: int) -> float:
    """指数退避 + 抖动:上限 60s,抖动避免多实例同步重试(thundering herd)。"""
    raw = min(_BACKOFF_BASE * (2 ** max(0, failures - 1)), _BACKOFF_MAX)
    return raw * (0.5 + random.random() * 0.5)


def _retry_after(response, failures: int) -> float:
    """429 优先采纳服务端 Retry-After(仍受上限约束),缺省走退避。"""
    raw = response.headers.get("Retry-After", "")
    if raw.strip().isdigit():
        return min(float(raw.strip()), _BACKOFF_MAX)
    return _backoff(failures)


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
    failures = 0          # 连续失败次数 → 退避与日志降噪共用
    complained = False    # 本轮故障是否已响亮报过(避免每秒一条 exc_info)
    async with make_client() as client:
        while True:
            delay = _IDLE_INTERVAL
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
                        if failures:
                            log.info("canonical event delivery recovered after %d failure(s)",
                                     failures)
                        failures = 0
                        complained = False
                    elif response.status_code == 429:
                        delay = _retry_after(response, failures)
                        log.debug("canonical event delivery throttled (429), retry in %.1fs", delay)
                        failures += 1
                    elif response.status_code >= 500:
                        failures += 1
                        delay = _backoff(failures)
                        if not complained:
                            log.warning("canonical event delivery returned HTTP %s; "
                                        "backing off (obs down?)", response.status_code)
                            complained = True
                        else:
                            log.debug("canonical event delivery still HTTP %s",
                                      response.status_code)
                    else:
                        # 4xx = obs 侧永久拒绝(鉴权/载荷):重试永不成功,直接死信
                        # 删行避免无界堆积,行数告警保留现场。
                        ids = [str(event["event_id"]) for event in events if event.get("event_id")]
                        await asyncio.to_thread(store.mark_outbox_delivered, ids)
                        log.error("canonical event delivery returned HTTP %s, "
                                  "%d event(s) dead-lettered", response.status_code, len(ids))
                        failures = 0
                        complained = False
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                delay = _backoff(failures)
                # 首次响亮带栈(定位用),其后降为 debug —— 此前每秒一条 exc_info,
                # obs 宕机一天能刷出 ~86k 条 traceback。
                if not complained:
                    log.warning("canonical event delivery failed; backing off", exc_info=True)
                    complained = True
                else:
                    log.debug("canonical event delivery still failing", exc_info=True)
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                return
            await asyncio.sleep(delay)


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
