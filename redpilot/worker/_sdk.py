"""官方 SDK 导入汇点:tsec_benchmark 的命名兼容层。

背景:本仓库代码统一使用 RedPilot 命名(`RedPilotmark` / `RedPilotmarkAsync`),
但 PyPI 上 `tsec-benchmark` 截至 0.1.2 导出的仍是 `TSecBenchmark` / `TSecBenchmarkAsync`
(经查无任何已发布版本提供 RedPilot 命名)。两者指向同一实现,差异只是类名。

在此做一次收敛,好处:
  - worker 在公开源上立刻可构建、可导入,不再因 SDK 命名而 ImportError;
  - 调用方只认本模块导出的名字,"SDK 改名"从线上事故降级为这里的一行别名。

沿革:这里原有一段 `try: from tsec_benchmark import <旧品牌名>` /
`except ImportError: ... as ...` 的双分支,理由是"上游某天改用本仓品牌名时自动跟上"。
2026-09 拆掉 —— 上游只会发行 TSec 命名的类,那个分支永远走不到,留着正是本模块
当初要消除的那类死代码。

调用方一律 `from ._sdk import ...`,不要直接 import tsec_benchmark。
"""

from __future__ import annotations

# 单性别名，不留 try/except 双分支：上游 `tsec-benchmark` 只会发行 TSec 命名的类
# （`RedPilotmark` 是本仓的品牌别名，PyPI 不会提供），保留"上游某天改叫 RedPilot"
# 的分支等于留一段永远走不到的死代码 —— 那正是本模块当初要消除的东西。
from tsec_benchmark import TSecBenchmark as RedPilotmark
from tsec_benchmark import TSecBenchmarkAsync as RedPilotmarkAsync

# 以下名字两侧同名,直接转出(已验证 0.1.2 全部提供)。
from tsec_benchmark import (  # noqa: E402
    Challenge,
    ChallengeNotFound,
    DuplicateSubmit,
    InvalidState,
    ResourceUnavailable,
    VpnCheckError,
)

__all__ = [
    "Challenge",
    "ChallengeNotFound",
    "DuplicateSubmit",
    "RedPilotmark",
    "RedPilotmarkAsync",
    "InvalidState",
    "ResourceUnavailable",
    "VpnCheckError",
]
