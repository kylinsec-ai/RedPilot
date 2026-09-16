"""求解引擎桥接 —— 朋友的 Pi Agent 实现接到框架的 AgentAdapter 接口上。

## 为什么需要这一层

朋友的引擎（`ghost_worker.adapter.solver.pi_agent.PiAgentBackend`，1,340 行）
比框架原来那个（`solver/pi_agent.py`，517 行）强得多：四个看门狗（stall /
deadline / stop_check / 子 agent 静默）、令牌式进程树回收、subagent + skills
安装器、按题 `.pi-home` 隔离、provider 配置落地、续接块回捞。

但它与框架的接口有三处硬差异，直接顶上会让**回归测试锁定的那条链**失效：

| | 框架 AgentAdapter | 朋友的 SolverBackend |
|---|---|---|
| 流式观测 | `on_event(kind, payload)` | 无 |
| 会话中断 | 靠 `asyncio.cancel` + 外部杀进程 | `stop_check()` 轮询 |
| 配置对象 | `SolverConfig`(2 字段) | `adapter.config.SolverConfig`(12 字段) |

外加：朋友的 `env["HOME"]` 是调用方注入的（它照抄 `os.environ`），框架侧没有这层；
`provider_failure`（`turns==0 and error`）是 `solve_one` 判 provider 故障的**唯一**
判据，朋友的结果模型里没有。

这一层只做翻译，不含策略。所有判定口径都来自朋友引擎，不另立一套。
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from .base import AgentAdapter, SolveResult

log = logging.getLogger("ghost_worker.solver.pi")

# 非阻塞钩子单次预算（秒）。钩子由 driver 注入（LiveReporter.set），只做
# 节流写快照 + 总线广播，正常远低于这个量级；给足余量只是防御。
_HOOK_BUDGET_S = 2.0


class _Watchdog:
    """把非阻塞钩子的**挂钟耗时**折算成子进程的剩余编辑预算。

    朋友的引擎在每次 select 唤醒时拿 `proc.poll()` 包 5s 超时，而 `poll()`
    **不消费 stdout**：钩子慢多久，引擎眼里的会话就少多久 —— 期间没有 stall
    判定，因为 stall 时钟在没有事件时才走。更糟的是 deadline 是挂钟的，
    所以钩子是在**偷**会话预算。

    正常情况照不到这里（钩子毫秒级）。但一旦照到，就会表现为"会话莫名提前
    结束"，而朋友那套默认 480s stall / 整段会话预算的设计里没有这个假设。
    这里显式记账：预算超过一半就响亮降级为不再调用钩子，宁可丢 live 更新，
    也不静默缩短 agent 的解题时间。
    """

    __slots__ = ("_budget", "_spent", "_disabled", "_kind")

    def __init__(self, budget: float = _HOOK_BUDGET_S) -> None:
        self._budget = budget
        self._spent = 0.0
        self._disabled = False
        self._kind = ""

    def charge(self, seconds: float, kind: str) -> None:
        self._spent += seconds
        if not self._disabled and self._spent > self._budget / 2:
            self._disabled = True
            self._kind = kind
            log.warning(
                "on_event/on_fact 钩子累计耗时 %.1fs 超过预算 %.1fs（最近一次: %s）"
                "—— 改为只发一条降级告警后停用钩子。live 面板会停在最后一次更新，"
                "但 Pi 会话的挂钟预算不再被钩子吃掉。",
                self._spent, self._budget, kind)

    @property
    def tripped(self) -> bool:
        return self._disabled

    def notice(self) -> str:
        return f"hooks_disabled:{self._kind}"


class FriendSolver(AgentAdapter):
    """朋友的 PiAgentBackend，经本层翻译后暴露为框架的 AgentAdapter。"""

    name = "pi-friend"

    def __init__(self, *, model: str = "", skills_dir: str = "",
                 max_turns: int = 60, thinking: str = "") -> None:
        from ..adapter.solver.pi_agent import PiAgentBackend
        self._backend = PiAgentBackend(model=model, skills_dir=skills_dir,
                                       max_turns=max_turns, thinking=thinking)
        self.name = f"pi-friend({self._backend.model or 'default'})"

    # ── 逐题实例令牌：进程回收要它才生效 ─────────────────────
    @staticmethod
    def stamp_instance(workdir: str, token: str) -> None:
        """把本次访问的令牌写进 `<workdir>/_instance.json`。

        朋友的引擎按这个文件取令牌、注入 pi 及其全部子孙的环境变量，收尾时扫
        `/proc/*/environ` 按令牌回收 —— 因此**驱动崩溃后脱组的 nohup/setsid
        子孙也收得回来**，而且按构造不会误伤 driver / VPN provider / 其他 worker。

        框架的编排链路原本不写这个文件，所以 `cleanup_instance_processes` 在容器
        里是一句空操作（安全，但等于没有回收）。由调用方（`solve_one`）在会话开始
        时盖一个随机令牌，会话收尾或租约丢失时回收。
        """
        import json
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "_instance.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"trace_scope": token}, fh, ensure_ascii=False)


    # ── 接口：AgentAdapter.solve ─────────────────────────────
    def solve(
        self,
        prompt: str,
        workdir: str,
        cfg,
        *,
        on_fact: Optional[Callable] = None,
        transcript_path: Optional[str] = None,
        max_retries: int = 2,
        on_event: Optional[Callable] = None,
    ) -> SolveResult:
        watchdog = _Watchdog()

        def _timed(kind: str, fn: Callable, *args, **kwargs):
            if watchdog.tripped:
                return None
            import time as _t
            t0 = _t.monotonic()
            try:
                return fn(*args, **kwargs)
            except Exception:
                log.debug("hook %s raised (ignored)", kind, exc_info=True)
                return None
            finally:
                watchdog.charge(_t.monotonic() - t0, kind)

        def _fact(tool_name, args, out) -> None:
            if on_fact is not None:
                _timed("on_fact", on_fact, tool_name, args, out)

        def _event(kind: str, payload: dict) -> None:
            if on_event is not None:
                _timed("on_event", on_event, kind, payload or {})

        def _stop() -> bool:
            """框架侧唯一的中断信号：钩子降级（见 _Watchdog）。"""
            return watchdog.tripped

        cfg = adopt_config(cfg)

        # 逐题 HOME 隔离（<workdir>/.pi-home）、令牌式进程回收、provider 配置落地
        # 都在朋友引擎内部完成，本层不重复。
        raw = self._backend.solve(
            prompt, workdir, cfg,
            on_fact=_fact,
            transcript_path=transcript_path,
            max_retries=max_retries,
            stop_check=_stop,
            on_event=_event,
        )

        result = SolveResult(
            flags=list(raw.flags),
            tool_outputs=list(raw.tool_outputs),
            observed_output=raw.observed_output,
            error=raw.error,
            turns=raw.turns,
            duration_s=raw.duration_s,
            infra_blocked=raw.infra_blocked,
            # 朋友独有、框架编排层暂时不消费，但保留在结果里以便过渡期排查
            # （handoff 目前全仓无消费者 —— 朋友自己也有这条注释）。
            final_answer=raw.final_answer,
            final_text=raw.final_text,
            handoff=raw.handoff,
            termination_reason=raw.termination_reason,
            target_fault=raw.target_fault,
        )
        # 钩子降级后 live 面板会冻结，必须让它显示得出原因（否则"面板不动"
        # 与"worker 卡住"在观测上无法区分）。
        if watchdog.tripped and on_event is not None:
            _raw_event(on_event, watchdog.notice())
        return result


def _raw_event(on_event: Callable, kind: str) -> None:
    """降级告警专用：绕过 watchdog，直接发一条 system 事件。"""
    try:
        on_event(kind, {"phase": "stderr",
                        "detail": "live 钩子超预算已停用（见 worker 日志 warning）"})
    except Exception:
        log.debug("degradation notice failed", exc_info=True)


# ── 配置适配 ────────────────────────────────────────────────

def adopt_config(cfg):
    """把框架的 SolverConfig 升级成朋友引擎认的 12 字段版本。

    朋友的 `solve()` 会读 `solver_cfg.api_key` / `.base_url` / `.max_turns` /
    `.session_seconds` 并据此写 `$HOME/.pi/agent/models.json`（provider 端点 + key）。
    只给 `model`/`session_seconds` 两个字段的话，models.json 会写出空的 baseUrl —
    pi 回退官方端点 → key 401 / 0 turns（朋友引擎的注释里记着这条）。

    值优先级：朋友 `adapter.config.SolverConfig.from_env()` 的原口径（ANTHROPIC_*
    > SOLVER_* > 预设）—— 不新造一套读法，只是让框架起点的 cfg 能参与合并。
    """
    from ..adapter.config import SolverConfig as AdapterConfig
    adapter_cfg = AdapterConfig.from_env()
    # 框架侧显式给的 model/session_seconds 覆盖 env 口径：调用方传进来的参数
    # 应当比环境变量更权威（driver 从 settings 构造，测试直接构造）。
    model = getattr(cfg, "model", "") or adapter_cfg.model
    session_seconds = int(getattr(cfg, "session_seconds", 0) or 0) or adapter_cfg.session_seconds
    return _replace(adapter_cfg, model=model, session_seconds=session_seconds)


def _replace(obj, **changes):
    import dataclasses
    return dataclasses.replace(obj, **changes)
