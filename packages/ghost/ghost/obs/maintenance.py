"""obs 后台维护:关过期 run、GC 死 worker 残留、按保留期清理原文事件行。

单源的理由:统一 app(ghost/app.py)与独立观测工厂(ghost/obs/app.py)都起这个
循环。此前两边各抄一份,保留策略这一项就漂了 —— 统一 app 有 prune_events,
独立工厂没有,同一份配置在两处语义不同。新增清理项请只加在这里。
"""

from __future__ import annotations

import asyncio
import logging

from starlette.concurrency import run_in_threadpool

from .store import ObsStore

log = logging.getLogger("obs.maintenance")


async def housekeep(store: ObsStore, settings) -> None:
    """常驻清理循环(调用方以 asyncio Task 装配,取消即停)。

    窗口阈值已由 lifespan 注入 store(ObsStore.stale_after = Settings.stale_after),
    房管直接吃 store 缺省 —— 关 stale run 与读端"在线上"判定永同一把尺。
    """

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
