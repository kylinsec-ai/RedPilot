"""模块边界执行点 R1–R3/R6/R7 —— 纯 stdlib AST，不 import 被测代码。

这是「非多包」后边界的唯一执行点：pip 依赖图只证明装了什么，
这里证明谁 import 谁。规则与红线的定义见 docs/modular-monolith-design.md §3.3。
"""

from __future__ import annotations

import ast

from _archlib import ROOT, STDLIB, iter_files, imports

# 禁边（仓库内规范名）：src 前缀 -> dst 前缀
FORBIDDEN = (
    ("worker", "control"), ("worker", "obs"), ("worker", "app"),
    ("control", "obs"), ("obs", "control"),
    ("control", "worker"), ("obs", "worker"), ("app", "worker"),
)

# R6：适配器层不得反向 import 编排层
WORKER_LOOP = ("orchestrator", "driver", "supervisor", "relay")

# R7：env 只允许在配置边界解析。contracts/paths.py 是内核显式例外
# （ADAPTER_SKILLS_DIR 的定位优先级写在那里，内核不能依赖 Settings 类）。
ENV_ALLOWED = {
    "contracts/paths.py",
    "control/config.py",
    "obs/config.py",
    "worker/settings.py",
    "worker/config.py",
    "worker/adapter/config.py",
}
# 存量债：只许缩短。每修好一个文件就把它从本集合删掉（有 stale 测试兜底）。
ENV_DEBT = {
    "worker/orchestrator.py",
    "worker/relay.py",
    "worker/roster.py",
    "worker/supervisor.py",
    "worker/dashboard.py",
    "worker/adapter/hallucination.py",
    "worker/adapter/platform_client.py",
    "worker/adapter/stoploss.py",
    "worker/adapter/taskprompt.py",
    "worker/adapter/verify.py",
    "worker/adapter/platform/generic_openapi.py",
    "worker/adapter/solver/pi_agent.py",
}


def _env_reads(path) -> int:
    """只认配置读取（getenv / environ.get / environ[...]），不认 environ.copy() 透传。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "getenv":
                n += 1
            elif (isinstance(f, ast.Attribute) and f.attr == "get"
                  and isinstance(f.value, ast.Attribute) and f.value.attr == "environ"):
                n += 1
        elif isinstance(node, ast.Subscript):
            v = node.value
            if isinstance(v, ast.Attribute) and v.attr == "environ":
                n += 1
    return n


# ── R4：装配根不承载领域逻辑 ─────────────────────────────

def test_composition_root_holds_no_domain_logic():
    """app.py 只做构造与路由装配：无顶层类、顶层函数只有 create_app。"""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    funcs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert not classes, f"app.py must not define classes: {classes}"
    assert set(funcs) <= {"create_app"}, f"app.py top-level functions: {funcs}"

    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in STDLIB:
                    bad.append(a.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            root = node.module.split(".")[0]
            if root not in STDLIB and root != "fastapi":
                bad.append(node.module)
    assert not bad, f"app.py may only import stdlib + fastapi + own modules: {bad}"


# ── R1：contracts 零第三方依赖 ────────────────────────────────

def test_contracts_is_stdlib_only():
    for path in (ROOT / "contracts").rglob("*.py"):
        for _, target in imports(path):
            root = target.split(".")[0]
            assert root in STDLIB or target == "contracts" or target.startswith("contracts."), \
                f"{path}: non-stdlib import {target!r}"


# ── R2/R3：模块禁边 ──────────────────────────────────────────

def test_forbidden_edges_absent():
    violations = []
    for path in iter_files():
        for mod, target in imports(path):
            for src, dst in FORBIDDEN:
                if (mod == src or mod.startswith(src + ".")) and \
                   (target == dst or target.startswith(dst + ".")):
                    violations.append(f"{path.relative_to(ROOT.parent)}: {src} -> {dst} ({target})")
    assert not violations, "forbidden imports:\n" + "\n".join(violations)


# ── R6：适配器层不得反向依赖编排层 ────────────────────────────

def test_adapters_do_not_import_loop():
    violations = []
    for path in iter_files():
        if "/adapter/" not in str(path).replace("\\", "/"):
            continue
        for _, target in imports(path):
            if target.rsplit(".", 1)[-1] in WORKER_LOOP:
                violations.append(f"{path.relative_to(ROOT.parent)}: adapter imports {target}")
    assert not violations, "adapter -> orchestration imports:\n" + "\n".join(violations)


# ── R7：配置读取收编 ─────────────────────────────────────────

def test_env_reads_are_collected():
    violations = []
    for path in iter_files():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if _env_reads(path) and rel not in ENV_ALLOWED and rel not in ENV_DEBT:
            violations.append(rel)
    assert not violations, (
        "env reads outside config boundary (R7): " + ", ".join(violations) +
        " —— 新代码请把 env 读取放进 settings/config")


def test_env_debt_entries_are_real():
    """存量债只许缩短：文件修好后必须把它从 ENV_DEBT 删掉，否则这里 red。"""
    stale = []
    for rel in sorted(ENV_DEBT):
        path = ROOT / rel
        if not path.is_file() or not _env_reads(path):
            stale.append(rel)
    assert not stale, f"ENV_DEBT entries no longer read env (remove them): {stale}"
