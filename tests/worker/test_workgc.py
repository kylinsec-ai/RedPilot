"""M4：/work 保留策略（默认 dry-run + 白名单）的执行点。

设计 `docs/solver-isolation-design.md` §7：先报告后删除；题目记忆
（MEMORY/黑板/转录/.closed）永不删；digest 未进观测的题目跳过。
"""

from __future__ import annotations

import json
import os
import time

from redpilot.worker.adapter.config import WorkGcConfig
from redpilot.worker.adapter import workgc

DAY = 86400.0


def _make_challenge(workdir, name, *, age_days=30.0, files=("artifacts/x.bin",)):
    d = workdir / name
    d.mkdir(parents=True)
    for rel in files:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)
    old = time.time() - age_days * DAY
    os.utime(d, (old, old))
    return d


def _write_digest(workdir, name):
    dg = workdir / ".live" / "digests"
    dg.mkdir(parents=True, exist_ok=True)
    (dg / f"{name}.json").write_text("{}", encoding="utf-8")


def test_scan_skips_young_control_and_dotdirs(tmp_path):
    _make_challenge(tmp_path, "old-chal", age_days=30)
    _make_challenge(tmp_path, "new-chal", age_days=1)
    (tmp_path / ".harness").mkdir()
    (tmp_path / "status").mkdir()
    report = workgc.scan_workdir(str(tmp_path), retention_days=14)
    codes = [c["code"] for c in report["candidates"]]
    skipped = [s["code"] for s in report["skipped"]]
    assert codes == [], "无 digest 的题目不得进可删名单"
    assert skipped == ["old-chal"], skipped
    assert report["total_bytes"] == 0


def test_scan_candidate_with_digest(tmp_path):
    _make_challenge(tmp_path, "old-chal", age_days=30)
    _write_digest(tmp_path, "old-chal")
    report = workgc.scan_workdir(str(tmp_path), retention_days=14)
    assert [c["code"] for c in report["candidates"]] == ["old-chal"]
    assert report["candidates"][0]["bytes"] > 0


def test_apply_keeps_whitelist(tmp_path):
    d = _make_challenge(tmp_path, "old-chal", age_days=30,
                        files=("artifacts/x.bin", "scan.txt"))
    (d / "MEMORY.md").write_text("notes", encoding="utf-8")
    (d / "_blackboard.json").write_text("{}", encoding="utf-8")
    workgc.mark_closed(str(d), "solved")
    _write_digest(tmp_path, "old-chal")
    # 终态时间 = 30 天前（mark_closed 只能写当下，测试里回填旧时间）
    (d / ".closed").write_text(
        json.dumps({"reason": "solved", "at": time.time() - 30 * DAY}),
        encoding="utf-8")

    report = workgc.scan_workdir(str(tmp_path), retention_days=14)
    res = workgc.apply_gc(str(tmp_path), report["candidates"])
    assert res["removed"] >= 1 and res["freed_bytes"] > 0
    assert (d / "MEMORY.md").is_file()
    assert (d / "_blackboard.json").is_file()
    assert (d / ".closed").is_file()
    assert not (d / "artifacts").exists()
    assert not (d / "scan.txt").exists()


def test_run_once_defaults_to_dry_run(tmp_path):
    _make_challenge(tmp_path, "old-chal", age_days=30, files=("artifacts/x.bin",))
    _write_digest(tmp_path, "old-chal")
    cfg = WorkGcConfig(enabled=True, apply=False, retention_days=14)
    res = workgc.run_once(str(tmp_path), cfg)
    assert res["dry_run"] is True and res["candidates"] == 1
    assert (tmp_path / "old-chal" / "artifacts" / "x.bin").is_file(), "dry-run 不得删除"


def test_run_once_apply_deletes_only_old(tmp_path):
    _make_challenge(tmp_path, "old-chal", age_days=30, files=("artifacts/x.bin",))
    _write_digest(tmp_path, "old-chal")
    cfg = WorkGcConfig(enabled=True, apply=True, retention_days=14)
    res = workgc.run_once(str(tmp_path), cfg)
    assert res["dry_run"] is False and res["removed"] >= 1
    assert not (tmp_path / "old-chal" / "artifacts").exists()


def test_mark_closed_is_readable_metadata(tmp_path):
    d = _make_challenge(tmp_path, "chal", age_days=1)
    workgc.mark_closed(str(d), "solved")
    data = json.loads((d / ".closed").read_text(encoding="utf-8"))
    assert data["reason"] == "solved" and data["at"] > 0


def test_closed_marker_time_wins_over_dir_mtime(tmp_path):
    """dir mtime 会被后续写入刷新，年龄必须以 .closed 标记为准。"""
    d = _make_challenge(tmp_path, "chal", age_days=1)
    workgc.mark_closed(str(d), "solved")
    (d / "late-write.txt").write_text("x", encoding="utf-8")   # 刷新目录 mtime
    old = time.time() - 30 * DAY
    (d / ".closed").write_text(json.dumps({"reason": "solved", "at": old}),
                               encoding="utf-8")
    _write_digest(tmp_path, "chal")
    report = workgc.scan_workdir(str(tmp_path), retention_days=14)
    assert [c["code"] for c in report["candidates"]] == ["chal"]
