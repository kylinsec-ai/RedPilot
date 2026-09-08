#!/usr/bin/env python3
"""
TSecBench 求解驱动 — 进程装配 + 主循环(从主树 benchmark_driver 重推导)。

形态: 一个带 VPN 的容器,常驻、一次开一道题、单会话求解、逐 flag 直接提交、
刷完轮询待命。多 flag 题未全解出时留待下一轮冷启动再试(无记忆续接)。

主循环:
  TSecBenchmarkAsync(入口 VPN 预检;list 平台为完成状态唯一权威)
    → 未完成题按 难度升序/分值降序 排列
    → 逐题 orchestration.solve_one(start → hint(无条件取) → 1 次 pi 会话
                    → 候选去重直提 → close)
    → 全部刷完 sleep 后重新列题(新题自动纳入)

本地可观测性: obs.localserver(注入式 stdlib 仪表板 :8080)
远端可观测性: tsecbench_worker.relay(→ obs 平台 /api/internal/*)

失败语义: 配置错误(缺凭据/坏 SOLVER_MODEL 等)exit 0(明示后停止,on-failure 不重启);
SDK 入口 VPN 预检失败 exit 4;平台任务结束(409)exit 0;列表失败 exit 3;
exit 3/4 由容器 restart 策略拉起;单题启动/提交失败只记日志,不 panic。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

from tsec_benchmark import InvalidState, TSecBenchmarkAsync, VpnCheckError

from obs.localserver import serve_forever_in_thread as _serve_local
from tsecbench_contracts.paths import HEARTBEAT_PATH, LIVE_DIR
from tsecbench_contracts.vocabulary import FLUSH_KINDS

from .config import SolverConfig
from .live import LiveBus, LiveState
from .relay import maybe_start_relay
from .roster import RosterPoller
from .settings import WorkerSettings, _parse_status_port
from .solver import create_solver, touch_heartbeat
from .orchestration import LiveReporter, _prioritize, solve_one

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tsecbench_worker.driver")

CYCLE_SLEEP = 30          # 一轮刷完后的等待(秒)
IDLE_SLEEP = 60           # 无待解题时的轮询间隔(秒)


# amain() 进入时填充:心跳线程据此探测事件循环活性(卡死 → os._exit(4))
_LOOP: "asyncio.AbstractEventLoop | None" = None


# ── 主循环 ──────────────────────────────────────────────────

async def amain(settings: WorkerSettings, cfg: SolverConfig, solver_backend) -> None:
    """异步主循环:SDK 入口 VPN 预检 → list/start/hint/solve/submit/close 串行刷题。"""
    global _LOOP
    _LOOP = asyncio.get_running_loop()  # 心跳线程据此探测事件循环活性
    reporter = LiveReporter(_LIVE, _BUS)
    submitted: dict[str, set[str]] = {}
    solved_ever: set[str] = set()
    try:
        async with TSecBenchmarkAsync(base_url=settings.benchmark_base_url,
                                      token=settings.benchmark_token) as client:
            while True:
                # 拉取题目;平台 is_completed 为完成状态的唯一权威
                try:
                    challenges = await client.list_challenges()
                except InvalidState:
                    log.info("task finished on platform, exiting")
                    sys.exit(0)
                except Exception as e:
                    log.error("failed to list challenges: %s", e)
                    sys.exit(3)  # 容器 restart 策略拉起(重连 VPN/API)

                pending = [c for c in challenges if not c.is_completed and c.unique_code not in solved_ever]
                if not pending:
                    # 无待解题:回 idle(带空 code)——否则 obs live_state 与两块仪表板
                    # 永久残留上个 run 的 phase='closing'('closing' ∈ ACTIVE_PHASES →
                    # 恒显 '求解中:<最后 code>',timeline meta.live 恒真)
                    _reporter_set(phase="idle", challenge_code="", error="")
                    log.info("no pending challenges, polling again in %ds", IDLE_SLEEP)
                    await asyncio.sleep(IDLE_SLEEP)
                    continue

                for ch in _prioritize(pending):
                    try:
                        solved, accepted = await solve_one(
                            client, ch, cfg=cfg, solver_backend=solver_backend,
                            reporter=reporter, relay=_RELAY,
                            workdir_root=settings.workdir,
                            flag_format=settings.flag_format,
                            submitted=submitted)
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        log.exception("unexpected error on %s", ch.unique_code)
                        solved, accepted = False, []
                    if solved:
                        solved_ever.add(ch.unique_code)
                        log.info("=== solved %s (%d flag(s)) ===", ch.unique_code, len(accepted))

                # 一轮全部会话已关 → 回 idle(空 code,同上:不残留 'closing')
                _reporter_set(phase="idle", challenge_code="", error="")
                log.info("=== pass done: %d pending, %d solved this run, polling in %ds ===",
                         len(pending), len(solved_ever), CYCLE_SLEEP)
                await asyncio.sleep(CYCLE_SLEEP)
    except VpnCheckError as e:
        # SDK 入口 VPN 预检失败:快速失败;容器 restart 策略在 VPN 恢复后拉起
        log.error("VPN pre-check failed: %s", e)
        sys.exit(4)


def _reporter_set(**fields) -> None:
    """模块级便捷转发(amain 的 idle 分支);_LIVE 未初始化时无操作。"""
    if _REPORTER is not None:
        _REPORTER.set(**fields)


# ── 装配单例(main() 初始化;None 时求解照常,仅无推送) ────────

_LIVE: LiveState | None = None
_BUS: LiveBus | None = None
_RELAY = None
_REPORTER: LiveReporter | None = None


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
    global _LIVE, _BUS, _RELAY, _REPORTER
    settings = WorkerSettings.from_env()
    if not settings.benchmark_base_url or not settings.benchmark_token:
        # 配置错误 = 正常终止:restart:on-failure 会重启一切非零退出,
        # 只有 exit 0 才能"停一次"——明示错误后停止,不进 restart 闷循环
        log.error("BENCHMARK_BASE_URL and BENCHMARK_TOKEN must be set")
        sys.exit(0)

    try:
        cfg = SolverConfig.from_env()
    except ValueError as e:
        # 裸 SOLVER_MODEL/垃圾 SESSION_SECONDS 等:明示错误后停止(exit 0),
        # 不进 restart 闷循环(非零退出会被 on-failure 无限重启)
        log.error("bad solver config: %s", e)
        sys.exit(0)
    os.makedirs(settings.workdir, exist_ok=True)
    touch_heartbeat()

    # 实时监视:LiveState(原子文件 <workdir>/.live/<worker>.json)+ SSE 广播线程
    _LIVE = LiveState(worker_id=settings.worker_id,
                      state_path=os.path.join(settings.workdir, LIVE_DIR,
                                              f"{settings.worker_id}.json"))
    _BUS = LiveBus()
    _REPORTER = LiveReporter(_LIVE, _BUS)
    # 题目总览轮询单实例:localserver 与 relay 共享同一 RosterPoller
    # (同 worker 只跑一个 60s 轮询,避免双线程双写 /work/.live/roster.json)。
    # 两边都没启用则不建。obs 是否启用以 OBSERVABILITY_URL 为准(maybe_start_relay 同判)。
    roster_poller = None
    if settings.status_port > 0 or settings.observability_url:
        try:
            roster_poller = RosterPoller(settings.workdir)
            roster_poller.start()
        except Exception:
            log.exception("roster poller start failed (dashboard shows local-only)")
            roster_poller = None
    try:
        _RELAY = maybe_start_relay(_LIVE, _BUS, settings=settings,
                                   roster_poller=roster_poller)
    except Exception:
        log.exception("obs relay start failed (platform ingestion disabled)")
        _RELAY = None
    try:
        _serve_local(_LIVE, _BUS, settings.status_port,
                     workdir=settings.workdir, poller=roster_poller)
    except Exception:
        log.exception("local status server failed to start (solving continues)")

    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()

    log.info("tsecbench-worker starting: model=%s base=%s",
             cfg.model, settings.benchmark_base_url)
    try:
        asyncio.run(amain(settings, cfg, create_solver()))
    finally:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("interrupted by user")
        sys.exit(0)
