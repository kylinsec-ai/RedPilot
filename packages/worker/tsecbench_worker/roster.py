"""worker 题目总览数据层(只读,stdlib;由 drivers/roster.py 拆分而来)。

职责:
1. RosterPoller —— 后台线程周期拉平台题目总览(官方 SDK sync 客户端)并叠加本地
   work/ 扫描(FLAG/transcript/crash/产物痕迹),原子落盘 /work/.live/roster.json。
   失败保旧缓存 + stale/platform_error 标记;缺 BENCHMARK_* 环境时优雅禁用
   (仅本地痕迹视图)。任何异常只记日志——绝不反向影响求解线程。
2. TranscriptDigest —— pi 原生 JSONL transcript 增量解析为"人读时间线"紧凑条目的
   I/O 壳:字节偏移续读、>5MB 重试截断检测、sidecar 缓存(/work/.live/digests/<code>.json)。
   事件解释(折叠状态机)全部委托 tsecbench_contracts.digest.FoldState —— 唯一语义源,
   与 obs 平台侧 fold_rows 共用同一实现(fold-parity 测试钉死)。

本地目录扫描/FLAG 读法/单行模板在 obs.localserver(纯 stdlib 注入侧);
此处从那里导入,保证两处读到的目录语义一致。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from tsecbench_contracts.digest import FoldState
from tsecbench_contracts.fsio import atomic_write_json
from tsecbench_contracts.paths import (DIGESTS_DIR, FLAG_FILES, LIVE_DIR,
                                       ROSTER_FILENAME, safe_code)
from tsecbench_contracts.text import ERROR_HEAD_MAX

from obs.localserver import (challenge_detail, empty_roster_snapshot,
                             local_challenge_row, read_flag_lines, scan_local,
                             scan_local_dir)

log = logging.getLogger("tsecbench_worker.roster")

__all__ = ["RosterPoller", "TranscriptDigest", "read_flag_lines", "scan_local",
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
            from tsec_benchmark import TSecBenchmark
            self._client = TSecBenchmark(base_url=base, token=token, auto_check_vpn=False)
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


# ── TranscriptDigest(I/O 壳;状态机单源 contracts.FoldState) ──
# 条目 schema(seq 单调,UI 按 seq 增量拉取):
#   {seq, kind: session|attempt|turn|tool|text|note|error, t, turn, tool, cmd,
#    out, err(bool), text, note, stop, tokens, sid, n}
# meta: {sessions, agent_ends, truncated, abrupt, live, dropped, bytes, unparsed}

_PERSIST_MIN_GAP = 8.0   # 状态落盘节流(秒)
_PERSIST_MIN_NEW = 200   # 或新增条目达到该数即落盘


class TranscriptDigest:
    """增量解析器:按 code 维护内存态 + 侧车缓存,供 /api/timeline 轮询。

    事件解释委托 FoldState(contracts 唯一状态机);本类只管:
    字节偏移续读、>5MB 重试截断检测、sidecar 持久化(节流:8s 或 200 新条目)。
    """

    def __init__(self, workdir: str):
        self._workdir = workdir
        self._cache_dir = os.path.join(workdir, LIVE_DIR, DIGESTS_DIR)
        self._reg: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── 内部状态 ──

    def _fresh_state(self, code: str) -> dict:
        return {
            "code": code,
            "offset": 0, "truncated": False,
            "persisted_at": 0.0, "persisted_entries": 0, "persist_ok": True,
            "loaded_cache": False,
            "fold": FoldState(),
        }

    def _state(self, code: str) -> dict:
        with self._lock:
            st = self._reg.get(code)
            if st is None:
                st = self._fresh_state(code)
                self._reg[code] = st
            return st

    def _load_cache(self, code: str, st: dict) -> None:
        p = os.path.join(self._cache_dir, f"{code}.json")
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            fold: FoldState = st["fold"]
            st["offset"] = int(data.get("offset") or 0)
            st["truncated"] = bool(data.get("truncated"))
            fold.sessions = int(data.get("sessions") or 0)
            fold.agent_ends = int(data.get("agent_ends") or 0)
            fold.unparsed = int(data.get("unparsed") or 0)
            fold.next_seq = int(data.get("next_seq") or 0)
            fold.entries = data.get("entries", [])
            fold.dropped = bool(data.get("dropped"))
            fold.turn = int(data.get("turn") or 0)
            fold.last_ts = data.get("last_ts")
        except (OSError, ValueError):
            pass  # 无缓存/损坏:从 0 开始

    def _persist(self, code: str, st: dict, force: bool = False) -> None:
        if not st.get("persist_ok", True):
            return  # 已确认不可写(如宿主直跑、work/ root 属主)——保持内存态即可
        fold: FoldState = st["fold"]
        now = time.monotonic()
        if not force and (now - st["persisted_at"] < _PERSIST_MIN_GAP
                          and fold.next_seq - st["persisted_entries"] < _PERSIST_MIN_NEW):
            return
        st["persisted_at"] = now
        st["persisted_entries"] = fold.next_seq
        p = os.path.join(self._cache_dir, f"{code}.json")
        ok = atomic_write_json(p, {
            "offset": st["offset"], "truncated": st["truncated"], "dropped": fold.dropped,
            "sessions": fold.sessions, "agent_ends": fold.agent_ends,
            "unparsed": fold.unparsed, "next_seq": fold.next_seq,
            "turn": fold.turn, "last_ts": fold.last_ts,
            "entries": fold.entries[-8000:],
        })
        if not ok:
            st["persist_ok"] = False
            log.warning("digest cache dir not writable (%s) — keeping state in memory", p)

    # ── 解析 ──

    def _parse_lines(self, st: dict, path: str) -> None:
        """从 st.offset 续读全部新行并合流。调用方持 per-code 锁。"""
        fold: FoldState = st["fold"]
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            if size < st["offset"]:
                # 文件被重截断(>5MB 重试清空 / 人工清理)
                st["offset"] = 0
                fold.entries = []
                fold.next_seq = 0
                st["truncated"] = True
                fold.text_buf = ""
                fold.tools = {}
                fold.sessions = 0
                fold.agent_ends = 0
                fold.turn = 0
                fold._add("note", note="transcript 被重截断（5MB 轮换或重试清空），历史从头重新解析")
            f.seek(st["offset"])
            if size == st["offset"]:
                return
            for raw in f:
                st["offset"] = f.tell()
                line = raw.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    fold.unparsed += 1
                    continue
                fold.feed(ev)
            fold.flush_text()

    # ── 对外:增量取条目 ──

    def timeline(self, code: str, *, after: int = 0, live: bool = False) -> dict:
        """返回 seq>after 的新条目 + meta。首次调用会全量解析(秒级)。"""
        st = self._state(code)
        with st.setdefault("lock", threading.Lock()):
            fold: FoldState = st["fold"]
            path = os.path.join(self._workdir, safe_code(code), "transcript.jsonl")
            if os.path.isfile(path):
                if not st.get("loaded_cache"):
                    self._load_cache(code, st)
                    st["loaded_cache"] = True
                self._parse_lines(st, path)
            # seq 从 0 起;客户端始终传上一轮的 next_seq(=已见条目数),故用 >=
            truncated = bool(st["truncated"])
            if after == 0 and truncated:
                # 全量同步(初始加载/截断恢复)即消费截断标记:只出现一次,
                # 增量客户端按它重建本地条目后,后续轮询恢复增量语义;
                # 否则压缩/重截断后的每次轮询都会被当成"又要全量重放"
                st["truncated"] = False
            entries = [e for e in fold.entries if e["seq"] >= after]
            meta = fold.meta(live=live, truncated=truncated, extra={"bytes": st["offset"]})
            if os.path.isfile(path):
                self._persist(code, st)
            return {"next_seq": fold.next_seq, "meta": meta, "entries": entries}
