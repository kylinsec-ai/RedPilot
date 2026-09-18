"""契约常量守卫：路径常量与 compose 对齐 / 词汇一致性 / 信封剥离语义。

R1（contracts 零第三方依赖）不在本文件 —— 它是 `tests/architecture/test_layers.py`
的执行点，本文件曾有的第二份实现已于 2026-09 死码清扫删除（见下方沿革注）。"""

from __future__ import annotations

import re
from pathlib import Path

from redpilot.contracts.paths import HEARTBEAT_PATH
from redpilot.contracts.vocabulary import (ACTIVE_PHASES, FLUSH_KINDS, PHASES,
                                            strip_for_snapshot, strip_out_of_band)


# 沿革（2026-09 死码清扫）：此处原有 `test_contracts_is_stdlib_only`，即 R1
# （contracts 零第三方依赖）的**第二份实现**。同一条不变量在
# `tests/architecture/test_layers.py` 里有一份基于共享 `_archlib` 的版本。
# 实测过两者等价：往 contracts 里塞一行 `import requests`，**两条都变红**。
# 本仓对同型的处置有先例（`tests/architecture/test_archlib.py` 的模块 docstring：
# "同一套边，两份实现会漂移"），故删这份、留架构那份（它是 R1–R8 的权威落点）。


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
