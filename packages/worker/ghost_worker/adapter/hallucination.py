#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""[B54] 做题幻觉哨兵 —— 把「凭空编」与「真推导但没资格」分开，只给前者装牙齿。

▌为什么必须分开记账（本模块存在的全部理由）

`verify.flag_confidence` 的拒收理由有 8 种，语义分属两个**相反**的族：

  幻觉族（conf ≤ 0.3）—— 凭空编 / 自问自答，没有任何来源
      placeholder_pattern   占位词（形如 test/admin/example 的信封）
      low_entropy           低熵串（同一个字符反复堆）
      agent_authored        只出现在 agent 自己敲的命令参数里
      not_grounded          命令输出与命令参数里都找不到

  推导族（conf 0.4~0.6）—— **有真来源，只是没资格进提交门**
      local_computed_only   只在本地静态产物里出现（须活靶标响应才算数）
      local_derived         body 先被观测到、信封是 agent 自己套的
      no_source_cmd         输出里有、但拿不到来源命令

框架自己的影子审计结论：**29 条被拒候选里 19 条实为正确答案**
（见 `adapter/taskprompt.py` 里 B45 段落的注释）。那批几乎全落在推导族。

所以本模块的**第一安全性质**：
    推导族永不计数、永不触发任何干预，只有幻觉族参与阈值。
把推导族当幻觉去杀，等于掐掉那 19/29 真答案的产出路径 —— 比不装监控更糟。

**第二安全性质**：本模块只记账、不裁决。
    所有副作用开关都在调用方（driver 决定是否轮换 / 是否关强提，pi_agent
    决定是否掐会话）。本模块只回答「这算幻觉吗 / 到阈值了吗」，绝不自己动手。

**第三安全性质**：任何异常一律吞掉、返回「不干预」。
    监控消失可以接受，解题路径被监控拖挂不可以。

▌状态文件
`<workdir>/.hallucination.json`，与 `.unverified_flags` 同级、同生命周期
（题目 solved 后由 `_purge_plaintext_artifacts` 一并清掉）。
只存计数与 sha1 指纹，**不存任何 flag 原文** —— 合规口径与账本回灌一致。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

log = logging.getLogger(__name__)

# ── 分类口径：与 verify.flag_confidence 的 reject_reason 一一对应 ──
# 只在这里定义一次；driver 与 pi_agent 都从这里取，避免两处口径漂移。
FABRICATION_REASONS = frozenset({
    "placeholder_pattern",
    "low_entropy",
    "agent_authored",
    "not_grounded",
})
DERIVATION_REASONS = frozenset({
    "local_computed_only",
    "local_derived",
    "no_source_cmd",
})

STATE_NAME = ".hallucination.json"
_MAX_MARKS = 200        # 指纹集合上限，防长命题目状态文件无界增长

# flag{...} 完整信封。只取 body、统一小写 —— 与 verify.normalize_flag_body 同口径。
_ENVELOPE_RX = re.compile(r"flag\{([^}\s]{1,200})\}", re.IGNORECASE)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


def rotate_at() -> int:
    """幻觉族候选累计到几条 → 建议本题轮换（软干预）。"""
    return _env_int("ADAPTER_HALU_ROT", 3)


def suppress_force_at() -> int:
    """幻觉族候选累计到几条 → 关掉本题强提交通道（保护平台提交配额）。"""
    return _env_int("ADAPTER_HALU_FORCE_OFF", 2)


def abort_enabled() -> bool:
    """会话内掐断总开关（硬干预）。默认开；置 0 可整体关掉。"""
    return str(os.environ.get("ADAPTER_HALU_ABORT", "1") or "1").strip() != "0"


def abort_limit() -> int:
    """同一个 body 被写进命令参数几次 → 判「自造声明成瘾」并掐断。"""
    return _env_int("ADAPTER_HALU_ABORT_N", 4)


def classify(reason: str) -> str:
    """把 reject_reason 归到 "fabrication" / "derivation" / ""（不归类）。

    不归类 ≠ 安全，只是**不参与阈值**：判不准的时候什么都不做，
    这是本模块唯一正确的保守方向。
    """
    r = (reason or "").strip()
    if r in FABRICATION_REASONS:
        return "fabrication"
    if r in DERIVATION_REASONS:
        return "derivation"
    return ""


def envelopes(text: str) -> set:
    """抽出文本里所有 flag{...} 信封的 body（小写归一）。

    给 pi_agent 的活体哨兵用 —— 口径与 verify 同源，免得两处各写一套正则。
    """
    return {m.group(1).lower() for m in _ENVELOPE_RX.finditer(text or "")}


# ── 状态存取（任何 IO 异常都不外抛）──────────────────────────────

def _path(workdir: str) -> str:
    return os.path.join(workdir, STATE_NAME)


def _mark(body: str) -> str:
    return hashlib.sha1((body or "").encode("utf-8", "replace")).hexdigest()[:12]


def _blank() -> dict:
    return {"fabrications": 0, "derivations": 0,
            "by_reason": {}, "marks": [], "first_wall": 0.0, "last_wall": 0.0}


def _load(workdir: str) -> dict:
    try:
        with open(_path(workdir), encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict):
            return _blank()
    except (OSError, ValueError):
        return _blank()
    base = _blank()
    for k, v in base.items():
        st.setdefault(k, v)
    if not isinstance(st.get("marks"), list):
        st["marks"] = []
    if not isinstance(st.get("by_reason"), dict):
        st["by_reason"] = {}
    return st


def _save(workdir: str, st: dict) -> None:
    p = _path(workdir)
    tmp = p + ".tmp"
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, p)          # 原子替换：中断也不会留半个 JSON
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def state(workdir: str) -> dict:
    """读该题的幻觉计数（缺失/损坏一律返回全 0）。"""
    if not workdir:
        return _blank()
    try:
        st = _load(workdir)
        return {"fabrications": int(st.get("fabrications", 0) or 0),
                "derivations": int(st.get("derivations", 0) or 0),
                "by_reason": dict(st.get("by_reason") or {}),
                "first_wall": float(st.get("first_wall", 0) or 0),
                "last_wall": float(st.get("last_wall", 0) or 0)}
    except Exception:
        return _blank()


def record(workdir: str, reason: str, body: str) -> dict:
    """记一条拒收。返回更新后的计数（失败时返回现值，绝不抛）。

    **同 (族, body) 只计一次** —— 同一个候选被 eager 通道与主循环各拒一次
    是常态（两条路径都会调 `_add_unverified_flag`），不去重会把阈值瞬间
    刷爆、把正常题误判成幻觉题。
    """
    kind = classify(reason)
    if not kind or not workdir or not body:
        return state(workdir)
    try:
        st = _load(workdir)
        key = kind + ":" + _mark(body)
        marks = st["marks"]
        if key in marks:                       # 同一候选重复拒收 → 不重复计数
            return state(workdir)
        marks.append(key)
        if len(marks) > _MAX_MARKS:            # 只留最近的，防无界增长
            del marks[:len(marks) - _MAX_MARKS]
        # 注意键名是**复数**（fabrications / derivations），与 _blank()/state() 同口径。
        # 曾写成单数 st[kind]，计数落进没人读的字段、state() 永远返回 0 —— 功能自测逮住。
        _key = "fabrications" if kind == "fabrication" else "derivations"
        st[_key] = int(st.get(_key, 0) or 0) + 1
        br = st["by_reason"]
        br[reason] = int(br.get(reason, 0) or 0) + 1
        now = time.time()
        if not st.get("first_wall"):
            st["first_wall"] = now
        st["last_wall"] = now
        _save(workdir, st)
        return state(workdir)
    except Exception:
        return state(workdir)


def summary(workdir: str) -> str:
    """给日志用的一行摘要，形如：

        幻觉族 2 (not_grounded=1, agent_authored=1) / 推导族 5 (local_derived=5)

    [B54c] 明细必须**按族分开挂**。`by_reason` 里两族的理由混在同一个 dict，
    初版把整个明细都拼到「幻觉族」后面，日志会打成
    `幻觉族 1 (not_grounded=1, local_computed_only=1) / 推导族 1` ——
    读起来像推导族也计进了幻觉，会让人误判该题该轮换。

    这正是本模块要消灭的那类错（B47b 同款）：**两种语义相反的东西在日志上同形，
    排查时必然被带偏**。本模块唯一的对外出口就是这一行，混着显示等于白做。
    """
    try:
        st = state(workdir)
        if not st["fabrications"] and not st["derivations"]:
            return ""

        def _det(fam: str) -> str:
            items = [(k, v) for k, v in st["by_reason"].items() if classify(k) == fam]
            items.sort(key=lambda kv: -kv[1])
            return ", ".join("%s=%d" % (k, v) for k, v in items)

        _f, _d = _det("fabrication"), _det("derivation")
        return "幻觉族 %d%s / 推导族 %d%s" % (
            st["fabrications"], (" (%s)" % _f) if _f else "",
            st["derivations"], (" (%s)" % _d) if _d else "")
    except Exception:
        return ""


def should_rotate(workdir: str) -> bool:
    """软干预：幻觉族超阈 → 调用方记一次「无进展」，让止损机制轮换本题。"""
    try:
        return state(workdir)["fabrications"] >= rotate_at()
    except Exception:
        return False


def should_suppress_force(workdir: str) -> bool:
    """保护提交配额：幻觉族超阈 → 关掉本题的强提交通道。"""
    try:
        return state(workdir)["fabrications"] >= suppress_force_at()
    except Exception:
        return False


# ── 会话内活体哨兵（pi_agent 用）──────────────────────────────────

class LiveSentinel:
    """会话**进行中**的幻觉观测器 —— 框架唯一能中途干预的观测点。

    框架要到会话结束才拿得到 `result.flags`（driver 先 solve() 再遍历），
    中途唯一的活体信号是**每条工具调用的命令参数与输出**。判据：

        同一个 body 被写进命令参数 ≥n 次
        且 整个会话任何一条工具输出里都没出现过它
        → 判「自造声明成瘾」，掐断会话（硬干预）

    两条判据缺一不可。特别是**「工具输出里出现过」这一条**：
    它一旦成立，这个 body 就已经是有效证据、属于真解路径，
    绝不能因为命令参数里也出现过就误杀 —— 推导族那 19/29 正是这种形态。

    另一处保守设计：**按 body 分别计数**。穷举爆破每轮换一个新 body，
    永远不会触阈；只有「反复重申同一个编造值」才会 —— 那才是纯浪费。
    """

    def __init__(self, limit: int = 0):
        self.limit = limit or abort_limit()
        self.authored: dict = {}     # body -> 在命令参数里出现的次数
        self.seen: set = set()       # 在工具输出里出现过的 body
        self.tripped = ""            # 已触发的 body（空串=没触发）

    def on_call(self, args) -> None:
        """工具调用开始：命令参数里出现的信封 = agent 自己敲的。"""
        if self.tripped:
            return
        try:
            if isinstance(args, dict):
                cmd = args.get("command") or args.get("cmd") or ""
                if not cmd:
                    cmd = " ".join(str(v) for v in args.values())
            else:
                cmd = str(args or "")
            for b in envelopes(cmd):
                self.authored[b] = self.authored.get(b, 0) + 1
        except Exception:
            pass

    def on_output(self, out: str) -> None:
        """工具输出：出现过即「有据」—— 该 body 从此免疫，不再可能触发掐断。"""
        try:
            self.seen |= envelopes(out)
        except Exception:
            pass

    def verdict(self) -> str:
        """返回该掐断的 body；没有则空串。

        只挑**命令参数里出现过、工具输出里从未出现、且重复 ≥limit 次**的 body。
        """
        if self.tripped:
            return self.tripped
        try:
            for b, n in self.authored.items():
                if n >= self.limit and b not in self.seen:
                    self.tripped = b
                    return b
        except Exception:
            pass
        return ""

    def fingerprint(self, body: str) -> str:
        """只给首 3 字符 —— 与账本回灌的截断口径一致，绝不吐原文。"""
        b = body or ""
        return (b[:3] + "…") if len(b) > 3 else b
