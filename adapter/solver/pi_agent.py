"""
Pi Agent CLI 求解器适配器

以 `pi --mode json --print --no-session` 一次性模式启动 pi 进程，按行解析 JSON 事件流：

- session / agent_start / turn_start / turn_end / agent_end
- message_update: assistantMessageEvent.{text_delta|thinking_delta}（增量文本）
- tool_execution_start / tool_execution_update / tool_execution_end（工具调用）
- error / terminal.failed

要点：
- --print 模式下工具自动执行，无需交互批准
- 模型使用 provider/model 格式（如 deepseek/deepseek-v4-flash）
- API Key 通过 --api-key 直传，不依赖环境变量
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Callable, Optional

from .base import SolveResult, SolverBackend, extract_flags, touch_heartbeat

log = logging.getLogger("adapter.solver.pi")

DEFAULT_PROVIDER = "deepseek"


def normalize_model(model: str) -> str:
    """将模型名规范化为 provider/model 格式（缺 provider 时补默认）"""
    model = (model or "").strip()
    if not model:
        return ""
    if "/" in model:
        return model
    return f"{DEFAULT_PROVIDER}/{model}"


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


class PiAgentBackend(SolverBackend):
    """Pi Agent CLI 求解器（json print 一次性模式）"""

    name = "pi-agent"

    def __init__(self, *, cmd: str = "pi", model: str = "",
                 skills_dir: str = ""):
        self.cmd = shutil.which(cmd) or cmd
        self.model = normalize_model(model)
        self.skills_dir = skills_dir

    def _build_cmd(self, prompt: str, api_key: str = "", *,
                   model: str = "", skills_dir: str = "") -> list[str]:
        """组装命令行；model/skills_dir 参数覆盖实例字段（供 solve 配置合并）"""
        cmd = [self.cmd, "--mode", "json", "--print", "--no-session"]
        if api_key:
            cmd += ["--api-key", api_key]
        model = model or self.model
        if model:
            cmd += ["--model", model]
        skills = skills_dir or self.skills_dir
        if skills and os.path.isdir(skills):
            cmd += ["--skill", skills]
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
    ) -> SolveResult:
        result = SolveResult()
        t0 = time.monotonic()

        # 配置合并: 显式参数 > solver_cfg
        model = self.model or normalize_model(getattr(solver_cfg, "model", ""))
        skills = self.skills_dir or getattr(solver_cfg, "skills_dir", "")
        api_key = getattr(solver_cfg, "api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "")

        cmd = self._build_cmd(prompt, api_key, model=model, skills_dir=skills)

        env = {**os.environ}
        env["HOME"] = os.environ.get("HOME", "/root")

        if transcript_path:
            os.makedirs(os.path.dirname(transcript_path), exist_ok=True)

        tool_outputs = []
        all_output_parts = []
        turns = 0
        text_buf = ""      # 助手文本累积（text_delta 是增量）
        thinking_buf = ""  # 思考流（忽略，不进入 observed_output）

        for attempt in range(max_retries + 1):
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )

                transcript_f = None
                if transcript_path:
                    transcript_f = open(transcript_path, "a", encoding="utf-8")

                deadline = time.monotonic() + solver_cfg.session_seconds

                try:
                    # 看门狗：子进程连续无输出超过 STALL_TIMEOUT 秒视为卡死
                    # → 杀掉子进程并重开会话（外层 for attempt 会重试）
                    STALL_TIMEOUT = float(os.environ.get("PI_STALL_TIMEOUT", "480"))
                    stall_deadline = time.monotonic() + STALL_TIMEOUT
                    import select
                    while True:
                        ready, _, _ = select.select([proc.stdout], [], [], 30)
                        if not ready:
                            if time.monotonic() > stall_deadline:
                                log.warning("pi session stalled %ds (no output) — killing and retrying",
                                            STALL_TIMEOUT)
                                proc.kill()
                                proc.wait(timeout=10)
                                result.error = "stalled_no_output"
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

                        touch_heartbeat()

                        if transcript_f:
                            transcript_f.write(line + "\n")

                        if time.monotonic() > deadline:
                            log.warning("pi session timeout after %ds", solver_cfg.session_seconds)
                            proc.terminate()
                            break

                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type", "")

                        # ── 工具调用 ──
                        if event_type == "tool_execution_start":
                            turns += 1

                        elif event_type == "tool_execution_end":
                            tool_name = event.get("toolName", "")
                            tool_args = event.get("args") or {}
                            out = _join_content(event.get("result", {}).get("content"))
                            if out:
                                tool_outputs.append((tool_name, tool_args, out))
                                all_output_parts.append(out)
                                if on_fact:
                                    try:
                                        on_fact(tool_name, tool_args, out)
                                    except Exception as e:
                                        log.warning("on_fact callback error: %s", e)
                                if "INFRA_BLOCKED" in out:
                                    result.infra_blocked = True
                                for f in extract_flags(out):
                                    if f not in result.flags:
                                        result.flags.append(f)

                        elif event_type == "tool_execution_update":
                            # 部分输出（实时流）
                            partial = _join_content(event.get("partialResult", {}).get("content"))
                            if partial and not all_output_parts:
                                all_output_parts.append(partial)

                        # ── 助手文本（delta 增量，累积） ──
                        elif event_type == "message_update":
                            msg = event.get("assistantMessageEvent") or {}
                            mtype = msg.get("type", "")
                            delta = msg.get("delta", "")
                            if mtype == "text_delta" and delta:
                                text_buf += delta
                            elif mtype == "thinking_delta" and delta:
                                thinking_buf += delta

                        # ── 终态 ──
                        elif event_type in ("agent_end", "turn_end"):
                            if text_buf:
                                all_output_parts.append(text_buf)
                                text_buf = ""

                        elif event_type == "error" or "error" in event_type.lower():
                            result.error = event.get("message") or event.get("error") or str(event)[:200]
                            log.warning("pi error: %s", result.error[:200])

                    # 末尾补上未 flush 的文本
                    if text_buf:
                        all_output_parts.append(text_buf)

                    proc.wait(timeout=30)

                finally:
                    if transcript_f:
                        transcript_f.close()
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()

                # 成功完成，不再重试
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