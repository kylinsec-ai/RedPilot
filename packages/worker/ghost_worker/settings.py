"""worker 进程配置 — 散落 getenv 的单一收编点。

env 变量名全部不变(compose 兼容是硬约束);整合的是"读取点"而非命名。
此前 driver/relay/roster/status_server 各自 os.getenv 共 10+ 处;
现统一 WorkerSettings.from_env() 一处读取,消费方经构造参数接收。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("ghost_worker.settings")


def _parse_status_port(raw: str | None = None) -> int:
    """STATUS_PORT 安全解析:import 期绝不抛;空串=禁用(0),垃圾值回退 8080 并告警"""
    raw = raw if raw is not None else os.getenv("STATUS_PORT", "8080")
    text = (raw or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        log.warning("bad STATUS_PORT=%r, falling back to 8080", raw)
        return 8080


def _status_bind() -> str:
    """STATUS_BIND:默认 127.0.0.1(状态服务数据无鉴权,默认不对局域网开放)"""
    bind = (os.getenv("STATUS_BIND") or "").strip() or "127.0.0.1"
    if bind.startswith("-"):
        log.warning("bad STATUS_BIND=%r, falling back to 127.0.0.1", bind)
        return "127.0.0.1"
    return bind


@dataclass
class WorkerSettings:
    """worker 进程装配配置(一次性 from_env,消费方不再各自 getenv)"""

    benchmark_base_url: str = ""
    benchmark_token: str = ""
    # assignment 模式的控制面地址与服务凭据;默认仍使用 legacy SDK list 模式
    worker_mode: str = "legacy"
    platform_url: str = ""
    platform_worker_token: str = ""
    assignment_lease_seconds: int = 300
    workdir: str = "/work"
    worker_id: str = "worker-1"
    status_port: int = 8080
    # 状态服务监听地址:默认仅回环(数据无鉴权:实时 FLAG/完整实录,不应对局域网开放);
    # compose 内由编排显式置 0.0.0.0 —— docker-proxy 转发需容器全网卡监听,宿主侧再收成回环
    status_bind: str = "127.0.0.1"
    observability_url: str = ""
    observability_token: str = ""
    flag_format: str = "flag{...}"   # 只渲染进任务 prompt(提取正则硬编码 flag{...})
    # ── 舰队/编排（朋友的竞技场主循环认 ADAPTER_* 命名，见 orchestrator.py）──
    # role="monitor" = worker-1：只维持 VPN + 状态汇总 + 他管，不参与做题。
    adapter_role: str = ""
    # 编排侧的 worker 序号（0=monitor）。**与上面的 worker_id 是两套命名**：
    # worker_id 是框架观测面的展示名（relay/LiveState 用 WORKER_ID），
    # adapter_worker_id 是朋友 status/worker-N.json 与能力分片用的序号
    # （ADAPTER_WORKER_ID）。装配层保证两者指向同一个 worker。
    adapter_worker_id: int = 1

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        # 未知 WORKER_MODE:compose 入口(entrypoint.sh)先行 exit 0 拦截;
        # 宿主直跑 driver 时此处 warn + 回落 legacy(单机调试不断腿)。
        mode = (os.getenv("WORKER_MODE", "legacy") or "legacy").strip().lower()
        if mode not in {"legacy", "assignment"}:
            log.warning("bad WORKER_MODE=%r, falling back to legacy", mode)
            mode = "legacy"
        try:
            lease_seconds = int(os.getenv("ASSIGNMENT_LEASE_SECONDS", "300"))
        except ValueError:
            log.warning("bad ASSIGNMENT_LEASE_SECONDS, falling back to 300")
            lease_seconds = 300
        lease_seconds = min(max(lease_seconds, 30), 3600)
        # ADAPTER_WORKER_ID 缺失时从 WORKER_ID 的尾部数字兜底（"worker-3" → 3），
        # 再不行回 1。朋友的 `_worker_id()` 有一模一样的兜底（HOSTNAME 正则），
        # 这里先解析出来是为了让 relay 的 worker_id 与编排侧序号能对齐校验。
        try:
            adapter_wid = int(os.getenv("ADAPTER_WORKER_ID", "") or "")
        except ValueError:
            adapter_wid = -1
        if adapter_wid < 0:
            m = re.search(r"(\d+)\s*$", os.getenv("WORKER_ID", "").strip())
            adapter_wid = int(m.group(1)) if m else 1
        return cls(
            benchmark_base_url=os.getenv("BENCHMARK_BASE_URL", "").strip(),
            benchmark_token=os.getenv("BENCHMARK_TOKEN", "").strip(),
            worker_mode=mode,
            platform_url=os.getenv("PLATFORM_URL", "").strip().rstrip("/"),
            platform_worker_token=os.getenv("PLATFORM_WORKER_TOKEN", "").strip(),
            assignment_lease_seconds=lease_seconds,
            workdir=os.getenv("ADAPTER_WORKDIR", "/work").strip() or "/work",
            worker_id=(os.getenv("WORKER_ID", "worker-1").strip() or "worker-1"),
            status_port=_parse_status_port(),
            status_bind=_status_bind(),
            observability_url=os.getenv("OBSERVABILITY_URL", "").strip().rstrip("/"),
            observability_token=os.getenv("OBSERVABILITY_TOKEN", "").strip(),
            flag_format=os.getenv("ADAPTER_FLAG_FORMAT", "flag{...}") or "flag{...}",
            adapter_role=(os.getenv("ADAPTER_ROLE", "").strip().lower()),
            adapter_worker_id=adapter_wid,
        )
