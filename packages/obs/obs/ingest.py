"""worker 摄取端点(仅 internal)— POST /api/internal/{live,events,run_close,roster,ping}。

鉴权:X-Observability-Token 常量时间比较;token 未配置 → 503(响亮失败,防静默空转)。
所有 DB 写经 run_in_threadpool(SQLite 阻塞不占事件循环;单写者=本进程,Store 锁内串行)。
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .schema import (ACTIVE_PHASES, EventsIn, LiveIn, PingIn, RosterIn, RunCloseIn,
                     require_run_id)
from .store import ObsStore

log = logging.getLogger("obs.ingest")

router = APIRouter(prefix="/api/internal", tags=["internal"])

_HEADER = "X-Observability-Token"

# live 快照的 idle 守卫:worker 重新上线却残留 running run → 重启残留
_IDLE_ERROR = "worker restarted idle"


def _check_token(request: Request) -> None:
    token = getattr(request.app.state, "obs_token", None)
    if not token:
        raise HTTPException(503, "observability token not configured")
    got = request.headers.get(_HEADER)
    if got is None or not hmac.compare_digest(got.encode(), token.encode()):
        raise HTTPException(401, "bad or missing token")


def _require_store(request: Request) -> ObsStore:
    return request.app.state.store


@router.post("/live")
async def post_live(body: LiveIn, request: Request,
                    _auth=Depends(_check_token)) -> dict:
    """worker 每 ~1s 一份快照;返回前份快照做崩溃守卫:换题未关旧 run / 重启残留 → interrupted。"""
    store = _require_store(request)
    snap = body.snapshot
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
        rows, body.model)
    if created:
        log.info("run %s open (worker=%s code=%s, %d events)", body.run_id,
                 body.worker_id, body.challenge_code, inserted)
    return {"ok": True, "inserted": inserted}


@router.post("/run_close")
async def post_run_close(body: RunCloseIn, request: Request,
                         _auth=Depends(_check_token)) -> dict:
    """关闭 run;终态幂等,无行静默忽略(relay 全序保证事件先于 close 到达)。"""
    store = _require_store(request)
    require_run_id(body.run_id)
    closed = await run_in_threadpool(
        store.close_run, body.run_id,
        status=body.status, error=body.error, turns=body.turns,
        sessions=body.sessions, flags_found=body.flags_found,
        flags_accepted=body.flags_accepted, ended_at=body.ended_at)
    if closed:
        log.info("run %s closed as %s (worker=%s)", body.run_id, body.status,
                 body.worker_id)
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
