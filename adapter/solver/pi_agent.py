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

import json
import logging
import os
import select
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional

from .base import SolveResult, SolverBackend, extract_flags, touch_heartbeat
from ..live.state import (
    ASSISTANT_PREVIEW_MAX,
    OUTPUT_TAIL_MAX,
    ensure_dir,
    head_text,
    tail_text,
)

log = logging.getLogger("adapter.solver.pi")


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


def _drain_stderr_to(stderr, buf: deque, lock) -> None:
    """排空子进程 stderr（PIPE 从不读取会阻塞子进程）；只保留尾部"""
    try:
        for err_line in stderr:
            with lock:
                buf.append(err_line.rstrip()[-500:])
    except Exception:
        pass


class PiAgentBackend(SolverBackend):
    """Pi Agent CLI 求解器（json print 一次性模式）"""

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
        flag_format: str = "flag{...}",
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
        thinking_buf = ""  # 思考流（忽略，不进入 observed_output）

        def _emit(kind: str, payload: dict | None = None) -> None:
            """非侵入观测：on_event 只读，异常吞掉不影响求解"""
            if not on_event:
                return
            try:
                on_event(kind, payload or {})
            except Exception as e:
                log.warning("on_event callback error: %s", e)

        # PI_STALL_TIMEOUT 启动时解析一次:空串/垃圾值 fail-fast 回退,不进重试循环
        try:
            STALL_TIMEOUT = float((os.environ.get("PI_STALL_TIMEOUT", "480") or "480").strip() or "480")
            if STALL_TIMEOUT <= 0:
                raise ValueError("non-positive")
        except ValueError:
            log.warning("bad PI_STALL_TIMEOUT=%r, using 480", os.environ.get("PI_STALL_TIMEOUT"))
            STALL_TIMEOUT = 480.0

        for attempt in range(max_retries + 1):
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
                )

                # stderr 排空线程：PIPE 从不读取会阻塞子进程；保留最后 20 行供报错
                # (deque 经锁共享,join 侧不再裸迭代)
                stderr_tail: deque[str] = deque(maxlen=20)
                stderr_lock = threading.Lock()
                threading.Thread(target=_drain_stderr_to,
                                 args=(proc.stderr, stderr_tail, stderr_lock),
                                 daemon=True, name="pi-stderr").start()

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
                    last_progress_emit = 0.0  # tool_execution_update 节流：最多 1/s
                    last_hb = 0.0  # 心跳/落盘节流:行级突发不再 syscall 风暴
                    last_flush = 0.0
                    while True:
                        ready, _, _ = select.select([proc.stdout], [], [], 30)
                        if not ready:
                            # 无输出分支同样执行 session deadline,静默 pi 不再绕过时长上限
                            if time.monotonic() > deadline:
                                log.warning("pi session timeout after %ds (silent)", solver_cfg.session_seconds)
                                _emit("system", {"phase": "timeout",
                                                 "detail": f"session_seconds={solver_cfg.session_seconds}"})
                                proc.kill()
                                try:
                                    proc.wait(timeout=10)
                                except subprocess.TimeoutExpired:
                                    pass
                                result.error = "session_timeout"
                                need_retry = True
                                break
                            if time.monotonic() > stall_deadline:
                                log.warning("pi session stalled %ds (no output) — killing and retrying",
                                            STALL_TIMEOUT)
                                _emit("system", {"phase": "stalled", "detail": f"no output {STALL_TIMEOUT:.0f}s"})
                                proc.kill()
                                proc.wait(timeout=10)
                                result.error = "stalled_no_output"
                                need_retry = True
                                stall_deadline = time.monotonic() + STALL_TIMEOUT
                                break
                            continue
                        line = proc.stdout.readline()
                        if not line:
                            break
                        line = line.strip()
                        stall_deadline = time.monotonic() + STALL_TIMEOUT
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
                                thinking_buf += delta
                                if now - last_progress_emit >= 1.0:
                                    last_progress_emit = now
                                    _emit("thinking", {"length": len(thinking_buf)})

                        # ── 终态 ──
                        elif event_type in ("agent_end", "turn_end"):
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
                    with stderr_lock:
                        stderr_preview = list(stderr_tail)
                    if proc.returncode and stderr_preview:
                        _emit("system", {"phase": "stderr",
                                         "detail": tail_text("\n".join(stderr_preview), 500)})

                finally:
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