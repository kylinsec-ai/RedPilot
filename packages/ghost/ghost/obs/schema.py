"""摄取/查询的 pydantic 请求模型(字段名与 worker 侧契约逐字对齐)+ 共享词汇表。

词汇取值集合(phase/run 状态/run_id 正则)单源于 ghost_contracts.vocabulary;
本模块 re-export 以保持 db/ingest/read/store 的既有 import 路径稳定。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ghost_contracts.vocabulary import (  # noqa: F401  (re-export)
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
    seq: int = Field(ge=0)
    type: str = Field(min_length=1, max_length=128)
    payload: str  # 原文 JSON 行


class EventsIn(BaseModel):
    run_id: str
    worker_id: str
    challenge_code: str
    model: str = ""
    evaluation_id: str | None = None
    job_id: str | None = None
    attempt_id: str | None = None
    events: list[EventIn] = Field(default_factory=list, max_length=500)


class LiveIn(BaseModel):
    worker_id: str
    kind: str = "lifecycle"
    snapshot: dict[str, Any]  # LiveState 18 键 JSON(原键名不动)


class RunCloseIn(BaseModel):
    run_id: str
    worker_id: str
    # 注意:排除 interrupted —— 控制面 interrupted 表示 job 回 pending 待重做,
    # obs 侧 run 保持 running 等下一轮 terminal 事件;relay 只发终态。
    status: Literal["solved", "done", "failed"]  # 展开字面量(兼容 3.10,同 contracts.RUN_CLOSE_STATUSES)
    evaluation_id: str | None = None
    job_id: str | None = None
    attempt_id: str | None = None
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


class CanonicalEventsIn(BaseModel):
    """core outbox 的事件批次；不携带 transcript 原文。"""

    events: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
