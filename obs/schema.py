"""摄取/查询的 pydantic 请求模型(字段名与 worker 侧契约逐字对齐)。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class EventIn(BaseModel):
    seq: int
    type: str
    ts: float | None = None  # worker ship 时刻 epoch s(可空)
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
    status: Literal["solved", "done", "failed"]
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
