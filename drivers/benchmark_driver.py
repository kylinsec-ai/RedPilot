#!/usr/bin/env python3
"""
TsecBench 基准测试驱动器

主驱动：调度、长会话重访、声明式提交。
参考 hxbai 的 benchmark_driver.py 架构实现。

流程:
1. 从答题 API 拉取题目列表，按难度和分值排序
2. 每道题分配工作目录，写入工具清单和题目上下文
3. 启动 Pi Agent 子会话解题
4. 子会话确证 flag 后写入 FLAG 文件
5. 控制器读取 FLAG 文件，经验证后提交
6. 未解出的题目挂起，后续轮次以递增时间盒重访
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import replace

# 确保 adapter 包可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapter.config import SolverConfig, ControllerConfig, build_verifier_config
from adapter.task import AgentTask
from adapter.verify import Verifier, flag_confidence, normalize_flag_body
from adapter.solver import create_solver, touch_heartbeat
from adapter.blackboard import Blackboard
from adapter.stoploss import StopLoss
from adapter.scheduler import run_fleet
from adapter.taskprompt import build_task_prompt, write_context_md, write_memory
from adapter.platform_client import (PlatformClient, RateLimitedClient, Challenge,
                                     InvalidState, APIError, ChallengeNotFound,
                                     ResourceUnavailable, VpnCheckError)
from adapter import observability as obs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("adapter.driver")

# ── 全局状态 ──────────────────────────────────────────────

_MAX_ACTIVE_RETRIES = int(os.getenv("ADAPTER_MAX_ACTIVE_RETRIES", "8"))
_SHARED_BOARDS: dict = {}
_BOARDS_LOCK = threading.Lock()

# ── 运行状态（Web 控制面板读取）─────────────────────────────
_STATUS: dict = {
    "worker_id": 0,
    "started_at": time.time(),
    "last_beat": time.time(),
    "current_code": "",
    "current_difficulty": "",
    "current_round": 0,
    "sessions": 0,
    "flags_found": [],
    "flags_submitted": 0,
    "total_earned": 0,
    "challenges_solved": 0,
    "last_event": "",
    "last_log": "",
}
_STATUS_LOCK = threading.Lock()


def _status_path() -> str:
    workdir = os.getenv("ADAPTER_WORKDIR", "/work")
    return os.path.join(workdir, "status", f"worker-{_worker_id()}.json")


def _update_status(**kw) -> None:
    with _STATUS_LOCK:
        _STATUS.update(kw)
        _STATUS["last_beat"] = time.time()
    try:
        p = _status_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(_STATUS, f, ensure_ascii=False)
    except Exception:
        pass


def _shared_board_for(code: str, workdir: str) -> Blackboard:
    with _BOARDS_LOCK:
        b = _SHARED_BOARDS.get(code)
        if b is None:
            b = Blackboard(os.path.join(workdir, "_blackboard.json"))
            _SHARED_BOARDS[code] = b
        return b


def _safe_code(code: str) -> str:
    """将 challenge code 转为安全的目录名"""
    raw = str(code)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
    return safe if safe == raw else f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


def _difficulty_rank(d: str) -> int:
    return {"easy": 0, "medium": 1, "hard": 2}.get((d or "").lower(), 1)


def _prioritize(challenges: list[Challenge]) -> list[Challenge]:
    """按难度升序、分值降序排列"""
    pending = [c for c in challenges if not c.is_completed]
    return sorted(pending, key=lambda c: (
        _difficulty_rank(c.difficulty),
        -int(c.total_score or 0),
    ))


def build_task(ch: Challenge, workdir: str, targets: list = None) -> AgentTask:
    """从 Challenge 构建 AgentTask — 对接真实 API 字段"""
    return AgentTask(
        objective=ch.description or "Capture the flag(s) from the target.",
        targets=targets or ch.container_addr or [],
        flag_count=ch.flag_count,
        flag_format=os.getenv("ADAPTER_FLAG_FORMAT", "flag{...}"),
        workdir=workdir,
        category=None,  # API 不返回 category，由 skill_loader 自动推断
        difficulty=ch.difficulty or None,
        unique_code=ch.unique_code,
        score=ch.total_score,
    )


# ── 启动/关闭实例 ──────────────────────────────────────────

def _start_with_retry(client, code: str, *, stop_event, retries=None):
    """带重试的实例启动 — 对接真实 API 异常"""
    max_retries = retries or _MAX_ACTIVE_RETRIES
    for i in range(max_retries):
        _beat()
        if stop_event.is_set():
            return None, "stop"
        try:
            return client.start_challenge(code), None
        except InvalidState as e:
            # 409: 活跃实例达上限(3个) 或 任务已结束
            if "上限" in e.message or "active" in e.message.lower() or "max" in e.message.lower():
                wait_s = min(3.0 * (i + 1), 20.0)
                log.warning("max active on %s; waiting %.0fs (%d/%d)",
                            code, wait_s, i + 1, max_retries)
                time.sleep(wait_s)
                continue
            else:
                # 任务已结束
                log.error("task ended (invalid_state): %s", e)
                stop_event.set()
                return None, "stop"
        except ResourceUnavailable as e:
            log.warning("resource unavailable on %s: %s, retry", code, e)
            if i + 1 < max_retries:
                time.sleep(5)
                continue
        except ChallengeNotFound:
            log.error("challenge not found: %s", code)
            return None, "not_found"
        except Exception as e:
            log.error("start_challenge failed: %s", e)
            if i + 1 < max_retries:
                time.sleep(3)
                continue
            raise
    return None, "retry"


def _close_with_retry(client, code: str, *, retries: int = 3):
    """带重试的实例关闭"""
    for i in range(retries):
        try:
            result = client.close_challenge(code)
            return result.closed if hasattr(result, 'closed') else True
        except Exception:
            if i + 1 < retries:
                time.sleep(min(2.0 * (i + 1), 6.0))
            else:
                log.error("FAILED to close %s after %d tries", code, retries)
    return False


# ── 单题求解 ──────────────────────────────────────────────

def _beat() -> None:
    """心跳（docker healthcheck 依据 /tmp/driver_heartbeat）+ 状态文件刷新"""
    touch_heartbeat()
    _update_status()


def _worker_shard(challenges: list) -> list:
    """
    Worker 分片：每个容器只处理自己分到的题目子集。

    - ADAPTER_WORKER_COUNT: worker 总数（默认 1 = 不分片）
    - ADAPTER_WORKER_ID:    本 worker 序号 0..count-1
      未设置时从容器 hostname 尾号推导（compose --scale 场景）:
      tsecbench-adapter-adapter-1/2/3 → id 0/1/2
    """
    count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
    if count <= 1:
        return challenges

    wid = _worker_id()
    shard = [c for i, c in enumerate(challenges) if i % count == wid]
    log.info("worker %d/%d: %d challenges assigned", wid, count, len(shard))
    return shard


# ── 优先任务队列（网页「Agent 解此题」派单给舰队）──────────

def _worker_id() -> int:
    """当前 worker 序号 — 状态文件命名与分片都以此为准。

    - ADAPTER_WORKER_ID: 本 worker 序号 0..count-1
      未设置时从容器 hostname 尾号推导（compose --scale 场景）:
      tsecbench-adapter-adapter-1/2/3 → id 0/1/2
    """
    count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
    wid_raw = os.environ.get("ADAPTER_WORKER_ID", "")
    wid = -1
    if wid_raw.strip() != "":
        try:
            wid = int(wid_raw) % count
        except ValueError:
            wid = -1
    if wid < 0:
        host = os.environ.get("HOSTNAME", "")
        m = re.search(r"-(\d+)$", host)
        if m:
            wid = (int(m.group(1)) - 1) % count
    if wid < 0:
        wid = 0
    return wid


def _load_priority(workdir: str, wid: int) -> list[str]:
    """读取优先任务文件（work/priority.txt，行格式: unique_code|worker_id）。

    只返回分配给本 worker 的优先题 code。
    """
    path = os.path.join(workdir, "priority.txt")
    codes: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                code = parts[0].strip()
                if not code:
                    continue
                if len(parts) > 1 and parts[1].strip():
                    try:
                        if int(parts[1].strip()) != wid:
                            continue
                    except ValueError:
                        pass
                if code not in codes:
                    codes.append(code)
    except OSError:
        pass
    return codes


def _apply_priority(challenges: list, workdir: str, wid: int) -> list:
    """把分配给本 worker 的优先题提到最前（未完成的）。"""
    prio = _load_priority(workdir, wid)
    if not prio:
        return challenges
    prio_set = set(prio)
    early = [c for c in challenges if c.unique_code in prio_set]
    rest = [c for c in challenges if c.unique_code not in prio_set]
    if early:
        log.info("priority queue for worker %d: %s", wid,
                 ",".join(c.unique_code for c in early))
    return early + rest


def _claim_priority(shard: list, all_challenges: list, workdir: str, wid: int) -> list:
    """分片后认领派单题：本 worker 的优先题若不在分片里，强制加入最前。

    网页「Agent 解此题」把题派给指定 worker，但分片按序号取模，
    派单题可能落在其它 worker 的分片 —— 这里确保被派单的 worker 能处理它。
    """
    prio = _load_priority(workdir, wid)
    if not prio:
        return shard
    have = {c.unique_code for c in shard}
    claimed = [c for c in all_challenges if c.unique_code in prio and c.unique_code not in have]
    if claimed:
        log.info("claim priority for worker %d: %s", wid,
                 ",".join(c.unique_code for c in claimed))
    return claimed + shard


def _worker_concurrency() -> int:
    """
    单容器内的 Pi Agent 并发数。
    worker 模式（count>1）下固定 1（一个容器一个 pi 进程，一次一道题）；
    单容器模式可用 ADAPTER_WORKER_CONCURRENCY 调整（默认 1）。
    """
    count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
    if count > 1:
        return 1
    try:
        return max(1, int(os.environ.get("ADAPTER_WORKER_CONCURRENCY", "1") or "1"))
    except ValueError:
        return 1


def _start_vpn_watchdog(client, *, interval: int = 60, failures_before_exit: int = 3):
    """
    VPN 断线看门狗（后台线程）。
    周期执行 VPN 预检；连续失败 N 次 → 记录日志并退出进程，
    由容器 restart 策略自动重启重连 VPN。
    VPN 预检不可用（后端无 check_vpn）时静默退出。
    """
    def _loop():
        fails = 0
        while True:
            time.sleep(interval)
            try:
                vpn = client.check_vpn(timeout=8)
                if vpn.ok:
                    fails = 0
                    continue
                fails += 1
                log.warning("VPN check failed (%d/%d): status=%r",
                            fails, failures_before_exit, vpn.status)
            except VpnCheckError as e:
                fails += 1
                log.warning("VPN check failed (%d/%d): reason=%s",
                            fails, failures_before_exit, getattr(e, "reason", "unknown"))
            except Exception as e:
                log.warning("VPN watchdog check error: %s (treated as pass)", e)
                fails = 0
                continue
            if fails >= failures_before_exit:
                log.error("VPN 断线超过 %d 次，重启容器以重连 VPN...", failures_before_exit)
                os._exit(4)

    th = threading.Thread(target=_loop, daemon=True, name="vpn-watchdog")
    th.start()
    log.info("VPN watchdog started (interval=%ds, exit after %d failures)",
             interval, failures_before_exit)


def solve_one(
    client: RateLimitedClient,
    ch: Challenge,
    visit_seconds: int,
    round_idx: int,
    *,
    solver: SolverConfig,
    ctrl: ControllerConfig,
    verifier: Verifier,
    stoploss: StopLoss,
    stop_event: threading.Event,
    submitted: dict,
    submitted_lock: threading.Lock,
    prio_codes: set = frozenset(),
) -> dict:
    """
    单题求解主逻辑。

    返回: {"solved": bool, "outcome": str, "flags": list}
    """
    code = ch.unique_code
    obs.context(challenge_id=str(code), attempt_id=str(round_idx))
    _update_status(current_code=code, current_difficulty=ch.difficulty or "",
                   current_round=round_idx + 1, last_event=f"visit {code}")

    # 止损检查
    stop, reason = stoploss.should_stop(code)
    if stoploss.rearm_dry(code):
        log.info("  %s stuck readmitted — dry window rearmed", code)
        stop = False
        reason = ""
    if stop or stop_event.is_set():
        return {"solved": False, "outcome": "dropped", "reason": reason}

    # 启动实例（派单题等待更耐心：槽位竞争时坚持等，不轻易轮换跳过）
    start_retries = 30 if code in prio_codes else None
    started, outcome = _start_with_retry(
        client, code, stop_event=stop_event, retries=start_retries)
    if started is None:
        return {"solved": False, "outcome": outcome or "start_failed"}

    workdir = os.path.join(ctrl.workdir, _safe_code(code))
    os.makedirs(workdir, exist_ok=True)
    write_context_md(workdir)

    targets = started.container_addr if hasattr(started, 'container_addr') else []
    task = build_task(ch, workdir, targets=targets)
    board = _shared_board_for(code, workdir)
    board.objective = task.objective
    stoploss.start(code, multi_flag=task.flag_count > 1)

    # 求解器后端（无状态，会话循环外创建一次）
    solver_backend = create_solver(
        model=os.environ.get("ADAPTER_SOLVER_MODEL", ""),
        skills_dir=os.environ.get("ADAPTER_SKILLS_DIR", ""),
    )

    log.info("round %d visit %s (flags=%d, diff=%s, visit<=%ds) targets=%s",
             round_idx + 1, code, task.flag_count,
             ch.difficulty or "?", visit_seconds, targets)

    solved = False
    accepted_flags = []
    session_idx = 0
    visit_deadline = time.monotonic() + max(60, visit_seconds)

    try:
        while time.monotonic() < visit_deadline and not stop_event.is_set():
            _beat()
            # 止损检查
            stop, reason = stoploss.should_stop(code)
            if stop:
                log.info("  stop-loss on %s: %s", code, reason)
                break

            sess_secs = min(
                solver.session_seconds,
                int(visit_deadline - time.monotonic()),
                stoploss.remaining_seconds(code),
            )
            if sess_secs < 60:
                break

            # 读取前次记忆
            prior_mem = os.path.join(workdir, "MEMORY.md")

            # 构建 prompt
            with submitted_lock:
                done_count = len(submitted.get(code, set()))
            prompt = build_task_prompt(
                task, board,
                prior_memory_path=prior_mem if os.path.isfile(prior_mem) else None,
                session_idx=session_idx,
                flags_submitted=done_count,
            )

            # 准备 solver 配置
            solver_this = replace(solver, session_seconds=max(60, sess_secs))

            new_facts = [0]

            def _on_fact(tool, args, output, _nf=new_facts):
                _nf[0] += board.observe(tool, args or {}, output or "", iter=session_idx)

            # 转录路径
            tpath = os.path.join(workdir, "_transcripts",
                                 f"round{round_idx}_session{session_idx}.jsonl")

            obs.emit("session_start", layer="driver",
                     payload={"code": code, "round": round_idx, "idx": session_idx})

            _beat()

            # 执行 Pi Agent 会话（唯一求解引擎）
            result = solver_backend.solve(
                prompt, workdir, solver_this,
                flag_format=task.flag_format,
                on_fact=_on_fact,
                transcript_path=tpath,
            )

            obs.emit("session_end", layer="driver",
                     payload={"code": code, "round": round_idx, "idx": session_idx,
                              "turns": result.turns, "flags": len(result.flags),
                              "infra_blocked": result.infra_blocked})

            # INFRA_BLOCKED 处理
            if result.infra_blocked and not result.flags:
                stoploss.record_unreachable(code)
                log.info("  %s INFRA_BLOCKED — backing off", code)
                break

            stoploss.record_reachable(code)

            # 事实更新
            if new_facts[0] > 0:
                stoploss.record_fact(code)
            else:
                stoploss.record_no_progress(code)

            # 候选 = 会话提取结果（pi_agent 已含 FLAG 文件补录）
            all_candidates = set(result.flags)

            # 验证并提交 flag
            for flag_candidate in all_candidates:
                with submitted_lock:
                    if code in submitted and normalize_flag_body(flag_candidate) in submitted[code]:
                        continue

                # 置信度评估
                claim = flag_confidence(
                    flag_candidate,
                    result.observed_output,
                    result.tool_outputs,
                )

                # 三重验证
                claim = verifier.verify(claim)

                if claim.verified:
                    # 真实 API: POST /openapi/v1/challenges/submit
                    # 返回: {correct, awarded, cumulative_score, correct_flag_count, ...}
                    submit_result = client.submit_flag(code, flag_candidate)
                    obs.emit("flag_submit", layer="driver",
                             payload={"code": code, "flag": flag_candidate[:20] + "...",
                                      "correct": submit_result.correct,
                                      "awarded": submit_result.awarded,
                                      "duplicate": submit_result.duplicate})

                    with submitted_lock:
                        submitted.setdefault(code, set()).add(normalize_flag_body(flag_candidate))

                    if submit_result.correct:
                        log.info("  FLAG CORRECT on %s: %s (+%d pts, total %d)",
                                 code, flag_candidate[:30],
                                 submit_result.awarded, submit_result.cumulative_score)
                        accepted_flags.append(flag_candidate)
                        stoploss.record_flag(code)
                        _update_status(
                            flags_submitted=len(accepted_flags),
                            total_earned=submit_result.cumulative_score,
                            last_event=f"FLAG CORRECT {flag_candidate[:20]}",
                            last_log=f"FLAG CORRECT on {code}: +{submit_result.awarded} pts",
                        )
                        # 检查是否所有 flag 都已提交
                        if submit_result.correct_flag_count >= submit_result.total_flag_count:
                            solved = True
                    elif submit_result.duplicate:
                        log.info("  duplicate flag on %s (already submitted)", code)
                    else:
                        log.info("  flag INCORRECT on %s: %s", code, flag_candidate[:30])

            if solved:
                _update_status(challenges_solved=int(_STATUS["challenges_solved"]) + 1,
                               last_event=f"solved {code}")
                break

            # 写入记忆
            mem_content = board.actionable_assets()
            if mem_content.strip():
                write_memory(workdir, mem_content)

            session_idx += 1
            _update_status(sessions=session_idx)

    except Exception as e:
        log.exception("solve_one error on %s", code)
        obs.emit("error", layer="driver",
                 payload={"code": code, "error": str(e)[:200]})
    finally:
        board.flush()  # 落盘节流中未保存的事实
        # 关闭实例
        if not solved or task.flag_count <= len(accepted_flags):
            _close_with_retry(client, code)

    return {
        "solved": solved,
        "outcome": "solved" if solved else "done",
        "flags": accepted_flags,
        "turns": getattr(result, "turns", 0),
        "api_error": bool(result.error and any(
            token in result.error for token in ("402", "401", "Insufficient", "Authentication", "Balance")
        )),
    }


# ── 多轮调度 ──────────────────────────────────────────────

def schedule_rounds(
    challenges: list[Challenge],
    client: RateLimitedClient,
    *,
    all_challenges: list[Challenge] | None = None,
    solver: SolverConfig,
    ctrl: ControllerConfig,
    verifier: Verifier,
    stoploss: StopLoss,
    stop_event: threading.Event,
) -> tuple[set, set]:
    """
    多轮调度主循环。

    每轮给每道未解题目一次访问，时间盒逐轮递增。
    """
    solved: set = set()
    dropped: set = set()
    submitted: dict = {}
    submitted_lock = threading.Lock()
    t0 = time.monotonic()
    rnd = 0
    # ── API 熔断（余额/认证故障防护）──────────────────
    # 连续 3 次会话 0 turns / API 错误（402/401）→ 暂停 300s，
    # 累计暂停 5 次 → 优雅退出。避免余额耗尽时疯狂开关靶场。
    api_fail_streak = 0
    api_pause_count = 0
    API_PAUSE_SECONDS = int(os.environ.get("ADAPTER_API_PAUSE_SECONDS", "300"))
    API_PAUSE_LIMIT = int(os.environ.get("ADAPTER_API_PAUSE_LIMIT", "5"))

    while not stop_event.is_set() and (time.monotonic() - t0) <= ctrl.total_seconds:
        # 熔断检查：上一轮 API 连续失败 → 暂停（不调度、不开关靶场）
        if api_fail_streak >= 3:
            api_pause_count += 1
            log.warning("API 连续失败 %d 次（疑似余额/认证问题）— 暂停 %ds (第 %d/%d 次)",
                        api_fail_streak, API_PAUSE_SECONDS, api_pause_count, API_PAUSE_LIMIT)
            if api_pause_count >= API_PAUSE_LIMIT:
                log.warning("API 暂停达到上限，优雅退出（请检查模型余额/API Key 后重新启动舰队）")
                break
            time.sleep(API_PAUSE_SECONDS)
            api_fail_streak = 0
            continue

        _beat()
        # 派单题永不 dropped：用户明确指定要解的题，排队等待期间不被放弃
        prio_codes = set(_load_priority(ctrl.workdir, _worker_id()))
        pending = [c for c in challenges
                   if c.unique_code not in solved
                   and (c.unique_code in prio_codes or c.unique_code not in dropped)]
        if not pending:
            break

        # 优先任务队列（prio_codes 本轮已读一次）：
        # 1) 运行中新派单的题可能不在本 worker 列表里，先认领（claim）
        # 2) 认领回来的题再次排除已 solved/dropped（防止已通关的派单题被加回）
        # 3) 已在列表里的派单题排到最前
        if all_challenges:
            pending = _claim_priority(pending, all_challenges, prio_codes)
        pending = [c for c in pending
                   if c.unique_code not in solved
                   and (c.unique_code in prio_codes or c.unique_code not in dropped)]
        pending = _apply_priority(pending, prio_codes)
        if not pending:
            break

        # 当前轮的时间盒（按难度分级，越靠后轮次乘数越大）
        factors = ctrl.round_factors
        factor = factors[min(rnd, len(factors) - 1)]
        base_e, base_m, base_h = ctrl.timebox_easy, ctrl.timebox_medium, ctrl.timebox_hard

        log.info("=== ROUND %d — %d challenges (timebox easy=%ds medium=%ds hard=%ds x%.1f, %.0f/%ds budget) ===",
                 rnd + 1, len(pending),
                 int(base_e * factor), int(base_m * factor), int(base_h * factor),
                 factor, time.monotonic() - t0, ctrl.total_seconds)

        def _visit(ch, _r=rnd):
            vs = int(ctrl.timebox_for_difficulty(ch.difficulty) * factors[min(_r, len(factors) - 1)])
            return solve_one(
                client, ch, vs, _r,
                solver=solver, ctrl=ctrl, verifier=verifier,
                stoploss=stoploss, stop_event=stop_event,
                submitted=submitted, submitted_lock=submitted_lock,
                prio_codes=prio_codes,
            )

        results = run_fleet(
            pending, _visit,
            is_success=lambda r: bool(r and r.get("solved")),
            # worker 模式（多容器扩展）下每容器只跑一个 Pi Agent
            max_concurrent=min(ctrl.max_concurrency, _worker_concurrency()),
            best_of=ctrl.best_of,
        )

        for c in pending:
            r = results.get(c.unique_code) or {}
            if r.get("solved"):
                solved.add(c.unique_code)
            elif r.get("outcome") == "dropped":
                dropped.add(c.unique_code)
            elif stoploss.should_stop(c.unique_code)[0]:
                dropped.add(c.unique_code)
            # API 故障计数（0 turns / 402/401）
            if r.get("api_error") or (r.get("turns", 0) == 0 and not r.get("solved")):
                api_fail_streak += 1
            elif r.get("turns", 0) > 0:
                api_fail_streak = 0

        log.info("=== ROUND %d done — solved=%d dropped=%d remaining=%d ===",
                 rnd + 1, len(solved), len(dropped),
                 len(challenges) - len(solved) - len(dropped))
        rnd += 1

    return solved, dropped


# ── 主入口 ──────────────────────────────────────────────

def main():
    base_url = os.getenv("BENCHMARK_BASE_URL", "")
    token = os.getenv("BENCHMARK_TOKEN", "")
    if not base_url or not token:
        log.error("BENCHMARK_BASE_URL and BENCHMARK_TOKEN must be set.\n"
                  "  For local eval: set them from the platform page and connect VPN first.\n"
                  "  For hosted mode: these are injected by the platform.")
        sys.exit(2)

    # 单题模式（网页「单独自动解」触发的定向 Agent）：
    # ADAPTER_CHALLENGE_ONLY 指定只处理一道题，且不干预常驻舰队容器
    only = os.getenv("ADAPTER_CHALLENGE_ONLY", "").strip()

    # 加载配置
    solver = SolverConfig.from_env()
    ctrl = ControllerConfig.from_env()

    os.makedirs(ctrl.workdir, exist_ok=True)
    _beat()  # 启动即写心跳，避免 healthcheck 误判

    # 独立心跳线程：只要 driver 进程存活就持续写心跳，
    # 健康检查语义 = "进程存活"（会话卡死由会话级看门狗重建，不会影响心跳）
    def _heartbeat_loop():
        while True:
            time.sleep(30)
            try:
                _beat()
            except Exception:
                pass

    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()
    obs.configure(os.path.join(ctrl.workdir, "_events.jsonl"),
                  run_id=f"adapter-{solver.provider}")
    obs.emit("run_start", layer="driver",
             payload={"provider": solver.provider, "model": solver.model,
                      "max_concurrency": ctrl.max_concurrency})

    log.info("tsecbench-adapter starting: provider=%s model=%s base=%s",
             solver.provider, solver.model, solver.base_url)

    # 初始化验证器
    verifier_cfg = build_verifier_config(solver)
    llm = None
    if verifier_cfg.is_usable():
        try:
            from adapter.llm import LLMClient
            llm = LLMClient(verifier_cfg)
        except Exception as e:
            log.warning("verifier LLM unavailable (%s); degrading to grounding-only", e)
    verifier = Verifier(llm, skeptic_votes=ctrl.skeptic_votes)

    # 止损器
    stoploss = StopLoss(
        per_challenge_seconds=ctrl.per_challenge_seconds,
        max_sessions=ctrl.max_sessions_per_challenge,
        dry_cutoff=ctrl.dry_facts_cutoff,
    )

    # 初始化平台客户端
    raw_client = PlatformClient(base_url, token)
    client = RateLimitedClient(raw_client, ctrl.min_request_interval)

    # VPN 联通预检（强制前置，平台要求最先执行）
    # 预检地址只在 VPN 网络内可达；status == "ok" 才视为通过
    try:
        vpn = raw_client.check_vpn(timeout=10)
        if not vpn.ok:
            log.error("VPN check failed: status=%r — 请检查靶场VPN网络配置", vpn.status)
            raise SystemExit(2)
        log.info("VPN check passed: client_ip=%s (%s)", vpn.client_ip, vpn.time)
    except VpnCheckError as e:
        log.error("VPN检测未通过,请检查靶场VPN网络配置 (reason=%s)", getattr(e, "reason", "unknown"))
        raise SystemExit(2)
    except Exception as e:
        log.warning("VPN precheck skipped (backend %s has no vpn check): %s",
                    getattr(raw_client.backend, "name", "?"), e)

    # VPN 看门狗：VPN 断线（连续 3 次预检失败）→ 退出进程
    # 容器 restart: unless-stopped 会自动重启并重连 VPN，避免无限无效解题
    _start_vpn_watchdog(raw_client, interval=60, failures_before_exit=3)

    # 获取挑战列表
    try:
        challenges = client.list_challenges()
    except InvalidState:
        # 平台判定任务已结束（409 invalid_state / task already finished）：
        # 优雅退出（exit 0），避免 restart 策略反复重启空转
        log.info("task finished on platform, exiting gracefully")
        sys.exit(0)
    except APIError as e:
        if e.status != 409:
            log.error("failed to list challenges: %s", e)
            sys.exit(3)
        log.info("task finished on platform, exiting gracefully")
        sys.exit(0)
    except Exception as e:
        log.error("failed to list challenges: %s", e)
        sys.exit(3)

    log.info("loaded %d challenges", len(challenges))

    # 同步平台通关状态：重启后不重访已通关的题（平台为准）
    try:
        platform_done = {c.unique_code for c in challenges if getattr(c, "is_completed", False)}
        if platform_done:
            challenges = [c for c in challenges if c.unique_code not in platform_done]
            log.info("skipping %d already-completed challenges", len(platform_done))
    except Exception:
        pass

    # 过滤和排序
    challenges = _prioritize(challenges)
    if not challenges:
        log.info("no pending challenges, nothing to do")
        return

    # 单题模式：只保留指定题
    if only:
        challenges = [c for c in challenges if c.unique_code == only]
        if not challenges:
            log.info("ADAPTER_CHALLENGE_ONLY=%s: challenge not found or already solved", only)
            return
        log.info("single-challenge mode: only %s (score=%d)", only, challenges[0].total_score)

    # Worker 分片：多容器水平扩展（docker compose up -d --scale adapter=N）
    # 每个容器一个 Pi Agent 进程，同一时间只解一道题；
    # worker id 由 ADAPTER_WORKER_ID 指定，未指定时从 hostname 尾号推导
    all_challenges = challenges  # 保留全量列表，供派单题认领
    challenges = _worker_shard(challenges)
    if not challenges:
        log.info("no challenges assigned to this worker, nothing to do")
        return

    # 优先任务队列（网页「Agent 解此题」派单）：本 worker 的优先题排最前，
    # 不在分片内的派单题强制认领
    prio_codes = set(_load_priority(ctrl.workdir, _worker_id()))
    challenges = _claim_priority(challenges, all_challenges, prio_codes)
    challenges = _apply_priority(challenges, prio_codes)
    if not challenges:
        log.info("no challenges assigned to this worker, nothing to do")
        return

    log.info("pending: %d challenges (first: %s, last: %s)",
             len(challenges), challenges[0].unique_code, challenges[-1].unique_code)

    # 执行多轮调度
    stop_event = threading.Event()
    try:
        solved, dropped = schedule_rounds(
            challenges, client,
            all_challenges=all_challenges,
            solver=solver, ctrl=ctrl,
            verifier=verifier, stoploss=stoploss,
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        log.info("interrupted by user")
        stop_event.set()
        solved, dropped = set(), set()
    except Exception:
        log.exception("fatal error in schedule_rounds")
        solved, dropped = set(), set()

    # 最终报告
    total = len(challenges)
    unsolved = total - len(solved) - len(dropped)
    log.info("=== FINAL: total=%d solved=%d dropped=%d unsolved=%d ===",
             total, len(solved), len(dropped), unsolved)
    obs.emit("run_end", layer="driver",
             payload={"total": total, "solved": len(solved),
                      "dropped": len(dropped), "unsolved": unsolved})
    obs.close()


if __name__ == "__main__":
    main()
