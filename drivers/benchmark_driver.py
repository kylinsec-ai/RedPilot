#!/usr/bin/env python3
"""
TsecBench 基准测试驱动器

主驱动：调度、长会话重访、声明式提交。

流程:
1. 从答题 API 拉取题目列表，按难度和分值排序
2. 每道题分配工作目录，写入工具清单和题目上下文
3. 启动 Pi Agent 子会话解题
4. 子会话确证 flag 后写入 FLAG 文件
5. 控制器读取 FLAG 文件，经验证后提交
6. 未解出的题目挂起，后续轮次以递增时间盒重访
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager

# 确保 adapter 包可导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ghost_worker.adapter.config import SolverConfig, ControllerConfig, build_verifier_config
from ghost_worker.adapter.progress import ChallengeProgress, extract_progress_from_result
from ghost_worker.adapter.task import AgentTask
from ghost_worker.adapter.verify import (Verifier, flag_confidence, flag_submission_key,
                            subagent_enabled,
                            flag_line_candidate, flag_evidence_policy,
                            is_task_remote_command, is_remote_provenance,
                            is_local_evidence_command,
                            is_remote_response_artifact_command,
                            downloaded_target_artifacts,
                            downloaded_target_response_artifacts,
                            derived_target_artifacts,
                            tainted_target_artifacts,
                            local_input_mutated,
                            authored_paths_from_call,
                            collect_script_bodies,
                            FlagEvidencePolicy)
from ghost_worker.adapter.solver import create_solver, extract_flags, extract_handoff, SolveResult
from ghost_worker.adapter.blackboard import Blackboard, goals_for_category
try:                                   # [B54] 监控缺失绝不能拖垮解题路径
    from ghost_worker.adapter import hallucination as _hallu
except Exception:                      # pragma: no cover
    _hallu = None
from ghost_worker.adapter.stoploss import StopLoss
from ghost_worker.adapter.scheduler import run_fleet
from ghost_worker.adapter.taskprompt import build_task_prompt, write_context_md, write_memory
from ghost_worker.adapter.platform_client import (PlatformClient, RateLimitedClient, Challenge,
                                     SubmitResult, InvalidState, DuplicateSubmit,
                                     ChallengeNotFound, ResourceUnavailable, VpnCheckError)
from ghost_worker.adapter import observability as obs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("adapter.driver")

# ── 全局状态 ──────────────────────────────────────────────

_MAX_ACTIVE_RETRIES = int(os.getenv("ADAPTER_MAX_ACTIVE_RETRIES", "8"))
_SHARED_BOARDS: dict = {}
_BOARDS_LOCK = threading.Lock()

# A task epoch is deliberately metadata-only.  It lets a long-lived worker
# distinguish a new benchmark run from a restarted process without retaining
# any prior challenge answer material.
_TASK_EPOCH = ""
_TASK_EPOCH_LOCK = threading.RLock()

# A continuation checkpoint is deliberately metadata-only.  It is separate
# from MEMORY/blackboard so a fresh platform instance can retain the fact that
# an earlier session already exhausted an attack surface without carrying
# answer text, command output, credentials, or target artifacts across the
# instance boundary.  The file is preserved only for the same task epoch.
_CONTINUATION_FILENAME = ".continuation.json"
_CONTINUATION_VERSION = 1

# ── 运行状态（Web 控制面板读取）─────────────────────────────
_STATUS: dict = {
    "worker_id": 0,
    "started_at": time.time(),
    "last_beat": time.time(),
    "current_code": "",
    "solving_active": False,
    "current_difficulty": "",
    "current_round": 0,
    "sessions": 0,
    "session_active": False,
    "session_started_at": 0.0,
    "last_activity": time.time(),
    "flags_found": [],
    "flags_submitted": 0,
    "total_earned": 0,
    "challenges_solved": 0,
    "last_event": "",
    "last_log": "",
}
_STATUS_LOCK = threading.Lock()

# 周期复活冷却基准在 stoploss 持久化状态里（revive() 打 last_revive_wall 戳）——
# 进程内存表重启即清零会让"刚 drop 的题"重启后立刻复活白拿新预算。


def _status_path() -> str:
    workdir = os.getenv("ADAPTER_WORKDIR", "/work")
    wid = os.getenv("ADAPTER_WORKER_ID", "")
    if not wid:
        host = os.getenv("HOSTNAME", "")
        m = re.search(r"-(\d+)$", host)
        wid = str(int(m.group(1)) - 1) if m else "0"
    return os.path.join(workdir, "status", f"worker-{wid}.json")


def _task_epoch_state_path(workdir: str | None = None) -> str:
    """Shared, answer-free task-generation state."""
    root = workdir or os.getenv("ADAPTER_WORKDIR", "/work")
    return os.path.join(root, "status", "task-epoch.json")


@contextmanager
def _advisory_lock(path: str):
    """Best-effort process lock for tiny JSON state files.

    All supported deployment targets are Linux, but keeping the fallback makes
    unit tests and non-Linux development harmless.  The lock contains no task
    output or candidate data; it only guards a rename transaction.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _challenge_lease_path(code: str, workdir: str | None = None) -> str:
    """Stable per-challenge lease path, outside disposable solver workdirs.

    A lease is intentionally only an ownership mutex: it contains no prompt,
    target, candidate, or answer data.  Keeping it under ``status`` means a
    new-instance cleanup cannot unlink the lock while another worker holds it.
    """
    root = workdir or os.getenv("ADAPTER_WORKDIR", "/work")
    digest = hashlib.sha256(str(code).encode("utf-8", "ignore")).hexdigest()[:32]
    return os.path.join(root, "status", ".challenge-leases", f"{digest}.lock")


def _try_acquire_challenge_lease(code: str, *, workdir: str | None = None):
    """Return a non-blocking cross-worker lease handle, or ``None`` if busy.

    Status files are useful for UI and diagnostics but are deliberately
    eventually consistent.  This small flock closes the remaining TOCTOU gap
    between two solvers both seeing an idle code and calling start_challenge.
    The kernel releases it automatically if a worker is killed.
    """
    path = _challenge_lease_path(code, workdir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError, BlockingIOError):
            handle.close()
            return None
        return handle
    except OSError:
        # A status-volume outage must not turn into a second independent
        # scheduler.  Treat it as busy and let the next polling cycle retry.
        return None


def _release_challenge_lease(handle) -> None:
    """Release a handle returned by :func:`_try_acquire_challenge_lease`."""
    if handle is None:
        return
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def _emit_challenge_lifecycle(event: str, code: str, **payload) -> None:
    """Record answer-free start/close telemetry for duplicate-operation audits."""
    try:
        obs.emit(event, layer="driver", payload={"code": str(code), **payload})
    except Exception:
        # Observability is never allowed to affect a live challenge lifecycle.
        pass


def _challenge_stable_identity(challenges) -> str:
    """Opaque digest of immutable-ish task metadata, never the raw prompt.

    A platform that exposes a task/run identifier keeps it as an attribute on
    its Challenge object.  We use it when available; otherwise description,
    tags and scoring metadata make code reuse detectable without persisting
    any challenge text.
    """
    rows = []
    identity_fields = (
        "task_id", "task_uuid", "benchmark_id", "run_id", "session_id",
        "round_id", "version", "created_at", "updated_at",
    )
    for ch in challenges or []:
        extra = {
            key: str(getattr(ch, key, "") or "")
            for key in identity_fields
            if getattr(ch, key, None) not in (None, "")
        }
        description = str(getattr(ch, "description", "") or "")
        rows.append({
            "code": str(getattr(ch, "unique_code", "") or ""),
            "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
            "category": str(getattr(ch, "category", "") or ""),
            "tags": sorted(str(t) for t in (getattr(ch, "tags", []) or [])),
            "difficulty": str(getattr(ch, "difficulty", "") or ""),
            "level": int(getattr(ch, "level", 0) or 0),
            "total_score": int(getattr(ch, "total_score", 0) or 0),
            "flag_count": int(getattr(ch, "flag_count", 0) or 0),
            "identity": extra,
        })
    # A benchmark token is a natural run boundary even when a platform reuses
    # an identical public challenge catalogue.  Persist only its one-way digest
    # so a token refresh cannot revive stop-loss/progress state from a prior
    # benchmark and no credential reaches the work volume.
    token = os.environ.get("BENCHMARK_TOKEN", "")
    token_scope = (hashlib.sha256(token.encode("utf-8")).hexdigest()
                   if token else "")
    raw = json.dumps({"token_scope_sha256": token_scope,
                      "challenges": sorted(rows, key=lambda row: row["code"])},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _challenge_runtime_snapshot(challenges) -> str:
    """Opaque mutable-state snapshot, used only to spot a reset to a new run."""
    rows = []
    for ch in challenges or []:
        rows.append((
            str(getattr(ch, "unique_code", "") or ""),
            bool(getattr(ch, "is_completed", False)),
            int(getattr(ch, "correct_flag_count", 0) or 0),
        ))
    raw = json.dumps(sorted(rows), separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _remove_retired_owner_state(workdir: str) -> None:
    """Delete retired answer-owner registries without opening their contents.

    Those files used to persist normalized flag bodies for cross-challenge
    filtering.  The architecture no longer permits cross-challenge answer
    reads, so retaining them is both unnecessary and a compliance liability.
    """
    status_dir = os.path.join(workdir, "status")
    try:
        names = os.listdir(status_dir)
    except OSError:
        return
    removed = 0
    for name in names:
        if not (name.startswith("owners-worker-") and name.endswith(".json")):
            continue
        try:
            os.remove(os.path.join(status_dir, name))
            removed += 1
        except OSError:
            pass
    if removed:
        log.info("[compliance] removed %d retired cross-challenge owner registry file(s)", removed)


def _activate_task_epoch(challenges, *, force_new: bool = False,
                         workdir: str | None = None) -> str:
    """Select the shared task epoch without clearing any live work directory.

    Actual per-challenge cleanup is deliberately lazy at the next safe
    ``solve_one`` boundary.  That avoids deleting artifacts from a worker that
    is still finishing the prior task while ensuring its stoploss/progress
    state cannot be reused once it starts the new one.
    """
    global _TASK_EPOCH
    root = workdir or os.getenv("ADAPTER_WORKDIR", "/work")
    state_path = _task_epoch_state_path(root)
    stable = _challenge_stable_identity(challenges)
    snapshot = _challenge_runtime_snapshot(challenges)
    lock_path = state_path + ".lock"
    with _advisory_lock(lock_path):
        old = {}
        try:
            with open(state_path, encoding="utf-8") as fh:
                old = json.load(fh) or {}
        except (OSError, ValueError, TypeError):
            old = {}
        old_generation = int(old.get("generation", 0) or 0)
        old_stable = str(old.get("stable_identity", "") or "")
        # Completion/progress values are deliberately excluded from the epoch
        # decision.  They change during one live task, and treating them as a
        # new generation discards retry/stoploss state every time another
        # worker completes a challenge.  ``force_new`` is only a caller hint:
        # a genuine same-identity reset must first mark the old epoch terminal
        # (or expose a different stable platform task identity), after which
        # this shared state transition is idempotent across both workers.
        rotate = (old_generation <= 0 or bool(old.get("terminal", False))
                  or old_stable != stable)
        generation = max(1, old_generation + 1) if rotate or old_generation <= 0 else old_generation
        epoch = f"t{generation}-{stable[:16]}"
        state = {
            "version": 1,
            "generation": generation,
            "epoch": epoch,
            "stable_identity": stable,
            "runtime_snapshot": snapshot,
            "terminal": False,
            "updated_at": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, state_path)
        except OSError:
            # A process-local epoch is still safer than falling back to legacy
            # event data if the shared status volume is temporarily unavailable.
            pass
    with _TASK_EPOCH_LOCK:
        previous_epoch = _TASK_EPOCH
        _TASK_EPOCH = epoch
    if previous_epoch and previous_epoch != epoch:
        # Blackboard objects are process-local caches.  Clear them at the same
        # generation boundary as the durable files, otherwise a reused code
        # would carry old network/service facts into the new task.
        with _BOARDS_LOCK:
            _SHARED_BOARDS.clear()
    try:
        obs.context(task_epoch=epoch)
    except Exception:
        pass
    _remove_retired_owner_state(root)
    return epoch


def _current_task_epoch() -> str:
    with _TASK_EPOCH_LOCK:
        return _TASK_EPOCH


def _mark_task_epoch_terminal(workdir: str | None = None) -> None:
    """Mark the current epoch terminal so an identical next task still rotates."""
    root = workdir or os.getenv("ADAPTER_WORKDIR", "/work")
    state_path = _task_epoch_state_path(root)
    with _advisory_lock(state_path + ".lock"):
        try:
            with open(state_path, encoding="utf-8") as fh:
                state = json.load(fh) or {}
        except (OSError, ValueError, TypeError):
            return
        if str(state.get("epoch", "")) != _current_task_epoch():
            return
        state["terminal"] = True
        state["updated_at"] = time.time()
        try:
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, state_path)
        except OSError:
            pass


def _other_worker_active_on(code: str, *, max_age: int = 150) -> bool:
    """另一解题目 worker 是否正在解本题（防止重启清理误杀他人活跃靶场）。

    两解题目 worker 能力重叠，同一题可能同时被两个 worker 接管。重启时若只按
    container_status=available 清理"遗留容器"，会把另一 worker 正在解的活跃目标
    关掉（实测曾把一个正在解某题的活跃靶场关掉，浪费整场会话）。这里读各
    worker 状态文件：只要某其他 worker 的 current_code 是本题且心跳新鲜，就视为
    有人正在解，跳过清理。
    """
    workdir = os.getenv("ADAPTER_WORKDIR", "/work")
    my = _status_path()
    try:
        for name in sorted(os.listdir(os.path.join(workdir, "status"))):
            if not name.endswith(".json"):
                continue
            p = os.path.join(workdir, "status", name)
            if os.path.abspath(p) == os.path.abspath(my):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            if (d.get("solving_active") is True and d.get("current_code") == code
                    and (time.time() - float(d.get("last_beat", 0)) < max_age)):
                return True
    except Exception:
        pass
    return False


def _heal_orphan_instances(client, *, exclude=None) -> list:
    """关闭无人认领的运行中靶场实例（孤儿槽位回收）。

    进程死于 visit 中段（外部强杀/SIGTERM 投递延迟期/OOM）会留下运行中的
    靶场实例；若重启后 shard 漂移导致该题不归任何 worker，就永远无人清理
    ——平台 3 个活跃槽位被占满后全队 start_challenge 409 堵死（实测
    某次两个孤儿实例把 worker-3 堵了 20 分钟，只能手工 close 解锁）。
    判据：container_status ∈ {pending, available} 且无其他 worker 正在解
    （_other_worker_active_on，150s 心跳新鲜度——活跃 worker 每 30s 刷
    last_beat，不会误杀）。exclude 保留正在 start 的本题主实例。
    返回关闭的 code 列表。
    """
    closed = []
    try:
        rows = client.list_challenges()
    except Exception as e:
        log.warning("[orphan-heal] list_challenges 失败: %s", str(e)[:120])
        return closed
    for c in rows:
        code = c.unique_code
        if c.container_status not in ("pending", "available"):
            continue
        if code == exclude:
            continue
        if _other_worker_active_on(code):
            continue
        # Status heartbeats are intentionally eventually consistent.  Take the
        # same per-challenge lease as solve_one before issuing a destructive
        # close, otherwise a second worker can start the target in the tiny
        # gap after the status check and have its live instance closed here.
        lease = _try_acquire_challenge_lease(code)
        if lease is None:
            log.debug("[orphan-heal] skip %s — lifecycle lease is held", code)
            continue
        try:
            # Re-check after acquiring the lease; a worker may have written a
            # fresh claim while this loop was waiting on the filesystem.
            if _other_worker_active_on(code):
                continue
            was_closed = _close_with_retry(client, code)
            _emit_challenge_lifecycle(
                "challenge_close", code, reason="orphan_heal",
                closed=bool(was_closed), task_epoch=_current_task_epoch())
            if was_closed:
                closed.append(code)
                log.info("[orphan-heal] 关闭孤儿实例 %s（无人认领，回收槽位）", code)
            else:
                log.warning("[orphan-heal] 关闭 %s 未获确认", code)
        finally:
            _release_challenge_lease(lease)
    return closed


def _other_solver_active_on(code: str, *, max_age: int = 150) -> bool:
    """另一「解题目」worker（wid 1/2）是否正在解此题——派发互斥守卫。

    与 _other_worker_active_on（容器清理保护）不同：这里只认解法 worker 的
    状态文件（worker-1.json / worker-2.json；manager 的 worker-0.json 不参与——
    它只派单/unknown，不占用解题）。防止两个解法 worker 并发写同一题 workdir /
    .pi-home 互相污染（MEMORY/models.json 互踩）并重复烧 token。
    自身状态文件按路径排除。
    """
    workdir = os.getenv("ADAPTER_WORKDIR", "/work")
    my = _status_path()
    base = os.path.dirname(my)
    try:
        for name in sorted(os.listdir(base)):
            if not name.endswith(".json") or name == "worker-0.json":
                continue  # worker-0 = manager，不参与解题
            p = os.path.join(base, name)
            if os.path.abspath(p) == os.path.abspath(my):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            # 只有"活跃求解"才算占用：空闲 worker 的 current_code 残留认领
            # 不应阻止另一 worker 接管（曾因双边互认对方活跃而双双绕行）。
            if (d.get("solving_active") is True and d.get("current_code") == code
                    and (time.time() - float(d.get("last_beat", 0)) < max_age)):
                return True
    except Exception:
        pass
    return False


# B19：认领复核前的等待（秒）——让并发的对手认领先落盘再判胜负。
_CLAIM_VERIFY_DELAY = float(os.environ.get("ADAPTER_CLAIM_VERIFY_DELAY", "0.5") or "0.5")


def _conflicting_claim(code: str, *, max_age: int = 150):
    """B19：并发认领检测 —— 返回对手的 (claim_ts, wid)，无冲突返回 None。

    与 _other_solver_active_on 同源（只看 wid1/2 的状态文件、只看心跳新鲜的
    活跃认领），但语义是"先认领、后复核"：调用时本 worker 已写入认领，因此
    任何仍活跃的他人认领都是并发竞争。按 (claim_ts, wid) 字典序定胜负，小者
    胜出 —— 两边独立计算得到同一结果，与各自复核时刻无关。
    claim_ts 缺失（旧代码进程）按 0 处理 = 视为更早，让行（安全侧）。
    """
    workdir = os.getenv("ADAPTER_WORKDIR", "/work")
    my = os.path.abspath(_status_path())
    base = os.path.dirname(my)
    try:
        for name in sorted(os.listdir(base)):
            if not name.endswith(".json") or name == "worker-0.json":
                continue  # worker-0 = manager，不参与解题
            p = os.path.join(base, name)
            if os.path.abspath(p) == my:
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            if (d.get("solving_active") is True and d.get("current_code") == code
                    and (time.time() - float(d.get("last_beat", 0)) < max_age)):
                return (float(d.get("claim_ts", 0) or 0),
                        int(d.get("worker_id", 0) or 0))
    except Exception:
        pass
    return None


def _update_status(**kw) -> None:
    with _STATUS_LOCK:
        _STATUS.update(kw)
        _STATUS["last_beat"] = time.time()
    try:
        p = _status_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STATUS, f, ensure_ascii=False)
        os.replace(tmp, p)  # 原子替换，防止其他 worker 读到半写 JSON
    except Exception:
        pass


def _adaptive_session_limits(ch: Challenge, solver: SolverConfig, session_idx: int,
                             last_facts: int, last_flags: int) -> tuple[int, int, str]:
    """按难度给单场 Pi 会话预算，避免 easy 题用 hard 题的长思考配额。

    Session 0（首次探索）限制为 5 分钟 / 20 turns，让 Agent 快速摸清题目
    基本情况。Session 0 结束后强制自评触发，Agent 可以决定是否派子 Agent。
    Session 1+ 恢复正常预算，带着自评结论和子 Agent 深入攻坚。

    多 flag 题保持连续会话（不拆分），避免丢失渗透链上下文。
    """
    if os.environ.get("ADAPTER_ADAPTIVE_SESSION", "1") == "0":
        return solver.max_turns, solver.session_seconds, "fixed"

    # 多 flag 题依赖连续上下文，保留原预算；拿到部分 flag 后也不能因效率闸门
    # 截断剩余 flag，只能在同一时间盒内换攻击面。
    if int(getattr(ch, "flag_count", 1) or 1) > 1:
        return solver.max_turns, solver.session_seconds, "multiflag-continuous"

    # Session 0：快速探索（5 分钟 / 20 turns），然后强制自评 + 决定是否叫人
    _FIRST_SESSION_SECS = int(os.environ.get("ADAPTER_FIRST_SESSION_SECS", "300") or "300")
    _FIRST_SESSION_TURNS = int(os.environ.get("ADAPTER_FIRST_SESSION_TURNS", "20") or "20")
    if session_idx == 0:
        return _FIRST_SESSION_TURNS, _FIRST_SESSION_SECS, "first-session-quick-explore"

    diff = (getattr(ch, "difficulty", "") or "").strip().lower()
    if diff == "easy":
        default_turns, default_secs = 36, 900
    elif diff == "hard":
        default_turns, default_secs = 60, solver.session_seconds
    else:
        default_turns, default_secs = 48, 1200

    def _env_int(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name, str(default)) or default))
        except (TypeError, ValueError):
            return default

    default_turns = _env_int(f"ADAPTER_MAX_TURNS_{diff.upper() or 'MEDIUM'}", default_turns)
    default_secs = _env_int(f"ADAPTER_SESSION_SECONDS_{diff.upper() or 'MEDIUM'}", default_secs)
    # 仅在上一场留下了新事实时放宽一档；没有事实的复访保持短场，避免连续
    # 复现同一条无效路线。不得超过全局 solver 上限。
    if session_idx > 0 and last_facts > 0:
        default_turns += 8
        default_secs += 300
    max_turns = min(solver.max_turns, default_turns)
    session_secs = min(solver.session_seconds, default_secs)
    note = f"adaptive diff={diff or 'medium'} turns={max_turns} secs={session_secs}"
    if session_idx > 0 and last_facts <= 0:
        note += " pivot-after-no-progress"
    elif last_flags > 0:
        note += " progress-extended"
    return max_turns, session_secs, note


def _multiflag_session_turn_limit(solver: SolverConfig) -> int:
    """Return the per-session tool-call allowance for a multi-flag chain.

    A multi-stage intrusion keeps its target instance, work directory and
    progress receipt alive for the whole visit.  Leaving it on the ordinary
    60-call ceiling silently cut that "continuous" chain into many new Pi
    processes, even while the visit itself still had ample time left.  That is
    particularly costly after the first accepted flag: the next process has
    to reread the handoff before it can resume lateral movement.

    The setting is deliberately a *floor*, not a replacement for an operator's
    larger global ``SOLVER_MAX_TURNS``.  It is only used for ``flag_count > 1``;
    ordinary single-flag tasks retain their adaptive, shorter sessions.
    """
    try:
        baseline = max(1, int(getattr(solver, "max_turns", 60) or 60))
    except (TypeError, ValueError):
        baseline = 60
    try:
        requested = int(os.environ.get("ADAPTER_MULTIFLAG_MAX_TURNS", "120") or "120")
    except (TypeError, ValueError):
        requested = 120
    return max(baseline, max(1, requested))


def _multiflag_hint_review_enabled() -> bool:
    """Return whether the one-shot platform hint review is enabled.

    Multi-stage tasks default to a single hint *after* the dry-window has been
    exhausted.  This is deliberately separate from ``ADAPTER_USE_HINTS``:
    that legacy switch controls optional hints for ordinary tasks, whereas the
    multi-stage state machine needs an explicit final review before abandoning
    a partially explored intranet chain.  Operators can disable the review for
    a platform that has no hint endpoint with ``...=0``; the driver still marks
    the review step so it will not spin on the same challenge.
    """
    return os.environ.get("ADAPTER_MULTIFLAG_HINT_REVIEW", "1") == "1"


def _request_multiflag_hint(client, code: str) -> tuple[bool, str]:
    """Fetch one platform hint without logging or persisting its contents.

    The returned text is kept only in the active ``solve_one`` context and is
    passed to the next Pi prompt as a lead, never as flag evidence.  A successful
    empty response still counts as a completed review; transport/API failures
    return ``False`` so callers can decide whether to retry or continue.
    """
    try:
        response = client.get_hint(code)
    except Exception as exc:  # noqa: BLE001 - a hint must never kill solving
        log.warning("  %s platform hint review failed: %s", code, str(exc)[:120])
        return False, ""
    hint = str(getattr(response, "hint", "") or "").strip()
    # Keep prompt growth bounded.  Do not include the value in logs/events: a
    # platform hint can contain challenge-specific text or even a flag-shaped
    # decoy, neither of which belongs in shared telemetry.
    return True, hint[:4000]


def _multiflag_hint_prompt_note(
    *,
    hint_reviewed: bool,
    hint_text: str,
    hint_review_session: int | None,
    session_idx: int,
    last_session_facts: int,
    last_session_repeats: int,
) -> str:
    """Build a truthful, answer-free steering note for a multi-flag session.

    Hint bodies intentionally live only in the current process.  A later
    visit therefore knows that the one permitted review already happened, but
    does *not* know its contents.  Do not fetch again (the platform may charge
    for hints) and do not imply that Pi has seen it: tell the solver to review
    remaining target-backed attack surfaces instead.  This helper only handles
    control-flow metadata; it never accepts or returns a hint body.
    """
    if hint_review_session == session_idx and bool(hint_text):
        return (
            "平台提示复核已完成；本场是提示后的完整复核窗口，先验证提示指向的目标证据，"
            "仍有新主机/服务/凭据就继续当前内网链；只有连续无新事实才可评估是否切题。"
        )
    if hint_reviewed and not hint_text:
        return (
            "此前已完成一次平台提示复核，但提示正文没有保存在状态中，当前进程不可用。"
            "不得猜测提示内容；请仅依据靶标当前证据，从尚未验证的攻击面重新复核。"
            "发现新主机、服务、凭据或权限链就继续当前内网题；只有连续无新事实才可评估切题。"
        )
    if session_idx > 0 and last_session_facts <= 0:
        return "上一场没有新增黑板事实；本场必须换攻击面，禁止原样重跑上一场命令。"
    if session_idx > 0 and last_session_repeats >= 3:
        return "上一场命令重复较多；优先选择尚未验证的路径。"
    return ""


def _shared_board_for(code: str, workdir: str) -> Blackboard:
    with _BOARDS_LOCK:
        b = _SHARED_BOARDS.get(code)
        if b is None:
            b = Blackboard(os.path.join(workdir, "_blackboard.json"))
            _SHARED_BOARDS[code] = b
        return b


def _safe_code(code: str) -> str:
    """将 challenge code 转为安全的目录名"""
    raw = str(code)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
    return safe if safe == raw else f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


def _difficulty_rank(d: str) -> int:
    return {"easy": 0, "medium": 1, "hard": 2}.get((d or "").lower(), 1)


# ── 题目类型推断 + 能力划分（worker-2/3 分派）──────────────

# 描述关键词 → 类型。分类只采用平台提供的元数据或题面文本，绝不依赖
# unique_code、历史题号或任何评测集专属命名规则。
_CATEGORY_KEYWORDS = [
    # cloud
    ("cloud", ["s3", "aws", "lambda", "bucket", "对象存储", "云", "oss", "密钥泄露", "cloud", "ec2", "iam"]),
    # web
    ("web", ["web", "http", "门户", "面板", "站点", "api", "网关", "网站", "前台", "后台",
             "注入", "xss", "upload", "上传", "sql", "越权", "响应", "response", "客户反馈",
             "审批", "检索引擎", "search", "代理"]),
    # pwn
    ("pwn", ["pwn", "栈溢出", "堆溢出", "格式化字符串", "buffer", "overflow", "二进制",
             "shellcode", "rop", "提权到root", "沙箱逃逸", "uaf"]),
    # reverse
    ("reverse", ["逆向", "reverse", "反编译", "脱壳", "unpack", "apk", "so文件", "混淆"]),
    # forensics
    ("forensics", ["取证", "forensic", "流量", "pcap", "内存", "磁盘", "隐写", "stego",
                   "volatility", "tshark", "文件恢复"]),
    # crypto
    # ⚠ 'des' 与 'hash' 这类**短英文词**必须写成 `\b...\b`：裸子串会命中普通
    #   英文词（'des' ⊂ "description"/"codes"/"modes"，'hash' ⊂ "hashmap"），
    #   而分类结果决定 flag 证据边界（crypto 属 _LOCAL_NATIVE_CATEGORIES，会
    #   打开"本地静态产物算证据"这道门，见 adapter/verify.py 的
    #   flag_evidence_policy）。实测 `"some totally unknown description here"`
    #   曾被判成 crypto —— 一个常见题面词就把边界从 remote-only 放宽了。
    #   反之**长词一律保持子串**（"forensic" 要能命中 "forensics"、"upload" 要能
    #   命中 "uploads"），所以是逐词 opt-in，不是全表词边界：整表加边界会丢
    #   复数/派生形式，实测把 "forensics challenge" 判成 unknown。
    #   边界语义见 _keyword_hits（不是正则 \b —— 它在 CJK 相邻处不成立）。
    ("crypto", [r"\bdes\b", "aes", "rsa", "加密", "解密", "哈希", r"\bhash\b", "cipher",
                "密码学", "crypto", "编码", "base64", "签名"]),
    # pentest
    ("pentest", ["渗透", "内网", "横向", "提权", "跳板", "隧道", "pivot", "lateral",
                 "privilege", "多阶段"]),
    # evasion
    ("evasion", ["对抗", "规避", "waf", "免杀", "bypass", "evasion", "绕waf", "检测规避",
                 "杀软", "edr"]),
]

# 已知分类词汇表（关键词表 + 兜底分类）；用于判断平台显式分类是否可信
_KNOWN_CATEGORIES = {cat for cat, _kw in _CATEGORY_KEYWORDS} | {"misc", "unknown"}


# 只对显式标注的短词做词边界匹配 —— opt-in 而非全表，理由见 _keyword_hits。
_ASCII_WORD = re.compile(r"[A-Za-z0-9]")
_WORD_BOUNDED = re.compile(r"\\b(.+)\\b\Z")


def _boundary_hits(word: str, desc: str) -> bool:
    """`word` 是否以独立的拉丁词出现（两侧都不是 [A-Za-z0-9]）。

    不用正则 `\\b`：`\\b` 的边界只认 \\w 而 CJK 也属 \\w，于是 "des加密" 会被
    判成无边界、漏掉真实的 DES 信号。用 str.find 逐次定位（C 速度）而不是
    编译正则：本函数按题调用、每次都要扫全表（实测 find 1.1µs vs 正则 2.7µs）。
    """
    n = len(word)
    start = 0
    while (i := desc.find(word, start)) != -1:
        j = i + n
        if (i == 0 or not _ASCII_WORD.match(desc[i - 1])) and \
           (j >= len(desc) or not _ASCII_WORD.match(desc[j])):
            return True
        start = i + 1
    return False


def _keyword_hits(keyword: str, desc: str) -> bool:
    """关键词命中判定：`\\bxxx\\b` 形式走词边界，其余按子串（保持原行为）。

    边界只对**显式标注**的短词启用（opt-in）。不改成"全表词边界"：长词加边界
    会丢复数/派生形式（"upload" 命中不了 "uploads"、"forensic" 命中不了
    "forensics"），实测能把 "uploads are allowed" 判成 unknown —— 那是拿
    分类准确率换整齐划一，而真正被利用的只是几个 ⊂ 普通英文词的短串。

    标注与否的依据见 _CATEGORY_KEYWORDS 里 crypto 一行：'des' ⊂ "description"
    会命中普通英文词，而分类结果决定 flag 证据边界（crypto 属
    _LOCAL_NATIVE_CATEGORIES，会打开本地取证门）。
    """
    m = _WORD_BOUNDED.match(keyword)
    if m:
        return _boundary_hits(m.group(1).lower(), desc)
    return keyword.lower() in desc


def _infer_category(ch: Challenge) -> str:
    """推断题目类型 —— 全动态，不硬编码任何赛题/项目代号。

    优先级:
      1) 平台显式分类（category/type/challenge_type；已知分类直接采用，
         未知分类让位给后续推断，避免把新分类误判成旧词汇）
      2) 描述关键词命中
      3) 均未命中 → unknown（按哈希均匀分流到各解题目 worker，见 _unknown_bucket）

    返回: web / cloud / pwn / reverse / forensics / crypto / pentest / evasion / misc / unknown
    """
    # 1) 平台显式分类（动态数据源，最高优先级）
    if getattr(ch, "category", ""):
        cat = ch.category.strip().lower()
        if cat in _KNOWN_CATEGORIES:
            return cat

    desc = (ch.description or "").lower()

    # 2) 描述关键词优先（命中口径统一在 _keyword_hits：拉丁词看词边界，中文看子串）
    for cat, keywords in _CATEGORY_KEYWORDS:
        if any(_keyword_hits(k, desc) for k in keywords):
            return cat

    return "unknown"


def _load_capabilities() -> set:
    """读取本 worker 的能力集合。

    - ADAPTER_CAPABILITIES: 主能力（如 web,cloud,exploit —— worker 主职）
    - ADAPTER_EXTRA_CAPABILITIES: 兜底能力（全自动派发时把无人认领的类型也接走，
      如 crypto,reverse,forensics,misc,unknown），不覆盖主职分工
    """
    caps: set = set()
    for var in ("ADAPTER_CAPABILITIES", "ADAPTER_EXTRA_CAPABILITIES"):
        raw = os.environ.get(var, "").strip()
        if raw:
            caps.update(x.strip().lower() for x in raw.split(",") if x.strip())
    return caps


def _unknown_bucket(code: str, wid: int, count: int) -> bool:
    """unknown 分流：把无法分类的题均匀分配到各解题目 worker 上。

    用 zlib.crc32（跨进程确定，不像内置 hash() 每进程随机）对 unique_code
    取模；wid 0 = 协调者/monitor（不解题）不参与兜底。这样任意新任务的
    新题名/新项目名都能被多个 worker 动态分摊，而不是全部堆到一个「兜底」worker。
    """
    solver_count = max(1, count - 1)   # 减掉 wid 0 协调者
    if wid < 1:
        return False                   # 协调者不兜底
    return (wid - 1) == (zlib.crc32((code or "").encode("utf-8")) % solver_count)


def _capability_filter(challenges: list) -> list:
    """按能力集合过滤题目：只保留本 worker 能处理的类型。

    - 已知分类：命中本 worker 能力集合才保留。
    - unknown（平台/关键词/前缀都无法确定）：按 unique_code 哈希均匀分流
      到各解题目 worker，保证新题/新项目名也有归属，不再全压 worker-2。
    """
    caps = _load_capabilities()
    if not caps:
        return challenges  # 未配置能力 → 全部处理（兼容旧行为）
    wid = _worker_id()
    count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
    kept = []
    for c in challenges:
        cat = _infer_category(c)
        if cat in caps:
            kept.append(c)
            continue
        if cat == "unknown" and _unknown_bucket(c.unique_code, wid, count):
            kept.append(c)
    return kept


def _prioritize(challenges: list[Challenge]) -> list[Challenge]:
    """
    派发顺序（框架策略：快分优先，渗透沉底）：

    1. 单 flag 题全部在前 —— 快进快出，先在任务时限内收割容易的分
    2. 多 flag 渗透题全部沉底 —— 需要数小时持续攻坚（持续会话），放最后
       即使时间耗尽，也已把单 flag 分数全部拿到

    组内排序：**分值升序（低分优先，B48）** → 同分再按难度 easy→hard
    """
    pending = [c for c in challenges if not c.is_completed]
    # [B48] 低分优先。实测依据：本轮 37/37 道解出来的题，末次访问→出分的中位数
    # 1.7 分钟、最长 7.8 分钟，与分值/难度都无关 ——「贵题更难」不成立，而「先做
    # 便宜的不吃亏」成立。轮次时限一到，队尾的题整批吃不到访问（本场 14 道题上过
    # 靶场却一次没提交）。多 flag 渗透题仍沉底：那类需要数小时持续攻坚，不适用。
    return sorted(pending, key=lambda c: (
        1 if int(c.flag_count or 1) > 1 else 0,   # 多 flag 渗透题沉底（最高优先级键）
        int(c.total_score or 0),                   # [B48] 分值升序：低分优先
        _difficulty_rank(c.difficulty),            # 同分再按 easy=0 → hard=2
    ))


def _rejected_flags_path(workdir: str) -> str:
    """跨会话错误账本：已被平台判错的 flag body（normalized），每行一个。

    位置在 challenge workdir（bind-mount 共享卷内）→ 跨会话/跨轮/跨 worker/跨重启
    一致：同一错误 body 永不再提交。真实 flag 不会入账（被平台判错的才记）。
    """
    return os.path.join(workdir, ".rejected_flags")


def _load_rejected_flags(workdir: str) -> set:
    """读取平台明确判错的精确候选键。

    旧版账本记录的是 lower-case body；新键包含完整候选，因此旧条目不会阻挡
    大小写不同的真实提交值。这是刻意的安全迁移：宁可重新验一次旧候选，也不能
    因为历史错误提交而漏掉真值。
    """
    p = _rejected_flags_path(workdir)
    try:
        with open(p, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def _append_ledger_once(path: str, value: str) -> bool:
    """Append one exact ledger key under a process lock, returning if it was new.

    The eager watcher and the post-session submission path can reject the same
    candidate almost concurrently.  Append-only files still need a read/check/
    append critical section; otherwise duplicate rows inflate prompt noise and
    repeatedly spend verifier work in successor sessions.
    """
    key = str(value or "").strip()
    if not key:
        return False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with _advisory_lock(path + ".lock"):
            try:
                with open(path, encoding="utf-8") as fh:
                    if any(line.strip() == key for line in fh):
                        return False
            except OSError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(key + "\n")
            return True
    except OSError:
        return False


def _drop_rejected_from_flag_files(workdir: str) -> None:
    """把已入账的判错候选从 FLAG/flag.txt/FLAG.txt 里摘除（B27）。

    FLAG 是框架的提交输入槽：判错的条目留在里面，下一场会话一进门就会读到一条
    **框架自己已知是错误的**候选，并可能据此判定本题已完成而提前收工。
    摘空后连 SOURCE 一起删（SOURCE 是为被摘掉的那条候选作证的）。
    多 flag 题里已判对的条目不在账本内，原样保留。
    """
    rejected = _load_rejected_flags(workdir)
    if not rejected:
        return
    for name in ("FLAG", "flag.txt", "FLAG.txt"):
        fp = os.path.join(workdir, name)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                lines = [ln.rstrip("\n") for ln in f]
        except OSError:
            continue
        filled = [ln for ln in lines if ln.strip()]
        keep = [ln for ln in filled
                if _normalize_flag_body(ln.strip()) not in rejected]
        if len(keep) == len(filled):
            continue
        try:
            if keep:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write("\n".join(keep) + "\n")
                log.info("  [B27] 从 %s 摘除 %d 条已判错候选（保留 %d 条）",
                         name, len(filled) - len(keep), len(keep))
            else:
                os.remove(fp)
                log.info("  [B27] %s 只剩已判错候选，已删除", name)
                sp = os.path.join(workdir, "SOURCE")
                if os.path.isfile(sp):
                    try:
                        os.remove(sp)
                        log.info("  [B27] 一并删除 SOURCE（为已删候选作证）")
                    except OSError:
                        pass
        except OSError:
            pass


def _add_rejected_flag(workdir: str, flag_candidate: str, *,
                       prune_delivery: bool = True) -> None:
    """把被平台判错的 flag 记入账本（幂等），并把它从 FLAG 交付文件里摘掉。

    B27：判错后只记账、FLAG 原样保留，会让后续会话一进门就读到一条框架已知
    错误的候选并当成本题答案。这里在收口点统一摘除 —— 三个判错调用路径都过
    _add_rejected_flag，一处即可全覆盖。
    """
    body = _normalize_flag_body(flag_candidate)
    if not body or len(body) < 2:
        return
    _append_ledger_once(_rejected_flags_path(workdir), body)
    # The eager watcher runs while Pi may append a later stage.  Rewriting a
    # live delivery queue can erase that append, so queue cleanup is deferred
    # to the post-session boundary in that path.
    if prune_delivery:
        _drop_rejected_from_flag_files(workdir)


def _unverified_flags_path(workdir: str) -> str:
    """跨会话「未通过验证」账本：被闸门拒收、且从未上平台的 flag body（每行一个）。

    与 .rejected_flags 的语义差别是根本性的：那本记的是**平台判错**（确凿为假，
    可以对 agent 说「不要再提交」）；这本记的是**框架没能坐实**（缺「活靶标
    响应」证据而放弃提交）—— 后者里混着真 flag（影子审计实测 19/29 被拒候选
    实为正确答案，靠强提救回）。因此本账本只允许用于两件事：
      1. 把该候选从 FLAG 交付槽摘掉，别让下一场一进门就判「本题已解」；
      2. 在下一场 prompt 里以 sha1 指纹告知「尚未坐实」，措辞不得写成「判错」。
    """
    return os.path.join(workdir, ".unverified_flags")


def _load_unverified_flags(workdir: str) -> set:
    """读取未验证候选的精确键（不把大小写不同的候选混为一条）。"""
    p = _unverified_flags_path(workdir)
    try:
        with open(p, encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def _drop_candidate_from_flag_files(workdir: str, flag_candidate: str) -> None:
    """把**指定一条**候选从 FLAG/flag.txt/FLAG.txt 摘除（B45，逐条粒度）。

    与 _drop_rejected_from_flag_files 的整批摘除不同，这里只摘刚被放弃的那一条：
    未验证账本是累积的，整批摘会把 agent 后来**重新坐实并写回**的同 body 候选
    一并抹掉。摘空时同样连带删 SOURCE（SOURCE 是为被摘候选作证的）。
    """
    body = _normalize_flag_body(flag_candidate)
    if not body:
        return
    for name in ("FLAG", "flag.txt", "FLAG.txt"):
        fp = os.path.join(workdir, name)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                lines = [ln.rstrip("\n") for ln in f]
        except OSError:
            continue
        filled = [ln for ln in lines if ln.strip()]
        keep = [ln for ln in filled
                if _normalize_flag_body(ln.strip()) != body]
        if len(keep) == len(filled):
            continue
        try:
            if keep:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write("\n".join(keep) + "\n")
                log.info("  [B45] 从 %s 摘除 1 条未验证候选（保留 %d 条）",
                         name, len(keep))
            else:
                os.remove(fp)
                log.info("  [B45] %s 只剩未验证候选，已删除", name)
                sp = os.path.join(workdir, "SOURCE")
                if os.path.isfile(sp):
                    try:
                        os.remove(sp)
                        log.info("  [B45] 一并删除 SOURCE（为已删候选作证）")
                    except OSError:
                        pass
        except OSError:
            pass


def _add_unverified_flag(workdir: str, flag_candidate: str, reason: str = "", *,
                         prune_delivery: bool = True) -> None:
    """把「被闸门拒收、从未上平台」的候选记入未验证账本并摘出 FLAG（B45）。

    重复调用无害（账本读取侧按集合去重）；每次都会尝试摘一次 FLAG，保证
    「框架已知没坐实的候选」不会留在交付槽里冒充「本题已解」的证据。

    [B54] `reason` = verify 的 reject_reason。本函数是**所有**「被闸门拒收、
    从未上平台」路径的唯一收口点，在这里记一笔幻觉账即自动全覆盖（eager 拒收 /
    本地静态产物否决 / 主循环拒收），不必逐个改调用点。
    不传 reason 时只记账本、**不计幻觉** —— 归类不明的候选绝不参与阈值。
    注意 B47 判断 Agent 的 3 个 veto 分支**故意不传 reason**：走到 veto 说明
    候选疑似真解、只是裁判不敢担保，记成幻觉是方向性错误。
    """
    body = _normalize_flag_body(flag_candidate)
    if not body or len(body) < 2:
        return
    p = _unverified_flags_path(workdir)
    _append_ledger_once(p, body)
    if _hallu is not None and reason:            # [B54] 幻觉族/推导族分族记账
        try:
            # The hallucination module deduplicates by (family, body).  Its
            # JSON read-modify-write still needs serialization across eager
            # and main-path callers so concurrent rejects cannot lose marks.
            with _advisory_lock(p + ".hallucination.lock"):
                _hallu.record(workdir, reason, body)
        except Exception:
            pass
    if prune_delivery:
        _drop_candidate_from_flag_files(workdir, flag_candidate)


def _prune_suppressed_delivery_candidates(workdir: str) -> None:
    """Clean rejected/unverified delivery lines only after Pi has stopped.

    This is intentionally a session-boundary operation.  During a live solve
    the FLAG file is append-only and may receive another stage at any moment;
    every whole-file rewrite is therefore deferred until both the solver and
    eager watcher have exited.
    """
    _drop_rejected_from_flag_files(workdir)
    for candidate in _load_unverified_flags(workdir):
        _drop_candidate_from_flag_files(workdir, candidate)


def _read_flag_file(workdir: str) -> list[str]:
    """Read delivery candidates in their append/discovery order.

    ``FLAG`` is a small, append-only hand-off queue during a live solve.  A
    set made the eager path submit several newly discovered flags in hash-table
    order, which is needlessly non-deterministic for a staged challenge.  Keep
    the first occurrence from each supported filename instead: one candidate
    can be submitted and acknowledged before the agent continues to the next
    stage, while duplicate lines still cost nothing.

    [B55b] Candidate recognition stays shared with ``pi_agent`` via
    :func:`verify.flag_line_candidate`.
    """
    out: list[str] = []
    seen: set[str] = set()
    for name in ("FLAG", "flag.txt", "FLAG.txt"):
        p = os.path.join(workdir, name)
        try:
            if os.path.isfile(p):
                with open(p, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        v = line.strip()
                        if flag_line_candidate(v) and v not in seen:
                            seen.add(v)
                            out.append(v)
        except Exception:
            pass
    return out


def _flag_delivery_signature(workdir: str) -> tuple:
    """Cheap change token for the transient FLAG delivery queue.

    Eager submission previously reparsed an ever-growing transcript on every
    polling tick even when the agent had not written a new delivery candidate.
    A multi-stage session can run for an hour, so that turns into background
    disk churn precisely while the agent is doing useful lateral work.  The
    signature lets the loop wake quickly for a real write while leaving an
    unchanged queue alone.  ``mtime_ns`` plus size handles both append and
    rewrite forms used by agents.
    """
    rows = []
    for name in ("FLAG", "flag.txt", "FLAG.txt"):
        path = os.path.join(workdir, name)
        try:
            st = os.stat(path)
            rows.append((name, int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            rows.append((name, 0, 0))
    return tuple(rows)


def _read_stable_flag_delivery_snapshot(workdir: str) -> tuple[list[str], tuple, bool]:
    """Read the FLAG delivery queue and its change token as one snapshot.

    The eager watcher used to read ``FLAG`` and only then stat it.  An agent
    append in that narrow gap made the watcher remember the *new* signature
    alongside the *old* contents, so the next quiet poll could suppress the
    newly written candidate forever.  Check the signature on both sides of
    each read instead.  One immediate retry covers the normal append/rewrite
    case without adding a polling interval; if the queue is still moving,
    report it as unstable and let the next poll acquire a clean snapshot.

    The returned signature intentionally contains only metadata.  Candidate
    text stays in the in-memory delivery list and is never persisted by this
    helper.
    """
    before = _flag_delivery_signature(workdir)
    flags = _read_flag_file(workdir)
    after = _flag_delivery_signature(workdir)
    if before == after:
        return flags, after, True

    # A write landed during the first read.  Read one more time using a new
    # before/after pair; do not publish a mixed content/signature snapshot.
    retry_before = _flag_delivery_signature(workdir)
    retry_flags = _read_flag_file(workdir)
    retry_after = _flag_delivery_signature(workdir)
    return retry_flags, retry_after, retry_before == retry_after


def _transcript_evidence_signature(
    workdir: str,
    transcript_path: str | None = None,
    *,
    trace_scope: str = "",
) -> tuple:
    """Return a cheap change token for the active instance's transcripts.

    The eager delivery watcher may keep a candidate pending while the matching
    ``tool_execution_end`` event is still being flushed.  It must retry when
    that evidence grows, but reparsing and re-verifying the same unchanged
    transcript every polling tick is pure busy work (and can repeatedly spend
    verifier LLM calls on an agent-authored candidate that will never gain
    evidence).  Keep this token answer-free: path names are scope-bound and
    only mtime/size are used.
    """
    rows = []
    for path in _scoped_transcript_paths(workdir, trace_scope, transcript_path):
        try:
            st = os.stat(path)
            rows.append((os.path.basename(path), int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            continue
    return tuple(rows)


def _eager_needs_scan(
    delivery_sig: tuple,
    last_delivery_sig: tuple | None,
    evidence_sig: tuple,
    last_evidence_sig: tuple | None,
    pending_evidence: set[str] | frozenset[str] | None,
    *,
    retry_due: bool = False,
) -> bool:
    """Decide whether the eager watcher should parse/verify the queue.

    A changed delivery queue always deserves a pass.  For an unchanged queue,
    a changed transcript can resolve a candidate waiting for its
    ``tool_execution_end`` evidence.  A platform submit exception is a
    different kind of pending work: it needs no new local evidence, but may
    wake the loop only when its bounded retry deadline has elapsed.  Keeping
    this tiny decision pure makes the anti-spin guarantee straightforward to
    regression-test.
    """
    if delivery_sig == last_delivery_sig:
        if pending_evidence and evidence_sig != last_evidence_sig:
            return True
        return bool(retry_due)
    return True


def _prune_confirmed_delivery_candidates(
    workdir: str,
    code: str,
    submitted: dict,
    submitted_lock: threading.Lock,
) -> int:
    """Remove already-confirmed lines from FLAG after a Pi session ends.

    The file is a *delivery queue*, not durable progress.  Its confirmed count
    and opaque candidate hashes are already in the task-local receipt, so
    retaining an accepted line only makes a successor session rediscover and
    reason about an old stage.  Pruning must not happen while Pi may be
    appending a later flag: a read-modify-write race could erase that later
    line.  Callers therefore invoke this only after the solver process and its
    eager watcher have both stopped.

    Non-confirmed lines are retained byte-for-byte in order.  SOURCE is
    intentionally untouched; it may still describe an unsubmitted line.
    """
    removed = 0
    for name in ("FLAG", "flag.txt", "FLAG.txt"):
        path = os.path.join(workdir, name)
        try:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        keep = []
        removed_here = 0
        for line in lines:
            candidate = line.strip()
            if flag_line_candidate(candidate):
                with submitted_lock:
                    delivered = _submitted_contains(submitted, code, candidate)
                if delivered:
                    removed_here += 1
                    continue
            keep.append(line)
        if not removed_here:
            continue
        try:
            if keep:
                tmp = path + ".delivery.tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(keep) + "\n")
                os.replace(tmp, path)
            else:
                os.remove(path)
            removed += removed_here
            log.info("  [delivery] pruned %d platform-confirmed line(s) from %s for %s",
                     removed_here, name, code)
        except OSError:
            # A failed cleanup cannot invalidate the platform receipt.  The
            # submitted digest will still suppress a duplicate on the next
            # pass, so leave the file intact and retry at a later boundary.
            try:
                os.remove(path + ".delivery.tmp")
            except OSError:
                pass
    return removed


def _normalize_flag_body(s: str) -> str:
    """返回平台语义下的候选键（保留完整值及大小写）。

    历史实现把外壳和 body 全部转为小写，但平台按原始提交值精确判题；这会让
    错误的小写提交把后续发现的正确大写答案一并拉黑。保留旧函数名以兼容调用点，
    实际语义改为精确提交键。
    """
    return flag_submission_key(s)


# 提交流程专用的 Flag 归一化正则（与 adapter.solver.base 的 body 合法性一致）
_FLAG_SHELL_RX = re.compile(
    r"^\s*(?P<shell>[fF][lL][aA][gG])\s*\{(?P<body>.*?)\}\s*$", re.S)
_FLAG_BODY_OK_RX = re.compile(r"^[A-Za-z0-9_\-.:/]{3,200}$")


def normalize_flag_envelope(fc: str) -> str:
    """清理候选的传输层杂质，但保留平台需要的原始大小写。

    - FLAG{body} / Flag{body} 保持各自的外壳大小写
    - 去掉 body 外的前后空白/引号（如 echo 'flag{...}' 的引号）
    - body 不合法（含换行/命令字符/过短）则原样返回（交给 verifier 判拒）
    只改外壳不改 body —— 框架层把 agent 的"近失手"变成命中，不会产生错误提交。
    """
    if not isinstance(fc, str):
        return fc
    m = _FLAG_SHELL_RX.match(fc)
    if not m:
        return fc.strip("\"' \t\n")
    body = m.group("body").strip()
    if not _FLAG_BODY_OK_RX.match(body):
        return fc.strip("\"' \t\n")
    return m.group("shell") + "{" + body + "}"


def _candidate_sha256(candidate: str) -> str:
    """Opaque, exact candidate identity used for dedupe and event recovery.

    The source value is first canonicalized with the same case-preserving key
    passed to the platform.  Therefore `FLAG{X}` and `flag{X}` remain distinct
    when the platform treats them as distinct, while no candidate plaintext is
    written to the shared event log.
    """
    key = _normalize_flag_body(candidate)
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _submitted_contains(submitted: dict, code: str, candidate: str) -> bool:
    """Check both the current digest format and the pre-migration key format.

    New events persist only an opaque SHA-256.  Keeping the old normalized-key
    check for one release prevents a live process that was seeded before the
    upgrade from re-submitting the same candidate, while no plaintext is read
    from the shared event log.
    """
    keys = {_candidate_sha256(candidate), _normalize_flag_body(candidate)}
    keys.discard("")
    existing = submitted.get(code, set())
    return any(key in existing for key in keys)


def _clean_flag_candidates(raw_flags) -> list[str]:
    """Normalize only local candidates and discard placeholders.

    Candidate ownership is established by evidence from the current challenge,
    not by opening sibling workspaces or historical event bodies.  This keeps
    the anti-hallucination gate intact without creating a cross-challenge
    answer-reading path.
    """
    out = []
    seen = set()
    for fc in raw_flags:
        nf = normalize_flag_envelope(fc)
        if nf == "":
            continue
        exact_key = _normalize_flag_body(nf)
        if exact_key in ("...", ""):
            log.info("  drop placeholder flag candidate")
            continue
        digest = _candidate_sha256(nf)
        if digest and digest not in seen:
            seen.add(digest)
            out.append(nf)
    return out


def _task_epoch_for(task) -> str:
    return str(getattr(task, "task_epoch", "") or _current_task_epoch() or "")


def _confirmed_progress_path(workdir: str) -> str:
    return os.path.join(workdir, ".confirmed-progress.json")


def _empty_confirmed_progress(epoch: str) -> dict:
    return {
        "version": 2,
        "task_epoch": epoch,
        "candidate_sha256": [],
        # A duplicate proves this exact candidate is already banked, but only
        # ``correct`` proves it was newly accepted after the platform baseline.
        # Keep those identities separate so a count-less backend can still
        # advance a partial N-of-M challenge without treating duplicates as new.
        "correct_candidate_sha256": [],
        "confirmed_count": 0,
        "platform_baseline": 0,
    }


def _confirmed_receipt_lower_bound(progress: dict) -> int:
    """Return the safe count implied by one task-local opaque receipt.

    ``platform_baseline`` is captured before locally observed ``correct``
    responses.  Each distinct correct response must therefore add one even
    when a compatible backend omits ``correct_flag_count``.  Duplicate hashes
    deliberately remain only a dedupe/lower-bound source.
    """
    try:
        baseline = max(0, int(progress.get("platform_baseline", 0) or 0))
    except (TypeError, ValueError):
        baseline = 0
    try:
        persisted = max(0, int(progress.get("confirmed_count", 0) or 0))
    except (TypeError, ValueError):
        persisted = 0
    candidates = set(progress.get("candidate_sha256") or ())
    correct = set(progress.get("correct_candidate_sha256") or ()) & candidates
    return max(persisted, baseline, len(candidates), baseline + len(correct))


def _read_confirmed_progress_unlocked(workdir: str, epoch: str) -> dict:
    if not epoch:
        return _empty_confirmed_progress("")
    try:
        with open(_confirmed_progress_path(workdir), encoding="utf-8") as fh:
            raw = json.load(fh) or {}
    except (OSError, ValueError, TypeError):
        raw = {}
    if str(raw.get("task_epoch", "")) != epoch:
        return _empty_confirmed_progress(epoch)
    hashes = sorted({str(value) for value in (raw.get("candidate_sha256") or [])
                     if re.fullmatch(r"[0-9a-f]{64}", str(value))})
    correct_hashes = sorted({str(value) for value in (
        raw.get("correct_candidate_sha256") or [])
        if re.fullmatch(r"[0-9a-f]{64}", str(value)) and str(value) in hashes})
    try:
        count = max(0, int(raw.get("confirmed_count", 0) or 0))
    except (TypeError, ValueError):
        count = 0
    try:
        baseline = max(0, int(raw.get("platform_baseline", 0) or 0))
    except (TypeError, ValueError):
        baseline = 0
    progress = {
        "version": 2,
        "task_epoch": epoch,
        "candidate_sha256": hashes,
        "correct_candidate_sha256": correct_hashes,
        "confirmed_count": count,
        "platform_baseline": baseline,
    }
    progress["confirmed_count"] = _confirmed_receipt_lower_bound(progress)
    return progress


def _write_confirmed_progress_unlocked(workdir: str, progress: dict) -> None:
    path = _confirmed_progress_path(workdir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    candidate_hashes = sorted({str(value) for value in progress.get("candidate_sha256", [])
                               if re.fullmatch(r"[0-9a-f]{64}", str(value))})
    correct_hashes = sorted({str(value) for value in progress.get(
        "correct_candidate_sha256", [])
        if re.fullmatch(r"[0-9a-f]{64}", str(value)) and str(value) in candidate_hashes})
    payload = {
        "version": 2,
        "task_epoch": str(progress.get("task_epoch", "") or ""),
        "candidate_sha256": candidate_hashes,
        "correct_candidate_sha256": correct_hashes,
        "confirmed_count": max(0, int(progress.get("confirmed_count", 0) or 0)),
        "platform_baseline": max(0, int(progress.get("platform_baseline", 0) or 0)),
        "updated_at": time.time(),
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def _confirmed_progress_count(workdir: str, task) -> int:
    """Current task's durable lower-bound number of confirmed flags."""
    epoch = _task_epoch_for(task)
    baseline = max(0, int(getattr(task, "correct_flag_count", 0) or 0))
    if not epoch:
        return baseline
    with _advisory_lock(_confirmed_progress_path(workdir) + ".lock"):
        progress = _read_confirmed_progress_unlocked(workdir, epoch)
        return max(baseline, _confirmed_receipt_lower_bound(progress))


def _hydrate_submitted_from_confirmed_progress(
    submitted: dict,
    submitted_lock: threading.Lock,
    code: str,
    workdir: str,
    task,
) -> int:
    """Restore this challenge's confirmed submission hashes from its own receipt.

    The old restart path scanned the shared event stream and selected entries
    by challenge code.  Even with opaque hashes, that required opening records
    belonging to every other challenge and made the event log part of the
    answer-selection path.  The per-challenge manifest is written atomically
    immediately after a platform-confirmed response, is epoch-scoped, and
    contains only one-way candidate identities.  It is therefore sufficient
    for restart dedupe without reading sibling workspaces or shared history.

    Returns the non-sensitive confirmed-count lower bound so callers can keep
    the prompt progress display in sync with the same local receipt.
    """
    epoch = _task_epoch_for(task)
    baseline = max(0, int(getattr(task, "correct_flag_count", 0) or 0))
    if not epoch:
        return baseline
    with _advisory_lock(_confirmed_progress_path(workdir) + ".lock"):
        progress = _read_confirmed_progress_unlocked(workdir, epoch)
        hashes = set(progress["candidate_sha256"])
        confirmed = max(baseline, _confirmed_receipt_lower_bound(progress))
    if hashes:
        with submitted_lock:
            submitted.setdefault(str(code), set()).update(hashes)
    return confirmed


def _initialize_confirmed_progress(workdir: str, task,
                                   candidate_hashes: set | None = None) -> int:
    """Merge known current-epoch progress before a visit starts.

    ``candidate_hashes`` is retained for API compatibility with callers that
    already hold local, opaque receipts.  Production recovery deliberately
    uses :func:`_hydrate_submitted_from_confirmed_progress` instead of shared
    event history, so this helper never needs to inspect another challenge.
    """
    epoch = _task_epoch_for(task)
    baseline = max(0, int(getattr(task, "correct_flag_count", 0) or 0))
    if not epoch:
        return baseline
    with _advisory_lock(_confirmed_progress_path(workdir) + ".lock"):
        progress = _read_confirmed_progress_unlocked(workdir, epoch)
        hashes = set(progress["candidate_sha256"])
        correct_hashes = set(progress["correct_candidate_sha256"])
        hashes.update(str(value) for value in (candidate_hashes or set())
                      if re.fullmatch(r"[0-9a-f]{64}", str(value)))
        progress["candidate_sha256"] = sorted(hashes)
        progress["correct_candidate_sha256"] = sorted(correct_hashes & hashes)
        # ``task.correct_flag_count`` is updated with the derived receipt count
        # after every submit.  Once local correct receipts exist it is no longer
        # a fresh platform baseline and folding it back here would double count.
        if not correct_hashes:
            progress["platform_baseline"] = max(
                int(progress["platform_baseline"]), baseline)
        progress["confirmed_count"] = max(
            _confirmed_receipt_lower_bound(progress), baseline)
        try:
            _write_confirmed_progress_unlocked(workdir, progress)
        except OSError:
            pass
        return int(progress["confirmed_count"])


def _record_confirmed_submission(workdir: str, task, candidate: str, submit_result) -> int:
    """Durably record one platform-confirmed candidate without its plaintext.

    Candidate hashes give a safe lower bound when a backend omits cumulative
    counts.  A duplicate proves a candidate was already accepted, but neither
    it nor a delayed correct response may blindly increment an already newer
    platform count: concurrent submits can return in a different order.
    Backend counts, when present, remain authoritative.
    """
    epoch = _task_epoch_for(task)
    baseline = max(0, int(getattr(task, "correct_flag_count", 0) or 0))
    digest = _candidate_sha256(candidate)
    if not epoch:
        return baseline
    with _advisory_lock(_confirmed_progress_path(workdir) + ".lock"):
        progress = _read_confirmed_progress_unlocked(workdir, epoch)
        hashes = set(progress["candidate_sha256"])
        correct_hashes = set(progress["correct_candidate_sha256"])
        is_correct = bool(getattr(submit_result, "correct", False))
        is_duplicate = bool(getattr(submit_result, "duplicate", False))
        # Capture this before adding the current response.  A first local
        # correct receipt must retain the platform count observed when the
        # visit started; after the add, ``correct_hashes`` is no longer useful
        # for distinguishing that immutable baseline from derived progress.
        had_correct_receipts = bool(correct_hashes)
        # An incorrect/rejected submission is not progress.  Never put its
        # digest in the confirmed set: doing so would make the hash cardinality
        # look like additional flags and could prematurely satisfy N-of-M.
        was_new = bool(digest and digest not in hashes and (is_correct or is_duplicate))
        if was_new:
            hashes.add(digest)
        if was_new and is_correct:
            correct_hashes.add(digest)
        # Preserve the platform count observed before the first local correct
        # response.  Later task attributes may already include this receipt's
        # derived count, so using them as a new baseline would overcount.
        if not had_correct_receipts:
            progress["platform_baseline"] = max(
                int(progress["platform_baseline"]), baseline)
        progress["candidate_sha256"] = sorted(hashes)
        progress["correct_candidate_sha256"] = sorted(correct_hashes & hashes)
        count = _confirmed_receipt_lower_bound(progress)
        try:
            reported = max(0, int(getattr(submit_result, "correct_flag_count", 0) or 0))
        except (TypeError, ValueError):
            reported = 0
        if reported:
            count = max(count, reported)
        progress["confirmed_count"] = count
        try:
            _write_confirmed_progress_unlocked(workdir, progress)
        except OSError:
            pass
        return count


def _emit_flag_submit(observation, code: str, candidate: str, submit_result, task, **extra) -> None:
    """Emit a recoverable submit event without candidate plaintext or prefixes."""
    if observation is None:
        return
    try:
        confirmed = _confirmed_progress_count(task.workdir, task)
    except Exception:
        confirmed = 0
    payload = {
        "code": code,
        "candidate_sha256": _candidate_sha256(candidate),
        "correct": bool(getattr(submit_result, "correct", False)),
        "awarded": int(getattr(submit_result, "awarded", 0) or 0),
        "duplicate": bool(getattr(submit_result, "duplicate", False)),
        "correct_flag_count": int(getattr(submit_result, "correct_flag_count", 0) or 0),
        "total_flag_count": int(getattr(submit_result, "total_flag_count", 0) or 0),
        "expected_flag_count": int(getattr(task, "flag_count", 1) or 1),
        "confirmed_flag_count": confirmed,
    }
    payload.update(extra)
    observation.emit("flag_submit", layer="driver", payload=payload)
def _remote_grounded(workdir: str, flag: str, cmd: str, trace_rows: list,
                     evidence_policy: FlagEvidencePolicy | None = None) -> bool:
    """Whether a first observation has current-instance remote provenance.

    The direct-command fast path remains useful, but a target response may be
    stored by a successful curl/wget call and read in a later tool event.  Run
    the exact same scoped provenance classifier for that latter case rather
    than inventing a looser ``cat`` exception in the fallback path.
    """
    policy = evidence_policy or FlagEvidencePolicy()
    if is_task_remote_command(cmd, policy):
        return True
    try:
        claim = flag_confidence(flag, "", trace_rows,
                                evidence_policy=policy)
    except Exception:
        return False
    return bool(claim.grounded and is_remote_provenance(claim.provenance))


def _mask_skeptic(s: str, body: str, n: int) -> str:
    """[B47] 压平空白 + 把候选串换成占位符 + 截断（证据包不许复制候选明文）。"""
    s = re.sub(r"\s+", " ", str(s or ""))
    if body:
        s = s.replace(body, "⟦候选⟧")
    return s[:n]


def _skeptic_evidence(workdir: str, flag: str, limit: int = 3, *,
                      trace_scope: str = "") -> str:
    """[B47] 给判断 Agent 的证据包：候选串在工具调用里的出现处（命令 + 输出窗口）。

    只取当前实例最近的调用，最多 limit 条。候选出现在**命令参数**里会单独标注
    —— agent 自己把猜测敲进命令行，是判「自造」最有力的证据；只出现在输出里，
    则要看输出上下文（靶场回包 vs 固件串）。

    空返回有语义：该候选在任何工具调用里都没留下痕迹 —— 纯幻觉的典型形态，
    调用方据此**不允许翻案**。
    """
    body = _normalize_flag_body(flag)
    if not body or not trace_scope:
        return ""
    rows: list = []
    # Iterate backwards so the bounded evidence packet favours the latest
    # session, but preserve a single shared scope and deterministic event
    # order from the transcript reader.
    for _tool, args, out in reversed(
            _tool_outputs_from_current_instance_transcripts(
                workdir, trace_scope=trace_scope)):
        if len(rows) >= limit:
            break
        args_s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        cmd = str(args.get("command", args)) if isinstance(args, dict) else str(args)
        # Comparison is body-normalized, while the original text remains only
        # in the masked evidence package.
        if body in str(args_s).lower():
            rows.append("[%d] 命令参数里就有该串（agent 自己敲的）: %s"
                        % (len(rows) + 1, _mask_skeptic(cmd, body, 150)))
        if len(rows) >= limit:
            break
        if body in str(out or "").lower():
            out_s = str(out or "")
            i = out_s.lower().find(body)
            rows.append("[%d] 命令: %s\n    输出: …%s…"
                        % (len(rows) + 1, _mask_skeptic(cmd, body, 150),
                           _mask_skeptic(out_s[max(0, i - 260):i + 90], body, 380)))
    return "\n".join(rows[:limit])


def _fmt_rescue_on() -> bool:
    """[B55] 非标准信封复核通道总开关。默认开；置 ADAPTER_FMT_RESCUE=0 整体关闭。

    关掉后行为与本补丁前**逐字一致**：invalid_format 候选直接进未验证账本，
    永不送判断 Agent、永不上平台。
    """
    return str(os.environ.get("ADAPTER_FMT_RESCUE", "1") or "1").strip() != "0"


def _skeptic_check(verifier, claim, workdir: str, code: str, *,
                   stage: str, memo: dict = None, trace_scope: str = "") -> str:
    """[B47] 请判断 Agent 复核一次。返回 ""（无意见/维持原判）| "rescue" | "veto"。

    stage="refuse"：确定性规则要**拒收**，问能否翻案（治误杀）；
    stage="submit"：强提/取证通道要**提交**，问是否该拦（治错交）。

    两条硬约束：
      · 证据为空 → 拒收侧一律不翻案（没有任何痕迹的候选 = 纯幻觉，不许复活）；
      · 任何异常/超时 → 返回 ""，主流程行为与打补丁前逐字一致。
    """
    verdict = ""
    try:
        ev = _skeptic_evidence(workdir, claim.flag, trace_scope=trace_scope)
        # 同一候选在 refuse（翻案）和 submit（拦截）阶段的判据不同，且后续
        # transcript 会补到新证据；不能按 lower-case body 跨阶段复用旧裁决。
        key = (stage, _normalize_flag_body(claim.flag),
               hashlib.sha256(ev.encode("utf-8", "ignore")).hexdigest())
        if memo is not None and key in memo:
            return memo[key]
        if not ev and stage == "refuse":
            verdict = ""            # 无痕迹，不给翻案
        else:
            claim = verifier.skeptic(claim, ev, code=code)
            v, c = claim.skeptic_verdict, claim.skeptic_conf
            if stage == "refuse" and v == "genuine" and c >= verifier.rescue_conf:
                log.info("  [skeptic] 翻案（conf=%.2f, %s）→ 放行提交: %s",
                         c, claim.skeptic_reason or v, claim.flag[:30])
                verdict = "rescue"
            elif stage == "submit":
                # 准入制：判断 Agent 必须认可这个 flag 才提交。
                # genuine + 高置信 → 放行；fabricated/decoy/stale + 高置信 → 拦截；
                # 不确定（conf<阈值）或 LLM 失败 → 宁放过不误伤，放行。
                if v == "genuine" and c >= verifier.veto_conf:
                    log.info("  [skeptic] 认可（genuine conf=%.2f）→ 提交: %s",
                             c, claim.flag[:30])
                    verdict = ""  # 允许提交
                elif v in ("fabricated", "decoy", "stale") and c >= verifier.veto_conf:
                    log.info("  [skeptic] 拦截（%s conf=%.2f, %s）→ 不提交: %s",
                             v, c, claim.skeptic_reason or "-", claim.flag[:30])
                    verdict = "veto"
                else:
                    # LLM 不确定（低置信度）或 verdict 为空 → 宁放过
                    log.info("  [skeptic] 不确定（%s conf=%.2f）→ 放行: %s",
                             v or "none", c, claim.flag[:30])
                    verdict = ""
            else:
                _note_skeptic_noop(stage, v, c, claim.flag)
    except Exception as e:                                   # noqa: BLE001
        log.warning("  [B47] 判断 Agent 异常，维持原判: %s", e)
        verdict = ""
    if memo is not None:
        memo[key] = verdict
    return verdict


# [B47b] 「无意见」留痕限流。只在**闸门没起作用**时说话；裁判与原判一致时不吭声。
# 纯日志：不参与任何判定，改坏了最坏结果只是少几条或多了几条日志。
_SKEPTIC_NOOP_GAP = 300.0     # 秒；同类提示最多 5 分钟一条，避免刷屏
_skeptic_noop_ts = [0.0]      # 上次打印时刻（进程内，不落盘）
_skeptic_noop_n = [0]         # 限流窗口内被压掉的次数


def _note_skeptic_noop(stage: str, v: str, c: float, flag: str) -> None:
    """[B47b] 记录一次「判断 Agent 没能改变结果」。

    只在闸门**本可能起作用却没起作用**时留痕：
      · 没有裁决（模型失联 / 重试耗尽）      —— 闸门等于不存在，最该看见的一种；
      · 裁决与确定性规则相反、但置信度不够    —— 闸门看见了却没敢动手，错交的近失。
    裁判与原判一致时直接返回（那是正常静默，打了就是刷屏）。
    """
    if not v:
        kind, lvl = "模型未给出可解析裁决（失联或重试耗尽）", "warning"
    elif stage == "refuse" and v == "genuine":
        kind, lvl = "倾向翻案（genuine conf=%.2f）但未达放行阈值" % c, "info"
    elif stage == "submit" and v in ("fabricated", "decoy", "stale"):
        kind, lvl = "倾向拦截（%s conf=%.2f）但未达拦截阈值" % (v, c), "warning"
    else:
        return                                  # 裁判与原判一致 —— 正常静默
    now = time.time()
    _skeptic_noop_n[0] += 1
    if now - _skeptic_noop_ts[0] < _SKEPTIC_NOOP_GAP:
        return                                  # 限流：压在计数里，下次一并报
    n, _skeptic_noop_n[0], _skeptic_noop_ts[0] = _skeptic_noop_n[0], 0, now
    msg = ("  [B47b] 判断 Agent 无意见（%s 阶段，%s；期间累计 %d 次）"
           "→ 维持确定性原判: %s")
    if lvl == "warning":
        log.warning(msg, stage, kind, n, flag[:30])
    else:
        log.info(msg, stage, kind, n, flag[:30])


def _flag_grounded_in_transcripts(workdir: str, flag: str, *,
                                  require_remote: bool = False,
                                  evidence_policy: FlagEvidencePolicy | None = None,
                                  trace_scope: str = "") -> bool:
    """flag 是否「先出现在工具输出」而非「先出现在命令参数」。

    agent 自写/猜的 flag（echo 'flag{...}' > FLAG）经 cat/tee 回显后也会
    出现在工具输出里，旧规则（出现在任何输出即强提）会把幻觉 flag 强提
    给平台 → 烧配额 + 连败熔断预算（实测某题 flag{guess} 被强提
    INCORRECT）。真 flag 是先出现在靶场响应输出里。
    判据：按事件顺序找 flag 首次出现——先在命令参数 = agent 自己写的
    （后续输出只是回显）；先在输出 = 外部来源，才强提。只读传入的
    当前实例 trace scope；绝不按挑战题号或 mtime 拼接旧会话。
    """
    if not trace_scope:
        return False
    trace_rows = _tool_outputs_from_current_instance_transcripts(
        workdir, trace_scope=trace_scope)
    for row_idx, (_tool, args, out) in enumerate(trace_rows):
        args_s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        if flag in str(args_s):
            # Flag first appeared in command args.  For reverse/crypto challenges
            # this is legitimate (agent computed the flag and wrote it via printf).
            # When require_remote=False, accept it; the platform judges correctness.
            if not require_remote:
                return True
            return False             # 参数先现（同调用内 echo 回显也算）
        if out and flag in str(out):
            verdict_cmd = (str(args.get("command", ""))
                           if isinstance(args, dict) else str(args))
            if require_remote and not _remote_grounded(
                    workdir, flag, verdict_cmd, trace_rows[:row_idx + 1],
                    evidence_policy):
                # 首现即「本地静态产物算出来」的候选不走强提；本地题必须由
                # 可复现 primary gate 自己放行，不能让这条补偿通道绕过去。
                log.info("  [force] 强提否决：首现命令非网络交互（本地静态产物）: %s",
                         flag[:30])
                return False
            return True
    return False


def build_task(ch: Challenge, workdir: str, targets: list = None) -> AgentTask:
    """从 Challenge 构建 AgentTask — 对接真实 API 字段"""
    task = AgentTask(
        objective=ch.description or "Capture the flag(s) from the target.",
        targets=targets or ch.container_addr or [],
        flag_count=ch.flag_count,
        flag_format=os.getenv("ADAPTER_FLAG_FORMAT", "flag{...}"),
        # Optional platform metadata only.  A filename is not answer evidence;
        # it merely lets the verifier recognize a command that reads an
        # officially supplied local input instead of an agent-created file.
        files=list(getattr(ch, "files", []) or []),
        workdir=workdir,
        category=ch.category or _infer_category(ch),
        difficulty=ch.difficulty or None,
        unique_code=ch.unique_code,
        score=ch.total_score,
    )
    # AgentTask 保持向后兼容，不扩展构造器位置参数；把平台已确认数量作为
    # 非敏感的运行时属性挂上去，供跨访问提示和不完整提交响应兜底。
    task.correct_flag_count = max(0, int(getattr(ch, "correct_flag_count", 0) or 0))
    # Not a challenge identifier or answer: this is the generation boundary
    # used to keep durable local progress isolated when a benchmark resets.
    task.task_epoch = _current_task_epoch()
    return task


def _submission_complete(task: AgentTask, submit_result, confirmed_count: int = 0,
                         *, accepted_count: int | None = None) -> bool:
    """只在确认收齐预期数量时结束题目，兼容不完整的后端响应。

    部分兼容后端/异常响应会把 ``correct_flag_count`` 和
    ``total_flag_count`` 都留为 0；直接比较会得到 ``0 >= 0``，多 flag
    题第一条正确提交就被误判为整题完成。优先使用平台返回值，但总数
    不得低于题目元数据；若平台没返回总数，则用本题已知进度兜底。
    """
    # ``accepted_count`` was the old public keyword.  Keep it as an alias so
    # extensions/tests written against the prior driver do not fail during the
    # completion path; the durable platform-confirmed meaning is unchanged.
    legacy_accepted = None
    if accepted_count is not None:
        legacy_accepted = max(0, int(accepted_count or 0))
        confirmed_count = max(int(confirmed_count or 0), legacy_accepted)
    expected = max(1, int(getattr(task, "flag_count", 1) or 1))
    correct = max(0, int(getattr(submit_result, "correct_flag_count", 0) or 0))
    reported_total = max(0, int(getattr(submit_result, "total_flag_count", 0) or 0))
    total = max(expected, reported_total)
    durable = max(0, int(confirmed_count or 0))
    if reported_total > 0:
        return max(correct, durable) >= total
    if expected <= 1:
        # 单 flag 后端即使不回计数，成功/duplicate 或 durable progress
        # proves completion.  The latter covers a crash between API response
        # and close retry.
        return bool(getattr(submit_result, "correct", False)
                    or getattr(submit_result, "duplicate", False)
                    or durable >= 1)
    known = max(0, int(getattr(task, "correct_flag_count", 0) or 0))
    # ``durable`` already includes the platform baseline when it was known;
    # adding ``known`` again would double-count multi-flag progress after a
    # restart.  Keep the maximum rather than a sum.
    # The legacy keyword represented only newly accepted values in this
    # process, whereas ``confirmed_count`` is a durable total.  Preserve that
    # older contract without double-counting the modern call path.
    if legacy_accepted is not None:
        return max(durable, known + legacy_accepted) >= expected
    return max(known, durable) >= expected


# ── 启动/关闭实例 ──────────────────────────────────────────

def _start_with_retry(client, code: str, *, stop_event, rate_wait, retries=None,
                      deadline: float | None = None):
    """带重试的实例启动 — 对接真实 API 异常"""
    max_retries = retries or _MAX_ACTIVE_RETRIES
    for i in range(max_retries):
        _beat()
        if stop_event.is_set():
            return None, "stop"
        if deadline is not None and time.monotonic() >= deadline:
            return None, "aborted"
        rate_wait()
        try:
            return client.start_challenge(code), None
        except InvalidState as e:
            # 409: 活跃实例达上限(3个) 或 任务已结束
            if "上限" in e.message or "active" in e.message.lower() or "max" in e.message.lower():
                wait_s = min(3.0 * (i + 1), 20.0)
                log.warning("max active on %s; waiting %.0fs (%d/%d)",
                            code, wait_s, i + 1, max_retries)
                if i == 2:
                    # 连等 3 次仍满 → 大概率有无人认领的孤儿实例占槽
                    #（进程死于 visit 中段留下的靶场）→ 回收后重试
                    try:
                        closed = _heal_orphan_instances(client, exclude=code)
                        if closed:
                            log.info("max active on %s: 已回收孤儿实例 %s，重试",
                                     code, ",".join(closed))
                    except Exception:
                        pass
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        return None, "aborted"
                    stop_event.wait(min(wait_s, remaining))
                else:
                    time.sleep(wait_s)
                continue
            else:
                # This is a platform/task terminal signal, not a local
                # shutdown request.  ``stop_event`` is also the graceful
                # reload signal; setting it here made the worker exit 0 and
                # stay down forever under Docker's ``restart:on-failure``.
                # Let schedule_rounds mark the epoch terminal and hand off to
                # await-task instead.
                log.error("task ended (invalid_state): %s", e)
                return None, "task_ended"
        except ResourceUnavailable as e:
            log.warning("resource unavailable on %s: %s, retry", code, e)
            if i + 1 < max_retries:
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        return None, "aborted"
                    stop_event.wait(min(5.0, remaining))
                else:
                    time.sleep(5)
                continue
        except ChallengeNotFound as e:
            log.error("challenge not found: %s", code)
            return None, "not_found"
        except Exception as e:
            log.error("start_challenge failed: %s", e)
            if i + 1 < max_retries:
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining <= 0:
                        return None, "aborted"
                    stop_event.wait(min(3.0, remaining))
                else:
                    time.sleep(3)
                continue
            # Return a typed, retryable outcome rather than letting the fleet
            # reduce it to an unclassified ``error``.  The dispatcher can then
            # persist one cooldown instead of immediately reopening the same
            # target on its next round.
            return None, "start_failed"
    return None, "retry"


_ACTIVE_CONTAINER_STATES = {
    "pending", "available", "starting", "running", "stop_pending", "stopping",
}


def _close_is_confirmed_inactive(client, code: str) -> bool:
    """Return whether a post-close platform snapshot proves the instance is gone.

    Some TsecBench-compatible backends answer a close request with
    ``closed=False`` while an asynchronous stop is already underway, or when a
    completed challenge was closed by the platform first.  Treating that reply
    as a terminal failure left a completed multi-flag container untracked until
    a later startup sweep.  A list response is answer-free lifecycle metadata,
    so it is safe to use solely to distinguish an active instance from one that
    is already absent/stopped.
    """
    try:
        rows = client.list_challenges()
    except Exception as exc:
        # A task-terminal response means the platform no longer has a live
        # instance for this worker to close.  Other failures remain unconfirmed
        # so the caller can retry instead of assuming success.
        return _task_finished(str(exc))
    for row in rows or []:
        if isinstance(row, dict):
            row_code = str(row.get("unique_code", "") or "")
            status = str(row.get("container_status", "") or "")
        else:
            row_code = str(getattr(row, "unique_code", "") or "")
            status = str(getattr(row, "container_status", "") or "")
        if row_code != str(code):
            continue
        return status.strip().lower() not in _ACTIVE_CONTAINER_STATES
    # The code disappeared from the live task list, which is equivalent to no
    # remaining instance.  This is common after a completed benchmark rotates.
    return True


def _close_with_retry(client, code: str, *, retries: int = 3):
    """Close once, then reconcile/retry transient false close acknowledgements.

    A positive acknowledgement succeeds immediately.  A false acknowledgement
    is *not* a reliable indication that the target is still active: compatible
    backends use it for an already-closed/async-stopping target.  Reconcile it
    with lifecycle state, and retry only while the target is still active.  This
    keeps the close idempotent while avoiding a leaked completed multi-flag
    instance after a timing race.
    """
    attempts = max(1, int(retries or 1))
    for i in range(attempts):
        try:
            result = client.close_challenge(code)
            if not hasattr(result, "closed") or bool(result.closed):
                return True
            if _close_is_confirmed_inactive(client, code):
                log.info("close %s acknowledged asynchronously/already closed", code)
                return True
            log.warning("close %s not yet confirmed active (%d/%d); retrying",
                        code, i + 1, attempts)
        except Exception as e:
            if i + 1 >= attempts:
                log.error("FAILED to close %s after %d tries: %s", code, attempts, e)
                return False
            log.warning("close %s failed (%d/%d): %s", code, i + 1, attempts, e)
        if i + 1 < attempts:
            time.sleep(min(2.0 * (i + 1), 6.0))
    log.error("FAILED to close %s after %d tries", code, attempts)
    return False


# ── 单题求解 ──────────────────────────────────────────────

HEARTBEAT_PATH = "/tmp/driver_heartbeat"
_BOOT_STAMP = f"{time.strftime('%m%d%H%M%S')}_{os.getpid()}"   # 启动戳+PID，避免同秒重启碰撞


def _beat() -> None:
    """更新心跳文件 mtime（docker healthcheck 据此判断存活）+ 状态文件"""
    try:
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
    except Exception:
        pass
    _update_status()


def _worker_shard(challenges: list) -> list:
    """
    Worker 分片：每个容器只处理自己分到的题目子集。

    - ADAPTER_WORKER_COUNT: worker 总数（默认 1 = 不分片）
    - ADAPTER_WORKER_ID:    本 worker 序号 0..count-1
      未设置时从容器 hostname 尾号推导（compose --scale 场景）:
      tsecbench-adapter-adapter-1/2/3 → id 0/1/2
    """
    count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
    if count <= 1:
        return challenges

    wid_raw = os.environ.get("ADAPTER_WORKER_ID", "")
    wid = -1
    if wid_raw.strip() != "":
        try:
            wid = int(wid_raw) % count
        except ValueError:
            wid = -1
    if wid < 0:
        host = os.environ.get("HOSTNAME", "")
        m = re.search(r"-(\d+)$", host)
        if m:
            wid = (int(m.group(1)) - 1) % count
    if wid < 0:
        wid = 0

    shard = [c for i, c in enumerate(challenges) if i % count == wid]
    log.info("worker %d/%d: %d challenges assigned", wid, count, len(shard))
    return shard


def _solver_shard(challenges: list) -> list:
    """把已知分类题只分给「解题目 worker」（wid>=1），monitor(wid0) 不参与。

    _worker_shard 按 count=3 会把 1/3 分到 wid0(monitor)——monitor 不解题，
    那些题会被饿死。这里只把任务在解题目 worker(1..count-1) 之间轮转：
    wid0 进入则返回 []（其列表在 main 里被过滤掉，只剩派单/unknown 逻辑）。

    碰撞安全：crc32 分片在小规模题集下可能全部碰撞到同一 bucket（实测
    小样本下全部题目 crc32%2=0 → wid2 空手）。检测到碰撞时回退到排序后下标轮转（按 unique_code
    升序确保各 worker 独立算出相同切片），保证每个 worker 都分到题、不重叠。
    """
    count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
    wid = _worker_id()
    if count <= 2:
        return challenges
    if wid < 1:
        return []
    n_solvers = count - 1
    # 用 unique_code 的 crc32 做确定性分片，而非平台列表的下标奇偶：
    # 平台 list_challenges() 各次返回顺序不稳定，下标分片会让两 worker 算出
    # 不同切片 → 题漏分无人接管（实测两题长期 pending 而 worker 空闲）。
    # 与 _unknown_bucket 同模式：不能用内置 hash()（Python 进程内随机加盐）。
    buckets = [0] * n_solvers
    for challenge in challenges:
        buckets[zlib.crc32(challenge.unique_code.encode("utf-8")) % n_solvers] += 1
    shard = [c for c in challenges
             if (zlib.crc32(c.unique_code.encode("utf-8")) % n_solvers) == (wid - 1)]
    # Small sets can leave one or more hash buckets empty.  Every solver must
    # make the same fallback decision, otherwise only the empty worker switches
    # to index sharding and overlaps the non-empty worker's hash slice.  The
    # deterministic sorted round-robin fallback provides full coverage without
    # involving the monitor worker.
    if any(size == 0 for size in buckets):
        sorted_ch = sorted(challenges, key=lambda c: c.unique_code)
        shard = [c for i, c in enumerate(sorted_ch) if (i % n_solvers) == (wid - 1)]
    return shard


def _capability_sharding_enabled() -> bool:
    """Return whether a second, worker-id shard is safe after capability filtering.

    Capability filtering is allowed to be asymmetric (for example W2 handles
    web while W3 handles pwn).  Applying the global worker-id hash a second
    time in that mode silently drops half of each worker's own capability
    pool: the other worker has already filtered those challenges out.  Keep
    deterministic sharding only for the shipped homogeneous capability pool,
    or when an operator explicitly opts in.  An explicit opt-out is useful
    for deployments that deliberately run identical but independently scoped
    workers.
    """
    raw = os.environ.get("ADAPTER_CAPABILITY_SHARD", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    caps = _load_capabilities()
    # The default compose pool is the complete known skill set (``unknown``
    # is assigned separately by _unknown_bucket).  Only that homogeneous
    # configuration can safely use the global id shard without seeing the
    # other worker's capability declaration.
    homogeneous_known = _KNOWN_CATEGORIES - {"unknown"}
    return homogeneous_known.issubset(caps)


# ── 优先任务队列（网页「Agent 解此题」派单给舰队）──────────

def _worker_id() -> int:
    """当前 worker 序号（与 _worker_shard 推导一致）。"""
    count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
    wid_raw = os.environ.get("ADAPTER_WORKER_ID", "")
    wid = -1
    if wid_raw.strip() != "":
        try:
            wid = int(wid_raw) % count
        except ValueError:
            wid = -1
    if wid < 0:
        host = os.environ.get("HOSTNAME", "")
        m = re.search(r"-(\d+)$", host)
        if m:
            wid = (int(m.group(1)) - 1) % count
    if wid < 0:
        wid = 0
    return wid



_last_priority_signature = None


def _priority_code_key(code: object) -> str:
    """Stable key for a queue code without changing the displayed code."""
    return str(code or "").strip().casefold()


def _purge_stale_priority(current_codes: set) -> None:
    """Keep only current unfinished-task entries in the user-facing queue.

    Unlike the retired owner registry, priority contains codes only and no
    answer material.  It is safe to prune lazily as the platform list changes.
    """
    global _last_priority_signature
    current_codes = {_priority_code_key(code) for code in current_codes
                     if _priority_code_key(code)}
    epoch = _current_task_epoch()
    signature = (epoch, tuple(sorted(current_codes)))
    # An all-completed platform response is meaningful: it must retire every
    # queue entry instead of leaving a solved code permanently "queued" in
    # the console and in the worker-load accounting.
    if signature == _last_priority_signature:
        return
    workdir = os.getenv("ADAPTER_WORKDIR", "/work")
    pp = os.path.join(workdir, "priority.txt")
    try:
        # The console appends under this same lock.  Without it, a UI request
        # arriving between this read and rewrite could be silently discarded.
        with _advisory_lock(pp + ".lock"):
            try:
                with open(pp, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except OSError:
                lines = []
            keep = []
            for ln in lines:
                stripped = ln.strip()
                if not stripped or stripped.startswith("#"):
                    keep.append(ln)
                    continue
                parts = stripped.split("|")
                code = _priority_code_key(parts[0])
                entry_epoch = parts[2].strip() if len(parts) > 2 else ""
                # A bound queue entry belongs only to its exact epoch.  Legacy
                # unbound entries are not safe after epoch activation because
                # a reused challenge code would inherit an old manual route.
                if code not in current_codes:
                    continue
                if epoch and entry_epoch != epoch:
                    continue
                keep.append(ln)
            if keep != lines:
                tmp = pp + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(chr(10).join(keep) + (chr(10) if keep else ""))
                os.replace(tmp, pp)
                log.info("registry self-heal: priority.txt 剔除跨任务题号/旧 epoch 派单")
        _last_priority_signature = signature
    except OSError:
        pass




def _close_orphan_sessions(events_path: str) -> None:
    """启动自愈：被杀会话的 session_start 悬空无 session_end。

    容器在会话中途被重启（守卫热部署等），共享 _events.jsonl 里就留下没有收尾的
    session_start——前端不渲染这类会话，时长/回合统计也失真。会话时长有硬上限
    （时间盒 3600s），所以超过宽限仍未收尾的一定来自已死进程；活跃会话必然在
    宽限期内，绝不会被误伤。只补旧记录，不动任何在途会话。
    """
    grace = float(os.getenv("ADAPTER_ORPHAN_GRACE", "4500"))
    # worker-2/3 start concurrently and share _events.jsonl.  A plain
    # read-then-emit lets both processes observe the same stale start and append
    # duplicate synthetic ends.  Serialize the scan and keep a tiny digest
    # marker so the operation is idempotent across restarts as well.
    marker = events_path + ".orphan-closed.json"
    with _advisory_lock(marker + ".lock"):
        try:
            with open(events_path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return
        open_pairs = {}
        for ln in lines:
            try:
                e = json.loads(ln)
            except Exception:
                continue
            ev = e.get("event")
            if ev not in ("session_start", "session_end"):
                continue
            p = e.get("payload") or {}
            # 以 worker/boot 隔离同题并发会话；只用 code+round+idx 会让不同
            # worker 的同索引会话互相覆盖，合成收尾也就无法准确对应。
            key = (str(e.get("worker_id", "unknown")),
                   str(e.get("boot_id", "legacy")),
                   str(p.get("code")), p.get("round"), p.get("idx"))
            if ev == "session_start":
                open_pairs[key] = float(e.get("ts") or 0)
            else:
                open_pairs.pop(key, None)
        closed = set()
        try:
            with open(marker, encoding="utf-8") as f:
                raw = json.load(f) or []
            closed = {str(item) for item in raw if item}
        except (OSError, ValueError, TypeError):
            pass
        now = time.time()
        stale = []
        for key, ts in open_pairs.items():
            if now - ts <= grace:
                continue
            digest = hashlib.sha256(
                json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if digest not in closed:
                stale.append((key, digest))
        for key, digest in stale:
            obs.emit("session_end", layer="driver",
                     payload={"code": key[2], "round": key[3], "idx": key[4],
                              "worker_id": key[0], "boot_id": key[1],
                              "turns": 0, "flags": 0, "infra_blocked": False,
                              "synthetic": True})
            closed.add(digest)
        if stale:
            try:
                tmp = marker + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(sorted(closed)[-4096:], f, ensure_ascii=False)
                os.replace(tmp, marker)
            except OSError:
                pass
            log.info("orphan self-heal: closed %d dangling session(s) from killed runs",
                     len(stale))


def _load_priority(workdir: str, wid: int) -> list[str]:
    """读取优先任务文件（``unique_code|worker_id[|task_epoch]``）。

    只返回分配给本 worker 的优先题 code。
    """
    path = os.path.join(workdir, "priority.txt")
    epoch = _current_task_epoch()
    codes: list[str] = []
    seen: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                code = parts[0].strip()
                if not code:
                    continue
                entry_epoch = parts[2].strip() if len(parts) > 2 else ""
                if epoch and entry_epoch != epoch:
                    # Do not revive an unbound legacy queue entry once a task
                    # generation is known.  This protects same-code task
                    # resets without reading or retaining any task material.
                    continue
                if len(parts) > 1 and parts[1].strip():
                    try:
                        assigned = int(parts[1].strip())
                        # 历史控制台曾把优先题轮流派给 0/1/2；0 是 monitor，
                        # 永远不会消费队列。兼容已写入的 |0 记录：在两个求解
                        # worker 之间按题号稳定分派，既不会遗漏也不会双解。
                        worker_count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
                        if assigned == 0 and worker_count >= 3 and wid >= 1:
                            assigned = 1 + (zlib.crc32(code.encode("utf-8")) % (worker_count - 1))
                        if assigned != wid:
                            continue
                    except ValueError:
                        pass
                code_key = _priority_code_key(code)
                if code_key and code_key not in seen:
                    codes.append(code)
                    seen.add(code_key)
    except OSError:
        pass
    return codes


def _apply_priority(challenges: list, workdir: str, wid: int) -> list:
    """把分配给本 worker 的优先题提到最前（未完成的）。"""
    prio = _load_priority(workdir, wid)
    if not prio:
        return challenges
    prio_set = {_priority_code_key(code) for code in prio}
    early = [c for c in challenges if _priority_code_key(c.unique_code) in prio_set]
    rest = [c for c in challenges if _priority_code_key(c.unique_code) not in prio_set]
    if early:
        log.info("priority queue for worker %d: %s", wid,
                 ",".join(c.unique_code for c in early))
    return early + rest


def _claim_priority(shard: list, all_challenges: list, workdir: str, wid: int) -> list:
    """分片后认领派单题：本 worker 的优先题若不在分片里，强制加入最前。

    网页「Agent 解此题」把题派给指定 worker，但分片按序号取模，
    派单题可能落在其它 worker 的分片 —— 这里确保被派单的 worker 能处理它。
    """
    prio = _load_priority(workdir, wid)
    if not prio:
        return shard
    prio_set = {_priority_code_key(code) for code in prio}
    have = {_priority_code_key(c.unique_code) for c in shard}
    claimed = [c for c in all_challenges
               if _priority_code_key(c.unique_code) in prio_set
               and _priority_code_key(c.unique_code) not in have]
    if claimed:
        log.info("claim priority for worker %d: %s", wid,
                 ",".join(c.unique_code for c in claimed))
    return claimed + shard


def _worker_concurrency() -> int:
    """
    单容器内的 Pi Agent 并发数。
    worker 模式（count>1）下固定 1（一个容器一个 pi 进程，一次一道题）；
    单容器模式可用 ADAPTER_WORKER_CONCURRENCY 调整（默认 1）。
    """
    count = int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1")
    if count > 1:
        return 1
    try:
        return max(1, int(os.environ.get("ADAPTER_WORKER_CONCURRENCY", "1") or "1"))
    except ValueError:
        return 1


def _vpn_readiness_path(workdir: str | None = None) -> str:
    return os.path.join(workdir or os.getenv("ADAPTER_WORKDIR", "/work"),
                        ".vpn-ready")


def _publish_vpn_readiness(ready: bool, *, reason: str = "", workdir: str | None = None) -> None:
    """Publish monitor-only VPN readiness as small, answer-free metadata.

    Docker health is intentionally a process-liveness signal.  The console
    needs a separate target-readiness signal, but it must not receive client
    IPs, credentials, or any challenge output.  Only worker-1 calls this
    helper; consumers treat an absent/stale marker as not ready.
    """
    path = _vpn_readiness_path(workdir)
    payload = {
        "ready": bool(ready),
        "ts": time.time(),
        "reason": str(reason or "")[:80],
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _start_vpn_watchdog(client, *, interval: int = 60,
                       failures_before_alert: int = 3,
                       publish_readiness: bool = False,
                       workdir: str | None = None):
    """
    VPN 断线看门狗（后台线程）—— **只告警，永不退出进程**。

    ⚠️ 这段 docstring 曾经写着「连续失败 N 次 → 退出进程，由容器 restart 策略
    自动重启重连 VPN」——**那是错的，而且恰好就是已经修掉的那个 bug 的描述**。
    worker-1 是 worker-2/3 的共享 netns 提供者（compose 里
    network_mode: service:worker-1），它一退出/重启，两个解题 worker 会残留在
    只有 lo 的孤立命名空间里（无 eth0、无路由、DNS 全崩）。所以这里的阈值
    **只用来升级告警措辞，绝不触发退出**。

    参数叫 failures_before_alert 而不是 …_exit，就是为了让**签名本身不再说谎**：
    旧名字每个调用点都写着 failures_before_exit=3，读代码的人会以为真有退出，
    甚至可能"顺手"把退出逻辑加回去 —— 那等于亲手把单点炸掉。

    对偶设计（刻意非对称，别去"统一"）：
      提供者 _start_vpn_watchdog  → 到阈值只告警，**绝不退出**
      消费者 _start_netns_watchdog → 到阈值 os._exit(3)，**必须退出**
    同一个网络故障，退出对前者是灾难（级联断裂），对后者是唯一恢复手段
    （重启才能重新 join worker-1 的 netns）。

    另：本函数**不检查后端是否实现 check_vpn**（旧 docstring 说
    「后端无 check_vpn 时静默退出」，两个词都错）。缺该方法时
    client.check_vpn(...) 抛 AttributeError，被下面的通用 except 捕获，
    按「视为通过」处理（fails=0）并每轮打一条 warning 继续循环 ——
    既不静默，也不退出。
    """
    def _loop():
        fails = 0
        while True:
            time.sleep(interval)
            try:
                vpn = client.check_vpn(timeout=8)
                if vpn.ok:
                    fails = 0
                    if publish_readiness:
                        _publish_vpn_readiness(True, reason="ok", workdir=workdir)
                    continue
                fails += 1
                if publish_readiness:
                    _publish_vpn_readiness(False, reason=str(getattr(vpn, "status", "not_ok")),
                                           workdir=workdir)
                log.warning("VPN check failed (%d/%d): status=%r",
                            fails, failures_before_alert, vpn.status)
            except VpnCheckError as e:
                fails += 1
                if publish_readiness:
                    _publish_vpn_readiness(False, reason=getattr(e, "reason", "error"),
                                           workdir=workdir)
                log.warning("VPN check failed (%d/%d): reason=%s",
                            fails, failures_before_alert, getattr(e, "reason", "unknown"))
            except Exception as e:
                log.warning("VPN watchdog check error: %s (treated as pass)", e)
                fails = 0
                if publish_readiness:
                    # Backends without a VPN endpoint cannot prove readiness;
                    # fail closed instead of turning an implementation error
                    # into a green target-readiness indicator.
                    _publish_vpn_readiness(False, reason="check_error", workdir=workdir)
                continue
            if fails >= failures_before_alert:
                fails = 0  # 重置计数，持续告警而不退出
                log.error(
                    "VPN 断线超过 %d 次仍未恢复 — 不退出进程。"
                    "内网访问由宿主 tun0 + Docker NAT 提供，worker-1 作为共享 netns 提供者"
                    "必须保持存活；退出/重启会孤立 worker-2/3 的共享网络导致全队 DNS 崩溃。"
                    "（历史 bug：此处曾被误解为可自动重连 VPN，实际只会触发级联重启）",
                    failures_before_alert,
                )

    th = threading.Thread(target=_loop, daemon=True, name="vpn-watchdog")
    th.start()
    log.info("VPN watchdog started (interval=%ds, 到 %d 次失败升级告警；"
             "本看门狗永不退出进程)", interval, failures_before_alert)


def _start_netns_watchdog(*, interval: int = 45, failures_before_exit: int = 3,
                          required_iface: str = "eth0"):
    """共享 netns 自愈看门狗（worker-2/3 专用，monitor 不启用）。

    worker-2/3 通过 network_mode: service:worker-1 共享 worker-1 的网络命名空间。
    当 worker-1 被重启/重建时，Docker 不会自动把依赖容器重挂到新 netns，
    worker-2/3 会残留在只有 lo 的孤立命名空间（无 eth0、无路由、DNS 全崩），
    表现为 NameResolutionError 刷屏且无法自行恢复 —— 唯一的恢复手段是退出进程，
    让 restart: on-failure 重建容器并重新 join worker-1 的 netns。
    """
    def _has_net() -> bool:
        try:
            if not os.path.exists(f"/sys/class/net/{required_iface}"):
                return False
            # 有接口但 operstate 非 up 同样视为不可用
            with open(f"/sys/class/net/{required_iface}/operstate") as f:
                if f.read().strip() != "up":
                    return False
            # 必须存在默认路由（孤立的 netns 可能残留接口但无路由）
            with open("/proc/net/route") as f:
                for line in f:
                    cols = line.split()
                    if len(cols) >= 2 and cols[1] == "00000000":
                        return True
            return False
        except OSError:
            return False

    def _loop():
        fails = 0
        while True:
            time.sleep(interval)
            try:
                if _has_net():
                    fails = 0
                    continue
                fails += 1
                log.warning("网络自愈检查 (%d/%d): %s 缺失或默认路由丢失，"
                            "疑似 worker-1 netns 已重建而本容器未重挂",
                            fails, failures_before_exit, required_iface)
                if fails >= failures_before_exit:
                    log.error("网络丢失连续 %d 次 — 退出进程触发 docker 重启以重挂 worker-1 共享 netns...",
                              failures_before_exit)
                    os._exit(3)
            except Exception as e:
                log.warning("网络自愈检查异常: %s (视为通过)", e)
                fails = 0

    th = threading.Thread(target=_loop, daemon=True, name="netns-watchdog")
    th.start()
    log.info("netns 自愈看门狗启动 (interval=%ds, %s 丢失 %d 次后退出)",
             interval, required_iface, failures_before_exit)


def _wait_and_dispatch(*, reason: str, client, ctrl, solver, verifier, stoploss,
                       stop_event, only, caps, obs) -> None:
    """本 worker 当前无题可做时：等新题出现并自动派发（B30）。

    原先这几处写法是 `_idle_loop(reason=...)` + `return`，而 `_idle_loop` 的循环体
    只有 _beat() + sleep + 打日志，**从头到尾没有查询过平台** —— 它的 docstring 却
    写着「每 60s 检查一次是否有新题」，正是这句与实现不符的描述让这个死胡同一直
    没被察觉。后果：worker 只要在启动瞬间碰上「平台列表为空 / 能力过滤后为空 /
    本片为空 / 派单题不存在」，进到这里就再也不会接新题；而心跳照常刷新，
    healthcheck 判它健康、restart 策略也不拉它 —— 只能人工 touch .reload.widN。

    修法用本文件里早就有的正确路径：auto_dispatch_loop(seed=[])。空 seed 会让它
    first_pass=False → 立即拉平台，并用**完全相同**的过滤链重新 collect；无题时
    走 _idle_tick 保持心跳，有题则直接进入 schedule_rounds。不重复实现任何过滤。

    未配置 ADAPTER_CAPABILITIES 的同质扩展同样可安全进入：collect() 会应用
    _worker_shard，因此不能再退回只写心跳、不查询平台的旧死循环。
    """
    log.info("no work for this worker (%s) — 转入派发循环轮询新题", reason)
    auto_dispatch_loop(client, seed=[], ctrl=ctrl, solver=solver, verifier=verifier,
                       stoploss=stoploss, stop_event=stop_event, only=only,
                       caps=caps, obs=obs)


def _idle_loop(stop_event=None, *, reason: str = "no work"):
    """常驻等待循环（**不查询平台**，仅保活）。

    ⚠️ 本函数不会发现新题。B30 之前它被当成「等新题」用，四处调用点因此成了
    死胡同（详见 _wait_and_dispatch 的说明）。现在只作为「无能力配置」这一支
    的保活兜底，以及 _finish_and_idle（终态上报后，当前无调用者）的落点。
    要等新题请用 _wait_and_dispatch / _await_task。
    仅当收到外部停止信号（SIGTERM 等）才退出。
    """
    log.info("idle: %s — 常驻等待（保持 VPN/心跳，不退出）", reason)
    idle = 0
    while True:
        _beat()
        time.sleep(60)
        idle += 1
        log.info("idle keepalive: %d min (%s)", idle, reason)
        if stop_event is not None and stop_event.is_set():
            log.info("idle exit: stop signal received")
            return


def _idle_tick(duration: int = 60, reason: str = "no work"):
    """单次常驻等待 tick：保活 + 休眠 + 日志。供自动派发循环每轮调用。"""
    _beat()
    time.sleep(duration)
    log.info("auto-dispatch keepalive: %ds (%s)", duration, reason)


# B15：重载请求标记 —— watcher 检测到 .reload.widN 即置位，供进程退出码判定
# （主线程可能先于 watcher 的 os._exit(86) 从 main() 正常返回）。
_RELOAD_REQUESTED = threading.Event()


def _reload_watch(stop_event):
    """协作式热重载：外部 touch work/.reload.wid{N} → 收尾后 exit 86 重启。

    替代外部 docker restart 加载 bind-mount 新代码：后者的 SIGTERM 投递
    有 1-2s 延迟，驱动在该间隙认领并启动下一题实例（实测两次投递
    延迟间隙各留下一例孤儿）——外挂 watcher 无论轮询多快都赢不过投递延迟。
    这里由驱动自己保证安全：先置 stop_event（所有认领/开新 visit 的路径
    都先查它，从此不再开新 visit），再等自身状态文件 solving_active=False
    （终态持久化全部完成后）才以 exit 86 退出——on-failure 重启策略自动
    加载新代码。零丢失、零孤儿。文件先删再退，防重启后残留造成退出循环。
    """
    path = os.path.join(os.getenv("ADAPTER_WORKDIR", "/work"),
                        f".reload.wid{_worker_id()}")
    while True:
        time.sleep(5)
        try:
            if not os.path.exists(path):
                continue
            os.remove(path)
        except OSError:
            continue
        _RELOAD_REQUESTED.set()
        log.info("[reload] 热重载请求 — 停止认领新题，等待当前 visit 收尾")
        stop_event.set()
        deadline = time.monotonic() + 4000   # 上限≈最长单 pi 会话+收尾
        while time.monotonic() < deadline:
            try:
                with open(_status_path(), encoding="utf-8") as f:
                    if json.load(f).get("solving_active") is False:
                        break
            except Exception:
                pass
            time.sleep(2)
        log.info("[reload] 会话边界已收尾 — 退出进程加载新代码 (exit 86)")
        os._exit(86)


def _task_finished(text: str) -> bool:
    """判断平台是否已判定任务结束（仅显式终态；裸 409 是瞬时冲突不算）。"""
    t = (text or "").lower()
    return ("already finished" in t or "invalid_state" in t or "task finished" in t
            or "task ended" in t)


def _finish_and_idle(obs, reason: str):
    """终态上报 + 常驻等待新任务（不退出，保持 VPN/心跳）。"""
    if obs is not None:
        try:
            obs.emit("run_end", layer="driver", payload={"reason": reason})
            obs.close()
        except Exception:
            pass
    log.info("=== %s — 常驻等待新任务（不退出、不销毁）===", reason)
    _idle_loop(reason=reason)


def _challenge_fingerprint(challenges) -> tuple:
    """任务列表的稳定指纹，用于区分"平台仍是同一任务"和真正的新任务。"""
    rows = []
    for ch in challenges or []:
        rows.append((
            str(getattr(ch, "unique_code", "")),
            bool(getattr(ch, "is_completed", False)),
            int(getattr(ch, "flag_count", 0) or 0),
            str(getattr(ch, "difficulty", "") or ""),
            int(getattr(ch, "total_score", 0) or 0),
        ))
    return tuple(sorted(rows))


def _await_task(client, *, poll: int, stop_event, beat_cb=None,
                known_fingerprint: tuple | None = None) -> list | None:
    """终态后的复查等待：周期重拉平台，直到出现有效任务/题目。

    - 仅当题目集合/状态发生实质变化时才返回列表给调用方重置预算
    - 终态标记 / 瞬态错误 → 继续等待（不永久停死，保持心跳）
    - 收到停止信号 → 返回 None
    保证"全自动派发"在任何终态下都能自愈（任务结束后出新题自动接）。
    """
    log.info("await-task: 周期复查平台，等待新任务/新题（每 %ds）", poll)
    while True:
        if beat_cb is not None:
            try:
                beat_cb()
            except Exception:
                pass
        try:
            fresh = client.list_challenges()
            if isinstance(fresh, (list, tuple)) and len(fresh) > 0:
                fresh = list(fresh)
                if (known_fingerprint is None
                        or _challenge_fingerprint(fresh) != known_fingerprint):
                    log.info("await-task: 发现新任务/题目变化（%d 题）— 恢复自动派发",
                             len(fresh))
                    return fresh
                log.info("await-task: 平台仍是同一任务 — 保持当前预算与重试状态")
            # 空列表（无题）→ 继续等
            log.info("await-task: 平台正常但无题目 — 继续等待")
        except Exception as e:
            text = str(e)
            if _task_finished(text):
                log.info("await-task: 平台仍显示任务结束 — 继续等待")
            else:
                log.warning("await-task: list_challenges failed (%s) — 继续等待",
                            str(e)[:100])
        if stop_event is not None and stop_event.is_set():
            log.info("await-task: STOP signal — 退出")
            return None
        time.sleep(poll)


def _monitor_loop(*, raw_client=None, watch_dir: str = "", stop_event=None):
    """worker-1 监控模式：只维持 VPN + 心跳，监控 worker-2/3 状态。

    - 不参与做题（能力为空的调度者角色）
    - 周期性读取 work/status/worker-1.json、worker-2.json 汇总到本 worker 状态
    - 常驻不退出（VPN 共享网络提供者必须保持存活）
    """
    log.info("=== worker-1 进入监控模式（VPN + 监控 + 他管，不参与做题）===")
    if not watch_dir:
        watch_dir = os.getenv("ADAPTER_WORKDIR", "/work")

    # ── B49「他管」层 ─────────────────────────────────────
    # worker-1 以监督者身份督促解题 worker。能力面被架构限死为一个动作：
    #     touch <workdir>/.reload.wid{N}   （协作式热重载）
    # worker-1 容器内没有 docker socket 也没有 docker 二进制，物理上不可能
    # 重启任何容器 —— 危险能力从架构上根除，而不是靠纪律。
    # 完整设计说明见 drivers/w1_supervisor.py 顶部注释；改之前先读它。
    # 模块加载失败只降级（他管停用），绝不拖垮 VPN 维持与状态汇总。
    _sup = None
    try:
        import w1_supervisor as _sup  # noqa: F401
        log.info("他管层已加载：卡死判定 → 协作式热重载（绝不重启容器）")
    except Exception as e:
        log.warning("他管层加载失败，本次仅做监控不做督促: %s", e)
    while True:
        _beat()
        # 汇总 worker-2/3 状态（只读，供网页/上级监控）
        summary = {"workers": {}}
        try:
            for wid in ("0", "1", "2"):
                p = os.path.join(watch_dir, "status", f"worker-{wid}.json")
                if os.path.isfile(p):
                    with open(p, encoding="utf-8") as f:
                        summary["workers"][wid] = json.load(f)
        except Exception as e:
            log.debug("monitor read status: %s", e)
        # B49「他管」：判定解题 worker 卡死 → 请求协作式热重载。
        # 放在写 _monitor.json 之前，动作才能一并进快照给网页看。
        if _sup is not None:
            try:
                _sup.mark_driver_side(watch_dir)   # 与 standalone 进程互认
                acts = _sup.supervise_tick(watch_dir, log)
                if acts:
                    summary["supervise_actions"] = acts
            except Exception as e:
                log.debug("supervise tick skipped: %s", e)
        try:
            with open(os.path.join(watch_dir, "_monitor.json"), "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        time.sleep(30)
        if stop_event is not None and stop_event.is_set():
            log.info("monitor exit: stop signal received")
            return


def _instance_stamp_path(workdir: str) -> str:
    """记录本题当前实例的目标地址（用于识别平台重发的新实例）。"""
    return os.path.join(workdir, "_instance.json")


def _workdir_epoch_path(workdir: str) -> str:
    return os.path.join(workdir, ".task-epoch.json")


# One live ``solve_one`` visit gets a cryptographically-unpredictable trace
# scope.  Transcript filenames are part of the *provenance boundary*, not a
# convenience for log browsing: an old visit for the same challenge code must
# never prove that a new instance downloaded or executed something.  The
# scope is kept in process memory and is also recorded as answer-free metadata
# in _instance.json for diagnosis.  It is deliberately not derived from a
# challenge code, target address, or task epoch, all of which can repeat.
_TRACE_SCOPE_RX = re.compile(r"^[0-9a-f]{32}$")
_TRACE_NAME_RX = re.compile(
    r"^(?P<scope>[0-9a-f]{32})--r(?P<round>\d+)--s(?P<session>\d+)--.+\.jsonl$")


def _new_trace_scope() -> str:
    return secrets.token_hex(16)


def _trace_filename(trace_scope: str, round_idx: int, session_idx: int) -> str:
    """Return a sortable, scope-bound transcript filename.

    Round/session sequence numbers, not filesystem mtimes, define evidence
    order.  A process restart or copy can alter mtimes; it must not rewrite
    the order in which a candidate first appeared.
    """
    return (f"{trace_scope}--r{max(0, int(round_idx)):06d}"
            f"--s{max(0, int(session_idx)):06d}--{_BOOT_STAMP}.jsonl")


def _scoped_transcript_paths(workdir: str, trace_scope: str,
                             transcript_path: str | None = None) -> list[str]:
    """Return only driver transcript files for one active trace scope.

    No scope means no transcript evidence.  Failing closed here is important:
    a best-effort ``glob`` fallback would quietly reintroduce cross-instance
    history into the evidence gate when a caller forgets to pass its scope.
    """
    if not _TRACE_SCOPE_RX.fullmatch(str(trace_scope or "")):
        return []
    root = os.path.join(workdir, "_transcripts")
    rows: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    try:
        entries = list(os.scandir(root))
    except OSError:
        entries = []
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            continue
        match = _TRACE_NAME_RX.fullmatch(entry.name)
        if not match or match.group("scope") != trace_scope:
            continue
        path = entry.path
        seen.add(os.path.abspath(path))
        rows.append((int(match.group("round")), int(match.group("session")), path))
    # A freshly opened active file may not yet have appeared in a directory
    # scan on unusual filesystems.  It is admissible only when its own name
    # proves that it belongs to the same scope.
    if transcript_path and os.path.isfile(transcript_path):
        name = os.path.basename(transcript_path)
        match = _TRACE_NAME_RX.fullmatch(name)
        absolute = os.path.abspath(transcript_path)
        if (match and match.group("scope") == trace_scope and absolute not in seen):
            rows.append((int(match.group("round")), int(match.group("session")),
                         transcript_path))
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    return [path for _round, _session, path in rows]


# ── [B52] 合规打码：两条清理路径共用的口径 ────────────────────────────
def _flag_plaintext_rx(workdir: str):
    """[B52] 本题的「答案明文」正则 —— **薄包装**，定义已搬到 adapter/compliance.py。

    为什么搬：打码现在有两条消费路径 —— 本文件的清理路径，与控制台观察面板的
    **只读展示**路径（`.heimdall.json` 的 why/evidence 是工具输出摘录，可能含
    明文）。两条路径各写一份正则迟早漂移，正是 B52 的根因。现在一处定义两处共用。

    本函数保留原签名与逐字不变的行为，调用点 `_scrub_flag_plaintext` 无需改动。
    """
    from ghost_worker.adapter.compliance import flag_plaintext_rx
    return flag_plaintext_rx(workdir)


def _without_flag_candidates_for_blackboard(workdir: str, output: str) -> str:
    """Remove candidate envelopes before prompt-facing fact extraction.

    Scoped transcripts retain the original tool output for provenance.  The
    blackboard deliberately does not: before platform confirmation a candidate
    must not become a durable fact or reset a no-progress window.
    """
    text = str(output or "")
    try:
        return _flag_plaintext_rx(workdir).sub("[REDACTED-FLAG]", text)
    except Exception:
        return text


class _ProgressEvidenceGate:
    """Keep StopLoss progress tied to this task's actual evidence sources.

    Blackboard extraction is intentionally useful but shallow: an IP address or
    service word in *any* local output looks like a new fact.  Treating a
    readback of ``MEMORY.md``, a transcript, or a generated work file as fresh
    reconnaissance therefore lets repeated local bookkeeping reset the
    three-session StopLoss window.  This small, visit-local trace tracker uses
    the same source rules as flag provenance, but only decides whether a tool
    output may be fed into the blackboard.

    It is not persisted.  A new target instance builds a new gate, so no path,
    artifact, or response lineage can cross a task-instance boundary.
    """

    _SUCCESS_KEY = "__tsecbench_execution_ok"

    def __init__(self, policy: FlagEvidencePolicy):
        self.policy = policy
        self.tainted_inputs: set[str] = set()
        self.authored_paths: set[str] = set()
        self.downloaded_artifacts: set[str] = set()
        self.derived_artifacts: set[str] = set()
        self.tainted_artifacts: set[str] = set()
        self.response_artifacts: set[str] = set()
        self.tainted_response_artifacts: set[str] = set()
        self.script_bodies: dict = {}

    @staticmethod
    def _command(tool, args) -> str:
        if isinstance(args, dict):
            value = args.get("command", args.get("cmd", ""))
        else:
            value = args or tool
        value = str(value or "").strip()
        return "" if value in {"", "{}", "[]", "None", "()"} else value

    @classmethod
    def _completed_successfully(cls, args) -> bool:
        # Partial output is useful for the human transcript, but a timed-out
        # command cannot prove that an observation came from the current target
        # or that a downloader replaced an old local response.
        return isinstance(args, dict) and args.get(cls._SUCCESS_KEY) is True

    def observe(self, tool, args, output) -> str:
        """Return the qualified source kind for one ordered tool event.

        The tracker consumes blank events too.  In particular, ``curl -o``
        normally has no stdout; omitting it would make the following direct
        ``cat response`` indistinguishable from a stale local file.
        """
        command = self._command(tool, args)
        if not command:
            return ""

        # Keep provenance state even for a failed/blank event.  A failed write
        # can still have damaged an official input or a previously trusted
        # artifact, while only a successful command may establish new lineage.
        try:
            current_authored = authored_paths_from_call(tool, args)
        except Exception:
            current_authored = set()
        try:
            self.script_bodies.update(collect_script_bodies(args))
            self.tainted_inputs.update(local_input_mutated(command, self.policy))
            prior_artifacts = self.downloaded_artifacts | self.derived_artifacts
            self.tainted_artifacts.update(tainted_target_artifacts(
                command, self.policy, prior_artifacts))
            self.tainted_response_artifacts.update(tainted_target_artifacts(
                command, self.policy, self.response_artifacts))
            if self._completed_successfully(args):
                self.derived_artifacts.update(derived_target_artifacts(
                    command, self.policy,
                    prior_artifacts - self.tainted_artifacts,
                    authored_paths=self.authored_paths))
                self.downloaded_artifacts.update(downloaded_target_artifacts(
                    command, self.policy, output=str(output or "")))
                self.response_artifacts.update(downloaded_target_response_artifacts(
                    command, self.policy, completed_success=True,
                    authored_paths=self.authored_paths))
        except Exception:
            # A provenance parser is a guard, never a source of solver failure.
            # If it cannot classify an event, fail closed for StopLoss progress.
            self.authored_paths.update(current_authored)
            return ""

        try:
            if not self._completed_successfully(args) or not str(output or "").strip():
                return ""
            effective_authored = self.authored_paths | current_authored
            if is_task_remote_command(command, self.policy, self.script_bodies):
                return "current_target"
            if is_remote_response_artifact_command(
                    command, self.policy,
                    self.response_artifacts - self.tainted_response_artifacts,
                    authored_paths=effective_authored):
                return "current_target_response"
            if is_local_evidence_command(
                    command, self.policy,
                    tainted_inputs=self.tainted_inputs,
                    authored_paths=effective_authored,
                    script_bodies=self.script_bodies,
                    downloaded_artifacts=self.downloaded_artifacts,
                    tainted_artifacts=self.tainted_artifacts,
                    derived_artifacts=self.derived_artifacts):
                return "task_artifact"
            return ""
        finally:
            # Current writes become prior writes only after this event has been
            # classified.  This preserves a successful `curl -o response`
            # registration while preventing later generated files from being
            # treated as task inputs.
            self.authored_paths.update(current_authored)


def _observe_qualified_tool_facts(
        board: Blackboard,
        gate: _ProgressEvidenceGate,
        workdir: str,
        tool,
        args,
        output,
        *,
        iter: int,
) -> int:
    """Extract blackboard facts only from qualified current-task evidence."""
    if not gate.observe(tool, args, output):
        return 0
    return board.observe(
        tool, args or {}, _without_flag_candidates_for_blackboard(workdir, output),
        iter=iter)


def _scrub_flag_plaintext(workdir: str, code: str, *, verbose: bool = True) -> int:
    """[B52] 给「会回灌进下一场」的三个文本文件打码，返回打码文件数。

    覆盖面刻意只有这三个 —— 它们是被**喂给下一步**的，明文残留要害最高：
      · MEMORY.md          每场作为 prior_memory 显式注入
      · _blackboard.json   黑板事实库，注入下一场 prompt
      · tried_commands.md  命令纪要，同上
    账本 `.unverified_flags` 自己**不打码** —— 它就是要留着 body（B45 语义）。
    """
    n = 0
    try:
        rx = _flag_plaintext_rx(workdir)
    except Exception:
        return 0
    for fn in ("MEMORY.md", "_blackboard.json", "tried_commands.md",
               ".heimdall.json"):
        fp = os.path.join(workdir, fn)
        try:
            with open(fp, encoding="utf-8") as fh:
                s0 = fh.read()
            s2 = rx.sub("[REDACTED-FLAG]", s0)
            if s2 != s0:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(s2)
                n += 1
                if verbose:
                    log.info("  [compliance] scrubbed flag plaintext from %s (%s)", fn, code)
        except (OSError, UnicodeDecodeError):
            pass
    return n


def _scrub_live_blackboard(board, workdir: str, code: str) -> int:
    """Scrub flag text from the in-memory blackboard before its next save.

    Scrubbing only ``_blackboard.json`` at a session boundary is insufficient:
    ``Blackboard.observe`` may append a later fact and serialize the old in-
    memory flag fact back to disk.  Replace matching content/source fields in
    the live object as well, while retaining the fact kind as a non-sensitive
    progress marker.  The scoped transcript remains the sole provenance store.
    """
    if board is None:
        return 0
    try:
        rx = _flag_plaintext_rx(workdir)
    except Exception:
        return 0
    changed = 0
    retained = []
    for fact in list(getattr(board, "facts", []) or []):
        # Legacy blackboards may still contain candidate facts from an older
        # build.  They are neither durable platform progress nor useful prompt
        # context, so remove them rather than redact/re-key them into a fresh
        # "new fact" on the next session.
        if getattr(fact, "kind", "") == "flag":
            changed += 1
            continue
        for attr in ("content", "source"):
            value = getattr(fact, attr, "")
            if not isinstance(value, str) or not value:
                continue
            redacted = rx.sub("[REDACTED-FLAG]", value)
            if redacted != value:
                setattr(fact, attr, redacted)
                changed += 1
        retained.append(fact)
    if changed:
        try:
            board.facts = retained
            board._seen = {
                f"{getattr(f, 'kind', '')}:{getattr(f, 'content', '')}"
                for f in getattr(board, "facts", [])
            }
            board._save()
        except Exception:
            log.warning("[continuation] live blackboard scrub save failed for %s",
                        code, exc_info=True)
    return changed


def _remove_core_dumps(workdir: str) -> int:
    """[B52] 清掉崩溃转储 `core.<pid>`，返回删除数。

    转储是进程内存快照，会内嵌当时存活的一切字符串 —— 实测 某题积了 9 个
    ×312KB，其中 7 个含账本候选 flag body。它不参与任何流程（不是取证链的输入、
    也不进 artifacts），属**纯垃圾**且无上限堆积，一律清。限定 core.<数字> 后缀，
    避免误删同名前缀的正常文件。
    """
    import glob as _glob
    n = 0
    for p in _glob.glob(os.path.join(workdir, "core.[0-9]*")):
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    return n


def _purge_stale_solutions(workdir: str, code: str, targets: list,
                           *, task_epoch: str = "", trace_scope: str = "") -> None:
    """为新的一次平台实例建立干净的 solver 工作区。

    旧实现只在 container_addr 变化时清理，而且只清理少数根文件；同地址重发或
    未入账访问会把 MEMORY/脚本/响应/转录带进下一题，形成可复用的定向解法。
    现在每次 ``solve_one`` 开始都清空 solver 可见产物。跨 Pi session 的连续性
    仍由同一次 ``solve_one`` 内的 MEMORY/blackboard 提供；跨平台实例不再复用。
    Same-epoch visits preserve answer-free stoploss and opaque confirmed-count
    state.  A changed epoch preserves neither, so a newly issued task with the
    same code never inherits the old task's budget or partial progress.
    """
    stamp_p = _instance_stamp_path(workdir)
    new = sorted([str(t) for t in (targets or [])])

    # 检测靶标是否重启（targets 变化）——靶标重启后旧的 MEMORY/artifacts
    # 指向旧靶标状态，不能保留，否则 Agent 会被过时信息误导。
    _old_targets = []
    try:
        with open(stamp_p, encoding="utf-8") as _sf:
            _old_targets = sorted(
                [str(t) for t in (json.load(_sf) or {}).get("targets", []) or []])
    except (OSError, ValueError, TypeError):
        pass
    _target_changed = bool(_old_targets and new and _old_targets != new)

    # 清理整个单题目录（目录本身由调用方保留）。同一 task epoch 仅保留
    # 止损计数、opaque confirmed progress 与 epoch marker；其它任何
    # 文件都可能包含题面、答案、利用脚本或远端响应，不能依赖文件名白名单。
    # 这里限定在已经由 _safe_code 生成的单题 workdir 内，不触碰共享 work/status。
    local_epoch = ""
    try:
        with open(_workdir_epoch_path(workdir), encoding="utf-8") as fh:
            local_epoch = str((json.load(fh) or {}).get("task_epoch", "") or "")
    except (OSError, ValueError, TypeError):
        pass
    same_epoch = bool(task_epoch and local_epoch == task_epoch)
    preserved = ({".stoploss.json", ".confirmed-progress.json", ".task-epoch.json",
                  _CONTINUATION_FILENAME, _CONTINUATION_FILENAME + ".lock"}
                 if same_epoch else set())
    # 同一 task epoch 且靶标未变 → 保留 Agent 工作成果，让未完成的题下次 visit
    # 同一 task epoch 内仅保留框架元数据（止损/进度/epoch marker）。
    # MEMORY.md、tried_commands.md、artifacts/ 等携带赛题特定知识和解法产物，
    # 跨场保留属于"违规使用外部历史答题记忆"和"非通用定向解法"——平台合规要求
    # 每次 visit 从干净状态开始，二进制从靶标重新下载、分析从头做。
    if same_epoch and not _target_changed:
        preserved.update({"CLAUDE.md", ".hallucination.json",
                          ".unverified_flags", ".rejected_flags",
                          ".unverified_flags.lock", ".rejected_flags.lock",
                          ".unverified_flags.hallucination.lock"})
    try:
        for name in os.listdir(workdir):
            if name in preserved:
                continue
            path = os.path.join(workdir, name)
            try:
                if os.path.islink(path) or os.path.isfile(path):
                    os.unlink(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                log.warning("  [compliance] unable to remove stale %s", path)
    except OSError:
        pass

    try:
        with open(stamp_p, "w", encoding="utf-8") as f:
            # This is intentionally metadata only.  The random scope ties
            # transcript evidence to this live platform instance without
            # retaining prompts, commands, outputs, or candidate values.
            json.dump({"code": code, "targets": new,
                       "trace_scope": trace_scope if _TRACE_SCOPE_RX.fullmatch(trace_scope) else ""},
                      f, ensure_ascii=False)
    except Exception:
        pass
    if task_epoch:
        try:
            with open(_workdir_epoch_path(workdir), "w", encoding="utf-8") as f:
                json.dump({"task_epoch": task_epoch, "updated_at": time.time()}, f,
                          ensure_ascii=False, sort_keys=True)
        except OSError:
            pass
    log.info("  [compliance] %s 新实例工作区已隔离（targets=%d, epoch=%s）",
             code, len(new), task_epoch[:12] if task_epoch else "none")


def _preflight_task_epoch_isolation(workdir: str, code: str,
                                    *, task_epoch: str = "") -> bool:
    """Discard a prior task's per-code state before consulting StopLoss.

    ``_purge_stale_solutions`` normally runs after ``start_challenge`` because
    it needs the freshly allocated target address and trace scope.  That is too
    late for a reused challenge code whose old ``.stoploss.json`` is terminal:
    the entry guard would return ``dropped`` before the normal purge is ever
    reached.  Inspect only the answer-free epoch marker here.  On a mismatch,
    use the same scoped purge with no target data; the normal post-start purge
    will immediately replace the temporary stamp with the real instance scope.

    Returns whether a stale namespace was removed.  A missing marker is also
    stale: it cannot establish that its retained counters belong to this task.
    """
    epoch = str(task_epoch or "")
    if not epoch:
        # Legacy/direct-call mode has no trustworthy task boundary.  Preserve
        # existing state rather than unexpectedly granting fresh budgets.
        return False
    local_epoch = ""
    try:
        with open(_workdir_epoch_path(workdir), encoding="utf-8") as fh:
            local_epoch = str((json.load(fh) or {}).get("task_epoch", "") or "")
    except (OSError, ValueError, TypeError):
        pass
    if local_epoch == epoch:
        return False
    _purge_stale_solutions(workdir, code, [], task_epoch=epoch, trace_scope="")
    log.info("  [compliance] %s preflight cleared stale task epoch (%s -> %s)",
             code, local_epoch[:12] or "none", epoch[:12])
    return True


def _purge_plaintext_artifacts(workdir: str, code: str, *, solved: bool = False) -> None:
    """题解入账后立即清理该目录的明文答案物证（合规红线）。

    workdir 中的 FLAG/SOURCE/MEMORY.md、转录和工具产物都可能含有本次访问的
    题面、候选或远端响应；即使容器地址未变，也不能让后续访问读取它们。
    这里在任何 solved（提交成功/duplicate）后调用：删除 FLAG/FLAG.txt/flag.txt/SOURCE
    与 core 转储，并把会回灌进下一场的三个文本文件打码，做到"解完即清、永不积留"。

    清理规则：
      · **打码口径**：旧正则只有信封形式，认不出账本的 bare body（去信封、小写
        归一）。打码改走 `_scrub_flag_plaintext`，与重发路径共用同一口径。
      · **core 转储**：也可能包含候选明文，必须一并清理。
      · **解完即清**：`solved=True` 时连账本与 `_transcripts/` 一起清。
        **未解的访问收尾（solved=False）绝不能清这两样**：
          - `.unverified_flags` 是下一场 prompt 的「尚未坐实」清单；
          - `_transcripts/*.jsonl` 是**真伪取证链** —— `_flag_grounded_in_transcripts`
            按事件顺序判「flag 先出现在工具输出（外部来源）还是命令参数（自己写的）」，
            删了会让下一场的真 flag 被判「无据」而拒收。
        账本与转录在题目完成后没有保留价值，必须同时移除。
      · **[B68] 解完同清的另三处**：`.bash_guard_state.json`（bash 命令原文账本，
        `echo 'flag{...}' > FLAG` 类写入命令的完整 body 会原样入库）；
        `artifacts/`（agent 自产文件可内嵌 flag body；未解时是续解注入输入）；
        `.rejected_flags`（平台判错 body 账本，多段题索引错位时可能含真 body）。
        三者未解时一律保留，与上面两条红线同一口径。
    """
    import glob
    removed = 0
    # FLAG/SOURCE 只在解出后删除（合规清理）。未解时保留——Agent 下次 visit
    # 需要看到之前的错误答案才能避免重复、从断点继续。
    if solved:
        for pat in ("FLAG", "FLAG.txt", "flag.txt", "SOURCE"):
            for p0 in glob.glob(os.path.join(workdir, pat)):
                try:
                    os.remove(p0)
                    removed += 1
                    log.info("  [compliance] removed %s", os.path.relpath(p0, workdir))
                except OSError:
                    pass
    removed += _remove_core_dumps(workdir)
    scrubbed = _scrub_flag_plaintext(workdir, code)
    # [B52] 解完即清：账本 + 转录。仅 solved=True —— 未解时两者都是下一场的输入。
    if solved:
        # The continuation checkpoint contains no answers, but a completed
        # challenge has no successor session that can consume it.  Remove it
        # with the rest of the per-challenge hand-off metadata so a later run
        # cannot mistake a solved chain for pending work.
        for _continuation_name in (
                _CONTINUATION_FILENAME, _CONTINUATION_FILENAME + ".lock"):
            try:
                os.remove(os.path.join(workdir, _continuation_name))
                removed += 1
            except OSError:
                pass
        try:
            os.remove(os.path.join(workdir, ".unverified_flags"))
            removed += 1
            log.info("  [compliance] removed .unverified_flags (%s)", code)
        except OSError:
            pass
        # [B68] 平台判错账本同族同命：body 是答案文本，解完即清（B52 漏项）。
        # 多段题存在「真 flag 配错 flag_index 被判错入账」的边角 → 账本可能含
        # 真 body；且「跨轮永不重投」本身与轮转清空跨轮记忆的合规红线相抵。
        # 未解时保留（同 .unverified_flags 口径：下一场去重输入）。
        for _ledger in (".rejected_flags", ".rejected_flags.lock"):
            try:
                os.remove(os.path.join(workdir, _ledger))
                removed += 1
                log.info("  [compliance] removed %s (%s)", _ledger, code)
            except OSError:
                pass
        # [B54] 幻觉账本随题目一起清。它只存计数与 sha1 指纹（无 flag 原文，
        # 合规上不属于明文物证），但题目已解就没必要再留。
        try:
            os.remove(os.path.join(workdir, ".hallucination.json"))
            removed += 1
        except OSError:
            pass
        # [B61] 观察图同清：它的依据字段是**工具输出摘录**，可能含 flag 明文；
        # 题目已解就没有留存价值（取证链是转录，那条走下面的 _transcripts 清理）。
        # 未解时**不清** —— 它是下一场注入的输入（同 .unverified_flags 口径）。
        try:
            os.remove(os.path.join(workdir, ".heimdall.json"))
            removed += 1
        except OSError:
            pass
        _n_tx = 0
        for p0 in glob.glob(os.path.join(workdir, "_transcripts", "*")):
            try:
                if os.path.isfile(p0):
                    os.remove(p0)
                    _n_tx += 1
            except OSError:
                pass
        if _n_tx:
            removed += _n_tx
            log.info("  [compliance] cleared %d transcript file(s) (%s)", _n_tx, code)
        # [B68] bash_guard 去重账本存**命令原文**（`echo 'flag{...}' > FLAG` 类写入
        # 命令的完整 body 会原样入库）且跨会话持久，不在打码口径内 —— 解完即删。
        # 未解时保留：它是护栏的跨会话去重缓存，不进任何 prompt。
        try:
            os.remove(os.path.join(workdir, ".bash_guard_state.json"))
            removed += 1
            log.info("  [compliance] removed .bash_guard_state.json (%s)", code)
        except OSError:
            pass
        # [B68] artifacts/ 是 agent 自产文件（脚本/数据，可能内嵌 flag body），同一
        # 题续场靠它注入（_reusable_artifacts），解完没有下一场 —— 整目录删。
        # 未解时保留：多段题剩余 flag 的续解输入（红线：未解不动）。
        try:
            _art_dir = os.path.join(workdir, ARTIFACTS_DIR)
            if os.path.isdir(_art_dir):
                import shutil as _shutil
                _shutil.rmtree(_art_dir, ignore_errors=True)
                removed += 1
                log.info("  [compliance] removed %s/ (%s)", ARTIFACTS_DIR, code)
        except OSError:
            pass
    if removed or scrubbed:
        log.info("  [compliance] %s — 清理 %d 个明文解法文件 / 打码 %d 个框架痕迹文件",
                 code, removed, scrubbed)


# 跨会话产物家园：workdir 下的持久子目录（在 bind-mount 的 work 卷内 → 容器
# 重启 / compose 重建都保留；_reusable_artifacts 会把它注入下一场 prompt）。
ARTIFACTS_DIR = "artifacts"
_ARTIFACT_MAX_DEPTH = 3       # /tmp 下递归深度护栏（最深收到 /tmp/a/b/c/file）
_ARTIFACT_MAX_FILES = 500     # 单次扫描文件数护栏（防失控遍历）
# [B58] `jiti` = pi 的 TS 扩展编译缓存目录（/tmp/jiti/*.mjs）。它不是 agent 产物，
# 却会被当产物并入 artifacts/ —— 实测污染 41 道题；更糟的是并入使
# `session_artifacts > 0` 成立，走 record_progress「视为实质进展」，
# 把真正的零进展信号洗掉（402 风暴期间还在打"有进展"的假信号）。
_ARTIFACT_SKIP_DIRS = (".X11-unix", ".font-unix", ".ICE-unix", ".Test-unix",
                       ".XIM-unix", "snap-private-tmp", "systemd-private-",
                       "tmux-", "ssh-", "jiti")
_ARTIFACT_SKIP_FILES = ("driver_heartbeat",)   # driver 自身心跳文件，非 agent 产物
# [B58] 前缀型：pi bash 工具自己的命令日志 /tmp/pi-bash-<hash>.log，同样非 agent 产物
# [B66] pi-subagent-<hash>/prompt-*.md：子 Agent 扩展的临时派发文件，内容与转录里
# START 事件的 args 重复；会话被硬杀时 finally 清理不跑才漏进 /tmp（09-15 实测）。
_ARTIFACT_SKIP_FILE_PREFIXES = ("pi-bash-",)
# [B66] 目录前缀型：pi-subagent-<hash>/ 整目录都跳过（同上，非 agent 产物）
_ARTIFACT_SKIP_DIR_PREFIXES = ("pi-subagent-",)


_API_FAULT_TOKENS = ("402", "401", "Insufficient", "Authentication", "Balance")


def _is_api_fault(result) -> bool:
    """[B59] 账号级 API 故障（余额耗尽 / 认证失效）——**不是**解题进展信号。

    这类失败与「这题挖不动」正交：靶标根本没被碰过。旧代码把它当普通零进展
    记进 stoploss，402 风暴里每题连输 3 场就被打成 stuck:dry_sessions=3 永久
    停掉（2026-09-11 实测 round 0 整轮阵亡，且全是**假止损**——额度恢复后
    这些题会被静默跳过）。判据与 solve_one 返回体的 api_error 字段同源。
    """
    return bool(result is not None and getattr(result, "error", None)
                and any(t in str(result.error) for t in _API_FAULT_TOKENS))


def _mark_api_fault(workdir: str, pauses: int) -> None:
    """[B59-b3b] 亮起账号级故障告警（控制台横幅读它）。

    在**第一次**连续失败（api_fail_streak>=3，约 10 秒）时就写，不等熔断耗尽
    （5 次暂停 ≈ 25 分钟）。理由：熔断阈值是「别再烧配额」的判据，告警阈值是
    「告诉人」的判据 —— 合成一个，横幅就会在舰队空转 25 分钟后才出现，
    那和没有告警是一回事（同日模型名事故 144 场失败也是这个形态）。
    每次退避刷新计数，让横幅能显示故障持续了多久、退避了几轮。
    """
    try:
        with open(os.path.join(workdir, ".api_fault"), "w", encoding="utf-8") as f:
            f.write("%d\n%d\n" % (int(time.time()), int(pauses)))
    except Exception:
        pass    # 告警落盘失败绝不能影响调度（同 stoploss._save 口径）


def _clear_api_fault(workdir: str) -> None:
    """[B59-b3] 账号级故障解除后撤掉告警标记。

    判据是「有会话真正跑起来了」（turns>0）—— 402 时求解器根本起不来，
    能跑起来就说明额度/Key 已恢复。不撤的话控制台横幅会永远亮着，
    那就从「不报警」变成「狼来了」，一样是坏掉的状态灯。
    """
    try:
        os.remove(os.path.join(workdir, ".api_fault"))
    except OSError:
        pass


# ══════════════════════════════════════════════════════════════
# [B61] 观察者 Agent（Heimdall）—— 读思路、画图、不攻击
# ══════════════════════════════════════════════════════════════
# 默认**关**（ADAPTER_HEIMDALL=1 打开）。它复用判断 Agent 的 LLM 通道，每题
# 每次会话结束后观察一次，把 <heimdall-map> 注入**下一场**的 prompt（跨 visit
# 连续，因为状态落在 work/<code>/.heimdall.json）。
#
# 为什么用模块级单例而不是加参数：solve_one 由 schedule_rounds 里的 _visit
# 闭包调用，加参数要穿三层签名。单例在本文件已有先例（_last_registry_codes），
# 且 main() 在并发起来**之前**只赋一次值，之后全程只读。
_HEIMDALL = None


def _heimdall_on() -> bool:
    """总开关。默认关 —— 新链路先默认不参与，观察够了再打开。"""
    return os.environ.get("ADAPTER_HEIMDALL", "0") == "1"


def _heimdall_init(llm) -> None:
    """构造观察者（main() 里调一次）。任何失败 → 保持 None（红线 4）。"""
    global _HEIMDALL
    if not _heimdall_on():
        return
    if llm is None:
        log.warning("[B61] 观察者已请求启用，但判断 Agent 的 LLM 通道不可用 — 跳过")
        return
    try:
        from ghost_worker.adapter.heimdall import Heimdall
        _HEIMDALL = Heimdall(
            llm, timeout=float(os.environ.get("ADAPTER_HEIMDALL_TIMEOUT", "45") or "45"))
        log.info("[B61] 观察者 Agent 已启用: timeout=%.0fs 首个观察场次=%d",
                 _HEIMDALL.timeout, _HEIMDALL.min_session)
    except Exception as e:
        _HEIMDALL = None
        log.warning("[B61] 观察者构造失败（已忽略）：%s", e)


def _heimdall_map_for(workdir: str) -> str:
    """取该题当前的观察图，供注入本场 prompt。任何异常 → ""（红线 4）。

    读的是**上一场**结束时的观察结果；首场读到的是上一次 visit 留下的，
    所以图是跨 visit 连续的。没启用时返回空串，注入点整段消失。
    """
    if _HEIMDALL is None:
        return ""
    try:
        from ghost_worker.adapter.heimdall import load_state, render
        return render(load_state(workdir))
    except Exception:
        return ""


def _heimdall_observe(workdir: str, tpath: str, session_idx: int) -> None:
    """本场结束后观察一次，刷新 work/<code>/.heimdall.json 供下一场注入。

    调用点放在 B59-b1 的 break **之后** —— 账号级故障那场没有任何可观察
    内容，不该白烧一次 LLM 调用。失败绝不外溢（红线 4）。
    """
    if _HEIMDALL is None:
        return
    try:
        from ghost_worker.adapter.heimdall import review
        review(_HEIMDALL, workdir, tpath, session_idx)
    except Exception as e:
        log.warning("[B61] 观察者异常（已忽略，不影响解题）：%s", e)


def _persist_session_artifacts(workdir: str, start_wall: float) -> int:
    """把本会话期间 agent 在容器 /tmp 创建的产物并入 workdir/artifacts/。

    深度 RE 会话（写 emulator / 解码脚本 / patch 二进制）常把中间产物写到 /tmp，
    而框架的跨会话承接先前看不到 /tmp、容器重启也清它。这里在会话结束后把
    mtime>=会话起点的 /tmp 产物并入持久产物目录 artifacts/ —— 跨会话可见、
    重启不丢。/tmp 下的子目录（如 /tmp/jdwp/ 的成组脚本）按相对路径并入
    artifacts/<子目录>/，保住目录结构（_reusable_artifacts 同步递归可见）。
    返回**新增**产物数（已存在的同路径同大小跳过）供"有实质进展"信号。
    """
    if not start_wall:
        return 0
    art = os.path.join(workdir, ARTIFACTS_DIR)
    try:
        os.makedirs(art, exist_ok=True)
    except OSError:
        return 0
    added = 0
    seen = 0
    try:
        for root, dirs, files in os.walk("/tmp", followlinks=False):
            rel = os.path.relpath(root, "/tmp")
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            if depth >= _ARTIFACT_MAX_DEPTH:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs
                           if not d.startswith(_ARTIFACT_SKIP_DIRS)
                           and not d.startswith(_ARTIFACT_SKIP_DIR_PREFIXES)]
            for name in files:
                seen += 1
                if seen > _ARTIFACT_MAX_FILES:
                    return added
                p = os.path.join(root, name)
                try:
                    if (os.path.islink(p) or name in _ARTIFACT_SKIP_FILES
                            or name.startswith(_ARTIFACT_SKIP_FILE_PREFIXES)):
                        continue
                    st = os.stat(p)
                    if st.st_mtime < start_wall:
                        continue
                    if st.st_size > 5_000_000:
                        continue
                    if rel == ".":
                        dst_rel, dst = name, os.path.join(art, name)
                    else:
                        dst_rel = os.path.join(rel, name)
                        dst = os.path.join(art, dst_rel)
                    if os.path.abspath(dst) == os.path.abspath(p):
                        continue
                    # 已存在且同大小 → 视为同一产物，避免重复计入/重复注入
                    if os.path.isfile(dst) and os.path.getsize(dst) == st.st_size:
                        continue
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(p, dst)
                    added += 1
                    log.info("  [artifacts] 并入 %s (%.0f bytes)", dst_rel, st.st_size)
                except OSError:
                    continue
    except Exception:
        pass
    return added


def _persist_tried_commands(workdir: str, tool_outputs) -> None:
    """把本会话执行过的 bash 命令累积写入 workdir/tried_commands.md。

    文件式（MEMORY 风格，非黑板）：跨会话、跨 driver 重启都保留，
    build_task_prompt 从第一场就注入"已尝试命令不要重复"，避免 agent 反复重试
    同一向量（曾见同一目录爆破/穿越探测重复 5-9 次）。
    """
    try:
        path = os.path.join(workdir, "tried_commands.md")

        def _replay_key(command: str) -> str:
            """Normalize a command for dedupe without retaining flag text."""
            key = " ".join(str(command or "").split())
            if not key:
                return ""
            try:
                key = _flag_plaintext_rx(workdir).sub("[REDACTED-FLAG]", key)
            except Exception:
                # The command is still useful as a replay key if the optional
                # compliance helper is unavailable; session-boundary scrubbing
                # remains the final fallback.
                pass
            return key

        seen = set()
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    s = line.strip().lstrip("$ ")
                    if s:
                        normalized = _replay_key(s)
                        if normalized:
                            seen.add(normalized)
        added = []
        for t, args, _out in (tool_outputs or []):
            if t != "bash":
                continue
            cmd = str((args or {}).get("command", "")).strip()
            key = _replay_key(cmd)
            if not key or key in seen:
                continue
            seen.add(key)
            added.append(key)
        if added:
            with open(path, "a", encoding="utf-8") as f:
                for key in added:
                    f.write("$ " + key + "\n")
            # 截断防无限增长（保留最近 200 条）
            if os.path.getsize(path) > 40000:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()[-200:]
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
    except Exception as e:
        log.warning("persist tried_commands failed: %s", e)


def _tool_outputs_from_current_instance_transcripts(
        workdir: str, transcript_path: str | None = None, *,
        trace_scope: str = "") -> list[tuple[str, object, str]]:
    """Read this challenge instance's tool evidence in chronological order.

    A Pi session is deliberately short-lived, while one ``solve_one`` visit can
    contain several sessions.  We retain every session in the *same random
    trace scope* so a later read of an agent-authored file cannot look like a
    fresh local observation.  Scope and round/session sequence are embedded in
    driver-generated filenames; we never infer instance membership from a
    reused challenge code, target address, or filesystem mtime.

    This reads only the active instance's private ``_transcripts`` records; it
    never uses another challenge's logs or any historical answer ledger.
    Blank-output calls are intentionally kept because writes are provenance
    events even when they do not print anything.  Missing/invalid scope returns
    no rows rather than falling back to an unsafe directory-wide glob.
    """
    paths = _scoped_transcript_paths(workdir, trace_scope, transcript_path)

    rows: list[tuple[str, object, str]] = []
    for path in paths:
        pending: dict[str, object] = {}
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        event = json.loads(line.strip())
                    except (TypeError, ValueError):
                        continue
                    kind = event.get("type", "")
                    call_id = event.get("toolCallId", "")
                    if kind == "tool_execution_start":
                        if call_id:
                            pending[call_id] = event.get("args", {})
                        continue
                    if kind == "tool_execution_end":
                        args = pending.pop(call_id, {})
                        if isinstance(args, dict):
                            args = dict(args)
                            # Keep the success bit that Pi emits on the end
                            # event.  It is required for a quiet current-target
                            # `curl -o` response to become evidence later.
                            args["__tsecbench_execution_ok"] = (
                                event.get("isError") is False)
                        content = (event.get("result") or {}).get("content") or []
                        output = "\n".join(
                            str(item.get("text", "")) for item in content
                            if isinstance(item, dict) and item.get("type") == "text")
                        rows.append((str(event.get("toolName", "")), args, output))
                        continue
                    if kind != "tool_execution_update":
                        continue
                    args = pending.get(call_id, {})
                    content = (event.get("partialResult") or {}).get("content")
                    if isinstance(content, str):
                        partial = content
                    elif isinstance(content, list):
                        partial = "\n".join(
                            str(item.get("text", "")) for item in content
                            if isinstance(item, dict) and item.get("type") == "text")
                    else:
                        partial = ""
                    if partial:
                        rows.append(("incomplete_tool", args, partial))
        except OSError:
            continue
    return rows


def _merge_memory(workdir: str, content: str) -> None:
    """把 driver 整理的事实/接力写入 MEMORY.md 固定段，不覆盖 agent 自写笔记。

    agent 用 write 工具维护 MEMORY.md 做跨会话续接；driver 若整体覆写会把结构化
    进展冲掉（实测长会话记忆被污染成黑板噪音）。这里在〈driver-memory〉固定段内
    幂等替换：agent 笔记原样保留，driver 段每次重建，不无限累积。
    """
    path = os.path.join(workdir, "MEMORY.md")
    START = "<!-- driver-memory -->"
    END = "<!-- /driver-memory -->"
    prev = ""
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = f.read()
        except Exception:
            prev = ""
    if not content.strip():
        return
    if START in prev and END in prev:
        prev = re.sub(re.escape(START) + r".*?" + re.escape(END),
                      f"{START}\n{content}\n{END}", prev, flags=re.S)
    else:
        prev = prev.rstrip() + "\n\n" + START + "\n" + content + "\n" + END + "\n"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(prev)
    except Exception:
        pass


def _continuation_checkpoint_path(workdir: str) -> str:
    """Return the per-challenge, answer-free continuation checkpoint path."""
    return os.path.join(workdir, _CONTINUATION_FILENAME)


def _load_continuation_checkpoint(workdir: str, task_epoch: str = "") -> dict:
    """Load a bounded continuation checkpoint for the current task epoch.

    The checkpoint is intentionally *not* a transcript or an answer ledger.
    Only counters and a small set of enum-like labels are accepted.  This
    makes it safe to retain across a same-epoch target re-creation while still
    ensuring a reused challenge code from a later benchmark starts clean.
    """
    epoch = str(task_epoch or "")
    if not epoch:
        return {}
    path = _continuation_checkpoint_path(workdir)
    try:
        # A corrupt/oversized file must never become prompt material.
        if os.path.getsize(path) > 16_384:
            return {}
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh) or {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    try:
        version = int(raw.get("version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    if (version != _CONTINUATION_VERSION
            or str(raw.get("task_epoch", "") or "") != epoch):
        return {}

    def _count(name: str) -> int:
        try:
            return max(0, min(100000, int(raw.get(name, 0) or 0)))
        except (TypeError, ValueError):
            return 0

    # Keep labels intentionally narrow.  Never copy arbitrary result/error
    # text into the next model prompt (it could contain a flag or credential).
    allowed_terms = {
        "completed", "max_turns", "timeout", "stalled", "stopped",
        "error", "api_fault", "target_fault", "no_effective_session",
    }
    termination = str(raw.get("termination", "") or "").strip().lower()
    if termination not in allowed_terms:
        termination = ""
    kinds = []
    for value in raw.get("fact_kinds", []) or []:
        value = str(value or "").strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) and value not in kinds:
            kinds.append(value)
    return {
        "task_epoch": epoch,
        "session": _count("session"),
        "confirmed_count": _count("confirmed_count"),
        "expected_count": _count("expected_count"),
        "new_facts": _count("new_facts"),
        "repeat_count": _count("repeat_count"),
        "duplicate_candidates": _count("duplicate_candidates"),
        "termination": termination,
        "fact_kinds": kinds[:16],
        "pivot_required": bool(raw.get("pivot_required", False)),
    }


def _write_continuation_checkpoint(
    workdir: str,
    *,
    task_epoch: str,
    session: int,
    confirmed_count: int,
    expected_count: int,
    new_facts: int,
    repeat_count: int,
    duplicate_candidates: int = 0,
    termination: str = "",
    pivot_required: bool = False,
    fact_kinds: list[str] | tuple[str, ...] = (),
) -> None:
    """Atomically persist non-sensitive state needed to continue a chain.

    Do not add candidate strings, command text, raw errors, or tool output to
    this file.  It is read on the next prompt and may survive a target
    instance restart, so the schema is deliberately restrictive.
    """
    epoch = str(task_epoch or "")
    if not epoch:
        return
    allowed_terms = {
        "completed", "max_turns", "timeout", "stalled", "stopped",
        "error", "api_fault", "target_fault", "no_effective_session",
    }
    term = str(termination or "").strip().lower()
    if term not in allowed_terms:
        term = "error" if term else ""
    safe_kinds = []
    for value in fact_kinds or ():
        value = str(value or "").strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value) and value not in safe_kinds:
            safe_kinds.append(value)
    payload = {
        "version": _CONTINUATION_VERSION,
        "task_epoch": epoch,
        "session": max(0, min(100000, int(session or 0))),
        "confirmed_count": max(0, min(100000, int(confirmed_count or 0))),
        "expected_count": max(0, min(100000, int(expected_count or 0))),
        "new_facts": max(0, min(100000, int(new_facts or 0))),
        "repeat_count": max(0, min(100000, int(repeat_count or 0))),
        "duplicate_candidates": max(0, min(100000, int(duplicate_candidates or 0))),
        "termination": term,
        "fact_kinds": safe_kinds[:16],
        "pivot_required": bool(pivot_required),
        "updated_at": time.time(),
    }
    path = _continuation_checkpoint_path(workdir)
    tmp = ""
    try:
        os.makedirs(workdir, exist_ok=True)
        # Same challenge leases serialize normal writers; this extra lock also
        # makes a process restart or diagnostic writer harmless.
        with _advisory_lock(path + ".lock"):
            fd, tmp = tempfile.mkstemp(
                prefix=".continuation.", suffix=".tmp", dir=workdir)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            tmp = ""
    except (OSError, TypeError, ValueError):
        log.warning("continuation checkpoint write failed for %s", workdir)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _continuation_prompt_note(
    checkpoint: dict | None,
    *,
    session_idx: int,
    confirmed_count: int,
    expected_count: int,
) -> str:
    """Render an answer-free, explicit hand-off instruction for Pi.

    Merely showing ``N/M`` was insufficient in the old flow: a new Pi process
    treated the task as a fresh puzzle and repeated the entry path.  This note
    names the session boundary and tells it to pivot while leaving all actual
    evidence in the current target/workdir.
    """
    cp = checkpoint or {}
    prior = bool(cp)
    if not prior and session_idx <= 0:
        return ""
    lines = [
        "## 连续会话续接（驱动生成的无答案状态）",
        f"这是同一题的第 {max(1, int(session_idx) + 1)} 个连续会话（目标实例可能已重建）；"
        "先核对当前目标状态，不要把会话边界当成新题重新侦察。",
        f"平台确认进度：{max(0, int(confirmed_count or 0))}/"
        f"{max(1, int(expected_count or 1))}。",
    ]
    if cp:
        if cp.get("session", 0):
            lines.append(f"上一会话序号：{int(cp['session'])}。")
        if cp.get("termination"):
            lines.append(f"上一会话结束类型：{cp['termination']}；先承接已有现场状态。")
        lines.append(
            f"上一会话新增事实 {int(cp.get('new_facts', 0))} 条，"
            f"重复命令 {int(cp.get('repeat_count', 0))} 次，"
            f"重复已确认候选 {int(cp.get('duplicate_candidates', 0))} 次。"
        )
        kinds = ", ".join(cp.get("fact_kinds", [])[:8])
        if kinds:
            lines.append(f"已触及的事实类别：{kinds}。")
    if cp.get("pivot_required") or session_idx > 0:
        lines.extend([
            "本场必须从尚未验证的攻击面/主机/服务继续：先读当前 MEMORY.md、"
            "tried_commands.md 和黑板摘要，禁止原样重跑上一场入口、扫描或已确认 flag 的获取路径。",
            "已确认的 flag 只是进度检查点；再次在输出中看到它时不要重新写入/提交，"
            "立即沿当前内网链寻找下一阶段。",
        ])
    return "\n".join(lines)


def _eager_submit_loop(
    *, stop_evt: threading.Event, client, code: str, workdir: str,
    attempt_id: str | None = None, transcript_path: str | None = None,
    trace_scope: str = "",
    task, verifier, submitted: dict, submitted_lock: threading.Lock,
    accepted_flags: list, obs, solved_flag: list, stoploss,
    progress_seen: list | None = None,
) -> None:
    """后台线程：session 运行期间实时监控 FLAG 文件，发现新 flag 立即提交。

    多段渗透题（6 flags）不再等 session 结束才提交——找到即入账，
    防止 session 超时/崩溃导致已发现 flag 丢失。
    """
    # This loop runs in a separate thread from the Pi session.  Establish the
    # same per-challenge observability context explicitly; thread-local event
    # metadata must not be inherited from whichever challenge used the thread
    # before it was created.
    try:
        context_values = {"challenge_id": str(code)}
        if attempt_id is not None:
            context_values["attempt_id"] = str(attempt_id)
        obs.context(**context_values)
    except Exception:
        pass
    try:
        # Three seconds is short enough that a discovered stage is handed to
        # the platform while the agent is still working on the next command.
        # The delivery-signature gate below keeps this from repeatedly parsing
        # a large transcript when FLAG did not change.
        _EAGER_INTERVAL = max(
            0.5, float(os.environ.get("ADAPTER_EAGER_SUBMIT_INTERVAL", "3") or "3"))
    except (TypeError, ValueError):
        _EAGER_INTERVAL = 3.0
    _fail_streak = [0]
    _FAIL_LIMIT = int(os.environ.get("ADAPTER_FAIL_SUBMIT_LIMIT", "6") or "6")
    try:
        # These are retries after the first request has failed.  A short
        # exponential schedule lets a transient platform/API outage recover
        # within the same Pi session without converting the eager watcher into
        # a request spinner.
        _SUBMIT_RETRY_LIMIT = max(
            0, int(os.environ.get("ADAPTER_EAGER_SUBMIT_RETRY_LIMIT", "3") or "3"))
    except (TypeError, ValueError):
        _SUBMIT_RETRY_LIMIT = 3
    try:
        _SUBMIT_RETRY_BASE_SECONDS = max(
            _EAGER_INTERVAL,
            float(os.environ.get("ADAPTER_EAGER_SUBMIT_RETRY_SECONDS", "3") or "3"),
        )
    except (TypeError, ValueError):
        _SUBMIT_RETRY_BASE_SECONDS = _EAGER_INTERVAL
    try:
        _SUBMIT_RETRY_MAX_SECONDS = max(
            _SUBMIT_RETRY_BASE_SECONDS,
            float(os.environ.get("ADAPTER_EAGER_SUBMIT_RETRY_MAX_SECONDS", "30") or "30"),
        )
    except (TypeError, ValueError):
        _SUBMIT_RETRY_MAX_SECONDS = max(_SUBMIT_RETRY_BASE_SECONDS, 30.0)
    _verify_rejected = set()  # verifier 拒绝过的 flag body，本轮不再重试
    _skeptic_memo: dict = {}  # [B47] 判断 Agent 裁决缓存（body → ""/"rescue"/"veto"）
    if progress_seen is None:
        progress_seen = [False]
    evidence_policy = flag_evidence_policy(
        getattr(task, "category", ""),
        targets=getattr(task, "targets", ()),
        files=getattr(task, "files", ()),
        workdir=getattr(task, "workdir", ""),
    )
    _last_delivery_sig = None
    # ``_pending_evidence`` candidates are retried only when the scoped
    # transcript actually grows.  Without this second token an unchanged
    # FLAG entry caused a full transcript parse + verifier call every polling
    # tick for the lifetime of the session.
    _last_evidence_sig = None
    # A candidate can be written slightly before the matching transcript event
    # flushes.  Keep only those deferred candidates polling until their
    # evidence arrives; everything else waits for a real FLAG-file change.
    _pending_evidence: set[str] = set()
    # A transient submission fault is not an evidence fault.  Store the
    # number of retries already scheduled plus its monotonic deadline, so a
    # stable FLAG/transcript pair can still be retried without polling hot.
    _submission_retries: dict[str, tuple[int, float]] = {}
    # Once a candidate uses its bounded retry allowance, preserve the queue
    # line for session-boundary recovery but do not start another retry cycle
    # merely because Pi continues to append unrelated transcript events.
    _submission_retry_exhausted: set[str] = set()

    while not stop_evt.is_set():
        stop_evt.wait(timeout=_EAGER_INTERVAL)
        if stop_evt.is_set():
            break
        try:
            file_flags, delivery_sig, delivery_stable = (
                _read_stable_flag_delivery_snapshot(workdir))
            if not delivery_stable:
                # Never bind a just-read candidate list to a signature from a
                # concurrent writer.  Leaving both last tokens untouched
                # forces one clean delivery pass once the agent's write has
                # settled, while pending evidence still remains transcript-
                # driven on stable polls.
                continue
            evidence_sig = _transcript_evidence_signature(
                workdir, transcript_path, trace_scope=trace_scope)
            current_keys = {
                _candidate_sha256(normalize_flag_envelope(value))
                or _normalize_flag_body(normalize_flag_envelope(value))
                for value in file_flags
            }
            current_keys.discard("")
            _pending_evidence.intersection_update(current_keys)
            _submission_retries = {
                key: retry for key, retry in _submission_retries.items()
                if key in current_keys
            }
            _submission_retry_exhausted.intersection_update(current_keys)
            retry_now = time.monotonic()
            retry_due = any(
                deadline <= retry_now
                for _retry_number, deadline in _submission_retries.values()
            )
            # A pending candidate is worth another pass only after a
            # matching/updated transcript event has been flushed.  A transient
            # platform submit error instead wakes only at its retry deadline.
            # New FLAG contents still wake the loop immediately via the
            # delivery token.
            if not _eager_needs_scan(
                    delivery_sig, _last_delivery_sig,
                    evidence_sig, _last_evidence_sig, _pending_evidence,
                    retry_due=retry_due):
                continue
            _last_delivery_sig = delivery_sig
            _last_evidence_sig = evidence_sig
            for fc_raw in file_flags:
                fc = normalize_flag_envelope(fc_raw)
                if not fc:
                    continue
                nb = _normalize_flag_body(fc)
                candidate_key = _candidate_sha256(fc) or nb
                if candidate_key in _submission_retry_exhausted:
                    continue
                retry_state = _submission_retries.get(candidate_key)
                if retry_state is not None and retry_state[1] > retry_now:
                    # Another due candidate or a new FLAG entry woke this
                    # scan.  Preserve this candidate's own backoff deadline.
                    continue
                # 去重（已提交 / 已验证拒绝 / 错误账本）
                with submitted_lock:
                    if _submitted_contains(submitted, code, fc):
                        _pending_evidence.discard(candidate_key)
                        _submission_retries.pop(candidate_key, None)
                        _submission_retry_exhausted.discard(candidate_key)
                        continue
                if nb in _verify_rejected:
                    _pending_evidence.discard(candidate_key)
                    _submission_retries.pop(candidate_key, None)
                    _submission_retry_exhausted.discard(candidate_key)
                    continue
                # 错误账本
                rejected = _load_rejected_flags(workdir)
                if nb in rejected:
                    _pending_evidence.discard(candidate_key)
                    _submission_retries.pop(candidate_key, None)
                    _submission_retry_exhausted.discard(candidate_key)
                    continue
                # 候选只允许来自当前题目的现场证据；不读取其它题目目录或
                # 共享历史账本。占位键在这里直接丢弃，避免进入验证器。
                if nb in ("...", ""):
                    continue
                # Validate against the whole current platform instance, not
                # only this Pi process.  A prior session may have authored a
                # temporary file that a later session reads back.
                _tool_outputs = _tool_outputs_from_current_instance_transcripts(
                    workdir, transcript_path, trace_scope=trace_scope)
                if nb in _load_unverified_flags(workdir):
                    # A prior gate refusal is not a permanent "wrong answer":
                    # fresh qualified current-instance evidence may rescue it.
                    # Without this check, each new Pi session re-verifies the
                    # same queued candidate even though its evidence has not
                    # changed.
                    _prior = flag_confidence(
                        fc, "", _tool_outputs, evidence_policy=evidence_policy)
                    if not (_prior.grounded and _prior.confidence >= 0.9):
                        log.info("  [eager] skip already-unverified candidate without new evidence: %s",
                                 fc[:30])
                        _pending_evidence.discard(candidate_key)
                        _submission_retries.pop(candidate_key, None)
                        _submission_retry_exhausted.discard(candidate_key)
                        continue
                    log.info("  [eager] recheck unverified candidate after new %s evidence: %s",
                             _prior.provenance or "qualified", fc[:30])
                _flag_in_tool_output = False
                _flag_in_args_first = False   # flag 先出现在命令参数 = agent 自写
                # 时序判别：首个包含 fc 的调用决定真伪——参数里有 = agent
                # 自己写的；只有先在输出中出现才可能是外部来源。
                for _name, _args, _out in _tool_outputs:
                    if _flag_in_tool_output or _flag_in_args_first:
                        break
                    _args_s = (_args if isinstance(_args, str)
                               else json.dumps(_args, ensure_ascii=False))
                    if fc in _args_s:
                        _flag_in_args_first = True
                    elif _out and fc in _out:
                        _flag_in_tool_output = True
                claim = flag_confidence(
                    fc, "", _tool_outputs, evidence_policy=evidence_policy)
                claim = verifier.verify(claim)
                if not claim.verified and not _flag_in_tool_output:
                    # FLAG 文件可以在同一条尚未结束的 bash 调用中先写入。转录尚未
                    # 落到 end/update 证据时，绝不能把候选删掉；下一次 eager 轮询会
                    # 在证据完整后重验，避免"真值先写、证据后到"造成漏交。
                    log.info("  [eager] defer candidate without completed evidence: %s (%s)",
                             fc[:30], claim.reject_reason)
                    _pending_evidence.add(candidate_key)
                    _submission_retries.pop(candidate_key, None)
                    _submission_retry_exhausted.discard(candidate_key)
                    continue
                if not claim.verified:
                    # Force-submit any grounded flag regardless of whether it
                    # appeared in tool output or only in command args.  For
                    # reverse/crypto challenges, the flag may legitimately
                    # only appear in a printf/echo command that writes to FLAG.
                    # The platform judges correctness.
                    if claim.grounded and claim.confidence >= 0.50:
                        log.info("  [eager] force submit grounded flag (conf=%.2f): %s",
                                 claim.confidence, fc[:30])
                    else:
                        log.info("  [eager] skip ungrounded flag (conf=%.2f): %s",
                                 claim.confidence, fc[:30])
                        _verify_rejected.add(nb)
                        _add_unverified_flag(workdir, fc, claim.reject_reason,
                                             prune_delivery=False)
                        _pending_evidence.discard(candidate_key)
                        _submission_retries.pop(candidate_key, None)
                        _submission_retry_exhausted.discard(candidate_key)
                        continue
                # [B47] 判断 Agent 拦截：自动通道这轮 34/34 全对，但错交的代价
                # 是实打实的 —— 提交前多问一句，置信度不到 0.85 不拦（宁放过不误伤）。
                if _skeptic_check(verifier, claim, workdir, code,
                                  stage="submit", memo=_skeptic_memo,
                                  trace_scope=trace_scope) == "veto":
                    _add_unverified_flag(workdir, fc, prune_delivery=False)
                    _pending_evidence.discard(candidate_key)
                    _submission_retries.pop(candidate_key, None)
                    _submission_retry_exhausted.discard(candidate_key)
                    continue
                # 提交
                try:
                    sr = client.submit_flag(code, fc)
                except Exception as e:
                    prior_retries = (_submission_retries.get(candidate_key) or (0, 0.0))[0]
                    if prior_retries >= _SUBMIT_RETRY_LIMIT:
                        _submission_retries.pop(candidate_key, None)
                        _submission_retry_exhausted.add(candidate_key)
                        log.warning(
                            "  [eager] submit error after %d retry/retries; leave candidate for "
                            "session-boundary recovery: %s", prior_retries, e)
                        continue
                    retry_number = prior_retries + 1
                    retry_delay = min(
                        _SUBMIT_RETRY_MAX_SECONDS,
                        _SUBMIT_RETRY_BASE_SECONDS * (2 ** (retry_number - 1)),
                    )
                    _submission_retries[candidate_key] = (
                        retry_number, time.monotonic() + retry_delay)
                    _submission_retry_exhausted.discard(candidate_key)
                    log.warning(
                        "  [eager] submit error; retry %d/%d in %.1fs: %s",
                        retry_number, _SUBMIT_RETRY_LIMIT, retry_delay, e)
                    # Do not require the agent to rediscover the same flag
                    # just because the platform had a transient failure.  It
                    # is deliberately not added to _pending_evidence: no new
                    # transcript write should be required for this retry.
                    continue
                if sr.correct:
                    # record_flag() is hash-idempotent; an already-known
                    # response must not keep a stalled session alive forever.
                    if stoploss.record_flag(code, fc):
                        progress_seen[0] = True
                elif sr.duplicate:
                    # A first duplicate can be the only local proof that a
                    # platform baseline already contains this flag.  Count it
                    # as progress once, but not on repeated duplicates.
                    if stoploss.record_duplicate(code, fc):
                        progress_seen[0] = True
                confirmed_count = _record_confirmed_submission(workdir, task, fc, sr)
                task.correct_flag_count = max(
                    int(getattr(task, "correct_flag_count", 0) or 0),
                    int(getattr(sr, "correct_flag_count", 0) or 0),
                    confirmed_count,
                )
                if stoploss.record_platform_progress(code, task.correct_flag_count):
                    progress_seen[0] = True
                _emit_flag_submit(obs, code, fc, sr, task,
                                  eager=True,
                                  confirmed_flag_count=confirmed_count)
                if sr.correct:
                    with submitted_lock:
                        submitted.setdefault(code, set()).add(_candidate_sha256(fc) or nb)
                    _pending_evidence.discard(candidate_key)
                    _submission_retries.pop(candidate_key, None)
                    _submission_retry_exhausted.discard(candidate_key)
                    log.info("  \u26a1 [EAGER] FLAG CORRECT on %s: %s (+%d pts, total %d)",
                             code, fc[:30], sr.awarded, sr.cumulative_score)
                    accepted_flags.append(fc)
                    _update_status(
                        flags_submitted=max(
                            len(accepted_flags),
                            int(getattr(task, "correct_flag_count", 0) or 0),
                            int(confirmed_count or 0),
                        ),
                        total_earned=sr.cumulative_score,
                        last_event=f"EAGER FLAG {fc[:20]}",
                        last_log=f"EAGER FLAG on {code}: +{sr.awarded} pts",
                    )
                    if _submission_complete(task, sr, confirmed_count):
                        solved_flag[0] = True
                        log.info("\U0001f389 [EAGER] All %d flags submitted!", sr.correct_flag_count)
                        return
                elif sr.duplicate:
                    with submitted_lock:
                        submitted.setdefault(code, set()).add(_candidate_sha256(fc) or nb)
                    _pending_evidence.discard(candidate_key)
                    _submission_retries.pop(candidate_key, None)
                    _submission_retry_exhausted.discard(candidate_key)
                    if task.flag_count > 1:
                        if _submission_complete(task, sr, confirmed_count):
                            solved_flag[0] = True
                            log.info("[eager] duplicate confirms all flags for %s", code)
                            return
                        log.info("  [eager] dup on %s (\u591aflag\u9898\uff0c\u7ee7\u7eed)", code)
                    else:
                        log.info("  [eager] dup on %s (\u5355flag\u9898\uff0c\u89c6\u4e3a\u5df2\u89e3)", code)
                        solved_flag[0] = True
                        accepted_flags.append(fc)
                        return
                else:
                    log.info("  [eager] INCORRECT on %s: %s", code, fc[:30])
                    _add_rejected_flag(workdir, fc, prune_delivery=False)
                    _pending_evidence.discard(candidate_key)
                    _submission_retries.pop(candidate_key, None)
                    _submission_retry_exhausted.discard(candidate_key)
                    _fail_streak[0] += 1
                    if _fail_streak[0] >= _FAIL_LIMIT:
                        log.warning("  [eager] %d consecutive failures", _fail_streak[0])
                        return
        except Exception as e:
            log.warning("[eager] loop error: %s", e)
    try:
        obs.clear_local_context()
    except Exception:
        pass


def _solve_one_unlocked(
    client: RateLimitedClient,
    ch: Challenge,
    visit_seconds: int,
    round_idx: int,
    *,
    solver: SolverConfig,
    ctrl: ControllerConfig,
    verifier: Verifier,
    stoploss: StopLoss,
    stop_event: threading.Event,
    submitted: dict,
    submitted_lock: threading.Lock,
    dispatch_deadline: float | None = None,
) -> dict:
    """
    单题求解主逻辑。

    返回: {"solved": bool, "outcome": str, "flags": list}
    """
    code = ch.unique_code
    obs.context(challenge_id=str(code), attempt_id=str(round_idx))

    # hard 题给更宽松的零 flag 止损阈值（逆向/密码题需要多轮试错）。
    stoploss.set_difficulty(code, getattr(ch, "difficulty", "") or "")

    if dispatch_deadline is not None and time.monotonic() >= dispatch_deadline:
        return {"solved": False, "outcome": "aborted", "reason": "dispatch_budget_exhausted"}

    # A benchmark can reuse a public code in a later task generation.  Do this
    # before the first StopLoss read: otherwise a terminal state from the prior
    # task returns ``dropped`` here and the normal post-start cleanup is never
    # reached.  The helper looks only at the answer-free epoch marker and is
    # protected by solve_one's per-challenge lifecycle lease in production.
    _preflight_workdir = os.path.join(ctrl.workdir, _safe_code(code))
    try:
        _preflight_task_epoch_isolation(
            _preflight_workdir, code, task_epoch=_current_task_epoch())
    except Exception:
        # Isolation failure must not make the driver invent a fresh state; the
        # existing StopLoss guard remains the safe fallback and the normal
        # post-start purge will retry if the visit can proceed.
        log.warning("  [compliance] %s preflight task-epoch isolation failed", code,
                    exc_info=True)

    # 止损检查。旧版本可能已经把一个多 flag 题记成 stuck，但还没有经过
    # 平台提示复核；把这种历史状态恢复成"待复核"，而不是直接切题。
    _multi_task_hint = int(getattr(ch, "flag_count", 1) or 1) > 1
    _force_hint_review = False
    stop, reason = stoploss.should_stop(code)
    if (stop and _multi_task_hint and not stoploss.hint_requested(code)
            and reason.startswith(("stuck:", "zero_flag_sessions:"))):
        stoploss.rearm_dry_window(code)
        _force_hint_review = True
        log.info("  %s 历史止损尚未做提示复核 — 恢复为提示待审状态", code)
        stop = False
    # 单 flag 的 ``stuck:dry_sessions`` 是一次真正的止损，不可在每个派发
    # 入口自动清零。旧逻辑会在三次零推进后立刻重开窗口，于是同一题被反复
    # start/close，直到外层重试耗尽才停。这里保留状态交给自动派发的冷却和
    # 周期 revive；显式派单仍可在下方走 fresh budget 的 revive 路径。
    if stop:
        # B) 派单强制复活：网页「Agent 解此题」= 用户明确要解本题，
        # 忽略 stoploss 直接 revive（fresh budget）；sessions/时间硬顶仍由
        # should_stop 内部约束（revive 重置 start_wall 后不会立刻再停掉）。
        if _priority_code_key(code) in {
                _priority_code_key(item)
                for item in _load_priority(ctrl.workdir, _worker_id())}:
            stoploss.revive(code)
            log.info("  %s 派单强制复活（忽略 stoploss: %s）", code, reason)
            stop = False
            reason = ""
        else:
            # A2) 周期复活：非派单被停题在冷却期满后自动获得一次 fresh budget，
            # 避免 hard 题被 stoploss 永久搁置（框架通用能力，总量仍受 sessions 上限约束）。
            cooldown = float(os.environ.get("ADAPTER_REVIVE_COOLDOWN", "3600") or "3600")
            if (time.time() - stoploss.last_revive_wall(code)) >= cooldown:
                stoploss.revive(code)
                log.info("  %s stoploss 冷却期满 — 周期复活重新尝试 (old reason=%s)",
                         code, reason)
                stop = False
                reason = ""
    if stop:
        if _multi_task_hint and stoploss.hint_requested(code):
            try:
                _ev = stoploss.abandonment_evidence(code)
                obs.emit("challenge_abandoned", layer="driver", payload=_ev)
            except Exception:
                pass
        return {"solved": False, "outcome": "dropped", "reason": reason}
    # [B46] 进程正在退出（热重载/停机）≠ 本题派发过一次没解出。
    # 这一支必须与真止损分开：合并写时，停机瞬间**尚未轮到**的题会毫秒级返回
    # "dropped"，被 auto_dispatch_loop 记成一次派发失败；每重载一次白扣一次，
    # 满 max_attempts 就 retry_at=inf 永久出局（实测：5 次重载烧掉 17 道题，
    # 其中多道连 work/<code>/ 都没建过、从未真正跑过）。改报 "aborted" ——
    # schedule_rounds 的 dropped 集合只认 == "dropped"，故本次不计账。
    if stop_event.is_set():
        log.info("  %s 停机/热重载信号 — 本次访问未开始，不计入派发重试", code)
        return {"solved": False, "outcome": "aborted", "reason": "shutdown"}

    # 派发互斥：其他解题目 worker 正在解此题 → 本次访问放弃。
    # （防止双 worker 并发写同一题 workdir/.pi-home 互相污染 + 重复烧 token；
    #  只对 worker-1/2 生效，manager 仅派单/unknown 不占解题）
    if _other_solver_active_on(code):
        log.info("  %s 正由另一解题目 worker 求解 — 本次访问放弃 (mutex)", code)
        return {"solved": False, "outcome": "active_elsewhere"}

    # ── 任务终态感知（B 修复）───────────────────────────────
    # 平台结束任务后，老驱动会把整场访问烧完才停（实测任务结束后又跑了
    # 23 分钟/59 回合的无效会话）。两层止损：
    #   1) 访问入口同步预检（一次轻量 list_challenges）；
    #   2) 访问期间看门狗线程周期探测（ADAPTER_LIVENESS_POLL，默认 150s），
    #      只认显式终态（_task_finished），瞬时网络错误忽略不误杀。
    task_dead = threading.Event()
    # 只在真正成功启动并初始化靶场后创建 liveness 线程。此前线程在
    # claim-lost/budget-skip/start-failed 三个早退路径上没有 stop 事件收口，
    # 每次调度让行都会泄漏一个线程，并持续轮询平台，最终表现为 API 刷屏和
    # 看似反复开关题。这里先做一次同步预检，后台线程延后到 setup 完成之后。
    _watch_stop = threading.Event()
    try:
        client.list_challenges()
    except Exception as _le:
        if _task_finished(str(_le)):
            log.info("  %s 跳过：平台任务已结束 (liveness precheck)", code)
            return {"solved": False, "outcome": "task_ended"}

    # 认领求解：只有通过互斥、真正开始尝试本题才占位。
    # 放在互斥检查之后：mutex-bail / 止损放弃的访问不再留下 current_code 残留认领，
    # 避免空闲 worker 的空认领毒化另一 worker 的派发互斥（活锁根因之一）。
    _my_claim_ts = time.time()
    _update_status(current_code=code, current_difficulty=ch.difficulty or "",
                   current_round=round_idx + 1, last_event=f"visit {code}",
                   solving_active=True, claim_ts=_my_claim_ts)
    # B19：认领后复核 —— 互斥检查与认领之间隔着一次平台 list_challenges 预检
    # （可达秒级），两 worker 可同时通过检查再同时认领（实测 22:39:34 wid1/wid2
    # 相差 16ms 双双认领同一题，并发写同一 workdir/.pi-home、重复烧 token）。
    # 冲突按 (claim_ts, wid) 定胜负，败者立刻释放认领 —— 不释放的话胜者的
    # _other_solver_active_on 会把败者的残留认领当真，双方互让（活锁重演）。
    time.sleep(_CLAIM_VERIFY_DELAY)
    _conf = _conflicting_claim(code)
    if _conf is not None and _conf < (_my_claim_ts, _worker_id()):
        log.info("  %s 认领竞争落败（另一 worker 同时认领且更早）— 释放认领并放弃", code)
        _update_status(current_code="", current_difficulty="",
                       solving_active=False, last_event=f"claim lost {code}")
        return {"solved": False, "outcome": "active_elsewhere"}

    # B22：开靶场前先判「这一访是否还够跑一个最小会话」。
    # 预算接近耗尽时若仍 start_challenge，进会话循环会立刻被
    # `sess_secs < 60: break` 弹回，随即 close_challenge —— 白烧一次容器启停。
    # 预算不够就别开容器：整题跳过，交给止损/复活逻辑处理。
    # 派单题排除在外：调度器对派单题豁免 dropped 过滤（"永不放弃"），
    # 在这里提前返回会让它每轮被重新挑中却什么都不做 → 空转烧平台 API。
    # 派单题维持原路径，由容器启停把节奏自然限在 ~10s/轮。
    prio_codes = {_priority_code_key(item)
                  for item in _load_priority(ctrl.workdir, _worker_id())}
    _is_prio = _priority_code_key(code) in prio_codes
    _MIN_SESS_SECS = 60
    _pre_stop, _pre_reason = stoploss.should_stop(code)
    _pre_remain = stoploss.remaining_seconds(code)
    _global_remain = (int(dispatch_deadline - time.monotonic())
                      if dispatch_deadline is not None else visit_seconds)
    if (_global_remain < _MIN_SESS_SECS):
        log.info("  skip visit %s: dispatch budget has only %ds remaining", code, _global_remain)
        _update_status(current_code="", current_difficulty="",
                       solving_active=False, last_event=f"global budget skip {code}")
        return {"solved": False, "outcome": "aborted",
                "reason": "dispatch_budget_exhausted"}
    if not _is_prio and (_pre_stop or min(visit_seconds, _pre_remain) < _MIN_SESS_SECS):
        log.info("  skip visit %s before start: %s（剩余预算 %ds < 最小会话 %ds）",
                 code, _pre_reason or "budget", _pre_remain, _MIN_SESS_SECS)
        _update_status(current_code="", current_difficulty="",
                       solving_active=False, last_event=f"skip {code}")
        # 必须返回 "dropped"：调度器只认 solved/dropped/should_stop，
        # 而此处 stoploss.start() 尚未调用、should_stop() 看不到本次访问，
        # 返回其它值会让该题既不算解决也不算放弃 → 下一轮再次被挑中 → 空转。
        return {"solved": False, "outcome": "dropped",
                "reason": "budget_exhausted_before_start"}

    # 启动实例（派单题等待更耐心：槽位竞争时坚持等，不轻易轮换跳过）
    start_retries = 30 if _is_prio else None
    started, outcome = _start_with_retry(
        client, code, stop_event=stop_event, rate_wait=lambda: None,
        retries=start_retries, deadline=dispatch_deadline)
    if started is None:
        _update_status(solving_active=False, current_code="")
        return {"solved": False, "outcome": outcome or "start_failed"}

    # From this point on the platform owns an active challenge instance.  Keep
    # an explicit local ownership bit so a failed setup/restart path cannot
    # issue a second close for the same instance in ``finally``.
    container_active = True
    _emit_challenge_lifecycle(
        "challenge_start", code, reason="visit_start", round=round_idx,
        worker_id=_worker_id(), task_epoch=_current_task_epoch())
    workdir = os.path.join(ctrl.workdir, _safe_code(code))
    os.makedirs(workdir, exist_ok=True)
    # Every start_challenge call receives a fresh trace namespace, even when a
    # platform reuses the same code/address.  This prevents any retained
    # transcript from a previous instance from becoming provenance here.
    trace_scope = _new_trace_scope()

    targets = started.container_addr if hasattr(started, 'container_addr') else []
    try:
        # 每次平台实例均先清空 solver 可见历史，再重新生成当前实例的上下文指令。
        _purge_stale_solutions(workdir, code, targets,
                               task_epoch=_current_task_epoch(),
                               trace_scope=trace_scope)
        write_context_md(workdir)
        task = build_task(ch, workdir, targets=targets)
        evidence_policy = flag_evidence_policy(
            task.category or "", targets=task.targets, files=task.files,
            workdir=task.workdir)
        log.info("  %s flag evidence policy: %s (category=%s, declared_inputs=%d)",
                 code, evidence_policy.mode, evidence_policy.category,
                 len(evidence_policy.declared_inputs))
        board = _shared_board_for(code, workdir)
        board.objective = task.objective
        board.seed_goals(goals_for_category(task.category or ""))
        stoploss.start(code, multi_flag=task.flag_count > 1)
        # Sync non-sensitive platform progress before the first session.  This is
        # what lets a later visit finish the final flag even when this visit only
        # contributes one new candidate (and keeps multi-flag zero-progress logic
        # from mistaking a partial platform count for a fresh zero).
        stoploss.record_platform_progress(code, task.correct_flag_count)
        # Keep the task's non-sensitive progress baseline in sync with the
        # durable manifest.  A platform list response may briefly lag after a
        # submit (or return zero after a worker restart); dropping this value
        # would make the next prompt tell the agent that no flags were
        # submitted and can cause it to repeat already-completed stages.
        _confirmed_initial = _initialize_confirmed_progress(workdir, task)
        # Rebuild exact submission dedupe from this challenge's own opaque
        # receipt.  Do not recover it from the global event stream: event
        # history is for observability, never a source of cross-challenge
        # candidate state.
        _confirmed_initial = max(
            _confirmed_initial,
            _hydrate_submitted_from_confirmed_progress(
                submitted, submitted_lock, code, workdir, task),
        )
        task.correct_flag_count = max(
            int(getattr(task, "correct_flag_count", 0) or 0),
            int(_confirmed_initial or 0),
        )
    except Exception as setup_error:
        # Setup runs after start_challenge but used to sit outside the guarded
        # solve/finally block.  Any filesystem/import failure therefore leaked an
        # active target and a solver claim, making the next round open another
        # target for the same code.  Close exactly once and classify it as a
        # retryable dispatch failure so the scheduler applies backoff.
        log.exception("solve_one setup failed on %s", code)
        _watch_stop.set()
        if container_active:
            was_closed = _close_with_retry(client, code)
            _emit_challenge_lifecycle(
                "challenge_close", code, reason="setup_failed",
                closed=bool(was_closed), worker_id=_worker_id(),
                task_epoch=_current_task_epoch())
            container_active = not was_closed
        try:
            stoploss.end_visit(code)
        except Exception:
            pass
        _update_status(solving_active=False, current_code="", session_active=False,
                       session_started_at=0.0, last_activity=time.time(),
                       last_event=f"setup failed {code}")
        return {"solved": False, "outcome": "start_failed",
                "error": str(setup_error)[:200], "turns": 0,
                "api_error": _is_api_fault(setup_error)}

    # The liveness watchdog is intentionally started only after all early-return
    # paths and post-start setup have succeeded.  This keeps a mutex/budget/start
    # failure from leaking a background list_challenges loop.
    def _liveness_loop():
        try:
            _iv = float(os.environ.get("ADAPTER_LIVENESS_POLL", "150") or "150")
        except ValueError:
            _iv = 150.0
        while not task_dead.is_set() and not _watch_stop.wait(_iv):
            try:
                client.list_challenges()
            except Exception as le:
                if _task_finished(str(le)):
                    task_dead.set()
                    log.info("  %s liveness: 平台任务已结束 — 终止当前访问", code)
                    return
                # 瞬时错误（VPN 抖动等）不是终态，继续观察

    threading.Thread(target=_liveness_loop, daemon=True,
                     name="task-liveness").start()

    log.info("round %d visit %s (flags=%d, diff=%s, visit<=%ds) targets=%s",
             round_idx + 1, code, task.flag_count,
             ch.difficulty or "?", visit_seconds, targets)

    solved = False
    accepted_flags = []
    # Provenance is scoped to this one live platform instance.  It is never
    # written to a cross-challenge store, but it must span Pi sessions so a
    # later read of an earlier agent-authored artifact cannot be washed clean.
    _visit_tool_outputs: list[tuple] = []
    # StopLoss facts use the same visit-local provenance boundary.  A later
    # `cat response` is progress only if this tracker saw the successful
    # current-target downloader which created that exact response path.
    _progress_evidence_gate = _ProgressEvidenceGate(evidence_policy)
    # 提交冷却：同一会话内连续失败达到阈值 → 停止本次提交（防幻觉 flag 刷屏）
    _FAIL_SUBMIT_LIMIT = int(os.environ.get("ADAPTER_FAIL_SUBMIT_LIMIT", "6"))
    _fail_submit_streak = 0
    session_idx = 0
    _last_session_flags = 0   # 上一场 pi session 发现的 flag 数（自适应时长依据）
    _last_session_facts = 0   # 上一场自动事实库新增数（无进展换向依据）
    _last_session_repeats = 0 # 上一场规范化命令重复次数（提示 Agent 换面）
    _last_duplicate_candidates = 0  # 上一场再次产出的已确认候选数（只计数）
    # 同一实例内，验证器已经明确拒收且没有新远端证据的候选不应在每场
    # 重复复核。新实例会在 solve_one 入口清空工作区，因此这里不会阻断
    # 下一次真实靶标访问；若后续会话出现远端证据，下面仍允许重新核验。
    _unverified_this_visit: set[str] = set()
    # 连续 unverified-only 检测：如果多场连续产出候选但全被闸门拒收/跳过，
    # 说明验证链不通（如 agent 总是先写文件再 grep，provenance 断裂）。
    # 达到阈值后主动 break，释放靶场槽位，不再死循环。
    _consecutive_unverified_only = 0
    # 平台提示正文只在当前活动访问的内存中保留；StopLoss 只记录是否已经
    # 做过复核，不把题目提示/答案写进共享状态。
    _hint_text = ""
    _hint_reviewed = bool(stoploss.hint_requested(code))
    _hint_review_session: int | None = None
    visit_deadline = time.monotonic() + max(60, visit_seconds)
    if dispatch_deadline is not None:
        visit_deadline = min(visit_deadline, dispatch_deadline)
    # 多 flag 内网链不能因普通轮次的 1800×2 时间盒到点就关闭实例。
    # 在同一次 solve_one 访问里延展到该题剩余 StopLoss 预算（同时受 worker
    # 全局 dispatch_deadline 约束）；这样入口→横向→提权的上下文和目标状态
    # 连续保留，只有提示复核后仍无进展/硬预算耗尽才会离开。
    if (task.flag_count > 1
            and os.environ.get("ADAPTER_CONTINUOUS_MULTIFLAG", "1") == "1"):
        try:
            _multi_remaining = max(0, int(stoploss.remaining_seconds(code)))
        except Exception:
            _multi_remaining = max(60, int(visit_seconds))
        _multi_limit = max(int(visit_seconds), _multi_remaining)
        _extended_deadline = time.monotonic() + _multi_limit
        if dispatch_deadline is not None:
            _extended_deadline = min(_extended_deadline, dispatch_deadline)
        if _extended_deadline > visit_deadline:
            log.info("  %s 多段题保持同一访问：时间盒 %ds 延展至剩余预算 %ds",
                     code, int(max(0, visit_deadline - time.monotonic())),
                     int(max(0, _extended_deadline - time.monotonic())))
            visit_deadline = _extended_deadline
    # BUG-I 修复：result 首赋值在 try 内，若 build_task_prompt/create_solver 在
    # 首场前抛错，except 后返回处会 UnboundLocalError 掩盖原异常 → 提前置 None 兜底。
    result = None
    # The scheduler must be able to distinguish a normal exhausted visit from
    # an account-wide/API or infrastructure failure.  Collapsing everything to
    # ``done`` made the same round immediately open the next target after a
    # zero-tool failure, producing the invalid start/close churn seen in the
    # evaluation run.
    visit_outcome = "done"

    # B16：目标服务故障 → 重启题目容器（close + start），而不是把基础设施故障
    # 记成 solver 的零进展。额度按访问计（stoploss.target_restarts），用尽退回退避。
    _TARGET_RESTART_MAX = int(os.environ.get("ADAPTER_TARGET_RESTART_MAX", "2") or "2")
    _TARGET_RESTART_WAIT = float(os.environ.get("ADAPTER_TARGET_RESTART_WAIT", "2") or "2")
    # 重启成功后补回的时间预算（默认一个会话的量）——否则 continue 后循环顶部
    # should_stop 会立刻停题，新容器一次都用不上。
    _TARGET_RESTART_GRACE = float(os.environ.get("ADAPTER_TARGET_RESTART_GRACE", "2400") or "2400")
    # B21：跨访问刹车 —— 单访问 2 次额度不够时，坏题会被每个访问反复重启
    # （实测某题 22:32、22:36 各重启一次，访问结束后新访问重新计数）。终身累计
    # 封顶 + 重启冷却，让"重启"保持为一次性手段而非周期性容器抖动。
    _TARGET_RESTART_TOTAL_MAX = int(os.environ.get(
        "ADAPTER_TARGET_RESTART_TOTAL_MAX", "6") or "6")
    _TARGET_RESTART_COOLDOWN = float(os.environ.get(
        "ADAPTER_TARGET_RESTART_COOLDOWN", "600") or "600")

    def _restart_target_for_fault() -> bool:
        """Restart a failed target and establish a new evidence instance."""
        nonlocal targets, task, container_active, trace_scope, evidence_policy
        nonlocal board, _visit_tool_outputs, _progress_evidence_gate
        nonlocal _unverified_this_visit
        used = stoploss.target_restarts(code)
        if used >= _TARGET_RESTART_MAX:
            log.info("  %s 目标服务故障：重启额度已用尽（%d/%d）— 退回退避",
                     code, used, _TARGET_RESTART_MAX)
            return False
        # B21：跨访问刹车（终身封顶 + 冷却），防止"坏题每个访问都重启一遍"
        total = stoploss.target_restarts_total(code)
        if total >= _TARGET_RESTART_TOTAL_MAX:
            log.warning("  %s 目标服务故障：重启累计已达上限（%d/%d）— 不再重启，"
                        "交由止损/复活周期处理", code, total, _TARGET_RESTART_TOTAL_MAX)
            return False
        _last_wall = stoploss.last_restart_wall(code)
        if _last_wall and (time.time() - _last_wall) < _TARGET_RESTART_COOLDOWN:
            log.warning("  %s 目标服务故障：距上次重启仅 %.0fs（冷却 %.0fs）— 本次不重启",
                        code, time.time() - _last_wall, _TARGET_RESTART_COOLDOWN)
            return False
        if stop_event.is_set() or _other_solver_active_on(code):
            log.info("  %s 目标服务故障：%s — 不重启", code,
                     "收到重载/停止信号" if stop_event.is_set() else "另一 worker 正在解")
            return False
        log.warning("  %s 目标服务持续 5xx（TARGET_BROKEN）— 重启题目容器（第 %d/%d 次）",
                    code, used + 1, _TARGET_RESTART_MAX)
        try:
            obs.emit("challenge_restart", layer="driver",
                     payload={"code": code, "attempt": used + 1, "reason": "target_fault"})
        except Exception:
            pass
        was_closed = _close_with_retry(client, code)
        _emit_challenge_lifecycle(
            "challenge_close", code, reason="target_fault_restart",
            closed=bool(was_closed), worker_id=_worker_id(),
            task_epoch=_current_task_epoch())
        if not was_closed:
            log.warning("  %s 重启前关闭未获确认 — 不启动第二个实例", code)
            return False
        container_active = False
        if dispatch_deadline is not None:
            remaining = max(0.0, dispatch_deadline - time.monotonic())
            if remaining <= 0:
                return False
            stop_event.wait(min(_TARGET_RESTART_WAIT, remaining))
        else:
            time.sleep(_TARGET_RESTART_WAIT)
        started2, outcome = _start_with_retry(
            client, code, stop_event=stop_event, rate_wait=lambda: None, retries=10,
            deadline=dispatch_deadline)
        if started2 is None:
            log.warning("  %s 重启失败（%s）— 退回退避", code, outcome or "start_failed")
            return False
        container_active = True
        _emit_challenge_lifecycle(
            "challenge_start", code, reason="target_fault_restart",
            worker_id=_worker_id(), task_epoch=_current_task_epoch())
        stoploss.record_target_restart(code)
        stoploss.record_progress(code)   # 基础设施故障不计 solver 零进展
        stoploss.grant_time(code, _TARGET_RESTART_GRACE)   # 也不计时间预算
        new_targets = getattr(started2, "container_addr", []) or []
        # A container restart is a new live target instance even when the
        # platform happens to reuse the same address.  Old transcripts,
        # response files, fact state and unverified candidates cannot prove
        # anything about its replacement.  Preserve only same-epoch
        # answer-free StopLoss/confirmed-progress/checkpoint state.
        prior_confirmed = _confirmed_progress_count(workdir, task)
        trace_scope = _new_trace_scope()
        targets = new_targets
        _purge_stale_solutions(
            workdir, code, targets, task_epoch=_current_task_epoch(),
            trace_scope=trace_scope)
        write_context_md(workdir)
        task = build_task(ch, workdir, targets=targets)
        evidence_policy = flag_evidence_policy(
            task.category or "", targets=task.targets, files=task.files,
            workdir=task.workdir)
        task.correct_flag_count = max(
            int(getattr(task, "correct_flag_count", 0) or 0),
            int(prior_confirmed or 0),
            int(_initialize_confirmed_progress(workdir, task) or 0),
        )
        with _BOARDS_LOCK:
            _SHARED_BOARDS.pop(code, None)
        board = _shared_board_for(code, workdir)
        board.objective = task.objective
        board.seed_goals(goals_for_category(task.category or ""))
        _visit_tool_outputs = []
        _progress_evidence_gate = _ProgressEvidenceGate(evidence_policy)
        _unverified_this_visit.clear()
        log.info("  %s 已重启并隔离旧实例证据，新 targets=%s", code, targets)
        return True

    try:
        while (time.monotonic() < visit_deadline and not stop_event.is_set()
               and not task_dead.is_set()):
            if dispatch_deadline is not None and time.monotonic() >= dispatch_deadline:
                break
            _beat()
            # 多段题达到连续无进展阈值后先做一次平台提示复核，再允许最终
            # 止损。提示正文只进入当前访问的下一次 Pi prompt，不写入共享账本；
            # 即使平台没有返回正文，也登记"已复核"，避免在同一题上刷 hint API。
            if (task.flag_count > 1 and not _hint_reviewed
                    and (_force_hint_review or stoploss.should_request_hint(code))):
                if _multiflag_hint_review_enabled():
                    _hint_ok, _hint_text = _request_multiflag_hint(client, code)
                else:
                    _hint_ok, _hint_text = True, ""
                    log.info("  %s 多段题提示复核已由配置关闭 — 进入最终复核窗口", code)
                # 一次请求（包括空提示/端点不可用）就是一次 review 尝试；
                # 只有成功登记后才打开 post-hint dry 计数，防止重复调用。
                if stoploss.record_hint_requested(code):
                    _hint_reviewed = True
                    _force_hint_review = False
                    _hint_review_session = session_idx
                    try:
                        obs.emit("hint_review", layer="driver", payload={
                            "code": code,
                            "available": bool(_hint_text),
                            "request_ok": bool(_hint_ok),
                            "session": session_idx,
                        })
                    except Exception:
                        pass
                    log.info("  %s 已完成一次提示复核（正文%s，下一场保留完整探索窗口）",
                             code, "可用" if _hint_text else "不可用")
            # 止损检查
            stop, reason = stoploss.should_stop(code)
            if stop:
                log.info("  stop-loss on %s: %s", code, reason)
                visit_outcome = "dropped"
                if task.flag_count > 1 and stoploss.hint_requested(code):
                    try:
                        _ev = stoploss.abandonment_evidence(code)
                        obs.emit("challenge_abandoned", layer="driver", payload=_ev)
                    except Exception:
                        pass
                break

            # ── session 时长策略 ──
            # 多段渗透题（flag_count>1）：持续会话——不断开。
            #   理念：渗透链（侦察→突破→横向→提权→取证）中断后需要昂贵的上下文重建
            #   （读 MEMORY.md + 重放事实 + agent 重新定位攻击位置），打断即丢"手感"。
            #   pi 内部自带上下文压缩，长会话不会溢出；卡死由 stall watchdog（15min）兜底；
            #   通关由 stop_check（全 flag 提交）立即终止；预算由 visit 截止/止损硬顶。
            # 其他题（单 flag）：保持时间盒轮转——session 边界重置上下文对卡住的题有益。
            _max_turns = solver.max_turns
            _dispatch_remaining = (
                int(dispatch_deadline - time.monotonic())
                if dispatch_deadline is not None else int(visit_deadline - time.monotonic())
            )
            if task.flag_count > 1 and os.environ.get("ADAPTER_CONTINUOUS_MULTIFLAG", "1") == "1":
                _max_turns = _multiflag_session_turn_limit(solver)
                sess_secs = min(
                    int(visit_deadline - time.monotonic()),
                    stoploss.remaining_seconds(code),
                    _dispatch_remaining,
                )
                if session_idx == 0:
                    log.info("  continuous session (多段渗透题): 最长 %ds / 最多 %d 工具调用 "
                             "— 通关/截止/止损才断开",
                             sess_secs, _max_turns)
            else:
                _max_turns, _adaptive_secs, _budget_note = _adaptive_session_limits(
                    ch, solver, session_idx, _last_session_facts, _last_session_flags)
                _sess_base = _adaptive_secs
                if _last_session_flags > 0:
                    _mult = float(os.environ.get("ADAPTER_HOT_SESSION_MULT", "1.5") or "1.5")
                    _sess_base = int(solver.session_seconds * max(1.0, _mult))
                    log.info("  adaptive session: %ds (上一场 +%d flags，攻势延续不打断)",
                             _sess_base, _last_session_flags)
                else:
                    log.info("  %s", _budget_note)
                sess_secs = min(
                    _sess_base,
                    int(visit_deadline - time.monotonic()),
                    stoploss.remaining_seconds(code),
                    _dispatch_remaining,
                )
            if sess_secs < 60:
                break

            # 读取前次记忆
            prior_mem = os.path.join(workdir, "MEMORY.md")

            # 构建 prompt
            with submitted_lock:
                done_count = len(submitted.get(code, set()))
            # 事件账本只保留打码后的 flag 前缀，重启后无法可靠还原完整候选；
            # 平台列表的 confirmed count 才是跨访问的无明文进度真相。
            # ``submitted`` is process-local and only contains candidate
            # hashes observed by this worker.  Include the current epoch's
            # durable lower bound so a restart/worker hand-off preserves the
            # platform-confirmed count in the prompt without reading any flag
            # plaintext.
            done_count = max(
                done_count,
                int(getattr(task, "correct_flag_count", 0) or 0),
                _confirmed_progress_count(workdir, task),
            )
            # Make the session boundary explicit to the fresh Pi process.  A
            # numeric N/M alone was not enough: a recorded multi-stage trace
            # showed the model rediscovering and re-emitting accepted stages on
            # every max-turn session.  The checkpoint contains only counters
            # and enum labels; no flag text, command output, or credentials.
            _continuation = _load_continuation_checkpoint(
                workdir, _task_epoch_for(task))
            _continuation_note = _continuation_prompt_note(
                _continuation,
                session_idx=session_idx,
                confirmed_count=done_count,
                expected_count=max(1, int(getattr(task, "flag_count", 1) or 1)),
            )
            # 连续 unverified-only 时，向 Agent 提供完整候选详情和拒绝原因，
            # 让它知道自己手里的 flag 就是之前被拒的那条，并给出补救建议。
            # 阈值=1：首场被拒后，第二场就给详情，给 Agent 两次修正机会。
            _unverified_details = []
            if _consecutive_unverified_only >= 1:
                for _uv_body in sorted(_load_unverified_flags(workdir))[:5]:
                    _unverified_details.append({
                        "body": _uv_body,
                        "reason": "remote_only: flag 未出现在远端命令的直接输出中",
                        "count": _consecutive_unverified_only,
                    })
            # 子 Agent 自评触发：首场结束后（约5分钟），强制 Agent 总结并决定
            # 是否需要帮手。不靠 Agent 自觉——强制注入自评提示。
            # [硬闭环] 未解场次 ≥2 且上一场零新 flag：自评升级为硬性首步，
            # 必须先派 checker 复盘全部产物与思路、拿回结论再自己继续。
            # Agent 普遍高估自己（实测全史派发数 0、自评里"帮手决策"一项
            # 直接跳过），建议式措辞不起作用，只能把指令变成硬性要求。
            # 开关跟随 subagent_enabled：能力关掉时自动回落到下方自评提示。
            _subagent_nudge = ""
            _expected_flags = max(1, int(getattr(task, "flag_count", 1) or 1))
            if (session_idx >= 2 and _last_session_flags == 0
                    and done_count < _expected_flags and subagent_enabled()):
                _subagent_nudge = (
                    "## ⏰ 硬性首步：本场先派 `checker` 复盘，拿回结论再自己继续\n\n"
                    "这道题已连续多场未解出，且上一场没有新 flag。"
                    "**你的第一条回复必须是一次 `subagent` 工具调用**——派 "
                    "`checker` 用干净上下文独立复盘本题全部已有工作；"
                    "拿到它的结论之前，禁止执行任何其他工具。\n\n"
                    "派活内容必须自带背景（子 Agent 看不到你的任何历史，缺了等于白派）：\n"
                    "1. 把你目前的理解、已试主线和卡点浓缩成几句话写进 task；\n"
                    "2. 让它独立读工作区全部产物（MEMORY.md、tried_commands.md、"
                    "artifacts/、逆向/扫描输出等）梳理；\n"
                    "3. 要它回答三件事：① 最接近成功的是哪条线、卡在哪一步；"
                    "② 哪些方向已被证据排除（别再试）；③ 下一步最值得验证的一个"
                    "具体假设——结论必须附命令原文与输出片段。\n\n"
                    "拿到复盘结论后结合它的建议自己继续；若它的判断与你相反，"
                    "先用一条命令验证分歧点再选路线。\n"
                    "若 `subagent` 工具调用失败或不可用，立即自己继续，不要卡在等帮手上。"
                )
            elif session_idx >= 1 and done_count == 0:
                _subagent_nudge = (
                    "## ⏰ 场间自评（上一场未解出，请先总结再继续）\n\n"
                    "你已经做了一场但还没找到答案。在继续执行命令之前，"
                    "请先输出以下 4 项自评，**然后立即继续行动**（同一条回复中"
                    "既包含自评文字也包含接下来的命令调用）：\n\n"
                    "**1. 实际难度**：比平台标注的简单/相当/更难？为什么？\n"
                    "**2. 已尝试方向**：列出试过的攻击面和结果\n"
                    "**3. 当前卡点**：最大障碍是什么？\n"
                    "**4. 帮手决策**：\n"
                    "   - 多个独立攻击面未探 → 派 `scout` 并行侦察\n"
                    "   - 某方向需大量枚举/逆向 → 派 `worker` 深挖\n"
                    "   - 有发现但不确信 → 派 `checker` 复核\n"
                    "   - 方向明确只差最后一步 → 自己继续\n\n"
                    "输出自评后**立即开始执行命令**，不要等待。"
                    "如果觉得需要帮手，现在就调用 `subagent` 工具。"
                )

            prompt = build_task_prompt(
                task, board,
                hint=_hint_text or None,
                prior_memory_path=prior_mem if os.path.isfile(prior_mem) else None,
                session_idx=session_idx,
                flags_submitted=done_count,
                continuation_note=_continuation_note,
                subagent_nudge=_subagent_nudge,
                spray_alert=_multiflag_hint_prompt_note(
                    hint_reviewed=_hint_reviewed,
                    hint_text=_hint_text,
                    hint_review_session=_hint_review_session,
                    session_idx=session_idx,
                    last_session_facts=_last_session_facts,
                    last_session_repeats=_last_session_repeats,
                ),
                # [B16] 把平台判错过的候选回灌给下一场：过去账本只用于跳过
                # 提交，agent 毫不知情 → 同一诱饵串被反复「发现」再被判错。
                rejected_flags=sorted(_load_rejected_flags(workdir)),
                # [B45] 闸门拒收、从未上平台的候选：措辞只说「未坐实」，不能说
                # 「判错」（其中混着真 flag），且已在收口点从 FLAG 摘除。
                unverified_flags=sorted(_load_unverified_flags(workdir)),
                # 连续 unverified-only 时提供完整候选详情（body+原因+次数），
                # 让 Agent 知道自己正在重复发现的 flag 就是被拒的那条。
                unverified_details=_unverified_details or None,
                # [B61] 观察者的镜子（默认关，ADAPTER_HEIMDALL=1 打开）。空串时
                # taskprompt 整段不注入 —— 未启用与「观察者本场没出图」同形。
                heimdall_map=_heimdall_map_for(workdir),
            )

            # 准备 solver 配置
            from dataclasses import replace
            solver_this = replace(
                solver,
                # Multi-flag chains receive their own bounded turn allowance.
                # Previously this branch silently put them back on the global
                # 60-call cap despite declaring a continuous session above.
                max_turns=_max_turns,
                session_seconds=max(60, sess_secs),
            )

            new_facts = [0]

            def _on_fact(tool, args, output, _nf=new_facts):
                # Keep the liveness heartbeat streaming, but defer fact
                # extraction until Pi returns its ordered event list.  Blank
                # `curl -o` events establish current-target response lineage;
                # filtering only non-empty callbacks would either lose that
                # valid route or let arbitrary local reads reset StopLoss.
                _update_status(last_activity=time.time())

            # 转录路径以本次 live instance 的随机 scope 隔离。round/session
            # 是证据顺序的唯一来源；mtime 可因重启/复制而变化，不参与判定。
            tpath = os.path.join(workdir, "_transcripts",
                                 _trace_filename(trace_scope, round_idx, session_idx))

            stoploss.start_session(code)
            stop, reason = stoploss.should_stop(code)
            if stop:
                log.info("  stop-loss before Pi session on %s: %s", code, reason)
                break

            obs.emit("session_start", layer="driver",
                     payload={"code": code, "round": round_idx, "idx": session_idx})

            _beat()

            # 执行 Pi Agent 会话（唯一求解引擎）
            solver_backend = create_solver(
                model=os.environ.get("ADAPTER_SOLVER_MODEL", ""),
                skills_dir=os.environ.get("ADAPTER_SKILLS_DIR", ""),
                max_turns=_max_turns,
                thinking=os.environ.get("ADAPTER_PI_THINKING", ""),
            )
            flags_before = len(accepted_flags)
            # A duplicate in a multi-flag task is meaningful even when it is
            # not a new local flag.  Snapshot the durable platform-confirmed
            # lower bound so the session's zero-flag accounting can recognize
            # that progress and avoid an erroneous stop-loss increment.
            confirmed_before = max(
                _confirmed_progress_count(workdir, task),
                int(getattr(task, "correct_flag_count", 0) or 0),
            )
            session_wall0 = time.time()

            # Eager flag submission: background thread watches FLAG file, submits immediately
            _eager_stop = threading.Event()
            _solved_flag = [False]
            _confirmed_activity = [False]
            _eager_thread = threading.Thread(
                target=_eager_submit_loop, daemon=True, name="eager-submit",
                kwargs=dict(
                    stop_evt=_eager_stop, client=client, code=code,
                    workdir=workdir, task=task, verifier=verifier,
                    attempt_id=str(round_idx), transcript_path=tpath,
                    trace_scope=trace_scope,
                    submitted=submitted, submitted_lock=submitted_lock,
                    accepted_flags=accepted_flags, obs=obs,
                    solved_flag=_solved_flag,
                    stoploss=stoploss,
                    progress_seen=_confirmed_activity,
                ),
            )
            _eager_thread.start()
            _update_status(session_active=True, session_started_at=time.time(),
                           last_activity=time.time(),
                           last_event=f"session start {code}#{session_idx}")

            try:
                result = solver_backend.solve(
                    prompt, workdir, solver_this,
                    flag_format=task.flag_format,
                    on_fact=_on_fact,
                    transcript_path=tpath,
                    stop_check=lambda: (
                        _solved_flag[0] or task_dead.is_set() or stop_event.is_set()
                        or (dispatch_deadline is not None
                            and time.monotonic() >= dispatch_deadline)
                    ),
                )
            finally:
                _eager_stop.set()
                _eager_thread.join(timeout=10)
                if _eager_thread.is_alive():
                    log.warning("eager thread did not exit in time for %s", code)
                else:
                    # The agent process and its delivery watcher have both
                    # stopped, so neither can append a second stage while we
                    # prune platform-confirmed lines.  Keeping flag #1 here
                    # would make the next Pi process rediscover it before
                    # continuing toward flags #2..N.
                    _prune_confirmed_delivery_candidates(
                        workdir, code, submitted, submitted_lock)
                    _prune_suppressed_delivery_candidates(workdir)

            # ``result.tool_outputs`` covers only the Pi process that just
            # ended.  Keep the ordered visit-local prefix as a fail-closed
            # fallback for a just-flushed transcript; the normal verifier
            # below reads the scope-bound transcript stream so a future
            # process cannot lose same-instance provenance.
            _visit_tool_outputs.extend(list(result.tool_outputs or []))
            for _fact_tool, _fact_args, _fact_output in (result.tool_outputs or []):
                new_facts[0] += _observe_qualified_tool_facts(
                    board, _progress_evidence_gate, workdir,
                    _fact_tool, _fact_args, _fact_output, iter=session_idx)
            _instance_tool_outputs = _tool_outputs_from_current_instance_transcripts(
                workdir, tpath, trace_scope=trace_scope)
            if not _instance_tool_outputs:
                _instance_tool_outputs = list(_visit_tool_outputs)

            # 本场 flag 数在下面清洗后统计（占位/外来不计入自适应时长，B12）

            # eager thread may have already solved the challenge
            if _solved_flag[0]:
                solved = True
            _persist_tried_commands(workdir, result.tool_outputs)
            # 编排加固：本会话 /tmp 产物沉淀到 workdir（跨会话可见 / 重启不丢）
            session_artifacts = _persist_session_artifacts(workdir, session_wall0)

            # [B59-b1] 账号级 API 故障：后续会话必然同样失败，白烧 2 场。
            # 立刻结束本 visit 的会话循环 —— 把配额让给别的题，也让轮级熔断
            # 尽快接管（旧形态要烧满一整轮 ~70 次调用才退避）。
            # 注意必须排除已解出的场次：break 会跳过下面的 solved 清理分支
            # （_purge_plaintext_artifacts），那是合规红线，不能绕。
            if not solved and _is_api_fault(result):
                log.warning("  [B59] %s 本场为账号级 API 故障 — 结束本 visit 的会话循环",
                            code)
                visit_outcome = "api_fault"
                break

            # [B61] 本场结束后观察一次（刻意放在上面 break 的**之后**：账号级
            # 故障那场没有任何可观察内容，不该白烧一次 LLM 调用）。只刷新
            # work/<code>/.heimdall.json，供**下一场**的 build_task_prompt 注入。
            _heimdall_observe(workdir, tpath, session_idx)

            # 记录本场效率信号：只统计 bash 命令，避免把模型文本/其它工具事件
            # 混入重复率。该信号既写入事件账本，也用于下一场 prompt 换向提示。
            _cmd_keys = [
                " ".join(str((args or {}).get("command", "")).split())
                for tool, args, _ in (result.tool_outputs or [])
                if tool == "bash" and str((args or {}).get("command", "")).strip()
            ]
            _last_session_facts = int(new_facts[0])
            _last_session_repeats = max(0, len(_cmd_keys) - len(set(_cmd_keys)))

            obs.emit("session_end", layer="driver",
                     payload={"code": code, "round": round_idx, "idx": session_idx,
                              "turns": result.turns, "flags": len(result.flags),
                              "infra_blocked": result.infra_blocked,
                              "duration_s": round(result.duration_s, 3),
                              "termination_reason": result.termination_reason,
                              "error": str(result.error or "")[:200],
                              "new_facts": new_facts[0],
                              "tool_calls": len(result.tool_outputs or []),
                              "unique_commands": len({
                                  " ".join(str((a or {}).get("command", "")).split())
                                  for t, a, _ in (result.tool_outputs or [])
                                  if t == "bash" and str((a or {}).get("command", "")).strip()
                              }),
                              "repeat_count": _last_session_repeats})

            # ── flag 候选清洗（提前到 INFRA_BLOCKED 判定之前，B12）──────
            # 外壳归一化（FLAG{}→flag{}、去体外污染，只改外壳不改 body）+
            # 剔除外来/占位 flag。占位 flag 不该挡住退避，也不该计入
            # 「上一场 +N flags」把自适应场次无谓延长（实测某题首场）。
            file_flags = _read_flag_file(workdir)
            # 只清洗当前题目/当前实例产生的候选。旧版本这里仍向
            # ``_clean_flag_candidates`` 传入已删除的第二个 ``foreign`` 参数，
            # 导致每个会话在收口阶段抛 TypeError；finally 随后关闭容器，调度器
            # 把本来已经完成的访问记成异常，表现为大量无意义的开关题操作。
            # 跨题答案过滤链已经移除，候选顺序也必须保持稳定。
            all_candidates = _clean_flag_candidates([*result.flags, *file_flags])
            _unverified_this_visit.update(_load_unverified_flags(workdir))
            # [B65] 「上一场 +N flags」是本场是否延长为热场的**唯一**判据。此前它
            # 数的是会话原始产出、不扣任何账本 —— 而核验拒收的候选同样被计入：
            # 实测某题连续 6 场每场都报 +1，热提 3600s 连开 4 次，攻势却一场没推进
            # （那 1 个 flag 每场都是同一条、每场都被拒）。
            # 改为只数**尚未进过任何账本**的候选 —— 两个账本都代表「这条路已经走过」：
            #   .rejected_flags   平台判错（确凿为假）
            #   .unverified_flags 闸门拒收、从未上平台（未坐实，但已知）
            # 真正的热场计数要等平台验真/提交结束后更新（见本场末尾）。
            # 原始候选、闸门拒绝、平台判错都不能延长下一场时间盒。
            _last_session_flags = 0

            # INFRA_BLOCKED 处理（清洗后仍无候选才算真退避）
            if result.infra_blocked and not all_candidates:
                stoploss.record_unreachable(code)
                log.info("  %s INFRA_BLOCKED — backing off", code)
                visit_outcome = "infra_blocked"
                break

            # A process that never reached a tool call cannot have performed
            # target work.  Do not turn this into another same-round lifecycle
            # attempt: return a retryable outcome so the outer dispatcher owns
            # the cooldown.  Flags/progress are explicitly exempt because an
            # eager submit can legitimately finish before Pi emits a tool end.
            if (not solved and not all_candidates and not _confirmed_activity[0]
                    and int(getattr(result, "turns", 0) or 0) <= 0
                    and not (getattr(result, "tool_outputs", None) or [])):
                visit_outcome = "no_effective_session"
                log.warning("  %s session had no usable tool execution — defer via dispatch backoff",
                            code)
                break

            # 只有本场没有网络/目标故障/看门狗静默时才重置不可达计数。过去这里
            # 先清零、场末又记一次不可达，连续故障永远只能累积到 1。
            if (not getattr(result, "target_fault", False)
                    and "stalled" not in str(result.error or "")):
                stoploss.record_reachable(code)

            # 事实更新
            if new_facts[0] > 0:
                stoploss.record_fact(code)
            else:
                stoploss.record_no_progress(code)

            # 验证并提交 flag
            rejected_here = _load_rejected_flags(workdir)   # 跨会话错误账本
            _duplicate_candidates_this_session = 0
            # The eager watcher can finish the complete N/N submission while
            # Pi is still returning its final result.  ``all_candidates`` then
            # contains a stale snapshot of delivery entries that were already
            # accepted by the platform.  Do not let the main-path cleanup
            # submit those residual values again: aside from wasting requests,
            # a trailing decoy can show up as an erroneous post-solve failure
            # after a valid submission.  A partial
            # multi-flag eager submit never sets ``solved``, so its remaining
            # candidates still flow through normally.
            if solved and all_candidates:
                log.info("  %s already completed by eager submission; skip %d residual candidate(s)",
                         code, len(all_candidates))
            for flag_candidate in ([] if solved else all_candidates):
                _nb = _normalize_flag_body(flag_candidate)
                with submitted_lock:
                    if _submitted_contains(submitted, code, flag_candidate):
                        # [B65] 这处 continue 此前**没有任何日志**：核验拒收的候选
                        # 被写进 submitted（见下方 else 分支的修正），下一场再产出
                        # 同一条 body 就在这里静默消失 —— 日志上是「本场 1 flags」
                        # 然后什么都没发生，运维与审计完全看不见（实测连续 5 场）。
                        # 判重本身要保留（已投递过的不重投），但必须留痕。
                        log.info("  skip already-submitted flag %s (submitted 集，本场不再投)",
                                 flag_candidate[:30])
                        _duplicate_candidates_this_session += 1
                        continue
                # 已被平台判错过的 body → 跳过（防止跨会话/跨轮把同一条错答案反复重喷）
                if _nb in rejected_here:
                    log.info("  skip already-rejected flag %s (ledger)", flag_candidate[:30])
                    continue
                if _nb in _unverified_this_visit:
                    # 未验证候选只在同一实例内短路；新的合格远端或本地题
                    # 证据都允许翻案，避免把本地附件题永久误杀。
                    _probe = flag_confidence(
                        flag_candidate, result.observed_output, _instance_tool_outputs,
                        evidence_policy=evidence_policy)
                    if not (_probe.grounded and _probe.confidence >= 0.9):
                        log.info("  skip already-unverified candidate this visit: %s",
                                 flag_candidate[:30])
                        continue
                    log.info("  recheck unverified candidate after new %s evidence: %s",
                             _probe.provenance or "qualified", flag_candidate[:30])

                # 置信度评估
                claim = flag_confidence(
                    flag_candidate,
                    result.observed_output,
                    _instance_tool_outputs,
                    evidence_policy=evidence_policy,
                )

                # 三重验证
                claim = verifier.verify(claim)

                if claim.verified:
                    # [B47] 判断 Agent 拦截（同上；此处取证更硬，但错交代价一致）
                    if _skeptic_check(verifier, claim, workdir, code,
                                      stage="submit", trace_scope=trace_scope) == "veto":
                        _add_unverified_flag(workdir, flag_candidate)
                        _unverified_this_visit.add(_nb)
                        continue
                    # 真实 API: POST /openapi/v1/challenges/submit
                    # 返回: {correct, awarded, cumulative_score, correct_flag_count, ...}
                    submit_result = client.submit_flag(code, flag_candidate)
                    if submit_result.correct:
                        if stoploss.record_flag(code, flag_candidate):
                            _confirmed_activity[0] = True
                    elif submit_result.duplicate:
                        if stoploss.record_duplicate(code, flag_candidate):
                            _confirmed_activity[0] = True
                    confirmed_count = _record_confirmed_submission(
                        workdir, task, flag_candidate, submit_result)
                    task.correct_flag_count = max(
                        int(getattr(task, "correct_flag_count", 0) or 0),
                        int(getattr(submit_result, "correct_flag_count", 0) or 0),
                        confirmed_count,
                    )
                    if stoploss.record_platform_progress(code, task.correct_flag_count):
                        _confirmed_activity[0] = True
                    _emit_flag_submit(obs, code, flag_candidate, submit_result, task,
                                      confirmed_flag_count=confirmed_count)

                    if submit_result.correct:
                        with submitted_lock:
                            submitted.setdefault(code, set()).add(
                                _candidate_sha256(flag_candidate) or _nb)
                        log.info("  FLAG CORRECT on %s: %s (+%d pts, total %d)",
                                 code, flag_candidate[:30],
                                 submit_result.awarded, submit_result.cumulative_score)
                        accepted_flags.append(flag_candidate)
                        _update_status(
                            flags_submitted=max(
                                len(accepted_flags),
                                int(getattr(task, "correct_flag_count", 0) or 0),
                                int(confirmed_count or 0),
                            ),
                            total_earned=submit_result.cumulative_score,
                            last_event=f"FLAG CORRECT {flag_candidate[:20]}",
                            last_log=f"FLAG CORRECT on {code}: +{submit_result.awarded} pts",
                        )
                        # 检查是否所有 flag 都已提交
                        if _submission_complete(task, submit_result, confirmed_count):
                            solved = True
                            log.info("🎉 All %d flags submitted! Closing container immediately.", 
                                     submit_result.correct_flag_count)
                            # ✅ 立即关闭容器，不等循环结束
                            # 全部 flag 已提交即终止：不再继续向平台提交剩余候选
                            # （否则会把占位符 flag{...} 也发出去 → 多余"答题失败"）
                            break
                    elif submit_result.duplicate:
                        with submitted_lock:
                            submitted.setdefault(code, set()).add(
                                _candidate_sha256(flag_candidate) or _nb)
                        # 平台 409 code=duplicate = 该 flag（同 flag_index）已被正确提交过，
                        # 属幂等保护。它只代表"这一个 flag 已收过"，不代表整道题完成。
                        #
                        # 多 flag 题：绝不得 solved、不得关容器 —— 仅这一个 flag 已入账
                        # （被本舰队其他 worker / 平台侧历史收过），本题可能还有剩余 flag
                        # 未收集。直接关容器会把这类多 flag 题卡死在 1/N（实测根因）。
                        # 这里跳过该候选、不终止本场，继续攻击剩余 flag；整题完成仍由上方
                        # correct 分支 correct_flag_count>=total_flag_count 判定（收齐即关）。
                        if task.flag_count > 1:
                            if _submission_complete(task, submit_result, confirmed_count):
                                solved = True
                                log.info("duplicate confirms all %d flags for %s",
                                         task.flag_count, code)
                                break
                            log.info("  DUPLICATE flag on %s (already banked) — 多flag题(%d)，跳过该flag继续找剩余",
                                     code, task.flag_count)
                            # 该 flag 是真实 flag（已被平台收过），只重置本场空转计数，
                            # 不递增本地 flags_found：它已在别处入账，不能伪造 N/N 进度。
                            _update_status(
                                last_event=f"dup skip {code}",
                                last_log=f"duplicate flag on {code} skipped (多flag题继续)",
                            )
                            # 该候选已被平台收过，无需再投；continue 处理其余候选，容器保持在线
                            continue
                        #
                        # 单 flag 题：平台已收过即整题实际完成（可能被其他 worker/早前轮次
                        # 解除），但平台把 is_completed 保持 False 重复下发；若不当已解处理
                        # 会让 auto_dispatch_loop 无限重派同一道重复题（死循环烧资源）。
                        # 故单 flag 题保持旧行为：视为已解，关容器，终止本场。
                        log.info("  DUPLICATE flag on %s (already submitted) — 视为已解，关闭容器", code)
                        solved = True
                        accepted_flags.append(flag_candidate)
                        # 这里不再单独 +1 challenges_solved；由末尾 if solved
                        # 统一计一次，避免 duplicate 路径双计。
                        _update_status(
                            last_event=f"duplicate solved {code}",
                            last_log=f"duplicate flag on {code} (already banked)",
                        )
                        # 视为已解后即终止，避免继续提交同一 code 的其余候选 flag
                        # （会导致重复 409 / 二次计数）
                        break
                    else:
                        log.info("  flag INCORRECT on %s: %s", code, flag_candidate[:30])
                        # 入错误账本：同一 body 本场后续及其他场/轮永不再提交
                        _add_rejected_flag(workdir, flag_candidate)
                        rejected_here.add(_nb)
                        _fail_submit_streak += 1
                        if _fail_submit_streak >= _FAIL_SUBMIT_LIMIT:
                            log.warning("  %s: %d consecutive failed submits — 提交冷却（幻觉防护）",
                                        code, _fail_submit_streak)
                            break
                else:
                    # Fallback: verifier 拒绝但 flag「先出现在工具输出」（真外部来源）→ 强制提交。
                    # 解决 agent_authored 误判（真 flag 先出现在靶场响应输出里）
                    # 和 not_grounded 误判（tool_outputs 解析不完整）。
                    # 时序判别：agent 自写/猜的 flag（echo > FLAG）经 cat/tee 回显也会
                    # 出现在输出里（实测 flag{guess} 被强提 INCORRECT 烧配额）——
                    # 参数先现不算真来源（见 _flag_grounded_in_transcripts）。
                    try:
                        _force = _flag_grounded_in_transcripts(workdir, flag_candidate,
                                                               require_remote=False,
                                                               evidence_policy=evidence_policy,
                                                               trace_scope=trace_scope)
                    except Exception:
                        _force = False
                    # [B55] 非标准信封复核通道。
                    # `invalid_format`（外壳不是 flag{...}）此前在这里被**静默丢弃** ——
                    # 只进未验证账本、永不上平台，判断 Agent 也见不到。它结构上
                    # **只可能来自 FLAG 文件**：extract_flags 用 flag\{[^}]{1,200}\}
                    # 严格匹配，工具输出那条路产不出非信封候选。所以这条通道等价于
                    # 「agent 明确写进交付槽、但外壳不合规」。
                    # 平台提交契约没规定格式，`flag{}` 是框架自己的假设 —— 万一某题
                    # 答案不是这个形状，真解会永久死在门外。改由判断 Agent 复核，
                    # 复用 _skeptic_check 的 refuse 语义：证据为空不翻案、conf>=0.75、
                    # 超时/异常一律「无意见」。
                    _rescued = False
                    if (not _force and claim.reject_reason == "invalid_format"
                            and _fmt_rescue_on()):
                        _rescued = (_skeptic_check(verifier, claim, workdir, code,
                                                   stage="refuse",
                                                   trace_scope=trace_scope) == "rescue")
                        if _rescued:
                            log.info("  [B55] 判断 Agent 复核放行非标准信封候选 → 提交: %s",
                                     flag_candidate[:30])
                    if (_force or _rescued) and _hallu is not None \
                            and _hallu.should_suppress_force(workdir):
                        # [B54] 幻觉族超阈 → 关掉本题强提通道。强提本是「误杀补偿」，
                        # 但它同样会拿编造的串去烧平台提交配额（实测自造串被强提
                        # INCORRECT）。幻觉计数到阈说明这条补偿在这里只会放大损失。
                        # 只认幻觉族：推导族（影子审计 19/29 为真）永不触发。
                        # [B55] 复核放行的候选同样受此闸约束 —— 它也烧配额。
                        _add_unverified_flag(workdir, flag_candidate, claim.reject_reason)
                        _unverified_this_visit.add(_nb)
                        log.warning("  [B54] 幻觉族超阈 → 本题强提交通道已关闭: %s",
                                    flag_candidate[:30])
                        continue
                    if _force or _rescued:
                        # [B47] 判断 Agent 拦截：强提通道是误杀的主要补偿手段，
                        # 也是错交的主要风险口 —— 提交前由 LLM 读证据把一道关。
                        # [B55] 已复核放行的候选**不再问第二次**：刚判过
                        # genuine>=0.75，再用同一份证据问 submit 只是浪费一次
                        # LLM 调用，且可能被 0.85 的更高阈值反手否决掉刚救回的
                        # 真解 —— 那正是这条通道要消除的误杀。
                        if _force and _skeptic_check(verifier, claim, workdir, code,
                                                     stage="submit",
                                                     trace_scope=trace_scope) == "veto":
                            _add_unverified_flag(workdir, flag_candidate)
                            _unverified_this_visit.add(_nb)
                            continue
                        if _force:
                            log.info("  verifier REJECT (%s) but flag in tool output → force submit %s",
                                     claim.reject_reason, flag_candidate[:30])
                        # 走提交流程（复制下面的 submit 逻辑）
                        try:
                            submit_result = client.submit_flag(code, flag_candidate)
                            if submit_result.correct:
                                if stoploss.record_flag(code, flag_candidate):
                                    _confirmed_activity[0] = True
                            elif submit_result.duplicate:
                                if stoploss.record_duplicate(code, flag_candidate):
                                    _confirmed_activity[0] = True
                            confirmed_count = _record_confirmed_submission(
                                workdir, task, flag_candidate, submit_result)
                            task.correct_flag_count = max(
                                int(getattr(task, "correct_flag_count", 0) or 0),
                                int(getattr(submit_result, "correct_flag_count", 0) or 0),
                                confirmed_count,
                            )
                            if stoploss.record_platform_progress(code, task.correct_flag_count):
                                _confirmed_activity[0] = True
                            _emit_flag_submit(obs, code, flag_candidate,
                                              submit_result, task,
                                              force=True,
                                              confirmed_flag_count=confirmed_count)
                            if submit_result.correct:
                                with submitted_lock:
                                    submitted.setdefault(code, set()).add(
                                        _candidate_sha256(flag_candidate) or _nb)
                                log.info("  ⚡ [FORCE] FLAG CORRECT on %s: %s (+%d pts, total %d)",
                                         code, flag_candidate[:30], submit_result.awarded,
                                         submit_result.cumulative_score)
                                accepted_flags.append(flag_candidate)
                                _update_status(flags_submitted=max(
                                                   len(accepted_flags),
                                                   int(getattr(task, "correct_flag_count", 0) or 0),
                                                   int(confirmed_count or 0)),
                                               total_earned=submit_result.cumulative_score,
                                               last_event=f"FORCE FLAG {flag_candidate[:20]}")
                                if _submission_complete(task, submit_result, confirmed_count):
                                    solved = True
                                    break
                            elif submit_result.duplicate:
                                with submitted_lock:
                                    submitted.setdefault(code, set()).add(
                                        _candidate_sha256(flag_candidate) or _nb)
                                if task.flag_count > 1:
                                    if _submission_complete(task, submit_result, confirmed_count):
                                        solved = True
                                        log.info("[FORCE] duplicate confirms all %d flags for %s",
                                                 task.flag_count, code)
                                        break
                                    continue
                                solved = True
                                accepted_flags.append(flag_candidate)
                                break
                            else:
                                _add_rejected_flag(workdir, flag_candidate)
                                rejected_here.add(_nb)
                        except Exception as e:
                            log.warning("  [force] submit error: %s", e)
                    else:
                    # [B65] 这里**不再**把候选写进 submitted 判重集。
                        # submitted 的语义是「已投递到平台」，而本分支的候选
                        # **从未上过平台** —— 写进去等于把 B45 反复强调的
                        # 「未验证 ≠ 判错」又抹平了，且后果是承重的：
                        #   ① 下一场同一条 body 在循环开头被静默 continue，
                        #      日志上完全消失（实测连续 5 场只剩「1 flags」）；
                        #   ② submitted 的生命周期是整个 round（schedule_rounds
                        #      局部变量），一旦写入该 body 本轮**永久**无法重投；
                        #   ③ B47 存在的**前提**正是闸门会误杀 —— 一条被误杀的
                        #      真 flag 即便后来在靶标响应里现了真身（B54 的
                        #      「输出里出现过 = 有据」），也永远救不回来。
                        # 去重职责交回账本：下方写入的 .unverified_flags，与循环
                        # 开头那关（有日志）的 .rejected_flags。下一次新实例访问会
                        # 重新核验同一条 body；同一实例内只有出现新的远端证据才翻案，
                        # 避免静态候选在连续 session 中反复消耗 verifier/会话额度。
                        log.info("  flag REJECTED by verifier on %s: %s (reason: %s)",
                                 code, flag_candidate[:30], claim.reject_reason)
                        # [B45] 从未上平台 ⇒ 不属 .rejected_flags 语义（那是确凿为假），
                        # 单列未验证账本并摘出 FLAG 交付槽 —— 否则下一场一进门读到
                        # 非空 FLAG 就判已解收工（B27 只覆盖了平台判错侧）。
                        _add_unverified_flag(workdir, flag_candidate,
                                             claim.reject_reason)
                        _unverified_this_visit.add(_nb)

            # Main-path submissions happen after the eager watcher has
            # stopped.  Trim their acknowledged delivery lines as well, so a
            # successor multi-stage session sees only work that is still
            # pending rather than repeatedly parsing a flag already banked by
            # the platform.
            if not _eager_thread.is_alive():
                _prune_confirmed_delivery_candidates(
                    workdir, code, submitted, submitted_lock)

            # 连续 unverified-only 检测：本场有候选但全被 skip/reject、无入账
            # → 说明验证链不通（agent 反复发现同一条 flag 但来源证据不足）。
            # 累积达到阈值后主动退出，释放靶场槽位，防止 8+ session 死循环。
            _this_session_all_unverified = (
                len(accepted_flags) <= flags_before
                and not _confirmed_activity[0]
                and all_candidates  # 有候选但全被跳过/拒绝
            )
            if _this_session_all_unverified:
                _consecutive_unverified_only += 1
            else:
                _consecutive_unverified_only = 0
            _UNVERIFIED_BREAK_LIMIT = int(
                os.environ.get("ADAPTER_UNVERIFIED_BREAK_LIMIT", "3") or "3")
            if _consecutive_unverified_only >= _UNVERIFIED_BREAK_LIMIT:
                log.warning(
                    "  %s 连续 %d 场仅有 unverified/rejected 候选（验证链不通）"
                    " — 主动退出，释放靶场槽位",
                    code, _consecutive_unverified_only)
                visit_outcome = "unverified_loop"
                break

            # 只由平台确认/duplicate 的真实入账驱动热场；候选、拒绝、错误提交
            # 都不会拉长下一轮时间盒。
            _last_session_flags = max(0, len(accepted_flags) - flags_before)

            # G3: 本场无新入账 flag → 累计零 flag 会话（与事实洪流解耦的止损）。
            # 但看门狗因长静默杀的会话（stalled_no_output）是"时间/基础设施"问题，
            # 不是"策略零进展"——慢速但真实的 hard RE 不应被误判为零进展而止损。
            # 改为 record_unreachable（连续不可达 3 次仍会停，语义正确）。
            confirmed_after = max(
                _confirmed_progress_count(workdir, task),
                int(getattr(task, "correct_flag_count", 0) or 0),
            )
            confirmed_progress = (
                confirmed_after > confirmed_before or _confirmed_activity[0])
            if len(accepted_flags) <= flags_before and not confirmed_progress:
                if result.error and "stalled" in str(result.error):
                    stoploss.record_unreachable(code)
                    log.info("  %s session stalled (watchdog) — 按不可达退避，不计 zero_flag", code)
                elif _is_api_fault(result):
                    # [B59-b2] 账号级故障是**零信息**：既不是零进展，也不是进展。
                    # 与 2624 行「基础设施故障不计 solver 零进展」同一口径。
                    # 这里**什么都不记**（不 record_zero_flag 也不 record_progress）——
                    # 记 zero_flag 会把好题误杀（旧 bug），记 progress 会把真该停的题
                    # 无限续命。不推进也不重置，额度恢复后按原窗口继续。
                    log.warning("  [B59] %s 本场为账号级 API 故障 — 不计零进展，"
                                "止损窗口保持不变", code)
                else:
                    # 产物会保留给下一场使用，但下载脚本、JS 缓存等不能单独证明
                    # 解题推进；否则会把连续跑满时间盒的空转伪装成"有进展"。
                    if session_artifacts:
                        log.info("  %s 本场沉淀 %d 个产物，但无平台确认 flag — 仍计 zero-flag",
                                 code, session_artifacts)
                    stoploss.record_zero_flag(code)
            elif len(accepted_flags) <= flags_before and confirmed_progress:
                # A duplicate or a platform baseline update can advance the
                # task without adding to this worker's accepted list.  It is
                # real progress, so do not charge this session as zero-flag.
                log.info("  %s session advanced confirmed progress %d→%d; skip zero-flag",
                         code, confirmed_before, confirmed_after)

            # Persist a small, answer-free hand-off before deciding whether to
            # open another Pi process.  In the old flow a max-turn session
            # returned only ``N/M``; the fresh process then treated the task as
            # a new puzzle and repeated the entry path.  The checkpoint carries
            # no candidate text or tool output, and is scoped to this task
            # epoch by ``_load_continuation_checkpoint``.
            if task.flag_count > 1:
                _last_duplicate_candidates = int(
                    _duplicate_candidates_this_session or 0)
                _checkpoint_term = "target_fault" if getattr(result, "target_fault", False) else (
                    str(getattr(result, "termination_reason", "") or "").strip().lower())
                _write_continuation_checkpoint(
                    workdir,
                    task_epoch=_task_epoch_for(task),
                    session=session_idx,
                    confirmed_count=confirmed_after,
                    expected_count=max(1, int(getattr(task, "flag_count", 1) or 1)),
                    new_facts=int(new_facts[0] or 0),
                    repeat_count=int(_last_session_repeats or 0),
                    duplicate_candidates=_last_duplicate_candidates,
                    termination=_checkpoint_term,
                    pivot_required=bool(
                        _last_duplicate_candidates > 0
                        or _last_session_repeats >= 2
                        or int(new_facts[0] or 0) <= 0
                        or _checkpoint_term in {"max_turns", "stalled", "timeout"}),
                    fact_kinds=[str(getattr(fact, "kind", "") or "")
                               for fact in getattr(board, "facts", [])],
                )

            if solved:
                # 只在平台确认整题收齐时发出完成事件。多 flag 题的单个
                # correct/duplicate 只代表部分进展，控制台不能据此误报整题完成。
                obs.emit("challenge_solved", layer="driver",
                         payload={"code": code, "flags": len(accepted_flags)})
                _update_status(challenges_solved=int(_STATUS["challenges_solved"]) + 1,
                               last_event=f"solved {code}")
                # 合规加固：题解入账后立即清理本目录明文答案物证（FLAG/SOURCE/MEMORY.md），
                # 防平台重发同题码时被判定"内置赛题信息/使用外部历史答题记忆"
                try:
                    # [B52] solved=True → 连账本与转录一起清（解完即清）。
                    _purge_plaintext_artifacts(workdir, code, solved=True)
                except Exception:
                    log.warning("[compliance] clean failed for %s", code, exc_info=True)
                break

            # 写入记忆：保留 agent 自写的结构化笔记，driver 事实进固定段
            # （_merge_memory 幂等替换，不再整体覆写 MEMORY.md）
            handoff = result.handoff or ""
            if not handoff:
                # B14：本场没产出续接块（预算耗尽/被杀的场次最常见）→ 保留上一场
                # 的块。否则 _merge_memory 重建 driver 段会把上次交接抹掉，下一场
                # 又从零摸索（跑了 10 场，MEMORY.md 仍只有 310B 噪音）。
                try:
                    with open(os.path.join(workdir, "MEMORY.md"), encoding="utf-8") as _mf:
                        handoff = extract_handoff(_mf.read())
                except Exception:
                    handoff = ""
            mem_content = board.actionable_assets()
            if handoff:
                mem_content += f"\n\n{handoff}"
            if mem_content.strip():
                _merge_memory(workdir, mem_content)

            # Do not feed an already-confirmed flag back to the next Pi
            # process through MEMORY/tried_commands/blackboard.  Those files
            # are useful for stage continuity, but retaining answer text makes
            # a fresh max-turn session prone to replaying the same
            # delivery step.  Provenance remains intact in the scoped transcript; the
            # prompt only needs the non-sensitive N/M progress and stage facts.
            if task.flag_count > 1:
                try:
                    _scrub_flag_plaintext(workdir, code)
                    _scrub_live_blackboard(board, workdir, code)
                except Exception:
                    log.warning("[continuation] flag scrub failed for %s", code,
                                exc_info=True)

            # B16：目标服务故障（端口通、后端持续 5xx）→ 本场记忆已落盘，
            # 重启题目容器后继续本访问；额度用尽/失败才按不可达退避。
            # 放在循环末尾：先走完本场的记忆/事实/产物结算，再决定重启。
            # B18：条件用"本场无新入账 flag"而非"无候选" —— 目标宕机时 agent 常
            # 编造候选（实测某题：2 个 placeholder 候选被 verifier 判
            # agent_authored 拒绝，却把重启挡掉了）。候选被拒＝目标仍给不出真实
            # 判词＝该重启；只有真入账才算目标可用。
            if (getattr(result, "target_fault", False)
                    and len(accepted_flags) <= flags_before):
                if _restart_target_for_fault():
                    session_idx += 1
                    _update_status(sessions=session_idx, session_active=False,
                                   session_started_at=0.0, last_activity=time.time())
                    continue
                stoploss.record_unreachable(code)
                log.info("  %s 目标服务故障 — 重启未成/额度用尽，退避", code)
                visit_outcome = "target_fault"
                break

            session_idx += 1
            _update_status(sessions=session_idx, session_active=False,
                           session_started_at=0.0, last_activity=time.time())

    except Exception as e:
        log.exception("solve_one error on %s", code)
        obs.emit("error", layer="driver",
                 payload={"code": code, "error": str(e)[:200]})
    finally:
        _watch_stop.set()   # 停终态看门狗
        # Pi tools can intentionally launch a short-lived listener or poller,
        # but one challenge's detached child must never survive into the next
        # one.  Reap only processes carrying this visit's random trace token;
        # this cannot touch the driver itself, worker-1's VPN, or another
        # worker.  Do it before the close/grace sequence so an unsolved visit
        # releases both platform and local resources promptly.
        try:
            from ghost_worker.adapter.solver.pi_agent import cleanup_instance_processes
            _orphan_count = cleanup_instance_processes(workdir)
            if _orphan_count:
                log.info("  [cleanup] reaped %d detached Pi/tool process(es) for %s",
                         _orphan_count, code)
        except Exception:
            log.warning("  [cleanup] detached process cleanup failed for %s", code,
                        exc_info=True)
        # [B63] 解出后不立刻关靶场：平台侧入账/评分可能仍需要实例存活，
        # 秒关会与提交竞态（用户口径：做完题过 30 秒再关）。
        # ★只对解出路径等待 —— 未解路径必须立刻腾出平台仅有的 3 个槽位。
        # ``solved`` 已由平台累计确认数判定，不再用本次 accepted_flags 长度；
        # 否则"前几次访问已有部分 flag、本次补齐最后一条"会跳过 grace/close。
        # 可 ADAPTER_CLOSE_GRACE_SECONDS 调参，设 0 关闭。
        if solved:
            try:
                _grace = float(os.environ.get("ADAPTER_CLOSE_GRACE_SECONDS", "30") or "90")
            except ValueError:
                _grace = 30.0
            if dispatch_deadline is not None:
                _grace = min(_grace, max(0.0, dispatch_deadline - time.monotonic()))
            if _grace > 0:
                log.info("  solved — 延迟 %.0fs 关靶场（避开与平台入账的竞态）", _grace)
                _grace_end = time.monotonic() + _grace
                while time.monotonic() < _grace_end and not stop_event.is_set():
                    time.sleep(min(1.0, max(0.0, _grace_end - time.monotonic())))
        # 每个 solve_one 只走一次统一关闭路径。提交分支不再自行 close，避免
        # "立即 close + finally close"造成重复关闭操作和平台 409 噪声。
        if container_active:
            was_closed = _close_with_retry(client, code)
            _emit_challenge_lifecycle(
                "challenge_close", code,
                reason="solved" if solved else "visit_end",
                closed=bool(was_closed), worker_id=_worker_id(),
                task_epoch=_current_task_epoch())
            container_active = not was_closed
        # [B54] 做题幻觉收口：软干预 + 本场留痕。
        # 只有**幻觉族**参与阈值 —— 推导族（影子审计 19/29 为真）绝不触发，
        # 否则等于把真答案的产出路径当故障掐掉。
        if _hallu is not None:
            try:
                _hs = _hallu.summary(workdir)
                if _hs:
                    log.info("  [B54] 做题 Agent 幻觉账（%s）：%s", code, _hs)
                if _hallu.should_rotate(workdir):
                    stoploss.record_no_progress(code)   # 记一次无进展 → 触发轮换
                    log.warning("  [B54] 幻觉族超阈（≥%d）→ 记无进展，本题将轮换",
                                _hallu.rotate_at())
            except Exception:
                pass
        # 结算本次访问的活动时长（须在 solving_active=False 之前——边界守卫
        # 在 False 窗口重启进程，晚于此的代码不保证执行）
        try:
            stoploss.end_visit(code)
        except Exception:
            pass
        # [B41] 未入账的访问收尾：同样清掉本场留下的答案明文。
        # 漏洞成因：_purge_stale_solutions 只在「目标地址变化」时清理，
        # _purge_plaintext_artifacts 只在入账后清理 —— 两不管的格子正是
        # 「同地址重发 + 上场未入账」（实测：17:00 场被误杀未入账，17:41 重发时
        # 残留 FLAG/SOURCE 被本场 agent 读到，白烧 3 回合 + 一次闸门误拒）。
        if not solved:
            try:
                _purge_plaintext_artifacts(workdir, code)
            except Exception:
                log.warning("[compliance] visit-end clean failed for %s", code, exc_info=True)
        # 释放求解认领：完成后清空 current_code/solving_active，
        # 防止本 worker 空闲时留下陈旧认领阻塞另一 worker 接管（活锁根因）。
        _update_status(solving_active=False, current_code="", session_active=False,
                       session_started_at=0.0, last_activity=time.time())
        # 上下文隔离：清空本题的共享黑板缓存，防止内存残留跨题污染
        # （同一容器后续解题时不应看到上一题 blackboard / 状态）
        try:
            with _BOARDS_LOCK:
                _SHARED_BOARDS.pop(code, None)
        except Exception:
            pass

    # 关题后留 10 秒缓冲再开下一题，给平台入账/槽位回收时间。
    _POST_CLOSE_DELAY = float(os.environ.get("ADAPTER_POST_CLOSE_DELAY", "10") or "10")
    if _POST_CLOSE_DELAY > 0 and not stop_event.is_set():
        log.info("  关题后缓冲 %.0fs 再开下一题", _POST_CLOSE_DELAY)
        try:
            _wait_end = time.monotonic() + _POST_CLOSE_DELAY
            while time.monotonic() < _wait_end and not stop_event.is_set():
                time.sleep(min(1.0, max(0.0, _wait_end - time.monotonic())))
        except Exception:
            pass

    return {
        "solved": solved,
        "outcome": "solved" if solved else (
            "task_ended" if task_dead.is_set() else visit_outcome),
        "flags": accepted_flags,
        "turns": getattr(result, "turns", 0) if result is not None else 0,
        "api_error": _is_api_fault(result),
    }


def solve_one(
    client: RateLimitedClient,
    ch: Challenge,
    visit_seconds: int,
    round_idx: int,
    *,
    solver: SolverConfig,
    ctrl: ControllerConfig,
    verifier: Verifier,
    stoploss: StopLoss,
    stop_event: threading.Event,
    submitted: dict,
    submitted_lock: threading.Lock,
    dispatch_deadline: float | None = None,
) -> dict:
    """Run one visit while exclusively owning that challenge lifecycle.

    Status files are advisory and can race around a concurrent scheduler tick.
    The lease is held from before the first claim until all close/cleanup work
    has completed, so two solver containers cannot both start or close the
    same target even when their heartbeats are momentarily stale.
    """
    code = str(ch.unique_code)
    lease = _try_acquire_challenge_lease(code, workdir=ctrl.workdir)
    if lease is None:
        log.info("  %s lifecycle lease is held by another worker — yielding", code)
        return {"solved": False, "outcome": "active_elsewhere", "reason": "lease_busy"}
    try:
        return _solve_one_unlocked(
            client, ch, visit_seconds, round_idx,
            solver=solver, ctrl=ctrl, verifier=verifier, stoploss=stoploss,
            stop_event=stop_event, submitted=submitted,
            submitted_lock=submitted_lock, dispatch_deadline=dispatch_deadline,
        )
    finally:
        # _solve_one_unlocked has a few deliberate pre-start returns.  Clear
        # here rather than relying only on its inner finally so those paths
        # cannot leak challenge_id/attempt_id into a reused executor thread.
        try:
            obs.clear_local_context()
        except Exception:
            pass
        _release_challenge_lease(lease)


# ── 多轮调度 ──────────────────────────────────────────────

def schedule_rounds(
    challenges: list[Challenge],
    client: RateLimitedClient,
    *,
    all_challenges: list[Challenge] | None = None,
    solver: SolverConfig,
    ctrl: ControllerConfig,
    verifier: Verifier,
    stoploss: StopLoss,
    stop_event: threading.Event,
    dispatch_deadline: float | None = None,
) -> tuple[set, set, bool, bool]:
    """
    多轮调度主循环。

    每轮给每道未解题目一次访问，时间盒逐轮递增。
    Returns ``(solved, dropped, api_fault, lifecycle_defer)``.

    ``lifecycle_defer`` is true only when a worker-wide transient failure
    aborted queued, not-yet-started visits.  The dispatcher uses it to wait
    briefly before it rebuilds a new pending list, without charging those
    untouched challenges a retry attempt.
    """
    solved: set = set()
    dropped: set = set()
    # Candidate dedupe is hydrated lazily from each challenge's own opaque
    # confirmed-progress receipt when a visit starts.  Never scan the shared
    # event stream here: it can contain records for unrelated challenges.
    submitted: dict = {}
    submitted_lock = threading.Lock()
    t0 = time.monotonic()
    # ``auto_dispatch_loop`` owns the worker-wide time budget.  A nested
    # schedule used to reset its own clock here, allowing a near-expired
    # worker to open another full multi-hour visit.  Keep the shorter deadline
    # and cap every visit to the remaining wall of that shared budget.
    local_deadline = t0 + max(0, ctrl.total_seconds)
    round_deadline = min(local_deadline, dispatch_deadline) \
        if dispatch_deadline is not None else local_deadline
    rnd = 0
    # ── API 熔断（余额/认证故障防护）──────────────────
    # 连续 3 次会话 0 turns / API 错误（402/401）→ 暂停 300s，
    # 累计暂停 5 次 → 优雅退出。避免余额耗尽时疯狂开关靶场。
    api_fail_streak = 0
    api_pause_count = 0
    api_fault = False
    lifecycle_defer = False
    API_PAUSE_SECONDS = int(os.environ.get("ADAPTER_API_PAUSE_SECONDS", "300"))
    API_PAUSE_LIMIT = int(os.environ.get("ADAPTER_API_PAUSE_LIMIT", "5"))

    while not stop_event.is_set() and time.monotonic() <= round_deadline:
        # 同步平台通关状态：其他 worker 运行中解掉的题直接跳过，
        # 避免重复劳动（如已通关题仍被 visit 十几分钟）
        try:
            platform_rows = client.list_challenges()
            newly = {c.unique_code for c in platform_rows if getattr(c, "is_completed", False)}
            diff = newly - solved
            if diff:
                log.info("sync platform: %d newly completed: %s",
                         len(diff), ",".join(sorted(diff))[:120])
                solved |= diff
        except Exception as e:
            if _task_finished(str(e)):
                log.info("platform sync: 平台任务已结束 — 停止调度（await-task 接管）")
                _mark_task_epoch_terminal(ctrl.workdir)
                break
            log.warning("platform sync failed: %s", str(e)[:120])

        # 熔断检查：上一轮 API 连续失败 → 暂停（不调度、不开关靶场）
        if api_fail_streak >= 3:
            api_pause_count += 1
            log.warning("API 连续失败 %d 次（疑似余额/认证问题）— 暂停 %ds (第 %d/%d 次)",
                        api_fail_streak, API_PAUSE_SECONDS, api_pause_count, API_PAUSE_LIMIT)
            # [B59-b3] 可 grep 的显式标记 + 落盘告警文件。旧形态只有一行中文
            # warning，卷在几百行调度日志里，属于「不崩不报警」那一族
            # （同日模型名事故 144 场失败也是同样形态）。控制台读 work/.api_fault
            # 亮横幅；[B59-b3b] 在**这里**就写，不等下面熔断耗尽（差 25 分钟）。
            _mark_api_fault(ctrl.workdir, api_pause_count)
            if api_pause_count >= API_PAUSE_LIMIT:
                log.warning("API 暂停达到上限，标记 api_fault 退出本轮（请检查模型余额/API Key；由自动派发循环退避重试）")
                log.warning("[API_FAULT] 账号级故障：累计暂停 %d 次达上限 — 本轮退出，"
                            "余额/Key 恢复后自动继续", api_pause_count)
                api_fault = True
                break
            remaining = max(0.0, round_deadline - time.monotonic())
            if remaining <= 0:
                break
            stop_event.wait(min(float(API_PAUSE_SECONDS), remaining))
            api_fail_streak = 0
            continue

        _beat()
        # 派单题永不 dropped：用户明确指定要解的题，排队等待期间不被放弃
        prio_codes = {_priority_code_key(item)
                      for item in _load_priority(ctrl.workdir, _worker_id())}
        pending = [c for c in challenges
                   if c.unique_code not in solved
                   and (_priority_code_key(c.unique_code) in prio_codes
                        or c.unique_code not in dropped)]
        if not pending:
            break

        # 优先任务队列：每轮重读派单文件——
        # 1) 运行中新派单的题可能不在本 worker 列表里，先认领（claim）
        # 2) 认领回来的题再次排除已 solved/dropped（防止已通关的派单题被加回）
        # 3) 已在列表里的派单题排到最前
        if all_challenges:
            pending = _claim_priority(pending, all_challenges, ctrl.workdir, _worker_id())
        pending = [c for c in pending
                   if c.unique_code not in solved
                   and (_priority_code_key(c.unique_code) in prio_codes
                        or c.unique_code not in dropped)]
        pending = _apply_priority(pending, ctrl.workdir, _worker_id())
        if not pending:
            break
        round_codes = {c.unique_code for c in pending}   # 本轮实际参与集合（含派单认领）

        # 当前轮的时间盒（按难度分级，越靠后轮次乘数越大）
        factors = ctrl.round_factors
        factor = factors[min(rnd, len(factors) - 1)]
        base_e, base_m, base_h = ctrl.timebox_easy, ctrl.timebox_medium, ctrl.timebox_hard

        log.info("=== ROUND %d — %d challenges (timebox easy=%ds medium=%ds hard=%ds x%.1f, %.0f/%ds budget) ===",
                 rnd + 1, len(pending),
                 int(base_e * factor), int(base_m * factor), int(base_h * factor),
                 factor, time.monotonic() - t0, ctrl.total_seconds)

        # ``run_fleet`` submits its whole input queue at once.  With one worker
        # those futures still begin serially, so a per-round abort gate prevents
        # a known account/infrastructure failure on the first target from
        # opening and immediately closing every later target in the queue.
        round_abort = threading.Event()
        round_abort_reason = [""]
        round_abort_lock = threading.Lock()

        def _abort_remaining(reason: str) -> None:
            with round_abort_lock:
                if not round_abort.is_set():
                    round_abort_reason[0] = reason
                    round_abort.set()

        def _visit(ch, attempt, variant, _r=rnd):
            if round_abort.is_set():
                return {"solved": False, "outcome": "aborted",
                        "reason": "round_abort:" + (round_abort_reason[0] or "failure"),
                        "turns": 0}
            remaining_global = int(round_deadline - time.monotonic())
            if remaining_global < 60:
                # Not a solve failure: the outer dispatcher will resume the
                # challenge on a later task budget.  Returning dropped here
                # would incorrectly consume its three-attempt retry allowance.
                return {"solved": False, "outcome": "aborted",
                        "reason": "dispatch_budget_exhausted", "turns": 0}
            base = ctrl.timebox_for_difficulty(ch.difficulty) * factors[min(_r, len(factors) - 1)]
            # 多flag题单轮 visit 时间盒放大：一轮内让 agent 在同实例上挖得更久，
            # 减少"挖不完就关容器换实例 → flag 值重发现"的轮换损耗（框架层，不动 agent）。
            # 默认 2.0，可用 ADAPTER_MULTIFLAG_VISIT_MULT 覆盖（1.0=关闭）。
            visit_mult = 1.0
            if int(getattr(ch, "flag_count", 1) or 1) > 1:
                try:
                    visit_mult = float(os.environ.get("ADAPTER_MULTIFLAG_VISIT_MULT", "2.0") or "2.0")
                except ValueError:
                    visit_mult = 2.0
            vs = int(base * max(1.0, visit_mult))
            # ── 首轮全覆盖（A 修复）─────────────────────────
            # 单题垄断会让整轮零覆盖（实测：worker 被单题独占 127 分钟，
            # 7 题里 4 题全程未触达）。首轮给每题一次保底触达：访问时长收敛到
            # 「首轮扫描预算 ADAPTER_FIRSTPASS_SWEEP（默认 3600s）/ 待访题数」，
            # 下限 ADAPTER_FIRSTPASS_FLOOR（默认 480s）；第 2 轮起恢复完整时间盒。
            # 效率基准：某题首访 2 分 16 秒即解出——8 分钟首轮足够出成果。
            if _r == 0:
                try:
                    _sweep = float(os.environ.get("ADAPTER_FIRSTPASS_SWEEP", "3600") or "3600")
                    _floor = float(os.environ.get("ADAPTER_FIRSTPASS_FLOOR", "480") or "480")
                except ValueError:
                    _sweep, _floor = 3600.0, 480.0
                if _sweep > 0 and len(pending) > 1:
                    _capped = int(max(_floor, min(vs, _sweep / len(pending))))
                    # [B63] 困难题与多段渗透题**不参与首轮压缩**。
                    # 首轮扫描本意是"先便宜地摸一遍"，但它的下限/除式会把每道题的首访压成同一个
                    # 短时长；而首访往往是**唯一**一次访问（关容器换实例 → flag 值重发现，见上面
                    # visit_mult 的注释）。于是困难题永远拿不到它需要的 90 分钟，"困难 90 分钟"
                    # 就成了空话 —— 配置在、行为不在。
                    # 多段渗透同理：它要的是连续时间，被切断就前功尽弃。
                    # 回滚：ADAPTER_FIRSTPASS_RESPECT_HARD=0
                    _respect = os.environ.get("ADAPTER_FIRSTPASS_RESPECT_HARD", "1") != "0"
                    _nflag = int(getattr(ch, "flag_count", 1) or 1)
                    if _respect and ((ch.difficulty or "").lower() == "hard" or _nflag > 1):
                        log.info("first-pass sweep: %s 首轮不压缩（%s%s），保留 %ds",
                                 ch.unique_code, ch.difficulty or "-",
                                 "/multi-flag" if _nflag > 1 else "", vs)
                        _capped = vs
                    if _capped < vs:
                        log.info("first-pass sweep: %s 首轮访问 %ds→%ds "
                                 "(sweep %.0fs / %d 题)",
                                 ch.unique_code, vs, _capped, _sweep, len(pending))
                        vs = _capped
            vs = min(vs, remaining_global)
            visit_result = solve_one(
                client, ch, vs, _r,
                solver=solver, ctrl=ctrl, verifier=verifier,
                stoploss=stoploss, stop_event=stop_event,
                submitted=submitted, submitted_lock=submitted_lock,
                dispatch_deadline=round_deadline,
            )
            if ((visit_result or {}).get("api_error")
                    or (visit_result or {}).get("outcome") in {
                        "api_fault", "infra_blocked", "no_effective_session",
                        "start_failed", "retry"}):
                _abort_remaining(str((visit_result or {}).get("outcome") or "api_fault"))
            return visit_result

        effective_best_of = 1
        if ctrl.best_of != 1:
            # A visit already contains its own session/strategy loop.  Fleet
            # ``best_of`` repeats the entire target lifecycle immediately and
            # is therefore a duplicate start/close mechanism, not useful
            # independent exploration.
            log.warning("ADAPTER_BEST_OF=%d ignored: one lifecycle visit per challenge is enforced",
                        ctrl.best_of)
        results = run_fleet(
            pending, _visit,
            is_success=lambda r: bool(r and r.get("solved")),
            # worker 模式（多容器扩展）下每容器只跑一个 Pi Agent
            max_concurrent=min(ctrl.max_concurrency, _worker_concurrency()),
            best_of=effective_best_of,
        )

        retryable_outcomes = {
            "retry", "start_failed", "not_found", "infra_blocked",
            "no_effective_session", "target_fault",
        }
        round_api_fault = False
        for c in pending:
            r = (results.get(c.unique_code) or {}).get("result") or {}
            is_api_fault = bool(r.get("api_error") or r.get("outcome") == "api_fault")
            if r.get("solved"):
                solved.add(c.unique_code)
            elif is_api_fault:
                # An expired balance/key is worker-wide, not a failure of this
                # particular challenge.  Do not burn its retry allowance.
                round_api_fault = True
            elif r.get("outcome") in {"dropped", *retryable_outcomes}:
                # Failed starts/setup must leave the round through the same
                # backoff path as a normal drop.  Otherwise the outer loop
                # immediately re-enters the same start API until it thrashes.
                dropped.add(c.unique_code)
            elif stoploss.should_stop(c.unique_code)[0]:
                dropped.add(c.unique_code)
            # API 故障计数（仅显式标记才计）。
            # BUG-K：不能用 turns==0 判断 —— stoploss-dropped / mutex-bail / start_failed
            # 的题 turns 也是 0；分片内旧题全被止损时会把"无 API 错误"误判为 4 次 API 失败
            # 并触发熔断（实测重启后 ROUND1 全 dropped → API 暂停 300s 假警报）。
            # api_error 只在会话结果 error 含 402/401/Insufficient/Authentication/Balance 时置位。
            if is_api_fault:
                api_fail_streak += 1
            elif r.get("turns", 0) > 0:
                api_fail_streak = 0
                # [B59-b3] 有会话真正跑起来了 = 账号级故障已解除 → 撤掉控制台告警。
                # 不撤就成了「狼来了」，和根本没有告警一样坏。
                _clear_api_fault(ctrl.workdir)

        # 任务终态感知：任一访问带回 task_ended → 终止本轮调度
        # （平台侧已终局，继续开关靶场/起会话全是无效功）
        if any(((results.get(c.unique_code) or {}).get("result") or {})
               .get("outcome") == "task_ended" for c in pending):
            log.info("=== 平台任务已结束 — 终止本轮调度（await-task 接管） ===")
            _mark_task_epoch_terminal(ctrl.workdir)
            break

        if round_api_fault:
            # The outcome is explicit and the round gate has already kept
            # queued targets from starting.  Back off immediately instead of
            # requiring three separate rounds (which still causes three
            # needless target lifecycles on an unmistakable 401/402).
            api_fault = True
            _mark_api_fault(ctrl.workdir, max(1, api_fail_streak))
            log.warning("round hit account/API fault — aborting queued visits and yielding to API backoff")
            break

        if (round_abort.is_set()
                and round_abort_reason[0] in {
                    "infra_blocked", "no_effective_session", "start_failed", "retry"}):
            # This round deliberately left queued targets untouched.  Mark a
            # short dispatcher-level pause rather than adding them to
            # ``dropped``; their normal retry budget belongs only to a visit
            # that actually started.
            lifecycle_defer = True

        # A resource/start/setup failure has already been classified into the
        # returned ``dropped`` set.  Return to auto_dispatch now so it can
        # persist one cooldown, rather than letting a priority entry create a
        # new lifecycle on every internal round.
        if any(((results.get(c.unique_code) or {}).get("result") or {})
               .get("outcome") in retryable_outcomes for c in pending):
            log.info("round contains retryable lifecycle failure — yielding to dispatch backoff")
            break

        # B20：整轮只有"互斥让行"时退避 —— 对手持题期间空闲 worker 以每轮一次
        # list_challenges 的节奏空转（实测 14 分钟 2000 轮），白烧平台 API 配额
        # 与日志；退避后对手访问结束仍能及时接管。
        if pending and all(
                ((results.get(c.unique_code) or {}).get("result") or {})
                .get("outcome") == "active_elsewhere" for c in pending):
            try:
                _mb = float(os.environ.get("ADAPTER_MUTEX_BACKOFF", "15") or "15")
            except ValueError:
                _mb = 15.0
            if _mb > 0:
                log.info("本轮 %d 题全部因互斥让行 — 退避 %.0fs 后重试", len(pending), _mb)
                stop_event.wait(_mb)

        log.info("=== ROUND %d done — solved=%d dropped=%d remaining=%d ===",
                 rnd + 1, len(solved), len(dropped),
                 len(round_codes - solved - dropped))
        rnd += 1

    return solved, dropped, api_fault, lifecycle_defer


# ── 主入口 ──────────────────────────────────────────────


# ── 全自动派发 ────────────────────────────────────────────

def _retry_state_path(workdir: str, wid: int) -> str:
    return os.path.join(workdir, f".dispatch_retry.wid{wid}.json")


def _release_abandoned(workdir: str, wid: int, retry_at: dict,
                       attempts: dict) -> tuple[dict, dict]:
    """一次性释放「被停机误记而永久放弃」的题（B46，见 .release_abandoned.widN 标记）。

    历史包袱：旧版把热重载/停机瞬间的秒退记成「派发过一次未解出」，满 3 次即
    retry_at=inf 永久出局 —— 实测 17 道题中招，多数从未真正跑过。本函数把这类
    inf 条目整批放回派题池并复位其 attempts，让它们重新有机会被访问（宁可多花
    一轮，也不要把没跑过的题永久搁置）。

    只生效一次：调用后标记文件改名为 .release_abandoned.widN.done。要再放一批，
    重新 touch work/.release_abandoned.widN 即可（下次加载状态时生效）。

    标记**按 worker 分文件**（B46b）：work/ 是 w2/w3 共享的卷，共用一个标记名时
    先重启的那个会把它改成 .done，后重启的直接扑空 —— 实测 w3 消费完 w2 就没了。
    """
    marker = os.path.join(workdir, ".release_abandoned.wid%d" % wid)
    if not os.path.isfile(marker):
        return retry_at, attempts
    released = sorted(k for k, v in retry_at.items() if v == float("inf"))
    for k in released:
        retry_at.pop(k, None)
        attempts.pop(k, None)
    if released:
        log.info("[B46] 一次性释放 %d 道被停机误记而永久放弃的题（已复位重试计数）：%s",
                 len(released), ",".join(released)[:160])
    try:
        os.replace(marker, marker + ".done")
    except OSError:
        pass
    return retry_at, attempts


def _load_retry_state(workdir: str, wid: int, *, task_epoch: str | None = None) -> tuple[dict, dict]:
    """Read retry backoff state for exactly one task epoch.

    Challenge codes are not globally unique across benchmark runs.  Keeping a
    retry cooldown or an ``inf`` abandonment from a prior task would either
    suppress a new challenge or make it appear that the dispatcher is stuck.
    Legacy files without an epoch are therefore intentionally not reused.
    """
    epoch = str(task_epoch if task_epoch is not None else _current_task_epoch() or "")
    try:
        with open(_retry_state_path(workdir, wid), encoding="utf-8") as f:
            d = json.load(f)
        stored_epoch = str(d.get("task_epoch", "") or "")
        if epoch and stored_epoch != epoch:
            log.info("auto-dispatch: discard retry state from epoch %s (current %s)",
                     stored_epoch[:12] or "legacy", epoch[:12])
            retry_at, attempts = {}, {}
        else:
            retry_at = {k: (float("inf") if v == -1 else float(v))
                        for k, v in (d.get("retry_at") or {}).items()}
            attempts = {k: int(v) for k, v in (d.get("attempts") or {}).items()}
    except Exception:
        retry_at, attempts = {}, {}
    # [B46] 释放开关放在读取之后、返回之前：即使状态文件缺失也要消费掉标记，
    # 免得它悄悄留到下一次重启才生效。
    return _release_abandoned(workdir, wid, retry_at, attempts)


def _save_retry_state(workdir: str, wid: int, retry_at: dict, attempts: dict,
                      *, task_epoch: str | None = None) -> None:
    try:
        p = _retry_state_path(workdir, wid)
        epoch = str(task_epoch if task_epoch is not None else _current_task_epoch() or "")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "task_epoch": epoch,
                       "retry_at": {k: (-1 if v == float("inf") else float(v))
                                    for k, v in retry_at.items()},
                       "attempts": attempts}, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def _zero_progress_retry_deadline(stoploss: StopLoss, code: str, *, now: float,
                                  fallback_seconds: float) -> tuple[float, str] | None:
    """Return the next eligible wall time for a terminal dry-session stop.

    ``StopLoss`` already owns the three-session threshold.  Treating its
    terminal zero-progress state as another failed *lifecycle* wastes the
    dispatch retry budget and prevents the configured periodic revive from ever
    becoming reachable.  Keep it out of the queue until the same generic revive
    cooldown that ``solve_one`` enforces at its entry.

    Only single-flag dry/zero-flag stops use this path.  A multi-flag
    post-hint exhaustion has its own completion policy and must not be silently
    reclassified here.
    """
    try:
        stopped, reason = stoploss.should_stop(code)
    except Exception:
        return None
    reason = str(reason or "")
    if not stopped or not reason.startswith((
            "stuck:dry_sessions=", "zero_flag_sessions:")):
        return None

    try:
        cooldown = max(0.0, float(
            os.environ.get("ADAPTER_REVIVE_COOLDOWN", "3600") or "3600"))
    except (TypeError, ValueError):
        cooldown = 3600.0
    try:
        retry_delay = max(0.0, float(fallback_seconds))
    except (TypeError, ValueError):
        retry_delay = 0.0

    retry_at = now + retry_delay
    try:
        revive_base = float(stoploss.last_revive_wall(code) or 0.0)
    except Exception:
        revive_base = 0.0
    if revive_base and cooldown > 0:
        retry_at = max(retry_at, revive_base + cooldown)
    return retry_at, reason


def auto_dispatch_loop(
    client: RateLimitedClient,
    *,
    seed: list,
    ctrl: ControllerConfig,
    solver: SolverConfig,
    verifier: Verifier,
    stoploss: StopLoss,
    stop_event: threading.Event,
    only: str,
    caps: set,
    obs=None,
) -> None:
    """
    全自动派发主循环（常驻）。

    批量做完后不进入"永眠"，而是周期重新拉取平台题目：
      - 新增 / 未解且能力匹配的题 → 自动派发解题（无需手动重启）
      - 因临时资源问题被放弃的题 → 冷却后自动重试（全自动派发的核心）
      - 无新题 → 常驻心跳等待（保持 VPN / 心跳，不退出）

    仅以下情况退出调度（继续常驻等待，不销毁）：
      - 平台任务已结束（invalid_state / already finished / 409）
      - 到达全局解题预算（ctrl.total_seconds）
      - 收到停止信号（SIGTERM / KeyboardInterrupt）
    """
    workdir = ctrl.workdir
    wid = _worker_id()
    poll_interval = int(os.environ.get("ADAPTER_AUTO_POLL", "60"))
    drop_retry = float(os.environ.get("ADAPTER_DROP_RETRY", "600"))
    max_attempts = int(os.environ.get("ADAPTER_AUTO_MAX_RETRY", "3"))
    api_backoff = float(os.environ.get("ADAPTER_API_BACKOFF", "600"))
    try:
        lifecycle_backoff = max(0.0, float(
            os.environ.get("ADAPTER_LIFECYCLE_BACKOFF", "60") or "60"))
    except ValueError:
        lifecycle_backoff = 60.0

    solved_by_me: set = set()
    # Retry state belongs to this task generation only.  A reused challenge
    # code in the next benchmark must never inherit a cooldown or permanent
    # abandonment from the previous one.
    task_epoch = _current_task_epoch()
    _retry_at, _attempts = _load_retry_state(workdir, wid, task_epoch=task_epoch)
    deadline = time.monotonic() + ctrl.total_seconds

    def collect(now: float, fresh: list) -> list:
        """从平台全量里挑出本 worker 可派发的题。"""
        _purge_stale_priority({c.unique_code for c in fresh
                               if not getattr(c, "is_completed", False)})
        _plat_done = {c.unique_code for c in fresh if getattr(c, "is_completed", False)}
        solved_by_me.update(_plat_done)   # 用 update（增强赋值会触发 UnboundLocalError）
        prio_codes = {_priority_code_key(code)
                      for code in _load_priority(workdir, wid)}
        base_pending = [c for c in fresh
                        if not getattr(c, "is_completed", False)
                        and c.unique_code not in solved_by_me]
        # Priority controls ordering and capability ownership, not lifecycle
        # safety.  A failed start still observes the same cooldown as every
        # other task; otherwise a queued task with an unavailable target can
        # defeat the backoff loop and hammer start/close indefinitely.
        eligible = [c for c in base_pending
                    if c.unique_code not in _retry_at
                    or time.time() >= _retry_at[c.unique_code]]
        forced = [c for c in eligible if _priority_code_key(c.unique_code) in prio_codes]
        pend = [c for c in eligible if _priority_code_key(c.unique_code) not in prio_codes]
        if caps:
            pend = _capability_filter(pend)
        # 多解题目 worker：对「已知分类」题按 worker 序号分片，避免每 worker 全量接管
        # 同一批题（重叠 → 重复劳动 + 互相杀对方活跃靶场）。派单题（forced）不受影响。
        # A worker-id shard is only safe for the shipped homogeneous pool.  If
        # capabilities are asymmetric, _capability_filter has already assigned
        # each category to its owner and a second shard would drop work that no
        # other worker can see.
        if (caps and _capability_sharding_enabled()
                and int(os.environ.get("ADAPTER_WORKER_COUNT", "1")) > 2
                and _worker_id() >= 1):
            sharded = _solver_shard(pend)
            # 空切片是小题集上的合法分配，不是分片失败。回退到全量会让本该
            # 等待的 worker 抢占另一 worker 的题，形成 mutex 轮询与重复解题。
            pend = sharded
            log.info("worker shard (collect): %d known-category challenges to worker %d",
                     len(sharded), _worker_id())
        elif not caps:
            # A three-container deployment reserves wid0 for VPN/monitoring.
            # Do not hash one third of a no-capabilities fallback into that
            # non-solving worker; use the same solver-only partition as the
            # configured homogeneous pool.  Two-worker legacy deployments
            # retain their original all-worker sharding behavior.
            if int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1") > 2:
                pend = _solver_shard(pend)
            else:
                pend = _worker_shard(pend)
        if only:
            pend = [c for c in pend if c.unique_code == only]
            forced = [c for c in forced if c.unique_code == only]
        # 派发互斥：其他解法 worker 正在解的题不进本次调度（与 solve_one 守卫互补，
        # 避免反复进入 schedule_rounds 造成目标容器启停抖动）。派单题(forced)同样跳过。
        pend = [c for c in pend if not _other_solver_active_on(c.unique_code)]
        forced = [c for c in forced if not _other_solver_active_on(c.unique_code)]
        return forced + pend

    def _finalize(reason: str):
        _finish_and_idle(obs, reason)

    # The bootstrap seed is only a snapshot, not an exemption from the normal
    # completed/cooldown/mutex filters.  Bypassing ``collect`` here made every
    # restart immediately re-open a backoff-limited challenge.
    last_list = list(seed)
    pend = _apply_priority(collect(time.monotonic(), last_list), workdir, wid)
    first_pass = bool(pend)
    while not stop_event.is_set():
        now = time.monotonic()

        # 全局解题预算：到达即停止自动派发，但不永久停死 ——
        # 周期复查平台，若出现新任务/新题则重置状态自动续跑。
        if now >= deadline:
            log.info("=== 全局解题预算 %.0fs 已耗尽 — 复查等待新任务 ===", ctrl.total_seconds)
            fresh = _await_task(client, poll=poll_interval, stop_event=stop_event,
                                beat_cb=_beat,
                                known_fingerprint=_challenge_fingerprint(last_list))
            if fresh is None:
                return
            next_epoch = _activate_task_epoch(fresh, force_new=True, workdir=workdir)
            if next_epoch == task_epoch:
                # Progress in the current task can wake await-task.  The
                # global budget remains exhausted in that case; keep waiting
                # instead of treating one completion as a fresh run.
                log.info("await-task observed current-task progress; global budget remains exhausted")
                last_list = fresh
                continue
            log.info("=== 新任务出现，重置派发状态并续跑 ===")
            task_epoch = next_epoch
            solved_by_me = set()
            _retry_at = {}
            _attempts = {}
            _save_retry_state(workdir, wid, _retry_at, _attempts, task_epoch=task_epoch)
            deadline = time.monotonic() + ctrl.total_seconds
            last_list = fresh
            pend = collect(now, fresh)
            pend = _apply_priority(pend, workdir, wid)
            first_pass = False
            continue

        if not first_pass:
            try:
                fresh = client.list_challenges()
            except Exception as e:
                text = str(e)
                if _task_finished(text):
                    # 终态也可能是瞬时误判：不永久停死，周期复查。
                    # 任务真结束后平台出新任务，这里会自动接上。
                    log.info("task finished 提示 — 进入复查等待（不永久停死）")
                    _mark_task_epoch_terminal(workdir)
                    fresh = _await_task(client, poll=poll_interval,
                                        stop_event=stop_event, beat_cb=_beat,
                                        # The next benchmark can legitimately
                                        # reuse an identical public challenge
                                        # set.  After an explicit terminal
                                        # signal, any non-empty list is a new
                                        # task; comparing mutable rows here
                                        # would wait forever on such a reset.
                                        known_fingerprint=None)
                    if fresh is None:
                        return
                    next_epoch = _activate_task_epoch(fresh, force_new=True, workdir=workdir)
                    if next_epoch != task_epoch:
                        log.info("=== 新任务出现，重置派发状态并续跑 ===")
                        task_epoch = next_epoch
                        solved_by_me = set()
                        _retry_at = {}
                        _attempts = {}
                        _save_retry_state(workdir, wid, _retry_at, _attempts,
                                          task_epoch=task_epoch)
                        deadline = time.monotonic() + ctrl.total_seconds
                    else:
                        log.info("=== 平台恢复为当前任务，保留预算与重试状态 ===")
                    last_list = fresh
                    pend = collect(now, fresh)
                    pend = _apply_priority(pend, workdir, wid)
                    first_pass = False
                    continue
                log.warning("auto-dispatch: list_challenges failed (%s) — %ds 后重试",
                            str(e)[:100], poll_interval)
                _idle_tick(poll_interval, reason="platform list failed, retrying")
                continue
            fresh = list(fresh or [])
            last_list = fresh
            # Detect a task-set replacement even when the API never returned an
            # explicit terminal error (some backends atomically swap runs).
            # The epoch helper rotates only metadata/progress namespaces; it
            # does not read or retain answer material.
            if fresh:
                next_epoch = _activate_task_epoch(fresh, workdir=workdir)
                if next_epoch != task_epoch:
                    log.info("auto-dispatch: detected a new task epoch; clearing old retry state")
                    task_epoch = next_epoch
                    solved_by_me = set()
                    _retry_at = {}
                    _attempts = {}
                    _save_retry_state(workdir, wid, _retry_at, _attempts,
                                      task_epoch=task_epoch)
            pend = collect(now, fresh)
            pend = _apply_priority(pend, workdir, wid)

        if not pend:
            log.info("auto-dispatch: 暂无待解新题 (solved=%d) — 常驻心跳 %ds",
                     len(solved_by_me), poll_interval)
            _idle_tick(poll_interval, reason="waiting for new challenges")
            continue

        log.info("=== auto-dispatch: %d 道题进入调度 %s ===",
                 len(pend), ",".join(sorted(c.unique_code for c in pend))[:160])
        try:
            batch_solved, batch_dropped, api_fault, lifecycle_defer = schedule_rounds(
                pend, client,
                all_challenges=last_list,
                solver=solver, ctrl=ctrl,
                verifier=verifier, stoploss=stoploss,
                stop_event=stop_event, dispatch_deadline=deadline,
            )
        except (KeyboardInterrupt, SystemExit):
            log.info("interrupted by user — stopping auto-dispatch")
            stop_event.set()
            return
        except Exception:
            log.exception("auto-dispatch: schedule_rounds error")
            _idle_tick(poll_interval, reason="schedule error, retrying")
            continue

        solved_by_me |= batch_solved
        _retry_at = {k: v for k, v in _retry_at.items() if k not in batch_solved}
        _attempts = {k: v for k, v in _attempts.items() if k not in batch_solved}
        drop_now = time.time()
        for code in batch_dropped:
            dry_backoff = _zero_progress_retry_deadline(
                stoploss, code, now=drop_now, fallback_seconds=drop_retry)
            if dry_backoff is not None:
                retry_at, reason = dry_backoff
                # The three useful sessions have already happened.  Do not
                # charge this as another start failure or make it permanent
                # before the stoploss revive window can run.
                _attempts.pop(code, None)
                _retry_at[code] = retry_at
                log.info("auto-dispatch: %s %s — %.0fs 后按 stoploss 冷却复查",
                         code, reason, max(0.0, retry_at - drop_now))
                continue
            _attempts[code] = _attempts.get(code, 0) + 1
            if _attempts[code] >= max_attempts:
                _retry_at[code] = float("inf")   # 放弃自动重试（避免无限消耗）
                log.info("auto-dispatch: %s 已自动派发 %d 次未解出，停止重试", code, _attempts[code])
            else:
                _retry_at[code] = drop_now + drop_retry
                log.info("auto-dispatch: %s 本轮未解出 — %ds 后自动重试 (第 %d/%d 次)",
                         code, int(drop_retry), _attempts[code], max_attempts)
        _save_retry_state(workdir, wid, _retry_at, _attempts, task_epoch=task_epoch)

        # API 熔断（余额/认证故障）：长退避后再轮询，避免空转热循环
        if api_fault:
            log.warning("auto-dispatch: API 暂停达上限 — 退避 %ds 后继续轮询（保持 VPN/心跳）",
                        int(api_backoff))
            _idle_tick(int(api_backoff), reason="api backoff after circuit breaker")
            continue

        if lifecycle_defer:
            # schedule_rounds stopped before opening the rest of this batch.
            # Keep their retry counters untouched, but do not immediately
            # rebuild the pending list and turn one transient failure into a
            # rapid start/close sweep across the remaining catalogue.
            if lifecycle_backoff > 0:
                log.info("auto-dispatch: transient lifecycle failure — wait %.0fs before retrying untouched queue",
                         lifecycle_backoff)
                _idle_tick(int(lifecycle_backoff),
                           reason="transient lifecycle failure backoff")
            first_pass = False
            continue

        first_pass = False
        # 循环顶部 → 重新拉取平台题目，接新题
def main():
    base_url = os.getenv("BENCHMARK_BASE_URL", "")
    token = os.getenv("BENCHMARK_TOKEN", "")
    if not base_url or not token:
        log.error("BENCHMARK_BASE_URL and BENCHMARK_TOKEN must be set.\n"
                  "  For local eval: set them from the platform page and connect VPN first.\n"
                  "  For hosted mode: these are injected by the platform.")
        sys.exit(2)

    # 单题模式（网页「单独自动解」触发的定向 Agent）：
    # ADAPTER_CHALLENGE_ONLY 指定只处理一道题，且不干预常驻舰队容器
    only = os.getenv("ADAPTER_CHALLENGE_ONLY", "").strip()
    single_mode = bool(only)

    # 加载配置
    solver = SolverConfig.from_env()
    ctrl = ControllerConfig.from_env()

    os.makedirs(ctrl.workdir, exist_ok=True)
    # 设置 worker_id（修复 reporting bug：此前 _STATUS 硬编码 worker_id=0，
    # 状态文件里所有 worker 都显示 id=0，监控面板无法区分各 worker）
    _STATUS["worker_id"] = _worker_id()
    _beat()  # 启动即写心跳，避免 healthcheck 误判

    # 独立心跳线程：只要 driver 进程存活就持续写心跳，
    # 健康检查语义 = "进程存活"（会话卡死由会话级看门狗重建，不会影响心跳）
    def _heartbeat_loop():
        while True:
            time.sleep(30)
            try:
                _beat()
            except Exception:
                pass

    threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat").start()
    obs.configure(os.path.join(ctrl.workdir, "_events.jsonl"),
                  run_id=f"adapter-{solver.provider}")
    # 事件流由三个 worker 共享；把 worker/进程启动戳写进上下文，便于区分
    # 同一 run_id 下的并行事件和重启前遗留事件，不把旧会话误拼进当前运行。
    obs.context(worker_id=_worker_id(), boot_id=_BOOT_STAMP,
                role=os.environ.get("ADAPTER_ROLE", "solver") or "solver")
    obs.emit("run_start", layer="driver",
             payload={"provider": solver.provider, "model": solver.model,
                      "max_concurrency": ctrl.max_concurrency})

    # 被杀会话的悬空 session_start 补合成收尾（见 _close_orphan_sessions）
    _close_orphan_sessions(os.path.join(ctrl.workdir, "_events.jsonl"))

    log.info("tsecbench-adapter starting: provider=%s model=%s base=%s",
             solver.provider, solver.model, solver.base_url)

    # 初始化验证器
    verifier_cfg = build_verifier_config(solver)
    llm = None
    if verifier_cfg.is_usable():
        try:
            from ghost_worker.adapter.llm import LLMClient
            llm = LLMClient(verifier_cfg)
        except Exception as e:
            log.warning("verifier LLM unavailable (%s); degrading to grounding-only", e)
    verifier = Verifier(llm, skeptic_votes=ctrl.skeptic_votes)
    # [B47] 判断 Agent 总开关。ADAPTER_SKEPTIC=0 → 摘掉 LLM，skeptic() 变空操作，
    # 整条链路回到纯确定性规则（无需回滚代码即可停用）。
    if os.environ.get("ADAPTER_SKEPTIC", "1") != "1":
        verifier.llm = None
        log.info("[B47] 判断 Agent 已停用（ADAPTER_SKEPTIC != 1）")
    else:
        try:
            verifier.rescue_conf = float(
                os.environ.get("ADAPTER_SKEPTIC_RESCUE_CONF", "0.75") or "0.75")
            verifier.veto_conf = float(
                os.environ.get("ADAPTER_SKEPTIC_VETO_CONF", "0.85") or "0.85")
            verifier.timeout = float(
                os.environ.get("ADAPTER_SKEPTIC_TIMEOUT", "30") or "30")
        except Exception:
            pass
        log.info("[B47] 判断 Agent 已启用: llm=%s votes=%d rescue>=%.2f veto>=%.2f timeout=%.0fs",
                 "yes" if verifier.llm else "no", verifier.skeptic_votes,
                 verifier.rescue_conf, verifier.veto_conf, verifier.timeout)

    # [B61] 观察者 Agent：复用判断 Agent 的 llm 通道。默认关，且拿不到 llm 时
    # 自动退化成「没有观察者」——观察者缺席绝不影响解题（红线 4）。
    _heimdall_init(llm)

    # 止损器
    stoploss = StopLoss(
        per_challenge_seconds=ctrl.per_challenge_seconds,
        max_sessions=ctrl.max_sessions_per_challenge,
        dry_cutoff=ctrl.dry_facts_cutoff,
        post_hint_dry_cutoff=int(os.environ.get(
            "ADAPTER_MULTIFLAG_POST_HINT_DRY_SESSIONS",
            str(ctrl.dry_facts_cutoff),
        ) or ctrl.dry_facts_cutoff),
        zero_flag_cutoff=int(os.environ.get("ADAPTER_ZERO_FLAG_CUTOFF", "3") or "3"),
        workdir=ctrl.workdir,
    )

    # 初始化平台客户端
    raw_client = PlatformClient(base_url, token)
    client = RateLimitedClient(raw_client, ctrl.min_request_interval)

    # 健康检查
    if not raw_client.health_check():
        log.warning("platform health check failed — proceeding anyway")

    role = os.environ.get("ADAPTER_ROLE", "").strip()

    # ── worker-1：纯 VPN + 监控模式（不参与做题）────────
    # 职责：开启 VPN 共享网络给 worker-2/3，周期性汇总它们的状态。
    # 常驻不退出（VPN 提供者必须保持存活，否则 worker-2/3 网络断开）。
    if role == "monitor":
        log.info("=== worker-1 monitor mode: VPN + 监控，不参与做题 ===")
        # Publish a conservative state before the first probe so a stale true
        # marker from a previous monitor process cannot make the console green.
        _publish_vpn_readiness(False, reason="probe_pending", workdir=ctrl.workdir)
        try:
            _vpn_initial = raw_client.check_vpn(timeout=10)
            _publish_vpn_readiness(bool(_vpn_initial.ok),
                                   reason="ok" if _vpn_initial.ok else "not_ok",
                                   workdir=ctrl.workdir)
            if _vpn_initial.ok:
                log.info("VPN check passed: client_ip=%s (%s)",
                         _vpn_initial.client_ip, _vpn_initial.time)
            else:
                log.warning("VPN precheck did not pass in monitor mode")
        except VpnCheckError as _vpn_err:
            _publish_vpn_readiness(False, reason=getattr(_vpn_err, "reason", "error"),
                                   workdir=ctrl.workdir)
            log.warning("VPN precheck skipped in monitor mode: %s", _vpn_err)
        except Exception:
            _publish_vpn_readiness(False, reason="check_error", workdir=ctrl.workdir)
            log.warning("VPN precheck skipped in monitor mode")
        _start_vpn_watchdog(raw_client, interval=60, failures_before_alert=3,
                            publish_readiness=True, workdir=ctrl.workdir)
        _monitor_loop(watch_dir=ctrl.workdir, raw_client=raw_client)
        return

    # ── 协作式热重载 watcher（B15）──────────────────────
    # 必须在这里启动：平台任务已结束时 driver 在初始 list_challenges 就 409，
    # 直接进 _await_task 复查等待，根本走不到派发循环前的 watcher 启动点
    # （实测 2026-09-09 19:44：touch .reload.wid1 无人消费）。放在 monitor
    # 分支之后是刻意的——worker-1（wid0）是 VPN/netns 提供者，属禁区，
    # 绝不能让 .reload.wid0 生效。
    _reload_stop = threading.Event()
    threading.Thread(target=_reload_watch, args=(_reload_stop,),
                     daemon=True, name="reload-watch").start()

    # ── worker-2/3：按能力做题 ─────────────────────────
    # 先判断任务状态：平台对不受信 IP / VPN 未起时会返回 409 / 任务无效，
    # 这类失败并不代表任务真结束 —— 一律通过 _await_task 复查等待，
    # 等 VPN/平台恢复、拿到 200 列表后再进入自动派发；不永久停死。
    # 常驻策略：不退出，保持 VPN/心跳；新任务/新题出现即自动接上。
    try:
        challenges = client.list_challenges()
    except Exception as e:
        text = str(e)
        log.warning("initial list_challenges failed (%s) — 复查等待平台/VPN 恢复",
                    str(e)[:120])
        fresh = _await_task(client, poll=int(os.environ.get("ADAPTER_AUTO_POLL", "60")),
                            stop_event=_reload_stop, beat_cb=_beat)
        if fresh is None:
            return  # stop signal
        challenges = fresh

    # 任务 epoch 必须在建立题目工作区之前确定，防止热重载沿用上一轮的
    # stoploss/confirmed-progress。所有 worker 对同一题集计算同一个不含题面
    # 明文的 epoch；await-task 返回的新任务显式 force_new。
    _epoch = _activate_task_epoch(challenges, workdir=ctrl.workdir)

    # 拿到列表后若仍不可用（空结果），交给 auto_dispatch_loop 的 seed 机制：
    # 首轮为空 → 立即轮询平台。

    # VPN 联通预检（任务仍有效才执行，失败非致命，交给 watchdog）
    try:
        vpn = raw_client.check_vpn(timeout=10)
        if not vpn.ok:
            log.error("VPN check failed: status=%r — 请检查靶场VPN网络配置", vpn.status)
        else:
            log.info("VPN check passed: client_ip=%s (%s)", vpn.client_ip, vpn.time)
    except VpnCheckError as e:
        log.error("VPN检测未通过,请检查靶场VPN网络配置 (reason=%s) — 继续尝试运行，由看门狗处理",
                  getattr(e, "reason", "unknown"))
    except Exception as e:
        log.warning("VPN precheck skipped (backend %s has no vpn check): %s",
                    getattr(raw_client.backend, "name", "?"), e)

    # VPN 看门狗：VPN 预检失败只告警不退出（退出会孤立 worker-1/2/3 共享 netns）
    _start_vpn_watchdog(raw_client, interval=60, failures_before_alert=3)
    # 共享 netns 自愈看门狗：worker-1 意外重建导致本容器被孤立时退出重启
    _start_netns_watchdog(interval=45, failures_before_exit=3, required_iface="eth0")

    log.info("loaded %d challenges", len(challenges))

    # 同步平台通关状态：重启后不重访已通关的题（平台为准）
    try:
        platform_done = {c.unique_code for c in challenges if getattr(c, "is_completed", False)}
        if platform_done:
            challenges = [c for c in challenges if c.unique_code not in platform_done]
            log.info("skipping %d already-completed challenges", len(platform_done))
    except Exception:
        pass
    # Keep the complete pending platform snapshot for priority claims and the
    # first dispatcher pass.  The working ``challenges`` list below is later
    # narrowed by capability and shard, so it cannot safely serve as the
    # inventory for a user-assigned cross-capability task.
    all_challenges = list(challenges)

    # ── 能力划分过滤：worker-2(web/cloud/exploit) / worker-3(pwn/pentest/evasion) ──
    # 未配置 ADAPTER_CAPABILITIES 时不过滤（兼容旧行为）
    caps = _load_capabilities()
    if caps:
        matched = _capability_filter(challenges)
        log.info("capability filter [%s]: %d/%d challenges",
                 ",".join(sorted(caps)), len(matched), len(challenges))
        challenges = matched

    # 过滤和排序
    challenges = _prioritize(challenges)
    # 多解题目 worker 时按 worker 序号分片：两 worker 能力重叠、都持全部能力时，
    # 若不按序号分片，每 worker 会各接管全量能力匹配题（实测各 38 道），
    # 造成重复劳动、互相杀对方正在解的靶场容器、白烧 LLM。
    # 只分给解题目 worker（wid>=1）；monitor(wid0) 不解题不参与；
    # unknown 题在 _capability_filter 前由 _unknown_bucket 已均匀分流，不受影响。
    if (caps and _capability_sharding_enabled()
            and int(os.environ.get("ADAPTER_WORKER_COUNT", "1")) > 2
            and _worker_id() >= 1):
        sharded = _solver_shard(challenges)
        log.info("worker shard (known-category): %d/%d challenges to worker %d",
                 len(sharded), len(challenges), _worker_id())
        challenges = sharded

    if not challenges and not _load_priority(ctrl.workdir, _worker_id()):
        log.info("no pending challenges matching my capabilities — 常驻等待")
        _wait_and_dispatch(reason="no matching challenges, waiting",
                           client=client, ctrl=ctrl, solver=solver,
                           verifier=verifier, stoploss=stoploss,
                           stop_event=_reload_stop, only=only, caps=caps, obs=obs)
        return

    # 单题模式：只保留指定题
    if only:
        challenges = [c for c in challenges if c.unique_code == only]
        if not challenges:
            log.info("ADAPTER_CHALLENGE_ONLY=%s: challenge not found or already solved — 常驻等待", only)
            _wait_and_dispatch(reason="single challenge not found, waiting",
                               client=client, ctrl=ctrl, solver=solver,
                               verifier=verifier, stoploss=stoploss,
                               stop_event=_reload_stop, only=only, caps=caps, obs=obs)
            return
        log.info("single-challenge mode: only %s (score=%d)", only, challenges[0].total_score)

    # 派发划分：全自动派发模式
    # 默认（能力分工）：配置了能力即按「能力」划分，不再按 worker 序号取模分片。
    #   原因：worker-1 是 monitor 不解题、worker-2/3 能力池不同，取模分片会把
    #   各 worker 能力池的 2/3 分给其他 worker/monitor，造成大量题目无人处理。
    # all_challenges 保留完整待解平台列表（供本 worker 派单认领；跨能力派单由
    # auto_dispatch_loop 的首轮就能看见，无需先跑完本地分片）。
    if caps:
        log.info("capability-based dispatch: 归本 worker 的 %d 道能力匹配题全部接管（不做序号分片）",
                 len(challenges))
    else:
        # 无能力配置时，三 worker compose 仍有 wid0=monitor；只在两个
        # 解题目 worker 之间划分，不能把 1/3 题交给 monitor。两 worker
        # 旧部署则保留原始的全 worker 分片语义。
        if int(os.environ.get("ADAPTER_WORKER_COUNT", "1") or "1") > 2:
            challenges = _solver_shard(challenges)
        else:
            challenges = _worker_shard(challenges)
    if not challenges and not _load_priority(ctrl.workdir, _worker_id()):
        log.info("no challenges assigned to this worker — 常驻等待")
        _wait_and_dispatch(reason="no assigned challenges, waiting",
                           client=client, ctrl=ctrl, solver=solver,
                           verifier=verifier, stoploss=stoploss,
                           stop_event=_reload_stop, only=only, caps=caps, obs=obs)
        return

    # 优先任务队列（网页「Agent 解此题」派单；认领范围用全量列表，支持跨能力）
    challenges = _claim_priority(challenges, all_challenges, ctrl.workdir, _worker_id())
    challenges = _apply_priority(challenges, ctrl.workdir, _worker_id())
    if not challenges:
        log.info("no challenges assigned to this worker — 常驻等待")
        _wait_and_dispatch(reason="no assigned challenges after priority, waiting",
                           client=client, ctrl=ctrl, solver=solver,
                           verifier=verifier, stoploss=stoploss,
                           stop_event=_reload_stop, only=only, caps=caps, obs=obs)
        return

    # 清理自己分片内的悬挂容器（重启遗留占槽；available=空闲无人在用，安全回收）
    my_codes = {c.unique_code for c in challenges}
    for c in all_challenges:
        # （原 try/else 错位：成功关闭后反而打 skip 日志，误导排障）
        if c.container_status != "available" or c.unique_code not in my_codes:
            continue
        if _other_worker_active_on(c.unique_code):
            log.info("  skip leftover close %s — 另一 worker 正在解它", c.unique_code)
            continue
        lease = _try_acquire_challenge_lease(c.unique_code, workdir=ctrl.workdir)
        if lease is None:
            log.info("  skip leftover close %s — lifecycle lease is held", c.unique_code)
            continue
        try:
            if _other_worker_active_on(c.unique_code):
                log.info("  skip leftover close %s — fresh worker claim observed", c.unique_code)
                continue
            was_closed = _close_with_retry(client, c.unique_code)
            _emit_challenge_lifecycle(
                "challenge_close", c.unique_code, reason="startup_leftover",
                closed=bool(was_closed), worker_id=_worker_id(),
                task_epoch=_current_task_epoch())
            if was_closed:
                log.info("closed my leftover container %s", c.unique_code)
            else:
                log.warning("  failed to close leftover %s", c.unique_code)
        finally:
            _release_challenge_lease(lease)
    # 孤儿实例回收：本 worker 重启前死在别的题 visit 中段、且 shard 漂移后
    # 该题不归本 worker 的运行中实例，在这里统一回收（无人认领判据见
    # _heal_orphan_instances），防止 3 槽位被占满堵死全队。
    _heal_orphan_instances(client, exclude=None)

    log.info("pending: %d challenges (first: %s, last: %s)",
             len(challenges), challenges[0].unique_code, challenges[-1].unique_code)

    # 全自动派发：批量做完后不退出，周期拉新题继续派发
    stop_event = _reload_stop   # B15：复用 main 开头启动的 watcher 事件
    dispatch_seed = all_challenges
    auto_dispatch_loop(
        client,
        seed=dispatch_seed,
        ctrl=ctrl, solver=solver, verifier=verifier, stoploss=stoploss,
        stop_event=stop_event,
        only=only, caps=caps, obs=obs,
    )
    return


if __name__ == "__main__":
    main()
    # B15 修复：重载请求下 main() 可能先于 watcher 的 os._exit(86) 正常返回，
    # 进程以 0 退出 → restart:on-failure 不重启 → worker 停摆。以 86 退出兜底。
    sys.exit(86 if _RELOAD_REQUESTED.is_set() else 0)
