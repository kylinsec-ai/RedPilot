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
    DuplicateSubmit,
    GhostmarkAsync,
    VpnCheckError,
)

# ── 策略层：朋友线合并进来的证据守门(现为 ghost_worker.adapter 子包) ──
# 缺包时降级为"平台 submit 即唯一闸门"(合并前的行为),绝不因策略层缺失而让
# 求解挂掉。这条 fail-open 只针对"模块不存在",不针对判定结果。
# ⚠ 曾经在这里多写了一个不存在的 `is_valid_flag`(它在 adapter/solver/base.py,
#   不在 verify.py),ImportError 被下面的 except 吞掉 → 整层静默失效。降级
#   必须是**响亮**的:留一条 warning,否则这类事故与"闸门通过了"无法区分。
try:  # pragma: no cover - 环境相关
    from .adapter.verify import Verifier, flag_confidence, flag_evidence_policy
except Exception as _policy_import_err:  # pragma: no cover
    Verifier = flag_confidence = flag_evidence_policy = None  # type: ignore[assignment]
    logging.getLogger("ghost_worker.orchestration").warning(
        "策略层 adapter.verify 不可用(%s) — 回退为平台 submit 单闸门;证据校验关闭",
        _policy_import_err)

from ghost_contracts.paths import safe_code
from ghost_contracts.text import (
    ASSISTANT_PREVIEW_MAX,
    ERROR_HEAD_MAX,
    OUTPUT_TAIL_MAX,
    head_text,
    tail_text,
)
from ghost_contracts.vocabulary import FLUSH_KINDS

from .config import SolverConfig
from .flags import extract_flags, is_valid_flag
from .solver.base import AgentAdapter
from .target import CLOSE_RETRIES, START_MAX_RETRIES, close_target, start_target
from .task import AgentTask
from .taskprompt import build_task_prompt, write_context_md
from .transcripts import compress_transcript

log = logging.getLogger("ghost_worker.orchestration")

# 靶场生命周期常量/实现单源 target.py。
PROVIDER_FAILURE_RETRIES = 2  # 0-turn+报错(provider 失败)的会话级重开次数

# ── 证据闸门常量 ──────────────────────────────────────────────
# 朋友侧原值:flag_confidence 的 grounded 门 + Verifier.verify。原样搬运,不调参 ——
# 这些数字是他在真靶场上标定出来的(B47 的 0.50 强提门、B44 的裸 body 不算证据)。
GROUNDED_SUBMIT_CONF = 0.50

# 题目分类单源：朋友 driver 的 _infer_category —— 只喂 flag_evidence_policy
# （决定"本地静态产物能否算证据"这道边界），不参与调度分流。**懒加载**：
# import drivers.benchmark_driver 会连带拉进 150+ 个模块、把仓库根塞进 sys.path，
# 而它只是"平台没给 category"时的兜底推断。懒加载保留响亮降级：第一次失败打
# warning —— 与上面的策略层 import 同一条性质，静默失效曾让整层闸门消失而无人察觉。
#
# ⚠ drivers/ 不在 worker 包里（它是朋友自己的竞技场主循环，6,975 行、自带一个与
#   ghost_worker.driver 竞争的 main()，只被根目录测试引用）。所以这条导入在**镜像里
#   永远不会成功** —— 它是静态检查用的 import。真正在容器里跑的时候，category 只能
#   来自平台；平台不给就是 remote-only（安全的那一侧）。
_INFER_CATEGORY = None      # None=未加载; False=加载失败(不再重试)


def _load_category_inferrer():
    """懒加载 driver 的 _infer_category；失败返回 None 并响亮降级一次。"""
    global _INFER_CATEGORY
    if _INFER_CATEGORY is None:
        try:  # pragma: no cover - 环境相关
            from drivers.benchmark_driver import _infer_category
            _INFER_CATEGORY = _infer_category
        except Exception as exc:  # pragma: no cover
            _INFER_CATEGORY = False
            log.warning("题目分类推断不可用(%s) — 证据边界退回 remote-only;"
                        "本地静态产物不再算证据", exc)
    return _INFER_CATEGORY or None


def _evidence_note_for(cand: str, tool_outputs: list, policy, code: str):
    """确定性 grounding 门 —— 返回提交时要附的注记，`None` 表示拒收。

    候选必须逐字出现在本会话真实工具输出里(`flag_confidence` 已排除
    echo/cat 自写与裸 body 自包信封的口径,见 adapter/verify.py 头部)。
    非空注记 = 放行但说明它是"强提"(grounded 且置信度过门)。
    **fail-open**:策略层缺失 / 判定抛异常 → 返回 "" 交平台判,
    宁多交一次也不因监控拖挂求解(与 hallucination.py 同一条安全性质)。
    """
    if flag_confidence is None:
        return ""
    try:
        claim = flag_confidence(cand, "", tool_outputs, evidence_policy=policy)
        if Verifier is not None:
            # llm=None:朋友的 skeptic/followup 两道 LLM 门需要 LLM 客户端,
            # worker 不持该依赖 → 只启用其中最硬的一道确定性 grounding 门。
            # Verifier 无状态(verify 不读任何实例字段),故按需构造即可。
            claim = Verifier(llm=None).verify(claim)
    except Exception:
        log.debug("flag gate unavailable on %s — falling back to platform-only",
                  code, exc_info=True)
        return ""
    if claim.verified:
        return ""
    if claim.grounded and claim.confidence >= GROUNDED_SUBMIT_CONF:
        return f" [force-submit grounded conf={claim.confidence:.2f}]"
    log.info("flag gate REJECT on %s (%s, conf=%.2f): %s",
             code, claim.reject_reason or "?", claim.confidence, cand[:40])
    return None


def _evidence_policy_for(task: AgentTask):
    """按题目元数据构造证据边界(Friend 的策略,喂给下面的闸门)。

    category 走朋友 driver 的 _infer_category —— 平台的 `Challenge` 不带
    category 字段(SDK 0.1.2 只有 10 个字段,实测),所以从描述推断。
    该函数按 `ch.category` / `ch.description` 取值,而 AgentTask 只有
    objective/category,故用一个最小只读探针喂进去(不复制它的关键词表,
    否则就是第三次"两处口径漂移")。
    """
    if flag_evidence_policy is None:
        return None
    try:
        category = str(getattr(task, "category", "") or "").strip()
        if not category:
            infer = _load_category_inferrer()
            if infer is not None:
                # _infer_category 只读 .category / .description 两个属性,
                # 探针只需提供这两个(见 _CategoryProbe)。
                category = infer(_CategoryProbe(task)) or ""
        return flag_evidence_policy(
            category,
            targets=getattr(task, "targets", ()) or (),
            files=getattr(task, "files", ()) or (),
            workdir=getattr(task, "workdir", "") or "",
        )
    except Exception:
        log.debug("evidence policy unavailable — falling back to remote-only", exc_info=True)
        return None


class _CategoryProbe:
    """把 AgentTask 伪装成 driver._infer_category 认的 Challenge（只读两个字段）。

    刻意不 import driver 的 Challenge 类：那是平台模型，不是任务模型；
    `_infer_category` 只读 `.category` 与 `.description`（见其实现），其余
    属性一概不碰，所以两字段的只读探针足够 —— __slots__ 把这层契约钉死。
    """

    __slots__ = ("category", "description")

    def __init__(self, task: AgentTask) -> None:
        self.category = str(getattr(task, "category", "") or "")
        self.description = str(getattr(task, "objective", "") or "")


class ProviderFailure(Exception):
    """provider 失败(0-turn 会话+报错)耗尽重试 — driver 据此计数熔断(exit 3)。

    2026-09-08 事故:pi 对 provider 400 只发 stopReason=error 的收尾消息,
    编排层误作"正常完成"静默烧题库(280 run/0 flag/63 题)。本异常把
    "LLM 上游坏了"从普通未解出中区分出来,由 driver 决定熔断。
    """


__all__ = ["solve_one", "build_task", "_prioritize", "LiveReporter", "make_live_hooks",
           "START_MAX_RETRIES", "CLOSE_RETRIES", "PROVIDER_FAILURE_RETRIES", "ProviderFailure",
           "GROUNDED_SUBMIT_CONF"]


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
    "tool_progress": lambda p: {"last_output_tail": tail_text(p.get("preview", "") or "", OUTPUT_TAIL_MAX)},
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
                     last_output_tail=tail_text(out or "", OUTPUT_TAIL_MAX),
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
    """从平台 Challenge 构建 AgentTask

    category/files 走 getattr:SDK 的 Challenge 目前只有 10 个字段(无这两个),
    平台补齐时自动生效 —— 口径与朋友 driver 的 build_task 一致(它同样用
    `ch.category or _infer_category(ch)` 与 `getattr(ch, "files", [])`)。
    这两个字段是证据边界(flag_evidence_policy)的输入,漏传会让"平台声明的
    附件"永远等于空、category 只能靠描述推断。
    """
    return AgentTask(
        objective=ch.description or "Capture the flag(s) from the target.",
        targets=targets or ch.container_addr or [],
        flag_count=ch.flag_count,
        flag_format=flag_format,
        workdir=workdir,
        difficulty=ch.difficulty or None,
        unique_code=ch.unique_code,
        score=ch.total_score,
        category=getattr(ch, "category", "") or None,
        files=list(getattr(ch, "files", []) or []),
    )


# ── 靶场生命周期(实现单源 target.py) ───────────────────────

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
    instance_token: str = "",
) -> tuple[bool, list[str]]:
    """
    单题单会话求解: start → hint(每题无条件取,平台规则扣分) → 工作目录 →
    单会话 agent(经 to_thread,避免阻塞事件循环) → 候选去重直提 → close。
    返回 (solved, 本轮正确的 flag 列表)。

    solver_backend: AgentAdapter 接口(pi 只是其中一个实现),本函数不依赖 pi。
    靶场生命周期经 target.start_target/close_target,telemetry 经 relay。
    relay:obs 中继(compress 前必须 flush_run 排干未读字节);None 跳过。
    instance_token:逐次访问的进程回收令牌。**只能由 driver 在同一事件循环、
    同一协作点分配** —— 乱序或嵌套的 solve_one 若各自 secrets.token_hex()，
    后写的那次会覆盖先写的，先跑的会话收尾时就找不回自己的标记了。
    空值 = 不盖标记(回收退化为空操作，安全但不回收)。
    """
    code = ch.unique_code
    submitted = submitted if submitted is not None else {}
    _live_set = reporter.set

    _live_set(phase="starting", challenge_code=code,
              started_at=time.time(), turns=0, error="",
              current_tool="", last_output_tail="",
              assistant_preview="", flags_found=0, accepted=0)
    started = await start_target(client, code)
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
        # 逐题实例令牌:引擎据此把 pi 及其全部子孙打上标记,收尾时按标记回收
        # 脱组的 nohup/setsid 子孙(驱动崩溃后也有效)。令牌由 driver 分配
        # (见 instance_token 参数说明),这里只负责落盘。
        if instance_token:
            try:
                stamp = getattr(solver_backend, "stamp_instance", None)
                if callable(stamp):
                    stamp(workdir, instance_token)
            except Exception:
                log.debug("instance token stamp failed on %s (process reaping degraded)",
                          code, exc_info=True)
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
        #
        # ── 证据闸门(朋友线合并) ──────────────────────────────────
        # 在"平台提交"之前多一道确定性 grounding 门(实现见 _evidence_note_for)。
        # 原委见 adapter/verify.py 头部:agent 自造的 flag 会进 FLAG 文件,
        # 只靠平台判分会白耗提交机会并污染账簿。
        # evidence_policy 决定**本地静态产物能否算证据**:有平台声明的附件、
        # 或无目标的本地原生题(reverse/crypto/forensics/pwn/misc)才开;
        # Web/内网/云一律 remote-only。没这层,本地文件里抠出来的字符串
        # 会被当成取证证据放行(朋友的 test_local_evidence_policy 锁的就是它)。
        _evidence_policy = _evidence_policy_for(task)
        tool_outputs = list(result.tool_outputs or [])
        if _evidence_policy is not None:
            log.info("flag evidence policy on %s: %s (category=%s)",
                     code, _evidence_policy.mode, _evidence_policy.category)
        elif flag_evidence_policy is None:
            log.info("flag evidence gate OFF on %s — 只有平台 submit 一道闸门", code)
        for cand in result.flags:
            if not is_valid_flag(cand):
                continue
            if cand in submitted.setdefault(code, set()):
                continue
            evidence_note = _evidence_note_for(cand, tool_outputs, _evidence_policy, code)
            if evidence_note is None:
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
                log.info("FLAG CORRECT on %s: %s (+%d pts, cumulative %d)%s",
                         code, cand[:40], r.awarded, r.cumulative_score, evidence_note)
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
        # 收尾先回收本题脱组的 Pi/工具子孙 —— 下一题启动前必须清干净,否则上一题
        # 的 nohup 监听器/轮询器会活进下一题(朋友侧在同一位置做这件事,见其
        # benchmark_driver 的 finally)。按本次访问的随机令牌回收,构造上不会误伤
        # driver 自身、VPN provider 或另一个 worker 的进程。
        # 放在 close 之前:让未解出的访问及时腾出本地资源,与平台侧释放同步。
        try:
            from .adapter.solver.pi_agent import cleanup_instance_processes
            reaped = await asyncio.to_thread(cleanup_instance_processes, workdir)
            if reaped:
                log.info("reaped %d detached Pi/tool process(es) for %s", reaped, code)
        except Exception:
            log.warning("detached process cleanup failed for %s", code, exc_info=True)
        # 单会话结束即释放实例(多 flag 剩题由下一轮重新 start 冷启动)。
        # _accepted_flags = 本会话平台确认正确的明文(FLAG 文件含被拒候选,不能作 accepted)
        _live_set(phase="closing", _extra={"_accepted_flags": list(accepted)})
        if not await close_target(client, code):
            log.warning("challenge %s left running on platform", code)


async def _solve_in_thread(solver_backend, prompt: str, workdir: str, cfg,
                           on_fact, on_event, transcript_path: str):
    """solver.solve 放线程执行(pi 同步阻塞);经 to_thread 避免阻塞事件循环。"""
    import asyncio
    return await asyncio.to_thread(solver_backend.solve, prompt, workdir, cfg,
                                   on_fact=on_fact, on_event=on_event,
                                   transcript_path=transcript_path)
