"""单题求解编排 — 从主树 benchmark_driver 重推导(行为为权威,worktree 仅结构参考)。

solve_one: start 重试 → hint(每题无条件取,平台规则扣分) → 工作目录 → 单会话 pi
(经 to_thread 由 driver 调度)→ 候选去重直提(平台 correct/duplicate 即唯一闸门)
→ finally close(收尾帧附 _accepted_flags 带外明文)。

与 worktree 版本的行为差异(全部有主树依据,不得"简化"回去):
- hint 无条件拉取(taskprompt 有专门章节渲染);
- DuplicateSubmit 异常处理(SDK 抛异常而非返回 duplicate 字段);
- dedup 仅在平台响应后标记,键为原始文本(平台按原文哈希判分,归一化键会误杀);
- 收尾帧 _extra={"_accepted_flags": [...]}(FLAG 文件含被拒候选,不能作 accepted);
- compress 前 relay.flush_run()(压缩重写前排干中继未读字节)。
"""

from __future__ import annotations

import logging
import os
import time

from ._sdk import (
    Challenge,
    ChallengeNotFound,
    DuplicateSubmit,
    InvalidState,
    ResourceUnavailable,
    GhostmarkAsync,
    VpnCheckError,
)

from ghost_contracts.paths import safe_code
from ghost_contracts.text import ASSISTANT_PREVIEW_MAX, ERROR_HEAD_MAX, head_text, tail_text
from ghost_contracts.vocabulary import FLUSH_KINDS

from .config import SolverConfig
from .flags import extract_flags, is_valid_flag
from .solver import touch_heartbeat  # noqa: F401 (兼容重导出,心跳单源 contracts.paths)
from .solver.base import AgentAdapter
from .target import CLOSE_RETRIES, START_MAX_RETRIES, close_target, start_target
from .task import AgentTask
from .taskprompt import build_task_prompt, write_context_md
from .transcripts import compress_transcript

log = logging.getLogger("ghost_worker.orchestration")

# 靶场生命周期常量/实现单源 target.py;此处重导出保持旧 import 路径兼容。
# (from .orchestration import START_MAX_RETRIES / _start_with_retry 仍可用)
__all__ = ["solve_one", "build_task", "_prioritize", "LiveReporter", "make_live_hooks",
           "START_MAX_RETRIES", "CLOSE_RETRIES", "PROVIDER_FAILURE_RETRIES", "ProviderFailure",
           "_start_with_retry", "_close_with_retry"]
PROVIDER_FAILURE_RETRIES = 2  # 0-turn+报错(provider 失败)的会话级重开次数


class ProviderFailure(Exception):
    """provider 失败(0-turn 会话+报错)耗尽重试 — driver 据此计数熔断(exit 3)。

    2026-09-08 事故:pi 对 provider 400 只发 stopReason=error 的收尾消息,
    编排层误作"正常完成"静默烧题库(280 run/0 flag/63 题)。本异常把
    "LLM 上游坏了"从普通未解出中区分出来,由 driver 决定熔断。
    """


__all__ = ["solve_one", "build_task", "_prioritize", "LiveReporter", "make_live_hooks",
           "START_MAX_RETRIES", "CLOSE_RETRIES", "PROVIDER_FAILURE_RETRIES", "ProviderFailure"]


# ── 实时监视报告器(重推导 _live_set,单例改注入式) ────────────

class LiveReporter:
    """update+publish 一行式;live 为 None 时无操作;异常吞掉不影响求解。

    update 节流保存;需要落盘的边界事件由 kind 命中 FLUSH_KINDS(contracts)
    的一次强制 flush 完成,恰好一次落盘。
    _extra:只走总线信封、不进 LiveState 快照的带外元数据(键以下划线开头,
    中继不落库;如 run 收尾时附带的平台已确认 accepted 明文列表)。
    """

    def __init__(self, live=None, bus=None):
        self._live = live
        self._bus = bus

    def set(self, kind: str = "lifecycle", _extra: dict | None = None, **fields) -> None:
        try:
            if self._live is None:
                return
            snap = self._live.update(**fields)
            if self._bus is not None and self._bus.has_subscribers():
                payload = {**snap, "kind": kind}
                if _extra:
                    payload.update(_extra)
                self._bus.publish(payload)
            if kind in FLUSH_KINDS:
                self._live.flush()
        except Exception:
            # 求解不受影响,但至少留一条 debug(否则仪表板冻结与 idle 无法区分)。
            log.debug("LiveReporter.set failed", exc_info=True)


# ── 事件表(driver 语义原样:kind → LiveState 字段) ────────────

def _ev_tool_start(p: dict) -> dict:
    from ghost_contracts.redact import summarize_args
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
    "tool_progress": lambda p: {"last_output_tail": tail_text(p.get("preview", "") or "", 2048)},
    "text": lambda p: {"assistant_preview": tail_text(p.get("preview", "") or "", ASSISTANT_PREVIEW_MAX)},
    "thinking": lambda p: {"thinking_len": int(p.get("length", 0) or 0)},
    "turn_done": lambda p: {"current_tool": ""},
    "error": lambda p: {"phase": "error", "error": head_text(str(p.get("error", "")), ERROR_HEAD_MAX)},
    "system": _ev_system,
}


def make_live_hooks(reporter: LiveReporter):
    """on_fact(tool_end 接口契约)+ on_event(表驱动分发)→ LiveReporter。

    返回 (on_fact, on_event);会话内已见 flag 计数供 flags_found 实时展示。
    """
    found: list[str] = []

    def on_fact(tool_name: str, args, out: str) -> None:
        for f in extract_flags(out or ""):
            if f not in found:
                found.append(f)
        # update 节流保存 + kind 命中 FLUSH_KINDS 的一次强制 flush,恰好一次落盘
        reporter.set("tool_end",
                     last_tool=tool_name or "",
                     last_output_tail=tail_text(out or "", 2048),
                     current_tool="",
                     flags_found=len(found))

    def on_event(kind: str, payload: dict) -> None:
        p = payload or {}
        fn = _EVENT_TABLE.get(kind)
        if fn is None:
            log.debug("unknown pi event kind dropped: %s", kind)
            return
        reporter.set(kind, **fn(p))

    return on_fact, on_event


# ── 排序与任务构建 ──────────────────────────────────────────

def _difficulty_rank(d: str) -> int:
    return {"easy": 0, "medium": 1, "hard": 2}.get((d or "").lower(), 1)


def _prioritize(challenges: list[Challenge]) -> list[Challenge]:
    """按难度升序、分值降序排列"""
    return sorted(challenges, key=lambda c: (_difficulty_rank(c.difficulty), -int(c.total_score or 0)))


def build_task(ch: Challenge, workdir: str, targets: list, *,
               flag_format: str = "flag{...}") -> AgentTask:
    """从平台 Challenge 构建 AgentTask"""
    return AgentTask(
        objective=ch.description or "Capture the flag(s) from the target.",
        targets=targets or ch.container_addr or [],
        flag_count=ch.flag_count,
        flag_format=flag_format,
        workdir=workdir,
        difficulty=ch.difficulty or None,
        unique_code=ch.unique_code,
        score=ch.total_score,
    )


# ── 靶场生命周期(实现单源 target.py;旧名保留兼容) ─────────────

async def _start_with_retry(client: GhostmarkAsync, code: str):
    """兼容包装:实现见 target.start_target。"""
    return await start_target(client, code)


async def _close_with_retry(client: GhostmarkAsync, code: str) -> bool:
    """兼容包装:实现见 target.close_target。"""
    return await close_target(client, code)


async def _async_sleep(s: float) -> None:
    import asyncio
    await asyncio.sleep(s)


# ── 单题求解 ────────────────────────────────────────────────

async def solve_one(
    client: GhostmarkAsync,
    ch: Challenge,
    *,
    cfg: SolverConfig,
    solver_backend: AgentAdapter,
    reporter: LiveReporter,
    relay=None,
    workdir_root: str = "/work",
    flag_format: str = "flag{...}",
    submitted: dict[str, set[str]] | None = None,
) -> tuple[bool, list[str]]:
    """
    单题单会话求解: start → hint(每题无条件取,平台规则扣分) → 工作目录 →
    单会话 agent(经 to_thread,避免阻塞事件循环) → 候选去重直提 → close。
    返回 (solved, 本轮正确的 flag 列表)。

    solver_backend: AgentAdapter 接口(pi 只是其中一个实现),本函数不依赖 pi。
    靶场生命周期经 target.start_target/close_target,telemetry 经 relay。
    relay:obs 中继(compress 前必须 flush_run 排干未读字节);None 跳过。
    """
    code = ch.unique_code
    submitted = submitted if submitted is not None else {}
    _live_set = reporter.set

    _live_set(phase="starting", challenge_code=code,
              started_at=time.time(), turns=0, error="",
              current_tool="", last_output_tail="",
              assistant_preview="", flags_found=0, accepted=0)
    started = await _start_with_retry(client, code)
    if started is None:
        _live_set(phase="idle", error="start failed")
        return False, []

    accepted: list[str] = []
    try:
        # hint:start 后无条件取。平台规则:查看提示(永久 mark_hint_viewed)后本题
        # 后续 flag 均按比例扣分——用户已确认接受。失败只告警,不阻塞求解。
        hint: str | None = None
        try:
            hint = (await client.get_hint(code)).hint or None
        except Exception as e:
            log.warning("hint unavailable on %s: %s", code, e)
        if hint:
            log.info("hint fetched on %s (%d chars); challenge score discounted per platform rule",
                     code, len(hint))

        workdir = os.path.join(workdir_root, safe_code(code))
        os.makedirs(workdir, exist_ok=True)
        write_context_md(workdir)
        task = build_task(ch, workdir, targets=getattr(started, "container_addr", []) or [],
                          flag_format=flag_format)
        log.info("solving %s (flags=%d, done=%d, diff=%s) targets=%s",
                 code, task.flag_count, ch.correct_flag_count,
                 ch.difficulty or "?", task.target_str())

        # 单会话求解:实时 hooks -> LiveState/SSE + transcript 落盘(pi 本体零改动)
        prompt = build_task_prompt(task, flags_submitted=ch.correct_flag_count, hint=hint)
        transcript_path = os.path.join(workdir, "transcript.jsonl")
        on_fact, on_event = make_live_hooks(reporter)
        _live_set(phase="solving", challenge_code=code,
                  transcript_path=transcript_path,
                  model=getattr(cfg, "model", ""))
        # pi 会话同步阻塞可达 ~1500s:必须放线程,禁止直接 await
        # provider 失败(0-turn+报错,pi 表象 err=none)重开至多 2 次:
        # provider 瞬断/路由抽风不应把整题让掉;真 bug 会连败烧重试预算后暴露
        result = None
        for session_no in range(PROVIDER_FAILURE_RETRIES + 1):
            result = await _solve_in_thread(solver_backend, prompt, workdir, cfg,
                                            on_fact, on_event, transcript_path)
            if not result.provider_failure or session_no >= PROVIDER_FAILURE_RETRIES:
                break
            backoff = 5.0 * (session_no + 1)
            log.warning("provider failure on %s (turns=0, %s) — retrying in %.0fs (%d/%d)",
                        code, (result.error or "")[:120], backoff,
                        session_no + 1, PROVIDER_FAILURE_RETRIES)
            _live_set(phase="solving", error=head_text(result.error))
            await _async_sleep(backoff)

        # session 结束:压缩 transcript,去掉 message_update 流式增量(占 92% 体积)
        # 压缩前先让 obs 中继把本 run 未读字节排干(压缩会重写文件,行内信息零丢失依赖此序)
        if relay is not None:
            relay.flush_run()
        compress_transcript(transcript_path)

        # 候选去重后逐个直接提交;平台 correct/duplicate 响应即唯一闸门。
        # 去重键用原文精确匹配(平台按原文哈希判分,大小写敏感;归一化键会误杀);
        # 仅在收到平台响应后标记(异常未触达平台的不标记,下一轮可重试);
        # 非法候选(含 prompt 占位符 flag{...})直接跳过。
        for cand in result.flags:
            if not is_valid_flag(cand):
                continue
            if cand in submitted.setdefault(code, set()):
                continue
            _live_set(phase="submitting")
            try:
                r = await client.submit_flag(code, cand)
            except DuplicateSubmit:
                # 平台已收该 flag(幂等,本会话外可能已提交过):记入去重集,避免
                # 本轮后续/下轮冷启动重复重提刷屏
                submitted[code].add(cand)
                log.info("duplicate flag on %s (already accepted)", code)
                continue
            except Exception as e:
                log.error("submit failed on %s: %s", code, e)
                continue
            submitted[code].add(cand)
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
            else:
                log.info("flag INCORRECT on %s: %s", code, cand[:40])

        log.info("session done on %s: %d turns, %.0fs, %d candidate(s), %d accepted%s",
                 code, result.turns, result.duration_s, len(result.flags), len(accepted),
                 f", error={head_text(result.error, 120)}" if result.error else "")
        if result.provider_failure:
            # 会话级重试已耗尽仍 0-turn+报错:向上抛,driver 计 streak 熔断。
            # close 仍走 finally(平台实例不泄漏),只是不再进入提交流程。
            raise ProviderFailure(
                f"{code}: {result.turns} turns after {PROVIDER_FAILURE_RETRIES + 1} "
                f"sessions, last error: {head_text(result.error or '', 200)}")
        _live_set(phase="done", accepted=len(accepted),
                  flags_found=len(result.flags),
                  error=head_text(result.error) if result.error else "")
        return False, accepted
    except ProviderFailure:
        raise  # 熔断信号直达 driver,不得被兜底 except 吞掉
    except VpnCheckError:
        raise  # VPN 掉线直达 driver 的 exit 4 通道,不得记成普通 unsolved
    except Exception:
        log.exception("solve_one error on %s", code)
        return False, accepted
    finally:
        # 单会话结束即释放实例(多 flag 剩题由下一轮重新 start 冷启动)。
        # _accepted_flags = 本会话平台确认正确的明文(FLAG 文件含被拒候选,不能作 accepted)
        _live_set(phase="closing", _extra={"_accepted_flags": list(accepted)})
        if not await _close_with_retry(client, code):
            log.warning("challenge %s left running on platform", code)


async def _solve_in_thread(solver_backend, prompt: str, workdir: str, cfg,
                           on_fact, on_event, transcript_path: str):
    """solver.solve 放线程执行(pi 同步阻塞);经 to_thread 避免阻塞事件循环。"""
    import asyncio
    return await asyncio.to_thread(solver_backend.solve, prompt, workdir, cfg,
                                   on_fact=on_fact, on_event=on_event,
                                   transcript_path=transcript_path)
