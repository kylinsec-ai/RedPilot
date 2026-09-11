"""官方 SDK 导入汇点:tsec_benchmark 的命名兼容层。

背景:本仓库代码与 SDK_API.md 统一使用 Ghost 命名(`Ghostmark` / `GhostmarkAsync`),
但 PyPI 上 `tsec-benchmark` 截至 0.1.2 导出的仍是 `TSecBenchmark` / `TSecBenchmarkAsync`
(经查无任何已发布版本提供 Ghost 命名)。两者指向同一实现,差异只是类名。

在此做一次收敛,好处:
  - worker 在公开源上立刻可构建、可导入,不再因 SDK 命名而 ImportError;
  - SDK 日后发布 Ghost 命名版本时,本模块自动优先采用,无需改任何调用方;
  - "SDK 改名"从线上事故降级为这里的一行分支。

调用方一律 `from ._sdk import ...`,不要直接 import tsec_benchmark。
"""

from __future__ import annotations

try:  # 新命名(Ghost 品牌,见 SDK_API.md)
    from tsec_benchmark import Ghostmark, GhostmarkAsync
except ImportError:  # PyPI <= 0.1.2:TSec 命名;同一实现,仅类名不同
    from tsec_benchmark import TSecBenchmark as Ghostmark
    from tsec_benchmark import TSecBenchmarkAsync as GhostmarkAsync

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
    "Ghostmark",
    "GhostmarkAsync",
    "InvalidState",
    "ResourceUnavailable",
    "VpnCheckError",
]
