"""摄取/查询的 pydantic 请求模型(字段名与 worker 侧契约逐字对齐)+ 共享词汇表。

run 状态/phase/run_id 的正则与取值集合全库单源于此:db CHECK、读端过滤、
close 校验、崩溃守卫都从这里 import,避免多份字面量漂移。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel

# ── 共享词汇表 ──

# runs.status 全集合;runs 表 CHECK 与 /api/runs 过滤共用
RUN_STATUSES: tuple[str, ...] = ("running", "solved", "done", "failed", "interrupted")
# run_close 可写的终态(其余状态只读)
CLOSE_STATUSES: tuple[str, ...] = ("solved", "done", "failed")
# live 快照 phase 集合(崩溃守卫与 active-live 判定;与 worker LiveState 一致)
ACTIVE_PHASES: tuple[str, ...] = ("starting", "solving", "submitting", "closing")
# 可被 close 写入的 run 状态(终态幂等)
CLOSABLE_STATUSES: tuple[str, ...] = ("running", "interrupted")
# run_id 形态:uuid4().hex
RUN_ID_RX = re.compile(r"^[0-9a-f]{32}$")


def require_run_id(run_id: str) -> None:
    """run_id 形态守卫(摄取/读端共 5 处调用点共用;非法直接 400)。"""
    if not RUN_ID_RX.fullmatch(run_id or ""):
        raise HTTPException(400, "bad run_id")


class EventIn(BaseModel):
    seq: int
    type: str
    payload: str  # 原文 JSON 行


class EventsIn(BaseModel):
    run_id: str
    worker_id: str
    challenge_code: str
    model: str = ""
    events: list[EventIn]


class LiveIn(BaseModel):
    worker_id: str
    kind: str = "lifecycle"
    snapshot: dict[str, Any]  # LiveState 18 键 JSON(原键名不动)


class RunCloseIn(BaseModel):
    run_id: str
    worker_id: str
    status: Literal[*CLOSE_STATUSES]
    error: str | None = None
    turns: int | None = None
    sessions: int | None = None
    flags_found: int | None = None
    flags_accepted: list[str] | None = None
    ended_at: float | None = None  # epoch s;缺省取服务端 now


class RosterIn(BaseModel):
    worker_id: str
    snapshot: dict[str, Any]  # 完整 5 键旧格式快照 {fetched_at,stale,platform_error,platform_disabled,challenges}


class PingIn(BaseModel):
    worker_id: str
