"""
多维止损治理器

从以下维度控制单题开销:
- 单题活动时间预算
- 连续无新事实的会话数
- 目标连续不可达的访问数
- 假设空间重复度
- 单题生命周期会话总数上限
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("adapter.stoploss")


@dataclass
class _ChallengeState:
    """单题止损状态"""
    code: str
    start_time: float = 0.0
    sessions: int = 0
    dry_sessions: int = 0          # 连续无新事实的会话数
    unreachable_visits: int = 0    # 连续不可达访问数
    total_facts: int = 0
    last_fact_session: int = -1
    last_commands: list = field(default_factory=list)  # 最近N次的命令快照
    flags_found: int = 0
    multi_flag: bool = False
    stopped: bool = False
    stop_reason: str = ""
    last_flag_session: int = -1
    zero_flag_sessions: int = 0       # 连续 0 flag 会话数（与事实洪流解耦）
    sessions_total: int = 0           # 终身会话总数（B13：revive 不重置，
                                      # 仅当 lifetime_sessions_cap>0 时作硬顶）
    start_wall: float = 0.0           # 墙钟开始时间（跨重启的时间预算依据；monotonic 重启归零）
    last_revive_wall: float = 0.0     # 上次 revive() 墙钟戳（复活冷却基准，跨重启）
    active_seconds: float = 0.0       # 累计活动时长（仅访问段，轮间空闲不计）
    visit_begin_wall: float = 0.0     # 本次访问起点（进程内瞬态，不持久化）


class StopLoss:
    """止损治理器"""

    def __init__(
        self,
        per_challenge_seconds: int = 4000,
        max_sessions: int = 8,
        dry_cutoff: int = 3,
        unreachable_cutoff: int = 3,
        multi_flag_max_mult: float = 4.0,
        lifetime_sessions_cap: int = 0,
        zero_flag_cutoff: int = 4,
        workdir: str = "",
    ):
        self.per_challenge_seconds = per_challenge_seconds
        self.max_sessions = max_sessions
        self.dry_cutoff = dry_cutoff
        self.unreachable_cutoff = unreachable_cutoff
        self.multi_flag_max_mult = multi_flag_max_mult
        self.lifetime_sessions_cap = lifetime_sessions_cap
        self.zero_flag_cutoff = zero_flag_cutoff
        self.workdir = workdir or os.environ.get("ADAPTER_WORKDIR", "/work")
        self._states: dict[str, _ChallengeState] = {}

    def _safe_code(self, code: str) -> str:
        """复刻 driver 的 _safe_code：目录名与工作目录保持一致。"""
        raw = str(code)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
        if safe == raw:
            return safe
        return f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"

    def _state_path(self, code: str) -> str:
        return os.path.join(self.workdir, self._safe_code(code), ".stoploss.json")

    def _load(self, code: str):
        try:
            with open(self._state_path(code), encoding="utf-8") as f:
                d = json.load(f)
            st = _ChallengeState(code=code)
            for k in ("start_time", "start_wall", "sessions", "dry_sessions",
                      "unreachable_visits", "total_facts", "last_fact_session",
                      "flags_found", "multi_flag", "stopped", "stop_reason",
                      "last_flag_session", "zero_flag_sessions",
                      "last_revive_wall", "active_seconds", "sessions_total"):
                if k in d:
                    setattr(st, k, d[k])
            # 旧状态迁移：无 active_seconds 时以 now-start_wall 近似（延续当前折损度，
            # 部署不白送预算）。visit_begin_wall 不持久化：重启后不可能有在访访问。
            if "active_seconds" not in d and st.start_wall:
                st.active_seconds = max(0.0, time.time() - st.start_wall)
            # 旧状态迁移（B13）：无 sessions_total 时以当前 sessions 起算终身计数
            if "sessions_total" not in d:
                st.sessions_total = st.sessions
            return st
        except Exception:
            return None

    def _save(self, st: _ChallengeState) -> None:
        """原子写状态文件（tmp+rename）。静默失败，不阻塞解题。"""
        try:
            p = self._state_path(st.code)
            d = {
                "code": st.code,
                "start_time": st.start_time,
                "start_wall": st.start_wall,
                "sessions": st.sessions,
                "sessions_total": st.sessions_total,
                "dry_sessions": st.dry_sessions,
                "unreachable_visits": st.unreachable_visits,
                "total_facts": st.total_facts,
                "last_fact_session": st.last_fact_session,
                "flags_found": st.flags_found,
                "multi_flag": st.multi_flag,
                "stopped": st.stopped,
                "stop_reason": st.stop_reason,
                "last_flag_session": st.last_flag_session,
                "zero_flag_sessions": st.zero_flag_sessions,
                "last_revive_wall": st.last_revive_wall,
                "active_seconds": st.active_seconds,
                "stop_ts_wall": time.time(),
            }
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception:
            pass

    def _get(self, code: str) -> _ChallengeState:
        if code not in self._states:
            loaded = self._load(code)
            self._states[code] = loaded if loaded is not None else _ChallengeState(code=code)
        return self._states[code]

    def start(self, code: str, *, multi_flag: bool = False) -> None:
        """标记题目开始"""
        st = self._get(code)
        if st.start_time == 0:
            st.start_time = time.monotonic()
        st.sessions += 1
        st.sessions_total += 1
        st.multi_flag = multi_flag
        if st.start_wall == 0:
            st.start_wall = time.time()
        st.visit_begin_wall = time.time()
        self._save(st)

    def record_fact(self, code: str) -> None:
        """记录新事实发现"""
        st = self._get(code)
        st.total_facts += 1
        st.last_fact_session = st.sessions
        st.dry_sessions = 0
        self._save(st)

    def record_no_progress(self, code: str) -> None:
        """记录无进展会话"""
        st = self._get(code)
        st.dry_sessions += 1
        self._save(st)

    def record_zero_flag(self, code: str) -> None:
        """记录一场 0 flag 会话（找到 flag 前不因“有事实”而重置）。"""
        st = self._get(code)
        st.zero_flag_sessions += 1
        self._save(st)

    def record_progress(self, code: str) -> None:
        """记录一场“有实质进展但未入账 flag”的会话（编排加固）。

        深度逆向会话产出中间产物（emulator/解码脚本/patch 二进制）是真实进展，
        不应被 zero_flag 误判为零进展而止损 —— 这里重置 zero-flag 与 dry 窗口。
        （产物创建比“口头事实”难伪造；纯盲注逐字符只会产生查询输出、不会新增产物。）
        """
        st = self._get(code)
        st.zero_flag_sessions = 0
        st.dry_sessions = 0
        self._save(st)

    def record_unreachable(self, code: str) -> None:
        """记录目标不可达"""
        st = self._get(code)
        st.unreachable_visits += 1
        self._save(st)

    def record_reachable(self, code: str) -> None:
        """记录目标可达 (重置不可达计数)"""
        st = self._get(code)
        st.unreachable_visits = 0
        self._save(st)

    def record_flag(self, code: str) -> None:
        """记录发现 flag"""
        st = self._get(code)
        st.flags_found += 1
        st.last_flag_session = st.sessions
        st.dry_sessions = 0
        st.zero_flag_sessions = 0
        self._save(st)

    def flags_banked(self, code: str) -> int:
        """返回已确认的 flag 数"""
        return self._get(code).flags_found

    def should_stop(self, code: str) -> tuple[bool, str]:
        """
        判断是否应停止该题。

        返回: (should_stop, reason)
        """
        st = self._get(code)

        if st.stopped:
            return True, st.stop_reason

        # 时间预算
        budget = self.per_challenge_seconds
        if st.multi_flag:
            budget = int(budget * self.multi_flag_max_mult)
        # 时间预算按【累计活动时长】（访问段求和，轮间空闲不计——空闲是调度器
        # 在解别的题，不该折损本题预算；实测 c-05 30min 空闲吃掉一半预算把 round-2
        # 时间盒 6120s 压到 1258s）。在访时加上当前段。
        elapsed = st.active_seconds
        if st.visit_begin_wall:
            elapsed += max(0.0, time.time() - st.visit_begin_wall)
        if elapsed > budget:
            st.stopped = True
            st.stop_reason = f"time_budget:{int(elapsed)}s>{budget}s"
            self._save(st)
            return True, st.stop_reason

        # 会话数上限（B13 两口径）
        # - 显式配置 lifetime_sessions_cap(>0) → 用终身计数 sessions_total，
        #   revive 不重置，硬顶真正不可绕过；
        # - 未配置 → 用本轮计数 sessions，revive 会清零，复活才是 fresh budget
        #   （旧行为：revive 不清 sessions，实测 c-05 复活 8ms 即被 sessions:9>8
        #   再停，白烧一次派发机会后永久放弃）。
        if self.lifetime_sessions_cap > 0:
            used, cap = st.sessions_total, self.lifetime_sessions_cap
        else:
            used, cap = st.sessions, self.max_sessions
        if st.multi_flag:
            cap = int(cap * self.multi_flag_max_mult)
        if used > cap:
            st.stopped = True
            st.stop_reason = f"sessions:{used}>{cap}"
            self._save(st)
            return True, st.stop_reason

        # 连续无新事实
        if st.dry_sessions >= self.dry_cutoff:
            # 如果是多flag且已有进展，用更宽松的阈值
            effective_cutoff = self.dry_cutoff
            if st.multi_flag and st.flags_found > 0:
                effective_cutoff = self.dry_cutoff * 2
            if st.dry_sessions >= effective_cutoff:
                st.stopped = True
                st.stop_reason = f"stuck:dry_sessions={st.dry_sessions}"
                self._save(st)
                return True, st.stop_reason

        # 连续 0 flag 会话 → 轮换（与“新事实”解耦：盲注逐字符每轮都产生事实，
        # 顶穿 dry 判据；只看是否真拿到 flag，连 N 场没 flag 就止损轮换）
        if st.zero_flag_sessions >= self.zero_flag_cutoff:
            st.stopped = True
            st.stop_reason = f"zero_flag_sessions:{st.zero_flag_sessions}>={self.zero_flag_cutoff}"
            self._save(st)
            return True, st.stop_reason

        # 连续不可达
        if st.unreachable_visits >= self.unreachable_cutoff:
            st.stopped = True
            st.stop_reason = f"unreachable:{st.unreachable_visits}"
            self._save(st)
            return True, st.stop_reason

        return False, ""

    def remaining_seconds(self, code: str) -> int:
        """返回该题剩余时间预算"""
        st = self._get(code)
        budget = self.per_challenge_seconds
        if st.multi_flag:
            budget = int(budget * self.multi_flag_max_mult)
        elapsed = st.active_seconds
        if st.visit_begin_wall:
            elapsed += max(0.0, time.time() - st.visit_begin_wall)
        return max(0, int(budget - elapsed))

    def rearm_dry_window(self, code: str) -> None:
        """重置干旱窗口 (用于被挂起后重新恢复)"""
        st = self._get(code)
        st.dry_sessions = 0
        st.stopped = False
        st.stop_reason = ""
        self._save(st)

    def end_visit(self, code: str) -> None:
        """结算本次访问的活动时长（solve_one finally 调用）。

        幂等：visit_begin_wall==0 时为 no-op（未开始过的访问/重复调用）。
        """
        st = self._get(code)
        if st.visit_begin_wall:
            st.active_seconds += max(0.0, time.time() - st.visit_begin_wall)
            st.visit_begin_wall = 0.0
            self._save(st)

    def last_revive_wall(self, code: str) -> float:
        """上次复活的墙钟时间（复活冷却基准）。

        旧状态文件无该字段（=0）时回退 start_wall：以本轮预算起点近似冷却起点，
        保守不给"从未复活"的老状态白送一次立即复活。
        """
        st = self._get(code)
        return st.last_revive_wall or st.start_wall or 0.0

    def revive(self, code: str) -> None:
        """复活一个被停止的题目 —— 给予一次全新尝试（fresh budget）。

        不仅清 stopped/dry/unreachable，还重置时间预算（start_wall/start_time）
        与 zero_flag 窗口；否则旧时间预算会让下一次 should_stop 因 time_budget
        立即再停（“复活失效”）。

        B13：本轮会话计数 sessions 也必须清零 —— 旧行为保留 sessions，复活后
        should_stop 立刻以 sessions:9>8 再停（实测 c-05 复活 8ms 即被杀，随后
        派发机会耗尽被永久放弃）。终身总量改由 sessions_total 记账，仅当显式
        配置 lifetime_sessions_cap>0 时充当硬顶，防止无限重烧。
        flags_found 保留不动（仅作统计/多 flag 判定）。
        """
        st = self._get(code)
        st.stopped = False
        st.stop_reason = ""
        st.sessions = 0
        st.dry_sessions = 0
        st.unreachable_visits = 0
        st.zero_flag_sessions = 0
        st.start_time = time.monotonic()
        st.start_wall = time.time()
        st.last_revive_wall = time.time()
        st.active_seconds = 0.0
        st.visit_begin_wall = 0.0
        self._save(st)
