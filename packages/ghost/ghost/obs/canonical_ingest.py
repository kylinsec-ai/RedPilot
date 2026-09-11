"""canonical ingest(权威):core outbox 的 attempt 生命周期事件。

权威语义:attempt.started/completed 是 obs 终态唯一权威(canonical source),
只能经本模块写入。telemetry_ingest.post_run_close 永不能覆盖此处写入的终态。
source 标记即 runs.canonical 列(1=权威终态已落库,0=仅 telemetry)。
乱序/幂等/跨 run_id 合并规则见 ObsStore.append_events/close_run 与
test_canonical_run_close.py(P0 不变量,本模块只做路由与字段映射,不改语义)。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ghost_contracts.platform import (
    is_canonical_event_type,
    is_canonical_terminal_status,
)

from ghost.obs.ingest_common import check_token, require_store
from ghost.obs.schema import CanonicalEventsIn, require_run_id

log = logging.getLogger("obs.canonical_ingest")

router = APIRouter(prefix="/api/internal", tags=["internal"])


@router.post("/canonical-events")
async def post_canonical_events(body: CanonicalEventsIn, request: Request,
                                _auth=Depends(check_token)) -> dict:
    """接收 core outbox 的 attempt 生命周期，投影到同一条 obs run。"""
    # 先全量校验 attempt_id 形态,任一非法即 400 且不落任何写(避免批内部分提交与 /events 语义不一致)
    for event in body.events:
        attempt_id = str(event.get("attempt_id") or "")
        if attempt_id:
            require_run_id(attempt_id)
    store = require_store(request)
    processed = 0
    skipped = 0
    for event in body.events:
        event_type = str(event.get("event_type") or "")
        attempt_id = str(event.get("attempt_id") or "")
        if not attempt_id:
            skipped += 1
            continue
        if not is_canonical_event_type(event_type):
            log.warning("canonical event with unknown type %r skipped (attempt=%s)",
                        event_type, attempt_id)
            skipped += 1
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        worker_id = str(event.get("worker_id") or "")
        evaluation_id = event.get("evaluation_id")
        job_id = event.get("job_id")
        occurred_at = event.get("occurred_at")
        if event_type == "attempt.started":
            code = str(payload.get("unique_code") or "unknown")
            if code == "unknown":
                log.warning("canonical attempt.started without unique_code (attempt=%s)", attempt_id)
            await run_in_threadpool(
                store.append_events,
                attempt_id,
                worker_id,
                code,
                [],
                str(payload.get("model") or ""),
                occurred_at if isinstance(occurred_at, (int, float)) else None,
                evaluation_id,
                job_id,
                attempt_id,
            )
            processed += 1
        elif event_type == "attempt.completed":
            status = str(payload.get("status") or "done")
            if not is_canonical_terminal_status(status):
                # 未知终态不落库:core 已标已投递,此处计数并告警,避免绿灯丢数据。
                log.warning("canonical attempt.completed with unknown status %r (attempt=%s)",
                            status, attempt_id)
                skipped += 1
                continue
            await run_in_threadpool(
                store.close_run,
                attempt_id,
                status=status,
                error=payload.get("error"),
                flags_found=payload.get("flags_found"),
                ended_at=occurred_at if isinstance(occurred_at, (int, float)) else None,
                evaluation_id=evaluation_id,
                job_id=job_id,
                attempt_id=attempt_id,
                canonical=True,
                worker_id=worker_id,
            )
            processed += 1
    return {"ok": True, "processed": processed, "skipped": skipped}
