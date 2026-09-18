"""RedPilot 本地观测平台 — agent 日志入库(SQLite WAL) + 只读态势 API + SPA 同源托管。

模块职责:
  obs/db.py        连接/建库/WAL pragma/版本化迁移
  obs/store.py     全部 SQL(摄取幂等写入 + 只读查询),单写者 = 本进程
  obs/schema.py    pydantic 摄取模型 + 词汇 re-export(单源 redpilot.contracts)
  obs/read.py      静态 SPA + 全部 GET /api/*(SSE /api/events + 时间线 fold_rows)
  obs/ingest.py    worker 摄取端点(X-Observability-Token)

共享语义(词汇/快照/脱敏/截断/原子 IO/折叠状态机/资产表)单源 redpilot.contracts;
本包绝不 import redpilot.worker/redpilot.control(执行点 tests/architecture)。
本地态势台已归 worker(dashboard.py),不再住在本包。
"""

