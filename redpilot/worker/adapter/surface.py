"""会话内攻击面预算（M3，设计 `docs/solver-isolation-design.md` §6）。

为什么需要它：`StopLoss` 的粒度是"题目 × 轮次"（连续 N 场干场才换题、终身时间
预算），而题面里真正约束行为的是动作级的"同一方向连续失败 3 次 → 换思路、
爆破 5 分钟无果 → 换攻击面"。harness 看不见"方向/攻击面"，这两条就只能靠模型
自觉 —— 一场 480–2000s 的会话可以把 80% 时间烧在一个死方向上，框架只在**场末**
才知道这场没新事实。

本模块是确定性的（无 LLM、无网络）：
  · surface = (target, tactic) 的哈希键；键由命令**内容**推导，不按 argv 原样
    哈希 —— 换工具写法不算换面（防指标博弈，设计 §6.2/§6.3-4）。
  · "有没有新事实"来自黑板的机械抽取（shadow `Blackboard`，不落盘、不写真实
    黑板）；它对本地读取也比正式判据宽松 —— 这是刻意的保守方向：宁可漏判停滞，
    不误杀正在推进的面。
  · 动作阶梯由 `tick()` 产出（soft → steer → abort），执行与观测在编排层
    （`orchestrator.py` 的 `_surface_control`），这里不碰传输、不发事件。

生命周期：每个 code 一份账本，落盘在 `<workdir>/.harness/surface/<safe>.json`
（控制面目录，求解者不可写）。跨场续接只认同一 task epoch（记忆系统的
"失效但不丢弃"，设计 §6.2）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass

from .blackboard import Blackboard
from redpilot.contracts.paths import HARNESS_DIR

log = logging.getLogger("adapter.surface")

# ── 确定性分类表（表驱动、可单测）────────────────────────────

_TACTIC_BY_TOOL = {
    # 扫描 / 发现
    "nmap": "scan", "masscan": "scan", "naabu": "scan", "netdiscover": "scan",
    "arp-scan": "scan",
    # 枚举 / 爆破
    "gobuster": "brute", "ffuf": "brute", "feroxbuster": "brute",
    "dirsearch": "brute", "wfuzz": "brute", "nikto": "brute",
    "hydra": "brute", "medusa": "brute", "john": "brute", "hashcat": "brute",
    # 利用
    "sqlmap": "exploit", "nuclei": "exploit", "msfconsole": "exploit",
    "msfvenom": "exploit", "searchsploit": "exploit",
    # 云 / 对象存储枚举
    "aws": "cloud_enum", "s3cmd": "cloud_enum", "az": "cloud_enum",
    "gcloud": "cloud_enum",
    # 二进制 / 数据分析
    "file": "analysis", "strings": "analysis", "gdb": "analysis",
    "ropper": "analysis", "objdump": "analysis", "readelf": "analysis",
    "binwalk": "analysis", "checksec": "analysis", "xxd": "analysis",
    "r2": "analysis", "radare2": "analysis", "ltrace": "analysis",
    "strace": "analysis",
    # HTTP / 网络交互
    "curl": "http", "wget": "http", "httpie": "http",
    "ssh": "network", "smbclient": "network", "enum4linux": "network",
    "ncat": "network", "nc": "network", "socat": "network",
    "redis-cli": "network", "psql": "network", "mysql": "network",
    # 脚本（含自定义 exploit/解码脚本）
    "python": "script", "python3": "script", "bash": "script", "sh": "script",
    "perl": "script", "ruby": "script", "php": "script", "node": "script",
}

# 建议"未试家族"时的稳定顺序（不含 other —— 它是兜底桶，不构成方向）。
ALL_TACTICS = ("scan", "brute", "http", "network", "exploit",
               "cloud_enum", "analysis", "script")

_URL_RX = re.compile(r"https?://([^\s'\"<>/]+)", re.I)
_IP_RX = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)(?::(\d{1,5}))?\b")
_PATH_RX = re.compile(
    r"(?:[\w.@~-]+/)*[\w.@~-]+\.(?:bin|elf|exe|out|so|zip|tar|tgz|gz|xz|dat|txt|md|"
    r"py|sh|js|json|xml|html|pcap|cap|img|iso|pdf|jpg|jpeg|png|apk|db|sqlite|"
    r"pem|key|crt|conf|pyc|jar|class)", re.I)

_SHELL_SPLIT = re.compile(r"(?:&&|\|\||;|\|)")
# 沿革（2026-09 死码清扫）：此处原有 `_FLAGS_WITH_ARG`（带参数的选项名集合），
# 零读者 —— 面的键改为按命令内容（target+tactic）推导后，那份表被架空。


def _command_of(args) -> str:
    if isinstance(args, dict):
        for key in ("command", "cmd", "file_path", "path"):
            val = args.get(key)
            if val:
                return str(val)
    return str(args or "")


def _first_executable(command: str) -> str:
    """取复合命令里第一个"像工具"的词（绝对/相对路径取 basename）。"""
    head = _SHELL_SPLIT.split(command or "", 1)[0].strip()
    if not head:
        return ""
    for tok in head.split():
        if tok.startswith("-") or "=" in tok:
            continue
        base = os.path.basename(tok.strip("'\""))
        if re.fullmatch(r"[\w.+-]+", base):
            return base.lower()
    return ""


def _target_of(command: str, tactic: str) -> str:
    """从命令里取归一化目标。

    先 URL host，再 IP[:port]，再文件路径；都没有就返回 ""（同一 tactic 共享
    一个面 —— 这是防换皮：换工具名不该绕开预算）。
    """
    m = _URL_RX.search(command or "")
    if m:
        return m.group(1).lower()
    m = _IP_RX.search(command or "")
    if m:
        port = m.group(2)
        return f"{m.group(1)}:{port}" if port else m.group(1)
    if tactic == "analysis":
        m = _PATH_RX.search(command or "")
        if m:
            return os.path.basename(m.group(0)).lower()
    return ""


def classify(tool: str, args) -> tuple[str, str]:
    """确定性返回 (target, tactic)；纯函数，表驱动单测。"""
    name = str(tool or "").strip().lower()
    command = _command_of(args)
    if name == "bash":
        exe = _first_executable(command)
        tactic = _TACTIC_BY_TOOL.get(exe, "")
        if not tactic:
            tactic = "other" if exe else "script"
            target = ""
        else:
            target = _target_of(command, tactic)
        # 明确文件路径出现在 scan/brute 之外时（如 python3 solve.py）仍按路径区分
        if not target and tactic in ("analysis", "script"):
            m = _PATH_RX.search(command)
            if m:
                target = os.path.basename(m.group(0)).lower()
        return target, tactic
    # 文件类工具（read/edit/write）：按路径区分（不同文件是不同工作面）
    if name in ("read", "edit", "write", "grep", "glob", "find", "ls"):
        path = ""
        if isinstance(args, dict):
            path = str(args.get("file_path") or args.get("path") or args.get("pattern") or "")
        return (os.path.basename(path).lower() if path else ""), "analysis"
    exe = _first_executable(command) or name
    tactic = _TACTIC_BY_TOOL.get(exe) or _TACTIC_BY_TOOL.get(name) or "other"
    return _target_of(command, tactic), tactic


def surface_key(tool: str, args) -> tuple[str, str, str]:
    """返回 (key, target, tactic)。key 只由 target+tactic 决定（防换皮）。"""
    target, tactic = classify(tool, args)
    digest = hashlib.sha1(f"{target}|{tactic}".encode("utf-8")).hexdigest()[:12]
    return f"{digest}:{tactic}", target, tactic


# ── 账本 ────────────────────────────────────────────────────

@dataclass
class SurfaceEntry:
    """单面状态。`facts` 是"新事实"计数（shadow 黑板机械抽取）。"""
    key: str
    target: str
    tactic: str
    first_wall: float
    last_fact_wall: float
    attempts: int = 0
    facts: int = 0
    soft_seen: bool = False
    steered: bool = False
    closed_reason: str = ""
    closed_at: float = 0.0
    calls_since_close: int = 0
    aborts: int = 0
    rescued: bool = False
    last_tool: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key, "target": self.target, "tactic": self.tactic,
            "attempts": self.attempts, "facts": self.facts,
            "soft_seen": self.soft_seen, "steered": self.steered,
            "closed_reason": self.closed_reason, "aborts": self.aborts,
            "rescued": self.rescued, "last_tool": self.last_tool,
        }


@dataclass
class Action:
    """tick() 的产物：由编排层负责执行（steer/abort）与上报。"""
    kind: str          # soft | steer | abort
    key: str
    target: str
    tactic: str
    reason: str        # stall | absolute | ignored_steer | soft
    text: str


class SurfaceLedger:
    """单题的面预算账本。确定性；持久化只写 `.harness/surface/<code>.json`。"""

    def __init__(self, cfg, code: str, *, epoch: str = "",
                 persist_path: str = "", clock=time.time):
        self.cfg = cfg
        self.code = str(code)
        self.epoch = str(epoch or "")
        self.persist_path = persist_path
        self._clock = clock
        self.entries: dict[str, SurfaceEntry] = {}
        self._cmd_keys: dict[str, str] = {}       # hash(cmd) -> surface key（防换皮留痕）
        self._board = Blackboard()                # shadow：不落盘
        self._dirty = False
        if persist_path:
            self._load()

    # ── 输入 ──

    def note_result(self, tool: str, args, output: str,
                    now: float | None = None) -> dict:
        """记录一次工具结果；返回 {key, target, tactic, rekey, facts, closed}。

        `rekey` 非空表示同一命令此前映射到另一个面（换面判定留痕，供
        `surface.rekey` 事件使用）。
        """
        now = self._clock() if now is None else now
        key, target, tactic = surface_key(tool, args)
        e = self.entries.get(key)
        if e is None:
            if len(self.entries) >= max(8, int(getattr(self.cfg, "max_surfaces", 64))):
                self._evict_lru()
            e = SurfaceEntry(key=key, target=target, tactic=tactic,
                             first_wall=now, last_fact_wall=now)
            self.entries[key] = e
        e.attempts += 1
        e.last_tool = str(tool or "")[:32]
        if e.closed_reason:
            e.calls_since_close += 1

        rekey = ""
        cmd = _command_of(args)
        if cmd:
            h = hashlib.sha1(cmd.encode("utf-8", "replace")).hexdigest()[:16]
            prev = self._cmd_keys.get(h)
            if prev and prev != key:
                rekey = prev
            self._cmd_keys[h] = key
            if len(self._cmd_keys) > 1024:
                for k in list(self._cmd_keys)[:256]:
                    self._cmd_keys.pop(k, None)

        added = 0
        if output and output.strip():
            try:
                added = self._board.observe(tool, args or {}, output, iter=0)
            except Exception as exc:                       # 影子账本绝不能拖垮解题
                log.warning("surface shadow observe failed: %s", exc)
        if added:
            e.facts += added
            e.last_fact_wall = now
            if e.closed_reason:
                e.rescued = True
        self._dirty = True
        return {"key": key, "target": target, "tactic": tactic, "rekey": rekey,
                "facts": added, "closed": bool(e.closed_reason)}

    # ── 判定阶梯 ──

    def tick(self, now: float | None = None) -> Action | None:
        """返回**最多一个**动作（优先级：abort → steer → 新关闭 → soft）。

        每级都有出路：关闭只关面、不关题；误杀可由 `rescued` 后验校准。
        """
        if not getattr(self.cfg, "enabled", True):
            return None
        now = self._clock() if now is None else now
        # 1) L2：无视 steer，仍在同面活动
        for e in self.entries.values():
            if (e.closed_reason and e.steered and e.aborts < 2
                    and e.calls_since_close >= max(1, int(self.cfg.ignore_calls))
                    and now >= e.closed_at + max(0, int(self.cfg.ignore_seconds))):
                e.aborts += 1
                self._dirty = True
                return Action("abort", e.key, e.target, e.tactic,
                              "ignored_steer", self._steer_text(e, "ignored_steer"))
        # 2) L1：关闭后的 steer 送达
        for e in self.entries.values():
            if e.closed_reason and not e.steered:
                e.steered = True
                self._dirty = True
                return Action("steer", e.key, e.target, e.tactic,
                              e.closed_reason, self._steer_text(e, e.closed_reason))
        # 3) 新关闭：绝对上限 / 停滞
        for e in self.entries.values():
            if e.closed_reason or e.attempts <= 0:
                continue
            if self.cfg.abs_seconds and (now - e.first_wall) >= self.cfg.abs_seconds:
                self._close(e, "absolute", now)
                return Action("steer", e.key, e.target, e.tactic, "absolute",
                              self._steer_text(e, "absolute"))
            if (self.cfg.hard_seconds and e.attempts >= 3
                    and (now - e.last_fact_wall) >= self.cfg.hard_seconds):
                self._close(e, "stall", now)
                return Action("steer", e.key, e.target, e.tactic, "stall",
                              self._steer_text(e, "stall"))
        # 4) L0：软信号（每面一次，不阻断）
        for e in self.entries.values():
            if (not e.closed_reason and not e.soft_seen and e.attempts >= 5
                    and self.cfg.soft_seconds
                    and (now - e.last_fact_wall) >= self.cfg.soft_seconds):
                e.soft_seen = True
                self._dirty = True
                return Action("soft", e.key, e.target, e.tactic, "soft",
                              self._steer_text(e, "soft"))
        return None

    def _close(self, e: SurfaceEntry, reason: str, now: float) -> None:
        e.closed_reason = reason
        e.closed_at = now
        e.calls_since_close = 0
        # 关闭与 steer 同一次 tick 送达，所以直接标已送 —— 否则下一次 tick
        # 会被 "steered=False" 再送回一条重复指令（实测）。
        e.steered = True
        self._dirty = True
        self.save()

    def _evict_lru(self) -> None:
        if not self.entries:
            return
        victim = min(self.entries.values(), key=lambda x: x.last_fact_wall)
        self.entries.pop(victim.key, None)

    def _steer_text(self, e: SurfaceEntry, reason: str) -> str:
        untried = self.untried_tactics()
        if untried:
            hint = ("尚未尝试的攻击面类型：" + "、".join(untried)
                    + "；优先选其中与当前证据最相关的一个。")
        else:
            hint = ("没有未试的攻击面类型了；优先深挖已有证据（凭据、入口、可复用产物），"
                    "不要原样重复已证死的命令。")
        why = {"absolute": "已超过单面绝对时间上限",
               "stall": "已连续无新事实",
               "ignored_steer": "在收到换面指令后仍停留在该面",
               "soft": "可能停滞"}.get(reason, reason)
        label = e.target or f"（无固定目标，{e.tactic}）"
        head = "## ⏱ 攻击面预算（框架判定）"
        if reason == "soft":
            return (f"{head}\n攻击面 `{label}`（类型 {e.tactic}）{why}："
                    f"{hint}（提示，不阻断）")
        closed = "、".join(
            sorted({f"{x.target or x.tactic}/{x.tactic}" for x in self.entries.values()
                    if x.closed_reason})) or "无"
        return (f"{head}\n攻击面 `{label}`（类型 {e.tactic}）{why}，**该面已被框架关闭**。"
                f"{hint}\n已关闭的路线：{closed}。"
                "不要用新工具或新写法重开同一目标/同一意图；如确有新证据必须回头，"
                "先把新证据写入 MEMORY.md 再继续。")

    # ── 查询 ──

    def untried_tactics(self) -> list[str]:
        tried = {e.tactic for e in self.entries.values()}
        return [t for t in ALL_TACTICS if t not in tried]

    def blocked_routes(self) -> list[dict]:
        """已关闭的面（元数据，可进续接/观测；不含命令与输出）。"""
        return [{"target": e.target, "tactic": e.tactic, "reason": e.closed_reason,
                 "facts": e.facts, "attempts": e.attempts, "rescued": e.rescued}
                for e in self.entries.values() if e.closed_reason]

    def prompt_note(self) -> str:
        """跨场注入：告诉下一场哪些面已被关闭（同 epoch 才作数）。"""
        blocked = self.blocked_routes()
        if not blocked:
            return ""
        items = "；".join(
            f"{b['target'] or b['tactic']}（{b['tactic']}，{b['reason']}）"
            for b in blocked[:8])
        return ("## 上一场已关闭的攻击面（框架记账，不要原样重开）\n"
                f"{items}\n"
                "只有**新机制/新证据**才允许回头；换个工具名或换个命令写法不算。")

    def session_summary(self) -> dict:
        return {
            "surfaces": len(self.entries),
            "facts": sum(e.facts for e in self.entries.values()),
            "closed": len(self.blocked_routes()),
            "rescued": sum(1 for e in self.entries.values() if e.rescued),
            "aborts": sum(e.aborts for e in self.entries.values()),
        }

    # ── 持久化（控制面目录；同 epoch 才续用）──

    def _load(self) -> None:
        try:
            with open(self.persist_path, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            return
        if str(d.get("epoch", "")) != self.epoch:
            return                                    # 旧 epoch 失效但不覆盖（下次 save 重写）
        now = self._clock()
        for raw in d.get("entries", []) or []:
            try:
                e = SurfaceEntry(
                    key=str(raw["key"]), target=str(raw.get("target", "")),
                    tactic=str(raw.get("tactic", "other")),
                    first_wall=float(raw.get("first_wall", now)),
                    last_fact_wall=float(raw.get("last_fact_wall", now)),
                    attempts=int(raw.get("attempts", 0)),
                    facts=int(raw.get("facts", 0)),
                    soft_seen=bool(raw.get("soft_seen", False)),
                    steered=bool(raw.get("steered", False)),
                    closed_reason=str(raw.get("closed_reason", "")),
                    aborts=int(raw.get("aborts", 0)),
                    rescued=bool(raw.get("rescued", False)),
                    last_tool=str(raw.get("last_tool", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self.entries[e.key] = e

    def save(self) -> None:
        if not self.persist_path:
            return
        payload = {
            "version": 1,
            "epoch": self.epoch,
            "saved_at": self._clock(),
            "entries": [e.to_dict() for e in self.entries.values()],
        }
        tmp = ""
        try:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=".surface.", suffix=".tmp",
                dir=os.path.dirname(self.persist_path))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.persist_path)
            self._dirty = False
        except (OSError, TypeError, ValueError) as exc:
            log.warning("surface ledger save failed: %s", exc)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def flush(self) -> None:
        """会话边界调用：只在有变更时落盘。"""
        if self._dirty:
            self.save()


def ledger_path(workdir: str, code: str, safe_code_fn) -> str:
    """账本路径单源：`<workdir>/.harness/surface/<safe>.json`。"""
    return os.path.join(workdir, HARNESS_DIR, "surface",
                        f"{safe_code_fn(code)}.json")
