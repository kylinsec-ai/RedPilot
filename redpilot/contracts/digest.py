"""transcript 事件折叠状态机 — "人读时间线"的单一语义源。

此前 workers/roster.py TranscriptDigest(已删,未接线)与 obs/digest.py
(DB 事件行全量重折叠用)是两台必须手工同步的状态机;现归一:本模块持唯一语义,
消费方为 obs 平台侧 fold_rows(无状态全量折叠)。

条目 schema(seq 单调,UI 按 seq 增量拉取):
  {seq, kind: session|attempt|turn|tool|text|note|error,
   t(epoch ms 近似), turn, tool, cmd, out, err(bool), text, note,
   stop, tokens, sid(会话 id 前 6 位), n(序号)}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable, Optional

from .redact import summarize_args

log = logging.getLogger("redpilot.contracts.digest")

ENTRY_CAP = 20000   # 单 code 条目上限:超出丢旧半,meta.dropped=true
TEXT_FLUSH = 1500   # 流式文本缓冲阈值(字符)
TEXT_ENTRY_MAX = 1200


def iso_to_ms(iso: str) -> Optional[float]:
    try:
        # py3.11+ fromisoformat 直接接受尾部 Z;3.10 需先换成 +00:00(大小写均处理,仅替换尾部一个字符)
        if iso.endswith(("Z", "z")):
            iso = iso[:-1] + "+00:00"
        return datetime.fromisoformat(iso).timestamp() * 1000.0
    except Exception:
        return None


def oneline(args, max_len: int = 300) -> str:
    """工具参数 -> 单行命令/摘要(summarize_args 已兜底截断/脱敏)"""
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        cmd = " ".join(args["command"].split())
        return cmd if len(cmd) <= max_len else cmd[:max_len] + "…"
    return summarize_args(args or {}, max_len=max_len)


def result_tail(result, tail_len: int = 2000) -> tuple[str, int]:
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


class FoldState:
    """一次折叠的状态(纯内存,无 I/O)。

    平属性 entries/next_seq/sessions/agent_ends/unparsed/turn/last_ts 供
    obs fold_rows 全量重折叠与增量客户端的 next_seq 续读使用。
    """

    __slots__ = ("entries", "next_seq", "sessions", "agent_ends", "unparsed",
                 "turn", "last_ts", "text_open", "text_buf", "last_turn_entry",
                 "tools", "dropped")

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.next_seq: int = 0
        self.sessions: int = 0
        self.agent_ends: int = 0
        self.unparsed: int = 0
        self.turn: int = 0
        self.last_ts: float | None = None
        # 跨事件折叠态
        self.text_open: bool = False
        self.text_buf: str = ""
        self.last_turn_entry: dict | None = None
        self.tools: dict[str, dict] = {}  # toolCallId -> 条目 dict 引用
        self.dropped: bool = False

    # ── 状态机内部 ──

    def _add(self, kind: str, **fields) -> dict:
        """追加条目并返回其 dict 引用(调用方持有引用即可后续补字段)"""
        seq = self.next_seq
        self.next_seq += 1
        entry: dict = {"seq": seq, "kind": kind, "t": self.last_ts, "turn": self.turn}
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.entries.append(entry)
        if len(self.entries) > ENTRY_CAP:
            self.entries = self.entries[-ENTRY_CAP // 2:]
            self.dropped = True
        return entry

    def flush_text(self) -> None:
        buf = self.text_buf.strip()
        if not buf:
            return
        more = len(buf) > TEXT_ENTRY_MAX
        self._add("text", text=(buf[:TEXT_ENTRY_MAX] + "…" if more else buf), more=more)
        self.text_buf = ""

    def _touch_ts(self, raw_ts) -> None:
        if raw_ts is None:
            return
        try:
            ms = float(raw_ts)
            if ms < 1e12:      # 秒级 -> 毫秒
                ms *= 1000.0
            self.last_ts = ms
        except (TypeError, ValueError):
            pass

    def feed(self, ev: dict) -> None:
        """消费一条 pi 原生事件(未知类型静默跳过;异常只记日志不打断调用方)。"""
        kind = ev.get("type", "")
        try:
            self._feed(kind, ev)
        except Exception:
            log.debug("digest feed error on %s: %r", kind, ev, exc_info=True)

    def _feed(self, kind: str, ev: dict) -> None:
        if kind == "_attempt":
            self._add("attempt", n=ev.get("attempt"))
        elif kind == "session":
            iso = ev.get("timestamp") or ""
            ms = iso_to_ms(iso) if iso else None
            if ms:
                self.last_ts = ms
            sid = str(ev.get("id") or "")
            self._add("session", sid=(sid[:6] if sid else None),
                      note=f"cwd: {ev.get('cwd') or ''}" or None)
            self.sessions += 1
            self.text_buf = ""
            self.tools = {}
        elif kind == "agent_start":
            pass
        elif kind == "agent_end":
            self.agent_ends += 1
            self._add("note", note="agent 正常结束")
        elif kind == "turn_start":
            self.turn = int(self.turn or 0) + 1
            self.last_turn_entry = self._add("turn", n=self.turn)
        elif kind == "turn_end":
            msg = ev.get("message") or {}
            usage = (msg.get("usage") or {})
            tokens = usage.get("totalTokens") if isinstance(usage, dict) else None
            stop = msg.get("stopReason")
            tgt = self.last_turn_entry
            if tgt is not None:
                if tokens:
                    tgt["tokens"] = tokens
                if stop:
                    tgt["stop"] = stop
            self.text_buf = ""
        elif kind == "message_start":
            msg = ev.get("message") or {}
            self._touch_ts(msg.get("timestamp"))
            if msg.get("role") == "user":
                self._add("note", note="任务输入（提示词）就绪")
        elif kind == "message_update":
            msg = ev.get("message") or {}
            self._touch_ts(msg.get("timestamp"))
            sub = (ev.get("assistantMessageEvent") or {}).get("type", "")
            delta = ev.get("assistantMessageEvent") or {}
            if sub == "text_start":
                self.text_open = True
                self.text_buf = ""
            elif sub == "text_delta":
                if self.text_open:
                    self.text_buf = (self.text_buf or "") + str(delta.get("delta") or "")
                    if len(self.text_buf) > TEXT_FLUSH:
                        self.flush_text()
            elif sub == "text_end":
                self.flush_text()
                self.text_open = False
            # thinking_*/toolcall_* 增量:不渲染,只认总量级(无独立时间戳)
        elif kind == "message_end":
            msg = ev.get("message") or {}
            self._touch_ts(msg.get("timestamp"))
            if msg.get("role") == "assistant":
                # 压缩后的 transcript 无 message_update,text_buf 为空;
                # 此时从 message_end 的 content 直接提取文本
                if not self.text_buf:
                    for c in (msg.get("content") or []):
                        if c.get("type") == "text" and c.get("text"):
                            self.text_buf = c["text"]
                            break
                self.flush_text()
        elif kind == "tool_execution_start":
            args = ev.get("args") or {}
            entry = self._add("tool", tool=ev.get("toolName"),
                              cmd=oneline(args if isinstance(args, dict) else {}),
                              id=str(ev.get("toolCallId") or ""))
            self.tools[str(ev.get("toolCallId") or "")] = entry
        elif kind == "tool_execution_update":
            pass  # 进度增量不渲染(结果以 end 为准)
        elif kind == "tool_execution_end":
            tid = str(ev.get("toolCallId") or "")
            entry = self.tools.pop(tid, None)
            result = ev.get("result") or {}
            tail, total = result_tail(result)
            if entry is None:
                entry = self._add("tool", tool=ev.get("toolName"),
                                  id=tid, out=None if not total else tail,
                                  err=bool(ev.get("isError")), out_len=total or None)
            else:
                entry["err"] = bool(ev.get("isError"))
                if total:
                    entry["out"] = tail
                    entry["out_len"] = total
        elif kind == "error":
            self._add("error", note=str(ev.get("message") or ev)[:400])
        # 其余未知类型:静默跳过

    def meta(self, *, live: bool = False, truncated: bool = False,
             extra: dict | None = None) -> dict:
        """折叠终态 meta(abrupt: 有会话无正常收尾且非 live 在播)。"""
        abrupt = bool(self.sessions) and self.agent_ends < self.sessions and not live
        out = {
            "sessions": self.sessions,
            "agent_ends": self.agent_ends,
            "truncated": truncated,
            "dropped": bool(self.dropped),
            "abrupt": abrupt,
            "live": bool(live),
            "unparsed": self.unparsed,
        }
        if extra:
            out.update(extra)
        return out


def fold_rows(rows: Iterable[dict[str, Any]], *, after: int = 0,
              live: bool = False) -> dict:
    """把按时间序的 DB 事件行(每个为 {payload: 原文 JSON 行,...})合流成时间线条目。

    返回 {next_seq, meta, entries}:entries 已按 seq>=after 切片;next_seq 为折叠总数
    (下一次轮询的 after)。同一事件集折叠结果确定 —— 折叠状态全部在本次重建。
    """
    st = FoldState()
    for row in rows:
        try:
            ev = json.loads(row["payload"])
            if not isinstance(ev, dict):
                raise ValueError("not a json object")
        except Exception:
            st.unparsed += 1
            continue
        st.feed(ev)
    st.flush_text()
    entries = [e for e in st.entries if e["seq"] >= after]
    return {"next_seq": st.next_seq, "meta": st.meta(live=live), "entries": entries}
