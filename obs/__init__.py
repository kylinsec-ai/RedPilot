"""TSecBench 本地观测平台 — agent 日志入库(SQLite WAL) + 只读态势 API + SPA 同源托管。

三层职责:
  obs/db.py      连接/建库/WAL pragma/版本化迁移
  obs/store.py   全部 SQL(摄取幂等写入 + 只读查询),单写者 = 本进程
  obs/digest.py  transcript 事件折叠为"人读时间线"(无状态确定性 fold)
  obs/redact.py  派生展示字段脱敏(有意重复移植,不 import worker 代码)
  obs/read.py    静态 SPA + 全部 GET /api/*
  obs/ingest.py  worker 摄取端点(X-Observability-Token)
"""

__version__ = "0.1.0"
