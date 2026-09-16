"""观测桥 —— 把竞技场主循环的状态翻译成 LiveState/LiveBus 事件。

## 为什么需要这一层

编排主循环（`ghost_worker.orchestrator`，朋友那份 6,975 行竞技场）有自己的
17 字段状态模型，落在 `work/status/worker-<N>.json`：

    current_code / current_difficulty / current_round / sessions / session_active /
    session_started_at / solving_active / last_activity / flags_found(list) /
    flags_submitted / total_earned / challenges_solved / last_event / last_log / lastbeat

框架的观测面吃的是 `LiveState` 的 18 键快照（`ghost_contracts.snapshot`），
经 `ghost_worker.relay` 推到 obs 平台、经 `ghost.obs.localserver` 喂 :8080 态势台。

**两层字段名几乎全不一样**（`current_code` vs `challenge_code`、`solving_active`
vs `phase`、`session_started_at` vs `started_at`…），所以需要一层显式映射。

## 三条不变量

1. **只读**：本模块只读 status dict，从不回写。status 文件仍是 supervisor 与
   只读控制台的数据源，观测面是它的**下游**，不是替代。
2. **绝不抛**：`push()` 里的任何异常都被吞掉（`orchestrator._update_status`
   还会再包一层）。观测面坏掉不能让解题停摆。
3. **未知字段不丢**：映射表没覆盖的键进 `_extra`（下划线开头），只走总线信封、
   不进 LiveState 快照 —— 与 `LiveReporter` 的既有约定一致（relay 不落库下划线键）。

## 一个刻意的不对称

`flush` 只在**状态语义发生跳变**时触发（换题 / 会话开停 / 解出），而不是每次
`push`。`_update_status` 是由 `_beat()` 每 30s 调一次的热路径，每次都强制落盘
会把 LiveState 的 1s 节流彻底废掉。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ghost_contracts.redact import summarize_args
from ghost_contracts.text import ERROR_HEAD_MAX, OUTPUT_TAIL_MAX, head_text, tail_text

log = logging.getLogger("ghost_worker.observability")

# `_extra` 里允许透传的 status 键（其余一概不进总线信封）。
# 这份清单刻意短：只带运维真正会看的进度量，避免把内部簿记刷到 obs 里。
_PASSTHROUGH_KEYS = (
    "current_difficulty",
    "current_round",
    "session_active",
    "sessions",
    "flags_submitted",
    "total_earned",
    "challenges_solved",
    "last_event",
)


def _phase_of(status: Mapping[str, Any]) -> str:
    """status → LiveState.phase。

    口径尽量与框架原 `orchestration.LiveReporter` 的 set 点对齐（starting /
    solving / submitting / done / closing / idle），这样前端与 relay 的状态机
    不需要认识新词汇。
    """
    event = str(status.get("last_event") or "")
    if event.startswith("session start"):
        return "solving"
    if status.get("session_active"):
        # 会话在跑但还没有会话内事件 → 仍在启动/推进阶段
        return "solving"
    if status.get("solving_active"):
        # visit 在跑但没有活跃会话：多会话之间的间隙（收尾/复盘/重启靶场）
        return "closing"
    return "idle"


class StatusBridge:
    """`orchestrator._update_status` 的下游。注入经 `orchestrator.set_status_bridge`。

    参数就是框架观测面既有的两个对象：
      - `live`: `ghost_worker.live.LiveState`（None = 只发总线不发快照）
      - `bus`:  `ghost_worker.live.LiveBus`  （None = 不广播）
      - `relay`: `ghost_worker.relay.ObsRelay`（可选；用于在解出时把当前 run
        未读字节排干后收尾）
    """

    def __init__(self, live=None, bus=None, *, relay=None, worker_id: str = "") -> None:
        self._live = live
        self._bus = bus
        self._relay = relay
        self._worker_id = worker_id
        # 上一次的快照语义签名：phase|code|sessions|flags_submitted，跳变才 flush
        self._sig: tuple | None = None
        # 会话内的 tool 名 → 供 current_tool/current_args_summary（status 不带）
        self._last_session: str = ""
        self._swallowed = 0

    # ── 主入口 ───────────────────────────────────────────────
    def push(self, status: Mapping[str, Any]) -> None:
        """接收一份 status 快照。异常一律吞掉（观测面不得拖挂解题）。"""
        try:
            self._push(status)
        except Exception:
            self._swallowed += 1
            # 只打前几次：热路径上同一种异常会刷满日志
            if self._swallowed <= 3:
                log.debug("status bridge push failed (swallowed)", exc_info=True)

    # ── 会话内工具钩子（由编排层在 on_fact 上调用）────────────
    def tool_start(self, tool: str, args) -> None:
        """一次工具调用开始：刷新面板的 current_tool/current_args_summary。

        竞技场主循环的 status 里**没有**工具粒度（它只有会话粒度），而面板要的
        是"此刻在跑什么"。这里由编排层在 `on_fact` 上补一笔。
        """
        try:
            self._last_session = tool or ""
            if self._live is not None:
                self._live.update(
                    phase="solving",
                    current_tool=tool or "",
                    current_args_summary=summarize_args(args or {}),
                    last_tool=tool or "",
                )
        except Exception:
            self._swallowed += 1

    def tool_output(self, out: str) -> None:
        """一次工具调用结束：把输出尾巴贴到面板（与框架侧 tool_end 同款口径）。"""
        try:
            if self._live is not None:
                self._live.update(
                    last_output_tail=tail_text(out or "", OUTPUT_TAIL_MAX),
                    current_tool="",
                )
                self._live.flush()
        except Exception:
            self._swallowed += 1

    def flags_submitted(self, flags: list[str]) -> None:
        """平台确认了新的 flag：把明文带外送给 obs（下划线键不进快照）。

        与框架 `solve_one` 收尾帧的 `_accepted_flags` 同一条通道 —— 读端靠它把
        "本 run 真正入账的明文"补进 Runs 历史。
        """
        if self._relay is None:
            return
        try:
            self._relay.send_accepted_flags(list(flags))
        except Exception:
            log.debug("accepted-flags send failed (swallowed)", exc_info=True)

    # ── 映射 ─────────────────────────────────────────────────
    def _push(self, status: Mapping[str, Any]) -> None:
        code = str(status.get("current_code") or "")
        phase = _phase_of(status)
        sessions = int(status.get("sessions") or 0)
        flags_found = status.get("flags_found") or []
        session_active = bool(status.get("session_active"))

        # 会话起点：status 里只有 session_started_at；转成 LiveState 的 started_at
        # 只在**换题**时取，否则 elapsed_s 会随每场会话重置（那是 session 的
        # 时长，不是本题的）。
        started_at = float(status.get("started_at") or 0.0)
        session_started = float(status.get("session_started_at") or 0.0)

        fields: dict[str, Any] = {
            "challenge_code": code,
            "phase": phase,
            "flags_found": len(flags_found),
            "turns": sessions,
            "accepted": int(status.get("challenges_solved") or 0),
            "error": self._error_of(status, session_active),
        }
        if self._worker_id:
            fields["worker_id"] = self._worker_id
        if code and session_started and session_active:
            fields["current_args_summary"] = ""
        if not session_active and self._last_session:
            # 会话收尾：清掉工具态，避免面板停在上一场的最后一个工具上
            fields["current_tool"] = ""
            self._last_session = ""
        if started_at:
            fields["started_at"] = started_at
        # last_log 是竞技场自己的短状态文案；面板的 assistant_preview 是它的
        # 最近邻（transcript 文本流另由 relay 从字节续读补上）
        last_log = str(status.get("last_event") or "")
        if last_log:
            fields["last_tool"] = head_text(last_log, OUTPUT_TAIL_MAX)
        if phase == "idle":
            fields["transcript_path"] = ""

        if self._live is not None:
            self._live.update(**fields)
        self._publish(phase, code, status, fields)
        self._maybe_flush(phase, code, sessions, int(status.get("flags_submitted") or 0))

    @staticmethod
    def _error_of(status: Mapping[str, Any], session_active: bool) -> str:
        """status 没有 error 字段；从 last_log 里挑出看起来是故障的那条。

        竞技场把失败文案写进 `last_log`（`_log_line` 同时落日志文件）。这里只做
        **展示用**的粗筛 —— 真正的故障判定在编排层，这里多报一条不影响任何决策。
        """
        if session_active:
            return ""
        text = str(status.get("last_log") or "")
        for marker in ("error", "failed", "ERROR", "FAILED", "traceback"):
            if marker in text:
                return head_text(text, ERROR_HEAD_MAX)
        return ""

    def _publish(self, phase: str, code: str, status: Mapping[str, Any],
                 fields: Mapping[str, Any]) -> None:
        if self._bus is None or not self._bus.has_subscribers():
            return
        snap = dict(self._live.snapshot()) if self._live is not None else dict(fields)
        payload = {**snap, "kind": "lifecycle"}
        extra = {k: status[k] for k in _PASSTHROUGH_KEYS if k in status}
        for k, v in extra.items():
            payload.setdefault(f"_{k}", v)
        payload["_phase"] = phase
        payload["_code"] = code
        self._bus.publish(payload)

    def _maybe_flush(self, phase: str, code: str, sessions: int, submitted: int) -> None:
        sig = (phase, code, sessions, submitted)
        changed = self._sig is not None and sig != self._sig
        self._sig = sig
        if self._live is not None and (changed or phase in ("done", "error")):
            self._live.flush()
