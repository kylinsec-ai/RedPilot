"""契约纯度与常量守卫:contracts 零第三方依赖 / 路径常量与 compose 对齐 / 词汇一致性。"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

from redpilot.contracts.paths import HEARTBEAT_PATH
from redpilot.contracts.vocabulary import (ACTIVE_PHASES, FLUSH_KINDS, PHASES,
                                            strip_for_snapshot, strip_out_of_band)

_CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "redpilot" / "contracts"

_ALLOWED_STDLIB = set(sys.stdlib_module_names)


def _iter_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue  # 包内相对 import,必为自身
            if node.module:
                yield node.module


def test_contracts_is_stdlib_only():
    """contracts 源码只允许 stdlib 与自身 import(零依赖承诺的执行点)。"""
    for py in _CONTRACTS_DIR.rglob("*.py"):
        for mod in _iter_imports(py):
            root = mod.split(".")[0]
            assert root in _ALLOWED_STDLIB or root == "redpilot.contracts", \
                f"{py.name}: non-stdlib import {mod!r}"


def test_heartbeat_path_matches_compose():
    """HEARTBEAT_PATH 与 docker-compose healthcheck 字面量对齐(双语言契约防漂移)。"""
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yaml"
    if not compose.is_file():  # 独立安装时跳过(仓库内跑必在)
        return
    text = compose.read_text(encoding="utf-8")
    assert HEARTBEAT_PATH in text, "compose healthcheck no longer reads HEARTBEAT_PATH literal"


def test_active_phases_subset_of_phases():
    assert set(ACTIVE_PHASES) <= set(PHASES)
    assert "idle" in PHASES and "error" in PHASES


def test_flush_kinds_include_boundaries():
    assert {"tool_end", "turn_done", "error", "system", "lifecycle"} == set(FLUSH_KINDS)


def test_strip_out_of_band_keeps_envelope():
    frame = {"kind": "closing", "ts": 1.0, "phase": "closing", "_accepted_flags": ["flag{x}"]}
    out = strip_out_of_band(frame)
    assert out == {"kind": "closing", "ts": 1.0, "phase": "closing"}


def test_strip_for_snapshot_drops_envelope_and_oob():
    frame = {"kind": "closing", "ts": 1.0, "phase": "closing", "_accepted_flags": ["flag{x}"]}
    out = strip_for_snapshot(frame)
    assert out == {"phase": "closing"}


def test_snapshot_keys_are_snake_case_unique():
    from redpilot.contracts.snapshot import LIVE_SNAPSHOT_KEYS
    assert len(LIVE_SNAPSHOT_KEYS) == 18
    assert len(set(LIVE_SNAPSHOT_KEYS)) == 18
    assert all(re.fullmatch(r"[a-z_][a-z0-9_]*", k) for k in LIVE_SNAPSHOT_KEYS)


def test_safe_code_matches_local_scan_rule():
    from redpilot.contracts.paths import safe_code
    # 可逆(纯 sanitize)名:原样返回
    assert safe_code("a-05") == "a-05"
    # 含非法字符:hash 后缀映射名(无法反解 -> scan_local 按目录名兜底)
    mapped = safe_code("a/05:x")
    assert "/" not in mapped and ":" not in mapped
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}-[0-9a-f]{6}", mapped)
    # 空串兜底(空 sanitize 后为 "chal",与原始 code 不同 → 带 hash 后缀)
    assert re.fullmatch(r"chal-[0-9a-f]{6}", safe_code(""))
