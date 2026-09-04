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

log = logging.getLogger("adapter.solver")

# flag 提取正则
_FLAG_RX = re.compile(r"flag\{[^}]{1,200}\}", re.IGNORECASE)
_FINAL_ANSWER_RX = re.compile(r"<FinalAnswer>(.*?)</FinalAnswer>", re.DOTALL)
# flag body 合法字符：字母数字 + 常见分隔符（防命令注入 payload 误提取）
_FLAG_BODY_RX = re.compile(r"^[A-Za-z0-9_\-.:/]{3,200}$")


def flag_body(flag: str) -> str:
    """提取 flag{} 内主体；无完整外壳时原样返回"""
    m = re.match(r"flag\{(.+)\}", flag, re.IGNORECASE)
    return m.group(1) if m else flag


def normalize_flag_body(flag: str) -> str:
    """去外壳 + 去空白 + 小写：跨会话去重与提交前归一化比较"""
    return flag_body(flag).strip().lower()


def is_valid_flag(flag: str) -> bool:
    """校验 flag 整体合法性：外壳完整 + body 无引号/空格/命令字符"""
    body = flag_body(flag)
    return body != flag and bool(_FLAG_BODY_RX.match(body))


def extract_flags(text: str) -> list[str]:
    """从文本中提取所有 flag{...} 格式的候选（过滤非法 body）"""
    if not text:
        return []
    found = set()
    for m in _FLAG_RX.finditer(text):
        f = m.group(0)
        if is_valid_flag(f):
            found.add(f)
    for m in _FINAL_ANSWER_RX.finditer(text):
        for fm in _FLAG_RX.finditer(m.group(1)):
            if is_valid_flag(fm.group(0)):
                found.add(fm.group(0))
    return list(found)


@dataclass
class SolveResult:
    """Agent 会话执行结果（各后端统一输出）"""
    flags: list[str] = field(default_factory=list)
    tool_outputs: list = field(default_factory=list)
    observed_output: str = ""
    error: str = ""
    turns: int = 0
    duration_s: float = 0.0
    infra_blocked: bool = False

    @property
    def has_flags(self) -> bool:
        return bool(self.flags)


# ── 心跳文件 ─────────────────────────────────────────────

HEARTBEAT_PATH = "/tmp/driver_heartbeat"


def touch_heartbeat() -> None:
    """更新心跳文件 mtime（失败静默）— driver 与 solver 会话共用"""
    try:
        with open(HEARTBEAT_PATH, "a"):
            os.utime(HEARTBEAT_PATH, None)
    except Exception:
        pass


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
        on_event: Optional[Callable] = None,
    ) -> SolveResult:
        """执行一次解题会话，返回统一结果"""

    @staticmethod
    def _read_flag_files(workdir: str, flags: list[str]) -> list[str]:

        """从工作目录的标准 flag 文件补录候选"""
        for name in ("FLAG", "flag.txt", "FLAG.txt"):
            p = os.path.join(workdir, name)
            try:
                if os.path.isfile(p):
                    with open(p, encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            v = line.strip()
                            if v and "{" in v and v.endswith("}") and len(v) <= 200:
                                if v not in flags:
                                    flags.append(v)
            except Exception:
                pass
        return flags