"""观测桥 —— 把竞技场主循环的状态翻译成 LiveState/LiveBus 事件。

## 为什么需要这一层

编排主循环（`redpilot_worker.orchestrator`，朋友那份 6,975 行竞技场）有自己的
状态模型，落在 `work/status/worker-<N>.json`：

    current_code / current_difficulty / current_round / sessions / session_active /
    session_started_at / solving_active / last_activity / flags_found(list) /
    flags_submitted / total_earned / challenges_solved / last_event / last_log /
    lastbeat / phase

框架的观测面吃的是 `LiveState` 的 18 键快照（`redpilot_contracts.snapshot`），
经 `redpilot_worker.relay` 推到 obs 平台、经 `redpilot.obs.localserver` 喂 :8080 态势台。

**两层字段名几乎全不一样**（`current_code` vs `challenge_code`、`solving_active`
vs `phase`、`session_started_at` vs `started_at`…），所以需要一层显式映射。

## phase 由编排层给，不在这里推导

`orchestrator._update_status` 的调用点知道此刻处于哪个阶段，它们写 `phase=`
（取值就是 `redpilot_contracts.vocabulary.PHASES` 的词表）。本模块只做**改名**，
不做**推导** —— 这一条是刻意的：

relay 的 run 状态机（`relay._on_frame`）靠 `starting` 与 `solving` 的区别决定
"开一个新 run"还是"锚定当前 run 的 transcript 起点"，而那两个字在 status 的
两个布尔里表达不出来。此前这里用 `last_event.startswith("session start")` 之类的
字符串嗅探去猜，结果是 `phase` 永远取不到 `starting` → **run 从不开启** →
transcript 有路径却没处挂（`f942d9f` 补的那个字段等于白补），全程不报错。
猜一个状态机出来是错的深度：状态机的主人（编排层）本来就知道答案。

`phase` 缺失时（直调 orchestrator 的老路径、测试夹具）才回落到从两个布尔推导 ——
口径略粗（会话开始与推进都归 `solving`，好在那些路径本来也没有 relay）。

## 三条不变量

1. **只读**：本模块只读 status dict，从不回写。status 文件仍是 supervisor 与
   只读控制台的数据源，观测面是它的**下游**，不是替代。
2. **绝不抛**：`push()` 里的任何异常都被吞掉（`orchestrator._update_status`
   还会再包一层）。观测面坏掉不能让解题停摆。
3. **快照外的进度量另走带外键**：映射表没覆盖、但运维会看的那些键（`_PASSTHROUGH_KEYS`）
   以下划线前缀进总线信封，不进 LiveState 快照 —— 与既有约定一致（relay 不落库
   下划线键）。清单是手工的，`test_status_bridge.PassthroughKeyDriftTests` 守着它。

## 落盘节拍：两个量级

- **状态快照**（`push`）：只在**语义跳变**时强制落盘（换题 / 会话开停 / 解出），
  其余交给 `LiveState` 自己的 1s 节流（`live/state.py` 的 `SAVE_MIN_INTERVAL`）。
  `push` 由 `_update_status` 驱动，而那是个每轮工具调用都走的热路径
  （`_on_fact` → `_update_status`）—— 每次都强制落盘等于把那个节流删掉。
- **工具边界**（`tool_call`）：每次强制落盘一次。它是 `FLUSH_KINDS` 的一员，
  也是 `/work/.live` 上读者唯一能看到的"这一场跑到哪了"的证据；按工具调用计费
  （一场会话数百次），而不是按 push（更多）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from redpilot_contracts.redact import summarize_args
from redpilot_contracts.text import ERROR_HEAD_MAX, OUTPUT_TAIL_MAX, tail_text
from redpilot_contracts.vocabulary import OUT_OF_BAND_PREFIX, PHASES

log = logging.getLogger("redpilot_worker.observability")

# `_extra` 里允许透传的 status 键（其余一概不进总线信封）。
# 这份清单刻意短：只带运维真正会看的进度量，避免把内部簿记刷到 obs 里。
# ⚠️ 手工维护，且**不会**因为 orchestrator 加了新 status 字段而自动跟上 ——
# 新字段默认被丢弃（沉默）。加 status 字段时顺手判一句要不要进这里。
# （端到端测试会在真编排层跑通时暴露转义问题，但不会暴露这个漏项。）
# 注意：只列**快照里没有对应列**的键。有对应列的（`sessions` → `turns`、
# `transcript_path` → 快照的 `transcript_path`）不再抄一份下划线副本 ——
# 一个事实在同一帧里出两次名，读者还得判断信哪个。
_PASSTHROUGH_KEYS = (
    "current_difficulty",
    "current_round",
    "session_active",
    "flags_submitted",
    "total_earned",
    "challenges_solved",
    "last_event",
)

# 兜底推导的取值必须落在契约词表内 —— 否则 relay/store 会把它当"非活跃阶段"
# 静默忽略（一个拼错的 phase 不会报错，只会让面板永远不动）。
assert set(("solving", "closing", "idle")) <= set(PHASES)


def _phase_of(status: Mapping[str, Any]) -> str:
    """status → LiveState.phase。

    编排层直接给的 `phase` 是唯一权威（见模块头）。只有它缺失时才从两个布尔
    兜底推导 —— 那条路径上没有 relay，粗一点无妨。
    """
    phase = str(status.get("phase") or "")
    if phase in PHASES:
        return phase
    # ── 兜底：从布尔推导（口径见模块头）──
    if status.get("session_active"):
        # 会话在跑：区分不出"刚开始"与"在推进"，都算 solving
        return "solving"
    if status.get("solving_active"):
        # visit 在跑但没有活跃会话：多会话之间的间隙（收尾/复盘/重启靶场）
        return "closing"
    return "idle"


class StatusBridge:
    """`orchestrator._update_status` 的下游。注入经 `orchestrator.set_status_bridge`。

    参数就是框架观测面既有的三个对象：
      - `live`: `redpilot_worker.live.LiveState`（None = 只发总线不发快照）
      - `bus`:  `redpilot_worker.live.LiveBus`  （None = 不广播）
      - `relay`: `redpilot_worker.relay.ObsRelay`（可选；用于把平台确认的明文 flag
        带外送进 Runs 历史）
    """

    __slots__ = ("_live", "_bus", "_relay", "_worker_id", "_sig", "_swallowed")

    def __init__(self, live=None, bus=None, *, relay=None, worker_id: str = "") -> None:
        self._live = live
        self._bus = bus
        self._relay = relay
        self._worker_id = worker_id
        # 上一次的快照语义签名：phase|code|sessions|flags_submitted，跳变才 flush
        self._sig: tuple | None = None
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
    def tool_call(self, tool: str, args, output: str) -> None:
        """一次工具调用**结束**：把工具名/参数摘要/输出尾巴一起写进面板。

        竞技场主循环的 status 里**没有**工具粒度（它只有会话粒度），而面板要的
        是"此刻在跑什么"。这里由编排层在 `on_fact` 上补一笔。

        刻意只留**一个**钩子而不是 start/end 两个：`on_fact` 是同一个回调里
        背靠背调用的（`orchestrator._on_fact`），两个钩子写的是同一次更新的
        前后两半，中间态没有任何观察者能看见（文件读者、SSE 订阅者都是下一次
        节拍才读），却要多付一次加锁拷贝与一次落盘尝试。合并成一次更新后，
        `summarize_args` 也只走一条路径。
        """
        try:
            summary = summarize_args(args or {})
            tail = tail_text(output or "", OUTPUT_TAIL_MAX)
            if self._live is not None:
                self._live.update(
                    phase="solving",
                    current_tool="",        # 工具**已结束**：面板不留"正在跑"
                    current_args_summary=summary,
                    last_tool=tool or "",
                    last_output_tail=tail,
                )
                # 工具边界是 FLUSH_KINDS 的一员（contracts.vocabulary）：这一帧
                # 必须落盘，否则 /work/.live 上的读者会停在上一场的尾部。
                #
                # 代价说明白：`update()` 自己那次 save 已被 1s 节流吃掉，所以
                # 这里每次工具调用**正好一个**强制写（不是两个）。反之，若改成
                # 不过滤就落盘，`on_fact` 的调用频率会让这个 bind-mount 上的
                # JSON 重写从"每场数百次"涨到"每次 delta 一次"。
                self._live.flush()
        except Exception:
            self._swallowed += 1

    def flags_submitted(self, flags: list[str]) -> None:
        """平台确认了新的 flag：把明文带外送给 obs（下划线键不进快照）。

        与收尾帧附带的 `_accepted_flags` 同一条通道 —— 读端靠它把"本 run 真正
        入账的明文"补进 Runs 历史。**eager 提交**（会话进行中就投递）尤其依赖它：
        那时 run 还没收尾，收尾帧要等整场结束才到。

        调用点在编排层的三处提交成功分支（eager / 常规 / force），每次送**单个**
        刚入账的明文，不是累计列表 —— 累计会让读端反复重写同一批（幂等但费流量）。
        """
        if not flags:
            return
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

        # started_at 是**进程**启动戳（orchestrator 在 import 期写一次，从不更新），
        # 所以 LiveState 的 elapsed_s 语义是"本进程跑了多久"，不是本题跑了多久。
        # 每题的时长看 `current_round` / `sessions`。
        started_at = float(status.get("started_at") or 0.0)

        fields: dict[str, Any] = {
            "challenge_code": code,
            "phase": phase,
            "flags_found": len(flags_found),
            "turns": sessions,
            "accepted": int(status.get("challenges_solved") or 0),
            "error": str(status.get("error") or "")[:ERROR_HEAD_MAX],
        }
        if self._worker_id:
            fields["worker_id"] = self._worker_id
        if started_at:
            fields["started_at"] = started_at
        # 收尾/空闲时清掉实录路径，避免面板停在上一题的记录上。
        # （工具态不用清：`tool_call` 每次结束都把 current_tool 置空。）
        if phase in ("idle", "done", "closing"):
            fields["transcript_path"] = ""
        else:
            tpath = str(status.get("transcript_path") or "")
            if tpath:
                fields["transcript_path"] = tpath

        # `update()` 返回它自己那份私有快照（state.py:57 已 copy），直接用；
        # 没有再 `snapshot()` 一次（那是第二次加锁 + 第二次拷贝）。
        snap = self._live.update(**fields) if self._live is not None else dict(fields)
        self._publish(status, snap)
        self._maybe_flush(phase, code, sessions, int(status.get("flags_submitted") or 0))

    def _publish(self, status: Mapping[str, Any], snap: Mapping[str, Any]) -> None:
        """广播一帧总线信封：纯快照 + kind + `_` 前缀带外元数据。

        闸门在**拷贝之前**：没有订阅者时（`OBSERVABILITY_URL`/`STATUS_PORT` 都没配）
        就不必造信封。注意 relay 一起来就会常驻订阅，所以生产环境这条早退基本
        不生效 —— 它挡的是裸跑 driver 的场景。
        """
        if self._bus is None or not self._bus.has_subscribers():
            return
        payload: dict[str, Any] = dict(snap)
        payload["kind"] = "lifecycle"
        for k in _PASSTHROUGH_KEYS:
            if k in status:
                payload[OUT_OF_BAND_PREFIX + k] = status[k]
        self._bus.publish(payload)

    def _maybe_flush(self, phase: str, code: str, sessions: int, submitted: int) -> None:
        """只在语义跳变时强制落盘（见模块头的"落盘节拍：两个量级"）。

        `done` 必须无条件落盘：relay 的 run 收尾读的就是它（`relay._on_frame` 的
        closing/idle 分支），而签名里的四个量在收尾帧上未必都变。`error` 留在
        条件里是**防御**：编排层目前不产出这个 phase，但 relay 的关闭分支认得它
        —— 将来加一个失败态时，忘记同步这里会让终态帧被节流失效吞掉。
        """
        sig = (phase, code, sessions, submitted)
        changed = self._sig is not None and sig != self._sig
        self._sig = sig
        if self._live is None:
            return
        # done/error 无论签名如何都强制落盘：终态帧是 relay 的收尾依据
        if changed or phase in ("done", "error"):
            self._live.flush()
