"""telemetry ingest(非权威):worker relay 的观测数据(events/live/run_close/roster/ping)。

权威语义:本模块所有写入都是非权威投影,只用于观测/调试,不参与计分,
永不能覆盖 canonical 终态(守卫在 ObsStore.close_run/append_events 内:
同行 canonical=1 拒写 + 跨行 attempt_id 全局守卫)。终态权威只归
canonical_ingest.post_canonical_events。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ghost.obs.ingest_common import check_token, require_store
from ghost_contracts.vocabulary import strip_for_snapshot
from ghost.obs.schema import (ACTIVE_PHASES, EventsIn, LiveIn, PingIn, RosterIn,
                     RunCloseIn, require_run_id)

log = logging.getLogger("obs.telemetry_ingest")

router = APIRouter(prefix="/api/internal", tags=["internal"])

# live 快照的 idle 守卫:worker 重新上线却残留 running run → 重启残留
_IDLE_ERROR = "worker restarted idle"


def _check_token(request: Request) -> None:
    check_token(request)


def _require_store(request: Request):
    return require_store(request)


@router.post("/live")
async def post_live(body: LiveIn, request: Request,
                    _auth=Depends(_check_token)) -> dict:
    """worker 每 ~1s 一份快照;返回前份快照做崩溃守卫:换题未关旧 run / 重启残留 → interrupted。"""
    store = _require_store(request)
    # 落库前剥掉 `_` 前缀带外键:ghost_contracts.vocabulary 规定这些键(如
    # `_accepted_flags` 明文 flag)只在中继→平台链路内部流转,不得进快照/仪表板。
    # 此前未剥,导致明文 flag 可经 /api/status 与 SSE 播给任何读端。
    snap = strip_for_snapshot(body.snapshot)
    phase = snap.get("phase") or ""
    prev = await run_in_threadpool(store.put_live, body.worker_id, snap)
    if prev:
        prev_phase = prev.get("phase") or ""
        code = snap.get("challenge_code") or ""
        prev_code = prev.get("challenge_code") or ""
        if phase == "idle" and prev_phase in ACTIVE_PHASES:
            # 前帧仍在求解、本帧 idle → worker 进程重启残留(未发 run_close 的 running run)
            closed = await run_in_threadpool(store.close_running_for_worker,
                                             body.worker_id, _IDLE_ERROR)
            if closed:
                log.info("worker %s idle after %s → interrupted running run(s): %s",
                         body.worker_id, prev_phase, closed)
        elif (phase in ACTIVE_PHASES and prev_phase in ACTIVE_PHASES
                and prev_code and prev_code != code):
            closed = await run_in_threadpool(store.close_runs_for_switch,
                                             body.worker_id, code)
            if closed:
                log.info("worker %s switched %s → %s, interrupted stale run(s): %s",
                         body.worker_id, prev_code, code, closed)
    bus = getattr(request.app.state, "bus", None)
    if bus is not None:  # SSE:每帧 = 一次 live POST 的 {**snapshot, kind}
        await bus.publish({**snap, "kind": body.kind})
    return {"ok": True}


@router.post("/events")
async def post_events(body: EventsIn, request: Request,
                      _auth=Depends(_check_token)) -> dict:
    """事件流批插入:UNIQUE(run_id,seq) 幂等;全重放新增=0。建行与批插单事务。"""
    store = _require_store(request)
    require_run_id(body.run_id)
    if not body.events:
        return {"ok": True, "inserted": 0}
    rows = [(e.seq, e.type, e.payload) for e in body.events]
    inserted, created = await run_in_threadpool(
        store.append_events, body.run_id, body.worker_id, body.challenge_code,
        rows, body.model, None, body.evaluation_id, body.job_id, body.attempt_id)
    if created:
        log.info("run %s open (worker=%s code=%s, %d events)", body.run_id,
                 body.worker_id, body.challenge_code, inserted)
    return {"ok": True, "inserted": inserted}


@router.post("/run_close")
async def post_run_close(body: RunCloseIn, request: Request,
                         _auth=Depends(_check_token)) -> dict:
    """关闭 run;终态幂等,无行静默忽略(relay 全序保证事件先于 close 到达)。

    非权威:目标行 canonical=1(或同 attempt 任一行 canonical=1)时拒写,
    详见 ObsStore.close_run。
    """
    store = _require_store(request)
    require_run_id(body.run_id)
    closed = await run_in_threadpool(
        store.close_run, body.run_id,
        status=body.status, error=body.error, turns=body.turns,
        sessions=body.sessions, flags_found=body.flags_found,
        flags_accepted=body.flags_accepted, ended_at=body.ended_at,
        evaluation_id=body.evaluation_id, job_id=body.job_id,
        attempt_id=body.attempt_id)
    if closed:
        log.info("run %s closed as %s (worker=%s)", body.run_id, body.status,
                 body.worker_id)
    else:
        # 无行:未知 run 或重复 close(终态幂等)——ok 仍返回,info 留痕供定位丢 run。
        log.info("run_close touched no rows (run=%s worker=%s status=%s)",
                 body.run_id, body.worker_id, body.status)
    return {"ok": True}


@router.post("/roster")
async def post_roster(body: RosterIn, request: Request,
                      _auth=Depends(_check_token)) -> dict:
    """整份旧格式快照(worker RosterPoller 60s 节奏;每 worker 一行)。"""
    store = _require_store(request)
    await run_in_threadpool(store.put_roster, body.worker_id, body.snapshot)
    return {"ok": True}


@router.post("/ping")
async def post_ping(body: PingIn, request: Request,
                    _auth=Depends(_check_token)) -> dict:
    """30s 心跳:仅刷新 updated_at;无 live 行则忽略。"""
    store = _require_store(request)
    await run_in_threadpool(store.ping, body.worker_id)
    return {"ok": True}
