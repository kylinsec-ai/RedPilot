"""摄取/查询的 pydantic 请求模型(字段名与 worker 侧契约逐字对齐)+ 共享词汇表。

词汇取值集合(phase/run 状态/run_id 正则)单源于 redpilot_contracts.vocabulary;
本模块 re-export 以保持 db/ingest/read/store 的既有 import 路径稳定。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from redpilot_contracts.vocabulary import (  # noqa: F401  (re-export)
    ACTIVE_PHASES,
    CLOSABLE_STATUSES,
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
    # 原文 JSON 行。上界:此前无上界 + 无保留策略 = 无界磁盘;单行 256 KiB 已远超
    # 任何真实工具输出行(超限的通常是进程失控或恶意载荷)。
    payload: str = Field(max_length=262_144)


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

    @model_validator(mode="after")
    def _snapshot_bounded(self) -> "LiveIn":
        """快照上界:LiveState 只有 18 个标量键,序列化后远小于此。

        无上界时,一份超大 snapshot 会落库(memory/磁盘)并被**回显给每个 SSE 客户端**,
        等于一个低成本的放大攻击面。
        """
        if len(json.dumps(self.snapshot, ensure_ascii=False)) > 65_536:
            raise ValueError("snapshot too large (max 64 KiB)")
        return self


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


class AcceptedFlagsIn(BaseModel):
    """已接受 flag 明文:非权威的**加性**观测数据,只补 runs.flags_accepted 一列。

    为何单开一条通道而不是塞进 canonical payload:canonical 事件会持久化到 core 的
    platform_events / outbox_events,而 core 的不变量是只存 SHA-256、不存明文 ——
    把明文塞进去会破坏该原则,且 worker 的 complete 请求本来也只带
    flags_found(计数),明文根本到不了 core。

    为何需要它:relay 有意跳过 run_close(canonical 拥有生命周期权威),而
    flags_accepted 此前只经 run_close 写入 —— 于是那条链路里已获得的 flag
    反而看不到(/api/challenge 恒返回 [])。
    """

    run_id: str
    flags: list[str] = Field(default_factory=list, max_length=64)


class RosterIn(BaseModel):
    worker_id: str
    snapshot: dict[str, Any]  # 完整 5 键旧格式快照 {fetched_at,stale,platform_error,platform_disabled,challenges}


class PingIn(BaseModel):
    worker_id: str


class CanonicalEventsIn(BaseModel):
    """core outbox 的事件批次；不携带 transcript 原文。"""

    events: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
