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

from contextlib import contextmanager
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, TypeVar

log = logging.getLogger("adapter.stoploss")

_T = TypeVar("_T")


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
    flag_hashes: list = field(default_factory=list)  # 已确认 flag 的不可逆去重键
    # 平台确认存在的唯一 flag 数，包含其他 worker / 旧访问已入账而本次收到
    # duplicate 的 flag。它不替代 flags_found，后者仍只统计本 worker 新入账数。
    confirmed_flags: int = 0
    multi_flag: bool = False
    # 多 flag 题的止损必须先经过一次平台提示复核。这里只保存流程元数据，
    # 不保存提示正文或任何候选答案，避免把平台提示变成跨题记忆源。
    hint_requested: bool = False
    post_hint_dry_sessions: int = 0
    stopped: bool = False
    stop_reason: str = ""
    last_flag_session: int = -1
    zero_flag_sessions: int = 0       # 连续 0 flag 会话数（与事实洪流解耦）
    sessions_total: int = 0           # 终身会话总数（B13：revive 不重置，
                                      # 仅当 lifetime_sessions_cap>0 时作硬顶）
    target_restarts: int = 0          # B16：本次访问已重启题目容器的次数（start 重置）
    target_restarts_total: int = 0    # B21：终身重启次数（start/revive 均不重置）
    last_restart_wall: float = 0.0    # B21：上次重启墙钟戳（重启冷却基准）
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
        post_hint_dry_cutoff: int | None = None,
        lifetime_sessions_cap: int = 0,
        # 单 flag 题连续三场没有平台确认 flag 即轮换，避免一个 worker
        # 长时间被同一道无进展题占用。多 flag 题在已有部分进展时仍不触发该阈值。
        zero_flag_cutoff: int = 3,
        workdir: str = "",
    ):
        self.per_challenge_seconds = per_challenge_seconds
        self.max_sessions = max_sessions
        self.dry_cutoff = dry_cutoff
        self.unreachable_cutoff = unreachable_cutoff
        self.multi_flag_max_mult = multi_flag_max_mult
        # 提示后的复核窗口默认与普通 dry 窗口相同，但单独暴露旋钮，
        # 便于在不改动“提示前必须继续”的语义下调整最终切题阈值。
        if post_hint_dry_cutoff is None:
            post_hint_dry_cutoff = dry_cutoff
        try:
            self.post_hint_dry_cutoff = max(1, int(post_hint_dry_cutoff))
        except (TypeError, ValueError):
            self.post_hint_dry_cutoff = max(1, int(dry_cutoff or 1))
        self.lifetime_sessions_cap = lifetime_sessions_cap
        self.zero_flag_cutoff = zero_flag_cutoff
        # hard 题的零 flag 容忍度（"逆向/密码难破解"的题需要更多轮
        # 逆算法→试解码→提交试错；默认与全局相同，driver 可按难度覆盖）。
        try:
            self.zero_flag_cutoff_hard = max(
                1, int(os.environ.get("ADAPTER_ZERO_FLAG_CUTOFF_HARD", "6") or "6"))
        except (TypeError, ValueError):
            self.zero_flag_cutoff_hard = 6
        # per-code 难度覆盖：difficulty -> bool "use harder cutoff"
        self._hard_codes: set[str] = set()
        self._hard_codes_guard = threading.Lock()
        self.workdir = workdir or os.environ.get("ADAPTER_WORKDIR", "/work")
        self._states: dict[str, _ChallengeState] = {}
        # ``visit_begin_wall`` 是本进程正在访问时的瞬态值，不能写进共享状态。
        # 否则崩溃重启会把停机时间也算作解题时间。其余字段则一律在锁内从磁盘
        # 重读，避免两个 worker 用各自的旧缓存互相覆盖。
        self._visit_begins: dict[str, float] = {}
        self._local_locks: dict[str, threading.RLock] = {}
        self._local_locks_guard = threading.Lock()

    def _safe_code(self, code: str) -> str:
        """复刻 driver 的 _safe_code：目录名与工作目录保持一致。"""
        raw = str(code)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
        if safe == raw:
            return safe
        return f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"

    # ── per-code 难度标记 ──
    def set_difficulty(self, code: str, difficulty: str) -> None:
        """标记某题为难做（hard），该题的零 flag 容忍走更宽松的阈值。"""
        is_hard = str(difficulty or "").strip().lower() == "hard"
        with self._hard_codes_guard:
            if is_hard:
                self._hard_codes.add(str(code))
            else:
                self._hard_codes.discard(str(code))

    def _zero_flag_limit(self, code: str) -> int:
        """返回该题实际使用的零 flag 阈值（hard 题更宽松）。"""
        with self._hard_codes_guard:
            hard = str(code) in self._hard_codes
        return self.zero_flag_cutoff_hard if hard else self.zero_flag_cutoff

    def _state_path(self, code: str) -> str:
        return os.path.join(self.workdir, self._safe_code(code), ".stoploss.json")

    def _lock_path(self, code: str) -> str:
        """返回跨进程锁路径。

        锁不能放在单题目录：driver 为隔离新实例会清空该目录；清理期间 unlink
        一个仍被 flock 的锁文件会让后续进程锁到新 inode，等同于没有锁。独立
        的控制目录只保存锁和临时状态文件，不包含题面、命令或 flag。
        """
        return os.path.join(
            self.workdir, ".stoploss-locks", f"{self._safe_code(code)}.lock")

    def _local_lock(self, code: str) -> threading.RLock:
        """同一 StopLoss 实例内也串行化，补足 flock 的线程语义。"""
        with self._local_locks_guard:
            lock = self._local_locks.get(code)
            if lock is None:
                lock = threading.RLock()
                self._local_locks[code] = lock
            return lock

    def _load(self, code: str):
        """在持有该题文件锁时读取状态；损坏的旧状态降级为新状态。"""
        try:
            with open(self._state_path(code), encoding="utf-8") as f:
                d = json.load(f)
            st = _ChallengeState(code=code)
            for k in ("start_time", "start_wall", "sessions", "dry_sessions",
                      "unreachable_visits", "total_facts", "last_fact_session",
                      "flags_found", "multi_flag", "stopped", "stop_reason",
                      "last_flag_session", "zero_flag_sessions",
                      "last_revive_wall", "active_seconds", "sessions_total",
                      "target_restarts", "target_restarts_total",
                      "last_restart_wall", "flag_hashes", "confirmed_flags",
                      "hint_requested", "post_hint_dry_sessions"):
                if k in d:
                    setattr(st, k, d[k])
            # 旧状态迁移：无 active_seconds 时以 now-start_wall 近似（延续当前折损度，
            # 部署不白送预算）。visit_begin_wall 不持久化：重启后不可能有在访访问。
            if "active_seconds" not in d and st.start_wall:
                st.active_seconds = max(0.0, time.time() - st.start_wall)
            # 旧状态迁移（B13）：无 sessions_total 时以当前 sessions 起算终身计数
            if "sessions_total" not in d:
                st.sessions_total = st.sessions
            # 旧/手工编辑的状态可能把哈希表写成字符串或 null；统一成列表，
            # 避免去重判断退化成子串匹配或在 append 时让止损链路报错。
            if not isinstance(st.flag_hashes, list):
                st.flag_hashes = []
            st.flag_hashes = [str(item) for item in st.flag_hashes if item]
            # ``confirmed_flags`` 是新字段。旧状态至少可从本地入账数量与保存的
            # hash 数迁移，不能把已有部分进展误判为 0 flag。
            try:
                st.flags_found = max(0, int(st.flags_found or 0))
            except (TypeError, ValueError):
                st.flags_found = 0
            try:
                st.confirmed_flags = max(0, int(st.confirmed_flags or 0))
            except (TypeError, ValueError):
                st.confirmed_flags = 0
            st.hint_requested = bool(st.hint_requested)
            try:
                st.post_hint_dry_sessions = max(0, int(st.post_hint_dry_sessions or 0))
            except (TypeError, ValueError):
                st.post_hint_dry_sessions = 0
            st.confirmed_flags = max(
                st.confirmed_flags, st.flags_found, len(st.flag_hashes))
            return st
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _save(self, st: _ChallengeState) -> None:
        """在已持有该题文件锁时原子写状态文件。

        临时文件放到稳定的锁目录，并使用 mkstemp 的唯一名称。旧实现使用固定
        ``.tmp``，两个 worker 同时保存时会互删/互换临时文件；即使 rename 原子，
        也会造成丢失更新。这里的调用者先在锁内重新读盘再写回。
        """
        tmp = ""
        try:
            p = self._state_path(st.code)
            d = {
                "code": st.code,
                "start_time": st.start_time,
                "start_wall": st.start_wall,
                "sessions": st.sessions,
                "sessions_total": st.sessions_total,
                "target_restarts": st.target_restarts,
                "target_restarts_total": st.target_restarts_total,
                "last_restart_wall": st.last_restart_wall,
                "dry_sessions": st.dry_sessions,
                "unreachable_visits": st.unreachable_visits,
                "total_facts": st.total_facts,
                "last_fact_session": st.last_fact_session,
                "flags_found": st.flags_found,
                "flag_hashes": list(st.flag_hashes)[-256:],
                "confirmed_flags": st.confirmed_flags,
                "hint_requested": bool(st.hint_requested),
                "post_hint_dry_sessions": st.post_hint_dry_sessions,
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
            lock_dir = os.path.dirname(self._lock_path(st.code))
            os.makedirs(lock_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=f".{self._safe_code(st.code)}.", suffix=".tmp", dir=lock_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            # 对 bind mount 也尽力刷目录元数据；不支持目录 fsync 的文件系统不让
            # 解题主流程失败。
            try:
                dir_fd = os.open(os.path.dirname(p), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        except (OSError, TypeError, ValueError) as exc:
            log.warning("unable to persist stoploss state for %s: %s", st.code, exc)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    @contextmanager
    def _locked_state(self, code: str, *, write: bool) -> Iterator[_ChallengeState]:
        """锁内读取共享状态，必要时写回。

        所有公共计数操作都经由此入口；因此每次更新都是
        ``flock → reload → mutate → unique-temp+replace → unlock``，不会由陈旧
        的进程缓存覆盖别的 worker 的增量。
        """
        with self._local_lock(code):
            lock_fd = -1
            try:
                state_dir = os.path.dirname(self._state_path(code))
                lock_path = self._lock_path(code)
                os.makedirs(state_dir, exist_ok=True)
                os.makedirs(os.path.dirname(lock_path), exist_ok=True)
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as exc:
                # 只在卷本身不可用时走内存降级；正常运行绝不会进入这里。保留
                # 日志而不是静默使用无锁写，方便运维发现“止损不再可持久化”。
                log.warning("unable to lock stoploss state for %s: %s", code, exc)
                st = self._states.get(code) or self._load(code) or _ChallengeState(code=code)
                visit_begin = self._visit_begins.get(code, 0.0)
                if visit_begin:
                    st.visit_begin_wall = visit_begin
                try:
                    yield st
                    if write:
                        self._save(st)
                    self._states[code] = st
                finally:
                    if lock_fd >= 0:
                        os.close(lock_fd)
                return

            try:
                st = self._load(code) or _ChallengeState(code=code)
                # 访问起点是当前进程私有的瞬态值，只用于计算本进程所花的活动时长。
                visit_begin = self._visit_begins.get(code, 0.0)
                if visit_begin:
                    st.visit_begin_wall = visit_begin
                yield st
                if write:
                    self._save(st)
                self._states[code] = st
            finally:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)

    def _mutate(self, code: str, update: Callable[[_ChallengeState], _T]) -> _T:
        with self._locked_state(code, write=True) as st:
            return update(st)

    def _read_state(self, code: str) -> _ChallengeState:
        with self._locked_state(code, write=False) as st:
            return st

    def _get(self, code: str) -> _ChallengeState:
        """兼容内部旧调用；读取也不能相信跨进程缓存。"""
        return self._read_state(code)

    def start(self, code: str, *, multi_flag: bool = False) -> None:
        """标记一次题目访问开始（不把访问误记成 Pi session）。"""
        now_wall = time.time()
        self._visit_begins[code] = now_wall

        def update(st: _ChallengeState) -> None:
            if st.start_time == 0:
                st.start_time = time.monotonic()
            st.target_restarts = 0   # B16：重启额度按访问重置
            st.multi_flag = multi_flag
            if st.start_wall == 0:
                st.start_wall = now_wall
            st.visit_begin_wall = now_wall

        self._mutate(code, update)

    def start_session(self, code: str) -> None:
        """记录一次真正启动的 Pi session。

        `max_sessions` 是会话上限，不能只在外层 visit 增加一次；否则单个
        20 分钟访问中的任意多次 Pi 重试都会绕过止损。这里不重置访问级的
        target_restarts，也不重新读盘覆盖本访问刚写入的状态。
        """
        def update(st: _ChallengeState) -> None:
            st.sessions += 1
            st.sessions_total += 1

        self._mutate(code, update)

    def record_fact(self, code: str) -> None:
        """记录新事实发现"""
        def update(st: _ChallengeState) -> None:
            st.total_facts += 1
            st.last_fact_session = st.sessions
            st.dry_sessions = 0
            # A newly observed host/service is meaningful progress even before
            # the next flag.  Reset the flag-less streak as well; otherwise a
            # fact-producing session followed by the bookkeeping call below
            # would still accumulate ``zero_flag_sessions`` and request a hint
            # while the intranet chain is actively advancing.
            st.zero_flag_sessions = 0
            # 提示后的复核窗口只统计连续无新事实；一条新事实就重新
            # 打开窗口，避免把仍在推进的内网链误判为无意义。
            st.post_hint_dry_sessions = 0

        self._mutate(code, update)

    def record_no_progress(self, code: str) -> None:
        """记录无进展会话"""
        def update(st: _ChallengeState) -> None:
            st.dry_sessions += 1
            if st.hint_requested:
                st.post_hint_dry_sessions += 1

        self._mutate(code, update)

    def record_zero_flag(self, code: str) -> None:
        """记录一场没有平台确认 flag 的会话。

        Driver 会在会话末尾统一记这笔账，而新主机、服务、凭据等事实是在
        同一会话稍早通过 :meth:`record_fact` 写入的。若不在这里辨别该顺序，
        ``record_fact()`` 刚清掉的空转计数会立刻被加回，仍在横向推进的内网链
        会错误地耗尽一次提示机会。事实和 flag 是不同维度，但“本会话有新事实”
        不能被记作连续无进展。
        """
        def update(st: _ChallengeState) -> None:
            if st.last_fact_session == st.sessions:
                return
            st.zero_flag_sessions += 1

        self._mutate(code, update)

    def record_progress(self, code: str) -> None:
        """记录一场“有实质进展但未入账 flag”的会话（编排加固）。

        深度逆向会话产出中间产物（emulator/解码脚本/patch 二进制）是真实进展，
        不应被 zero_flag 误判为零进展而止损 —— 这里重置 zero-flag 与 dry 窗口。
        （产物创建比“口头事实”难伪造；纯盲注逐字符只会产生查询输出、不会新增产物。）
        """
        def update(st: _ChallengeState) -> None:
            st.zero_flag_sessions = 0
            st.dry_sessions = 0
            st.post_hint_dry_sessions = 0

        self._mutate(code, update)

    def should_request_hint(self, code: str) -> bool:
        """是否到了多 flag 题的一次性提示复核点。

        这只是一个流程信号，不会读取或推断提示内容。提示前的 dry/zero
        窗口只能触发一次复核，不能直接把仍有部分进度的题切走。
        """
        st = self._read_state(code)
        if not st.multi_flag or st.hint_requested:
            return False
        return bool(
            st.dry_sessions >= max(1, self.dry_cutoff)
            or st.zero_flag_sessions >= max(1, self._zero_flag_limit(code))
        )

    def hint_requested(self, code: str) -> bool:
        """返回本题是否已经完成过一次提示复核（仅元数据）。"""
        return bool(self._read_state(code).hint_requested)

    def record_hint_requested(self, code: str) -> bool:
        """记录一次提示复核并重置干旱窗口。

        返回 ``True`` 仅表示本次调用首次登记；重复调用不会重新打开
        提示通道或篡改计数。提示正文由 driver 只在当前解题上下文中传递，
        不进入止损状态文件。
        """
        def update(st: _ChallengeState) -> bool:
            if st.hint_requested:
                return False
            st.hint_requested = True
            st.dry_sessions = 0
            st.zero_flag_sessions = 0
            st.post_hint_dry_sessions = 0
            # 若旧状态刚好在阈值边界，登记复核后不应保留旧的终止标志。
            if st.stop_reason.startswith(("stuck:", "zero_flag_sessions:")):
                st.stopped = False
                st.stop_reason = ""
            return True

        return self._mutate(code, update)

    def abandonment_evidence(self, code: str) -> dict:
        """返回可审计的、无答案内容的弃题证据快照。

        仅用于调度事件/测试；绝不包含 flag、题面、命令或提示正文。
        """
        st = self._read_state(code)
        return {
            "code": st.code,
            "terminal": bool(st.stopped),
            "reason": str(st.stop_reason or ""),
            "hint_requested": bool(st.hint_requested),
            "post_hint_dry_sessions": int(st.post_hint_dry_sessions or 0),
            "dry_sessions": int(st.dry_sessions or 0),
            "zero_flag_sessions": int(st.zero_flag_sessions or 0),
            "total_facts": int(st.total_facts or 0),
            "confirmed_flags": int(st.confirmed_flags or 0),
            "sessions": int(st.sessions or 0),
        }

    def record_unreachable(self, code: str) -> None:
        """记录目标不可达"""
        self._mutate(
            code, lambda st: setattr(st, "unreachable_visits", st.unreachable_visits + 1))

    def record_reachable(self, code: str) -> None:
        """记录目标可达 (重置不可达计数)"""
        self._mutate(code, lambda st: setattr(st, "unreachable_visits", 0))

    def grant_time(self, code: str, seconds: float) -> None:
        """B16：目标重启成功后补回时间预算。

        对"已宕机目标"的开销是基础设施时间，不记在 solver 头上；不补的话
        B16 的 continue 分支会被循环顶部的 should_stop 立刻掐死，新容器白起。
        每次重启最多补回一个会话的量（driver 侧 _TARGET_RESTART_GRACE 控制），
        重启次数本身由 target_restarts 限额约束。
        """
        amount = max(0.0, float(seconds))
        self._mutate(
            code,
            lambda st: setattr(st, "active_seconds", max(0.0, st.active_seconds - amount)),
        )

    def record_target_restart(self, code: str) -> None:
        """B16：记录一次"目标服务故障 → 重启题目容器"。"""
        now_wall = time.time()

        def update(st: _ChallengeState) -> None:
            st.target_restarts += 1
            st.target_restarts_total += 1          # B21：终身累计，跨访问不重置
            st.last_restart_wall = now_wall         # B21：冷却基准

        self._mutate(code, update)

    def target_restarts(self, code: str) -> int:
        """B16：本次访问已用掉的重启额度。"""
        return self._read_state(code).target_restarts

    def target_restarts_total(self, code: str) -> int:
        """B21：终身已重启次数（跨访问累计；revive 不重置）。"""
        return self._read_state(code).target_restarts_total

    def last_restart_wall(self, code: str) -> float:
        """B21：上次重启墙钟戳（0=从未重启）。"""
        return self._read_state(code).last_restart_wall

    def record_flag(self, code: str, flag: str = "") -> bool:
        """记录一次新的平台确认 flag，并按候选去重。

        多段题常见同一 flag 在 eager/main 两条路径或跨 worker 重复返回；
        旧实现每次都累加 ``flags_found``，造成 5/4 之类的假进度。只持久化
        SHA-256，不把 flag 明文写入止损状态；未传候选时保留旧调用兼容。
        返回值表示本次是否真的新增计数。
        """
        candidate = str(flag).strip()

        def update(st: _ChallengeState) -> bool:
            key = ""
            if candidate:
                key = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                if key in st.flag_hashes:
                    return False
            if key:
                st.flag_hashes.append(key)
            st.flags_found += 1
            # HTTP responses from concurrent submitters can be processed out
            # of order.  If another response already published a newer
            # cumulative platform count, adding one here would fabricate a
            # 4/3-style progress value.  The hash cardinality is the only safe
            # count-only fallback; record_platform_progress() immediately
            # below the submit path supplies the authoritative total when the
            # backend returns it.
            st.confirmed_flags = max(st.confirmed_flags, len(st.flag_hashes))
            st.last_flag_session = st.sessions
            st.dry_sessions = 0
            st.zero_flag_sessions = 0
            st.post_hint_dry_sessions = 0
            return True

        return self._mutate(code, update)

    def record_duplicate(self, code: str, flag: str = "") -> bool:
        """记录平台确认的重复 flag，但不把它计为本地新 flag。

        多 worker/跨访问时，平台可能返回 duplicate，说明该 flag 已由别处入账。
        这能证明本场有真实进展并重置连续空转计数，但若递增 ``flags_found`` 会
        产生虚假的 N/N 进度；哈希仍落盘用于后续去重。
        """
        candidate = str(flag).strip()
        if not candidate:
            return False

        def update(st: _ChallengeState) -> bool:
            key = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
            if key in st.flag_hashes:
                return False
            st.flag_hashes.append(key)
            # duplicate 只能证明“至少已有部分进展”，不能安全地按 +1 累加：
            # 若刚从平台同步了 correct_flag_count，这条 duplicate 很可能已经
            # 包含在那个总数里。hash 数提供无身份基线之外的额外下界。
            st.confirmed_flags = max(
                st.confirmed_flags, st.flags_found, len(st.flag_hashes))
            st.last_flag_session = st.sessions
            st.dry_sessions = 0
            st.zero_flag_sessions = 0
            st.post_hint_dry_sessions = 0
            return True

        return self._mutate(code, update)

    def record_platform_progress(self, code: str, confirmed_count: int) -> bool:
        """同步平台已确认的累计 flag 数，供多 flag 止损判断使用。

        该值来自题目列表/提交响应的 ``correct_flag_count``，不含 flag 明文。
        它不会改动 ``flags_found``，因此状态展示仍可区分“本 worker 新提交”与
        “平台此前已入账”。当累计数真正增加时才重置空转窗口，避免轮询同一个
        旧计数无限续命。
        """
        try:
            count = max(0, int(confirmed_count))
        except (TypeError, ValueError):
            return False

        def update(st: _ChallengeState) -> bool:
            known = max(st.confirmed_flags, st.flags_found, len(st.flag_hashes))
            if count <= known:
                return False
            st.confirmed_flags = count
            st.last_flag_session = st.sessions
            st.dry_sessions = 0
            st.zero_flag_sessions = 0
            st.post_hint_dry_sessions = 0
            return True

        return self._mutate(code, update)

    def flags_banked(self, code: str) -> int:
        """返回本 worker 新确认入账的 flag 数（不含平台历史基线）。"""
        return self._read_state(code).flags_found

    def should_stop(self, code: str) -> tuple[bool, str]:
        """
        判断是否应停止该题。

        返回: (should_stop, reason)
        """
        def evaluate(st: _ChallengeState) -> tuple[bool, str]:
            if st.stopped:
                return True, st.stop_reason

            # 时间预算
            budget = self.per_challenge_seconds
            if st.multi_flag:
                budget = int(budget * self.multi_flag_max_mult)
            # 时间预算按【累计活动时长】（访问段求和，轮间空闲不计——空闲是调度器
            # 在解别的题，不该折损本题预算）。在访时仅加本进程私有的当前访问段。
            elapsed = st.active_seconds
            if st.visit_begin_wall:
                elapsed += max(0.0, time.time() - st.visit_begin_wall)
            if elapsed > budget:
                st.stopped = True
                st.stop_reason = f"time_budget:{int(elapsed)}s>{budget}s"
                return True, st.stop_reason

            # 会话数上限（B13 两口径）
            if self.lifetime_sessions_cap > 0:
                used, cap = st.sessions_total, self.lifetime_sessions_cap
            else:
                used, cap = st.sessions, self.max_sessions
            if st.multi_flag:
                cap = int(cap * self.multi_flag_max_mult)
            if used > cap:
                st.stopped = True
                st.stop_reason = f"sessions:{used}>{cap}"
                return True, st.stop_reason

            # 连续无新事实。多 flag 题绝不能把“提示前达到 dry/zero 阈值”
            # 当成切题条件：那只是请求一次平台提示的信号。提示后也必须按
            # ``post_hint_dry_sessions`` 独立计数；不能再借用 dry_sessions，
            # 否则把 POST_HINT_DRY_SESSIONS 调小于普通阈值时仍会多跑几场，
            # 调大时又会出现语义不清的双重阈值。
            if st.multi_flag:
                if (st.hint_requested
                        and st.post_hint_dry_sessions >= self.post_hint_dry_cutoff):
                    st.stopped = True
                    st.stop_reason = (
                        f"stuck:post_hint_dry_sessions={st.post_hint_dry_sessions}")
                    return True, st.stop_reason
            else:
                if st.dry_sessions >= self.dry_cutoff:
                    st.stopped = True
                    st.stop_reason = f"stuck:dry_sessions={st.dry_sessions}"
                    return True, st.stop_reason

                # 连续 0 flag 会话 → 单 flag 题轮换（与“新事实”解耦）。
                # 多 flag 链的 zero 计数只用于触发提示；无论是否已得到首条
                # flag，都不能绕过“提示后完整复核窗口”的终止顺序。
                # hard 题用更宽松阈值（防御性逆向需要多轮试错）。
                if st.zero_flag_sessions >= self._zero_flag_limit(code):
                    st.stopped = True
                    st.stop_reason = (
                        f"zero_flag_sessions:{st.zero_flag_sessions}>="
                        f"{self._zero_flag_limit(code)}")
                    return True, st.stop_reason

            # 连续不可达
            if st.unreachable_visits >= self.unreachable_cutoff:
                st.stopped = True
                st.stop_reason = f"unreachable:{st.unreachable_visits}"
                return True, st.stop_reason

            return False, ""

        # evaluate 在锁内运行；即使本次没有触发 stop，也把别的 worker 最新
        # 计数作为快照保存，避免下次以本地旧缓存作判断。
        return self._mutate(code, evaluate)

    def remaining_seconds(self, code: str) -> int:
        """返回该题剩余时间预算"""
        st = self._read_state(code)
        budget = self.per_challenge_seconds
        if st.multi_flag:
            budget = int(budget * self.multi_flag_max_mult)
        elapsed = st.active_seconds
        if st.visit_begin_wall:
            elapsed += max(0.0, time.time() - st.visit_begin_wall)
        return max(0, int(budget - elapsed))

    def rearm_dry_window(self, code: str) -> None:
        """重置干旱窗口 (用于被挂起后重新恢复)"""
        def update(st: _ChallengeState) -> None:
            st.dry_sessions = 0
            st.post_hint_dry_sessions = 0
            st.stopped = False
            st.stop_reason = ""

        self._mutate(code, update)

    def end_visit(self, code: str) -> None:
        """结算本次访问的活动时长（solve_one finally 调用）。

        幂等：visit_begin_wall==0 时为 no-op（未开始过的访问/重复调用）。
        """
        with self._local_lock(code):
            visit_begin = self._visit_begins.get(code, 0.0)
            if not visit_begin:
                return
            now_wall = time.time()

            def update(st: _ChallengeState) -> None:
                st.active_seconds += max(0.0, now_wall - visit_begin)
                st.visit_begin_wall = 0.0

            self._mutate(code, update)
            self._visit_begins.pop(code, None)

    def last_revive_wall(self, code: str) -> float:
        """上次复活的墙钟时间（复活冷却基准）。

        旧状态文件无该字段（=0）时回退 start_wall：以本轮预算起点近似冷却起点，
        保守不给"从未复活"的老状态白送一次立即复活。
        """
        st = self._read_state(code)
        return st.last_revive_wall or st.start_wall or 0.0

    def revive(self, code: str) -> None:
        """复活一个被停止的题目 —— 给予一次全新尝试（fresh budget）。

        不仅清 stopped/dry/unreachable，还重置时间预算（start_wall/start_time）
        与 zero_flag 窗口；否则旧时间预算会让下一次 should_stop 因 time_budget
        立即再停（“复活失效”）。

        B13：本轮会话计数 sessions 也必须清零 —— 旧行为保留 sessions，复活后
        should_stop 立刻以 sessions:9>8 再停（实测某题复活 8ms 即被杀，随后
        派发机会耗尽被永久放弃）。终身总量改由 sessions_total 记账，仅当显式
        配置 lifetime_sessions_cap>0 时充当硬顶，防止无限重烧。
        flags_found 保留不动（仅作统计/多 flag 判定）。
        """
        now_wall = time.time()
        with self._local_lock(code):
            self._visit_begins.pop(code, None)

            def update(st: _ChallengeState) -> None:
                st.stopped = False
                st.stop_reason = ""
                st.sessions = 0
                st.dry_sessions = 0
                st.unreachable_visits = 0
                st.zero_flag_sessions = 0
                # 复活代表一次新的完整解题机会；多 flag 题可再次经过一次
                # 提示复核，但既有平台确认进度仍保留。
                st.hint_requested = False
                st.post_hint_dry_sessions = 0
                # B21：target_restarts_total 故意不重置 —— 复活只给"解题机会"，
                # 不给"无限重启额度"；重启是有限手段，累计封顶后由止损周期接管。
                st.start_time = time.monotonic()
                st.start_wall = now_wall
                st.last_revive_wall = now_wall
                st.active_seconds = 0.0
                st.visit_begin_wall = 0.0

            self._mutate(code, update)
