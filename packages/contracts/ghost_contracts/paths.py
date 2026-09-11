"""路径与文件名约定 — 进程间文件契约的单一来源。

safe_code 此前在 drivers/roster.py(_safe_code,driver/status_server 惰性取);
/work 布局常量此前散在 driver/roster/status_server 的字符串字面量里;
HEARTBEAT_PATH 此前在 adapter/solver/base.py,同时被 compose healthcheck 以
字面量读取 —— 改动此处须同步 docker-compose.yaml。
"""

from __future__ import annotations

import hashlib
import re

# ── FLAG 候选文件读法(relay 兜底与 challenge_detail 共用) ──

FLAG_FILES: tuple[str, ...] = ("FLAG", "flag.txt", "FLAG.txt")
FLAG_MAX_LINES = 50


def safe_code(code: str) -> str:
    """code -> 安全目录名(sanitize + sha1 hash 后缀)。全仓唯一实现:
    驱动/仪表板/中继都从这里取(纯 stdlib,不触发 SDK 导入)。
    产出必须与 scan_local 的可逆判定(_dir_maps_to_code)一致。"""
    raw = str(code)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
    return safe if safe == raw else f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


# ── /work 布局 ──

LIVE_DIR = ".live"                    # workdir 下实时状态目录
LIVE_STATE_FMT = "{worker_id}.json"    # .live/<worker_id>.json
ROSTER_FILENAME = "roster.json"        # .live/roster.json
DIGESTS_DIR = "digests"                # .live/digests/<code>.json
TRANSCRIPT_FILENAME = "transcript.jsonl"  # <workdir>/<safe_code>/transcript.jsonl

# 心跳文件(compose healthcheck 以同一字面量读取 —— 两处必须同步改)
HEARTBEAT_PATH = "/tmp/driver_heartbeat"
