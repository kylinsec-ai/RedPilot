"""platform/digest.py — transcript 事件折叠为"人读时间线"(无状态确定性 fold)。

从 drivers/roster.py 的 TranscriptDigest._feed 移植语义:一次请求把某 code(或单 run)
的 DB 事件行按时间序全部合流,产出条目与 meta。无侧车缓存,重启稳定;
~k 行/题的量级下每次全量重折叠成本可忽略。message_update 增量折叠分支保留
(DB 流里 worker 已丢,防回填)。

条目 schema(seq 全局单调,UI 按 seq 增量拉取):
  {seq, kind: session|attempt|turn|tool|text|note|error,
   t(epoch ms 近似), turn, tool, cmd, out, err(bool), text, note,
   stop, tokens, sid(会话 id 前 6 位), n(序号)}
meta: {sessions, agent_ends, truncated(恒 False —— DB 行不会因文件截断丢失),
       dropped, abrupt, live, unparsed}
(与旧 digest 相比去掉 bytes 键;超量丢旧半由 dropped 表达。)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from .redact import summarize_args

log = logging.getLogger("obs.digest")

_ENTRY_CAP = 20000   # 单 code 条目上限:超出丢旧半,meta.dropped=true
_TEXT_FLUSH = 1500   # 流式文本缓冲阈值(字符)
_TEXT_ENTRY_MAX = 1200


def _iso_to_ms(iso: str) -> Optional[float]:
    try:
        s = iso
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp() * 1000.0
    except Exception:
        return None


def _oneline(args, max_len: int = 300) -> str:
    """工具参数 -> 单行命令/摘要(复用 redact.summarize_args;与 worker roster._oneline 同语义)"""
    try:
        if isinstance(args, dict) and isinstance(args.get("command"), str):
            cmd = " ".join(args["command"].split())
            return cmd if len(cmd) <= max_len else cmd[:max_len] + "…"
        return summarize_args(args or {}, max_len=max_len)
    except Exception:
        try:
            s = json.dumps(args or {}, ensure_ascii=False)
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


def _fresh_state() -> dict:
    return {
        "sessions": 0, "agent_ends": 0, "unparsed": 0, "dropped": False,
        "entries": [], "next_seq": 0, "turn": 0, "last_ts": None,
        # 跨事件折叠态
        "text_open": False, "text_buf": "", "last_turn_entry": None,
        "tools": {},  # toolCallId -> 条目 dict 引用
    }


def _add(st: dict, kind: str, **fields) -> dict:
    """追加条目并返回其 dict 引用(调用方持有引用即可后续补字段)"""
    seq = st["next_seq"]
    st["next_seq"] += 1
    entry: dict = {"seq": seq, "kind": kind, "t": st["last_ts"], "turn": st.get("turn")}
    entry.update({k: v for k, v in fields.items() if v is not None})
    st["entries"].append(entry)
    if len(st["entries"]) > _ENTRY_CAP:
        st["entries"] = st["entries"][-_ENTRY_CAP // 2:]
        st["dropped"] = True
    return entry


def _flush_text(st: dict) -> None:
    buf = st.get("text_buf", "").strip()
    if not buf:
        return
    more = len(buf) > _TEXT_ENTRY_MAX
    _add(st, "text", text=(buf[:_TEXT_ENTRY_MAX] + "…" if more else buf), more=more)
    st["text_buf"] = ""


def _touch_ts(st: dict, raw_ts) -> None:
    if raw_ts is None:
        return
    try:
        ms = float(raw_ts)
        if ms < 1e12:      # 秒级 -> 毫秒
            ms *= 1000.0
        st["last_ts"] = ms
    except (TypeError, ValueError):
        pass


def _feed(st: dict, ev: dict) -> None:
    """与 drivers/roster.py TranscriptDigest._feed 同款状态机(语义逐字移植)。"""
    kind = ev.get("type", "")
    try:
        if kind == "_attempt":
            _add(st, "attempt", n=ev.get("attempt"))
        elif kind == "session":
            iso = ev.get("timestamp") or ""
            ms = _iso_to_ms(iso) if iso else None
            if ms:
                st["last_ts"] = ms
            sid = str(ev.get("id") or "")
            _add(st, "session", sid=(sid[:6] if sid else None),
                 note=f"cwd: {ev.get('cwd') or ''}" or None)
            st["sessions"] += 1
            st["text_buf"] = ""
            st["tools"] = {}
        elif kind == "agent_start":
            pass
        elif kind == "agent_end":
            st["agent_ends"] += 1
            _add(st, "note", note="agent 正常结束")
        elif kind == "turn_start":
            st["turn"] = int(st.get("turn") or 0) + 1
            st["last_turn_entry"] = _add(st, "turn", n=st["turn"])
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
            _touch_ts(st, msg.get("timestamp"))
            if msg.get("role") == "user":
                _add(st, "note", note="任务输入（提示词）就绪")
        elif kind == "message_update":
            msg = ev.get("message") or {}
            _touch_ts(st, msg.get("timestamp"))
            sub = (ev.get("assistantMessageEvent") or {}).get("type", "")
            delta = ev.get("assistantMessageEvent") or {}
            if sub == "text_start":
                st["text_open"] = True
                st["text_buf"] = ""
            elif sub == "text_delta":
                if st.get("text_open"):
                    st["text_buf"] = (st.get("text_buf") or "") + str(delta.get("delta") or "")
                    if len(st["text_buf"]) > _TEXT_FLUSH:
                        _flush_text(st)
            elif sub == "text_end":
                _flush_text(st)
                st["text_open"] = False
            # thinking_*/toolcall_* 增量:不渲染,只认总量级(无独立时间戳)
        elif kind == "message_end":
            msg = ev.get("message") or {}
            _touch_ts(st, msg.get("timestamp"))
            if msg.get("role") == "assistant":
                # 压缩后的 transcript 无 message_update,text_buf 为空;
                # 此时从 message_end 的 content 直接提取文本
                if not st.get("text_buf"):
                    for c in (msg.get("content") or []):
                        if c.get("type") == "text" and c.get("text"):
                            st["text_buf"] = c["text"]
                            break
                _flush_text(st)
        elif kind == "tool_execution_start":
            args = ev.get("args") or {}
            entry = _add(st, "tool", tool=ev.get("toolName"),
                         cmd=_oneline(args if isinstance(args, dict) else {}),
                         id=str(ev.get("toolCallId") or ""))
            st["tools"][str(ev.get("toolCallId") or "")] = entry
        elif kind == "tool_execution_update":
            pass  # 进度增量不渲染(结果以 end 为准)
        elif kind == "tool_execution_end":
            tid = str(ev.get("toolCallId") or "")
            entry = st["tools"].pop(tid, None)
            result = ev.get("result") or {}
            tail, total = _result_tail(result)
            if entry is None:
                entry = _add(st, "tool", tool=ev.get("toolName"),
                             id=tid, out=None if not total else tail,
                             err=bool(ev.get("isError")), out_len=total or None)
            else:
                entry["err"] = bool(ev.get("isError"))
                if total:
                    entry["out"] = tail
                    entry["out_len"] = total
        elif kind == "error":
            _add(st, "error", note=str(ev.get("message") or ev)[:400])
        # 其余未知类型:静默跳过
    except Exception:
        log.debug("digest feed error on %s: %r", kind, ev, exc_info=True)


def fold_rows(rows: Iterable[dict[str, Any]], *, after: int = 0,
              live: bool = False) -> dict:
    """把按时间序的 DB 事件行(每个为 {payload: 原文 JSON 行,...})合流成时间线条目。

    返回 {next_seq, meta, entries}:entries 已按 seq>=after 切片;next_seq 为折叠总数
    (下一次轮询的 after)。同一事件集折叠结果确定 —— 折叠状态全部在本次重建。
    """
    st = _fresh_state()
    for row in rows:
        try:
            ev = json.loads(row["payload"])
            if not isinstance(ev, dict):
                raise ValueError("not a json object")
        except Exception:
            st["unparsed"] += 1
            continue
        _feed(st, ev)
    _flush_text(st)
    entries = [e for e in st["entries"] if e["seq"] >= after]
    abrupt = bool(st["sessions"]) and st["agent_ends"] < st["sessions"] and not live
    meta = {
        "sessions": st["sessions"],
        "agent_ends": st["agent_ends"],
        "truncated": False,
        "dropped": bool(st["dropped"]),
        "abrupt": abrupt,
        "live": bool(live),
        "unparsed": st["unparsed"],
    }
    return {"next_seq": st["next_seq"], "meta": meta, "entries": entries}
