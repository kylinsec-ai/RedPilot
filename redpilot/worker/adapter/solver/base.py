"""
Agent 能力层 — 统一结果模型、flag 提取与 SolverBackend 抽象接口

设计目标：解题 Agent（solver）固定使用 Pi Agent 编排网络安全 Agent。
- pi_agent.py      Pi Agent CLI 适配器（唯一求解引擎）
- factory.py       创建 Pi Agent 后端

上层（driver）只依赖本文件的 SolveResult 与 solve() 接口。
"""

from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Optional

# [B55b] FLAG 文件候选口径的唯一来源 —— 与 driver 侧 _read_flag_file 共用，
# 别再各写一套（B55 的根因就是两层口径漂移）。verify.py 是叶子模块，无循环依赖。
from ..verify import flag_line_candidate

log = logging.getLogger("adapter.solver")

# flag 提取正则
_FLAG_RX = re.compile(r"flag\{[^}]{1,200}\}", re.IGNORECASE)
_FINAL_ANSWER_RX = re.compile(r"<FinalAnswer>(.*?)</FinalAnswer>", re.DOTALL)
# flag body 合法字符：字母数字 + 常见分隔符（防命令注入 payload 误提取）
_FLAG_BODY_RX = re.compile(r"^[A-Za-z0-9_\-.:/]{3,200}$")


def is_valid_flag(flag: str) -> bool:
    """校验 flag 整体合法性：外壳完整 + body 无引号/空格/命令字符"""
    m = re.match(r"flag\{(.+)\}", flag, re.IGNORECASE)
    if not m:
        return False
    return bool(_FLAG_BODY_RX.match(m.group(1)))


def extract_flags(text: str) -> list[str]:
    """从文本中提取所有 flag{...} 格式的候选（过滤非法 body）"""
    if not text:
        return []
    # 提交顺序也属于解题流程：多 flag 题若使用 set，Python hash 随进程改变，
    # 同一份工具输出会得到随机投递顺序。保留首现顺序，同时仍做精确去重。
    found: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)

    for m in _FLAG_RX.finditer(text):
        f = m.group(0)
        if is_valid_flag(f):
            _add(f)
    for m in _FINAL_ANSWER_RX.finditer(text):
        for fm in _FLAG_RX.finditer(m.group(1)):
            if is_valid_flag(fm.group(0)):
                _add(fm.group(0))
    return found


# ── 续接块（B14）──────────────────────────────────────────────
# prompt 要求 agent 未解出时在结尾输出「已达成原语/已证死路/下一步」，
# 但 SolveResult.handoff 全仓没有赋值点 → 最有价值的跨场交接信息（已证
# 死路、下一步）100% 丢失。这里提供从文本回捞的解析器，solver 与 driver 共用。
HANDOFF_KEYS = ("已达成原语", "已证死路", "下一步")
_HANDOFF_VALUE_MAX = 400      # 单字段保留上限
_HANDOFF_TOTAL_MAX = 1600     # 单块扫描窗口上限
_HANDOFF_PLACEHOLDER_RX = re.compile(r"^<[^>]{0,40}>$")


def extract_handoff(text: str) -> str:
    """从文本中回捞最后一次出现的续接块。

    - 块起点取最后一次「已达成原语」（完整块开头）；只有部分字段时退到
      最后一个已出现的键
    - 字段之间允许空行（真实块就是空行分隔）；空行后若下一个非空行是新
      字段键则继续，否则视为块结束；markdown 标题/代码围栏也终止块
    - 占位符（`<进展>` 等模板原文）与空值丢弃
    """
    if not text:
        return ""
    pos = -1
    for key in HANDOFF_KEYS:
        pos = text.rfind(key)
        if pos >= 0:
            break
    if pos < 0:
        return ""
    lines = text[pos:pos + _HANDOFF_TOTAL_MAX].splitlines()
    fields: list[list[str]] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        key = next((k for k in HANDOFF_KEYS if s.startswith(k)), None)
        if not key:
            i += 1
            continue
        fields.append([key, s[len(key):].lstrip(" \t:：*").strip()])
        i += 1
        while i < len(lines):
            s2 = lines[i].strip()
            if any(s2.startswith(k) for k in HANDOFF_KEYS):
                break                      # 下一个字段，交回外层
            if not s2:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                nxt = lines[j].strip() if j < len(lines) else ""
                i = j if (nxt and any(nxt.startswith(k) for k in HANDOFF_KEYS)) else len(lines)
                break
            if s2.startswith(("#", "```", "---")):
                i = len(lines)
                break
            fields[-1][1] = (fields[-1][1] + " " + s2).strip() if fields[-1][1] else s2
            i += 1
    out = []
    for k, v in fields:
        v = v.strip().strip("*").strip()
        if not v or _HANDOFF_PLACEHOLDER_RX.match(v):
            continue
        out.append(f"{k}: {v[:_HANDOFF_VALUE_MAX]}")
    return "\n".join(out)


@dataclass
class SolveResult:
    """Agent 会话执行结果（各后端统一输出）"""
    flags: list[str] = field(default_factory=list)
    final_answer: str = ""
    final_text: str = ""
    handoff: str = ""
    tool_outputs: list = field(default_factory=list)
    observed_output: str = ""
    error: str = ""
    turns: int = 0
    duration_s: float = 0.0
    termination_reason: str = ""  # completed / timeout / stalled / stopped / max_turns / error
    infra_blocked: bool = False
    target_fault: bool = False   # B16：目标端口通但服务持续 5xx/后端崩溃

    @property
    def has_flags(self) -> bool:
        return bool(self.flags)


class SolverBackend(ABC):
    """
    Agent 求解器抽象接口。
    所有求解器实现本接口，上层 driver 只依赖 solve()。
    """

    name: str = "abstract"

    @abstractmethod
    def solve(
        self,
        prompt: str,
        workdir: str,
        cfg,
        *,
        flag_format: str = "flag{...}",
        on_fact: Optional[Callable] = None,
        transcript_path: Optional[str] = None,
        max_retries: int = 2,
    ) -> SolveResult:
        """执行一次解题会话，返回统一结果"""

    @staticmethod
    def _read_flag_files(workdir: str, flags: list[str]) -> list[str]:
        """从工作目录的标准 flag 文件补录候选。

        [B55b] 判据统一走 `verify.flag_line_candidate`（信封 + 裸答案），
        与 driver 侧 `_read_flag_file` 同一口径。
        """
        for name in ("FLAG", "flag.txt", "FLAG.txt"):
            p = os.path.join(workdir, name)
            try:
                if os.path.isfile(p):
                    with open(p, encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            v = line.strip()
                            if flag_line_candidate(v) and v not in flags:
                                flags.append(v)
            except Exception:
                pass
        return flags
