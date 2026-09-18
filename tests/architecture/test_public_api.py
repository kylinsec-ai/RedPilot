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


def test_every_dunder_all_entry_is_actually_bound():
    """`__all__` 里的名字必须真实存在 —— 否则它是一句**没人验证的承诺**。

    为什么需要这条：上面那条守卫只检查**消费者**有没有用 `__all__` 里的名字，
    从不检查 `__all__` 里的名字是否真的存在。于是一个指向已删符号的条目，
    只要没人 import 它，就永远绿 —— 与「守卫的绿分不清『没问题』与『没在看』」
    同型。

    实测来源：2026-09 拆除 canonical 通道时，`contracts/__init__.py` 的 `__all__`
    里留着 `EventEnvelope` / `is_canonical_event_type` 等 7 个已删符号，全套
    架构测试没有任何一条变红。

    纯 AST：不 import 被测代码（跨模块 import 会拉起 fastapi 等依赖）。
    """
    violations: list[str] = []
    for path in iter_files():
        if path.name != "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # ast.walk 覆盖 tree.body 及其子树：条件导入（try/except、if TYPE_CHECKING）
        # 里的绑定也算数，故不需要再单独扫顶层。
        bound: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.Assign):
                bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
            elif isinstance(node, ast.Import):
                bound.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                bound.update(a.asname or a.name for a in node.names)
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for entry in _dunder_all(tree):
            if entry not in bound and entry != "*":
                violations.append(f"{rel}: __all__ 列出了未绑定的名字 {entry!r}")
    assert not violations, "__all__ 指向不存在的符号:\n" + "\n".join(violations)


def _dunder_all(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                return [e.value for e in node.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []
