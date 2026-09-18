"""求解引擎契约 — 竞技场主循环与 Pi 引擎之间的那层薄接口。

## 这一层还剩什么

只剩 `touch_heartbeat()`：compose healthcheck 读的那个心跳文件
（路径单源在 `redpilot.contracts.paths.HEARTBEAT_PATH`）。

竞技场主循环（`redpilot.worker.orchestrator`）用**朋友自己的** `SolverBackend`
接口调引擎（`flag_format=` / `on_fact=` / `stop_check=` / `transcript_path=`），
引擎侧的结果模型是 `redpilot.worker.adapter.solver.SolveResult` —— 与这里曾经那个
同名类**不是同一个**。编排层 import 的也是 adapter 那个（`orchestrator.py:57`）。

## 已删除：框架侧 `SolveResult`（2026-09 死码清扫）

它曾带着一个 `provider_failure` 属性（0-turn + 报错 = 引擎压根没跑起来）。
删它的判据是**零消费者**，三条都实测过：

- 没有任何生产代码构造它 —— 全仓 `SolveResult(` 只命中 adapter 那个类；
- 唯一 import 方是 façade 再导出与 `tests/worker/test_provider_failure_guard.py`，
  而**那个测试自己写明了**：判据由编排层自建，属性不在朋友引擎的结果模型里；
- `orchestrator.py` 的 `_is_api_fault` docstring 曾引用 `engine_solver.SolveResult
  .provider_failure`，而 `engine_solver` 这个符号在整个文件里**只出现在那行注释里**。

## 留下的教训（别把这条判据一起删掉）

2026-09-08 事故：pi 对 provider 400 只发 `stopReason=error`，漏读 → `err=none`
→ 编排层当成"正常跑完没解出来" → **280 run / 0 flag / 63 题静默烧库**。

"0-turn + 报错 = 引擎没跑起来"这条判据**仍然成立**，只是它的执行点不在这个文件里，
而在编排侧：`orchestrator._is_api_fault`（认报错文本里的余额/认证 token，管"跑到一半
账号挂了"）与 stoploss 的多维止损配套 —— 两条判据互补，合并会丢一半语义。
**将来若要恢复一个"引擎没起来"的显式判据，落点应在编排层，不要在这里重建一个没人读的类。**

## 已退役的部分

- `AgentAdapter` 抽象与 `solver/friend.py` 桥接层：它们服务的是框架自己的
  `orchestration.solve_one`，而那条链路已整体退位（竞技场主循环直接调朋友引擎）。
- `solver/factory.create_solver()`：竞技场用它那边的工厂
  （`redpilot.worker.adapter.solver.factory.create_solver`），不再需要框架这一份。
"""

from __future__ import annotations

import os

from redpilot.contracts.paths import HEARTBEAT_PATH


def touch_heartbeat() -> None:
    """刷新心跳文件 mtime（compose healthcheck 依据）。失败静默 —— 心跳写不动
    时该报的警由 healthcheck 那一侧报，这里抛异常只会把启动流程带崩。"""
    try:
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
    except Exception:
        pass
