"""R2 的运行时验证：worker 的 import 闭包不得拉入平台依赖。

静态 AST 会被动态 import 绕过（函数体内 import、importlib），这里用子进程
做真实 import 足迹检查，补上那一半。红线见 docs/modular-monolith-design.md §3.3。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PLATFORM_DEPS = {"fastapi", "pydantic", "uvicorn", "starlette"}


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=REPO,
                          capture_output=True, text=True)


def test_worker_orchestrator_import_closure_has_no_platform_deps():
    code = (
        "import sys\n"
        "import redpilot.worker.orchestrator\n"
        "deps = sorted({m.split('.')[0] for m in sys.modules} & "
        "{'fastapi', 'pydantic', 'uvicorn', 'starlette'})\n"
        "mods = sorted(m for m in sys.modules "
        "if m.startswith('redpilot.control') or m.startswith('redpilot.obs'))\n"
        "print('|'.join(deps + mods))\n"
    )
    r = _run(code)
    assert r.returncode == 0, f"worker orchestrator import failed:\n{r.stderr}"
    leaked = r.stdout.strip()
    assert leaked == "", (
        f"worker import closure leaked platform code: {leaked} "
        "—— Kali 镜像瘦身与 worker↛platform 红线（R2）被破坏")


def test_worker_package_import_closure_is_clean():
    code = (
        "import sys\n"
        "import redpilot.worker\n"
        "deps = sorted({m.split('.')[0] for m in sys.modules} & "
        "{'fastapi', 'pydantic', 'uvicorn', 'starlette'})\n"
        "print('|'.join(deps))\n"
    )
    r = _run(code)
    assert r.returncode == 0, f"redpilot.worker import failed:\n{r.stderr}"
    assert r.stdout.strip() == "", f"redpilot.worker leaked: {r.stdout.strip()}"
