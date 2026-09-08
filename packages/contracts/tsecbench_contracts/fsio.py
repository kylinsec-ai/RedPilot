"""文件系统原子操作 — ensure_dir / atomic_write_json(单一来源)。

此前 adapter/live/state.py 持实现、drivers/roster.py 惰性转调;归拢到此。
"""

from __future__ import annotations

import json
import os
import threading
import time


def ensure_dir(path: str) -> None:
    """dirname 存在则建目录；空 dirname 不做事（os.makedirs('') 会抛错）"""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
    except Exception:
        pass


def atomic_write_json(path: str, obj) -> bool:
    """tmp + os.replace 原子写。成功返回 True。

    tmp 名带线程 + 纳秒(同进程并发 save 不互踩);失败返回 False 由调用方
    决定重试/记录 —— 静默吞掉会让节流窗口冻结而无任何线索。
    """
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False
