"""
队列 worker — cozy 计划 Phase 1 jobs API 的客户端与常驻循环

平台侧下发 job → 本模块 claim → ack_running → solve_challenge → complete/fail。
15s 心跳线程同时刷新平台 lease(长 pi 会话续租)与 /tmp/driver_heartbeat
(compose healthcheck 依据)。

契约(见 cozy 计划 Phase 1 jobs API):
  POST /api/v1/jobs/claim           body {worker_id};204 = 无任务/无槽位
  POST /api/v1/jobs/{id}/running    领取确认
  POST /api/v1/jobs/{id}/complete   body {solved, accepted, turns, transcript_path, error}
  POST /api/v1/jobs/{id}/fail       body {error}
  POST /api/v1/workers/heartbeat    body {worker_id, status, current_job_id}
认证: BENCHMARK_TOKEN header + 自声明 WORKER_ID(与服务端一致)。
"""

from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import SolverConfig, _envs
from .flags import normalize_flag_body
from .orchestration import SolveCallbacks, SolveOutcome, solve_challenge
from .platform import Challenge, create_platform
from .solver import SolverBackend, create_solver, touch_heartbeat

DEFAULT_FLAG_FORMAT = "flag{...}"   # 与 flags.py 提取正则对应(仅渲染进 prompt)

log = logging.getLogger("tsecbench_worker.queue")

HEARTBEAT_INTERVAL = 15.0   # 平台心跳/续租(秒)
CLAIM_POLL = 5.0            # 无任务时的认领轮询(秒)
IDLE_SLEEP = 30.0           # 认领失败时的退避(秒)


@dataclass
class JobInfo:
    job_id: str
    unique_code: str
    task_token: str = ""
    priority: int = 0


class JobQueueClient:
    """cozy Phase 1 jobs API 客户端(薄 HTTP 封装,异常上抛由调用方决定退避)"""

    def __init__(self, base_url: str, token: str, worker_id: str, *, timeout: float = 15.0):
        self._base = base_url.rstrip("/")
        self._worker_id = worker_id
        self._timeout = timeout
        self._session = httpx.Client(timeout=timeout)
        self._session.headers["BENCHMARK_TOKEN"] = token
        self._session.headers["Content-Type"] = "application/json"

    def _post(self, path: str, body: dict) -> httpx.Response:
        return self._session.post(f"{self._base}{path}", json=body)

    def claim(self) -> Optional[JobInfo]:
        """认领一个 queued job;204(无任务/无槽位)返回 None"""
        r = self._post("/api/v1/jobs/claim", {"worker_id": self._worker_id})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        d = r.json()
        return JobInfo(job_id=str(d.get("id", "")), unique_code=str(d.get("unique_code", "")),
                       task_token=str(d.get("task_token", "")), priority=int(d.get("priority", 0) or 0))

    def ack_running(self, job_id: str) -> None:
        r = self._post(f"/api/v1/jobs/{job_id}/running", {"worker_id": self._worker_id})
        r.raise_for_status()

    def complete(self, job_id: str, result_json: dict) -> None:
        r = self._post(f"/api/v1/jobs/{job_id}/complete", result_json)
        r.raise_for_status()

    def fail(self, job_id: str, error: str) -> None:
        r = self._post(f"/api/v1/jobs/{job_id}/fail", {"error": error[:500]})
        r.raise_for_status()

    def heartbeat(self, *, status: str = "idle", current_job_id: Optional[str] = None) -> None:
        r = self._post("/api/v1/workers/heartbeat",
                       {"worker_id": self._worker_id, "status": status,
                        "current_job_id": current_job_id or ""})
        r.raise_for_status()


def _resolve_challenge(platform, unique_code: str) -> Optional[Challenge]:
    try:
        for c in platform.list_challenges():
            if c.unique_code == unique_code:
                return c
    except Exception:
        log.exception("list_challenges failed while resolving %s", unique_code)
    return None


@dataclass
class WorkerRuntime:
    """进程装配一次的结果：两入口(console script / driver)共享，避免 env 装配漂移"""

    base_url: str
    platform: object
    jobs: JobQueueClient
    worker_id: str
    cfg: SolverConfig
    workdir_root: str
    solver: SolverBackend
    flag_format: str


def build_runtime_from_env() -> WorkerRuntime:
    """env 装配唯一入口：缺必需变量直接 exit(2)(两入口行为一致)。"""
    base_url = _envs("BENCHMARK_BASE_URL")
    token = _envs("BENCHMARK_TOKEN")
    if not base_url or not token:
        log.error("BENCHMARK_BASE_URL and BENCHMARK_TOKEN must be set")
        sys.exit(2)
    try:
        cfg = SolverConfig.from_env()
    except ValueError as e:
        # 裸 SOLVER_MODEL/垃圾 SESSION_SECONDS 等:明示退出码,不进 restart 闷循环
        log.error("bad solver config: %s", e)
        sys.exit(2)

    worker_id = _envs("WORKER_ID", "worker-1")
    workdir_root = os.path.join(_envs("ADAPTER_WORKDIR", "/work"), worker_id)
    return WorkerRuntime(
        base_url=base_url,
        platform=create_platform(base_url, token),
        jobs=JobQueueClient(base_url, token, worker_id),
        worker_id=worker_id,
        cfg=cfg,
        workdir_root=workdir_root,
        solver=create_solver(),
        flag_format=_envs("ADAPTER_FLAG_FORMAT", DEFAULT_FLAG_FORMAT),
    )


def _handle_job(*, platform, jobs: JobQueueClient,
                worker_id: str, cfg: SolverConfig,
                solver: SolverBackend,
                workdir_root: str, flag_format: str,
                callbacks: Optional[SolveCallbacks],
                submitted: dict[str, set[str]], job: JobInfo) -> None:
    """单 job 处理：ack → 解析题目 → 求解 → complete/fail 上报。异常内部消化。"""
    try:
        jobs.ack_running(job.job_id)
        challenge = _resolve_challenge(platform, job.unique_code)
        if challenge is None:
            jobs.fail(job.job_id, f"challenge not found: {job.unique_code}")
            return
        outcome: SolveOutcome = solve_challenge(
            platform=platform, challenge=challenge, cfg=cfg, solver=solver,
            workdir_root=workdir_root, flag_format=flag_format,
            callbacks=callbacks, submitted=submitted)
        jobs.complete(job.job_id, {
            "solved": outcome.solved,
            "accepted": len(outcome.accepted_flags),
            "turns": outcome.result.turns,
            "transcript_path": outcome.transcript_path,
            "error": outcome.result.error or "",
        })
        log.info("job %s reported: solved=%s accepted=%d turns=%d",
                 job.job_id, outcome.solved, len(outcome.accepted_flags),
                 outcome.result.turns)
    except Exception as e:
        log.exception("job %s failed", job.job_id)
        try:
            jobs.fail(job.job_id, str(e))
        except Exception:
            log.exception("fail report for job %s failed", job.job_id)


def run_queue_worker(
    *,
    platform,
    jobs: JobQueueClient,
    worker_id: str,
    cfg: SolverConfig,
    workdir_root: str,
    solver: Optional[SolverBackend] = None,
    callbacks: Optional[SolveCallbacks] = None,
    flag_format: str = DEFAULT_FLAG_FORMAT,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
    claim_poll: float = CLAIM_POLL,
    idle_sleep: float = IDLE_SLEEP,
) -> None:
    """常驻队列循环: claim → ack_running → solve_challenge → complete/fail。

    阻塞运行(console script / driver 的主循环);心跳由守护线程维护,
    每次心跳同时 touch 文件心跳(compose healthcheck)。
    """
    solver = solver or create_solver()
    state: dict = {"job_id": None}
    submitted: dict[str, set[str]] = {}

    def _beat() -> None:
        while True:
            time.sleep(heartbeat_interval)
            # 文件心跳(compose healthcheck)与平台 lease 分开 try:失败归因可区分
            try:
                touch_heartbeat()
            except Exception:
                log.debug("file heartbeat failed", exc_info=True)
            try:
                jobs.heartbeat(status="solving" if state["job_id"] else "idle",
                               current_job_id=state["job_id"])
            except Exception:
                log.debug("platform heartbeat failed", exc_info=True)

    threading.Thread(target=_beat, daemon=True, name="queue-heartbeat").start()
    os.makedirs(workdir_root, exist_ok=True)
    log.info("queue worker %s starting: model=%s workdir=%s",
             worker_id, cfg.model, workdir_root)

    while True:
        job = None
        try:
            job = jobs.claim()
        except Exception as e:
            log.warning("claim failed: %s; retrying in %.0fs", e, idle_sleep)
            time.sleep(idle_sleep)
            continue
        if job is None:
            # 空认领加抖动:多 worker 不再同一秒齐刷 claim
            time.sleep(claim_poll + random.uniform(0, 2.0))
            continue
        if not job.job_id or not job.unique_code:
            log.error("malformed job %r, skipping", job)
            continue

        state["job_id"] = job.job_id
        log.info("claimed job %s: %s (priority=%d)", job.job_id, job.unique_code, job.priority)
        try:
            _handle_job(platform=platform, jobs=jobs, worker_id=worker_id, cfg=cfg,
                        solver=solver, workdir_root=workdir_root,
                        flag_format=flag_format, callbacks=callbacks,
                        submitted=submitted, job=job)
        finally:
            state["job_id"] = None


def main() -> None:
    """console script 入口(tsecbench-worker): env 装配后进入队列循环"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    rt = build_runtime_from_env()
    run_queue_worker(platform=rt.platform, jobs=rt.jobs, worker_id=rt.worker_id,
                     cfg=rt.cfg, workdir_root=rt.workdir_root,
                     flag_format=rt.flag_format, solver=rt.solver)


if __name__ == "__main__":
    main()
