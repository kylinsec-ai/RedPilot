"""架构测试共用工具：AST import 图解析（纯 stdlib，不 import 被测代码）。

`ROOT` 指向仓库内的 `redpilot/` 包目录；所有名字都归一化为「仓库内规范名」：
`redpilot.control.store` -> `control.store`，`redpilot` -> `redpilot`。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "redpilot"
PKG = "redpilot"
STDLIB = set(sys.stdlib_module_names)
MODULES = ("contracts", "control", "obs", "worker", "app")


def module_name(path: Path) -> tuple[str, bool]:
    """返回（仓库内规范模块名, 是否包 __init__）。"""
    rel = path.relative_to(ROOT)
    if rel.name == "__init__.py":
        return ".".join(rel.parent.parts), True
    return ".".join(rel.with_suffix("").parts), False


def canon(dotted: str) -> str:
    return dotted[len(PKG) + 1:] if dotted.startswith(PKG + ".") else dotted


def resolve_relative(parts: list[str], is_pkg: bool, node: ast.ImportFrom) -> str:
    """相对 import -> 仓库内规范模块名。level 在包 __init__ 与普通模块中语义不同。"""
    if not node.level:
        return canon(node.module or "")
    drop = node.level - 1 if is_pkg else node.level
    base = parts[: len(parts) - drop] if drop else parts
    return ".".join(base + ([node.module] if node.module else []))


def module_exists(dotted: str) -> bool:
    p = ROOT.joinpath(*dotted.split("."))
    return p.with_suffix(".py").is_file() or (p / "__init__.py").is_file()


def iter_files():
    for p in sorted(ROOT.rglob("*.py")):
        if "__pycache__" not in p.parts:
            yield p


def imports(path: Path):
    """产出 (源模块规范名, 目标模块规范名)；`from pkg import submodule` 会补全子模块。"""
    mod, is_pkg = module_name(path)
    parts = mod.split(".")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield mod, canon(a.name)
        elif isinstance(node, ast.ImportFrom):
            target = resolve_relative(parts, is_pkg, node)
            if not target:
                continue
            yield mod, target
            for a in node.names:
                # 先归一化再查存在性。`from redpilot import control` 这类：target 是
                # "redpilot"（不带点，canon 不减前缀），拼出来是 "redpilot.control"，
                # 去 ROOT/redpilot/control 找必然落空 —— 于是这条**看起来像依赖子模块**
                # 的写法会被静默放过，正是禁边守卫最该拦住的一种。canon 之后为
                # "control"，与 `from redpilot.control import x` 走同一条路径。
                sub = canon(f"{target}.{a.name}")
                if module_exists(sub):
                    yield mod, sub


def public_names(mod: str) -> set[str]:
    """模块的公共 API：`__all__` 优先；否则取 `__init__` 顶层非下划线定义/赋值。"""
    init = ROOT.joinpath(*mod.split(".")) / "__init__.py"
    if not init.is_file():
        return set()
    tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                    names |= {e.value for e in node.value.elts
                              if isinstance(e, ast.Constant) and isinstance(e.value, str)}
                elif isinstance(t, ast.Name) and not t.id.startswith("_"):
                    names.add(t.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
    return names
