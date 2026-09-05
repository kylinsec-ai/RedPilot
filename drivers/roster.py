"""drivers/roster.py — 监视台数据层（只读，stdlib）。

两个独立职责：
1. RosterPoller —— 后台线程周期拉平台题目总览（官方 SDK sync 客户端）并叠加本地
   work/ 扫描（FLAG/transcript/crash/产物痕迹），原子落盘 /work/.live/roster.json。
   失败保旧缓存 + stale/platform_error 标记；缺 BENCHMARK_* 环境时优雅禁用
   （仅本地痕迹视图）。任何异常只记日志——绝不反向影响求解线程。
2. TranscriptDigest —— 把 pi 原生 JSONL transcript 增量解析为"人读时间线"紧凑条目。
   message_update/toolcall_delta 之类逐 token 增量必须折叠（实测 127MB/7.6k 行原始文件
   合流后仅 ~1k 条）；只按字节偏移续读；>5MB 重试截断检测重置；崩溃无终止标记 →
   abrupt 元信息。缓存落 /work/.live/digests/<code>.json。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger("adapter.roster")

_SAFE_RX_IMPORT = None


def _safe_code(code: str) -> str:
    """与 driver 一致的 code->目录映射（sanitize+hash 后缀）；失败回退纯 sanitize"""
    global _SAFE_RX_IMPORT
    try:
        from drivers.benchmark_driver import _safe_code as sc
        _SAFE_RX_IMPORT = sc
        return sc(code)
    except Exception:
        pass
    import hashlib
    import re
    raw = str(code)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
    return safe if safe == raw else f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


def atomic_write_json(path: str, obj) -> bool:
    """tmp + os.replace 原子写（沿用 adapter/live/state.py 的落地模式）。成功返回 True。"""
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        log.debug("atomic_write_json failed: %s", path)
        return False


# ── 本地目录扫描 ─────────────────────────────────────────────

_SKIP_NAMES = {".", "..", "__pycache__", ".live"}


def _dir_maps_to_code(dirname: str) -> Optional[str]:
    """目录名是否可能是某 code 的 _safe_code 映射（可逆的纯 sanitize 名返回原名）"""
    # 带 hash 后缀的映射名无法反解真实 code -> 返回 None（上层按目录名当本地痕迹兜底）
    import re
    if not dirname or dirname.startswith((".", "_")) or dirname in _SKIP_NAMES:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", dirname):
        return dirname
    return None


def _scan_artifacts(dpath: str, max_files: int = 20) -> list[str]:
    """非 dot/非 _ 开头的普通文件（transcript/FLAG/CLAUDE.md/core.* 单列，不入此表）"""
    out: list[str] = []
    try:
        for name in sorted(os.listdir(dpath)):
            if name.startswith((".", "_")):
                continue
            if name in ("transcript.jsonl", "FLAG", "CLAUDE.md") or name.startswith("core."):
                continue
            p = os.path.join(dpath, name)
            if os.path.isfile(p) and os.path.getsize(p) <= 500_000:
                out.append(name)
            if len(out) >= max_files:
                break
    except OSError:
        pass
    return out


def scan_local_dir(workdir: str, dirname: str) -> dict:
    """单个题目目录的本地痕迹（stat 级，不读大文件）"""
    dpath = os.path.join(workdir, dirname)
    out: dict = {
        "dir": dirname,
        "flag": False,
        "transcript_bytes": 0,
        "transcript_mtime": 0.0,
        "crashed": False,
        "artifacts": [],
        "last_activity": 0.0,
    }
    try:
        for name in os.listdir(dpath):
            p = os.path.join(dpath, name)
            if not os.path.isfile(p):
                continue
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            if name == "FLAG":
                out["flag"] = True
            elif name == "transcript.jsonl":
                out["transcript_bytes"] = os.path.getsize(p)
                out["transcript_mtime"] = mtime
            elif name.startswith("core."):
                out["crashed"] = True
            out["last_activity"] = max(out["last_activity"], mtime)
        out["artifacts"] = _scan_artifacts(dpath)
    except OSError:
        pass
    return out


def scan_local(workdir: str) -> dict[str, dict]:
    """扫描 workdir 顶层全部题目目录 -> {code: local}（code 用可反解目录名）"""
    result: dict[str, dict] = {}
    try:
        for name in sorted(os.listdir(workdir)):
            if name in _SKIP_NAMES or name.startswith((".", "_")):
                continue
            if not os.path.isdir(os.path.join(workdir, name)):
                continue
            code = _dir_maps_to_code(name)
            if code is None:
                continue  # hash 后缀映射名无法归属平台题号，跳过（平台行会自带映射）
            result[code] = scan_local_dir(workdir, name)
    except OSError:
        pass
    return result


def challenge_detail(workdir: str, code: str, platform_row: Optional[dict]) -> dict:
    """单题详情：平台行 + 本地痕迹 + FLAG 内容 + 文件明细。code 需先经调用方守卫。"""
    dpath = os.path.join(workdir, _safe_code(code))
    local = scan_local_dir(workdir, os.path.basename(dpath)) if os.path.isdir(dpath) else {}
    flags: list[str] = []
    transcript_samples: list[str] = []
    fp = os.path.join(dpath, "FLAG")
    if os.path.isfile(fp):
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                for i, ln in enumerate(f):
                    if i >= 50:
                        break
                    s = ln.strip()
                    if s:
                        flags.append(s)
        except OSError:
            pass
    row = dict(platform_row or {})
    row.update({"unique_code": code, "local": local, "flags": flags})
    return row


# ── RosterPoller ─────────────────────────────────────────────

_ROSTER_PATH = None  # 由构造参数定


def _client_from_env():
    """惰性建 sync SDK 客户端；缺环境/未装包返回 None"""
    base = os.getenv("BENCHMARK_BASE_URL", "").strip()
    token = os.getenv("BENCHMARK_TOKEN", "").strip()
    if not base or not token:
        return None
    try:
        from tsec_benchmark import TSecBenchmark
        return TSecBenchmark(base_url=base, token=token, auto_check_vpn=False)
    except Exception:
        log.exception("tsec_benchmark unavailable; roster platform poll disabled")
        return None


class RosterPoller:
    """60s 周期：平台 list_challenges + 本地扫描 -> 原子落 roster.json。

    平台行字段：unique_code/difficulty/level/total_score/flag_count/
    correct_flag_count/is_completed/container_status/container_addr/description。
    每行再叠加本地痕迹 local。纯只读，异常隔离。
    """

    def __init__(self, workdir: str, *, interval: float = 60.0):
        self._workdir = workdir
        self._interval = interval
        self._path = os.path.join(workdir, ".live", "roster.json")
        self._lock = threading.Lock()
        self._snap = {
            "fetched_at": 0.0,
            "stale": True,
            "platform_error": "",
            "platform_disabled": False,
            "challenges": {},
        }
        self._client = None

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

    def poll_once(self) -> None:
        """一次拉取+落盘；平台失败保旧缓存并记 stale。"""
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
        # 本地痕迹叠加到当前快照（平台失败时也刷本地——保留旧平台行）
        with self._lock:
            challenges = dict(self._snap["challenges"])
        for code, lc in local.items():
            row = challenges.setdefault(code, {"unique_code": code, "local_only": True,
                                               "description": "", "difficulty": "",
                                               "total_score": 0, "flag_count": 0,
                                               "correct_flag_count": 0,
                                               "is_completed": False,
                                               "container_status": "",
                                               "container_addr": [], "level": 0})
            row["local"] = lc
        with self._lock:
            self._snap["challenges"] = challenges
        atomic_write_json(self._path, self._snap)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    def _poll_local_only(self) -> None:
        """平台不可用/被禁时：至少刷一次本地痕迹视图（platform 行清空）"""
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
            challenges[code] = {"unique_code": code, "local_only": True, "description": "",
                                "difficulty": "", "total_score": 0, "flag_count": 0,
                                "correct_flag_count": 0, "is_completed": False,
                                "container_status": "", "container_addr": [], "level": 0,
                                "local": lc}
        with self._lock:
            self._snap["challenges"] = challenges
        atomic_write_json(self._path, self._snap)

    def _loop(self) -> None:
        while True:
            try:
                if self._client is None:
                    self._client = _client_from_env()
                    if self._client is None:
                        log.info("roster platform poll disabled (BENCHMARK_* unset or SDK missing)")
                        self._poll_local_only()
                        return
                self.poll_once()
            except Exception:
                log.exception("roster poll loop error")
            time.sleep(self._interval)

    def start(self) -> threading.Thread | None:
        t = threading.Thread(target=self._loop, daemon=True, name="roster-poller")
        t.start()
        return t


# ── TranscriptDigest ─────────────────────────────────────────
# 条目 schema（seq 单调，UI 按 seq 增量拉取）：
#   {seq, kind: session|attempt|turn|tool|text|note|error,
#    t(epoch ms 近似), turn, tool, cmd, out, err(bool), text, note,
#    stop, tokens, sid(会话 id 前 6 位), n(序号)}
# meta: {sessions, agent_ends, truncated, abrupt, live, dropped, next_seq}

_ENTRY_CAP = 20000       # 单 code 条目上限：超出丢旧半，meta.dropped=true
_PERSIST_MIN_GAP = 8.0   # 状态落盘节流（秒）
_PERSIST_MIN_NEW = 200   # 或新增条目达到该数即落盘
_TEXT_FLUSH = 1500       # 流式文本缓冲阈值（字符）


def _iso_to_ms(iso: str) -> Optional[float]:
    try:
        s = iso
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp() * 1000.0
    except Exception:
        return None


def _oneline(args: dict, max_len: int = 300) -> str:
    """工具参数 -> 单行命令/摘要（复用 adapter 的脱敏 summarize_args）"""
    try:
        from adapter.live.state import summarize_args
        if isinstance(args, dict) and isinstance(args.get("command"), str):
            cmd = " ".join(args["command"].split())
            return cmd if len(cmd) <= max_len else cmd[:max_len] + "…"
        return summarize_args(args or {}, max_len=max_len)
    except Exception:
        import json as _json
        try:
            s = _json.dumps(args or {}, ensure_ascii=False)
        except Exception:
            s = str(args)
        return s if len(s) <= max_len else s[:max_len] + "…"


def _result_tail(result, tail_len: int = 2000) -> tuple[str, int]:
    """tool_execution_end.result.content[].text 拼接后的尾部 + 全长度"""
    parts: list[str] = []
    total = 0
    try:
        for block in (result or {}).get("content", []):
            text = block.get("text") or block.get("thinking") or ""
            if isinstance(text, str):
                parts.append(text)
                total += len(text)
    except Exception:
        pass
    joined = "\n".join(parts)
    return (joined if len(joined) <= tail_len else "…" + joined[-tail_len:]), total


class TranscriptDigest:
    """增量解析器：按 code 维护内存态 + 侧车缓存，供 /api/timeline 轮询。"""

    def __init__(self, workdir: str):
        self._workdir = workdir
        self._cache_dir = os.path.join(workdir, ".live", "digests")
        self._reg: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── 内部状态 ──

    def _fresh_state(self, code: str) -> dict:
        return {
            "code": code,
            "offset": 0, "truncated": False, "dropped": False,
            "sessions": 0, "agent_ends": 0, "unparsed": 0,
            "entries": [], "next_seq": 0, "turn": 0,
            "last_ts": None,
            # 跨 parse 折叠态
            "text_open": False, "text_buf": "", "last_turn_entry": None,
            "tools": {},        # toolCallId -> 条目 dict 引用
            "persisted_at": 0.0, "persisted_entries": 0, "persist_ok": True,
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
            st.update({k: data[k] for k in ("offset", "truncated", "sessions", "agent_ends",
                                            "unparsed", "next_seq") if k in data})
            st["entries"] = data.get("entries", [])
            st["dropped"] = bool(data.get("dropped"))
            st["turn"] = int(data.get("turn") or 0)
            st["last_ts"] = data.get("last_ts")
        except (OSError, ValueError):
            pass  # 无缓存/损坏：从 0 开始

    def _persist(self, code: str, st: dict, force: bool = False) -> None:
        if not st.get("persist_ok", True):
            return  # 已确认不可写（如宿主直跑、work/ root 属主）——保持内存态即可
        now = time.monotonic()
        if not force and (now - st["persisted_at"] < _PERSIST_MIN_GAP
                          and st["next_seq"] - st["persisted_entries"] < _PERSIST_MIN_NEW):
            return
        st["persisted_at"] = now
        st["persisted_entries"] = st["next_seq"]
        p = os.path.join(self._cache_dir, f"{code}.json")
        ok = atomic_write_json(p, {
            "offset": st["offset"], "truncated": st["truncated"], "dropped": st["dropped"],
            "sessions": st["sessions"], "agent_ends": st["agent_ends"],
            "unparsed": st["unparsed"], "next_seq": st["next_seq"],
            "turn": st["turn"], "last_ts": st["last_ts"],
            "entries": st["entries"][-8000:],
        })
        if not ok:
            st["persist_ok"] = False
            log.warning("digest cache dir not writable (%s) — keeping state in memory", p)

    # ── 条目构建 ──

    def _add(self, st: dict, kind: str, **fields) -> dict:
        """追加条目并返回其 dict 引用（调用方持有引用即可后续补字段）"""
        seq = st["next_seq"]
        st["next_seq"] += 1
        entry: dict = {"seq": seq, "kind": kind, "t": st["last_ts"], "turn": st.get("turn")}
        entry.update({k: v for k, v in fields.items() if v is not None})
        st["entries"].append(entry)
        if len(st["entries"]) > _ENTRY_CAP:
            st["entries"] = st["entries"][-_ENTRY_CAP // 2:]
            st["dropped"] = True
        return entry

    def _flush_text(self, st: dict) -> None:
        buf = st.get("text_buf", "").strip()
        if not buf:
            return
        more = len(buf) > 1200
        self._add(st, "text", text=(buf[:1200] + "…" if more else buf), more=more)
        st["text_buf"] = ""

    def _touch_ts(self, st: dict, raw_ts) -> None:
        if raw_ts is None:
            return
        try:
            ms = float(raw_ts)
            if ms < 1e12:      # 秒级 -> 毫秒
                ms *= 1000.0
            st["last_ts"] = ms
        except (TypeError, ValueError):
            pass

    # ── 解析 ──

    def _parse_lines(self, st: dict, path: str) -> None:
        """从 st.offset 续读全部新行并合流。调用方持 per-code 锁。"""
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            if size < st["offset"]:
                # 文件被重截断（>5MB 重试清空 / 人工清理）
                st["offset"] = 0
                st["entries"] = []
                st["next_seq"] = 0
                st["truncated"] = True
                st["text_buf"] = ""
                st["tools"] = {}
                st["sessions"] = 0
                st["agent_ends"] = 0
                st["turn"] = 0
                self._add(st, "note", note="transcript 被重截断（5MB 轮换或重试清空），历史从头重新解析")
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
                    st["unparsed"] += 1
                    continue
                self._feed(st, ev)
            self._flush_text(st)

    def _feed(self, st: dict, ev: dict) -> None:
        kind = ev.get("type", "")
        try:
            if kind == "_attempt":
                self._add(st, "attempt", n=ev.get("attempt"))
            elif kind == "session":
                iso = ev.get("timestamp") or ""
                ms = _iso_to_ms(iso) if iso else None
                if ms:
                    st["last_ts"] = ms
                sid = str(ev.get("id") or "")
                self._add(st, "session", sid=(sid[:6] if sid else None),
                          note=f"cwd: {ev.get('cwd') or ''}" or None)
                st["sessions"] += 1
                st["text_buf"] = ""
                st["tools"] = {}
            elif kind == "agent_start":
                pass
            elif kind == "agent_end":
                st["agent_ends"] += 1
                self._add(st, "note", note="agent 正常结束")
            elif kind == "turn_start":
                st["turn"] = int(st.get("turn") or 0) + 1
                st["last_turn_entry"] = self._add(st, "turn", n=st["turn"])
            elif kind == "turn_end":
                msg = ev.get("message") or {}
                usage = (msg.get("usage") or {})
                tokens = usage.get("totalTokens") if isinstance(usage, dict) else None
                stop = msg.get("stopReason")
                tgt = st.get("last_turn_entry")
                if tgt is not None:
                    if tokens:
                        tgt["tokens"] = tokens
                    if stop:
                        tgt["stop"] = stop
                st["text_buf"] = ""
            elif kind == "message_start":
                msg = ev.get("message") or {}
                self._touch_ts(st, msg.get("timestamp"))
                role = msg.get("role")
                if role == "user":
                    self._add(st, "note", note="任务输入（提示词）就绪")
            elif kind == "message_update":
                msg = ev.get("message") or {}
                self._touch_ts(st, msg.get("timestamp"))
                sub = (ev.get("assistantMessageEvent") or {}).get("type", "")
                delta = ev.get("assistantMessageEvent") or {}
                if sub == "text_start":
                    st["text_open"] = True
                    st["text_buf"] = ""
                elif sub == "text_delta":
                    if st.get("text_open"):
                        st["text_buf"] = (st.get("text_buf") or "") + str(delta.get("delta") or "")
                        if len(st["text_buf"]) > _TEXT_FLUSH:
                            self._flush_text(st)
                elif sub == "text_end":
                    self._flush_text(st)
                    st["text_open"] = False
                # thinking_*/toolcall_* 增量：不渲染，只认总量级（无独立时间戳）
            elif kind == "message_end":
                msg = ev.get("message") or {}
                self._touch_ts(st, msg.get("timestamp"))
                if (msg.get("role")) == "assistant":
                    # 压缩后的 transcript 无 message_update，text_buf 为空；
                    # 此时从 message_end 的 content 直接提取文本
                    if not st.get("text_buf"):
                        for c in (msg.get("content") or []):
                            if c.get("type") == "text" and c.get("text"):
                                st["text_buf"] = c["text"]
                                break
                    self._flush_text(st)
            elif kind == "tool_execution_start":
                args = ev.get("args") or {}
                entry = self._add(st, "tool", tool=ev.get("toolName"),
                                  cmd=_oneline(args if isinstance(args, dict) else {}),
                                  id=str(ev.get("toolCallId") or ""))
                st["tools"][str(ev.get("toolCallId") or "")] = entry
            elif kind == "tool_execution_update":
                pass  # 进度增量不渲染（结果以 end 为准）
            elif kind == "tool_execution_end":
                tid = str(ev.get("toolCallId") or "")
                entry = st["tools"].pop(tid, None)
                result = ev.get("result") or {}
                tail, total = _result_tail(result)
                if entry is None:
                    entry = self._add(st, "tool", tool=ev.get("toolName"),
                                      id=tid, out=None if not total else tail,
                                      err=bool(ev.get("isError")), out_len=total or None)
                else:
                    entry["err"] = bool(ev.get("isError"))
                    if total:
                        entry["out"] = tail
                        entry["out_len"] = total
            elif kind == "error":
                self._add(st, "error", note=str(ev.get("message") or ev)[:400])
            # 其余未知类型：静默跳过（计数由 unparsed 外的未知不做统计，避免噪声）
        except Exception:
            log.debug("digest feed error on %s: %r", st.get("code"), kind)

    # ── 对外：增量取条目 ──

    def timeline(self, code: str, *, after: int = 0, live: bool = False) -> dict:
        """返回 seq>after 的新条目 + meta。首次调用会全量解析（秒级）。"""
        st = self._state(code)
        with st.setdefault("lock", threading.Lock()):
            path = os.path.join(self._workdir, _safe_code(code), "transcript.jsonl")
            if os.path.isfile(path):
                if not st.get("loaded_cache"):
                    self._load_cache(code, st)
                    st["loaded_cache"] = True
                self._parse_lines(st, path)
            # seq 从 0 起；客户端始终传上一轮的 next_seq(=已见条目数)，故用 >=
            entries = [e for e in st["entries"] if e["seq"] >= after]
            abrupt = bool(st["sessions"]) and st["agent_ends"] < st["sessions"] and not live
            meta = {
                "sessions": st["sessions"],
                "agent_ends": st["agent_ends"],
                "truncated": bool(st["truncated"]),
                "dropped": bool(st["dropped"]),
                "abrupt": abrupt,
                "live": bool(live),
                "bytes": st["offset"],
                "unparsed": st["unparsed"],
            }
            if os.path.isfile(path):
                self._persist(code, st)
            return {"next_seq": st["next_seq"], "meta": meta, "entries": entries}
