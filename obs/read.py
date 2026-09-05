"""只读端点:静态 SPA + /api/*(契约与旧 status_server 字节兼容)+ runs 历史接口。

SSE /api/events 由摄取触发的进程内 asyncio bus 驱动:连接即推最新快照首帧
(kind=snapshot),其后每帧 = 一次 live POST 的 {**snapshot, kind, ts},15s 无帧写
心跳注释行 —— 与旧 status_server._sse 协议一致(asyncio 订阅随客户端断开自动清理)。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .digest import fold_rows

log = logging.getLogger("obs.read")

router = APIRouter(tags=["read"])

# ── 前端构建产物(vite 多文件 dist → web/assets/*)──
# 名字只允许 URL 安全平铺名(vite 只发 <hash>.js/.css),杜绝路径穿越
_ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
_ASSET_RX = re.compile(r"[A-Za-z0-9._-]{1,120}")
_VALID_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RUN_ID_RX = re.compile(r"^[0-9a-f]{32}$")


def _store(request: Request):
    return request.app.state.store


def _web_dir(request: Request) -> str | None:
    return getattr(request.app.state, "web_dir", None)


def _no_store(body: bytes, media_type: str) -> Response:
    return Response(content=body, media_type=media_type,
                    headers={"Cache-Control": "no-store"})


def _check_code(code: str) -> None:
    if not _VALID_RX.fullmatch((code or "").strip()):
        raise HTTPException(400, "bad code")


# ── 静态 SPA ──

@router.get("/", include_in_schema=False)
@router.get("/index.html", include_in_schema=False)
def index(request: Request) -> Response:
    web_dir = _web_dir(request)
    if not web_dir:
        raise HTTPException(404, "web dir not configured"
                                  " (set OBSERVABILITY_WEB or pass web_dir)")
    try:
        with open(os.path.join(web_dir, "index.html"), "rb") as f:
            return _no_store(f.read(), "text/html; charset=utf-8")
    except OSError:
        raise HTTPException(
            404, "web/index.html not baked (run `cd frontend && npm run build`; commit web/)")


@router.get("/assets/{name:path}", include_in_schema=False)
def asset(name: str, request: Request) -> Response:
    """静态构建产物(web/assets/*)。名字只允许 URL 安全平铺名,杜绝穿越。"""
    ext = os.path.splitext(name)[1].lower()
    if (not name or "/" in name or "\\" in name or "\x00" in name
            or not _ASSET_RX.fullmatch(name) or ext not in _ASSET_TYPES):
        raise HTTPException(404, "asset not found")
    web_dir = _web_dir(request)
    if not web_dir:
        raise HTTPException(404, "asset not found")
    p = os.path.join(web_dir, "assets", name)  # name 无分隔符 => 必在 web_dir 内
    try:
        with open(p, "rb") as f:
            return _no_store(f.read(), _ASSET_TYPES[ext])
    except OSError:
        raise HTTPException(
            404, "web/assets/" + name
            + " not baked (run `cd frontend && npm run build`; commit web/)")


# ── 系统 ──

@router.get("/api/health")
async def health(request: Request) -> Response:
    store = _store(request)
    if not await run_in_threadpool(store.health):
        raise HTTPException(500, "db check failed")
    return Response("ok", media_type="text/plain")


# ── 状态 / 事件推送 ──

@router.get("/api/status")
async def status(request: Request) -> dict:
    """最新活 worker 的 LiveState 快照;无任何数据时 {}。"""
    store = _store(request)
    snap = await run_in_threadpool(store.live_latest)
    return snap if snap else {}


@router.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    """SSE:首帧 = 最新快照(kind=snapshot);其后每帧 = 一次 live POST;15s 心跳。"""
    import asyncio

    store = _store(request)
    bus = request.app.state.bus
    q = bus.subscribe()

    async def gen():
        try:
            latest = await run_in_threadpool(store.live_latest)
            first = dict(latest) if latest else {}
            first.setdefault("kind", "snapshot")
            first.setdefault("ts", time.time())
            yield "data: " + json.dumps(first, ensure_ascii=False) + "\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive"})


# ── 题目总览 / 详情 ──

@router.get("/api/roster")
async def roster(request: Request) -> dict:
    """题目总览:多 worker roster 读合并(单 worker 与原快照同键同值)。"""
    store = _store(request)
    return await run_in_threadpool(store.roster_merged)


@router.get("/api/challenge")
async def challenge(code: str = Query(...), request: Request = None) -> dict:
    """单题详情:平台行 + local + flags(取自最近一次含 flags_accepted 的 run);
    本地无数据返回最小行,不 500。"""
    code = (code or "").strip()
    _check_code(code)
    store = _store(request)
    rows = await run_in_threadpool(store.roster_rows)
    platform_row: dict = {}
    for r in sorted(rows, key=lambda x: x["fetched_at"] or 0.0, reverse=True):
        row = r["challenges"].get(code)
        if row is not None:
            platform_row = dict(row)
            break
    merged = {**platform_row, "unique_code": code}
    if not platform_row:
        merged.setdefault("local_only", True)
    flags = await run_in_threadpool(store.challenge_flags, code)
    merged["flags"] = flags
    return merged


@router.get("/api/transcript")
async def transcript(code: str = Query(...), tail: int = Query(200),
                     request: Request = None) -> dict:
    """该 code 的原文事件尾部(跨 run):行 = events.payload(无 message_update)。"""
    code = (code or "").strip()
    _check_code(code)
    try:
        tail = max(1, min(int(tail), 2000))
    except (TypeError, ValueError):
        tail = 200
    store = _store(request)
    lines = await run_in_threadpool(store.transcript_tail, code, tail)
    return {"code": code, "lines": lines}


@router.get("/api/timeline")
async def timeline(code: str = Query(...), after: int = Query(0),
                   request: Request = None) -> dict:
    """该 code 的时间线(跨 run 合流折叠);after=上一轮 next_seq 增量续拉。"""
    code = (code or "").strip()
    _check_code(code)
    after = max(0, int(after))
    store = _store(request)
    rows = await run_in_threadpool(store.events_for_code, code)
    live = code in await run_in_threadpool(store.active_live_codes)
    return await run_in_threadpool(fold_rows, rows, after=after, live=live)


# ── runs 历史 ──

_RUN_STATUSES = ("running", "solved", "done", "failed", "interrupted")


def _flags_list(row: dict) -> list | None:
    raw = row.get("flags_accepted")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None


@router.get("/api/runs")
async def runs_list(status: str | None = None, worker: str | None = None,
                    challenge: str | None = None, limit: int = Query(200, ge=1, le=500),
                    request: Request = None) -> dict:
    if status is not None and status not in _RUN_STATUSES:
        raise HTTPException(400, "bad status")
    if worker is not None and not _VALID_RX.fullmatch(worker):
        raise HTTPException(400, "bad worker")
    if challenge is not None:
        _check_code(challenge)
    store = _store(request)
    rows = await run_in_threadpool(store.list_runs, status, worker, challenge, limit)
    for row in rows:  # flags_accepted 归一化与 detail 一致(list | None)
        row["flags_accepted"] = _flags_list(row)
    return {"runs": rows}


@router.get("/api/runs/{run_id}")
async def runs_detail(run_id: str, request: Request = None) -> dict:
    if not _RUN_ID_RX.fullmatch(run_id):
        raise HTTPException(400, "bad run_id")
    store = _store(request)
    row = await run_in_threadpool(store.run_row, run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    if row["flags_accepted"]:
        try:
            row["flags_accepted"] = json.loads(row["flags_accepted"])
        except Exception:
            row["flags_accepted"] = []
    else:
        row["flags_accepted"] = []
    return row


@router.get("/api/runs/{run_id}/events")
async def runs_events(run_id: str, after: int = Query(0),
                      limit: int = Query(500, ge=1, le=1000),
                      request: Request = None) -> dict:
    if not _RUN_ID_RX.fullmatch(run_id):
        raise HTTPException(400, "bad run_id")
    after = max(0, int(after))
    store = _store(request)
    if await run_in_threadpool(store.run_row, run_id) is None:
        raise HTTPException(404, "run not found")
    return await run_in_threadpool(store.run_events, run_id, after, limit)


@router.get("/api/runs/{run_id}/timeline")
async def runs_timeline(run_id: str, after: int = Query(0),
                        request: Request = None) -> dict:
    """单 run 折叠时间线(复用同一 fold,seq 为该 run 内序)。"""
    if not _RUN_ID_RX.fullmatch(run_id):
        raise HTTPException(400, "bad run_id")
    after = max(0, int(after))
    store = _store(request)
    if await run_in_threadpool(store.run_row, run_id) is None:
        raise HTTPException(404, "run not found")
    rows = await run_in_threadpool(store.events_for_run, run_id)
    return await run_in_threadpool(fold_rows, rows, after=after, live=False)
