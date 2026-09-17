"""RedPilot — 单发行版、模块化单体。

模块（依赖方向不可逆，由 tests/architecture 强制）:
  redpilot.contracts  共享内核(零第三方依赖): 词汇/路径/脱敏/摘要/快照
  redpilot.control    控制面: 挑战/调度/判分/容器供给/VPN
  redpilot.obs        观测平台: 摄取/读端/SSE/SPA
  redpilot.worker     求解 worker: 竞技场 + Pi 引擎 + relay + 本地态势台
  redpilot.app        平台进程装配根(唯一同时 import control 与 obs 的地方)

边界红线(执行点 tests/architecture/):
  - contracts 只允许 stdlib 与自身
  - worker 不得 import control/obs/app; 其 import 闭包内不得出现 fastapi/pydantic/uvicorn
  - control ↮ obs(只有 app.py 能同时见两者; worker/driver.py 是 worker 的装配根)
  - 跨模块只走各模块 __init__.py 的 __all__; 装配根与 contracts 子模块例外

安装: 基线为最小依赖; 平台进程用 [platform] extra, worker 镜像用 [worker] extra
(fastapi/pydantic 不进 Kali 镜像见 Dockerfile 的瘦身约束)。
"""

__version__ = "0.1.0"
