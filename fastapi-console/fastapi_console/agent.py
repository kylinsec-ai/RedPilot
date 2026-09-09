"""Agent 舰队控制：通过 Docker Compose 启停/监控 tsecbench worker 容器。

worker 容器（tsecbench-worker-1/2/3）由 TsecBench-main/docker-compose.yaml 定义，
每个容器一个 Pi Agent，自动拉取平台题目解题并提交。
"""

from __future__ import annotations

import json
import threading
import os
import subprocess
from pathlib import Path
from typing import Any

from tsecbench.errors import APIError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # TsecBench-main/
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yaml"
AGENT_ENV_FILE = PROJECT_ROOT / ".agent.env"
WORK_STATUS_DIR = PROJECT_ROOT / "work" / "status"
WORKER_NAMES = ["tsecbench-worker-1", "tsecbench-worker-2", "tsecbench-worker-3"]

ENV_KEYS = ("BENCHMARK_BASE_URL", "BENCHMARK_TOKEN", "SOLVER_API_KEY",
             "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
             "SOLVER_SESSION_SECONDS", "ADAPTER_PI_THINKING")


def _run(args: list[str], *, timeout: float = 60, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise APIError(503, "docker_unavailable", f"Docker 调用失败: {exc}") from exc


def _compose_env() -> dict[str, str]:
    """compose 环境：以 .agent.env 为准，剔除宿主环境里的冲突变量（如 demo-token）。"""
    env = dict(os.environ)
    for key in ENV_KEYS:
        env.pop(key, None)
    env.update(load_agent_env())
    return env


def load_agent_env() -> dict[str, str]:
    """读取 .agent.env（KEY=VALUE 行），供 compose 注入。"""
    env: dict[str, str] = {}
    if not AGENT_ENV_FILE.exists():
        return env
    for line in AGENT_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _thinking_value(values: dict) -> str:
    """Web 端「思考模式（Thinking）」→ ADAPTER_PI_THINKING 映射。

    llmThinking(bool) + llmReasoningEffort(low/medium/high)：
    - 勾选开  → effort 原样（low/medium/high）
    - 未勾选  → off
    """
    eff = str(values.get("llmReasoningEffort") or "low").lower()
    if any(k in values and values[k] for k in ("llmThinking",)):
        return eff if eff in ("low", "medium", "high") else "off"
    return "off"


def save_agent_env(values: dict[str, str]) -> None:
    """将 Agent 配置写入 .agent.env（隐藏 Key）。"""
    current = load_agent_env()
    normalized = {k: v for k, v in values.items() if k in ENV_KEYS}
    if ("llmThinking" in values) or ("llmReasoningEffort" in values):
        normalized["ADAPTER_PI_THINKING"] = _thinking_value(values)
    current.update(normalized)
    lines = ["# TSecBench Agent 舰队配置（由控制台写入，请勿提交）"]
    for key in ENV_KEYS:
        lines.append(f"{key}={current.get(key, '')}")
    AGENT_ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _worker_status_file(worker: str) -> Path:
    if worker == "tsecbench-single":
        return WORK_STATUS_DIR / "worker-9.json"
    return WORK_STATUS_DIR / f"{worker.replace('tsecbench-worker-', 'worker-')}.json"


def _parse_worker_stats_from_logs(worker: str) -> dict[str, Any]:
    """从 worker 容器日志解析战绩（镜像内旧 driver 无状态文件时使用）。

    日志格式: FLAG CORRECT on <code>: flag{...} (+200 pts, total 200)
    """
    import re

    result = _run(["docker", "logs", "--tail", "5000", worker], timeout=30)
    if result.returncode != 0:
        return {}
    logs = result.stdout + result.stderr
    flags_found: list[str] = []
    total = 0
    solved_codes: set[str] = set()
    for m in re.finditer(r"FLAG CORRECT on ([^\s:]+):.*?\(([+-]?\d+) pts, total (\d+)\)", logs):
        code, cum = m.group(1), int(m.group(3))
        flag_m = re.search(r"FLAG CORRECT on %s: ([^\s]+)" % re.escape(code), logs)
        if flag_m and flag_m.group(1) not in flags_found:
            flags_found.append(flag_m.group(1))
        total = cum
        solved_codes.add(code)
    current = ""
    cur_m = re.findall(r"round \d+ visit ([^\s]+)", logs)
    if cur_m:
        current = cur_m[-1]
    event = ""
    ev_m = re.findall(r"(session done|FLAG CORRECT|flag INCORRECT|INFRA_BLOCKED|pi session timeout)", logs)
    if ev_m:
        event = ev_m[-1]
    return {
        "current_code": current,
        "last_event": event,
        "flags_found": flags_found,
        "flags_submitted": len(flags_found),
        "total_earned": total,
        "challenges_solved": len(solved_codes),
    }


def _read_worker_status(worker: str) -> dict[str, Any]:
    path = _worker_status_file(worker)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _aggregate_events() -> dict[str, Any]:
    """从 _events.jsonl 聚合舰队实时进展（镜像内旧 driver 无状态文件时兜底）。"""
    path = PROJECT_ROOT / "work" / "_events.jsonl"
    events: list[dict] = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines()[-1000:]:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass

    summary = {"total_earned": 0, "flags_submitted": 0, "flags_found": [], "solved": 0}
    active: dict[str, float] = {}
    solved_codes: set[str] = set()
    for event in events:
        payload = event.get("payload") or {}
        etype = event.get("event")
        if etype == "session_start":
            active[payload.get("code")] = event.get("ts", 0)
        elif etype == "session_end":
            code = payload.get("code")
            active.pop(code, None)
        elif etype == "flag_submit":
            code = payload.get("code")
            if payload.get("correct"):
                summary["flags_submitted"] += 1
                summary["total_earned"] += int(payload.get("awarded", 0) or 0)
                flag = payload.get("flag") or ""
                if flag and flag not in summary["flags_found"]:
                    summary["flags_found"].append(flag)
                if code and code not in solved_codes:
                    solved_codes.add(code)
                    summary["solved"] += 1
    # 进行中的题目（最近 session_start 且尚未 session_end）
    current = [
        {"code": code, "since": ts}
        for code, ts in sorted(active.items(), key=lambda kv: -kv[1])
    ][:5]
    last_events = [e.get("event", "") for e in events[-5:]]
    return {"summary": summary, "current": current, "last_events": last_events}


def fleet_status() -> dict[str, Any]:
    """容器状态 + worker 状态文件 + 事件流聚合 + 单题定向容器列表。"""
    events = _aggregate_events()
    workers: list[dict[str, Any]] = []
    for name in WORKER_NAMES:
        status = _run(["docker", "inspect", name, "--format",
                       "{{.State.Status}}|{{.State.Health.Status}}|{{.State.ExitCode}}|{{.State.Running}}"])
        if status.returncode != 0:
            workers.append({"name": name, "container": "absent", "running": False,
                            "health": "", "exit_code": None, "state": {}})
            continue
        fields = status.stdout.strip().split("|")
        running = fields[3] == "true"
        health = fields[1] if len(fields) > 1 and fields[1] else ("" if running else "")
        exit_code = int(fields[2]) if len(fields) > 2 and fields[2].isdigit() else None
        state = _read_worker_status(name)
        if running and not state:
            # 无状态文件（镜像内旧 driver）：从各自容器日志解析战绩
            state = _parse_worker_stats_from_logs(name)
        workers.append({
            "name": name,
            "container": fields[0],
            "running": running,
            "health": health,
            "exit_code": exit_code,
            "state": state,
        })

    summary = {
        "total": len(workers),
        "running": sum(1 for w in workers if w["running"]),
        "healthy": sum(1 for w in workers if w["health"] == "healthy"),
        "flags_found": [],
        "flags_submitted": 0,
        "total_earned": 0,
        "solved": 0,
        "current": [],
    }
    for worker in workers:
        state = worker.get("state") or {}
        summary["flags_found"] = list(dict.fromkeys(summary["flags_found"] + list(state.get("flags_found", []))))
        summary["flags_submitted"] += int(state.get("flags_submitted", 0) or 0)
        summary["total_earned"] += int(state.get("total_earned", 0) or 0)
        summary["solved"] += int(state.get("challenges_solved", 0) or 0)
        if state.get("current_code"):
            summary["current"].append(
                {"worker": worker["name"], "code": state["current_code"],
                 "round": state.get("current_round", 0), "event": state.get("last_event", "")}
            )
    # 状态文件缺失时以事件流兜底
    if not any(w.get("state") for w in workers):
        summary["flags_found"] = events["summary"]["flags_found"]
        summary["flags_submitted"] = events["summary"]["flags_submitted"]
        summary["total_earned"] = events["summary"]["total_earned"]
        summary["solved"] = events["summary"]["solved"]
        for item in events["current"]:
            summary["current"].append({"code": item["code"], "round": "进行中", "event": "session active"})
    summary["flags_found"] = summary["flags_found"][:20]
    summary["fleet_events"] = events["last_events"]

    # 派单队列（供网页区分"派单中/自动解题"状态）
    priority = {}
    try:
        if PRIORITY_FILE.exists():
            for line in PRIORITY_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "|" in line:
                    code, wid = line.split("|", 1)
                    priority[code.strip()] = int(wid.strip()) if wid.strip().isdigit() else -1
    except OSError:
        pass
    summary["priority"] = priority

    # 单题定向容器（网页「单独自动解」拉起的 tsecbench-single-*）
    singles: list[dict[str, Any]] = []
    result = _run(["docker", "ps", "-a", "--filter", "name=tsecbench-single-",
                   "--format", "{{.Names}}|{{.Status}}|{{.State}}"], timeout=30)
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.strip().split("|")
            if len(parts) < 3:
                continue
            name = parts[0]
            code = name.replace("tsecbench-single-", "", 1)
            s = _read_worker_status("tsecbench-single")
            singles.append({
                "name": name,
                "code": code,
                "status": parts[1],
                "running": parts[2] == "true",
                "state": s,
            })
    return {"workers": workers, "summary": summary, "env_configured": bool(load_agent_env()), "singles": singles}


def fleet_start() -> dict[str, Any]:
    if not AGENT_ENV_FILE.exists():
        raise APIError(400, "agent_env_missing", "请先在设置页配置 Agent 舰队（平台地址 / Token / SOLVER_API_KEY）")
    # 新任务周期开始：轮转历史事件文件、清理旧状态文件，
    # 确保战绩统计从 0 开始（不显示上一轮任务的旧分数）
    _rotate_stats()
    result = _run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "--env-file", str(AGENT_ENV_FILE), "up", "-d"],
        timeout=300,
        env=_compose_env(),
    )
    if result.returncode != 0:
        raise APIError(500, "agent_start_failed", f"启动失败: {result.stderr.strip()[-500:]}")
    return fleet_status()


def _rotate_stats() -> None:
    """新任务周期开始：重置所有记忆与战绩，防止跨轮作弊。

    - 清空事件文件（战绩只统计本轮；历史轮转备份一并删除）
    - 删除全部题目工作目录（MEMORY.md / FLAG / blackboard / 转录 / 工具产物）
    - 删除根级共享黑板
    - 清理旧 worker 状态文件
    - 清理 flag 归属登记（status/owners-worker-*.json）与历史任务优先题号（priority.txt）
      —— 两者跨轮保留设计，任务轮换时不清即成「外部历史答题记忆」（合规红线）
    保留：status/ 心跳运行文件（自动重建）、_events.jsonl（截断清空）、monitor.log
    """
    import time

    work_dir = PROJECT_ROOT / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1. 清空事件日志（截断保留 inode：worker 以 O_APPEND 追加，句柄不破坏）
    events = work_dir / "_events.jsonl"
    try:
        if events.exists() and events.stat().st_size > 0:
            with events.open("r+b") as f:
                f.truncate(0)
    except OSError:
        pass

    # 2. 清空题目工作目录与共享黑板（防跨轮记忆作弊）
    keep = {"status", "_events.jsonl", "monitor.log", "_blackboard.json.bak"}
    try:
        for child in work_dir.iterdir():
            if child.name in keep or child.name.startswith("_events."):
                continue
            if child.is_dir():
                import shutil
                shutil.rmtree(child, ignore_errors=True)
                log_sys = child  # noqa
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    except OSError:
        pass

    # 3. 清理旧 worker 状态文件
    try:
        for old in (WORK_STATUS_DIR.glob("worker-*.json") if WORK_STATUS_DIR.exists() else []):
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass

    # 清理 flag 归属登记（跨轮保留设计 → 任务轮换时不清即成外部历史答题记忆）
    try:
        for f in (work_dir / "status").glob("owners-worker-*.json"):
            try:
                f.unlink()
            except OSError:
                pass
    except OSError:
        pass

    # 兜底：事件文件即使为空也保证存在（driver obs 会追加）
    try:
        events.touch(exist_ok=True)
    except OSError:
        pass

    # 4. 清空历史事件备份（防外部答题记忆残留）
    try:
        for bak in work_dir.glob("_events.*.bak"):
            try:
                bak.unlink()
            except OSError:
                pass
    except OSError:
        pass

    # 5. 清空浏览器会话记录（含 console_ai_history 答题历史 / 远端题目状态）
    import shutil
    sessions_dir = PROJECT_ROOT / "data" / "fastapi_sessions"
    try:
        if sessions_dir.is_dir():
            for f in sessions_dir.glob("*.json"):
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def fleet_stop() -> dict[str, Any]:
    result = _run(["docker", "compose", "-f", str(COMPOSE_FILE), "stop"], timeout=120)
    if result.returncode != 0:
        raise APIError(500, "agent_stop_failed", f"停止失败: {result.stderr.strip()[-500:]}")
    return fleet_status()


def worker_logs(worker: str, tail: int = 200) -> str:
    if worker not in WORKER_NAMES:
        raise APIError(400, "unknown_worker", "未知 worker")
    result = _run(["docker", "logs", "--tail", str(tail), worker], timeout=30)
    if result.returncode != 0:
        raise APIError(404, "worker_not_found", f"容器 {worker} 不存在或未运行")
    return result.stdout + result.stderr

# ── 实时对话流（transcript）─────────────────────────────
# driver 把 pi 每轮事件的 JSONL（session/turn/tool_call/thinking/text…）
# 实时追加到 work/<code>/_transcripts/roundX_sessionY.jsonl（共享挂载，宿主直读）。
# 这里提供「会话列表 + 按行增量读取」，前端轮询即可看到 Agent 正在调什么工具。

def _safe_transcript(rel: str) -> Path | None:
    """只允许定位到 work/<code>/_transcripts/*.jsonl，防路径穿越。"""
    if not rel or "\\" in rel:
        return None
    try:
        p = (PROJECT_ROOT / "work" / rel).resolve()
        p.relative_to((PROJECT_ROOT / "work").resolve())
    except (ValueError, OSError):
        return None
    if p.parent.name != "_transcripts" or p.suffix != ".jsonl":
        return None
    return p


def list_transcripts() -> dict[str, Any]:
    """扫描各题 _transcripts 下的会话文件（按 code/round/session 聚合元信息）。"""
    files: list[dict[str, Any]] = []
    work_dir = PROJECT_ROOT / "work"
    if work_dir.is_dir():
        for d in sorted(p for p in work_dir.iterdir() if p.is_dir()):
            tdir = d / "_transcripts"
            if not tdir.is_dir():
                continue
            for f in sorted(tdir.glob("*.jsonl")):
                try:
                    st = f.stat()
                    lines = sum(1 for _ in f.open(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                files.append({
                    "code": d.name,
                    "file": f"{d.name}/_transcripts/{f.name}",
                    "name": f.name,
                    "size": st.st_size,
                    "lines": lines,
                    "mtime": int(st.st_mtime),
                })
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": files}


def read_transcript(rel: str, line: int = 0, limit: int = 300) -> dict[str, Any]:
    """从第 line 行（0=全部）开始增量读取某 transcript，返回解析后的事件数组。"""
    p = _safe_transcript(rel)
    if p is None or not p.exists():
        raise APIError(404, "transcript_not_found", "transcript 文件不存在")
    start = max(0, int(line or 0))
    cap = max(1, min(500, int(limit or 300)))
    events: list[dict[str, Any]] = []
    total = 0
    with p.open(encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh, 1):
            total = i
            if i < start or len(events) >= cap:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                events.append({"type": "raw", "line": raw})
    return {"file": rel, "from_line": start, "total_lines": total, "events": events}


# ── LLM Token 用量统计（transcripts 聚合）─────────────────
# pi 每条 assistant 消息的 message_end.usage 携带 input/output/cacheRead/
# cacheWrite（totalTokens = input+cacheRead+output；推理 token 已计入 output，
# provider 不单列）。transcript 为 append-only JSONL：offset 增量读取 +
# 进程内缓存，页面轮询零成本。

_USAGE_FIELDS = ("input", "output", "cacheRead", "cacheWrite")
_usage_cache: dict[str, dict[str, Any]] = {}
_usage_lock = threading.Lock()


def _usage_file_totals(f: Path) -> dict[str, Any] | None:
    """单 transcript 的累计用量（offset 增量读取，缓存于 _usage_cache）。"""
    key = str(f)
    try:
        size = f.stat().st_size
    except OSError:
        return None
    ent = _usage_cache.get(key)
    if ent is None or size < ent["offset"]:
        ent = {"offset": 0, "totals": {k: 0 for k in _USAGE_FIELDS}, "calls": 0, "model": ""}
    totals = ent["totals"]
    if size == ent["offset"]:
        return {**totals, "calls": ent["calls"], "model": ent["model"]}
    try:
        with f.open("rb") as fh:
            fh.seek(ent["offset"])
            chunk = fh.read()
    except OSError:
        return {**totals, "calls": ent["calls"], "model": ent["model"]}
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return {**totals, "calls": ent["calls"], "model": ent["model"]}   # 尚无完整新行
    for raw in chunk[:last_nl].split(b"\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "message_end":
            continue
        m = ev.get("message") or {}
        if m.get("role") != "assistant":
            continue
        u = m.get("usage") or {}
        for k in _USAGE_FIELDS:
            totals[k] += u.get(k) or 0
        ent["calls"] += 1
        if m.get("model"):
            ent["model"] = m["model"]
    ent["offset"] += last_nl + 1
    _usage_cache[key] = ent
    return {**totals, "calls": ent["calls"], "model": ent["model"]}


def usage_summary() -> dict[str, Any]:
    """聚合全部题目的 LLM token 用量：舰队总览 + 按题分项（网页用量面板）。"""
    work_dir = PROJECT_ROOT / "work"
    per_code: dict[str, dict[str, Any]] = {}
    model = ""
    seen: set[str] = set()
    if work_dir.is_dir():
        with _usage_lock:
            for d in sorted(p for p in work_dir.iterdir() if p.is_dir()):
                tdir = d / "_transcripts"
                if not tdir.is_dir():
                    continue
                agg = {k: 0 for k in _USAGE_FIELDS}
                calls = sessions = 0
                for f in sorted(tdir.glob("*.jsonl")):
                    seen.add(str(f))
                    st = _usage_file_totals(f)
                    if st is None:
                        continue
                    sessions += 1
                    for k in _USAGE_FIELDS:
                        agg[k] += st[k]
                    calls += st["calls"]
                    model = st["model"] or model
                if sessions:
                    per_code[d.name] = {**agg, "calls": calls, "sessions": sessions}
            # 任务轮转/清理后消失的文件：同步清缓存，防陈旧数据混入新周期
            for gone in [p for p in _usage_cache if p not in seen]:
                _usage_cache.pop(gone, None)

    rows = []
    fleet = {k: 0 for k in _USAGE_FIELDS}
    fleet_calls = fleet_sessions = 0
    for code, t in per_code.items():
        denom = t["cacheRead"] + t["input"]
        rows.append({
            "code": code,
            "sessions": t["sessions"],
            "calls": t["calls"],
            "input": t["input"],
            "output": t["output"],
            "cacheRead": t["cacheRead"],
            "cacheWrite": t["cacheWrite"],
            "totalTokens": t["input"] + t["output"] + t["cacheRead"] + t["cacheWrite"],
            "cacheHit": round(100.0 * t["cacheRead"] / denom, 1) if denom else 0.0,
        })
        for k in _USAGE_FIELDS:
            fleet[k] += t[k]
        fleet_calls += t["calls"]
        fleet_sessions += t["sessions"]
    rows.sort(key=lambda r: -r["totalTokens"])
    denom = fleet["cacheRead"] + fleet["input"]
    return {
        "summary": {
            **fleet,
            "calls": fleet_calls,
            "sessions": fleet_sessions,
            "totalTokens": fleet["input"] + fleet["output"] + fleet["cacheRead"] + fleet["cacheWrite"],
            "cacheHit": round(100.0 * fleet["cacheRead"] / denom, 1) if denom else 0.0,
            "model": model,
            "challenges": len(rows),
        },
        "challenges": rows,
    }


# ── 派单给舰队（网页「Agent 解此题」）───────────────────

def _platform_challenge(env: dict[str, str], code: str) -> dict[str, Any] | None:
    """轻量查询平台题目状态（供派单预检）。"""
    import urllib.error
    import urllib.request

    base = (env.get("BENCHMARK_BASE_URL") or "").rstrip("/")
    token = env.get("BENCHMARK_TOKEN", "")
    req = urllib.request.Request(
        base + "/openapi/v1/challenges",
        headers={"BENCHMARK_TOKEN": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return next((c for c in rows if c.get("unique_code") == code), None)


PRIORITY_FILE = PROJECT_ROOT / "work" / "priority.txt"
_assign_lock = threading.Lock()


def _next_worker() -> int:
    """轮转分配 worker（worker-1/2/3 → id 0/1/2）。"""
    with _assign_lock:
        try:
            count = PRIORITY_FILE.read_text(encoding="utf-8").count("\n") if PRIORITY_FILE.exists() else 0
        except OSError:
            count = 0
        return count % 3


def solve_one(code: str) -> dict[str, Any]:
    """网页「Agent 解此题」：把题派给 3-worker 舰队优先处理（不另起容器）。"""
    env = load_agent_env()
    if not env.get("BENCHMARK_TOKEN") or not env.get("BENCHMARK_BASE_URL"):
        raise APIError(400, "agent_env_missing", "请先在设置页配置 Agent 舰队")
    fleet = fleet_status()
    if fleet["summary"]["running"] < 1:
        raise APIError(409, "fleet_not_running", "舰队未运行，请先到「Agent 舰队」页点击「▶ 启动舰队」")
    row = _platform_challenge(env, code)
    if row is None:
        raise APIError(404, "challenge_not_found", f"题库中不存在 {code}")
    if row.get("is_completed"):
        raise APIError(409, "already_solved", f"{code} 已通关，无需派单")
    if row.get("container_status") == "available":
        raise APIError(409, "already_active", f"{code} 容器已就绪（舰队正在解），无需重复派单")

    wid = _next_worker()
    worker = WORKER_NAMES[wid]
    try:
        existing = PRIORITY_FILE.read_text(encoding="utf-8") if PRIORITY_FILE.exists() else ""
        if any(l.strip().split("|")[0] == code for l in existing.splitlines() if l.strip()):
            raise APIError(409, "already_queued", f"{code} 已在舰队优先队列中")
        PRIORITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PRIORITY_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{code}|{wid}\n")
    except APIError:
        raise
    except OSError as exc:
        raise APIError(500, "priority_write_failed", f"写入优先队列失败: {exc}") from exc

    return {
        "started": True,
        "container": worker,
        "status": "queued",
        "message": f"已派单给 {worker}：优先处理 {code}（舰队 worker 下一轮立即响应）",
    }


def single_status(code: str) -> dict[str, Any]:
    """派单任务状态：队列位置 + 平台题状态。"""
    queued = False
    if PRIORITY_FILE.exists():
        try:
            for l in PRIORITY_FILE.read_text(encoding="utf-8").splitlines():
                if l.strip() and l.strip().split("|")[0] == code:
                    queued = True
                    break
        except OSError:
            pass
    env = load_agent_env()
    row = _platform_challenge(env, code) if env else None
    return {
        "code": code,
        "queued": queued,
        "running": bool(row and row.get("container_status") == "available"),
        "completed": bool(row and row.get("is_completed")),
        "platform": row or {},
    }