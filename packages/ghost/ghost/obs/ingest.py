"""ingest 路由装配(telemetry 非权威 + canonical 权威)。

拆分说明:实际处理见 telemetry_ingest(worker relay 观测)与
canonical_ingest(core outbox 权威终态);本模块只做 router 组装。
canonical 终态只能经 canonical 处理路径改变,relay run_close 永不覆盖。
"""

from __future__ import annotations

from fastapi import APIRouter

from ghost.obs.canonical_ingest import router as canonical_router
from ghost.obs.telemetry_ingest import router as telemetry_router

router = APIRouter()
router.include_router(telemetry_router)
router.include_router(canonical_router)
