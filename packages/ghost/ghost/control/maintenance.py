"""控制面后台维护:过期租约回收。

为何需要独立 sweeper:租约过期原先**只在 claim_job 的事务内机会性回收** ——
若无人再 claim(worker 全下线、或队列已空),过期 job 就永远钉在 running,
attempt 也永远停在中间态,obs 侧 run 无终态。

为何放 core 而非 obs housekeeper:
  - lease/attempt/job 都是 core 的表,obs 库里根本没有这些行;
  - obs 不得 import control(依赖方向硬约束);
  - 回收要写 canonical 事件到 core 的 platform_events + outbox_events。

装配:与 outbox 同款三件套(start/stop/loop),由 app lifespan 起停。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging

log = logging.getLogger("ghost.maintenance")

# 默认扫描周期(秒)。租约通常 300s,30s 粒度足以在过期后及时回收,
# 又不至于让后台任务本身成为写锁竞争的来源。
DEFAULT_LEASE_SWEEP_INTERVAL = 30.0


async def lease_sweep_loop(store, interval: float) -> None:
    """常驻回收循环(调用方以 asyncio Task 装配,取消即停)。"""

    while True:
        try:
            reaped = await asyncio.to_thread(store.reap_expired_leases)
            if reaped:
                log.warning("lease sweeper interrupted %d expired attempt(s): %s",
                            len(reaped), reaped)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lease sweeper error")
        await asyncio.sleep(interval)


def start_lease_sweeper(store, interval: float):
    """装配辅助:起回收 Task;调用方在 lifespan finally 里 cancel/await。"""

    if interval <= 0:  # 显式关闭
        log.info("lease sweeper disabled (interval=0); expiry reaped only on claim")
        return None
    log.info("lease sweeper started (interval=%.0fs)", interval)
    return asyncio.create_task(lease_sweep_loop(store, interval))


async def stop_lease_sweeper(task) -> None:
    """取消回收 Task(幂等,None 可入)。"""

    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
