"""R5：跨模块只走 façade —— `__all__` 白名单 + 禁私有/深 import。

装配根（组合根）例外：app.py 与 worker/driver.py 要接具体实现；
contracts 是内核，其子模块本身即公共 API（docs/modular-monolith-design.md §3.3）。
"""

from __future__ import annotations

import ast

from _archlib import (MODULES, ROOT, canon, iter_files, module_exists,
                      module_name, public_names, resolve_relative)

COMPOSITION_ROOTS = {"app.py", "worker/driver.py"}
KERNEL = "contracts"


def test_cross_module_imports_use_facade():
    violations: list[str] = []
    for path in iter_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel in COMPOSITION_ROOTS:
            continue
        mod, is_pkg = module_name(path)
        parts = mod.split(".")
        src_top = mod.split(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    target = canon(a.name)
                    tgt_top = target.split(".")[0]
                    if tgt_top not in MODULES or tgt_top in (src_top, KERNEL):
                        continue
                    violations.append(f"{rel}: deep import {target}")
            elif isinstance(node, ast.ImportFrom):
                target = resolve_relative(parts, is_pkg, node)
                if not target:
                    continue
                tgt_top = target.split(".")[0]
                if tgt_top not in MODULES or tgt_top in (src_top, KERNEL):
                    continue
                if target != tgt_top:  # 直接 import 目标模块的深层子模块
                    violations.append(f"{rel}: deep import from {target}")
                    continue
                allowed = public_names(tgt_top)
                for a in node.names:
                    if a.name == "*":
                        violations.append(f"{rel}: star import from {target}")
                    elif a.name.startswith("__") and a.name.endswith("__"):
                        continue  # 版本等双下划线元数据不算模块 API
                    elif module_exists(f"{tgt_top}.{a.name}"):
                        violations.append(f"{rel}: deep import {tgt_top}.{a.name}")
                    elif a.name not in allowed:
                        violations.append(
                            f"{rel}: {a.name!r} not in redpilot.{tgt_top}.__all__")
    assert not violations, "cross-module imports bypassing facade:\n" + "\n".join(violations)
