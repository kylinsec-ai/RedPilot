"""
任务提示组装模块 — Pi Agent 风格

核心思路（与 hxbai 的关键区别）：
- hxbai: 11 类战术全量硬编码注入 prompt
- 我们: Skill 渐进式披露，只注入匹配的 skill 正文

组装流程:
1. 写入 CLAUDE.md (工具清单) 到工作目录
2. 通过 SkillStore 匹配最相关的 1-2 个 skill，按需加载正文
3. 拼接: 角色 + 任务信息 + 匹配 skill 正文 + 已知事实 + 续接块 + 指令
"""

from __future__ import annotations

import logging
import os

from adapter.task import AgentTask

log = logging.getLogger("adapter.taskprompt")

# 全局单例（延迟初始化）
_skill_store = None


def _get_skill_store():
    global _skill_store
    if _skill_store is None:
        from adapter.skill_loader import SkillStore
        _skill_store = SkillStore()
    return _skill_store


# ── 内网多阶段渗透编排（多 flag 题）──────────────────

_INTRANET_ORCHESTRATION = """\
## 内网多阶段渗透编排（本题含多个 flag）
本题存在多个 flag，通常分布在内网不同深度。按阶段持续推进，全部找齐前不要停止：

- **阶段 A · 入口突破**: 突破入口服务/应用（Web/API/登录等），获取初始凭据或执行点。入口处的 flag 直接收集。
- **阶段 B · 内网探测与横向移动**: 拿到入口权限后进入内网拓扑——`ip addr`/`ip route` 看本机网段，对网段做服务探测（nmap -Pn 常见网段、常见端口 22/80/445/3306/6379/8080 等）；用已获凭据尝试 SSH/RDP/共享/数据库登录；用 `proxychains4`、`chisel`、`ssh -L/-D` 建立横向隧道向新主机移动。每发现一台主机写入 MEMORY.md 并继续。
- **阶段 C · 提权与深度 flag**: 在横向到达的主机上提权（SUID 二进制、内核/服务漏洞、错误配置、弱口令复用），读取受保护目录/数据库/配置/管理后台中的剩余 flag。

规则:
1. 每个阶段推进前先更新 MEMORY.md，保证跨会话记忆连贯
2. 找到的 flag 立即 `echo 'flag{...}' > FLAG`（同一题可写入多个）
3. 避免在内网重复扫描相同网段；优先利用已获得的凭据与已发现的服务
4. 若内网不可达（无目标网段/路由），明确输出 INFRA_BLOCKED 并停止该方向"""


# ── CLAUDE.md 内容 ──

_CLAUDE_MD = """\
# 工具使用指南

你是一个自主安全测试 Agent，正在对已授权的靶场目标进行安全评估。

## 可用工具
- **Web**: nmap, ffuf, gobuster, nikto, whatweb, sqlmap, nuclei, curl, wget
- **Pwn**: gdb, r2, objdump, strings, pwntools (python3)
- **Crypto**: pycryptodome, sympy, gmpy2, z3-solver
- **Forensics**: tshark, binwalk, foremost, exiftool, steghide
- **Network**: ncat, socat, proxychains4, chisel, hydra
- **Database**: mysql, psql, redis-cli

## 内网横向移动
- 入口权限后先看本机网段: `ip addr` / `ip route`
- 扫描内网: `nmap -Pn -p 22,80,445,3306,6379,8080 <网段>` / `proxychains4 nmap -sT -Pn <内网IP>`
- 建立隧道: `chisel client <入口>:<端口> R:socks` / `ssh -D 1080 user@host`
- 复用已获凭据横向: SSH/MySQL/Redis/SMB 登录尝试

## Flag 提交
找到 flag 后: `echo "flag{...}" > FLAG`
多 flag 题: 找到多个 flag 就多次写入 FLAG（每行一个），或逐个 echo 追加
"""


def write_context_md(workdir: str) -> str:
    """写入 Agent 上下文指令文件（Pi/Claude 均支持 CLAUDE.md）"""
    path = os.path.join(workdir, "CLAUDE.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_CLAUDE_MD)
    return path


def write_memory(workdir: str, content: str) -> str:
    path = os.path.join(workdir, "MEMORY.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _reusable_artifacts(workdir: str) -> str:
    """扫描工作目录中已有的文件"""
    skip = {"_transcripts", "__pycache__", ".git", "MEMORY.md", "CLAUDE.md", "FLAG", "flag.txt"}
    artifacts = []
    try:
        for name in sorted(os.listdir(workdir)):
            if name in skip or name.startswith(("_", ".")):
                continue
            p = os.path.join(workdir, name)
            if os.path.isfile(p) and os.path.getsize(p) < 500_000:
                artifacts.append(name)
            if len(artifacts) >= 15:
                break
    except OSError:
        pass
    return ", ".join(artifacts) if artifacts else ""


def build_task_prompt(
    task: AgentTask,
    board=None,
    *,
    prior_memory_path: str = None,
    session_idx: int = 0,
    flags_submitted: int = 0,
) -> str:
    """
    组装完整 prompt — 渐进式披露版

    关键创新点：不是把所有战术都灌进去，而是：
    1. 只把 skill 名称和描述作为 XML 摘要放在上下文
    2. 把最匹配的 1-2 个 skill 的完整正文加载进来
    3. 其余 skill 的详细内容留在磁盘上，节省 token
    """
    sections = []
    store = _get_skill_store()

    # ── 角色 ──
    sections.append(
        "你是一个自主安全测试 Agent。你已获得明确授权对以下靶场目标进行安全评估。\n"
        "找到所有 flag 并写入 FLAG 文件。"
    )

    # ── 任务信息 ──
    task_lines = [
        "## 任务",
        f"- 目标: {task.objective}",
        f"- 地址: {task.target_str()}",
        f"- Flag 数量: {task.flag_count}",
    ]
    if task.flag_count > 1:
        remaining = max(0, task.flag_count - int(flags_submitted or 0))
        task_lines.append(
            f"- 已提交: {int(flags_submitted or 0)}/{task.flag_count}，剩余 {remaining} 个（多 flag 题：剩余 flag 通常在内网深处，见内网编排）"
        )
    if task.flag_format:
        task_lines.append(f"- Flag 格式: {task.flag_format}")
    if task.difficulty:
        task_lines.append(f"- 难度: {task.difficulty}")
    sections.append("\n".join(task_lines))

    # ── 内网多阶段编排（仅多 flag 题注入，节省 token）──
    if task.flag_count > 1:
        sections.append(_INTRANET_ORCHESTRATION)

    # ── 渐进式 Skill 注入（核心差异点）──
    matched = store.match_skills(
        task.objective,
        targets=task.targets,
        files=task.files,
    )
    if matched:
        skill_section = ["## 攻击参考（按相关度自动匹配）"]
        for m in matched[:2]:  # 最多注入 2 个 skill 正文
            body = store.load_skill(m["name"])
            if body:
                skill_section.append(f"\n### [{m['name']}] (相关度: {m['score']:.1f})\n")
                skill_section.append(body)
        sections.append("\n".join(skill_section))
    else:
        # 兜底：至少告诉 Agent 有哪些 skill 可用
        sections.append(store.skill_summary_xml())

    # ── 已知事实 ──
    if board is not None:
        assets = board.actionable_assets()
        if assets:
            sections.append(f"## 已知事实\n{assets}")

    # ── 前次记忆 ──
    if prior_memory_path and os.path.isfile(prior_memory_path) and session_idx > 0:
        try:
            with open(prior_memory_path, "r", encoding="utf-8") as f:
                prior = f.read().strip()
            if prior:
                sections.append(f"## 前次会话记忆\n{prior[:2000]}")
        except Exception:
            pass

    # ── 工作目录产物 ──
    artifacts = _reusable_artifacts(task.workdir)
    if artifacts:
        sections.append(f"工作目录已有文件: {artifacts}")

    # ── 工作指令 ──
    sections.append(
        "\n## 工作指令\n"
        "1. 读取 CLAUDE.md 了解可用工具\n"
        "2. 如有 MEMORY.md，先读取前次进展\n"
        "3. 侦察 → 漏洞发现 → 利用 → 获取 flag\n"
        "4. 每个重要发现写入 MEMORY.md\n"
        "5. 找到 flag: `echo 'flag{...}' > FLAG`\n"
        "6. 未解出时输出续接块:\n"
        "   已达成原语: <进展>\n"
        "   已证死路: <死路+原因>\n"
        "   下一步: <具体命令>"
    )

    return "\n\n".join(sections)
