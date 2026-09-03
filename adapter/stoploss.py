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

import logging
import time
from dataclasses import dataclass, field

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
    ):
        self.per_challenge_seconds = per_challenge_seconds
        self.max_sessions = max_sessions
        self.dry_cutoff = dry_cutoff
        self.unreachable_cutoff = unreachable_cutoff
        self.multi_flag_max_mult = multi_flag_max_mult
        self.lifetime_sessions_cap = lifetime_sessions_cap
        self._states: dict[str, _ChallengeState] = {}

    def _get(self, code: str) -> _ChallengeState:
        if code not in self._states:
            self._states[code] = _ChallengeState(code=code)
        return self._states[code]

    def start(self, code: str, *, multi_flag: bool = False) -> None:
        """标记题目开始"""
        st = self._get(code)
        if st.start_time == 0:
            st.start_time = time.monotonic()
        st.sessions += 1
        st.multi_flag = multi_flag

    def record_fact(self, code: str) -> None:
        """记录新事实发现"""
        st = self._get(code)
        st.total_facts += 1
        st.last_fact_session = st.sessions
        st.dry_sessions = 0

    def record_no_progress(self, code: str) -> None:
        """记录无进展会话"""
        st = self._get(code)
        st.dry_sessions += 1

    def record_unreachable(self, code: str) -> None:
        """记录目标不可达"""
        st = self._get(code)
        st.unreachable_visits += 1

    def record_reachable(self, code: str) -> None:
        """记录目标可达 (重置不可达计数)"""
        st = self._get(code)
        st.unreachable_visits = 0

    def record_flag(self, code: str) -> None:
        """记录发现 flag"""
        st = self._get(code)
        st.flags_found += 1
        st.last_flag_session = st.sessions
        st.dry_sessions = 0

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
        elapsed = time.monotonic() - st.start_time if st.start_time else 0
        if elapsed > budget:
            st.stopped = True
            st.stop_reason = f"time_budget:{int(elapsed)}s>{budget}s"
            return True, st.stop_reason

        # 会话数上限
        cap = self.lifetime_sessions_cap if self.lifetime_sessions_cap > 0 else self.max_sessions
        if st.multi_flag:
            cap = int(cap * self.multi_flag_max_mult)
        if st.sessions > cap:
            st.stopped = True
            st.stop_reason = f"sessions:{st.sessions}>{cap}"
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
                return True, st.stop_reason

        # 连续不可达
        if st.unreachable_visits >= self.unreachable_cutoff:
            st.stopped = True
            st.stop_reason = f"unreachable:{st.unreachable_visits}"
            return True, st.stop_reason

        return False, ""

    def remaining_seconds(self, code: str) -> int:
        """返回该题剩余时间预算"""
        st = self._get(code)
        budget = self.per_challenge_seconds
        if st.multi_flag:
            budget = int(budget * self.multi_flag_max_mult)
        elapsed = time.monotonic() - st.start_time if st.start_time else 0
        return max(0, int(budget - elapsed))

    def rearm_dry(self, code: str) -> bool:
        """若题目因连续干旱(stuck)被停则重置并放行，返回是否放行"""
        st = self._get(code)
        if not st.stop_reason.startswith("stuck:"):
            return False
        st.dry_sessions = 0
        st.stopped = False
        st.stop_reason = ""
        return True
