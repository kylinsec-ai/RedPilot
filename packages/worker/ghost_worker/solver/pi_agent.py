"""
Pi Agent CLI 求解器适配器

以 `pi --mode json --print --no-session` 一次性模式启动 pi 进程，按行解析 JSON 事件流：

- session / agent_start / turn_start / turn_end / agent_end
- message_update: assistantMessageEvent.{text_delta|thinking_delta}（增量文本）
- tool_execution_start / tool_execution_update / tool_execution_end（工具调用）
- error / terminal.failed

要点：
- --print 模式下工具自动执行，无需交互批准
- 模型使用 provider/model 完整格式（如 deepseek/deepseek-v4-flash），逐字透传
- API 凭据由 pi 按官方 env 名（或 ~/.pi/agent/auth.json，优先于 env）自行解析，
  仓库代码不读 key、不经 --api-key 传递（子进程 env 继承父进程 os.environ）
"""

from __future__ import annotations

import codecs
import contextlib
import json
import logging
import math
import os
import select
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional

from ghost_contracts.fsio import ensure_dir
from ghost_contracts.text import ASSISTANT_PREVIEW_MAX, head_text, tail_text
from ghost_contracts.text import OUTPUT_TAIL_MAX
from .base import SolveResult, SolverBackend, touch_heartbeat
from ..flags import extract_flags

log = logging.getLogger("ghost_worker.solver.pi")


def _clean_str(s) -> str:
    """None/空白安全规范化((s or '').strip() 的统一拼写)"""
    return (s or "").strip()


def _join_content(content) -> str:
    """从 content block 数组提取纯文本"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            t = block.get("type", "")
            if t in ("text", "output_text"):
                parts.append(str(block.get("text", "")))
            elif t == "tool_result":
                inner = block.get("content")
                if isinstance(inner, str):
                    parts.append(inner)
                elif isinstance(inner, list):
                    parts.append(_join_content(inner))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


# ── 在飞求解进程登记表 ───────────────────────────────────
# 为什么需要:driver 用 asyncio.to_thread 跑 solve,而**取消线程不会终止它启动的
# 子进程** —— lease 丢失后 solve_task.cancel() 只让等待方放手,pi 仍在跑,继续烧
# LLM 时长、继续写同一个 workdir;而 job 已回 pending 可能被再次领取(甚至被本
# worker 自己),于是同一 workdir 出现两个并发会话互相踩。
# 登记表让取消方能真正杀掉它。(key = workdir,与"一次一个会话"的假设一致)
_LIVE_SOLVERS: dict[str, subprocess.Popen] = {}
_LIVE_LOCK = threading.Lock()


def kill_solver_processes(workdir: str, *, grace: float = 10.0) -> bool:
    """杀掉该 workdir 上在飞的 pi 进程组(SIGTERM → grace 秒 → SIGKILL)。

    返回是否确实杀掉了进程。幂等:进程已退出、未登记、已清理都安全返回 False。
    """
    with _LIVE_LOCK:
        proc = _LIVE_SOLVERS.pop(workdir, None)
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(Exception):
            proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
    return True


def _unregister_solver(workdir: str, proc: subprocess.Popen) -> None:
    """仅移除自己那一条 —— 避免新会话登记后又被旧会话的收尾误删。"""
    with _LIVE_LOCK:
        if _LIVE_SOLVERS.get(workdir) is proc:
            _LIVE_SOLVERS.pop(workdir, None)


def _drain_stderr_to(stderr, buf: deque, lock) -> None:
    """排空子进程 stderr（PIPE 从不读取会阻塞子进程）；只保留尾部"""
    try:
        for err_line in stderr:
            with lock:
                buf.append(err_line.rstrip()[-500:])
    except Exception:
        pass


class PiAgentBackend(SolverBackend):
    """Pi Agent CLI 求解器（json print 一次性模式）。

    AgentAdapter 的一个实现(pi 只是可替换 adapter 之一);编排层只见
    AgentAdapter 接口,不依赖本类。"""

    name = "pi-agent"

    def __init__(self, *, cmd: str = "pi", model: str = "",
                 skills_dir: str = ""):
        self.cmd = shutil.which(cmd) or cmd
        self.model = _clean_str(model)
        self.skills_dir = _clean_str(skills_dir)

    def _build_cmd(self, prompt: str, *,
                   model: str = "", skills_dir: str = "") -> list[str]:
        """组装命令行；model/skills_dir 参数覆盖实例字段（供 solve 配置合并）"""
        cmd = [self.cmd, "--mode", "json", "--print", "--no-session"]
        model = model or self.model
        if model:
            cmd += ["--model", model]
        skills = skills_dir or self.skills_dir
        if skills and os.path.isdir(skills):
            cmd += ["--skill", skills]
        elif skills:
            log.warning("skills_dir %r not a directory — solving without --skill", skills)
        cmd.append(prompt)
        return cmd

    def solve(
        self,
        prompt: str,
        workdir: str,
        solver_cfg,
        *,
        on_fact: Optional[Callable] = None,
        transcript_path: Optional[str] = None,
        max_retries: int = 2,
        on_event: Optional[Callable] = None,
    ) -> SolveResult:
        result = SolveResult()
        t0 = time.monotonic()

        # 配置合并: 显式参数 > solver_cfg
        model = _clean_str(self.model) or _clean_str(getattr(solver_cfg, "model", ""))
        skills = _clean_str(self.skills_dir) or _clean_str(getattr(solver_cfg, "skills_dir", ""))

        cmd = self._build_cmd(prompt, model=model, skills_dir=skills)

        # env 继承父进程: key 由 pi 子进程按官方 env 名(或 auth.json)自行解析;
        # 仅 HOME 缺失时补默认(免去每次全量拷贝 environ)
        env = None if "HOME" in os.environ else {**os.environ, "HOME": "/root"}

        tool_outputs = []
        all_output_parts = []
        turns = 0
        text_buf = ""      # 助手文本累积（text_delta 是增量）
        thinking_len = 0   # 思考流长度（只用长度，累积原文会无界增长）

        def _emit(kind: str, payload: dict | None = None) -> None:
            """非侵入观测：on_event 只读，异常吞掉不影响求解"""
            if not on_event:
                return
            try:
                on_event(kind, payload or {})
            except Exception as e:
                log.warning("on_event callback error: %s", e)

        # PI_STALL_TIMEOUT 启动时解析一次:空串/垃圾值/inf/nan fail-fast 回退,不进重试循环
        try:
            STALL_TIMEOUT = float((os.environ.get("PI_STALL_TIMEOUT", "480") or "480").strip() or "480")
            if not math.isfinite(STALL_TIMEOUT) or STALL_TIMEOUT <= 0:
                raise ValueError("non-positive-or-infinite")
        except ValueError:
            log.warning("bad PI_STALL_TIMEOUT=%r, using 480", os.environ.get("PI_STALL_TIMEOUT"))
            STALL_TIMEOUT = 480.0

        for attempt in range(max_retries + 1):
            # 每次尝试从干净错误态开始:上一轮 stall/timeout/异常不得污染成功轮
            # (否则 driver 的 closing 帧会把重试后部分成功的 run 关成 failed)
            result.error = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",  # stderr 非 UTF-8 字节不再杀死排空线程(否则 PIPE 满则子进程阻塞)
                    bufsize=1,
                    # 自成进程组:取消时要整组杀 —— pi 可能带起子进程(curl/nmap 等),
                    # 只杀父进程会留下孤儿继续占用靶场与网络。
                    start_new_session=True,
                )
                _LIVE_SOLVERS[workdir] = proc

                # stderr 排空线程：PIPE 从不读取会阻塞子进程；保留最后 20 行供报错
                # (deque 经锁共享,join 侧不再裸迭代)
                stderr_tail: deque[str] = deque(maxlen=20)
                stderr_lock = threading.Lock()
                stderr_thread = threading.Thread(target=_drain_stderr_to,
                                                 args=(proc.stderr, stderr_tail, stderr_lock),
                                                 daemon=True, name="pi-stderr")
                stderr_thread.start()

                transcript_f = None
                if transcript_path:
                    ensure_dir(transcript_path)
                    try:
                        # 跨轮冷启动复用同一路径:超 5MB 则截断,防 transcript 无限增长
                        if attempt == 0 and os.path.isfile(transcript_path) \
                                and os.path.getsize(transcript_path) > 5 * 1024 * 1024:
                            log.warning("transcript %s oversized, truncating", transcript_path)
                            open(transcript_path, "w", encoding="utf-8").close()
                        transcript_f = open(transcript_path, "a", encoding="utf-8")
                    except OSError as e:
                        log.warning("transcript open failed %s: %s", transcript_path, e)
                        transcript_f = None
                if transcript_f:
                    # 带 type 的结构化哨兵行:按 pi 事件重放的解析器不再 KeyError
                    try:
                        transcript_f.write(json.dumps({"type": "_attempt", "attempt": attempt}) + "\n")
                        transcript_f.flush()
                    except OSError as e:
                        log.warning("transcript write failed %s: %s", transcript_path, e)
                        transcript_f.close()
                        transcript_f = None

                deadline = time.monotonic() + solver_cfg.session_seconds

                try:
                    # 看门狗：子进程连续无输出超过 STALL_TIMEOUT 秒视为卡死
                    # → 杀掉子进程并重开会话（置 need_retry,外层 for attempt 重试）
                    stall_deadline = time.monotonic() + STALL_TIMEOUT
                    need_retry = False
                    stopped_by_us = False  # 本轮是否我们主动 terminate/kill(区分外部 SIGKILL/SIGTERM)
                    last_progress_emit = 0.0  # tool_execution_update 节流：最多 1/s
                    last_hb = 0.0  # 心跳/落盘节流:行级突发不再 syscall 风暴
                    last_flush = 0.0
                    # 增量行读取器:stdout 切非阻塞,攒整行再处理。readline() 在
                    # 半行(无换行)输出上会永久阻塞,绕过 stall 看门狗与 deadline,
                    # 故用 os.read 攒 buffer;只有完整行(或 EOF 残余)才进入处理。
                    out_buf = ""
                    out_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    stdout_fd = proc.stdout.fileno()
                    os.set_blocking(stdout_fd, False)
                    eof_seen = False
                    while True:
                        ready, _, _ = select.select([proc.stdout], [], [], 30)
                        if not ready:
                            # 无输出分支同样执行 session deadline,静默 pi 不再绕过时长上限。
                            # 与有输出分支同语义:预算耗尽即收尾(SIGTERM,不 kill+重试)——
                            # 静默不足 30s 也可能只是模型在憋大招;真卡死由 stall 看门狗击毙
                            if time.monotonic() > deadline:
                                log.warning("pi session timeout after %ds (silent)",
                                            solver_cfg.session_seconds)
                                _emit("system", {"phase": "timeout",
                                                 "detail": f"session_seconds={solver_cfg.session_seconds}"})
                                proc.terminate()
                                stopped_by_us = True
                                # 零输出的预算耗尽同样记错并重试:否则空 transcript
                                # 以 err=None 收尾,调用方误作正常完成(重试拿新鲜 deadline)
                                result.error = "session_timeout"
                                need_retry = True
                                break
                            if time.monotonic() > stall_deadline:
                                log.warning("pi session stalled %ds (no output) — killing and retrying",
                                            STALL_TIMEOUT)
                                _emit("system", {"phase": "stalled", "detail": f"no output {STALL_TIMEOUT:.0f}s"})
                                proc.kill()
                                stopped_by_us = True
                                proc.wait(timeout=10)
                                result.error = "stalled_no_output"
                                need_retry = True
                                stall_deadline = time.monotonic() + STALL_TIMEOUT
                                break
                            continue
                        # 有数据分支同样执行 stall 检查:纯空行滴答让 select 持续
                        # 可读、从不进入上面的无输出分支,不在这里查就永远查不到。
                        # 顺序:先读后判 —— 结束静默的内容必须先取走,看门狗不得
                        # 在读取前 kill(否则刚过 stall 阈值到达的答案连读的机会都没有)。
                        try:
                            chunk = os.read(stdout_fd, 65536)
                        except BlockingIOError:
                            chunk = None
                        if chunk:
                            out_buf += out_decoder.decode(chunk)
                            # 只有含实质内容的字节才喂狗:纯换行/空白输出不重置,
                            # 否则卡死的 pi 靠空行即可绕过 stalled_no_output
                            if chunk.strip():
                                stall_deadline = time.monotonic() + STALL_TIMEOUT
                        elif chunk is not None and not eof_seen:
                            eof_seen = True
                            out_buf += out_decoder.decode(b"", final=True)
                        # 本次没读到实质内容(空行滴答/EAGAIN)且看门狗超时 → 击毙。
                        # EOF(b"") 除外:进程自己退出不算 stall
                        if (chunk is None or (chunk and not chunk.strip())) \
                                and time.monotonic() > stall_deadline:
                            log.warning("pi session stalled %ds (no output) — killing and retrying",
                                        STALL_TIMEOUT)
                            _emit("system", {"phase": "stalled", "detail": f"no output {STALL_TIMEOUT:.0f}s"})
                            proc.kill()
                            stopped_by_us = True
                            proc.wait(timeout=10)
                            result.error = "stalled_no_output"
                            need_retry = True
                            stall_deadline = time.monotonic() + STALL_TIMEOUT
                            break
                        if "\n" in out_buf:
                            line, out_buf = out_buf.split("\n", 1)
                        elif eof_seen:
                            # EOF:残余半行作为最后一行;耗尽后退出循环
                            if not out_buf.strip():
                                break
                            line, out_buf = out_buf, ""
                        else:
                            continue
                        line = line.strip()
                        if not line:
                            continue

                        now_line = time.monotonic()
                        if now_line - last_hb >= 10.0:
                            last_hb = now_line
                            touch_heartbeat()

                        if transcript_f:
                            transcript_f.write(line + "\n")
                            if now_line - last_flush >= 1.0:  # 逐行写缓冲、按秒落盘
                                last_flush = now_line
                                transcript_f.flush()  # detail 页可近实时 tail

                        if time.monotonic() > deadline:
                            log.warning("pi session timeout after %ds", solver_cfg.session_seconds)
                            _emit("system", {"phase": "timeout",
                                             "detail": f"session_seconds={solver_cfg.session_seconds}"})
                            proc.terminate()
                            stopped_by_us = True
                            break

                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type", "")
                        if not isinstance(event_type, str):
                            event_type = ""  # "type": null/数字等畸形行视作未知事件

                        # ── 工具调用 ──
                        if event_type == "tool_execution_start":
                            turns += 1
                            _emit("tool_start", {"tool": event.get("toolName", ""),
                                                 "args": event.get("args") or {}})

                        elif event_type == "tool_execution_end":
                            tool_name = event.get("toolName", "")
                            tool_args = event.get("args") or {}
                            out = _join_content(event.get("result", {}).get("content"))
                            # 空输出也算一次 tool 结束:on_fact/INFRA 标记不受 out 非空 gating,
                            # 否则 driver 的 current_tool 永远不清(只 all_output_parts 要求非空,
                            # 以免空串污染 partial 种子检查 `not all_output_parts`)
                            tool_outputs.append((tool_name, tool_args, out))
                            if out:
                                all_output_parts.append(out)
                            if on_fact:
                                try:
                                    on_fact(tool_name, tool_args, out)
                                except Exception as e:
                                    log.warning("on_fact callback error: %s", e)
                            if "INFRA_BLOCKED" in out:
                                result.infra_blocked = True
                            if out:
                                for f in extract_flags(out):
                                    if f not in result.flags:
                                        result.flags.append(f)

                        elif event_type == "tool_execution_update":
                            # 部分输出（实时流，节流转发防 SSE 洪水）
                            partial = _join_content(event.get("partialResult", {}).get("content"))
                            if partial and not all_output_parts:
                                all_output_parts.append(partial)
                            now = time.monotonic()
                            if partial and now - last_progress_emit >= 1.0:
                                last_progress_emit = now
                                _emit("tool_progress",
                                      {"preview": tail_text(partial, OUTPUT_TAIL_MAX)})

                        # ── 助手文本（delta 增量，累积；与 progress 共用 1/s 节流）──
                        elif event_type == "message_update":
                            msg = event.get("assistantMessageEvent") or {}
                            mtype = msg.get("type", "")
                            delta = msg.get("delta", "")
                            now = time.monotonic()
                            if mtype == "text_delta" and delta:
                                text_buf += delta
                                if now - last_progress_emit >= 1.0:
                                    last_progress_emit = now
                                    _emit("text", {"preview": tail_text(text_buf, ASSISTANT_PREVIEW_MAX)})
                            elif mtype == "thinking_delta" and delta:
                                thinking_len += len(delta)
                                if now - last_progress_emit >= 1.0:
                                    last_progress_emit = now
                                    _emit("thinking", {"length": thinking_len})

                        # ── 终态 ──
                        elif event_type in ("agent_end", "turn_end"):
                            # provider 失败上游只落在收尾消息的 stopReason=error +
                            # errorMessage(pi 不发顶层 error 事件,漏读则 0-turn
                            # 会话以 err=none 表象完成 → 编排层静默烧题库)。
                            end_msgs = event.get("messages") or []
                            last_msg = end_msgs[-1] if isinstance(end_msgs, list) and end_msgs else {}
                            if isinstance(last_msg, dict) \
                                    and last_msg.get("stopReason") == "error" \
                                    and last_msg.get("errorMessage"):
                                if not result.error:
                                    result.error = str(last_msg["errorMessage"])
                                log.warning("pi stopReason=error: %s", result.error[:200])
                                _emit("error", {"error": head_text(result.error)})
                            if text_buf:
                                all_output_parts.append(text_buf)
                                _emit("text", {"preview": tail_text(text_buf, ASSISTANT_PREVIEW_MAX)})
                                text_buf = ""
                            _emit("turn_done", {})

                        elif event_type == "error" or "error" in event_type.lower():
                            result.error = event.get("message") or event.get("error") or str(event)[:200]
                            log.warning("pi error: %s", result.error[:200])
                            _emit("error", {"error": head_text(result.error)})

                    # 末尾补上未 flush 的文本
                    if text_buf:
                        all_output_parts.append(text_buf)

                    proc.wait(timeout=30)
                    # 排空线程是 daemon:进程退出 ≠ 缓冲已读完。稍等一下,否则
                    # "非零退出"的诊断信息会退化成光秃秃的退出码(实测过)。
                    stderr_thread.join(timeout=1.0)
                    with stderr_lock:
                        stderr_preview = list(stderr_tail)
                    # 我们自己发的 SIGTERM(-15)/SIGKILL(-9)(deadline/stall)不算异常退出:
                    # 已分别发过 timeout/stalled 事件,再发 stderr 错误相位会把正常收尾
                    # 误标成 error(rc=-15 且 pi 曾有常规 stderr 输出时必触发)。
                    # 外部击毙(OOM 等,非我们发的)不在此列:照常发 stderr 保留 crash 线索。
                    if proc.returncode and (proc.returncode not in (-9, -15)
                                            or not stopped_by_us):
                        if stderr_preview:
                            _emit("system", {"phase": "stderr",
                                             "detail": tail_text("\n".join(stderr_preview), 500)})
                        # 非零退出必须留下 error,否则 0-turn 护栏失效:
                        # SolveResult.provider_failure = (turns==0 and error!=""),
                        # 而 pi 因坏模型名/缺凭据/参数错误而"只往 stderr 输出后非零退出"
                        # 时,error 仍为空 → 该题被静默记成普通未解,provider 故障被吞。
                        # 这与 2026-09-08 那次 0-turn 静默烧题属同一类。
                        if not result.error:
                            detail = tail_text("\n".join(stderr_preview), 500) if stderr_preview \
                                else f"pi exited with code {proc.returncode}"
                            result.error = detail or f"pi exited with code {proc.returncode}"

                finally:
                    _unregister_solver(workdir, proc)
                    if transcript_f:
                        transcript_f.close()
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()

                if need_retry and attempt < max_retries:
                    time.sleep(3)
                    continue  # stall/静默超时:重开会话再试一次
                # 成功完成(或重试耗尽),不再重试
                break

            except Exception as e:
                log.error("pi session attempt %d failed: %s", attempt + 1, e)
                result.error = str(e)
                if attempt < max_retries:
                    time.sleep(3)
                    continue

        result.tool_outputs = tool_outputs
        result.observed_output = "\n".join(all_output_parts[-50:])
        result.turns = turns
        result.duration_s = time.monotonic() - t0

        # 从 FLAG 文件读取
        self._read_flag_files(workdir, result.flags)

        log.info("pi session done: %d turns, %.0fs, %d flags, err=%s",
                 turns, result.duration_s, len(result.flags),
                 result.error[:60] if result.error else "none")
        return result