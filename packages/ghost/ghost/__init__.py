"""Ghost 平台统一包:控制面(control) + 观测平台(obs) + 统一 ASGI 装配(app)。

分层:
  ghost.control  控制面(调度/判分/供给/VPN);依赖 dotenv、httpx(python 侧无 fastapi 亦可导入 config/models)
  ghost.obs      观测平台(摄取/读端/SPA);localserver.py 为零 fastapi 的 stdlib 变体,worker 镜像只装本基线
  ghost.app      统一 FastAPI 装配(控制面路由 + 观测路由 + 控制代理)

边界约束:
  - obs 不得 import ghost.control(单向:control 不依赖 obs,obs 不依赖 control)
  - worker 只允许 import ghost.obs.localserver
  - 默认依赖仅 ghost-contracts;FastAPI 等归 [platform] extra —— 保证 Kali worker 镜像
    装本发行版时不会把 fastapi/pydantic 拉进来(镜像瘦身为既定决策)
"""

__version__ = "0.1.0"
