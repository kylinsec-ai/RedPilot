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

ENV_KEYS = ("BENCHMARK_BASE_URL", "BENCHMARK_TOKEN", "SOLVER_API_KEY")


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


def save_agent_env(values: dict[str, str]) -> None:
    """将 Agent 配置写入 .agent.env（隐藏 Key）。"""
    current = load_agent_env()
    current.update({k: v for k, v in values.items() if k in ENV_KEYS})
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

    日志格式: FLAG CORRECT on d-01: flag{...} (+200 pts, total 200)
    """
    import re

    result = _run(["docker", "logs", "--tail", "5000", worker], timeout=30)
    if result.returncode != 0:
        return {}
    logs = result.stdout + result.stderr
    flags_found: list[str] = []
    total = 0
    solved_codes: set[str] = set()
    # 单遍扫描：一次匹配出 code + flag（原实现对每个 code 重复全量 re.search）
    for m in re.finditer(r"FLAG CORRECT on ([^\s:]+): ([^\s]+) \(([+-]?\d+) pts, total (\d+)\)", logs):
        code, flag = m.group(1), m.group(2)
        if flag not in flags_found:
            flags_found.append(flag)
        total = int(m.group(4))
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
            # 状态页高频轮询：只读文件尾部（~256KB，覆盖最后 1000+ 行），不整读全量
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024))
                tail = f.read().decode("utf-8", errors="ignore")
            for line in tail.splitlines()[-1000:]:
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
            if payload.get("correct"):
                summary["flags_submitted"] += 1
                summary["total_earned"] += int(payload.get("awarded", 0) or 0)
                flag = payload.get("flag") or ""
                if flag and flag not in summary["flags_found"]:
                    summary["flags_found"].append(flag)
                if code not in solved_codes:
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
    # 一次批量 inspect 三个容器（.Name 前缀区分）；缺失的容器不在输出中 → absent
    result = _run(["docker", "inspect", *WORKER_NAMES, "--format",
                   "{{.Name}}|{{.State.Status}}|{{.State.Health.Status}}|{{.State.ExitCode}}|{{.State.Running}}"],
                  timeout=30)
    inspected: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 5:
            inspected[parts[0].lstrip("/")] = line.strip()
    for name in WORKER_NAMES:
        row = inspected.get(name)
        if row is None:
            workers.append({"name": name, "container": "absent", "running": False,
                            "health": "", "exit_code": None, "state": {}})
            continue
        fields = row.split("|")[1:]
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
    """轮转事件日志与状态文件：备份旧文件，让统计只反映当前任务周期。"""
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    events = PROJECT_ROOT / "work" / "_events.jsonl"
    try:
        if events.exists() and events.stat().st_size > 0:
            events.rename(PROJECT_ROOT / "work" / f"_events.{stamp}.jsonl.bak")
    except OSError:
        pass
    try:
        for old in (WORK_STATUS_DIR.glob("worker-*.json") if WORK_STATUS_DIR.exists() else []):
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass
    # 兜底：事件文件即使为空也保证存在（driver obs 会追加）
    try:
        PROJECT_ROOT.joinpath("work").mkdir(parents=True, exist_ok=True)
        events.touch(exist_ok=True)
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


# ── 派单给舰队（网页「Agent 解此题」）───────────────────

def _platform_challenge(env: dict[str, str], code: str) -> dict[str, Any] | None:
    """轻量查询平台题目状态（供派单预检）。"""
    # 延迟导入：services 顶层依赖 agent，避免循环
    from .services import _remote_request

    base = (env.get("BENCHMARK_BASE_URL") or "").rstrip("/")
    token = env.get("BENCHMARK_TOKEN", "")
    try:
        rows = _remote_request(base, token, "/openapi/v1/challenges")
    except (APIError, ValueError):
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