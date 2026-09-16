"""worker 题目总览数据层(只读,stdlib;由 drivers/roster.py 拆分而来)。

职责:
RosterPoller —— 后台线程周期拉平台题目总览(官方 SDK sync 客户端)并叠加本地
work/ 扫描(FLAG/transcript/crash/产物痕迹),原子落盘 /work/.live/roster.json。
失败保旧缓存 + stale/platform_error 标记;缺 BENCHMARK_* 环境时优雅禁用
(仅本地痕迹视图)。任何异常只记日志——绝不反向影响求解线程。

本地目录扫描/FLAG 读法/单行模板在 obs.localserver(纯 stdlib 注入侧);
此处从那里导入,保证两处读到的目录语义一致。
架构边界:这是 worker→obs 唯一允许的代码 import(obs.localserver 诊断侧),
只复用 stdlib 纯函数,不触碰 obs.store/db(无数据依赖)。
"""

from __future__ import annotations

import logging
import os
import threading
import time

from redpilot_contracts.fsio import atomic_write_json
from redpilot_contracts.paths import LIVE_DIR, ROSTER_FILENAME

from redpilot.obs.localserver import (challenge_detail, empty_roster_snapshot,
                             local_challenge_row, read_flag_lines, scan_local)

log = logging.getLogger("redpilot_worker.roster")

__all__ = ["RosterPoller", "read_flag_lines", "scan_local",
           "challenge_detail", "empty_roster_snapshot", "local_challenge_row"]


# ── RosterPoller ─────────────────────────────────────────────

class RosterPoller:
    """60s 周期:平台 list_challenges + 本地扫描 -> 原子落 roster.json。

    平台行字段:unique_code/difficulty/level/total_score/flag_count/
    correct_flag_count/is_completed/container_status/container_addr/description。
    每行再叠加本地痕迹 local。纯只读,异常隔离。
    """

    def __init__(self, workdir: str, *, interval: float = 60.0,
                 base_url: str = "", token: str = ""):
        self._workdir = workdir
        self._interval = interval
        self._path = os.path.join(workdir, LIVE_DIR, ROSTER_FILENAME)
        self._lock = threading.Lock()
        self._snap = empty_roster_snapshot(platform_disabled=False)
        self._client = None
        self._base_url = base_url
        self._token = token

    def _platform_rows(self) -> dict[str, dict]:
        client = self._client
        if client is None:
            return {}
        rows: dict[str, dict] = {}
        try:
            for ch in client.list_challenges():
                rows[ch.unique_code] = {
                    "unique_code": ch.unique_code,
                    "difficulty": ch.difficulty,
                    "level": ch.level,
                    "total_score": ch.total_score,
                    "flag_count": ch.flag_count,
                    "correct_flag_count": ch.correct_flag_count,
                    "is_completed": ch.is_completed,
                    "container_status": ch.container_status,
                    "container_addr": list(ch.container_addr or []),
                    "description": ch.description or "",
                }
        except Exception as e:
            log.warning("roster platform poll failed: %s", e)
            raise
        return rows

    def _ensure_client(self):
        """惰性建 sync SDK 客户端;缺环境/未装包返回 None"""
        if self._client is not None:
            return self._client
        base = self._base_url or os.getenv("BENCHMARK_BASE_URL", "").strip()
        token = self._token or os.getenv("BENCHMARK_TOKEN", "").strip()
        if not base or not token:
            return None
        try:
            from ._sdk import RedPilotmark
            self._client = RedPilotmark(base_url=base, token=token, auto_check_vpn=False)
        except Exception:
            log.exception("tsec_benchmark unavailable; roster platform poll disabled")
        return self._client

    def poll_once(self) -> None:
        """一次拉取+落盘;平台失败保旧缓存并记 stale。"""
        local = scan_local(self._workdir)
        try:
            platform = self._platform_rows()
            with self._lock:
                self._snap = {
                    "fetched_at": time.time(),
                    "stale": False,
                    "platform_error": "",
                    "platform_disabled": False,
                    "challenges": platform,
                }
        except Exception as e:
            with self._lock:
                self._snap["fetched_at"] = time.time()
                self._snap["stale"] = True
                self._snap["platform_error"] = str(e)
        # 本地痕迹叠加到当前快照(平台失败时也刷本地——保留旧平台行)。
        # 行级深拷贝一层:已交出的 snapshot()/排队中的 relay 载荷不再被下轮原地改写。
        with self._lock:
            challenges = {code: dict(row)
                          for code, row in self._snap["challenges"].items()}
        for code, lc in local.items():
            row = challenges.setdefault(code, local_challenge_row(code))
            row["local"] = lc
        with self._lock:
            self._snap["challenges"] = challenges
        atomic_write_json(self._path, self.snapshot())

    def snapshot(self) -> dict:
        """行级拷贝交出:调用方持有的是快照时刻的副本,与轮询线程的 live 行解耦。"""
        with self._lock:
            snap = dict(self._snap)
            snap["challenges"] = {code: dict(row)
                                  for code, row in snap["challenges"].items()}
            return snap

    def _poll_local_only(self) -> None:
        """平台不可用/被禁时:至少刷一次本地痕迹视图(platform 行清空)"""
        local = scan_local(self._workdir)
        with self._lock:
            self._snap = {
                "fetched_at": time.time(),
                "stale": False,
                "platform_error": "",
                "platform_disabled": True,
                "challenges": {},
            }
        challenges: dict = {}
        for code, lc in local.items():
            challenges[code] = local_challenge_row(code, local=lc)
        with self._lock:
            self._snap["challenges"] = challenges
        atomic_write_json(self._path, self.snapshot())

    def _loop(self) -> None:
        warned_sdk = False
        while True:
            try:
                if self._ensure_client() is None:
                    if not warned_sdk:
                        log.info("roster platform poll disabled "
                                 "(BENCHMARK_* unset or SDK missing); retrying each cycle")
                        warned_sdk = True
                    # 平台不可用也要每轮重扫本地:新题目录/FLAG/transcript 增长
                    # 都会出现;SDK 就绪后 _ensure_client 可自愈
                    self._poll_local_only()
                    time.sleep(self._interval)
                    continue
                warned_sdk = False
                self.poll_once()
            except Exception:
                log.exception("roster poll loop error")
            time.sleep(self._interval)

    def start(self) -> threading.Thread | None:
        t = threading.Thread(target=self._loop, daemon=True, name="roster-poller")
        t.start()
        return t
