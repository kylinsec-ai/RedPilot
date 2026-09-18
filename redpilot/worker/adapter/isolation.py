"""求解身份隔离与目录权限（M1/M2 的执行点）。

设计全文：`docs/solver-isolation-design.md` §4（身份模型）、§5（控制面状态搬家）。

本模块只提供**机制**，不解析环境变量 —— R7 要求 env 解析集中在
`adapter/config.py`（见 `IsolationConfig.from_env`），调用方把配置传进来。

核心不变量（S1/S2）：
  · 求解进程（Pi 及其子孙）以非 root 身份运行，写不了 `/app` 与 `.harness`；
  · 求解进程只对自己题目的目录可读写，其他题目目录不可读；
  · driver / relay / dashboard 保持 root（看门狗与 `/proc` 回收依赖它）。

任何权限操作失败都不抛异常到解题主路径：返回结果 + 日志，由调用方决定是
降级告警（缺省）还是 fail closed（`ADAPTER_ISOLATION_STRICT=1`）。
"""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import subprocess

log = logging.getLogger("adapter.isolation")

# 启动自检报告由 driver 生成、由编排层在 obs.configure 之后上报（driver 阶段
# 观测通道还没配好，直接 emit 会丢）。进程内单例，无并发写入者。
_LAST_REPORT: dict = {}


def set_last_report(report: dict) -> None:
    """保存启动自检结果（driver 启动阶段调用）。"""
    global _LAST_REPORT
    _LAST_REPORT = dict(report or {})


def last_report() -> dict:
    """取回启动自检结果（编排层在 obs.configure 后调用并 emit）。"""
    return dict(_LAST_REPORT)

# 权限收紧后，控制面目录一律 0700（root 独占）。求解者甚至不该"能看见"。
_CONTROL_DIRS = (
    "stoploss",
    "surface",
)
# 舰队锁目录与观测目录由其它模块创建，这里只保证存在与权限收敛。
_EXTRA_CONTROL_DIRS = (
    ".stoploss-locks",
    ".live",
    "status",
)


# ── 身份解析 ────────────────────────────────────────────────

def resolve_identity(cfg) -> dict:
    """把 IsolationConfig 解析成 ``subprocess.Popen`` 的身份参数。

    返回 ``{}`` 表示"不降权"（关闭、用户不存在、或当前进程不是 root ——
    非 root 无法 setuid 别人）。调用方必须以返回值是否为空作为唯一判据。
    """
    if not getattr(cfg, "enabled", False):
        return {}
    user = str(getattr(cfg, "user", "") or "").strip()
    if not user:
        return {}
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        log.warning("isolation: user %r does not exist — running as current uid", user)
        return {}
    if os.geteuid() != 0:
        log.warning("isolation: driver is not root (euid=%d) — cannot drop to %s",
                    os.geteuid(), user)
        return {}
    if pw.pw_uid == 0:
        log.warning("isolation: user %r has uid 0 — refusing to call that isolation", user)
        return {}
    return {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": []}


def identity_uid_gid(cfg) -> tuple[int, int] | None:
    """返回 (uid, gid)；不可降权时返回 None。供 ACL/chown 使用。"""
    ident = resolve_identity(cfg)
    if not ident:
        return None
    return int(ident["user"]), int(ident["group"])


# ── 目录权限 ────────────────────────────────────────────────

def _chmod_quiet(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log.warning("isolation: chmod %s -> %o failed: %s", path, mode, exc)


def prepare_control_dirs(workdir: str) -> None:
    """确保控制面目录存在且只有 root 可读写（M2）。

    幂等；任何时候调用都安全。权限收敛失败不抛异常 —— 由启动自检上报。
    """
    base = os.path.join(workdir, ".harness")
    dirs = [os.path.join(base, name) for name in _CONTROL_DIRS]
    dirs += [os.path.join(workdir, name) for name in _EXTRA_CONTROL_DIRS]
    for d in dirs:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            log.warning("isolation: cannot create control dir %s: %s", d, exc)
            continue
        _chmod_quiet(d, 0o700)
    _chmod_quiet(base, 0o700)


def chown_tree(path: str, uid: int, gid: int, *, limit: int = 20000) -> int:
    """把 path 下（含自身）非目标属主的条目 chown 给 (uid, gid)。

    在 bind mount 上题目产物可能到十万级，limit 是防失控上限：到顶后打
    warning 并继续返回（未迁移部分下一次会话再迁，不会阻塞解题）。
    符号链接一律 lchown 且不追随（题目产物里 symlink 很常见）。
    """
    done = 0
    if not uid:
        return 0
    for root, dirs, files in os.walk(path, followlinks=False):
        try:
            st = os.lstat(root)
            if st.st_uid != uid or st.st_gid != gid:
                os.chown(root, uid, gid)
                done += 1
        except OSError:
            pass
        for name in dirs + files:
            if done >= limit:
                log.warning("isolation: chown_tree truncated at %d entries under %s",
                            limit, path)
                return done
            fp = os.path.join(root, name)
            try:
                st = os.lstat(fp)
                if st.st_uid != uid or st.st_gid != gid:
                    os.chown(fp, uid, gid)
                    done += 1
            except OSError:
                continue
    return done


def _setfacl(path: str, uid: int) -> bool:
    """给题目目录打访问 + 默认 ACL（driver 新建的文件对 solver 仍可写）。

    ACL 优于 chown 的理由：题目目录里 driver 与 Pi 都是写者，默认 ACL 一次
    生效、覆盖未来文件；逐个写点 chown 漏一个就是"下次会话改不动笔记"的隐性
    故障（见设计 §4.1）。工具不存在或文件系统不支持时返回 False。
    """
    exe = shutil.which("setfacl")
    if not exe:
        return False
    try:
        r = subprocess.run(
            [exe, "-R", "-m", f"u:{uid}:rwx", "-m", f"d:u:{uid}:rwx", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("isolation: setfacl failed on %s: %s", path, exc)
        return False
    if r.returncode != 0:
        log.warning("isolation: setfacl rc=%d on %s: %s", r.returncode, path,
                    (r.stderr or b"").decode("utf-8", "replace")[:200])
        return False
    return True


def prepare_challenge_dir(workdir: str, code: str, cfg, safe_code_fn) -> str:
    """把一道题的目录交给求解身份（会话前调用，幂等）。

    返回 "acl" | "chown" | "none" | "disabled"，仅供观测/日志使用。
    """
    if not getattr(cfg, "enabled", False):
        return "disabled"
    pairs = identity_uid_gid(cfg)
    if pairs is None:
        return "none"
    uid, gid = pairs
    path = os.path.join(workdir, safe_code_fn(code))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        log.warning("isolation: cannot create challenge dir %s: %s", path, exc)
        return "none"

    if _setfacl(path, uid):
        return "acl"
    # 备选路径（设计 §4.5）：整树 chown。默认 ACL 缺失时，driver 之后新建的
    # MEMORY/黑板文件对 solver 只读 —— 由运行期哨兵探针发现并告警。
    n = chown_tree(path, uid, gid)
    return "chown" if n else "none"


def prepare_pi_home(pi_home: str, cfg) -> None:
    """逐题 HOME（`.pi-home`）必须归求解身份所有，否则 pi 起不来。"""
    try:
        os.makedirs(pi_home, exist_ok=True)
    except OSError as exc:
        log.warning("isolation: cannot create pi home %s: %s", pi_home, exc)
        return
    pairs = identity_uid_gid(cfg)
    if pairs is None:
        return
    uid, gid = pairs
    chown_tree(pi_home, uid, gid, limit=2000)


# ── 启动自检（S4/S5：隔离失败必须响亮且可观测） ─────────────

def capability_note(cfg) -> str:
    """降权后告知解题 Agent 的身份与能力边界（tool-design：错误必须可行动）。

    只在隔离启用时注入；隔离关闭（ADAPTER_ISOLATION=0）时返回空串，
    避免告诉模型一个不存在的限制。
    """
    if not getattr(cfg, "enabled", False):
        return ""
    user = str(getattr(cfg, "user", "") or "solver")
    return (
        "## 🔐 运行身份与能力边界（降权运行）\n"
        f"你（Pi 与它派生的所有命令）以非 root 用户 `{user}` 运行：\n"
        "- **可写**：当前题目目录与 `$HOME`（题目目录下的 `.pi-home`）；\n"
        "- **只读**：`/app`（平台代码）与其他题目目录；\n"
        "- **不可用**：`apt-get`/`dpkg`、`sudo`（无 root）；需要 Python 包时用题目目录里的 venv；\n"
        "- **扫描能力**：若 `nmap -sS`/`-O` 报权限错误，改用 `-sT`（TCP connect），别反复重试。\n"
        "遇到 `Permission denied` 先看是否踩到这里的边界，不要当成目标问题排查。"
    )


def probe(workdir: str, cfg) -> dict:
    """以求解身份跑一次哨兵探针，返回结构化结果（进 obs）。

    检查四件事：身份可切换、题目级临时目录可写、`/app` 与控制面不可写。
    不做任何修改性副作用：成功写入的探针文件立即清理。
    """
    ident = resolve_identity(cfg)
    report = {
        "enabled": bool(getattr(cfg, "enabled", False)),
        "user": str(getattr(cfg, "user", "") or ""),
        "identity": bool(ident),
        "writable_tmp": None,
        "app_readonly": None,
        "control_readonly": None,
        "detail": "",
    }
    if not ident:
        report["detail"] = "identity unavailable (disabled / no user / not root)"
        return report

    probe_dir = os.path.join(workdir, ".harness", ".probe")
    try:
        os.makedirs(probe_dir, exist_ok=True)
        os.chown(probe_dir, ident["user"], ident["group"])
    except (OSError, KeyError) as exc:
        report["detail"] = f"cannot prepare probe dir: {exc}"
        return report

    ok_file = os.path.join(probe_dir, "ok")
    # /app 只存于镜像；本机开发树里没有该路径时跳过这一项（返回 None），
    # 绝不往仓库目录里写探针文件。
    app_dir = "/app"
    if os.path.isdir(app_dir):
        app_target = os.path.join(app_dir, ".isolation_probe_write")
        app_check = (f"touch {app_target!r} 2>/dev/null && echo APP_W_OK || echo APP_DENIED; "
                     f"rm -f {app_target!r} 2>/dev/null; ")
    else:
        app_check = ""
    control_target = os.path.join(workdir, ".harness", ".probe_write")
    script = (
        f"touch {ok_file!r} 2>/dev/null && echo W_OK; "
        + app_check +
        f"touch {control_target!r} 2>/dev/null && echo CTRL_W_OK || echo CTRL_DENIED; "
        f"rm -f {ok_file!r} {control_target!r} 2>/dev/null; true"
    )
    try:
        r = subprocess.run(
            ["/bin/sh", "-c", script], user=ident["user"], group=ident["group"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
        )
        out = (r.stdout or b"").decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        report["detail"] = f"probe exec failed: {exc}"
        return report

    report["writable_tmp"] = "W_OK" in out
    if app_check:
        report["app_readonly"] = "APP_W_OK" not in out
    report["control_readonly"] = "CTRL_W_OK" not in out
    report["detail"] = out.strip().replace("\n", ";")[:300]
    checks = [report["writable_tmp"], report["control_readonly"]]
    if report["app_readonly"] is not None:
        checks.append(report["app_readonly"])
    if not all(checks):
        log.error("isolation probe FAILED: %s", report)
    return report
