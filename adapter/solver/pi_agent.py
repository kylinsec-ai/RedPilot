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

from .base import SolveResult, SolverBackend, extract_flags

log = logging.getLogger("adapter.solver.pi")

DEFAULT_PROVIDER = "deepseek"

# 心跳文件：docker healthcheck 据此判断 driver 是否存活
HEARTBEAT_PATH = "/tmp/driver_heartbeat"

# INFRA_BLOCKED 证据接地（B12）：marker 是 agent 自己输出的字面串，写文件、
# 回显 MEMORY.md 旧结论都会带上它——只有同会话出现过真实网络失败签名，才
# 认可这次"内网不可达"判定。
_INFRA_FAILURE_SIGNS = (
    "connection refused", "connection timed out", "operation timed out",
    "failed to connect", "failed to establish a new connection",
    "max retries exceeded", "connectionreseterror", "connection reset by peer",
    "no route to host", "network is unreachable", "destination host unreachable",
    "100% packet loss", "host seems down", "could not resolve host",
    "temporary failure in name resolution", "code=000",
)


def _beat() -> None:
    """更新心跳文件 mtime（失败静默）"""
    try:
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
    except Exception:
        pass


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


def strip_provider(model: str) -> str:
    """去掉模型名里的 provider 前缀（deepseek/xxx → xxx）。"""
    m = (model or "").strip()
    if "/" in m:
        m = m.rsplit("/", 1)[1]
    return m


def _slim_line(raw: str) -> str:
    """瘦身 transcript 单行：message_update 的 delta 事件只保留增量文本。

    pi 的 thinking_delta/text_delta 事件里 partial 和顶层 message 每次都携带
    累计全文，导致 transcript 近乎二次方膨胀（实测单会话可达 1.3GB）。
    transcript 只用于人工排查、从不回读，因此只写增量即可，其余事件原样保留。
    """
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if ev.get("type") != "message_update":
        # tool_execution_update 是流式 partial 中间态（可能数十KB），完整结果在
        # tool_execution_end 事件里；transcript 只用于人工排查，写满 partial 会暴涨
        # （实测单会话 15MB+）。直接跳过，不落盘（前端 transcript 也本就不渲染 update）。
        if ev.get("type") == "tool_execution_update":
            return ""
        return raw
    msg = ev.get("assistantMessageEvent") or {}
    mtype = msg.get("type", "")
    delta = msg.get("delta", "")
    return json.dumps(
        {"type": "message_update", "assistantMessageEvent": {"type": mtype, "delta": delta}},
        ensure_ascii=False,
    )


def _write_pi_models(pi_home: str, *, base_url: str, model: str) -> None:
    """把可用的模型配置写入 pi 的 $HOME/.pi/agent/models.json。

    pi 以题目 workdir 下的 .pi-home 作为 HOME（上下文隔离），若不在这里
    配置 provider，pi 会回退到默认 provider / 官方端点（openrouter、
    api.deepseek.com 等），导致网关 key 401 / 模型不存在 -> 每次会话 0 turns。

    这里按环境生成单 provider（deepseek）配置：baseUrl 使用平台网关
    （ANTHROPIC_BASE_URL），模型 id 与 solver 配置一致（如
    deepseek-v4-flash-0731）。
    """
    model_id = strip_provider(model or "")
    if not model_id:
        return
    base = (base_url or "").rstrip("/")
    if not base:
        return
    cfg = {
        "providers": {
            "deepseek": {
                "baseUrl": base,
                "api": "openai-completions",
                "apiKey": "$DEEPSEEK_API_KEY",
                "models": [
                    {
                        "id": model_id,
                        "name": model_id,
                        "contextWindow": 1000000,
                        "maxTokens": 384000,
                        "input": ["text"],
                        "reasoning": True,
                        "compat": {
                            "requiresReasoningContentOnAssistantMessages": True,
                            "thinkingFormat": "deepseek",
                            "reasoningEffortMap": {
                                "minimal": "high", "low": "high", "medium": "high",
                                "high": "high", "xhigh": "max",
                            },
                        },
                    }
                ],
            }
        }
    }
    try:
        cfg_dir = os.path.join(pi_home, ".pi", "agent")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "models.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        log.warning("failed to write pi models.json under %s", pi_home)


class PiAgentBackend(SolverBackend):
    """Pi Agent CLI 求解器（json print 一次性模式）"""

    name = "pi-agent"

    def __init__(self, *, cmd: str = "pi", model: str = "",
                 skills_dir: str = "", max_turns: int = 60, thinking: str = ""):
        self.cmd = shutil.which(cmd) or cmd
        self.model = normalize_model(model)
        self.skills_dir = skills_dir
        self.max_turns = max_turns
        # 思考模式强度（Web 端「思考模式（Thinking）」→ ADAPTER_PI_THINKING）：
        # "" 不传（用 pi 默认）；否则透传为 pi --thinking <level>
        # (off|minimal|low|medium|high|xhigh|max)。
        self.thinking = (thinking or "").strip().lower()

    def _build_cmd(self, prompt: str, api_key: str = "") -> list[str]:
        cmd = [self.cmd, "--mode", "json", "--print", "--no-session"]
        if api_key:
            cmd += ["--api-key", api_key]
        if self.model:
            cmd += ["--model", self.model]
        if self.skills_dir and os.path.isdir(self.skills_dir):
            cmd += ["--skill", self.skills_dir]
        if self.thinking:
            cmd += ["--thinking", self.thinking]

        # bash 工具安全护栏扩展 (P0: 防边读边写同一文件撑爆磁盘, e.g. awk wl2.txt >> wl2.txt)。
        # 拦截自引用重定向 + 强制 timeout/ulimit。ADAPTER_PI_EXTENSIONS 可冒号分隔多个扩展；
        # 默认用仓库内 adapter/pi_ext/bash_guard.js (bind-mount 到 /app/adapter/pi_ext/)。
        for ext in (os.environ.get("ADAPTER_PI_EXTENSIONS",
                                   "/app/adapter/pi_ext/bash_guard.js").split(":")):
            if ext and os.path.exists(ext):
                cmd += ["-e", ext]

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
        stop_check: Optional[Callable] = None,
    ) -> SolveResult:
        result = SolveResult()
        t0 = time.monotonic()

        # 配置合并: 显式参数 > solver_cfg
        model = self.model or normalize_model(getattr(solver_cfg, "model", ""))
        skills = self.skills_dir or getattr(solver_cfg, "skills_dir", "")
        api_key = getattr(solver_cfg, "api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "")

        backend = PiAgentBackend(cmd=self.cmd, model=model, skills_dir=skills,
                                 max_turns=solver_cfg.max_turns, thinking=self.thinking)
        cmd = backend._build_cmd(prompt, api_key)

        # 上下文隔离：每道题使用独立的 HOME（本题 workdir 下），
        # 隔离 pi 的全局状态/缓存/会话残留，防止跨题上下文污染导致的幻觉。
        # 不同题 workdir 不同 → HOME 天然隔离；同一题多轮共享该 HOME（保留 MEMORY）。
        env = {**os.environ}
        pi_home = os.path.join(workdir, ".pi-home")
        try:
            os.makedirs(pi_home, exist_ok=True)
            env["HOME"] = pi_home
        except OSError:
            env["HOME"] = os.environ.get("HOME", "/root")

        # 运行时写入 pi 模型配置（网关 baseUrl + 实际模型 id），
        # 否则 pi 回退到官方端点/默认模型，导致 key 401 / 0 turns。
        _write_pi_models(pi_home, base_url=getattr(solver_cfg, "base_url", "") or
                         os.environ.get("ANTHROPIC_BASE_URL", ""),
                         model=model or getattr(solver_cfg, "model", ""))

        if transcript_path:
            os.makedirs(os.path.dirname(transcript_path), exist_ok=True)

        tool_outputs = []
        all_output_parts = []
        saw_net_failure = False   # 本会话是否见过真实网络失败签名（B12 接地）
        turns = 0
        pending_calls = {}   # toolCallId -> args（tool_execution_end 不带 args）
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
                    # 注意：必须用 os.read 非阻塞读（readline 会在部分行时永久阻塞，
                    #       导致看门狗失效、会话悬挂）
                    STALL_TIMEOUT = float(os.environ.get("PI_STALL_TIMEOUT", "480"))
                    stall_deadline = time.monotonic() + STALL_TIMEOUT
                    _stop_check_interval = 5  # check stop every 5s
                    _last_stop_check = time.monotonic()
                    import select
                    import os as _os
                    read_fd = proc.stdout.fileno()
                    line_buf = ""
                    session_timed_out = False
                    while True:
                        ready, _, _ = select.select([proc.stdout], [], [], 30)
                        if not ready:
                            # 会话预算：即使子进程静默（如长工具/长思考）也必须到点终止——
                            # 否则只要进程持续吐行（重置 stall）就永不超时，
                            # 单会话可远超 session_seconds，导致轮次时间盒失效。
                            if time.monotonic() > deadline:
                                log.warning("pi session timeout after %ds (quiet)",
                                            int(solver_cfg.session_seconds))
                                proc.terminate()
                                session_timed_out = True
                                break
                            if time.monotonic() > stall_deadline:
                                log.warning("pi session stalled %ds (no output) — killing and retrying",
                                            STALL_TIMEOUT)
                                proc.kill()
                                try:
                                    proc.wait(timeout=10)
                                except subprocess.TimeoutExpired:
                                    pass
                                result.error = "stalled_no_output"
                                break
                            continue
                        try:
                            chunk = _os.read(read_fd, 65536).decode("utf-8", "replace")
                        except OSError:
                            break
                        if not chunk:
                            break  # EOF
                        line_buf += chunk
                        stall_deadline = time.monotonic() + STALL_TIMEOUT
                        if session_timed_out:
                            break
                        while "\n" in line_buf:
                            raw, line_buf = line_buf.split("\n", 1)
                            line = raw.strip()
                            if not line:
                                continue

                            if session_timed_out:
                                break

                            _beat()

                            if transcript_f:
                                slim = _slim_line(line)
                                if slim:
                                    transcript_f.write(slim + "\n")

                            if time.monotonic() > deadline:
                                log.warning("pi session timeout after %ds", solver_cfg.session_seconds)
                                proc.terminate()
                                session_timed_out = True
                                break

                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            event_type = event.get("type", "")

                            # ── 工具调用 ──
                            if event_type == "tool_execution_start":
                                turns += 1
                                pending_calls[event.get("toolCallId", "")] = event.get("args") or {}
                                # BUG-J：straggler start 无配对 end 会让缓存无限增长（长会话数百key）。
                                # pi 工具顺序执行，同时 in-flight 极少；超阈值即逐出最旧。
                                if len(pending_calls) > 2048:
                                    for _ in range(512):
                                        try:
                                            pending_calls.pop(next(iter(pending_calls)))
                                        except StopIteration:
                                            break

                            elif event_type == "tool_execution_end":
                                tool_name = event.get("toolName", "")
                                # tool_execution_end 事件不带 args/command（实测），
                                # 从 start 事件按 toolCallId 取回，否则 command 丢失
                                # → tried_commands 持久化与 on_fact 事实提取全失效。
                                tool_args = (event.get("args")
                                             or pending_calls.pop(event.get("toolCallId", ""), {}))
                                out = _join_content(event.get("result", {}).get("content"))
                                if out:
                                    tool_outputs.append((tool_name, tool_args, out))
                                    all_output_parts.append(out)
                                    if on_fact:
                                        try:
                                            on_fact(tool_name, tool_args, out)
                                        except Exception as e:
                                            log.warning("on_fact callback error: %s", e)
                                    # INFRA_BLOCKED 接地（B12）：marker 由 agent
                                    # 自己输出（写文件/回显 MEMORY.md 旧结论都会
                                    # 带上），单凭它判定"内网不可达"会把 agent
                                    # 自述当证据（实测 c-03 开场 cat MEMORY.md
                                    # 即置位）。要求本会话此前（或本条输出内）
                                    # 出现过真实网络失败签名才认可。
                                    _out_low = out.lower()
                                    if any(s in _out_low for s in _INFRA_FAILURE_SIGNS):
                                        saw_net_failure = True
                                    if saw_net_failure and "INFRA_BLOCKED" in out:
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
                            elif event_type in ("agent_end", "turn_end", "message_end"):
                                if text_buf:
                                    all_output_parts.append(text_buf)
                                    text_buf = ""
                                # BUG: 模型层错误（如 402 Insufficient Balance / 认证失败）出现在
                                # stopReason="error" + errorMessage，不匹配下方 error 事件分支，
                                # 导致 result.error 一直为 None → 框架 API 熔断不触发 → 空转开关靶场。
                                # 这里捕获终态里的 stopReason/errorMessage。
                                _stop = event.get("message", {}) or {}
                                if isinstance(_stop, dict) and _stop.get("stopReason") == "error":
                                    em = _stop.get("errorMessage") or _stop.get("error") or ""
                                    if not result.error and em:
                                        result.error = str(em)
                                        log.warning("pi terminal error: %s", result.error[:200])
                                elif isinstance(_stop, dict) and _stop.get("stopReason") in ("aborted", "error"):
                                    if not result.error:
                                        em = _stop.get("errorMessage") or _stop.get("error") or str(_stop)[:200]
                                        result.error = str(em)
                                        log.warning("pi terminal error(%s): %s", _stop.get("stopReason"), result.error[:200])

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
        if all_output_parts:
            result.final_text = all_output_parts[-1]

        # 从 FLAG 文件读取
        self._read_flag_files(workdir, result.flags)

        log.info("pi session done: %d turns, %.0fs, %d flags, err=%s",
                 turns, result.duration_s, len(result.flags),
                 result.error[:60] if result.error else "none")
        return result