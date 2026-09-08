"""共享词汇表 — phase / run 状态 / 事件 kind / 信封约定 / SSE 协议常量。

worker(驱动与中继)与 obs(摄取/读端/仪表板协议)的共同取值集合:
此前分散在 benchmark_driver/_FLUSH_KINDS、status_server、obs_relay 状态机、
obs/schema.py 与前端 types.ts(手工镜像)等多处;此处为唯一权威源。
前端 types.ts 仍为手工镜像(本期不接包),改动此处须同步 types.ts。
"""

from __future__ import annotations

import re
from typing import Literal

# ── live 快照 phase(worker LiveState 更新用;obs 崩溃守卫/active 判定) ──

PHASES: tuple[str, ...] = ("starting", "solving", "submitting", "closing", "done", "idle", "error")
Phase = Literal[*PHASES]
# 进行中 phase 集合(崩溃守卫与 active-live 判定;'closing' 在内 —— 收尾帧也算活跃)
ACTIVE_PHASES: tuple[str, ...] = ("starting", "solving", "submitting", "closing")

# ── runs.status(obs 平台侧;SQL CHECK 与 /api/runs 过滤共用) ──

RUN_STATUSES: tuple[str, ...] = ("running", "solved", "done", "failed", "interrupted")
# run_close 可写的终态(其余状态只读)
RUN_CLOSE_STATUSES: tuple[str, ...] = ("solved", "done", "failed")
# 可被 close 写入的 run 状态(终态幂等)
CLOSABLE_STATUSES: tuple[str, ...] = ("running", "interrupted")

# ── run_id 形态:uuid4().hex ──

RUN_ID_RX = re.compile(r"^[0-9a-f]{32}$")

# ── solver → driver 回调事件 kind(pi_agent._emit 的规范化事件) ──

LIVE_EVENT_KINDS: tuple[str, ...] = (
    "tool_start", "tool_progress", "text", "thinking",
    "turn_done", "error", "system",
)
# 边界事件:立即落地快照文件(高频 progress/text 只走内存+SSE)
FLUSH_KINDS: frozenset[str] = frozenset({"tool_end", "turn_done", "error", "system", "lifecycle"})

# ── pi 原生 transcript 事件类型(中继过滤/压缩/两处 digest 折叠共用) ──

MESSAGE_UPDATE = "message_update"
TOOL_EXECUTION_START = "tool_execution_start"
TOOL_EXECUTION_END = "tool_execution_end"
TOOL_EXECUTION_UPDATE = "tool_execution_update"
SESSION = "session"
AGENT_START = "agent_start"
AGENT_END = "agent_end"
TURN_START = "turn_start"
TURN_END = "turn_end"
ATTEMPT = "_attempt"
ERROR = "error"

# ── SSE 协议(两服务字节兼容的公共部分) ──

SNAPSHOT_KIND = "snapshot"          # 连接后首帧的 kind
SSE_HEARTBEAT_S = 15                # 心跳注释帧间隔(秒)

# ── 总线信封约定 ──
# 总线帧 = LiveState 纯快照 + kind(+ts)信封 + 下划线前缀带外元数据。
# kind/ts 只在通道内使用,不入库(obs LiveIn 契约 snapshot = 纯快照);
# `_` 前缀键(如 closing 帧附带的 _accepted_flags 明文)只走 relay→平台链路,
# 绝不向仪表板广播、不落快照。

ENVELOPE_KEYS: frozenset[str] = frozenset({"kind", "ts"})
OUT_OF_BAND_PREFIX = "_"


def strip_out_of_band(frame: dict) -> dict:
    """SSE 出口用:剥掉 `_` 前缀带外元数据,保留 kind/ts 信封(前端按 kind 区分首帧)。"""
    return {k: v for k, v in frame.items()
            if not (isinstance(k, str) and k.startswith(OUT_OF_BAND_PREFIX))}


def strip_for_snapshot(frame: dict) -> dict:
    """中继落库用:剥掉信封键(kind/ts)与 `_` 前缀带外元数据,只留 LiveState 状态键。"""
    return {k: v for k, v in frame.items()
            if k not in ENVELOPE_KEYS
            and not (isinstance(k, str) and k.startswith(OUT_OF_BAND_PREFIX))}
