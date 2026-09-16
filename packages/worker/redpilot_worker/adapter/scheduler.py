"""
并发调度器

管理多个挑战的并行执行:
- 线程池并发
- best-of-N 尝试
- 结果汇总
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

log = logging.getLogger("adapter.scheduler")


def run_fleet(
    challenges: list,
    visit_fn: Callable,
    *,
    is_success: Callable = None,
    max_concurrent: int = 3,
    best_of: int = 1,
) -> dict:
    """
    并发执行一批挑战。

    参数:
        challenges: 挑战列表 (需有 unique_code 属性)
        visit_fn: 单次访问函数 (challenge, attempt, variant) -> result
        is_success: 判断结果是否成功的函数
        max_concurrent: 最大并发数
        best_of: 每题最多尝试次数
    """
    results: dict = {}
    lock = threading.Lock()

    def _execute(ch):
        code = ch.unique_code
        for attempt in range(best_of):
            try:
                result = visit_fn(ch, attempt, 0)
                with lock:
                    results[code] = {"result": result, "attempt": attempt}
                if is_success and is_success(result):
                    break
            except Exception as e:
                log.error("fleet execution error on %s attempt %d: %s", code, attempt, e)
                with lock:
                    results[code] = {"result": {"error": str(e)}, "attempt": attempt}

    with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
        futures = {pool.submit(_execute, ch): ch for ch in challenges}
        for future in as_completed(futures):
            ch = futures[future]
            try:
                future.result()
            except Exception as e:
                log.error("fleet thread error on %s: %s", ch.unique_code, e)

    return results
