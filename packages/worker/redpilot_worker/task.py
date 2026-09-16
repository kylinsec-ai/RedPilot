"""
统一任务抽象 AgentTask
封装单个挑战的元信息，作为本地/托管驱动的公共入口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentTask:
    """单个挑战/题目的任务描述"""

    # 任务目标描述
    objective: str

    # 目标地址列表 (IP:port / URL)
    targets: list[str] = field(default_factory=list)

    # 预期 flag 数量
    flag_count: int = 1

    # flag 格式正则 (如 flag{...})
    flag_format: Optional[str] = None

    # 工作目录
    workdir: str = "/work"

    # 题目分类 (web/pwn/crypto/cloud/reverse/forensics/misc/pentest/evasion/mobile/blockchain)
    category: Optional[str] = None

    # 难度等级
    difficulty: Optional[str] = None

    # 题目唯一标识码
    unique_code: Optional[str] = None

    # 分值
    score: int = 0

    # 平台声明的本地材料元数据(文件名/字典)。**只用于来源边界判定,绝不下载**:
    # 有声明附件的题才允许"本地静态产物算证据"(见 adapter/verify.py 的
    # flag_evidence_policy)。朋友侧同名同义字段,合并后取并集。
    files: list = field(default_factory=list)

    def target_str(self) -> str:
        """格式化目标地址为可读字符串"""
        return ", ".join(self.targets) if self.targets else "(no network target; local files only)"
