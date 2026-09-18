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

# 控制面（harness）状态目录：与题目目录（求解 Agent 可写）分离的唯一落点。
# 预算状态、面预算账本等"裁决状态"只能写这里；题目目录里放的只能是工作记忆
# （MEMORY/黑板/FLAG/转录）。完整论证与权限矩阵见 docs/solver-isolation-design.md §4–§5。
# 沿革（2026-09 死码清扫）：这里原有 `harness_dir()` / `harness_subdir()` 两个
# 便捷函数。删除理由是零调用 —— 真正的消费方（`adapter/stoploss.py:152`、
# `adapter/surface.py:473`、`adapter/workgc.py:25`）都 import 本常量自己 join，
# 那两个包装函数从加进来那天起就没有过调用方。**拼装遵守同一约定即可，不必经过 helper。**
HARNESS_DIR = ".harness"
HARNESS_STOPLOSS_SUBDIR = "stoploss"

# 心跳文件(compose healthcheck 以同一字面量读取 —— 两处必须同步改)
HEARTBEAT_PATH = "/tmp/driver_heartbeat"


# ── skills/ 根目录定位 ──


def is_skill_dir(path: str) -> bool:
    """目录是否算一个技能：**含 SKILL.md**。全仓唯一判据。

    两个消费者必须同判据，否则会出现"名录里有、pi 发现不了"的静默错位：
      - `worker/adapter/skill_loader.py` 的 `SkillStore._scan`（名录）
      - `worker/adapter/solver/pi_agent.py` 的 `_install_skills`（软链装载）
    """
    return os.path.isfile(os.path.join(path, "SKILL.md"))


def skills_root(start_file: str, *, extra: str = "") -> str:
    """从 `start_file` 定位仓库/镜像里的 `skills/` 目录；找不到返回 ""。

    为什么需要这个函数（而不是继续在调用点 `dirname(dirname(__file__))`）：
    那两处**数固定层数**的写法在朋友的目录布局里是对的，而策略层两次搬迁
    （packages/worker/redpilot_worker/adapter/ → redpilot/worker/adapter/）之后
    **全部指错** —— 指向一个不存在的 skills/ 路径，于是技能扫描静默退化成 0 个
    （打一条 warning 就完事，不报错）。数层数这种写法换一次布局就会再坏一次。

    优先级：
      1. `ADAPTER_SKILLS_DIR` 环境变量（运维显式指定，compose 里设的就是它）
      2. `extra`（容器侧传 `/app/skills`：镜像里那条不随 HOME 改写消失的路径）
      3. 从 `start_file` 逐级上溯找**含 SKILL.md 子目录**的 `skills/`
         （要求含 SKILL.md 是为了不误命中一个同名的空目录）

    上溯**走到文件系统根为止**，不设层数上限：此前是定死的 6 层，而最深的调用方
    （`adapter/solver/pi_agent.py`）恰好用满第 6 层——再多一层包目录就静默归零，
    正是这个函数当初要修的那类故障。层数上限换个布局就要再调一次，索性去掉。

    纯 stdlib、无副作用；契约包不得依赖任何第三方（见 tests/test_purity.py）。
    """
    candidates: list[str] = []
    env_dir = (os.environ.get("ADAPTER_SKILLS_DIR") or "").strip()
    if env_dir:
        candidates.append(env_dir)
    if extra:
        candidates.append(extra)
    cur = os.path.dirname(os.path.abspath(start_file))
    while True:
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
                is_skill_dir(os.path.join(cand, e)) for e in os.listdir(cand)
            )
        except OSError:
            continue
        if has_skill:
            return cand
    return ""
