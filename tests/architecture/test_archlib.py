"""守卫自检：架构边界守卫必须真的**看得见**违规，而不是「没在看」。

没有这一层，一个把目录写错的守卫会**永远绿** —— 本仓吃过同型的亏：数固定层数的
skills 定位在搬迁后静默归零（见 `contracts/paths.py:skills_root`）。所以拿合成包树
把两个方向都验一遍：真违规要被抓到，而注释/docstring 里的模块名不算依赖。

搬自 `tests/test_ghost_purity.py` 的两个 meta-test。那个文件的三条禁边断言已由
`test_layers.py` 的 `test_forbidden_edges_absent` 覆盖（同一套边，两份实现会漂移），
故只把这一层自检搬过来。
"""

from __future__ import annotations

import _archlib as archlib
from _archlib import canon, module_name, resolve_relative
from test_layers import FORBIDDEN


def _violations(root, files_imports):
    """与 `test_layers.test_forbidden_edges_absent` 同一套判据。"""
    out = []
    for mod, target in files_imports:
        for src, dst in FORBIDDEN:
            if (mod == src or mod.startswith(src + ".")) and \
               (target == dst or target.startswith(dst + ".")):
                out.append(f"{mod} -> {target}")
    return out


def _scaffold(tmp_path, monkeypatch, files: dict[str, str]):
    """在临时目录搭一棵假 `redpilot/` 包树，并把 `_archlib.ROOT` 指过去。

    这样既验了真实现，又不用往源码树里写文件。
    """
    root = tmp_path / "redpilot"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(archlib, "ROOT", root)
    return root


# ── 纯函数：解析错一个，整套禁边就静默失效 ────────────────────


def test_canon_strips_the_repo_prefix():
    assert canon("redpilot.control.store") == "control.store"
    assert canon("redpilot") == "redpilot"
    assert canon("os.path") == "os.path"


def test_module_name_distinguishes_package_inits():
    root = archlib.ROOT
    assert module_name(root / "control" / "store.py") == ("control.store", False)
    assert module_name(root / "control" / "__init__.py") == ("control", True)


def test_resolve_relative_in_plain_module():
    """普通模块里 `level=1` 是同级，不是上一级。"""
    import ast
    node = ast.parse("from . import sibling").body[0]
    assert resolve_relative(["worker", "adapter", "mod"], False, node) == "worker.adapter"
    node2 = ast.parse("from .. import sibling").body[0]
    assert resolve_relative(["worker", "adapter", "mod"], False, node2) == "worker"


def test_resolve_relative_in_package_init():
    """包 `__init__` 里 `level=1` 指的是包自己所在的那一级 —— 与普通模块语义不同。

    这一条曾经是真事故点：两者混为一谈会让 `from . import x` 在 `__init__` 里
    被解析到父包，禁区判定随之整体偏移。
    """
    import ast
    node = ast.parse("from . import sub").body[0]
    assert resolve_relative(["worker", "adapter"], True, node) == "worker.adapter"


# ── 端到端：合成违规必须被抓到 ──────────────────────────────


def test_guard_sees_synthetic_violations(tmp_path, monkeypatch):
    """`worker -> control` 的合成违规必须被抓到。"""
    _scaffold(tmp_path, monkeypatch, {
        "worker/__init__.py": "",
        "worker/bad.py": "from redpilot.control import store\n",
        "control/__init__.py": "",
        "control/store.py": "",
    })
    seen = [imp for path in archlib.iter_files() for imp in archlib.imports(path)]
    assert _violations(None, seen), f"守卫没看见合成违规；实际看到 {seen}"


def test_guard_sees_submodule_form(tmp_path, monkeypatch):
    """`from X import <子模块>` 也要算 —— 只看 `node.module` 的写法会漏掉它。"""
    _scaffold(tmp_path, monkeypatch, {
        "worker/__init__.py": "",
        "worker/bad.py": "from redpilot import control\n",
        "control/__init__.py": "",
    })
    seen = [imp for path in archlib.iter_files() for imp in archlib.imports(path)]
    assert any(t == "control" for _, t in seen), (
        f"`from redpilot import control` 没被解析成 control：{seen}")


def test_guard_reads_syntax_not_text(tmp_path, monkeypatch):
    """注释与 docstring 里的模块名是**说明**，不是依赖 —— AST 判的是语法。"""
    _scaffold(tmp_path, monkeypatch, {
        "worker/__init__.py": "",
        "worker/doc.py": (
            '"""本模块绝不 import redpilot.control（见边界说明）。"""\n'
            "# 也绝不 from redpilot.control import anything\n"
            "from redpilot.contracts import paths\n"
        ),
        "control/__init__.py": "",
        "contracts/__init__.py": "",
        "contracts/paths.py": "",
    })
    seen = [imp for path in archlib.iter_files() for imp in archlib.imports(path)]
    assert not _violations(None, seen), f"把文本当成了依赖：{seen}"
