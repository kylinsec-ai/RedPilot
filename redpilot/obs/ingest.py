"""ingest 路由装配。

实际处理见 telemetry_ingest(worker relay 观测);本模块只做 router 组装。

**沿革**:此前并装一条 canonical_ingest(core outbox 的权威终态)。那条通道随
control 侧的 evaluation/job/attempt 派发协议于 2026-09 一并拆除,现在摄取
只有一个来源:relay telemetry。
"""

from __future__ import annotations

from fastapi import APIRouter

from redpilot.obs.telemetry_ingest import router as telemetry_router

router = APIRouter()
router.include_router(telemetry_router)
