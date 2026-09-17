"""单一来源：同一领域对象/函数只许有一份定义（Phase 5 去重的回归守卫）。

历史债：AgentTask / build_task_prompt / extract_flags 曾在
`redpilot_worker/{task,taskprompt,flags}.py` 与 `adapter/` 各有一份，
orchestrator 用一份、`__init__` 导出另一份 —— 改 A 忘 B 是必然。
"""

from __future__ import annotations

import ast

from _archlib import ROOT, iter_files


def _definitions(kind: str, name: str) -> list[str]:
    hits = []
    node_types = (ast.ClassDef,) if kind == "class" else (ast.FunctionDef, ast.AsyncFunctionDef)
    for path in iter_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, node_types) and node.name == name:
                hits.append(str(path.relative_to(ROOT.parent)))
    return hits


def test_agent_task_defined_once():
    assert _definitions("class", "AgentTask") == ["redpilot/worker/adapter/task.py"]


def test_build_task_prompt_defined_once():
    assert _definitions("func", "build_task_prompt") == ["redpilot/worker/adapter/taskprompt.py"]


def test_extract_flags_defined_once():
    assert _definitions("func", "extract_flags") == ["redpilot/worker/adapter/solver/base.py"]


def test_version_defined_once():
    """单发行版 -> 单版本号：只在 redpilot/__init__.py 定义。"""
    hits = []
    for path in iter_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
                hits.append(str(path.relative_to(ROOT.parent)))
    assert hits == ["redpilot/__init__.py"], f"__version__ definitions: {hits}"
