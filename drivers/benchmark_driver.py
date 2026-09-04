#!/usr/bin/env python3
"""
TSecBench 极简求解驱动 — 单 worker 串行刷题

形态: 一个带 VPN 的容器,常驻、一次开一道题、单会话求解、逐 flag 直接提交、
刷完轮询待命。多 flag 题未全解出时留待下一轮冷启动再试(无记忆续接)。

主循环:
  list_challenges(平台为完成状态唯一权威)
    → 未完成题按 难度升序/分值降序 排列
    → 逐题 solve_one(start → 1 次 pi 会话 → 候选 flag 去重直提 → close)
    → 全部刷完 sleep 后重新列题(新题自动纳入)

闸门只有: flag 格式候选 + 平台判分/幂等(duplicate)。无轮次/熔断/止损/
黑板记忆/验证器 LLM —— 那些机制已随 MVP 精简移除。

失败语义: 平台任务结束(409)或列表失败按旧行为退出,由容器 restart 策略拉起;
单题启动/提交失败只记日志,不 panic。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import threading
import time

# 确保 adapter 包可导入(容器内 /app 下运行时 cwd 为 /app,无需此句;宿主机直跑需要)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapter.config import SolverConfig
from adapter.live import (
    ASSISTANT_PREVIEW_MAX,
    ERROR_HEAD_MAX,
    OUTPUT_TAIL_MAX,
    LiveBus,
    LiveState,
    head_text,
    summarize_args,
    tail_text,
)
from adapter.task import AgentTask
from adapter.taskprompt import build_task_prompt, write_context_md
from adapter.solver import create_solver, normalize_flag_body, touch_heartbeat
from adapter.solver.base import extract_flags
from adapter.platform import (
    Challenge, ChallengeNotFound, InvalidState, ResourceUnavailable, create_platform,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("adapter.driver")

WORKDIR = os.getenv("ADAPTER_WORKDIR", "/work")
FLAG_FORMAT = os.getenv("ADAPTER_FLAG_FORMAT", "flag{...}")
CYCLE_SLEEP = 30          # 一轮刷完后的等待(秒)
IDLE_SLEEP = 60           # 无待解题时的轮询间隔(秒)
START_MAX_RETRIES = 8     # 单题启动重试(平台并发槽位竞争)
CLOSE_RETRIES = 3
def _parse_status_port() -> int:
    """STATUS_PORT 安全解析:import 期绝不抛;空串=禁用(0),垃圾值回退 8080 并告警"""
    raw = os.getenv("STATUS_PORT", "8080")
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        log.warning("bad STATUS_PORT=%r, falling back to 8080", raw)
        return 8080


STATUS_PORT = _parse_status_port()
WORKER_ID = os.getenv("WORKER_ID", "worker-1").strip() or "worker-1"

# ── 实时监视单例（main() 初始化；None 时求解照常，仅无推送）──
_LIVE: LiveState | None = None
_BUS: LiveBus | None = None

# 边界事件：立即落地快照文件（高频 progress/text 只走内存+SSE）
_FLUSH_KINDS = {"tool_end", "turn_done", "error", "system", "lifecycle"}


def _live_set(kind: str = "lifecycle", _immediate: bool = True, **fields) -> None:
    """update+publish 一行式；_LIVE 为 None 时无操作；异常吞掉不影响求解"""
    try:
        if _LIVE is None:
            return
        snap = _LIVE.update(_immediate=_immediate, **fields)
        if _BUS is not None and _BUS.has_subscribers():
            _BUS.publish({**snap, "kind": kind})
        if kind in _FLUSH_KINDS:
            _LIVE.flush()
    except Exception:
        pass


def _make_hooks():
    """on_fact（tool_end，接口契约）+ on_event（表驱动分发）-> LiveState/Bus"""
    found: list[str] = []  # 会话内已见 flag(去重计数,供 flags_found 实时展示)

    def on_fact(tool_name: str, args, out: str) -> None:
        for f in extract_flags(out or ""):
            if f not in found:
                found.append(f)
        # _immediate=False:update 节流 + 下方 kind 命中 _FLUSH_KINDS 的一次强制 flush,
        # 恰好一次落盘(原默认 True 会 double-persist)
        _live_set("tool_end", _immediate=False,
                  last_tool=tool_name or "",
                  last_output_tail=tail_text(out or "", OUTPUT_TAIL_MAX),
                  current_tool="",
                  flags_found=len(found))

    def on_event(kind: str, payload: dict) -> None:
        p = payload or {}
        fn = _EVENT_TABLE.get(kind)
        if fn is None:
            log.debug("unknown pi event kind dropped: %s", kind)
            return
        _live_set(kind, _immediate=False, **fn(p))

    return on_fact, on_event


def _ev_tool_start(p: dict) -> dict:
    return {"phase": "solving", "current_tool": p.get("tool", ""),
            "current_args_summary": summarize_args(p.get("args") or {}),
            "_turns_inc": 1}


def _ev_system(p: dict) -> dict:
    # pi 内部相位(stalled/timeout/stderr)归一化为 error,web 红点才能点亮;
    # 原始相位仍保留在 error 文案里
    phase = str(p.get("phase", "system"))
    return {"phase": "error" if phase in ("stalled", "timeout", "stderr") else phase,
            "error": head_text(str(p.get("detail", "")), ERROR_HEAD_MAX)}


_EVENT_TABLE = {
    "tool_start": _ev_tool_start,
    "tool_progress": lambda p: {"last_output_tail": tail_text(p.get("preview", "") or "", OUTPUT_TAIL_MAX)},
    "text": lambda p: {"assistant_preview": tail_text(p.get("preview", "") or "", ASSISTANT_PREVIEW_MAX)},
    "thinking": lambda p: {"thinking_len": int(p.get("length", 0) or 0)},
    "turn_done": lambda p: {"current_tool": ""},
    "error": lambda p: {"phase": "error", "error": head_text(str(p.get("error", "")), ERROR_HEAD_MAX)},
    "system": _ev_system,
}


# ── 排序与任务构建 ──────────────────────────────────────────

def _safe_code(code: str) -> str:
    """将 challenge code 转为安全的目录名"""
    raw = str(code)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
    return safe if safe == raw else f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


def _difficulty_rank(d: str) -> int:
    return {"easy": 0, "medium": 1, "hard": 2}.get((d or "").lower(), 1)


def _prioritize(challenges: list[Challenge]) -> list[Challenge]:
    """按难度升序、分值降序排列"""
    return sorted(challenges, key=lambda c: (_difficulty_rank(c.difficulty), -int(c.total_score or 0)))


def build_task(ch: Challenge, workdir: str, targets: list) -> AgentTask:
    """从平台 Challenge 构建 AgentTask"""
    return AgentTask(
        objective=ch.description or "Capture the flag(s) from the target.",
        targets=targets or ch.container_addr or [],
        flag_count=ch.flag_count,
        flag_format=FLAG_FORMAT,
        workdir=workdir,
        difficulty=ch.difficulty or None,
        unique_code=ch.unique_code,
        score=ch.total_score,
    )


# ── 实例启动/关闭 ───────────────────────────────────────────

def _start_with_retry(client, code: str) -> object | None:
    """带重试的实例启动;失败返回 None(平台槽位满时退避等待)"""
    for i in range(START_MAX_RETRIES):
        touch_heartbeat()
        try:
            return client.start_challenge(code)
        except InvalidState as e:
            # 409: 活跃实例达上限或任务已结束
            msg = getattr(e, "message", "") or str(e)
            if any(t in msg for t in ("上限", "active", "max")):
                wait = min(3.0 * (i + 1), 20.0)
                log.warning("max active on %s; waiting %.0fs (%d/%d)", code, wait, i + 1, START_MAX_RETRIES)
                time.sleep(wait)
                continue
            log.error("task ended (invalid_state) on %s: %s", code, msg)
            return None
        except ResourceUnavailable:
            log.warning("resource unavailable on %s, retry", code)
            time.sleep(5)
        except ChallengeNotFound:
            log.error("challenge not found: %s", code)
            return None
        except Exception as e:
            log.error("start_challenge failed on %s: %s", code, e)
            if i + 1 < START_MAX_RETRIES:
                time.sleep(3)
    log.error("giving up starting %s after %d tries", code, START_MAX_RETRIES)
    return None


def _close_with_retry(client, code: str) -> bool:
    """带重试的实例关闭"""
    for i in range(CLOSE_RETRIES):
        try:
            return client.close_challenge(code).closed
        except Exception:
            if i + 1 < CLOSE_RETRIES:
                time.sleep(min(2.0 * (i + 1), 6.0))
    log.error("FAILED to close %s after %d tries", code, CLOSE_RETRIES)
    return False


# ── 单题求解 ────────────────────────────────────────────────

def solve_one(
    client,
    ch: Challenge,
    *,
    cfg: SolverConfig,
    solver_backend,
    submitted: dict[str, set[str]],
) -> tuple[bool, list[str]]:
    """
    单题单会话求解: start → 工作目录 → 单会话 pi → 候选去重直提 → close。
    返回 (solved, 本轮正确的 flag 列表)。
    """
    code = ch.unique_code
    _live_set(phase="starting", challenge_code=code,
              started_at=time.time(), turns=0, error="",
              current_tool="", last_output_tail="",
              assistant_preview="", flags_found=0, accepted=0)
    started = _start_with_retry(client, code)
    if started is None:
        _live_set(phase="idle", error="start failed")
        return False, []

    accepted: list[str] = []
    try:
        workdir = os.path.join(WORKDIR, _safe_code(code))
        os.makedirs(workdir, exist_ok=True)
        write_context_md(workdir)
        task = build_task(ch, workdir, targets=getattr(started, "container_addr", []) or [])
        log.info("solving %s (flags=%d, done=%d, diff=%s) targets=%s",
                 code, task.flag_count, ch.correct_flag_count,
                 ch.difficulty or "?", task.target_str())

        # 单会话求解：实时 hooks -> LiveState/SSE + transcript 落盘（pi 本体零改动）
        prompt = build_task_prompt(task, flags_submitted=ch.correct_flag_count)
        transcript_path = os.path.join(workdir, "transcript.jsonl")
        on_fact, on_event = _make_hooks()
        _live_set(phase="solving", challenge_code=code,
                  transcript_path=transcript_path,
                  model=getattr(cfg, "model", ""))
        result = solver_backend.solve(prompt, workdir, cfg, flag_format=FLAG_FORMAT,
                                      on_fact=on_fact, on_event=on_event,
                                      transcript_path=transcript_path)

        # 候选去重后逐个直接提交;平台 correct/duplicate 响应即唯一闸门
        for cand in result.flags:
            norm = normalize_flag_body(cand)
            if norm in submitted.setdefault(code, set()):
                continue
            submitted[code].add(norm)
            _live_set(phase="submitting")
            try:
                r = client.submit_flag(code, cand)
            except Exception as e:
                log.error("submit failed on %s: %s", code, e)
                continue
            if r.correct:
                log.info("FLAG CORRECT on %s: %s (+%d pts, cumulative %d)",
                         code, cand[:40], r.awarded, r.cumulative_score)
                accepted.append(cand)
                if r.correct_flag_count >= r.total_flag_count:
                    log.info("solved %s (%d/%d flags)", code,
                             r.correct_flag_count, r.total_flag_count)
                    _live_set(phase="done", accepted=len(accepted),
                              flags_found=len(result.flags), error="")
                    return True, accepted
            elif r.duplicate:
                log.info("duplicate flag on %s (already accepted)", code)
            else:
                log.info("flag INCORRECT on %s: %s", code, cand[:40])

        log.info("session done on %s: %d turns, %.0fs, %d candidate(s), %d accepted",
                 code, result.turns, result.duration_s, len(result.flags), len(accepted))
        _live_set(phase="done", accepted=len(accepted),
                  flags_found=len(result.flags),
                  error=head_text(result.error) if result.error else "")
        return False, accepted
    except Exception:
        log.exception("solve_one error on %s", code)
        return False, accepted
    finally:
        # 单会话结束即释放实例(多 flag 剩题由下一轮重新 start 冷启动)
        _live_set(phase="closing")
        if not _close_with_retry(client, code):
            log.warning("challenge %s left running on platform", code)


# ── 主循环 ──────────────────────────────────────────────────

def main() -> None:
    base_url = os.getenv("BENCHMARK_BASE_URL", "").strip()
    token = os.getenv("BENCHMARK_TOKEN", "").strip()
    if not base_url or not token:
        log.error("BENCHMARK_BASE_URL and BENCHMARK_TOKEN must be set")
        sys.exit(2)

    try:
        cfg = SolverConfig.from_env()
    except ValueError as e:
        # 裸 SOLVER_MODEL/垃圾 SESSION_SECONDS 等:明示退出码,不进 restart 闷循环
        log.error("bad solver config: %s", e)
        sys.exit(2)
    os.makedirs(WORKDIR, exist_ok=True)
    touch_heartbeat()

    # 实时监视：LiveState（原子文件 /work/.live/<worker>.json）+ SSE 广播线程
    global _LIVE, _BUS
    _LIVE = LiveState(worker_id=WORKER_ID,
                      state_path=os.path.join(WORKDIR, ".live", f"{WORKER_ID}.json"))
    _BUS = LiveBus()
    try:
        from drivers.status_server import serve_forever_in_thread
        serve_forever_in_thread(_LIVE, _BUS, STATUS_PORT, workdir=WORKDIR)
    except Exception:
        log.exception("status server failed to start (solving continues)")

    # 独立心跳线程: 进程存活即刷新 /tmp/driver_heartbeat(compose healthcheck 依据)
    def _heartbeat_loop():
        while True:
            time.sleep(30)
            try:
                touch_heartbeat()
            except Exception:
                pass

    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()

    client = create_platform(base_url, token)
    solver_backend = create_solver()
    submitted: dict[str, set[str]] = {}
    solved_ever: set[str] = set()
    log.info("tsecbench-adapter starting: model=%s base=%s", cfg.model, base_url)

    while True:
        # 拉取题目;平台 is_completed 为完成状态的唯一权威
        try:
            challenges = client.list_challenges()
        except InvalidState:
            log.info("task finished on platform, retrying later")
            time.sleep(IDLE_SLEEP)
            continue
        except Exception as e:
            log.error("failed to list challenges: %s", e)
            sys.exit(3)  # 容器 restart 策略拉起(重连 VPN/API)

        pending = [c for c in challenges if not c.is_completed and c.unique_code not in solved_ever]
        if not pending:
            log.info("no pending challenges, polling again in %ds", IDLE_SLEEP)
            time.sleep(IDLE_SLEEP)
            continue

        for ch in _prioritize(pending):
            try:
                solved, accepted = solve_one(client, ch, cfg=cfg, solver_backend=solver_backend,
                                             submitted=submitted)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("unexpected error on %s", ch.unique_code)
                solved, accepted = False, []
            if solved:
                solved_ever.add(ch.unique_code)
                log.info("=== solved %s (%d flag(s)) ===", ch.unique_code, len(accepted))

        log.info("=== pass done: %d pending, %d solved this run, polling in %ds ===",
                 len(pending), len(solved_ever), CYCLE_SLEEP)
        time.sleep(CYCLE_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("interrupted by user")
        sys.exit(0)
