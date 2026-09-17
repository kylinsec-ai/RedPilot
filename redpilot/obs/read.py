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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from redpilot.obs.ingest_common import check_read_token
from redpilot.obs.schema import RUN_STATUSES, require_run_id
from redpilot.contracts.assets import ASSET_RX as _ASSET_RX
from redpilot.contracts.assets import ASSET_TYPES as _ASSET_TYPES
from redpilot.contracts.digest import fold_rows
from redpilot.contracts.vocabulary import SSE_HEARTBEAT_S, SNAPSHOT_KIND, strip_out_of_band

log = logging.getLogger("obs.read")

# 两个 router:保护是**面的属性**,不是逐端点的记得。
# 默认(protected)带读凭据 —— 新增 /api/* 路由天然受保护,漏挂不再等于裸奔;
# 只有 SPA 资产与健康检查放行(不含任何答案材料)。
public = APIRouter(tags=["read"])
protected = APIRouter(tags=["read"], dependencies=[Depends(check_read_token)])

# ── 前端构建产物(vite 多文件 dist → web/assets/*)──
# mime 表与穿越守卫单源于 redpilot.contracts.assets(与 worker 侧仪表板共用);
# 本服务的缓存策略(index/assets 头)是自身关切,不归 contracts。
_VALID_RX = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _store(request: Request):
    return request.app.state.store


def _web_dir(request: Request) -> str | None:
    return getattr(request.app.state, "web_dir", None)


# NOTE:控制面反向代理已移至 obs.control_proxy(可选插件,默认不挂载);
# 本模块只保留纯读端(观测 API),不再转发 /api/v1/*。
#
# 鉴权分层(见 ingest_common.check_read_token):
#   公开 —— `/`、`/assets/*`(SPA 外壳,不含数据;否则登录界面本身无法渲染)
#           `/api/health`(存活探测,不含数据)
#   需凭据 —— 其余全部 `/api/*`:这些返回**明文 flag 与完整 agent 实录**,
#           而 worker 容器持 ingest token 且与平台同网,不能靠网络位置兜底。


def _no_store(body: bytes, media_type: str) -> Response:
    return Response(content=body, media_type=media_type,
                    headers={"Cache-Control": "no-store"})


def _check_code(code: str) -> None:
    if not _VALID_RX.fullmatch((code or "").strip()):
        raise HTTPException(400, "bad code")


# ── 静态 SPA ──

@public.get("/", include_in_schema=False)
@public.get("/index.html", include_in_schema=False)
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


@public.get("/assets/{name:path}", include_in_schema=False)
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
            body = f.read()
    except OSError:
        raise HTTPException(
            404, "web/assets/" + name
            + " not baked (run `cd frontend && npm run build`; commit web/)")
    # vite 产物名带内容 hash → 不可变缓存;只有 index.html 保持 no-store(开发期热更)
    return Response(content=body, media_type=_ASSET_TYPES[ext],
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ── 系统 ──

@public.get("/api/health")
async def health(request: Request) -> Response:
    store = _store(request)
    if not await run_in_threadpool(store.health):
        raise HTTPException(500, "db check failed")
    return Response("ok", media_type="text/plain")


# ── 状态 / 事件推送 ──

@protected.get("/api/status")
async def status(request: Request) -> dict:
    """最新活 worker 的 LiveState 快照;无任何数据时 {}。

    出口剥掉 `_` 前缀带外键:redpilot.contracts.vocabulary 规定这些键(如 `_accepted_flags`
    明文 flag)**不得广播给仪表板**;worker 侧 localserver 遵守了,obs 此前没有。
    """
    store = _store(request)
    snap = await run_in_threadpool(store.live_latest)
    return strip_out_of_band(dict(snap)) if snap else {}


@protected.get("/api/events")
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
            first.setdefault("kind", SNAPSHOT_KIND)
            first.setdefault("ts", time.time())
            # 出口统一剥带外键(见 status 的说明);落库侧已剥,此处为防御性再剥。
            yield "data: " + json.dumps(strip_out_of_band(first), ensure_ascii=False) + "\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_S)
                    yield "data: " + json.dumps(strip_out_of_band(ev), ensure_ascii=False) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive"})


# ── 题目总览 / 详情 ──

@protected.get("/api/roster")
async def roster(request: Request) -> dict:
    """题目总览:多 worker roster 读合并(单 worker 与原快照同键同值)。"""
    store = _store(request)
    return await run_in_threadpool(store.roster_merged)


@protected.get("/api/challenge")
async def challenge(code: str = Query(...), request: Request = None) -> dict:
    """单题详情:平台行 + local + flags(取自最近一次含 flags_accepted 的 run);
    行选择与 /api/roster 同一份合并视图(非 local_only 优先)。本地无数据返回最小行,不 500。"""
    code = (code or "").strip()
    _check_code(code)
    store = _store(request)
    merged = await run_in_threadpool(store.roster_merged)
    row = merged["challenges"].get(code)
    out = dict(row) if row else {}
    out["unique_code"] = code
    if not row:
        out.setdefault("local_only", True)
    out["flags"] = await run_in_threadpool(store.challenge_flags, code)
    return out


@protected.get("/api/transcript")
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


@protected.get("/api/timeline")
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

@protected.get("/api/runs")
async def runs_list(status: str | None = None, worker: str | None = None,
                    challenge: str | None = None, limit: int = Query(200, ge=1, le=500),
                    request: Request = None) -> dict:
    if status is not None and status not in RUN_STATUSES:
        raise HTTPException(400, "bad status")
    if worker is not None and not _VALID_RX.fullmatch(worker):
        raise HTTPException(400, "bad worker")
    if challenge is not None:
        _check_code(challenge)
    store = _store(request)
    # flags_accepted 已在 store 层解析(list;无 = [])
    rows = await run_in_threadpool(store.list_runs, status, worker, challenge, limit)
    return {"runs": rows}


@protected.get("/api/runs/{run_id}")
async def runs_detail(run_id: str, request: Request = None) -> dict:
    require_run_id(run_id)
    store = _store(request)
    row = await run_in_threadpool(store.run_row, run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    return row


@protected.get("/api/runs/{run_id}/events")
async def runs_events(run_id: str, after: int = Query(0),
                      limit: int = Query(500, ge=1, le=1000),
                      request: Request = None) -> dict:
    require_run_id(run_id)
    after = max(0, int(after))
    store = _store(request)
    # 存在性探测走轻量 SELECT 1(不必拉整行投影)
    if not await run_in_threadpool(store.run_exists, run_id):
        raise HTTPException(404, "run not found")
    return await run_in_threadpool(store.run_events, run_id, after, limit)


@protected.get("/api/runs/{run_id}/timeline")
async def runs_timeline(run_id: str, after: int = Query(0),
                        request: Request = None) -> dict:
    """单 run 折叠时间线(复用同一 fold,seq 为该 run 内序)。"""
    require_run_id(run_id)
    after = max(0, int(after))
    store = _store(request)
    if not await run_in_threadpool(store.run_exists, run_id):
        raise HTTPException(404, "run not found")
    rows = await run_in_threadpool(store.events_for_run, run_id)
    return await run_in_threadpool(fold_rows, rows, after=after, live=False)


# 对外单一入口(装配方 include_router(read.router) 不变)。
router = APIRouter()
router.include_router(public)
router.include_router(protected)
