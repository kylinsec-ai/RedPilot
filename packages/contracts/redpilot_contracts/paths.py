"""路径与文件名约定 — 进程间文件契约的单一来源。

safe_code 此前在 drivers/roster.py(_safe_code,driver/status_server 惰性取);
/work 布局常量此前散在 driver/roster/status_server 的字符串字面量里;
HEARTBEAT_PATH 此前在 adapter/solver/base.py,同时被 compose healthcheck 以
字面量读取 —— 改动此处须同步 docker-compose.yaml。
"""

from __future__ import annotations

import hashlib
import os
import re

# ── FLAG 候选文件读法(relay 兜底与 challenge_detail 共用) ──

FLAG_FILES: tuple[str, ...] = ("FLAG", "flag.txt", "FLAG.txt")
FLAG_MAX_LINES = 50


def safe_code(code: str) -> str:
    """code -> 安全目录名(sanitize + sha1 hash 后缀)。全仓唯一实现:
    驱动/仪表板/中继都从这里取(纯 stdlib,不触发 SDK 导入)。
    产出必须与 scan_local 的可逆判定(_dir_maps_to_code)一致。"""
    raw = str(code)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:64] or "chal"
    return safe if safe == raw else f"{safe}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


# ── /work 布局 ──

LIVE_DIR = ".live"                    # workdir 下实时状态目录
LIVE_STATE_FMT = "{worker_id}.json"    # .live/<worker_id>.json
ROSTER_FILENAME = "roster.json"        # .live/roster.json
DIGESTS_DIR = "digests"                # .live/digests/<code>.json
TRANSCRIPT_FILENAME = "transcript.jsonl"  # <workdir>/<safe_code>/transcript.jsonl

# 心跳文件(compose healthcheck 以同一字面量读取 —— 两处必须同步改)
HEARTBEAT_PATH = "/tmp/driver_heartbeat"


# ── skills/ 根目录定位 ──

# 上溯搜索的最大层数。真实布局里 `adapter/solver/pi_agent.py` 距仓库根 4 层
# （pi_agent → solver → adapter → redpilot_worker → packages → 仓库根），留一层余量。
_SKILLS_WALK_MAX = 6


def skills_root(start_file: str, *, extra: str = "") -> str:
    """从 `start_file` 定位仓库/镜像里的 `skills/` 目录；找不到返回 ""。

    为什么需要这个函数（而不是继续在调用点 `dirname(dirname(__file__))`）：
    那两处**数固定层数**的写法在朋友的目录布局里是对的，而策略层搬进
    `packages/worker/redpilot_worker/adapter/` 之后**全部指错** —— 指向一个不存在的
    `packages/worker/skills`，于是技能扫描静默退化成 0 个（打一条 warning 就完事，
    不报错）。数层数这种写法换一次布局就会再坏一次。

    优先级：
      1. `ADAPTER_SKILLS_DIR` 环境变量（运维显式指定，compose 里设的就是它）
      2. `extra`（容器侧传 `/app/skills`：镜像里那条不随 HOME 改写消失的路径）
      3. 从 `start_file` 逐级上溯找**含 SKILL.md 子目录**的 `skills/`
         （要求含 SKILL.md 是为了不误命中一个同名的空目录）

    纯 stdlib、无副作用；契约包不得依赖任何第三方（见 tests/test_purity.py）。
    """
    candidates: list[str] = []
    env_dir = (os.environ.get("ADAPTER_SKILLS_DIR") or "").strip()
    if env_dir:
        candidates.append(env_dir)
    if extra:
        candidates.append(extra)
    cur = os.path.dirname(os.path.abspath(start_file))
    for _ in range(_SKILLS_WALK_MAX):
        candidates.append(os.path.join(cur, "skills"))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    for cand in candidates:
        if not os.path.isdir(cand):
            continue
        try:
            has_skill = any(
                os.path.isfile(os.path.join(cand, e, "SKILL.md"))
                for e in os.listdir(cand)
            )
        except OSError:
            continue
        if has_skill:
            return cand
    return ""
