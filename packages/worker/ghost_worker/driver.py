#!/usr/bin/env python3
"""
Ghost 求解驱动 — 进程装配 + 主循环(从主树 benchmark_driver 重推导)。

形态: 一个带 VPN 的容器,常驻、一次开一道题、单会话求解、逐 flag 直接提交、
刷完轮询待命。多 flag 题未全解出时留待下一轮冷启动再试(无记忆续接)。

主循环:
  GhostmarkAsync(入口 VPN 预检;list 平台为完成状态唯一权威)
    → 未完成题按 难度升序/分值降序 排列
    → 逐题 orchestration.solve_one(start → hint(无条件取) → 1 次 pi 会话
                    → 候选去重直提 → close)
    → 全部刷完 sleep 后重新列题(新题自动纳入)

本地可观测性: obs.localserver(注入式 stdlib 仪表板 :8080)
远端可观测性: ghost_worker.relay(→ obs 平台 /api/internal/*)

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

from ._sdk import InvalidState, GhostmarkAsync, VpnCheckError

from ghost.obs.localserver import serve_forever_in_thread as _serve_local
from ghost_contracts.paths import LIVE_DIR, safe_code

from .assignment import AssignmentClient, AssignmentError
from .assignment_session import complete_with_retry, lease_watch
from .config import SolverConfig
from .live import LiveBus, LiveState
from .relay import maybe_start_relay
from .roster import RosterPoller
from .settings import WorkerSettings
from .solver import create_solver, touch_heartbeat
from .solver.pi_agent import kill_solver_processes
from .orchestration import LiveReporter, ProviderFailure, _prioritize, solve_one

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ghost_worker.driver")

CYCLE_SLEEP = 30          # 一轮刷完后的等待(秒)
IDLE_SLEEP = 60           # 无待解题时的轮询间隔(秒)
PROVIDER_FAILURE_EXIT_STREAK = 3  # 连续 N 题 provider 失败 → exit 3 熔断


# amain() 进入时填充:心跳线程据此探测事件循环活性(卡死 → os._exit(4))
_LOOP: "asyncio.AbstractEventLoop | None" = None


# ── 主循环 ──────────────────────────────────────────────────


async def _assignment_lease_watch(
    client: AssignmentClient,
    assignment: dict,
    lease_seconds: int,
    lease_lost: "asyncio.Event",
) -> None:
    """兼容包装:实现见 assignment_session.lease_watch(租约生命周期单源)。"""

    await lease_watch(client, assignment, lease_seconds, lease_lost)


async def _solve_assignment(
    settings: WorkerSettings,
    cfg: SolverConfig,
    solver_backend,
    assignment_client: AssignmentClient,
    assignment: dict,
    *,
    reporter,
    relay,
) -> None:
    """领取到一个 assignment 后复用现有 solve_one 执行器。"""

    attempt_id = assignment["attempt_id"]
    lease_id = assignment["lease_id"]
    code = assignment["unique_code"]
    status = "failed"
    solved = False
    accepted: list[str] = []
    error = None
    if relay is not None:
        try:
            relay.bind_attempt(
                evaluation_id=assignment.get("evaluation_id"),
                job_id=assignment.get("job_id"),
                attempt_id=attempt_id,
            )
        except Exception:
            log.exception("failed to bind obs run to assignment %s", attempt_id)
    lease_lost = asyncio.Event()
    lease_task = asyncio.create_task(
        _assignment_lease_watch(assignment_client, assignment,
                                settings.assignment_lease_seconds, lease_lost)
    )
    vpn_failed = False
    try:
        base_url = (
            assignment.get("benchmark_base_url")
            or settings.benchmark_base_url
            or settings.platform_url
        )
        token = str(assignment.get("benchmark_token") or "")
        if not base_url or not token:
            raise RuntimeError("assignment does not contain benchmark connection settings")
        async with GhostmarkAsync(base_url=base_url, token=token) as benchmark:
            challenges = await benchmark.list_challenges()
            challenge = next((item for item in challenges if item.unique_code == code), None)
            if challenge is None:
                raise RuntimeError(f"assigned challenge is not visible to benchmark token: {code}")
            solve_task = asyncio.create_task(solve_one(
                benchmark,
                challenge,
                cfg=cfg,
                solver_backend=solver_backend,
                reporter=reporter,
                relay=relay,
                workdir_root=settings.workdir,
                flag_format=settings.flag_format,
            ))
            lease_gone = asyncio.create_task(lease_lost.wait())
            done, _pending = await asyncio.wait(
                {solve_task, lease_gone}, return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_gone in done and not solve_task.done():
                # 租约已丢:继续算只是烧 Pi 时长,complete 必 409 —— 中止并上报 interrupted
                # (job 回 pending,下一轮可重做)。
                log.error("assignment lease lost for %s, aborting solve", code)
                solve_task.cancel()
                # 关键:取消 asyncio 任务**杀不掉** to_thread 里已启动的 pi 子进程。
                # 不显式杀,它会在后台继续烧 LLM 时长、继续写同一个 workdir,而 job 已回
                # pending 可能被再次领取 —— 同一 workdir 两个会话互相踩。
                killed = await asyncio.to_thread(
                    kill_solver_processes, os.path.join(settings.workdir, safe_code(code)))
                if killed:
                    log.warning("killed in-flight pi process group for %s after lease loss", code)
                try:
                    await solve_task
                except asyncio.CancelledError:
                    pass
                status, error = "interrupted", "lease lost; solve aborted"
            else:
                lease_gone.cancel()
                try:
                    await lease_gone
                except asyncio.CancelledError:
                    pass
                solved, accepted = await solve_task
                status = "solved" if solved else "done"
    except ProviderFailure as exc:
        error = str(exc)
        log.error("assignment provider failure on %s: %s", code, exc)
    except VpnCheckError as exc:
        # 运行中 VPN 掉线与入口预检同语义:上报 interrupted 后走 exit 4 重启通道。
        error = str(exc)
        log.error("assignment VPN failed on %s: %s", code, exc)
        vpn_failed = True
        status = "interrupted"
    except Exception as exc:
        error = str(exc)
        log.exception("assignment failed on %s", code)
    finally:
        lease_task.cancel()
        try:
            await lease_task
        except asyncio.CancelledError:
            pass
        # 已接受 flag 明文补给平台(非权威,加性):assignment 模式不关 run,
        # 而该字段此前只经 run_close 写入 —— 不补则 /api/challenge 恒返回空 flags。
        # 放在 complete 之前:平台侧 run 行此时已由事件流建立,补写必命中。
        if relay is not None and accepted:
            try:
                relay.send_accepted_flags(accepted)
            except Exception:
                log.exception("failed to ship accepted flags for %s", attempt_id)
        # 终态上报(租约语义单源 assignment_session.complete_with_retry)。
        await complete_with_retry(
            assignment_client, code, attempt_id, lease_id,
            status=status, solved=solved,
            flags_found=len(accepted) if accepted else None,
            error=error,
        )
        if relay is not None:
            try:
                relay.clear_attempt()
            except Exception:
                log.exception("failed to clear obs assignment context %s", attempt_id)
    if vpn_failed:
        sys.exit(4)


async def _assignment_main(
    settings: WorkerSettings,
    cfg: SolverConfig,
    solver_backend,
    *,
    reporter,
    relay,
) -> None:
    """assignment 模式主循环；legacy list 模式保留在 amain 下方。"""

    async with AssignmentClient(
        settings.platform_url,
        settings.platform_worker_token,
        settings.worker_id,
    ) as assignment_client:
        try:
            await assignment_client.register(
                {"solver": "pi", "model": cfg.model, "worker_mode": "assignment"}
            )
        except AssignmentError as exc:
            log.error("worker registration failed: %s", exc)
            # 配置错(401/403/未配 token 的 503)停服 exit 0;瞬断 503/5xx 才 exit 3 拉起。
            config_error = exc.status_code in {401, 403} or exc.code in {
                "worker_token_required", "worker_token_not_configured",
            }
            sys.exit(0 if config_error else 3)

        while True:
            try:
                assignment = await assignment_client.claim(settings.assignment_lease_seconds)
            except AssignmentError as exc:
                log.error("job claim failed: %s", exc)
                if exc.code == "worker_not_registered":
                    # 控制面 DB 被重置/重启:补注册一次,而不是 60s 空转到天荒地老。
                    try:
                        await assignment_client.register(
                            {"solver": "pi", "model": cfg.model, "worker_mode": "assignment"}
                        )
                        continue
                    except AssignmentError as reg_exc:
                        log.error("worker re-registration failed: %s", reg_exc)
                        sys.exit(0 if reg_exc.status_code in {401, 403} else 3)
                if exc.status_code in {401, 403} or exc.code in {
                    "worker_token_required", "worker_token_not_configured",
                }:
                    sys.exit(0)
                await asyncio.sleep(IDLE_SLEEP)
                continue
            except Exception:
                log.exception("job claim transport failed")
                await asyncio.sleep(IDLE_SLEEP)
                continue

            if assignment is None:
                reporter.set(phase="idle", challenge_code="", error="")
                await asyncio.sleep(IDLE_SLEEP)
                continue

            await _solve_assignment(
                settings,
                cfg,
                solver_backend,
                assignment_client,
                assignment,
                reporter=reporter,
                relay=relay,
            )
            reporter.set(phase="idle", challenge_code="", error="")

async def amain(settings: WorkerSettings, cfg: SolverConfig, solver_backend, *,
                reporter=None, relay=None) -> None:
    """异步主循环:SDK 入口 VPN 预检 → list/start/hint/solve/submit/close 串行刷题。

    reporter/relay 由 main() 显式注入(装配单例);None 时求解照常,仅无实时推送
    (测试直调 amain 依赖此默认)。本函数不读任何模块级装配全局。
    """
    global _LOOP
    _LOOP = asyncio.get_running_loop()  # 心跳线程据此探测事件循环活性
    reporter = reporter or LiveReporter(None, None)
    # 模式分流:assignment(控制面 claim → solve → complete → canonical 关闭)
    # vs legacy(SDK list 轮询,relay run_close 关闭)。legacy 保留兼容,不删除。
    if getattr(settings, "worker_mode", "legacy") == "assignment":
        await _assignment_main(
            settings,
            cfg,
            solver_backend,
            reporter=reporter,
            relay=relay,
        )
        return
    submitted: dict[str, set[str]] = {}
    solved_ever: set[str] = set()
    provider_fail_streak = 0  # 连续 provider 失败(0-turn+报错)计数,见循环内熔断
    try:
        async with GhostmarkAsync(base_url=settings.benchmark_base_url,
                                      token=settings.benchmark_token) as client:
            while True:
                # 拉取题目;平台 is_completed 为完成状态的唯一权威
                try:
                    challenges = await client.list_challenges()
                except InvalidState:
                    log.info("task finished on platform, exiting")
                    sys.exit(0)
                except Exception as e:
                    # 鉴权/配置错(坏 BENCHMARK_TOKEN)是确定性失败:exit 0 明示停止,
                    # 不进 exit 3 的 restart 闷循环。SDK 未分类型,按状态码子串启发判断。
                    lowered = str(e).lower()
                    if "401" in lowered or "403" in lowered or "unauthor" in lowered:
                        log.error("challenge list auth failed (bad BENCHMARK_TOKEN?): %s", e)
                        sys.exit(0)
                    log.error("failed to list challenges: %s", e)
                    sys.exit(3)  # 容器 restart 策略拉起(重连 VPN/API)

                pending = [c for c in challenges if not c.is_completed and c.unique_code not in solved_ever]
                if not pending:
                    # 无待解题:回 idle(带空 code)——否则 obs live_state 与两块仪表板
                    # 永久残留上个 run 的 phase='closing'('closing' ∈ ACTIVE_PHASES →
                    # 恒显 '求解中:<最后 code>',timeline meta.live 恒真)
                    reporter.set(phase="idle", challenge_code="", error="")
                    log.info("no pending challenges, polling again in %ds", IDLE_SLEEP)
                    await asyncio.sleep(IDLE_SLEEP)
                    continue

                for ch in _prioritize(pending):
                    try:
                        solved, accepted = await solve_one(
                            client, ch, cfg=cfg, solver_backend=solver_backend,
                            reporter=reporter, relay=relay,
                            workdir_root=settings.workdir,
                            flag_format=settings.flag_format,
                            submitted=submitted)
                    except ProviderFailure as e:
                        # 会话级重试耗尽仍 0-turn+报错:计连续 streak,达阈值熔断。
                        # 说明 LLM 上游坏了而非题目难——继续循环只会静默烧完
                        # roster(2026-09-08 事故:280 run/0 flag/63 题全烧)。
                        # exit 3 复用"瞬断自动拉起"语义,给 provider/网络恢复留窗口。
                        provider_fail_streak += 1
                        log.error("provider failure streak %d/%d on %s: %s",
                                  provider_fail_streak, PROVIDER_FAILURE_EXIT_STREAK,
                                  ch.unique_code, e)
                        solved, accepted = False, []
                    except KeyboardInterrupt:
                        raise
                    except VpnCheckError as e:
                        # 运行中 VPN 掉线:按普通 unsolved 记会静默烧 roster,
                        # 必须走 exit 4 重启通道(与入口预检同语义)。
                        log.error("VPN failed mid-run on %s: %s", ch.unique_code, e)
                        sys.exit(4)
                    except Exception:
                        log.exception("unexpected error on %s", ch.unique_code)
                        provider_fail_streak = 0
                        solved, accepted = False, []
                    else:
                        provider_fail_streak = 0
                    if solved:
                        solved_ever.add(ch.unique_code)
                        log.info("=== solved %s (%d flag(s)) ===", ch.unique_code, len(accepted))
                    if provider_fail_streak >= PROVIDER_FAILURE_EXIT_STREAK:
                        log.critical(
                            "%d consecutive challenges ended provider-failure — "
                            "LLM upstream is down; exiting 3 for container restart",
                            provider_fail_streak)
                        sys.exit(3)

                # 一轮全部会话已关 → 回 idle(空 code,同上:不残留 'closing')
                reporter.set(phase="idle", challenge_code="", error="")
                log.info("=== pass done: %d pending, %d solved this run, polling in %ds ===",
                         len(pending), len(solved_ever), CYCLE_SLEEP)
                await asyncio.sleep(CYCLE_SLEEP)
    except VpnCheckError as e:
        # SDK 入口 VPN 预检失败:快速失败;容器 restart 策略在 VPN 恢复后拉起
        log.error("VPN pre-check failed: %s", e)
        sys.exit(4)


def _reporter_set(**fields) -> None:
    """模块级便捷转发(测试 monkeypatch 兼容);amain 已改走参数注入,仅保留薄壳。"""
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
    if getattr(settings, "worker_mode", "legacy") == "assignment":
        if not settings.platform_url or not settings.platform_worker_token:
            log.error("PLATFORM_URL and PLATFORM_WORKER_TOKEN must be set in assignment mode")
            sys.exit(0)
    elif not settings.benchmark_base_url or not settings.benchmark_token:
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
    # 注意:assignment 模式下 poller 只有本地题面(旧 platform/queue 客户端已删,控制面
    # job 不进 roster)——仪表板挑战列表为空不代表控制面无 job,以 claim 为准。
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
        # 数据无鉴权 → 默认只绑回环(STATUS_BIND);compose 内编排显式 0.0.0.0(proxy 转发),
        # 宿主侧再默认收成回环发布 —— 见 docker-compose.yaml 注释
        _serve_local(_LIVE, _BUS, settings.status_port,
                     workdir=settings.workdir, poller=roster_poller,
                     host=settings.status_bind)
    except Exception:
        log.exception("local status server failed to start (solving continues)")

    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()

    log.info("ghost-worker starting: model=%s base=%s",
             cfg.model, settings.benchmark_base_url)
    try:
        asyncio.run(amain(settings, cfg, create_solver(),
                          reporter=_REPORTER, relay=_RELAY))
    finally:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("interrupted by user")
        sys.exit(0)
