"""靶场生命周期:challenge 实例 start/close 重试语义(从 orchestration 抽出)。

职责:只负责目标靶场可用性(start 重试/close 重试),不碰 LLM 会话、不碰
telemetry、不碰 assignment lease。orchestration.solve_one 经本模块拿
targets,driver 不直调 SDK start/close。
SDK 异常语义保持原样:InvalidState(任务结束/槽位满)→退避或 None;
ChallengeNotFound→None;其余异常→重试后 None/False。
"""

from __future__ import annotations

import asyncio
import logging

from tsec_benchmark import (
    ChallengeNotFound,
    InvalidState,
    ResourceUnavailable,
)

from .solver import touch_heartbeat

log = logging.getLogger("ghost_worker.target")

START_MAX_RETRIES = 8     # 单题启动重试(平台并发槽位竞争)
CLOSE_RETRIES = 3


async def start_target(client, code: str, max_retries: int = START_MAX_RETRIES):
    """带重试的实例启动;失败返回 None(平台槽位满时退避等待)。"""

    for i in range(max_retries):
        touch_heartbeat()
        try:
            return await client.start_challenge(code)
        except InvalidState as e:
            # 409: 活跃实例达上限或任务已结束
            msg = getattr(e, "message", "") or str(e)
            if any(t in msg for t in ("上限", "active", "max")):
                wait = min(3.0 * (i + 1), 20.0)
                log.warning("max active on %s; waiting %.0fs (%d/%d)", code, wait, i + 1, max_retries)
                await asyncio.sleep(wait)
                continue
            log.error("task ended (invalid_state) on %s: %s", code, msg)
            return None
        except ResourceUnavailable:
            log.warning("resource unavailable on %s, retry", code)
            await asyncio.sleep(5)
        except ChallengeNotFound:
            log.error("challenge not found: %s", code)
            return None
        except Exception as e:
            log.error("start_challenge failed on %s: %s", code, e)
            if i + 1 < max_retries:
                await asyncio.sleep(3)
    log.error("giving up starting %s after %d tries", code, max_retries)
    return None


async def close_target(client, code: str, max_retries: int = CLOSE_RETRIES) -> bool:
    """带重试的实例关闭;SDK 对已关/任务已停(404/409)视为已关闭返回 False。"""

    for i in range(max_retries):
        try:
            return (await client.close_challenge(code)).closed
        except (ChallengeNotFound, InvalidState):
            return False
        except Exception:
            if i + 1 < max_retries:
                await asyncio.sleep(min(2.0 * (i + 1), 6.0))
    log.error("FAILED to close %s after %d tries", code, max_retries)
    return False
