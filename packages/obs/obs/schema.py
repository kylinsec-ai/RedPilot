"""摄取/查询的 pydantic 请求模型(字段名与 worker 侧契约逐字对齐)+ 共享词汇表。

词汇取值集合(phase/run 状态/run_id 正则)单源于 tsecbench_contracts.vocabulary;
本模块 re-export 以保持 db/ingest/read/store 的既有 import 路径稳定。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel

from tsecbench_contracts.vocabulary import (  # noqa: F401  (re-export)
    ACTIVE_PHASES,
    CLOSABLE_STATUSES,
    RUN_CLOSE_STATUSES as CLOSE_STATUSES,
    RUN_ID_RX,
    RUN_STATUSES,
)


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
