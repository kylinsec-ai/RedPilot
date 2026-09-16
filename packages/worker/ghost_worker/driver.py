#!/usr/bin/env python3
"""
Ghost worker 装配层 — 进程装配 + 观测面接线，主循环交给 orchestrator。

形态: 一个容器(worker-1 持 VPN 并向外共享 netns，worker-2/3 复用它)。

    driver.main()                       ← 本模块：装配
      ├── WorkerSettings/SolverConfig   校验配置（错就 exit 0，明示后停止）
      ├── 心跳线程 + LiveState/LiveBus  观测面（下一段）
      ├── StatusBridge 注入 orchestrator ← 把编排状态翻成 live 快照
      ├── ObsRelay / RosterPoller / :8080 态势台
      └── orchestrator.main()            ← 竞技场主循环（list→派发→多会话→提交）

主循环本体（多会话/时间盒/止损/eager 提交/能力分片/舰队监督/热重载）在
`ghost_worker.orchestrator`，本模块不再有自己的 list 循环。

本地可观测性: ghost.obs.localserver(注入式 stdlib 仪表板 :8080)
远端可观测性: ghost_worker.relay(→ obs 平台 /api/internal/*)
编排侧状态:   <workdir>/status/worker-<N>.json（supervisor 与只读控制台读）
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

from ghost.obs.localserver import serve_forever_in_thread as _serve_local
from ghost_contracts.paths import LIVE_DIR

from . import orchestrator
from .config import SolverConfig
from .live import LiveBus, LiveState
from .observability import StatusBridge
from .relay import maybe_start_relay
from .roster import RosterPoller
from .settings import WorkerSettings
from .solver import touch_heartbeat

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ghost_worker.driver")

# 退出码契约见 orchestrator.main() 的注释（0/2/86/4）。本模块不自行退出。


# ── 装配 ────────────────────────────────────────────────────

def _heartbeat_loop() -> None:
    """独立心跳线程: 30s 刷心跳文件(compose healthcheck 依据);
    同时探测事件循环活性 —— 纯文件心跳只证明进程存活,asyncio loop 同步卡死
    (死锁/无超时阻塞)时 pi 线程仍会 touch_heartbeat,文件恒新,健康检查永绿。
    探针:call_soon_threadsafe 的应答若连续 5 次(≈150s)未在 1s 内回来 →
    loop 已卡死 → os._exit(4) 由容器 restart 策略拉起(与 VPN 看门狗同款语义)。"""
    ack = threading.Event()
    missed = 0
    while True:
        time.sleep(30)
        try:
            touch_heartbeat()
        except Exception:
            pass
        loop = _LOOP
        if loop is None:
            continue  # amain 尚未进入(启动期),文件心跳已足够
        ack.clear()
        try:
            loop.call_soon_threadsafe(ack.set)
        except RuntimeError:
            continue  # loop 已关闭(正常退出路径)
        if ack.wait(timeout=1.0):
            missed = 0
            continue
        missed += 1
        if missed >= 5:
            log.critical("event loop unresponsive for ~%ds — exiting 4 for container restart",
                         missed * 30)
            os._exit(4)


def main() -> None:
    """装配进程并进入竞技场主循环（`orchestrator.main()`，本函数不返回）。

    本层只做三件事：**校验配置**、**起观测面**、**把观测面接到编排上**。
    求解/调度/提交的一切判定都在 orchestrator 内部 —— 本模块不得新增任何
    关于"要不要解这道题"的逻辑，否则又会出现两套口径。
    """
    settings = WorkerSettings.from_env()
    # 凭据校验交给 orchestrator.main()：朋友的语义是"缺 BENCHMARK_* → exit 2"，
    # 而 exit 2 会被 compose 的 restart:on-failure 拉起（无限重启）。
    # 这里先拦一道，把配置错收敛成 exit 0（"明示后停止"），与 entrypoint 一致。
    if not settings.benchmark_base_url or not settings.benchmark_token:
        log.error("BENCHMARK_BASE_URL / BENCHMARK_TOKEN must be set — 容器停止，"
                  "补齐后重新 docker compose up -d")
        sys.exit(0)

    try:
        cfg = SolverConfig.from_env()
    except ValueError as e:
        # 裸 SOLVER_MODEL / 垃圾 SESSION_SECONDS：非零退出会被 on-failure 无限重启，
        # 只有 exit 0 能"停一次"并让运维看见原因。
        log.error("bad solver config: %s", e)
        sys.exit(0)

    os.makedirs(settings.workdir, exist_ok=True)
    touch_heartbeat()

    # ── 观测面 ──────────────────────────────────────────────
    # LiveState(原子文件 <workdir>/.live/<worker>.json)+ LiveBus(SSE 广播)
    live = LiveState(worker_id=settings.worker_id,
                     state_path=os.path.join(settings.workdir, LIVE_DIR,
                                             f"{settings.worker_id}.json"))
    bus = LiveBus()
    # 题目总览轮询单实例：localserver 与 relay 共享同一 RosterPoller
    # (同 worker 只跑一个 60s 轮询，避免双线程双写 /work/.live/roster.json)。
    # 两边都没启用则不建（观测面是可选件，求解不依赖它）。
    roster_poller = None
    if settings.status_port > 0 or settings.observability_url:
        try:
            roster_poller = RosterPoller(settings.workdir)
            roster_poller.start()
        except Exception:
            log.exception("roster poller start failed (dashboard shows local-only)")
            roster_poller = None
    try:
        relay = maybe_start_relay(live, bus, settings=settings,
                                  roster_poller=roster_poller)
    except Exception:
        log.exception("obs relay start failed (platform ingestion disabled)")
        relay = None

    # ── 观测桥：编排状态 → LiveState/LiveBus ────────────────
    # 竞技场的 status 字段名与 LiveState 的 18 键几乎全不一样，映射在
    # observability.StatusBridge 里（那层是唯一的新逻辑，有单测）。
    # **worker_id 对齐**：relay/LiveState 用 settings.worker_id（展示名），
    # 编排侧用 ADAPTER_WORKER_ID（序号，写 status/worker-N.json）。两者指同一个
    # worker，装配层在这里做一次一致性告警 —— 漂移会让 :8080 面板与 status
    # 文件显示成两个 worker，而两边都不报错。
    _warn_if_worker_id_drift(settings)
    orchestrator.set_status_bridge(
        StatusBridge(live, bus, relay=relay, worker_id=settings.worker_id))

    try:
        # 数据无鉴权 → 默认只绑回环(STATUS_BIND)；compose 内编排显式 0.0.0.0
        # (docker-proxy 转发需容器全网卡监听)，宿主侧再收成回环发布。
        _serve_local(live, bus, settings.status_port,
                     workdir=settings.workdir, poller=roster_poller,
                     host=settings.status_bind)
    except Exception:
        log.exception("local status server failed to start (solving continues)")

    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()

    log.info("ghost-worker starting: role=%s wid=%s model=%s base=%s",
             settings.adapter_role or "solver", settings.adapter_worker_id,
             cfg.model, settings.benchmark_base_url)
    # 主循环接管（不返回）：热重载走 os._exit(86)，任务终态走 sys.exit(0)。
    # relay 的收尾由 orchestrator 在退出路径上 flush（见其 `_drain_observability`）。
    orchestrator.main()


def _warn_if_worker_id_drift(settings: WorkerSettings) -> None:
    """relay 的 WORKER_ID 与编排的 ADAPTER_WORKER_ID 指向不同序号时告警一次。

    两者**不需要**逐字相同（一个是展示名 "worker-1"、一个是序号 1），但尾部
    数字必须一致 —— 否则 :8080 上看到的是 worker-2 在解题，而 status 文件里
    写的是 worker-1，两边都不报错（这是静默故障那一族）。
    """
    import re as _re
    m = _re.search(r"(\d+)\s*$", settings.worker_id or "")
    if m and int(m.group(1)) != settings.adapter_worker_id:
        log.warning("worker id drift: WORKER_ID=%r (→%s) 与 ADAPTER_WORKER_ID=%s 不一致 — "
                    "态势台与 status/*.json 会显示成两个 worker",
                    settings.worker_id, m.group(1), settings.adapter_worker_id)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("interrupted by user")
        sys.exit(0)
