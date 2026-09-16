"""Agent 舰队控制：通过 Docker Compose 启停/监控 tsecbench worker 容器。

worker 容器（tsecbench-worker-1/2/3）由 TsecBench-main/docker-compose.yaml 定义，
每个容器一个 Pi Agent，自动拉取平台题目解题并提交。
"""

from __future__ import annotations

import json
import threading
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tsecbench.errors import APIError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # TsecBench-main/
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yaml"
AGENT_ENV_FILE = PROJECT_ROOT / ".agent.env"
WORK_STATUS_DIR = PROJECT_ROOT / "work" / "status"
WORKER_NAMES = ["tsecbench-worker-1", "tsecbench-worker-2", "tsecbench-worker-3"]
# worker-1 (wid=0) 只提供 VPN/监控，不能接收任何解题派单。
_SOLVER_WIDS = (1, 2)
VPN_READY_FILE = PROJECT_ROOT / "work" / ".vpn-ready"
_FLAG_LIKE_RE = re.compile(r"flag\{[^}\r\n]+\}", re.IGNORECASE)
_FLEET_START_LOCK = threading.Lock()

ENV_KEYS = ("BENCHMARK_BASE_URL", "BENCHMARK_TOKEN", "SOLVER_API_KEY",
             "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
             "SOLVER_SESSION_SECONDS", "ADAPTER_PI_THINKING")


def _solver_worker_names() -> set[str]:
    """Resolve solver container names without assuming a full fleet list.

    Tests, staged rollouts, and partial Docker deployments can expose fewer
    than three names.  Indexing ``WORKER_NAMES`` with the canonical IDs then
    raises ``IndexError`` and makes the status endpoint unusable.  Prefer the
    configured IDs, fall back to conventional ``-2``/``-3`` suffixes, and
    finally treat every non-monitor entry as a solver.
    """
    names = {
        WORKER_NAMES[i] for i in _SOLVER_WIDS
        if 0 <= i < len(WORKER_NAMES)
    }
    names.update(
        name for name in WORKER_NAMES
        if re.search(r"-(?:2|3)$", str(name))
    )
    if not names and len(WORKER_NAMES) > 1:
        names.update(WORKER_NAMES[1:])
    return names


def _redact_flag_like_text(value: str) -> str:
    """Redact answer-shaped text before it reaches a control-plane response."""
    return _FLAG_LIKE_RE.sub("[REDACTED FLAG]", str(value or ""))


def _redact_worker_state(value: Any) -> Any:
    """Keep status useful without exposing worker-produced answer text."""
    if isinstance(value, str):
        return _redact_flag_like_text(value)
    if isinstance(value, list):
        return [_redact_worker_state(item) for item in value]
    if isinstance(value, dict):
        # A historical status format could contain discovered flags.  Drop the
        # values entirely; counters below remain available to the UI/API.
        return {
            key: _redact_worker_state(item)
            for key, item in value.items()
            if key != "flags_found"
        }
    return value


@contextmanager
def _priority_lock():
    """Serialize queue writes with the driver-side stale-entry pruning."""
    lock_path = PRIORITY_FILE.with_name(PRIORITY_FILE.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl = None
        locked = False
        try:
            import fcntl as _fcntl
            fcntl = _fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except (ImportError, OSError):
            # The deployed workers are Linux and use flock.  Keep the control
            # plane usable for development platforms where it is unavailable;
            # the driver has the same best-effort fallback.
            pass
        try:
            yield
        finally:
            if locked and fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass


def _priority_task_epoch() -> str:
    """Return the active answer-free task generation for a queued job."""
    try:
        raw = json.loads(
            (PRIORITY_FILE.parent / "status" / "task-epoch.json").read_text(
                encoding="utf-8"))
        if isinstance(raw, dict) and not raw.get("terminal"):
            return str(raw.get("epoch", "") or "")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return ""


def _parse_priority_record(line: str) -> tuple[str, int | None, str] | None:
    """Parse ``code|worker_id[|task_epoch]`` without rejecting old queues.

    The first two columns predate task epochs, so a missing third column is
    intentionally represented as an empty epoch.  Callers with an active
    epoch then reject it; callers serving an older driver can still read it.
    """
    stripped = str(line or "").strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split("|", 2)
    code = parts[0].strip()
    if not code:
        return None
    worker_id = None
    if len(parts) > 1 and parts[1].strip():
        try:
            worker_id = int(parts[1].strip())
        except ValueError:
            pass
    epoch = parts[2].strip() if len(parts) > 2 else ""
    return code, worker_id, epoch


def _priority_code_key(code: object) -> str:
    """Compare queue codes case-insensitively without rewriting the UI text."""
    return str(code or "").strip().casefold()


def _priority_is_current(record: tuple[str, int | None, str], epoch: str) -> bool:
    """Whether a queue record belongs to the active task generation."""
    return not epoch or record[2] == epoch


def _read_priority_lines() -> list[str]:
    """Read a complete queue snapshot; writers always replace atomically."""
    if not PRIORITY_FILE.exists():
        return []
    return PRIORITY_FILE.read_text(encoding="utf-8").splitlines()


def _next_worker_for_lines(lines: list[str], epoch: str) -> int:
    """Choose the least-loaded solver from one locked queue snapshot."""
    loads = {wid: 0 for wid in _SOLVER_WIDS}
    for line in lines:
        record = _parse_priority_record(line)
        if record is None or not _priority_is_current(record, epoch):
            continue
        assigned = record[1]
        if assigned in loads:
            loads[assigned] += 1
    return min(_SOLVER_WIDS, key=lambda wid: (loads[wid], wid))


def _vpn_ready() -> bool:
    """Whether worker-1 has positively marked the shared VPN as ready.

    Docker's healthcheck only proves the driver heartbeat.  The marker is
    maintained by the worker-1 supervisor and is removed on tunnel failure, so
    absence must be treated as not target-ready rather than as an unknown
    success.
    """
    try:
        marker = VPN_READY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not marker:
        return False
    try:
        payload = json.loads(marker)
    except json.JSONDecodeError:
        return marker.lower() not in {"0", "false", "down", "not_ready", "unready"}
    if not isinstance(payload, dict) or not payload.get("ready"):
        return False
    # A monitor can disappear while its last marker remains on the shared
    # volume.  Treat an old positive probe as unknown/not-ready instead of
    # advertising target reachability indefinitely.
    try:
        ts = float(payload.get("ts", 0) or 0)
    except (TypeError, ValueError):
        return False
    try:
        max_age = float(os.environ.get("VPN_READY_MAX_AGE", "180") or "180")
    except (TypeError, ValueError):
        max_age = 180.0
    return ts > 0 and (time.time() - ts) <= max(1.0, max_age)


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


# [B56] 容器名 → wid：**只认容器自己的 ADAPTER_WORKER_ID**，不再从名字后缀猜。
# compose 里 tsecbench-worker-1 是 wid 0（且 ADAPTER_ROLE=monitor），后缀猜法让
# 整块面板错位一格：w1 位置显示 w2 的进度、w2 显示 w3 的、w3 读不到文件只能
# 退回解析日志。驱动侧一直按 wid 命名（_status_path → status/worker-{wid}.json），
# 所以这里也必须按 wid 取。
_WID_FALLBACK = {"tsecbench-worker-1": 0, "tsecbench-worker-2": 1,
                 "tsecbench-worker-3": 2, "tsecbench-single": 9}


def _worker_wid(worker: str, env_blob: str = "") -> int:
    """取 worker 的 wid：优先容器 env 里的 ADAPTER_WORKER_ID，取不到退回静态表。

    env_blob 来自 fleet_status 已有的那次 `docker inspect`（.Config.Env），
    **不额外增加 docker 调用**。静态表只是容器没起来时的兜底，不作为权威。
    """
    for tok in (env_blob or "").split():
        if tok.startswith("ADAPTER_WORKER_ID="):
            raw = tok.split("=", 1)[1].strip()
            if raw.isdigit():
                return int(raw)
    return _WID_FALLBACK.get(worker, 9)


def _worker_status_file(worker: str, wid: int | None = None) -> Path:
    if wid is None:
        wid = _worker_wid(worker)
    return WORK_STATUS_DIR / f"worker-{wid}.json"


def _parse_worker_stats_from_logs(worker: str) -> dict[str, Any]:
    """Parse counter-only fallback stats from an old worker's Docker log.

    This compatibility path must not turn Docker logs into an answer API.  It
    deliberately counts matching confirmation lines without extracting or
    retaining the flag portion of those lines.
    """

    result = _run(["docker", "logs", "--tail", "5000", worker], timeout=30)
    if result.returncode != 0:
        return {}
    logs = result.stdout + result.stderr
    total = 0
    submitted = 0
    # 旧日志只有“某题出现过正确 flag”，没有题目总数，不能据此推断整题完成。
    # 因此仅用于展示提交/得分，整题 solved 由新版事件或状态文件提供。
    for m in re.finditer(r"FLAG CORRECT on [^\s:]+:.*?\(([+-]?\d+) pts, total (\d+)\)", logs):
        submitted += 1
        total = int(m.group(2))
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
        "flags_found_count": submitted,
        "flags_submitted": submitted,
        "total_earned": total,
        "challenges_solved": 0,
    }


def _read_worker_status(worker: str, wid: int | None = None) -> dict[str, Any]:
    path = _worker_status_file(worker, wid)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _current_task_epoch() -> str:
    """Return the active, answer-free task epoch recorded by the driver.

    The event stream is deliberately retained across automatic task changes so
    operators can inspect process liveness.  Its counters, however, must never
    cross a benchmark-run boundary.  The driver writes this small metadata file
    before it starts a solver visit; an absent or malformed file means an older
    driver is in use and keeps the legacy aggregation behaviour.
    """
    path = PROJECT_ROOT / "work" / "status" / "task-epoch.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    epoch = raw.get("epoch")
    return str(epoch).strip() if isinstance(epoch, str) else ""


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

    # A new task may reuse a challenge code, so code/worker alone cannot safely
    # scope counters.  When the modern driver has published a task epoch, only
    # that epoch contributes progress.  Lifecycle cleanup events emitted during
    # process bootstrap predate the epoch context; they remain eligible only to
    # close a matching in-memory session, never to affect counters.
    task_epoch = _current_task_epoch()

    # Keep progress counter-only.  Events may carry a one-way candidate hash
    # in new builds or a historical plaintext ``flag`` field; neither belongs
    # in a control-plane response.
    summary = {
        "total_earned": 0,
        "flags_submitted": 0,
        "flags_found": [],  # backward-compatible, intentionally always empty
        "flags_found_count": 0,
        "solved": 0,
    }
    # 同一道题可能被不同 worker 先后/并行访问；只用 code 做 key 会让
    # worker-A 的 session_end 误关闭 worker-B 的活动会话。新版事件带
    # worker_id+boot_id，旧事件则退回 unknown 标识。
    active: dict[tuple[str, str, str], float] = {}
    completed_codes: set[str] = set()
    # 新版 flag_submit 携带题目总数/已收数；只在确认收齐时标记完成。
    # 没有这些元数据的旧事件不再按“出现过正确 flag”推断整题完成，
    # 避免多 flag 题 1/N 被面板报成 solved。
    progress: dict[str, dict[str, int]] = {}

    def _count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    visible_events: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") or {}
        etype = event.get("event")
        event_epoch = str(event.get("task_epoch", "") or "")
        in_current_epoch = not task_epoch or event_epoch == task_epoch

        # ``run_start`` and synthetic ``session_end`` can be emitted before a
        # restarted driver has re-established its epoch context.  They carry no
        # score or answer data, and are needed to retire an old same-epoch
        # session.  All other legacy/different-epoch events are ignored.
        lifecycle_only = etype in {"run_start", "session_end"} and not event_epoch
        if not in_current_epoch and not lifecycle_only:
            continue
        if in_current_epoch:
            visible_events.append(event)
        worker = str(event.get("worker_id", "unknown"))
        boot = str(event.get("boot_id", "legacy"))
        code = str(payload.get("code") or "")
        active_key = (worker, boot, code)
        if etype == "session_start" and code:
            active[active_key] = event.get("ts", 0)
        elif etype == "run_start":
            # 新进程启动意味着同 worker 的旧 boot 已经不再运行；同时清掉
            # 没有 worker/boot 标识的 legacy 会话，避免面板把历史悬空会话继续显示为当前。
            active = {
                key: ts for key, ts in active.items()
                if key[0] not in {worker, "unknown"}
            }
        elif etype == "session_end":
            # 启动自愈会由当前进程代发旧会话的 synthetic 收尾；目标 worker/boot
            # 放在 payload 中，不能使用代发进程的顶层上下文去匹配。
            close_worker = str(payload.get("worker_id", worker))
            close_boot = str(payload.get("boot_id", boot))
            active.pop((close_worker, close_boot, code), None)
        elif etype == "flag_submit":
            code = payload.get("code")
            if payload.get("correct"):
                summary["flags_submitted"] += 1
                summary["flags_found_count"] += 1
                summary["total_earned"] += int(payload.get("awarded", 0) or 0)
                if code:
                    code = str(code)
                    item = progress.setdefault(
                        code, {"expected": 0, "total": 0, "correct": 0, "expected_seen": 0})
                    expected_value = _count(payload.get("expected_flag_count"))
                    item["expected"] = max(item["expected"], expected_value)
                    item["expected_seen"] = max(item["expected_seen"], int(expected_value > 0))
                    item["total"] = max(item["total"], _count(payload.get("total_flag_count")))
                    item["correct"] = max(item["correct"], _count(payload.get("correct_flag_count")))
                    expected = max(1, item["expected"])
                    total = max(expected, item["total"])
                    if item["total"] > 0:
                        if item["correct"] >= total:
                            completed_codes.add(code)
                    elif item["expected_seen"] and expected <= 1:
                        completed_codes.add(code)
        elif etype == "challenge_solved":
            code = payload.get("code")
            if code:
                completed_codes.add(code)
    # 只认明确的整题完成事件或带完整计数的提交事件，不把部分入账当作 solved。
    summary["solved"] = len(completed_codes)
    # 进行中的题目（最近 session_start 且尚未 session_end）
    current = [
        {"code": key[2], "since": ts, "worker_id": key[0], "boot_id": key[1]}
        for key, ts in sorted(active.items(), key=lambda kv: -kv[1])
    ][:5]
    last_events = [e.get("event", "") for e in visible_events[-5:]]
    return {
        "summary": summary,
        "current": current,
        "last_events": last_events,
        "task_epoch": task_epoch,
        "epoch_scoped": bool(task_epoch),
    }


def _read_api_fault() -> dict[str, Any] | None:
    """[B59] 账号级 API 故障标记（driver 熔断达上限时写 work/.api_fault）。

    文件由 driver 写、由 driver 在「有会话真正跑起来」时删除，控制台**只读**。
    读不到就是没有故障 —— 本地跑单题、容器没起来都不会有这个文件。
    陈旧文件也有意义：说明上次故障后 driver 还没跑出过一场成功会话。
    """
    try:
        lines = (PROJECT_ROOT / "work" / ".api_fault").read_text(
            encoding="utf-8").splitlines()
        return {"since": int(lines[0]),
                "pauses": int(lines[1]) if len(lines) > 1 and lines[1].strip() else 0}
    except (OSError, ValueError, IndexError):
        return None


def fleet_status() -> dict[str, Any]:
    """容器状态 + worker 状态文件 + 事件流聚合 + 单题定向容器列表。"""
    events = _aggregate_events()
    workers: list[dict[str, Any]] = []
    for name in WORKER_NAMES:
        status = _run(["docker", "inspect", name, "--format",
                       "{{.State.Status}}|{{.State.Health.Status}}|{{.State.ExitCode}}|{{.State.Running}}"
                       "|{{range .Config.Env}}{{.}} {{end}}"])
        if status.returncode != 0:
            workers.append({"name": name, "container": "absent", "running": False,
                            "health": "", "exit_code": None, "state": {}})
            continue
        fields = status.stdout.strip().split("|")
        running = fields[3] == "true"
        health = fields[1] if len(fields) > 1 and fields[1] else ("" if running else "")
        exit_code = int(fields[2]) if len(fields) > 2 and fields[2].isdigit() else None
        # [B56] wid 取自本次 inspect 已带回的 .Config.Env（fields[4]），
        # 不额外调 docker；容器名后缀不再被当作 wid。
        env_blob = "|".join(fields[4:]) if len(fields) > 4 else ""
        state = _read_worker_status(name, _worker_wid(name, env_blob))
        if running and not state:
            # 无状态文件（镜像内旧 driver）：从各自容器日志解析战绩
            state = _parse_worker_stats_from_logs(name)
        state = _redact_worker_state(state)
        workers.append({
            "name": name,
            "container": fields[0],
            "running": running,
            "health": health,
            "exit_code": exit_code,
            "state": state,
        })

    solver_names = _solver_worker_names()
    summary = {
        "total": len(workers),
        "running": sum(1 for w in workers if w["running"]),
        "healthy": sum(1 for w in workers if w["health"] == "healthy"),
        # worker-1 is a VPN/monitor provider and must never count as a solver
        # for a UI dispatch request.  Keep this separate from ``running`` so a
        # healthy monitor alone cannot make the queue look consumable.
        "solver_running": sum(
            1 for w in workers
            if w["name"] in solver_names
            and w["running"]
        ),
        "solver_healthy": sum(
            1 for w in workers
            if w["name"] in solver_names
            and w["health"] == "healthy"
        ),
        "flags_found": [],
        "flags_found_count": 0,
        "flags_submitted": 0,
        "total_earned": 0,
        "solved": 0,
        "current": [],
    }
    for worker in workers:
        state = worker.get("state") or {}
        summary["flags_submitted"] += int(state.get("flags_submitted", 0) or 0)
        # Status-file formats before the plaintext removal had only
        # ``flags_found``; do not read those values, use the submission count
        # as the conservative display counter instead.
        summary["flags_found_count"] += int(
            state.get("flags_found_count", state.get("flags_submitted", 0)) or 0
        )
        # [B56] total_earned 是**平台返回的账号级累计分**（三个 worker 共用同一个
        # BENCHMARK_TOKEN，各自在 last submit 时看到的是同一个数），跨 worker 相加
        # = 数倍虚高。取 max：最接近当前真实累计分。
        # challenges_solved 仍相加 —— 那是各 worker 自己数自己的，语义正确。
        summary["total_earned"] = max(summary["total_earned"],
                                      int(state.get("total_earned", 0) or 0))
        summary["solved"] += int(state.get("challenges_solved", 0) or 0)
        if state.get("current_code"):
            summary["current"].append(
                {"worker": worker["name"], "code": state["current_code"],
                 "round": state.get("current_round", 0), "event": state.get("last_event", "")}
            )
    # 事件流是本轮舰队计数的权威来源；状态文件只适合展示单个 worker 的当前题。
    # 旧实现只在所有状态文件缺失时才使用事件，导致面板在正常运行时把
    # flags_submitted/total_earned 覆盖成“当前题”的 1/300，而不是本轮 3/800。
    if events.get("epoch_scoped") or events["summary"]["flags_submitted"] > 0:
        summary["flags_submitted"] = events["summary"]["flags_submitted"]
        summary["total_earned"] = events["summary"]["total_earned"]
        summary["flags_found_count"] = events["summary"]["flags_found_count"]
        summary["solved"] = events["summary"]["solved"]

    # 状态文件缺失时以事件流兜底
    if not any(w.get("state") for w in workers):
        summary["flags_found_count"] = events["summary"]["flags_found_count"]
        summary["flags_submitted"] = events["summary"]["flags_submitted"]
        summary["total_earned"] = events["summary"]["total_earned"]
        summary["solved"] = events["summary"]["solved"]
        for item in events["current"]:
            summary["current"].append({"code": item["code"], "round": "进行中", "event": "session active"})
    # Deliberately leave the legacy list empty: callers should consume the
    # numeric ``flags_found_count`` / ``flags_submitted`` fields instead.
    summary["flags_found"] = []
    summary["fleet_events"] = events["last_events"]

    # 派单队列（供网页区分"派单中/自动解题"状态）
    priority = {}
    try:
        with _priority_lock():
            epoch = _priority_task_epoch()
            for line in _read_priority_lines():
                record = _parse_priority_record(line)
                if record is None or not _priority_is_current(record, epoch):
                    continue
                priority[record[0]] = record[1] if record[1] is not None else -1
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
            s = _redact_worker_state(_read_worker_status("tsecbench-single"))
            singles.append({
                "name": name,
                "code": code,
                "status": parts[1],
                "running": parts[2] == "true",
                "state": s,
            })
    return {"workers": workers, "summary": summary,
            "env_configured": bool(load_agent_env()), "singles": singles,
            "api_fault": _read_api_fault(),
            # ``healthy`` is only the driver heartbeat.  Consumers must check
            # this separately before treating a worker as able to reach targets.
            "vpn_ready": _vpn_ready()}


def _active_solver_containers() -> list[str]:
    """Return fleet containers which make a work-directory rotation unsafe.

    The historical name is kept because the helper is internal and already
    covered by callers/tests.  The monitor is deliberately included as well:
    during ``compose up`` it is normally the first container to run, while the
    solver containers are still waiting on its health check.  Treating that
    window as "no solver is active" let a second Start click erase the shared
    work tree underneath the first start.  A caller that wants a fresh task
    must stop the fleet first, so a conservative false positive is preferable
    to destroying an in-flight task.
    """
    active: list[str] = []
    for name in dict.fromkeys(WORKER_NAMES):
        result = _run(
            ["docker", "inspect", "--format",
             "{{.State.Running}}|{{.State.Restarting}}|{{.State.Status}}", name],
            timeout=30,
        )
        if result.returncode != 0:
            detail = f"{result.stdout}\n{result.stderr}".lower()
            if "no such object" in detail:
                continue
            raise APIError(503, "agent_state_unknown", f"无法确认 {name} 是否正在运行；为保护工作目录，未轮转")
        fields = [part.strip().lower() for part in result.stdout.strip().split("|", 2)]
        running, restarting, status = (fields + ["", "", ""])[:3]
        # Docker reports a brief ``created`` state while Compose is bringing a
        # dependency chain up.  It is part of an active start operation, not a
        # safe gap in which to rotate files.  ``paused`` is treated the same
        # way: it may resume and continue using its existing work tree.
        if (running == "true" or restarting == "true"
                or status in {"created", "restarting", "paused", "running"}):
            active.append(name)

    # A user-triggered single-challenge worker writes the same work tree, so it
    # receives the same protection even though it is not compose-managed.
    singles = _run(
        ["docker", "ps", "--filter", "name=tsecbench-single-", "--format", "{{.Names}}"],
        timeout=30,
    )
    if singles.returncode != 0:
        raise APIError(503, "agent_state_unknown", "无法确认单题 worker 是否正在运行；为保护工作目录，未轮转")
    active.extend(name.strip() for name in singles.stdout.splitlines() if name.strip())
    return active


def fleet_start() -> dict[str, Any]:
    if not AGENT_ENV_FILE.exists():
        raise APIError(400, "agent_env_missing", "请先在设置页配置 Agent 舰队（平台地址 / Token / SOLVER_API_KEY）")

    # Two simultaneous "start" clicks used to interleave rotation with a
    # compose up.  Serialize the state decision and refuse to touch work/ once
    # any fleet container can own it (including the monitor startup window).
    with _FLEET_START_LOCK:
        active = _active_solver_containers()
        if active:
            status = fleet_status()
            status["start_action"] = "already_running"
            status["active_solver_containers"] = active
            return status

        # No fleet container owns work/: it is now safe to start a genuinely new task
        # epoch and clear all task-scoped artifacts before Compose creates one.
        _rotate_stats()
        result = _run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "--env-file", str(AGENT_ENV_FILE), "up", "-d"],
            timeout=300,
            env=_compose_env(),
        )
        if result.returncode != 0:
            raise APIError(500, "agent_start_failed", f"启动失败: {result.stderr.strip()[-500:]}")
        status = fleet_status()
        status["start_action"] = "started"
        return status


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
    # A blackboard backup can retain evidence or an answer from an old task.
    # Never preserve it across an explicit task-epoch rotation.
    keep = {"status", "_events.jsonl", "monitor.log"}
    try:
        for child in work_dir.iterdir():
            if child.name in keep or child.name.startswith("_events."):
                continue
            if child.is_dir():
                import shutil
                shutil.rmtree(child, ignore_errors=True)
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

    # Keep the generation record but mark it terminal.  Deleting it would make
    # an identical later task start again at generation 1 and therefore reuse
    # the same epoch string.  ``fleet_start`` reaches here only after proving
    # no solver owns work/, so this metadata-only update cannot race a live
    # task.  The next driver activation will allocate a strictly newer epoch.
    epoch_state = work_dir / "status" / "task-epoch.json"
    try:
        raw = json.loads(epoch_state.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("task epoch metadata must be an object")
        raw["terminal"] = True
        raw["updated_at"] = time.time()
        tmp = epoch_state.with_name(f".{epoch_state.name}.rotate.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, epoch_state)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # Invalid metadata cannot safely identify a prior task.  It contains no
        # solver output, so removing it is the conservative way to force a new
        # epoch on the next activation.
        try:
            epoch_state.unlink(missing_ok=True)
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
    # Use exactly the same environment source as fleet_start().  Omitting
    # --env-file here made a stop action resolve the default .env instead of
    # the active console configuration, which is unsafe when the two belong
    # to different benchmark runs.
    result = _run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "--env-file", str(AGENT_ENV_FILE), "stop"],
        timeout=120,
        env=_compose_env(),
    )
    if result.returncode != 0:
        raise APIError(500, "agent_stop_failed", f"停止失败: {result.stderr.strip()[-500:]}")
    return fleet_status()


def worker_logs(worker: str, tail: int = 200) -> str:
    if worker not in WORKER_NAMES:
        raise APIError(400, "unknown_worker", "未知 worker")
    result = _run(["docker", "logs", "--tail", str(tail), worker], timeout=30)
    if result.returncode != 0:
        raise APIError(404, "worker_not_found", f"容器 {worker} 不存在或未运行")
    # Logs may contain tool output or historical driver lines with a flag.
    # They are useful for liveness/debugging, but must not become an answer
    # transport to the web control plane.
    return _redact_flag_like_text(result.stdout + result.stderr)

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
    """把派单分给两个解题 worker，优先选择当前队列较短的一方。

    wid=0 是 monitor；旧代码用 ``count % 3`` 轮转，导致每第三个派单写成
    ``code|0`` 后永久无人消费。这里不按容器名下标轮转，只在 wid 1/2 间均衡。
    """
    with _assign_lock:
        with _priority_lock():
            try:
                return _next_worker_for_lines(
                    _read_priority_lines(), _priority_task_epoch())
            except OSError:
                # Match the historical UI behaviour: a transient queue read
                # failure must not route work to monitor worker 0.
                return _SOLVER_WIDS[0]


def solve_one(code: str) -> dict[str, Any]:
    """网页「Agent 解此题」：把题派给 3-worker 舰队优先处理（不另起容器）。"""
    env = load_agent_env()
    if not env.get("BENCHMARK_TOKEN") or not env.get("BENCHMARK_BASE_URL"):
        raise APIError(400, "agent_env_missing", "请先在设置页配置 Agent 舰队")
    fleet = fleet_status()
    # The first worker is deliberately monitor-only.  Checking the aggregate
    # running count used to accept a dispatch while only worker-1 was alive;
    # the request was then queued forever because no solver consumed it.
    summary = fleet.get("summary") or {}
    solver_running = summary.get("solver_running")
    if solver_running is None:
        # Compatibility with lightweight callers/tests and an older console
        # response.  A real fleet_status response always includes workers and
        # the solver-specific count above.
        solver_running = sum(
            1 for worker in (fleet.get("workers") or [])
            if worker.get("name") in {WORKER_NAMES[i] for i in _SOLVER_WIDS}
            and worker.get("running")
        )
        if not fleet.get("workers"):
            solver_running = summary.get("running", 0)
    if int(solver_running or 0) < 1:
        raise APIError(409, "fleet_not_running", "舰队未运行，请先到「Agent 舰队」页点击「▶ 启动舰队」")
    row = _platform_challenge(env, code)
    if row is None:
        raise APIError(404, "challenge_not_found", f"题库中不存在 {code}")
    if row.get("is_completed"):
        raise APIError(409, "already_solved", f"{code} 已通关，无需派单")
    if row.get("container_status") == "available":
        raise APIError(409, "already_active", f"{code} 容器已就绪（舰队正在解），无需重复派单")

    try:
        # Worker selection and append must use one queue snapshot.  Calling
        # ``_next_worker`` before acquiring this lock lets two simultaneous UI
        # requests both see an empty queue and overfill worker 1.
        with _assign_lock:
            with _priority_lock():
                epoch = _priority_task_epoch()
                lines = _read_priority_lines()
                # Drop stale/bare entries when there is an active task epoch.
                # The driver performs the same filtering, but doing it here
                # keeps an old task's record from blocking a fresh request
                # before the next scheduler poll.
                retained = []
                for line in lines:
                    record = _parse_priority_record(line)
                    if record is not None and not _priority_is_current(record, epoch):
                        continue
                    retained.append(line)
                if any(record is not None
                       and _priority_code_key(record[0]) == _priority_code_key(code)
                       for record in (_parse_priority_record(line) for line in retained)):
                    raise APIError(409, "already_queued", f"{code} 已在舰队优先队列中")
                wid = _next_worker_for_lines(retained, epoch)
                retained.append(f"{code}|{wid}" + (f"|{epoch}" if epoch else ""))
                tmp = PRIORITY_FILE.with_name(f".{PRIORITY_FILE.name}.tmp")
                tmp.write_text("\n".join(retained) + "\n", encoding="utf-8")
                os.replace(tmp, PRIORITY_FILE)
    except APIError:
        raise
    except OSError as exc:
        raise APIError(500, "priority_write_failed", f"写入优先队列失败: {exc}") from exc

    worker = WORKER_NAMES[wid]
    return {
        "started": True,
        "container": worker,
        "status": "queued",
        "message": f"已派单给 {worker}：优先处理 {code}（舰队 worker 下一轮立即响应）",
    }


def single_status(code: str) -> dict[str, Any]:
    """派单任务状态：队列位置 + 平台题状态。"""
    env = load_agent_env()
    row = _platform_challenge(env, code) if env else None
    queued = False
    # A completed platform row is authoritative.  The driver will remove its
    # queue record on the next poll, but reporting it as queued in this small
    # interval makes the UI prioritize a task that cannot run any more.
    if not (row and row.get("is_completed")):
        try:
            with _priority_lock():
                epoch = _priority_task_epoch()
                for line in _read_priority_lines():
                    record = _parse_priority_record(line)
                    if record is not None and _priority_is_current(record, epoch) \
                            and _priority_code_key(record[0]) == _priority_code_key(code):
                        queued = True
                        break
        except OSError:
            pass
    return {
        "code": code,
        "queued": queued,
        "running": bool(row and row.get("container_status") == "available"),
        "completed": bool(row and row.get("is_completed")),
        "platform": row or {},
    }
