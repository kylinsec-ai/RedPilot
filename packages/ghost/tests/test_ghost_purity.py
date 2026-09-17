"""ghost 包纯度守卫:两条被文档承诺、此前无人强制的架构规则。

规则一  `packages/ghost/ghost/obs/__init__.py:12`  —— 本包绝不 import ghost_worker
规则二  `packages/ghost/ghost/obs/localserver.py:6-10` —— worker → obs.localserver 是
        唯一允许的 worker→obs 代码 import;worker 绝不 import obs.store/db/schema/ingest

为什么值得一条测试:这两句都是**结构性**承诺,违反它们不会报错,只会变味 —— worker
镜像会因为一句 import 被迫装上平台侧的 fastapi/pydantic(基线依赖是既定决策,见
`packages/ghost/pyproject.toml`),观测面会悄悄依赖 worker 的运行态(于是"观测平台"
再也不能独立部署)。注释拦不住 import。

为什么走 AST 而不是 grep:注释与 docstring 里写着 `ghost_worker` 是**说明**,不是依赖 ——
在 `obs/__init__.py` / `eval/__init__.py` / `replay.py` 里都有这类字样。用文本匹配会
把解释当违规,逼人删掉解释,那是最坏的结局(约束还在,理由没了)。所以下面只解析语法树。

姊妹文件:`packages/contracts/tests/test_purity.py`(contracts 零第三方依赖的同款守卫)。
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_GHOST_PKG = _REPO / "packages" / "ghost" / "ghost"
_WORKER_PKG = _REPO / "packages" / "worker" / "ghost_worker"
_OBS_PKG = _GHOST_PKG / "obs"

# worker 侧唯一放行的 ghost 模块。`ghost` 与 `ghost.obs` 只是两级包的 __init__(纯
# docstring 空壳):`from ghost.obs import localserver` 必然把它们加载进来,拦下来只会
# 逼出更难读的写法,而边界不在那儿 —— 真正放行的模块只有一个。
_WORKER_ALLOWED = "ghost.obs.localserver"
_WORKER_ALLOWED_PACKAGES = ("ghost", "ghost.obs")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO))
    except ValueError:
        return str(path)          # 自检用的临时文件在仓库外


def _import_paths(path: Path) -> list[set[str]]:
    """每个 import 语句 → 它**可能真正加载**的模块全名集合。

    `from X import a` 的加载目标有两种可能:X 本身,或子模块 X.a(取决于 a 是模块
    还是 X 的属性)。两种都收进来一起判:只看 `node.module` 会放走
    `from ghost.obs import store` —— 那一句确实会把 obs.store 拉进来。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[set[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.append({a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue          # 相对 import 走不出所在包
            out.append({node.module} | {f"{node.module}.{a.name}" for a in node.names})
    return out


def _ghost_side_violations(paths: list[Path]) -> list[str]:
    """ghost/** 里 import 到 ghost_worker(或其子模块)的地方。"""
    found: list[str] = []
    for py in paths:
        bad: set[str] = set()
        for mods in _import_paths(py):
            bad |= {m for m in mods if m.split(".")[0] == "ghost_worker"}
        if bad:
            found.append(f"{_rel(py)}: {', '.join(sorted(bad))}")
    return found


def _worker_side_violations(paths: list[Path]) -> list[str]:
    """worker/** 里除 `ghost.obs.localserver` 之外的 ghost 包 import。"""
    found: list[str] = []
    for py in paths:
        bad: set[str] = set()
        for mods in _import_paths(py):
            for m in mods:
                if not (m == "ghost" or m.startswith("ghost.")):
                    continue      # 不是 ghost 包的事(worker 另有 SDK/pi 依赖)
                if m in _WORKER_ALLOWED_PACKAGES or m == _WORKER_ALLOWED \
                        or m.startswith(_WORKER_ALLOWED + "."):
                    continue
                bad.add(m)
        if bad:
            found.append(f"{_rel(py)}: {', '.join(sorted(bad))}")
    return found


# ── 两条规则(收集全部违规再一次断言:逐个 fail-fast 会让下一次破坏更难看见)──


def test_ghost_does_not_import_worker():
    """ghost/** 绝不 import ghost_worker。"""
    assert _GHOST_PKG.is_dir(), f"ghost 源码目录不存在:{_GHOST_PKG}"
    violations = _ghost_side_violations(sorted(_GHOST_PKG.rglob("*.py")))
    assert not violations, (
        "ghost 包不得 import ghost_worker(观测/评估面不得依赖 worker 运行态):\n  "
        + "\n  ".join(violations))


def _obs_side_violations(paths: list[Path]) -> list[str]:
    """obs/** 里 import 到 ghost.control(或其子模块)的地方。"""
    found: list[str] = []
    for py in paths:
        bad: set[str] = set()
        for mods in _import_paths(py):
            bad |= {m for m in mods
                    if m == "ghost.control" or m.startswith("ghost.control.")}
        if bad:
            found.append(f"{_rel(py)}: {', '.join(sorted(bad))}")
    return found


def test_obs_does_not_import_control():
    """obs/** 不 import ghost.control（`ghost/__init__.py:9` 的第三条边界）。

    为什么这条值得单独测：obs 是 worker 镜像唯一装得起的那半边（零 fastapi 基线），
    而 control 拖着 dotenv/httpx 与调度/VPN 的整套运行态。两者一旦合流，worker
    镜像会被迫装上它根本不需要的控制面 —— 而这条边界和另外两条一样，只写在
    docstring 里、没有任何东西拦着。违反它不会报错，只会让镜像变胖、让观测面
    再也无法独立部署。
    """
    assert _OBS_PKG.is_dir(), f"obs 源码目录不存在：{_OBS_PKG}"
    violations = _obs_side_violations(sorted(_OBS_PKG.rglob("*.py")))
    assert not violations, (
        "obs 不得 import ghost.control（零 fastapi 基线 / 观测面须能独立部署）：\n  "
        + "\n  ".join(violations))


def test_worker_imports_only_obs_localserver():
    """worker 侧只允许 `ghost.obs.localserver` 一个 ghost import,其余一律违规。"""
    assert _WORKER_PKG.is_dir(), f"worker 源码目录不存在:{_WORKER_PKG}"
    violations = _worker_side_violations(sorted(_WORKER_PKG.rglob("*.py")))
    assert not violations, (
        f"worker 只允许 import {_WORKER_ALLOWED}(零 fastapi 基线 / 无数据依赖),"
        "以下 import 越界:\n  " + "\n  ".join(violations))


# ── 守卫自检:确保"绿"是"没问题",而不是"没在看"──


def test_guard_sees_synthetic_violations(tmp_path):
    """合成违规必须被抓到。

    没有这一条,一个把目录写错的守卫会**永远绿** —— 本仓吃过同型的亏(数固定层数的
    skills 定位在搬迁后静默归零,见 `ghost_contracts/paths.py:skills_root`)。
    守卫的绿必须能区分"没问题"与"没在看",所以拿合成样本把两个方向都验一遍。
    """
    bad_ghost = tmp_path / "bad_ghost.py"
    bad_ghost.write_text("from ghost_worker import taskprompt\n", encoding="utf-8")
    assert _ghost_side_violations([bad_ghost]), "ghost 侧守卫没看见合成违规"

    bad_worker = tmp_path / "bad_worker.py"
    bad_worker.write_text("from ghost.obs import store\n", encoding="utf-8")
    assert _worker_side_violations([bad_worker]), (
        "worker 侧守卫没看见 `from ghost.obs import store`(只看 node.module 的写法会漏掉)"
    )

    # 第三条:obs 侧的守卫(走真函数,不是把判据抄一遍 —— 抄一遍就测不到实现)
    bad_obs = tmp_path / "bad_obs.py"
    bad_obs.write_text("from ghost.control import service\n", encoding="utf-8")
    assert _obs_side_violations([bad_obs]), (
        "obs 侧守卫没看见 `from ghost.control import service`")


def test_guard_reads_syntax_not_text(tmp_path):
    """注释/docstring 里的 `ghost_worker` 不算依赖 —— AST 判的是语法,不是文本。"""
    doc = tmp_path / "doc.py"
    doc.write_text(
        '"""本模块绝不 import ghost_worker(见 obs/__init__.py 的边界说明)。"""\n'
        "# 也绝不 from ghost_worker import anything\n"
        "from ghost.obs.localserver import serve_forever_in_thread\n",
        encoding="utf-8")
    assert _ghost_side_violations([doc]) == []
    assert _worker_side_violations([doc]) == []
