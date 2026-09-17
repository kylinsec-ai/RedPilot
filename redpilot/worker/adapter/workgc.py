"""`/work` 保留策略（M4，设计 `docs/solver-isolation-design.md` §7）。

obs 侧的事件流有保留天数与分批删除；`/work` 没有 —— 靶标产物、扫描输出、
临时 venv 永久堆积，先撑爆 bind 挂载的宿主盘。本模块只做一件事：**按题目
目录的年龄与白名单，报告（默认）或删除**。

安全优先的三个设计：
  · **默认 dry-run**（`ADAPTER_WORK_GC=0`）：先跑一段只会产生 `work.gc_report`
    事件的报告模式，人工确认后再开真删；
  · **白名单**：`MEMORY.md` / `_blackboard.json` / `transcript.jsonl` / `.closed`
    永不删除（题目记忆与承接），控制面目录（`.harness`/`.live`/`status`/
    `.stoploss-locks`）不进入扫描；
  · **digest 前置**：真删前要求 `<workdir>/.live/digests/<dir>.json` 已存在
    （说明观测侧已留存该题的折叠时间线），否则跳过并在报告里标原因。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time

from redpilot.contracts.paths import HARNESS_DIR, LIVE_DIR

log = logging.getLogger("adapter.workgc")

# 永不删除（题目记忆与续接入口）。
KEEP_FILES = frozenset({"MEMORY.md", "_blackboard.json", "transcript.jsonl",
                        ".closed", ".pi-home"})
# 不参与扫描的控制面/观测目录。
SKIP_DIRS = frozenset({HARNESS_DIR, LIVE_DIR, "status", ".stoploss-locks",
                       "_transcripts", "digests"})
DIGESTS_DIR = os.path.join(LIVE_DIR, "digests")


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _age_days(path: str, now: float) -> float:
    """目录年龄（天）。有 `.closed` 标记时以标记时间为准 —— 目录 mtime 会被
    后续清理/写入刷新，不能代表题目终态时间。"""
    closed = os.path.join(path, ".closed")
    try:
        if os.path.isfile(closed):
            with open(closed, encoding="utf-8") as f:
                at = float(json.load(f).get("at", 0) or 0)
            if at > 0:
                return max(0.0, (now - at) / 86400.0)
    except (OSError, ValueError, TypeError):
        pass
    try:
        return max(0.0, (now - os.path.getmtime(path)) / 86400.0)
    except OSError:
        return 0.0


def scan_workdir(workdir: str, *, retention_days: int = 14,
                 now: float | None = None) -> dict:
    """扫描可回收的题目目录（只读，不删任何东西）。"""
    now = time.time() if now is None else now
    candidates, skipped = [], []
    try:
        names = sorted(os.listdir(workdir))
    except OSError:
        return {"candidates": [], "skipped": [], "total_bytes": 0}
    for name in names:
        if name in SKIP_DIRS or name.startswith("."):
            continue
        path = os.path.join(workdir, name)
        if not os.path.isdir(path):
            continue
        age = _age_days(path, now)
        if age < max(0, retention_days):
            continue
        digest = os.path.join(workdir, DIGESTS_DIR, f"{name}.json")
        entry = {"code": name, "age_days": round(age, 2),
                 "bytes": _dir_size(path)}
        if os.path.isfile(digest):
            entry["digest"] = True
            candidates.append(entry)
        else:
            entry["reason"] = "skipped_no_digest"
            skipped.append(entry)
    return {"candidates": candidates, "skipped": skipped,
            "total_bytes": sum(c["bytes"] for c in candidates)}


def apply_gc(workdir: str, candidates: list[dict]) -> dict:
    """真删：只删白名单之外的文件，保留目录本身与白名单条目。"""
    removed = 0
    freed = 0
    errors = 0
    for item in candidates:
        path = os.path.join(workdir, item["code"])
        try:
            for name in os.listdir(path):
                if name in KEEP_FILES:
                    continue
                fp = os.path.join(path, name)
                try:
                    if os.path.islink(fp) or os.path.isfile(fp):
                        size = os.path.getsize(fp)
                        os.unlink(fp)
                        removed += 1
                        freed += size
                    elif os.path.isdir(fp):
                        freed += _dir_size(fp)
                        shutil.rmtree(fp, ignore_errors=True)
                        removed += 1
                except OSError:
                    errors += 1
        except OSError:
            errors += 1
    return {"removed": removed, "freed_bytes": freed, "errors": errors}


def mark_closed(challenge_dir: str, reason: str = "solved") -> None:
    """写题目终态标记（年龄基准 + 人读元数据；白名单文件，永不被回收）。"""
    try:
        with open(os.path.join(challenge_dir, ".closed"), "w", encoding="utf-8") as f:
            json.dump({"reason": str(reason)[:64], "at": time.time()}, f)
    except OSError:
        pass


def run_once(workdir: str, cfg, *, now: float | None = None) -> dict:
    """heartbeat 周期调用：扫描 +（可选）真删，返回可直接进观测的报告。"""
    retention = max(1, int(getattr(cfg, "retention_days", 14) or 14))
    scan = scan_workdir(workdir, retention_days=retention, now=now)
    apply = bool(getattr(cfg, "apply", False))
    result = {"dry_run": not apply, "retention_days": retention,
              "candidates": len(scan["candidates"]),
              "skipped_no_digest": len(scan["skipped"]),
              "total_bytes": scan["total_bytes"]}
    if apply and scan["candidates"]:
        result.update(apply_gc(workdir, scan["candidates"]))
    log.info("work gc: %s", json.dumps(result, ensure_ascii=False))
    return result
