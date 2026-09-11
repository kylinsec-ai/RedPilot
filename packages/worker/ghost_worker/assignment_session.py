"""assignment 生命周期:claim/heartbeat/complete 的客户端语义(从 driver 抽出)。

职责:只负责控制面租约(续租循环 + complete 重试),不碰靶场 start/close、
不碰 agent 会话、不碰 telemetry relay。driver._solve_assignment 经本模块
做 lease 看护与终态上报;legacy list 模式不经过本模块。
"""

from __future__ import annotations

import asyncio
import logging

from .assignment import AssignmentClient, AssignmentError

log = logging.getLogger("ghost_worker.assignment_session")

TERMINAL_REJECTIONS = frozenset({
    "attempt_not_found", "lease_conflict", "worker_not_registered",
})
COMPLETE_ATTEMPTS = 5


def is_terminal_rejection(exc: AssignmentError) -> bool:
    """终态拒绝(404/409/鉴权失效):重试无意义,调用方应放弃。"""

    return (
        exc.status_code in {401, 403, 404, 409}
        or exc.code in TERMINAL_REJECTIONS
    )


async def lease_watch(client: AssignmentClient, assignment: dict,
                      lease_seconds: int, lease_lost: "asyncio.Event") -> None:
    """长时间会话期间续租;仅终态码置 lease_lost,传输失败退避重试。

    与 driver 旧 _assignment_lease_watch 同语义,抽出后 driver 只做装配。
    """

    interval = max(10.0, min(60.0, lease_seconds / 3))
    failures = 0
    while True:
        await asyncio.sleep(interval)
        try:
            await client.attempt_heartbeat(
                assignment["attempt_id"], assignment["lease_id"], lease_seconds
            )
        except AssignmentError as exc:
            if is_terminal_rejection(exc):
                log.error("assignment lease lost for %s (%s), abandoning solve",
                          assignment.get("job_id"), exc.code)
                lease_lost.set()
                return
            failures += 1
            log.warning("assignment heartbeat failed for %s (%s), retrying (%d)",
                        assignment.get("job_id"), exc, failures)
        except Exception:
            failures += 1
            log.exception("assignment heartbeat transport failed for %s, retrying (%d)",
                          assignment.get("job_id"), failures)
        else:
            failures = 0


async def complete_with_retry(client: AssignmentClient, code: str,
                              attempt_id: str, lease_id: str, *,
                              status: str, solved: bool = False,
                              flags_found: int | None = None,
                              error: str | None = None) -> None:
    """complete 最多 5 次;终态拒绝立即停,瞬断退避重试(与 driver 旧循环同语义)。"""

    for attempt_no in range(COMPLETE_ATTEMPTS):
        try:
            await client.complete(
                attempt_id,
                lease_id,
                status=status,
                solved=solved,
                flags_found=flags_found,
                error=error[:2000] if error else None,
            )
            break
        except AssignmentError as exc:
            if is_terminal_rejection(exc):
                log.error("assignment completion rejected for %s: %s", code, exc)
                break
            log.warning("assignment completion failed for %s (%s), retrying (%d)",
                        code, exc, attempt_no + 1)
        except Exception:
            log.exception("assignment completion transport failed for %s, retrying (%d)",
                          code, attempt_no + 1)
        await asyncio.sleep(min(2.0 * (attempt_no + 1), 10.0))
