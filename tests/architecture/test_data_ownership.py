"""R8：数据所有权 —— 每块状态只有一个写模块（执行点）。

- worker 不碰 SQLite（它只拥有 /work 下的文件与 HTTP relay）；
- control / obs 各自只有 store 层写自己的 SQLite（跨模块读走 API/事件）。
"""

from __future__ import annotations

from _archlib import ROOT, iter_files, imports


def _sqlite_files(subdir: str) -> set[str]:
    hit = set()
    for path in (ROOT / subdir).rglob("*.py"):
        for _, target in imports(path):
            if target.split(".")[0] == "sqlite3":
                hit.add(str(path.relative_to(ROOT / subdir)).replace("\\", "/"))
    return hit


def test_worker_never_imports_sqlite3():
    hit = _sqlite_files("worker")
    assert not hit, f"worker must not own a database; sqlite3 found in: {sorted(hit)}"


def test_control_sqlite_is_owned_by_store():
    assert _sqlite_files("control") == {"store.py"}, (
        "control 的 SQLite 只允许 store.py 打开 —— 新增第二个 writer 会破坏单写者模型")


def test_obs_sqlite_is_owned_by_db_and_store():
    assert _sqlite_files("obs") == {"db.py", "store.py"}, (
        "obs 的 SQLite 只允许 db.py(连接/迁移)与 store.py(全部 SQL)打开")


def test_no_cross_module_state_imports():
    """数据面禁边：只有装配根 app.py 可以 import 平台 store；其他模块不行。"""
    violations = []
    for path in iter_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if rel == "app.py":
            continue  # 组合根：负责构造平台 store 并注入路由
        top = rel.split("/")[0]
        for _, target in imports(path):
            if target in ("control.store", "obs.store", "control.db", "obs.db"):
                owner = target.split(".")[0]
                if top != owner:
                    violations.append(f"{rel}: {top} imports {target}")
    assert not violations, "cross-module data access:\n" + "\n".join(violations)
