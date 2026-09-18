"""
Pi Agent CLI 求解器适配器

以 `pi --mode json --print --no-session` 一次性模式启动 pi 进程，按行解析 JSON 事件流：

- session / agent_start / turn_start / turn_end / agent_end
- message_update: assistantMessageEvent.{text_delta|thinking_delta}（增量文本）
- tool_execution_start / tool_execution_update / tool_execution_end（工具调用）
- error / terminal.failed

要点：
- --print 模式下工具自动执行，无需交互批准
- 模型使用 provider/model 格式（如 deepseek/mimo-v2.5）
- API Key 通过 --api-key 直传，不依赖环境变量
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import shutil
import subprocess
import time
from typing import Callable, Optional

from .base import SolveResult, SolverBackend, extract_flags, extract_handoff
from .pi_transport import make_transport

from redpilot.contracts.paths import HEARTBEAT_PATH  # 心跳路径单源(contracts)
from .. import isolation  # 求解身份降权（设计 §4）
from ..config import IsolationConfig

log = logging.getLogger("adapter.solver.pi")

DEFAULT_PROVIDER = "deepseek"

# 心跳文件：docker healthcheck 据此判断 driver 是否存活。
# 路径单源在 contracts.paths（框架 driver 与 compose healthcheck 都对齐它）；
# 这里保留同名模块常量只为兼容既有引用（tests 直接 import 它），值从单源取。

# The driver writes a fresh random trace scope into each active workdir's
# _instance.json.  Pi and every tool it launches inherit this tag, allowing us
# to reap only the processes belonging to a finished challenge visit.  This is
# needed for commands such as `nohup ... &` / `setsid ...`: they can outlive
# Pi's normal process group and otherwise leak into the next challenge.
_INSTANCE_TOKEN_ENV = "REDPILOT_PI_INSTANCE_TOKEN"
_INSTANCE_TOKEN_RX = re.compile(r"^[0-9a-f]{32}$")

# INFRA_BLOCKED 证据接地（B12）：marker 是 agent 自己输出的字面串，写文件、
# 回显 MEMORY.md 旧结论都会带上它——只有同会话出现过真实网络失败签名，才
# 认可这次"内网不可达"判定。
_INFRA_FAILURE_SIGNS = (
    "connection refused", "connection timed out", "operation timed out",
    "failed to connect", "failed to establish a new connection",
    "max retries exceeded", "connectionreseterror", "connection reset by peer",
    "no route to host", "network is unreachable", "destination host unreachable",
    "100% packet loss", "host seems down", "could not resolve host",
    "temporary failure in name resolution",
)

# 目标服务故障签名（B16）：端口通、后端持续 5xx —— 与 B12 的"网络不可达"区分。
# 这类故障重启题目容器通常可解（分析器进程崩了/实例脏了），不该记成 solver 的零进展。
_TARGET_FAULT_SIGNS = (
    "internal server error", "bad gateway", "service unavailable",
    "gateway timeout", "http 500", "http 502", "http 503", "http 504",
)
# 整条工具输出就是一个 5xx 状态码（curl -w '%{http_code}' 探针）——精确匹配，
# 不做裸数字子串匹配（"500" 在正文里到处都是，会淹没信号）。
_FAULT_BARE_STATUS = frozenset({
    "500", "502", "503", "504", "http 500", "http 502", "http 503", "http 504",
})


def target_fault_verdict(fault_hits: int, marker: bool, *,
                         min_hits: int = 3, hard_hits: int = 60) -> bool:
    """B16 判定：agent 上报 TARGET_BROKEN + 少量 5xx 证据，或裸 5xx 达硬阈值。

    纯函数（便于自测）：marker 提供上下文判断（区分"服务坏了"与"题目就是
    打崩服务/fuzz 噪声"），硬阈值兜底覆盖 agent 未上报但服务明显全崩的场次。
    """
    return (marker and fault_hits >= max(1, min_hits)) or fault_hits >= max(1, hard_hits)


def _beat() -> None:
    """更新心跳文件 mtime（失败静默）"""
    try:
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
    except Exception:
        pass


# ── provider 画像：「换 provider」以前要改三处写死的代码，现在只改配置 ──
#
# 容器内 provider 名是**网关路由标签**，必须在三处保持一致：
#   1) `--model <provider>/<id>` 的前缀
#   2) `$HOME/.pi/agent/models.json` 的 providers 键
#   3) 该 provider 的官方凭据 env 名（pi 的 apiKey 写 "$<ENV>"）
# 此前 1/2 写死 "deepseek"、3 写死 DEEPSEEK_API_KEY。
#
# 配置入口（优先级从高到低）：back-end 构造参数 provider > SOLVER_PROVIDER >
# ADAPTER_PROVIDER > "deepseek"。模型仍由 SOLVER_MODEL 或 preset 决定。
_PROVIDER_PROFILES: dict[str, dict] = {
    "deepseek": {
        "api": "openai-completions",
        "api_key_env": "DEEPSEEK_API_KEY",
        "context_window": 1000000,
        "max_tokens": 384000,
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
    },
    "glm": {
        "api": "openai-completions",
        "api_key_env": "GLM_API_KEY",
        "context_window": 200000,
        "max_tokens": 131072,
        "input": ["text"],
        "reasoning": True,
    },
    "openai": {
        "api": "openai-completions",
        "api_key_env": "OPENAI_API_KEY",
        "context_window": 400000,
        "max_tokens": 128000,
        "input": ["text", "image"],
        "reasoning": True,
    },
    "anthropic": {
        "api": "anthropic-messages",
        "api_key_env": "ANTHROPIC_API_KEY",
        "context_window": 200000,
        "max_tokens": 64000,
        "input": ["text", "image"],
        "reasoning": True,
    },
    "openrouter": {
        "api": "openai-completions",
        "api_key_env": "OPENROUTER_API_KEY",
        "context_window": 200000,
        "max_tokens": 64000,
        "input": ["text", "image"],
        "reasoning": True,
    },
}

# 未知 provider 的兼底画像：OpenAI 兼容 + <PROVIDER>_API_KEY。
# "换 provider 不用改仓库代码" 靠的就是这一条 —— 只要网关能路由。
_FALLBACK_PROFILE: dict = {
    "api": "openai-completions",
    "context_window": 200000,
    "max_tokens": 65536,
    "input": ["text"],
    "reasoning": True,
}


def resolve_provider(explicit: str = "") -> str:
    """显式参数 > SOLVER_PROVIDER > ADAPTER_PROVIDER > deepseek。"""
    return (explicit
            or os.environ.get("SOLVER_PROVIDER")
            or os.environ.get("ADAPTER_PROVIDER")
            or DEFAULT_PROVIDER).strip().lower()


def provider_profile(provider: str) -> dict:
    """合并兼底画像与 provider 专属画像；api_key_env 缺失时按名推导。"""
    p = resolve_provider(provider)
    prof = dict(_FALLBACK_PROFILE)
    prof.update(_PROVIDER_PROFILES.get(p, {}))
    if not prof.get("api_key_env"):
        prof["api_key_env"] = f"{p.upper().replace('-', '_')}_API_KEY"
    prof["name"] = p
    return prof


def normalize_model(model: str, provider: str = "") -> str:
    """将模型名规范化为 provider/model 格式（缺 provider 时补配置的 provider）"""
    model = (model or "").strip()
    if not model:
        return ""
    if "/" in model:
        return model
    return f"{resolve_provider(provider)}/{model}"


def split_model(model: str, provider: str = "") -> tuple[str, str]:
    """解析出 (provider, model_id)。

    模型自带 `<provider>/` 前缀时**前缀优先** —— 否则 `--model openai/gpt-5`
    会与 `models.json` 里按 ADAPTER_PROVIDER 写出的 provider 键不一致，
    pi 直接 503/找不到路由。裸模型名才回退到配置的 provider。
    """
    m = (model or "").strip()
    if "/" in m:
        p, _, mid = m.partition("/")
        if p and mid:
            return p.strip().lower(), mid
    return resolve_provider(provider), m


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


def _merge_partial_output(previous: str, current: str, *, limit: int = 32768) -> str:
    """合并同一工具调用的流式输出，兼容 cumulative 与 delta 两种事件语义。"""
    previous, current = previous or "", current or ""
    if not current:
        return previous[-limit:]
    if current.startswith(previous):
        merged = current
    elif previous.endswith(current):
        merged = previous
    else:
        merged = previous + current
    return merged[-limit:]


def _stop_process_tree(proc: subprocess.Popen, *, force: bool = False,
                       wait_seconds: float = 10.0) -> None:
    """终止 Pi 及其工具子进程，并始终 wait，避免 PPID=1 zombie 残留。"""
    if proc.poll() is not None:
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        if os.name == "posix":
            os.killpg(proc.pid, sig)
        elif force:
            proc.kill()
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=wait_seconds)
    except subprocess.TimeoutExpired:
        if not force:
            _stop_process_tree(proc, force=True, wait_seconds=wait_seconds)


def _instance_cleanup_token(workdir: str) -> str:
    """Read one answer-free driver trace token, or return no cleanup scope."""
    try:
        with open(os.path.join(workdir, "_instance.json"), encoding="utf-8") as handle:
            token = str((json.load(handle) or {}).get("trace_scope", "") or "")
    except (OSError, ValueError, TypeError):
        return ""
    return token if _INSTANCE_TOKEN_RX.fullmatch(token) else ""


def _tagged_processes(token: str) -> list[int]:
    """Return live PIDs that inherited exactly one Pi instance token."""
    if not _INSTANCE_TOKEN_RX.fullmatch(str(token or "")):
        return []
    marker = (_INSTANCE_TOKEN_ENV + "=" + token).encode("ascii")
    found: list[int] = []
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return found
    mine = os.getpid()
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == mine:
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as handle:
                values = handle.read().split(b"\0")
        except OSError:
            continue
        if marker in values:
            found.append(pid)
    return found


def cleanup_instance_processes(workdir: str, *, grace_seconds: float = 2.0) -> int:
    """Stop escaped Pi/tool descendants after one challenge visit ends.

    This deliberately keys on the driver's per-instance random token rather
    than process name, parent PID, or a broad ``pkill``.  It therefore cannot
    touch the driver, the VPN provider, or another worker's task.  Returns the
    number of tagged processes observed before termination.
    """
    token = _instance_cleanup_token(workdir)
    pids = _tagged_processes(token)
    if not pids:
        return 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while time.monotonic() < deadline:
        if not _tagged_processes(token):
            return len(pids)
        time.sleep(0.05)
    for pid in _tagged_processes(token):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return len(pids)


# 沿革（2026-09 死码清扫）：此处原有 `strip_provider(model)`，零调用点。
# 实际拆前缀的是 `split_model(model, provider)`（返回 provider 与 model id 两段，
# `_write_pi_models` 用它）。`adapter/config.py` 里那句「model id 由 strip_provider
# 后透传」是句空话，一并改掉了。


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
        if ev.get("type") == "tool_execution_update":
            # 工具可能在输出 flag 后卡死，没有 end 事件。保留尾部窗口给 eager
            # 与超时收口取证，同时避免把 cumulative partial 写成二次方大文件。
            partial = _join_content((ev.get("partialResult") or {}).get("content"))
            if not partial:
                return ""
            return json.dumps(
                {"type": "tool_execution_update", "toolCallId": ev.get("toolCallId", ""),
                 "toolName": ev.get("toolName", ""),
                 "partialResult": {"content": [{"type": "text", "text": partial[-4096:]}]},
                 "incomplete": True},
                ensure_ascii=False,
            )
        return raw
    msg = ev.get("assistantMessageEvent") or {}
    mtype = msg.get("type", "")
    delta = msg.get("delta", "")
    return json.dumps(
        {"type": "message_update", "assistantMessageEvent": {"type": mtype, "delta": delta}},
        ensure_ascii=False,
    )


def _write_pi_models(pi_home: str, *, base_url: str, model: str,
                     provider: str = "") -> None:
    """把可用的模型配置写入 pi 的 $HOME/.pi/agent/models.json。

    pi 以题目 workdir 下的 .pi-home 作为 HOME（上下文隔离），若不在这里
    配置 provider，pi 会回退到默认 provider / 官方端点（openrouter、
    api.deepseek.com 等），导致网关 key 401 / 模型不存在 -> 每次会话 0 turns。

    生成**单 provider** 配置：provider 名与凭据 env、api 类型、上下文窗口都取自
    `provider_profile()`（即 `ADAPTER_PROVIDER` 可配，见该函数），baseUrl 用平台
    网关（ANTHROPIC_BASE_URL），模型 id 与 solver 配置一致（如 mimo-v2.5）。
    """
    prov_name, model_id = split_model(model, provider)
    prof = provider_profile(prov_name)
    if not model_id:
        return
    base = (base_url or "").rstrip("/")
    if not base:
        return
    model_entry = {
        "id": model_id,
        "name": model_id,
        "contextWindow": prof.get("context_window", 200000),
        "maxTokens": prof.get("max_tokens", 65536),
        "input": prof.get("input", ["text"]),
        "reasoning": bool(prof.get("reasoning", True)),
    }
    if prof.get("compat"):
        model_entry["compat"] = prof["compat"]
    cfg = {
        "providers": {
            prof["name"]: {
                "baseUrl": base,
                "api": prof.get("api", "openai-completions"),
                "apiKey": "$" + prof["api_key_env"],
                "models": [model_entry],
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


# ── [B57] 子 Agent：把官方 subagent 扩展与框架自有角色装进本题 HOME ──
_SUBAGENT_PKG_REL = "examples/extensions/subagent"
# 角色定义放 adapter/ 下（构建期 COPY 到 /app/adapter/pi_agents，见 Dockerfile 第 5 步）。
# 注意别放仓库根：容器里只存在镜像自带的那几棵树，仓库根的目录**根本不在容器里**
# （B57 首版就栽在这，角色软链静默落空）。
# 框架自有资产，不含任何题目情报。
_AGENT_ROLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pi_agents")


def _subagent_pkg_dir() -> str:
    """官方 subagent 扩展在镜像里的位置；找不到返回空串。"""
    for root in ("/usr/local/lib/node_modules", "/usr/lib/node_modules",
                 os.path.expanduser("~/.nvm/versions/node")):
        p = os.path.join(root, "@earendil-works", "pi-coding-agent",
                         _SUBAGENT_PKG_REL)
        if os.path.isdir(p):
            return p
    return ""


_SUBAGENT_EXIT_PATCH_MARKER = "// [tsecbench] subagent exit fix applied"


def _patch_subagent_exit_bug(ext_src: str) -> None:
    """修复 pi-agent subagent 子进程完成任务后不退出的 bug。

    根因：子 pi 进程完成 AI 任务后，Node.js 事件循环有残留 handle
    （文件描述符/定时器），进程不退出 → proc.on("close") 永不触发
    → 父 Agent 的 Promise 永不 resolve → tool_execution_end 永不发出。

    修复：在 processLine 检测到 terminal stopReason 后，启动一个
    grace period 定时器（默认 10s），到时强杀子进程。
    """
    import glob as _glob
    index_ts = os.path.join(ext_src, "index.ts")
    if not os.path.isfile(index_ts):
        return
    try:
        with open(index_ts, "r", encoding="utf-8") as f:
            src = f.read()
        if _SUBAGENT_EXIT_PATCH_MARKER in src:
            return  # already patched

        # 1. 在 tool_result_end 处理后插入 agent_end 退出检测。
        #    agent_end 是子进程 agent 运行的真正终点；stopReason 不可靠
        #    （实际值为驼峰 "toolUse"，且工具调用轮次不是终点，会误杀干活中的子Agent）。
        old_tail = (
            '\t\t\t\tif (event.type === "tool_result_end" && event.message) {\n'
            '\t\t\t\t\tcurrentResult.messages.push(event.message as Message);\n'
            '\t\t\t\t\temitUpdate();\n'
            '\t\t\t\t}\n'
            '\t\t\t};\n'
        )
        new_tail = (
            '\t\t\t\tif (event.type === "tool_result_end" && event.message) {\n'
            '\t\t\t\t\tcurrentResult.messages.push(event.message as Message);\n'
            '\t\t\t\t\temitUpdate();\n'
            '\t\t\t\t}\n'
            '\n'
            '\t\t\t\t// [tsecbench] subagent exit fix applied\n'
            '\t\t\t\t// agent_end = child agent run is truly complete. pi bug:\n'
            '\t\t\t\t// process sometimes hangs after completion. Force-kill\n'
            '\t\t\t\t// after a grace period so the parent can collect results.\n'
            '\t\t\t\tif (event.type === "agent_end") {\n'
            '\t\t\t\t\tconst graceMs = parseInt(\n'
            '\t\t\t\t\t\tprocess.env.PI_SUBAGENT_EXIT_GRACE_MS '
            '|| "10000", 10);\n'
            '\t\t\t\t\tsetTimeout(() => {\n'
            '\t\t\t\t\t\tif (!processClosed) {\n'
            '\t\t\t\t\t\t\tproc.kill("SIGTERM");\n'
            '\t\t\t\t\t\t\tsetTimeout(() => {\n'
            '\t\t\t\t\t\t\t\tif (!processClosed) '
            'proc.kill("SIGKILL");\n'
            '\t\t\t\t\t\t\t}, 5000);\n'
            '\t\t\t\t\t\t}\n'
            '\t\t\t\t\t}, graceMs);\n'
            '\t\t\t\t}\n'
            '\t\t\t};\n'
        )
        if old_tail not in src:
            log.debug("[B57] subagent index.ts 结构不匹配，跳过 exit patch")
            return
        src = src.replace(old_tail, new_tail, 1)

        # 2. 加 processClosed 标志
        src = src.replace(
            'let wasAborted = false;\n',
            'let wasAborted = false;\n\t\tlet processClosed = false;\n', 1)

        # 3. close handler 设 processClosed = true
        src = src.replace(
            'proc.on("close", (code) => {\n'
            '\t\t\t\tif (buffer.trim()) processLine(buffer);',
            'proc.on("close", (code) => {\n'
            '\t\t\t\tprocessClosed = true;\n'
            '\t\t\t\tif (buffer.trim()) processLine(buffer);', 1)

        with open(index_ts, "w", encoding="utf-8") as f:
            f.write(src)

        # 4. 清 jiti 缓存，强制重编译
        for cached in _glob.glob("/tmp/jiti/subagent-index.*.mjs"):
            try:
                os.unlink(cached)
            except OSError:
                pass
        log.info("[B57] subagent exit bug patch applied")
    except Exception as exc:
        log.debug("[B57] subagent exit patch failed (ignored): %s", exc)


def _patch_subagent_parallel_bugs(ext_src: str) -> None:
    """[B66] 修并行派发三缺陷（09-15 首次真实派发实测暴露）。

    1. 假 done：runSingleAgent 的 currentResult 初始化 exitCode=0，而并行
       分支用 exitCode===-1 表示「还在跑」——子 Agent 发出第一条流式更新就被
       数成 done（实测一次派发 138 条进度里 137 条写着 2/2 done，而当时
       两个孩子都还在跑，worker 跑到会话被杀都没停）。初始化改 -1，
       close 后才落真实退出码。
    2. 无时限：父会话的 max_turns 由 driver 在事件流侧执行，子进程一个
       上限都拿不到；跑不完的子任务把父会话拖到被杀，tool_execution_end
       永不发出 → 父 Agent 拿不到任何结论，子任务烧的配额全作废（实测：
       worker 子 Agent 连跑 10.5 分钟未完成，父会话全程干等，最后被
       stop_check 终止，一次派发零产出）。加挂钟预算
       PI_SUBAGENT_TIMEOUT_MS（默认 480000=8 分钟）：到时 SIGTERM→5s→
       SIGKILL，close 照常触发，父 Agent 拿到部分结果（exitCode=124，
       stderr 注明超时）。
    3. update 洪流：子进程每条事件都重发一次全量进度（内容没变也发，
       实测 10 分钟 137 条一模一样的行）。加去重闸：计数没变不发。

    各补丁独立锚定、独立幂等；锚点失配逐条跳过，互不影响；异常一律吞掉。
    """
    import glob as _glob
    index_ts = os.path.join(ext_src, "index.ts")
    if not os.path.isfile(index_ts):
        return
    try:
        with open(index_ts, "r", encoding="utf-8") as f:
            src = f.read()
        applied = []

        # ── 1. 假 done：exitCode 0 → -1（并声明超时标志，供 2/3 引用）──
        old_init = (
            '\tconst currentResult: SingleResult = {\n'
            '\t\tagent: agentName,\n'
            '\t\tagentSource: agent.source,\n'
            '\t\ttask,\n'
            '\t\texitCode: 0,\n'
        )
        if old_init in src:
            src = src.replace(old_init, (
                '\t// [tsecbench] subagent parallel fixes:\n'
                '\t// -1 = still running（原值 0 让并行计数从第一条 update\n'
                '\t//   起就把在跑的子 Agent 数成 done）。\n'
                '\tlet subagentTimedOut = false;\n'
                '\tconst currentResult: SingleResult = {\n'
                '\t\tagent: agentName,\n'
                '\t\tagentSource: agent.source,\n'
                '\t\ttask,\n'
                '\t\texitCode: -1,\n'
            ), 1)
            applied.append("progress-exitcode")

            # ── 2. 挂钟预算（依赖上面的 subagentTimedOut 声明）──
            if "PI_SUBAGENT_TIMEOUT_MS" not in src:
                # 锚点闭合行是 3 tab（与 handler 行对齐，不是正文的 4 tab）
                # ——09-15 逐字节对拍实测，人工数 tab 三次全错。
                old_stderr = (
                    '\t\t\tproc.stderr.on("data", (data) => {\n'
                    '\t\t\t\tcurrentResult.stderr += data.toString();\n'
                    '\t\t\t});\n'
                )
                if old_stderr in src:
                    src = src.replace(old_stderr, old_stderr + (
                        '\n'
                        '\t\t\t// [tsecbench] 子 Agent 挂钟预算：子进程拿不到\n'
                        '\t\t\t// max_turns（那是 driver 在事件流侧执行的），\n'
                        '\t\t\t// 没有上限的子任务会把父会话拖到被杀且结果全损。\n'
                        '\t\t\t// 到时强杀，close 照常触发 → 父 Agent 拿部分结果。\n'
                        '\t\t\tconst timeoutMs = parseInt(\n'
                        '\t\t\t\tprocess.env.PI_SUBAGENT_TIMEOUT_MS '
                        '|| "480000", 10);\n'
                        '\t\t\tconst _budgetExited = { done: false };\n'
                        '\t\t\tproc.on("exit", () => { _budgetExited.done = '
                        'true; });\n'
                        '\t\t\tsetTimeout(() => {\n'
                        '\t\t\t\tif (!subagentTimedOut && !_budgetExited.done)'
                        ' {\n'
                        '\t\t\t\t\tsubagentTimedOut = true;\n'
                        '\t\t\t\t\tcurrentResult.stderr += '
                        '"\\n[tsecbench] subagent wall-clock budget '
                        'exhausted (" + timeoutMs + "ms); '
                        'killed, partial result returned";\n'
                        '\t\t\t\t\tproc.kill("SIGTERM");\n'
                        '\t\t\t\t\tsetTimeout(() => {\n'
                        '\t\t\t\t\t\tif (!_budgetExited.done) '
                        'proc.kill("SIGKILL");\n'
                        '\t\t\t\t\t}, 5000);\n'
                        '\t\t\t\t}\n'
                        '\t\t\t}, timeoutMs);\n'
                    ), 1)
                    applied.append("wallclock")

            # ── 3. 超时退出码（同依赖声明；被杀进程 close 给的是 null）──
            old_exit = '\t\tcurrentResult.exitCode = exitCode;\n'
            if old_exit in src:
                src = src.replace(
                    old_exit,
                    '\t\tcurrentResult.exitCode = '
                    'subagentTimedOut ? 124 : exitCode;\n', 1)
                applied.append("exit-124")

        # ── 4. update 去重闸（独立：计数没变就不重发）──
        if "_lastParallelKey" not in src:
            old_emit = (
                '\t\t\t\tconst emitParallelUpdate = () => {\n'
                '\t\t\t\t\tif (onUpdate) {\n'
                '\t\t\t\t\t\tconst running = allResults.filter('
                '(r) => r.exitCode === -1).length;\n'
                '\t\t\t\t\t\tconst done = allResults.filter('
                '(r) => r.exitCode !== -1).length;\n'
            )
            if old_emit in src:
                src = src.replace(old_emit, (
                    '\t\t\t\t// [tsecbench] 计数没变不重发（实测一次派发\n'
                    '\t\t\t\t// 刷了 137 条一模一样的进度行）。\n'
                    '\t\t\t\tlet _lastParallelKey = "";\n'
                ) + old_emit + (
                    '\t\t\t\t\t\tconst _key = done + "/" '
                    '+ allResults.length + "/" + running;\n'
                    '\t\t\t\t\t\tif (_key === _lastParallelKey) return;\n'
                    '\t\t\t\t\t\t_lastParallelKey = _key;\n'
                ), 1)
                applied.append("update-dedup")

        # ── 5. 并行汇总带超时注记（单任务模式的错误路径天然带 stderr，
        #    不用补；并行汇总行原本只写 completed/failed，父 Agent 无从
        #    知道是「预算耗尽被召回」还是「任务本身失败」）──
        if "_budgetNote" not in src:
            old_sum = ('\t\t\t\t\treturn `[${r.agent}] ${r.exitCode === 0 ? '
                       '"completed" : "failed"}: ${preview || "(no output)"}`;\n')
            if old_sum in src:
                src = src.replace(old_sum, (
                    '\t\t\t\t\t// [tsecbench] 超时(124)把预算注记带进汇总，\n'
                    '\t\t\t\t\t// 父 Agent 才知道要切小粒度重派而不是原样重试。\n'
                    '\t\t\t\t\tconst _budgetNote = r.exitCode === 124\n'
                    '\t\t\t\t\t\t? " [" + ((r.stderr || "").split("\\n")'
                    '.filter(Boolean).pop()\n'
                    '\t\t\t\t\t\t|| "wall-clock budget exhausted") + "]"\n'
                    '\t\t\t\t\t\t: "";\n'
                ) + old_sum.replace('`;\n', '${_budgetNote}`;\n'), 1)
                applied.append("parallel-summary-note")

        if not applied:
            return
        with open(index_ts, "w", encoding="utf-8") as f:
            f.write(src)
        # 清 jiti 缓存，强制重编译
        for cached in _glob.glob("/tmp/jiti/subagent-index.*.mjs"):
            try:
                os.unlink(cached)
            except OSError:
                pass
        log.info("[B66] subagent parallel patches applied: %s",
                 ",".join(applied))
    except Exception as exc:
        log.debug("[B66] subagent parallel patch failed (ignored): %s", exc)


def _inject_role_model(text: str, model: str) -> str:
    """在角色 frontmatter 里注入/覆盖 `model:` 字段。

    子 Agent 必须使用求解模型（Web 设置页 ANTHROPIC_MODEL）。若角色无 model，
    子 Agent 会落到 pi 默认模型（可能是 models.json 里不存在的 → 503）。
    """
    if not model:
        return text
    fm_start = text.find("---")
    fm_end = text.find("\n---", 3)
    if fm_start != 0 or fm_end < 0:
        # 不是标准 frontmatter：把 model 追加到开头最安全？直接原样返回。
        return text
    front = text[3:fm_end]
    lines = front.splitlines()
    out = []
    injected = False
    for ln in lines:
        if ln.strip().startswith("model:"):
            out.append(f"model: {model}")
            injected = True
        else:
            out.append(ln)
    if not injected:
        # frontmatter 末尾补 model（在 description/tools 之后）
        out.append(f"model: {model}")
    new_front = "\n".join(out)
    return "---\n" + new_front + text[fm_end:]


def _install_subagents(pi_home: str, provider: str = "") -> bool:
    """[B57] 把子 Agent 能力装进本题 HOME，返回是否可用。

    pi 只从 `$HOME/.pi/agent/extensions/*/index.ts` 与
    `$HOME/.pi/agent/agents/*.md` 自动发现扩展与角色。本框架给**每道题独立的
    HOME**（见本文件 `env["HOME"] = pi_home`），所以装载点必须在这里 ——
    装到 `/root` 永远不会被发现。

    用符号链接指向镜像内自带的官方示例：不 vendor 第三方代码、pi 升版自动跟随。
    全程幂等；任何异常一律吞掉返回 False —— 子 Agent 是加分项，
    装不上绝不能拖垮解题主路径。
    """
    from ..verify import subagent_enabled
    if not subagent_enabled():
        return False
    try:
        ext_src = _subagent_pkg_dir()
        if not ext_src:
            log.debug("[B57] 镜像内找不到 subagent 示例扩展，跳过")
            return False

        def _relink(src: str, dst: str) -> None:
            """幂等软链：已是正确链接就不动，否则清掉重建。"""
            if os.path.islink(dst):
                if os.readlink(dst) == src:
                    return
                os.unlink(dst)
            elif os.path.isdir(dst):
                shutil.rmtree(dst)
            elif os.path.exists(dst):
                os.unlink(dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(src, dst)

        _relink(ext_src, os.path.join(pi_home, ".pi", "agent", "extensions",
                                      "subagent"))
        # 角色：复制到每题 pi-home 并注入当前求解模型，而不是直接软链。
        # 子 Agent 必须用「Web 设置页 / 容器 env 的 ANTHROPIC_MODEL」——
        # 若角色不写 model，pi 默认落到 deepseek-v4-pro（models.json 没有该
        # 模型 → 503 No available provider route）。写死字面量又不跟随设置，
        # 故这里按每题启动时的 ANTHROPIC_MODEL 动态生成角色副本。
        agent_dir = os.path.join(pi_home, ".pi", "agent", "agents")
        os.makedirs(agent_dir, exist_ok=True)
        _model_override = normalize_model(
            (os.environ.get("ANTHROPIC_MODEL", "")
             or os.environ.get("ADAPTER_SOLVER_MODEL", "")
             or "mimo-v2.5"), provider)
        if os.path.isdir(_AGENT_ROLES_DIR):
            for _role_md in ("worker.md", "scout.md", "checker.md"):
                _src_md = os.path.join(_AGENT_ROLES_DIR, _role_md)
                _dst_md = os.path.join(agent_dir, _role_md)
                if not os.path.isfile(_src_md):
                    continue
                try:
                    with open(_src_md, encoding="utf-8") as _rf:
                        _text = _rf.read()
                    # 替换/注入 model 字段（frontmatter 内）
                    _text = _inject_role_model(_text, _model_override)
                    with open(_dst_md, "w", encoding="utf-8") as _wf:
                        _wf.write(_text)
                except OSError:
                    log.debug("[B57] 角色 %s 复制失败（忽略）", _role_md)
        else:
            log.debug("[B57] 角色目录不存在 %s，仅装载扩展", _AGENT_ROLES_DIR)

        # ── 修复子 Agent 进程不退出的 bug ──
        _patch_subagent_exit_bug(ext_src)
        # ── [B66] 修复并行派发三缺陷（假 done / 无时限 / update 洪流）──
        _patch_subagent_parallel_bugs(ext_src)

        log.info("[B57] subagent 就绪: model=%s", _model_override)
        return True
    except Exception as exc:      # noqa: BLE001
        log.debug("[B57] 子 Agent 装载失败（忽略，不影响解题）: %s", exc)
        return False


# ── [B67] 技能自主调用：把仓库 skills/ 装进本题 HOME（pi 原生发现）──
# 与 subagent 同一装载坑：pi 只从 `$HOME/.pi/agent/skills/` 发现技能，本框架给
# 每题独立 HOME → 必须逐题软链。skills/ 是 bind-mount（容器内 /app/skills），
# 软链而非复制：改技能/补正则即时生效，无需镜像重建或 compose 改动。
# pi 的原生机制（渐进披露）：系统提示只放 <available_skills>（名字+描述+路径），
# Agent 按自己对题目的分析用 read 主动加载 SKILL.md 全文——即「Agent 自我决策
# 调哪个 skill」；taskprompt 只补一段自主调用指引，框架不再预选正文。
def _install_skills(pi_home: str) -> int:
    """[B67] 把仓库 skills/ 软链进本题 HOME，返回已装载的技能数。

    幂等；任何异常一律吞掉——技能装不上只退回纯框架注入，
    绝不能拖垮解题主路径。
    """
    from ..verify import skill_agent_enabled
    if not skill_agent_enabled():
        return 0
    try:
        # 定位由 contracts 单源（env → /app/skills → 从本文件上溯）。此前是
        # "三级上溯即仓库根"：那在朋友的 `adapter/solver/` 布局里对，搬进
        # `redpilot/worker/adapter/solver/` 之后指向不存在的 skills/ 路径
        # —— pi 的原生技能面静默归零（agent 拿不到任何 SKILL.md）。
        from redpilot.contracts.paths import is_skill_dir, skills_root as _skills_root
        skills_root = _skills_root(__file__, extra="/app/skills")
        if not skills_root:
            log.debug("[B67] 找不到 skills/ 目录，跳过")
            return 0
        dest = os.path.join(pi_home, ".pi", "agent", "skills")
        os.makedirs(dest, exist_ok=True)
        installed = 0
        for entry in sorted(os.listdir(skills_root)):
            src = os.path.join(skills_root, entry)
            # pi 的发现规则：含 SKILL.md 的目录才算技能（判据单源，与 SkillStore._scan 同）
            if not os.path.isdir(src) or not is_skill_dir(src):
                continue
            link = os.path.join(dest, entry)
            if os.path.islink(link):
                if os.readlink(link) == src:
                    installed += 1          # 已装，幂等通过
                    continue
                os.unlink(link)             # 指向变了，重建
            elif os.path.isdir(link):
                continue                    # 真目录（非本函数产物），别动
            elif os.path.exists(link):
                os.unlink(link)
            os.symlink(src, link)
            installed += 1
        if installed:
            log.info("[B67] pi 原生技能装载 %d 个（Agent 可自主调用）", installed)
        return installed
    except Exception as exc:      # noqa: BLE001
        log.debug("[B67] 技能装载失败（忽略，不影响解题）: %s", exc)
        return 0


def _write_pi_settings(pi_home: str) -> None:
    """把 provider 重试/超时旋钮写入 pi 的 $HOME/.pi/agent/settings.json。

    背景：pi 未配置时直接用 openai SDK 的默认值（总超时 10 分钟、重试 2 次），
    日志里反复出现 `pi terminal error: Request timed out.`，且发生在会话开始
    后 60~130s（远早于客户端 10 分钟超时，容器内 api.deepseek.com 走 LAN 不经
    VPN）→ 是上游网关偶发失败，但一次失败就把该回合判死，会话剩余预算空转。

    pi 已暴露这组旋钮（settings-manager.js getProviderRetrySettings ->
    sdk.js streamSimple），此前本项目从未写过 settings.json，故一直取默认值：
      - provider.timeoutMs：取「正常回合之上、空等之下」。实测正常回合
        130~200s，故默认 300s —— 连接真死了 5 分钟就放弃重试，而不是干等 10 分钟。
      - provider.maxRetries：偶发失败重试而不是判死。
    可用环境变量覆盖（PI_PROVIDER_TIMEOUT_MS / PI_PROVIDER_MAX_RETRIES /
    PI_PROVIDER_MAX_RETRY_DELAY_MS / PI_RETRY_MAX）。

    已存在的 settings.json 会先读入再合并，保留其它键。
    """
    def _int_env(name: str, default: int) -> int:
        try:
            return int(float(os.environ.get(name, "") or default))
        except (TypeError, ValueError):
            return default

    retry = {
        "enabled": True,
        "maxRetries": _int_env("PI_RETRY_MAX", 4),
        "baseDelayMs": _int_env("PI_RETRY_BASE_DELAY_MS", 2000),
        "provider": {
            "timeoutMs": _int_env("PI_PROVIDER_TIMEOUT_MS", 300000),
            "maxRetries": _int_env("PI_PROVIDER_MAX_RETRIES", 4),
            "maxRetryDelayMs": _int_env("PI_PROVIDER_MAX_RETRY_DELAY_MS", 30000),
        },
    }
    try:
        cfg_dir = os.path.join(pi_home, ".pi", "agent")
        os.makedirs(cfg_dir, exist_ok=True)
        path = os.path.join(cfg_dir, "settings.json")
        cfg = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg = loaded
            except (OSError, ValueError):
                cfg = {}
        cfg["retry"] = retry
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        log.warning("failed to write pi settings.json under %s", pi_home)


class PiAgentBackend(SolverBackend):
    """Pi Agent CLI 求解器（json print 一次性模式）"""

    name = "pi-agent"

    def __init__(self, *, cmd: str = "pi", model: str = "",
                 skills_dir: str = "", max_turns: int = 60, thinking: str = "",
                 provider: str = ""):
        self.cmd = shutil.which(cmd) or cmd
        # provider 可配（back-end 参数 > SOLVER_PROVIDER > ADAPTER_PROVIDER > deepseek）
        self.provider = resolve_provider(provider)
        self.model = normalize_model(model, self.provider)
        self.skills_dir = skills_dir
        self.max_turns = max_turns
        # 思考模式强度（Web 端「思考模式（Thinking）」→ ADAPTER_PI_THINKING）：
        # "" 不传（用 pi 默认）；否则透传为 pi --thinking <level>
        # (off|minimal|low|medium|high|xhigh|max)。
        self.thinking = (thinking or "").strip().lower()

    def _build_cmd(self, prompt: str, api_key: str = "",
                   transport: str = "print") -> list[str]:
        # rpc: 常驻 JSONL（prompt 走 stdin，不拼 argv）；print: 一次性（原行为）
        rpc = (transport or "").strip().lower() == "rpc"
        cmd = [self.cmd, "--mode", "rpc" if rpc else "json"]
        if not rpc:
            cmd.append("--print")
        cmd.append("--no-session")
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

        if not rpc:
            cmd.append(prompt)   # rpc 的 prompt 由传输层经 stdin 发送
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
        on_event: Optional[Callable] = None,
        control: Optional[Callable] = None,
    ) -> SolveResult:
        result = SolveResult()
        t0 = time.monotonic()

        # 配置合并: 显式参数 > solver_cfg
        # provider 可配：solver_cfg（ADAPTER_PROVIDER 派生）优先，其次本实例
        provider = resolve_provider(getattr(solver_cfg, "provider", "") or self.provider)
        model = self.model or normalize_model(getattr(solver_cfg, "model", ""), provider)
        provider, _ = split_model(model, provider)   # 模型前缀优先（见 split_model）
        skills = self.skills_dir or getattr(solver_cfg, "skills_dir", "")
        # 凭据回退也要跟着 provider 走（不再写死 DEEPSEEK_API_KEY）
        api_key = (getattr(solver_cfg, "api_key", "")
                   or os.environ.get(provider_profile(provider)["api_key_env"], ""))

        backend = PiAgentBackend(cmd=self.cmd, model=model, skills_dir=skills,
                                 max_turns=solver_cfg.max_turns, thinking=self.thinking,
                                 provider=provider)
        # 传输选择：rpc(常驻 JSONL，默认) / print(一次性，回退)。
        # 见 docs/pi-rpc-migration-research.md；老版 pi 无 --mode rpc 时自动降级。
        _transport_name = (os.environ.get("ADAPTER_PI_TRANSPORT", "rpc")
                           or "rpc").strip().lower()
        cmd = backend._build_cmd(prompt, api_key, transport=_transport_name)

        # 上下文隔离：每道题使用独立的 HOME（本题 workdir 下），
        # 隔离 pi 的全局状态/缓存/会话残留，防止跨题上下文污染导致的幻觉。
        # 不同题 workdir 不同 → HOME 天然隔离；同一题多轮共享该 HOME（保留 MEMORY）。
        env = {**os.environ}
        pi_home = os.path.join(workdir, ".pi-home")
        try:
            os.makedirs(pi_home, exist_ok=True)
            # 降权后 pi 要能写自己的 HOME（缓存/会话/技能软链），必须先把
            # 属主交给求解身份；driver 后续写入的文件靠题目目录的默认 ACL。
            _iso_cfg = IsolationConfig.from_env()
            isolation.prepare_pi_home(pi_home, _iso_cfg)
            env["HOME"] = pi_home
        except OSError:
            _iso_cfg = IsolationConfig.from_env()
            env["HOME"] = os.environ.get("HOME", "/root")
        # Tool children inherit this tag.  It is not prompt material and it
        # contains no task text or answer; the driver uses it only to clean up
        # a detached background process when the whole challenge visit ends.
        _instance_token = _instance_cleanup_token(workdir)
        if _instance_token:
            env[_INSTANCE_TOKEN_ENV] = _instance_token

        # 运行时写入 pi 模型配置（网关 baseUrl + 实际模型 id），
        # 否则 pi 回退到官方端点/默认模型，导致 key 401 / 0 turns。
        _write_pi_models(pi_home, base_url=getattr(solver_cfg, "base_url", "") or
                         os.environ.get("ANTHROPIC_BASE_URL", ""),
                         model=model or getattr(solver_cfg, "model", ""),
                         provider=provider)
        # 同一处补上重试/超时旋钮（见 _write_pi_settings 注释）。
        _write_pi_settings(pi_home)
        # [B57] 子 Agent 能力随 HOME 一起装（pi 只从 $HOME 发现扩展/角色）
        _install_subagents(pi_home, provider=provider)
        # [B67] 技能自主调用：同一装载坑——pi 只从 $HOME/.pi/agent/skills/ 发现技能
        _install_skills(pi_home)

        if transcript_path:
            os.makedirs(os.path.dirname(transcript_path), exist_ok=True)

        tool_outputs = []
        all_output_parts = []
        saw_net_failure = False   # 本会话是否见过真实网络失败签名（B12 接地）
        saw_target_broken = False  # agent 是否上报 TARGET_BROKEN（B16）
        fault_hits = 0             # 本会话目标 5xx 的工具输出条数（B16）
        # [B54] 会话内幻觉活体哨兵。框架要到会话结束才看得到 result.flags，
        # 中途唯一的观测点是每条工具调用的命令参数与输出 —— 由哨兵实时比对。
        _halu_sentinel = None
        try:
            from .. import hallucination as _hm
            if _hm.abort_enabled():
                _halu_sentinel = _hm.LiveSentinel()
        except Exception:
            _halu_sentinel = None
        _fault_min = int(os.environ.get("ADAPTER_TARGET_FAULT_MIN", "3") or "3")
        _fault_hard = int(os.environ.get("ADAPTER_TARGET_FAULT_HARD_MIN", "60") or "60")
        turns = 0
        pending_calls = {}   # toolCallId -> args（tool_execution_end 不带 args）
        pending_partials = {}  # toolCallId -> capped partial output（超时也可取证）
        text_buf = ""      # 助手文本累积（text_delta 是增量）
        text_parts: list = []   # 助手文本分段（B14 续接块回捞；只收助手文本，
                                # 不收工具输出——避免把 cat MEMORY.md 回显的旧块当本场结论）
        thinking_buf = ""  # 思考流（忽略，不进入 observed_output）

        def _emit(kind: str, payload: dict | None = None) -> None:
            """流式观测出口（可选）。只读，异常吞掉不影响求解。"""
            if not on_event:
                return
            try:
                on_event(kind, payload or {})
            except Exception as e:
                log.warning("on_event callback error: %s", e)

        deadline = t0 + max(30, int(getattr(solver_cfg, "session_seconds", 0) or 0))
        for attempt in range(max_retries + 1):
            if time.monotonic() >= deadline:
                result.termination_reason = result.termination_reason or "timeout"
                result.error = result.error or "session_timeout"
                break
            transport = None
            try:
                transport = make_transport(
                    transport=_transport_name, cmd_base=cmd, prompt=prompt,
                    workdir=workdir, env=env, stop_fn=_stop_process_tree,
                    cmd_path=self.cmd,
                    identity=isolation.resolve_identity(_iso_cfg))
                proc = transport.proc

                transcript_f = None
                if transcript_path:
                    transcript_f = open(transcript_path, "a", encoding="utf-8", buffering=1)

                try:
                    # 看门狗：子进程连续无输出超过 STALL_TIMEOUT 秒视为卡死
                    # → 杀掉子进程并重开会话（外层 for attempt 会重试）
                    # 注意：必须用 os.read 非阻塞读（readline 会在部分行时永久阻塞，
                    #       导致看门狗失效、会话悬挂）
                    STALL_TIMEOUT = float(os.environ.get("PI_STALL_TIMEOUT", "480"))
                    stall_deadline = time.monotonic() + STALL_TIMEOUT
                    # RPC 启动护栏：pi 会「接受 prompt（response success）但完全
                    # 静默」—— 实测形态：模型/凭据无法解析时不发 agent_start、
                    # 不报错、也不退出（print 模式会非零退出，由 0-turn 护栏接住）。
                    # 不设这条就只能等会话 deadline（默认 1500s）白烧完整场预算。
                    _rpc_startup_grace = float(
                        os.environ.get("ADAPTER_RPC_STARTUP_GRACE", "90"))
                    _stop_check_interval = 5  # check stop every 5s
                    _last_stop_check = time.monotonic()

                    def _stop_now() -> bool:
                        """stop_check 命中（通关/任务结束）→ 立即终止会话。

                        原先只初始化了 _stop_check_interval/_last_stop_check 却从未
                        调用 stop_check，驱动承诺的"通关立即终止"是死代码：会话只能
                        跑满时间盒（实测任务 23:09 结束后仍白跑 10 分钟）。
                        """
                        nonlocal _last_stop_check
                        if stop_check is None:
                            return False
                        if time.monotonic() - _last_stop_check < _stop_check_interval:
                            return False
                        _last_stop_check = time.monotonic()
                        try:
                            if not stop_check():
                                return False
                        except Exception as e:
                            log.warning("stop_check error: %s", e)
                            return False
                        log.info("pi session 提前终止：stop_check 命中（通关/任务结束）")
                        transport.stop()
                        result.termination_reason = "stopped"
                        return True
                    line_buf = ""
                    junk_tail = ""   # 非 JSON 行（stderr 已被并进 stdout）
                    session_timed_out = False
                    session_stopped = False   # stop_check 命中主动收尾
                    session_settled = False   # RPC 可靠终态（print 模式收尾靠进程退出）
                    _events_seen = 0          # 本场收到的真实事件数（协议帧不计）
                    _session_start = time.monotonic()
                    # ── 子 Agent 静默看门狗（兜底） ──
                    # 检测「事件流停止」而非 "N/N done"（该信号在子 Agent 刚开始
                    # 工作时就误报——currentResult.exitCode 初始为 0 非 -1）。
                    # 子 Agent 干活时 update 持续流动 → 不杀；只有完全静默
                    # （真死锁：完成了但不退出且不再产生事件）超时才杀。
                    # 正常完成由 exit patch（agent_end 检测）处理。
                    _subagent_last_event: dict[str, float] = {}
                    _subagent_active: set = set()
                    _SUBAGENT_SILENCE_SECS = float(
                        os.environ.get("PI_SUBAGENT_DEADLOCK_SECS", "600"))

                    def _kill_main_pi_children() -> int:
                        """SIGTERM 主 pi 的直接子进程，返回杀掉的个数。"""
                        _main_pid = proc.pid
                        _killed = 0
                        try:
                            for _cdir in os.listdir("/proc"):
                                if not _cdir.isdigit():
                                    continue
                                _cpid = int(_cdir)
                                if _cpid == _main_pid:
                                    continue
                                try:
                                    with open(f"/proc/{_cpid}/stat") as _sf:
                                        _stat = _sf.read()
                                    _ppid = int(_stat.split()[3])
                                    if _ppid == _main_pid:
                                        os.kill(_cpid, signal.SIGTERM)
                                        _killed += 1
                                except (OSError, ValueError, IndexError):
                                    pass
                        except OSError:
                            pass
                        return _killed
                    def _drain_control() -> None:
                        """把编排层的面预算动作送进 RPC 会话（print 无双向通道）。

                        只在确实有动作时发帧；control() 必须便宜且绝不抛异常
                        （编排层内部已兜底，这里再兜一层，求解路径优先）。
                        """
                        if control is None or not getattr(
                                transport, "terminates_on_settled", False):
                            return
                        try:
                            frames = control() or []
                        except Exception as exc:
                            log.warning("control callback error: %s", exc)
                            return
                        for frame in frames:
                            if isinstance(frame, dict) and frame.get("type"):
                                transport.send(frame)
                                _emit("control", {
                                    "frame": str(frame.get("type")),
                                    "detail": str(frame.get("message", ""))[:200],
                                })

                    while True:
                        _drain_control()
                        # tick 粒度 = stop_check 轮询间隔：静默会话也按 5s
                        # 醒来检查 deadline/stall/stop_check（原为 30s，判定
                        # 最多滞后 30s）。
                        if not transport.wait(_stop_check_interval):
                            # 会话预算：即使子进程静默（如长工具/长思考）也必须到点终止——
                            # 否则只要进程持续吐行（重置 stall）就永不超时，
                            # 单会话可远超 session_seconds，导致轮次时间盒失效。
                            if time.monotonic() > deadline:
                                log.warning("pi session timeout after %ds (quiet)",
                                            int(solver_cfg.session_seconds))
                                transport.stop()
                                session_timed_out = True
                                result.termination_reason = "timeout"
                                result.error = result.error or "session_timeout"
                                _emit("system", {"phase": "timeout",
                                                 "detail": "session deadline reached"})
                                break
                            if time.monotonic() > stall_deadline:
                                log.warning("pi session stalled %ds (no output) — killing and retrying",
                                            STALL_TIMEOUT)
                                transport.stop(force=True)
                                result.error = "stalled_no_output"
                                result.termination_reason = "stalled"
                                break
                            # RPC：prompt 已接受却一个事件都没有 → 启动即失败
                            # （模型/凭据解析不了）。提前判死，别烧完整场预算。
                            if (transport.terminates_on_settled and _events_seen == 0
                                    and time.monotonic() - _session_start
                                    > _rpc_startup_grace):
                                log.warning(
                                    "RPC 会话 %.0fs 内无任何事件（prompt 已接受但 "
                                    "agent 未启动）—— 典型原因：模型不可解析/凭据缺失",
                                    time.monotonic() - _session_start)
                                transport.stop(force=True)
                                result.error = "rpc_no_agent_start"
                                result.termination_reason = "error"
                                break
                            # ── 子 Agent 静默看门狗 ──
                            # 活跃 subagent 调用若超过静默阈值无任何事件，
                            # 判定死锁（完成不退出 / API 悬挂），杀子进程解锁。
                            if _subagent_active:
                                _now = time.monotonic()
                                for _sa_id in list(_subagent_active):
                                    _last = _subagent_last_event.get(_sa_id, 0)
                                    if (_last and
                                            _now - _last > _SUBAGENT_SILENCE_SECS):
                                        _killed = _kill_main_pi_children()
                                        log.warning(
                                            "subagent silence %ds (call %s) — "
                                            "killed %d child process(es)",
                                            int(_now - _last), _sa_id[:16], _killed)
                                        _subagent_active.discard(_sa_id)
                            if _stop_now():
                                break
                            continue
                        chunk = transport.read()
                        if not chunk:
                            break  # EOF
                        line_buf += chunk
                        stall_deadline = time.monotonic() + STALL_TIMEOUT
                        if session_timed_out or session_stopped:
                            break
                        while "\n" in line_buf:
                            raw, line_buf = line_buf.split("\n", 1)
                            line = raw.strip()
                            if not line:
                                continue

                            if session_timed_out:
                                break

                            _beat()

                            if _stop_now():
                                session_stopped = True
                                break

                            if transcript_f:
                                slim = _slim_line(line)
                                if slim:
                                    transcript_f.write(slim + "\n")
                                    transcript_f.flush()

                            if time.monotonic() > deadline:
                                log.warning("pi session timeout after %ds", solver_cfg.session_seconds)
                                transport.stop()
                                session_timed_out = True
                                result.termination_reason = "timeout"
                                result.error = result.error or "session_timeout"
                                break

                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                # stderr 并进了 stdout，所以非 JSON 行几乎全是
                                # 诊断文本。留个尾巴：非零退出时它是唯一的线索。
                                junk_tail = (junk_tail + " " + line)[-400:]
                                continue

                            event_type = event.get("type", "")
                            _events_seen += 1

                            # ── 工具调用 ──
                            if event_type == "tool_execution_start":
                                turns += 1
                                _emit("tool_start", {"tool": event.get("toolName", ""),
                                                     "args": event.get("args") or {}})
                                if backend.max_turns > 0 and turns > backend.max_turns:
                                    log.warning("pi session reached max_turns=%d", backend.max_turns)
                                    result.error = "max_turns_reached"
                                    _emit("system", {"phase": "stalled",
                                                     "detail": f"max_turns={backend.max_turns}"})
                                    result.termination_reason = "max_turns"
                                    transport.stop()
                                    session_timed_out = True
                                    break
                                _targs = event.get("args") or {}
                                pending_calls[event.get("toolCallId", "")] = _targs
                                if _halu_sentinel is not None:   # [B54] 活体观测：命令参数
                                    _halu_sentinel.on_call(_targs)
                                # 子 Agent 调用注册：静默看门狗开始追踪
                                if (event.get("toolName", "") == "subagent"
                                        and event.get("toolCallId")):
                                    _sa_id = event.get("toolCallId")
                                    _subagent_active.add(_sa_id)
                                    _subagent_last_event[_sa_id] = time.monotonic()
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
                                # Preserve completion status alongside the
                                # original command for provenance consumers.
                                # A silent `curl -o response` has no stdout;
                                # treating a mere end event as success could
                                # let a failed fetch bless a stale local file.
                                if isinstance(tool_args, dict):
                                    tool_args = dict(tool_args)
                                    tool_args["__tsecbench_execution_ok"] = (
                                        event.get("isError") is False)
                                pending_partials.pop(event.get("toolCallId", ""), None)
                                _subagent_active.discard(event.get("toolCallId", ""))
                                _subagent_last_event.pop(event.get("toolCallId", ""), None)
                                out = _join_content(event.get("result", {}).get("content"))
                                # Keep calls with an empty result as provenance
                                # records too.  Otherwise `printf flag{x} >
                                # input.bin` vanishes and a later `cat
                                # input.bin` can be mistaken for independent
                                # local evidence.  Fact extraction remains
                                # output-only below.
                                tool_outputs.append((tool_name, tool_args, out))
                                if out:
                                    all_output_parts.append(out)
                                    if _halu_sentinel is not None:   # [B54] 活体观测：工具输出
                                        _halu_sentinel.on_output(out)
                                    if on_fact:
                                        try:
                                            on_fact(tool_name, tool_args, out)
                                        except Exception as e:
                                            log.warning("on_fact callback error: %s", e)
                                    _emit("tool_progress",
                                          {"preview": out[-400:] if out else ""})
                                    _emit("turn_done", {})
                                    # INFRA_BLOCKED 接地（B12）：marker 由 agent
                                    # 自己输出（写文件/回显 MEMORY.md 旧结论都会
                                    # 带上），单凭它判定"内网不可达"会把 agent
                                    # 自述当证据（实测某题开场 cat MEMORY.md
                                    # 即置位）。要求本会话此前（或本条输出内）
                                    # 出现过真实网络失败签名才认可。
                                    _out_low = out.lower()
                                    if any(s in _out_low for s in _INFRA_FAILURE_SIGNS):
                                        saw_net_failure = True
                                    if saw_net_failure and "INFRA_BLOCKED" in out:
                                        result.infra_blocked = True
                                    # B16：目标服务故障证据累计（判定在会话结束收口）。
                                    # 除错误页特征串外，也认"整条输出就是一个 5xx 状态码"。
                                    if (any(s in _out_low for s in _TARGET_FAULT_SIGNS)
                                            or out.strip().lower() in _FAULT_BARE_STATUS):
                                        fault_hits += 1
                                    if "TARGET_BROKEN" in out:
                                        saw_target_broken = True
                                    for f in extract_flags(out):
                                        if f not in result.flags:
                                            result.flags.append(f)
                                    # [B54] 硬干预：同一 body 被反复写进命令参数、且本会话
                                    # 任何工具输出里都没出现过 → 判「自造声明成瘾」，掐断。
                                    # 输出里出现过即免疫（那已是有效证据，属真解路径）。
                                    # 退出管路复用超时那套：置 session_timed_out + break 内层，
                                    # 外层读循环据此收口（见 pi_agent 读循环结构）。
                                    if _halu_sentinel is not None:
                                        _hv = _halu_sentinel.verdict()
                                        if _hv:
                                            log.warning(
                                                "[B54] 做题幻觉掐断：body %s（%d 字符）被写进"
                                                "命令参数 %d 次、工具输出里从未出现 —— 判为"
                                                "自造声明，提前终止本会话",
                                                _halu_sentinel.fingerprint(_hv), len(_hv),
                                                _halu_sentinel.authored.get(_hv, 0))
                                            result.error = "hallucination_abort"
                                            _emit("system", {"phase": "stalled",
                                                             "detail": "hallucination_abort"})
                                            transport.stop()
                                            result.termination_reason = "stopped"
                                            session_timed_out = True
                                            break

                            elif event_type == "tool_execution_update":
                                # 部分输出可能是 delta 也可能是全量快照；按调用 ID
                                # 合并并在会话中断时作为 incomplete 证据落盘。
                                partial = _join_content(event.get("partialResult", {}).get("content"))
                                call_id = event.get("toolCallId", "")
                                if partial and call_id:
                                    pending_partials[call_id] = _merge_partial_output(
                                        pending_partials.get(call_id, ""), partial)
                                # ── 子 Agent 事件追踪 ──
                                # 每条 subagent update 都刷新时间戳：事件在流动 =
                                # 子 Agent 在干活，静默看门狗不触发。
                                if event.get("toolName", "") == "subagent" and call_id:
                                    _subagent_last_event[call_id] = time.monotonic()

                            # ── 助手文本（delta 增量，累积） ──
                            elif event_type == "message_update":
                                msg = event.get("assistantMessageEvent") or {}
                                mtype = msg.get("type", "")
                                delta = msg.get("delta", "")
                                if mtype == "text_delta" and delta:
                                    text_buf += delta
                                    _emit("text", {"preview": text_buf[-400:]})
                                elif mtype == "thinking_delta" and delta:
                                    thinking_buf += delta
                                    _emit("thinking", {"length": len(thinking_buf)})

                            # ── 终态 ──
                            elif event_type in ("agent_end", "turn_end", "message_end"):
                                if text_buf:
                                    all_output_parts.append(text_buf)
                                    text_parts.append(text_buf)
                                    if "TARGET_BROKEN" in text_buf:   # B16：助手文本标记
                                        saw_target_broken = True
                                    text_buf = ""
                                # BUG: 模型层错误（如 402 Insufficient Balance / 认证失败）出现在
                                # stopReason="error" + errorMessage，不匹配下方 error 事件分支，
                                # 导致 result.error 一直为 None → 框架 API 熔断不触发 → 空转开关靶场。
                                # 这里捕获终态里的 stopReason/errorMessage。
                                #
                                # 朋友 driver 里 message["stopReason"] 是标量（本函数原本只试了那条
                                # 形状）；但 pi 的 agent_end 事件用的是 messages 数组。两种形状都认，
                                # 否则 agent_end 这一路（pi 真实的收尾事件）永远读不出错误，
                                # 0-turn 护栏对"假 pi 只发 agent_end"这类故障是失效的。
                                _msgs = event.get("messages")
                                if isinstance(_msgs, list) and _msgs:
                                    _stop = _msgs[-1]
                                else:
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

                            elif event_type == "agent_settled":
                                # RPC 的可靠终态：retry/compaction/队列均已落定。
                                # print 模式不依赖它（收尾靠进程退出）。
                                session_settled = True

                            elif event_type == "error" or "error" in event_type.lower():
                                result.error = event.get("message") or event.get("error") or str(event)[:200]
                                log.warning("pi error: %s", result.error[:200])

                        if session_settled and transport.terminates_on_settled:
                            break

                    # 末尾补上未 flush 的文本
                    if text_buf:
                        all_output_parts.append(text_buf)
                        text_parts.append(text_buf)

                    # 工具在输出后超时/被中断时不会产生 end 事件。把已收到的
                    # partial 作为 incomplete 输出交给事实库与 flag 提取，避免
                    # 最后一条长命令的证据在会话边界蒸发。
                    for call_id, partial in list(pending_partials.items()):
                        if not partial:
                            continue
                        tool_args = pending_calls.pop(call_id, {})
                        tool_outputs.append(("incomplete_tool", tool_args, partial))
                        all_output_parts.append(partial)
                        if on_fact:
                            try:
                                on_fact("incomplete_tool", tool_args, partial)
                            except Exception as e:
                                log.warning("on_fact partial callback error: %s", e)
                        for flag in extract_flags(partial):
                            if flag not in result.flags:
                                result.flags.append(flag)
                    pending_partials.clear()

                    # RPC 常驻进程在 agent_settled 后仍然活着：必须显式关停，
                    # 否则下面的 proc.wait(30) 会白等超时。print 模式此处是 no-op。
                    transport.shutdown()
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        transport.stop(force=True)
                        try:
                            proc.wait(timeout=10)
                        except Exception:
                            pass
                    # 非零退出且没留下任何错误 —— 0-turn 护栏的最后一道。
                    # 典型形态：模型名写错 / 缺凭据 / 参数错误 → pi 立刻非零退出，
                    # stdout 一行 JSON 都没有（turns==0）。这时 stderr 是唯一的
                    # 线索，而本引擎把 stderr 并进了 stdout（stderr=STDOUT），
                    # 非 JSON 的行在解析处被跳过 → 什么都不剩。
                    #
                    # 不在这里兜住，编排层会把它当成"正常跑完但没解出来"
                    # （2026-09-08 那次静默烧题的另一种形态）。诊断文本很关键：
                    # "pi exited 2" 与 "model not found: prov/nope" 对排障不是一个量级。
                    if not result.error and proc.returncode:
                        detail = " ".join(junk_tail.split()) or "(no output)"
                        result.error = f"pi exited {proc.returncode}: {detail}"
                        _emit("error", {"error": result.error})
                        result.termination_reason = "error"
                    if not result.termination_reason:
                        result.termination_reason = "completed"

                finally:
                    if transcript_f:
                        transcript_f.close()
                    if proc.poll() is None:
                        transport.stop(force=True)

                # 只有静默 stall 才在同一总时间盒内重开一次 Pi；timeout 已到
                # 总截止，stopped/max_turns 则交给 driver 开下一场。
                if (result.termination_reason == "stalled"
                        and attempt < max_retries
                        and time.monotonic() + 3 < deadline):
                    time.sleep(3)
                    continue
                break

            except Exception as e:
                log.error("pi session attempt %d failed: %s", attempt + 1, e)
                result.error = str(e)
                result.termination_reason = result.termination_reason or "error"
                if attempt < max_retries and time.monotonic() + 3 < deadline:
                    time.sleep(3)
                    continue

        # B16：目标服务故障统一裁定（证据在流式扫描中累计）
        if target_fault_verdict(fault_hits, saw_target_broken,
                                min_hits=_fault_min, hard_hits=_fault_hard):
            result.target_fault = True
            log.warning("目标服务故障判定：5xx 输出 %d 条，TARGET_BROKEN=%s（B16）",
                        fault_hits, saw_target_broken)

        result.tool_outputs = tool_outputs
        result.observed_output = "\n".join(all_output_parts[-50:])
        result.turns = turns
        result.duration_s = time.monotonic() - t0
        if all_output_parts:
            result.final_text = all_output_parts[-1]
        # B14：续接块回捞。只扫助手文本（不含工具输出），预算耗尽/被杀
        # 的场次同样能捞到会话中途写过的块。
        result.handoff = extract_handoff("\n".join(text_parts))

        # 从 FLAG 文件读取
        self._read_flag_files(workdir, result.flags)

        log.info("pi session done: %d turns, %.0fs, %d flags, err=%s",
                 turns, result.duration_s, len(result.flags),
                 result.error[:60] if result.error else "none")
        return result
