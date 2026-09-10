"""Ghost 本地观测平台 — agent 日志入库(SQLite WAL) + 只读态势 API + SPA 同源托管。

模块职责:
  obs/db.py        连接/建库/WAL pragma/版本化迁移
  obs/store.py     全部 SQL(摄取幂等写入 + 只读查询),单写者 = 本进程
  obs/schema.py    pydantic 摄取模型 + 词汇 re-export(单源 ghost_contracts)
  obs/read.py      静态 SPA + 全部 GET /api/*(SSE /api/events + 时间线 fold_rows)
  obs/ingest.py    worker 摄取端点(X-Observability-Token)
  obs/localserver.py  stdlib 本地实时源变体(worker 容器 :8080,协作者注入,零 fastapi 依赖)

共享语义(词汇/快照/脱敏/截断/原子 IO/折叠状态机/资产表)单源 ghost_contracts;
本包绝不 import ghost_worker(纯度测试强制)。
"""

__version__ = "0.1.0"
