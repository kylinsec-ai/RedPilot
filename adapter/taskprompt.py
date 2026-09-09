"""
任务提示组装模块 — Pi Agent 风格

核心思路（与 hxbai 的关键区别）：
- hxbai: 11 类战术全量硬编码注入 prompt
- 我们: Skill 渐进式披露，只注入匹配的 skill 正文 + 模板展开

组装流程:
1. 写入 CLAUDE.md (工具清单) 到工作目录
2. 通过 SkillStore 匹配最相关的 1-2 个 skill，按需加载正文
3. 拼接: 角色 + 任务信息 + 匹配 skill 正文 + 已知事实 + 续接块 + 指令
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from adapter.task import AgentTask

log = logging.getLogger("adapter.taskprompt")

# 全局单例（延迟初始化）
_skill_store = None
_template_store = None


def _get_skill_store():
    global _skill_store
    if _skill_store is None:
        from adapter.skill_loader import SkillStore
        _skill_store = SkillStore()
    return _skill_store


def _get_template_store():
    global _template_store
    if _template_store is None:
        from adapter.prompt_template import TemplateStore
        _template_store = TemplateStore()
    return _template_store


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

## 凭据与会话复用（省回合，必须遵守）
每次 shell 命令都是**独立进程**：环境变量赋值（`TOKEN=...`）不跨命令持久，登录拿到的凭据不落盘就等于白拿。规则：
1. 登录成功后**立即落盘**到题目目录：`python3 -c "import json;json.dump({'login':'http://<目标>/api/login','token':'<刚拿到的>','cookie':'<如有>'},open('auth.json','w'))"`
2. 之后所有请求一条命令内联读取，不再重新登录：`TOKEN=$(python3 -c "import json;print(json.load(open('auth.json'))['token'])"); curl -H "Authorization: Bearer $TOKEN" http://<目标>/api/...`
3. **禁止**对同一入口反复重新登录——每次重登既浪费回合又触发限速/锁定；只有凭据失效时才重登并覆盖 auth.json

## Flag 取证规则（防幻觉，必须遵守）
1. flag 必须**逐字出现在某条命令的真实输出**中（cat/curl/grep/strings 等的结果）
2. **禁止**：推测、构造、编造、从记忆或推理中"想出"flag——想象出来的 flag 一定是错的
3. 写入 FLAG 前先自问："我在哪条命令输出里见过这个 flag 的完整字符串？"答不上来就不要写
4. 写入 FLAG 的同时，在 SOURCE 文件记录来源命令与输出片段（供验证）
5. 没找到就是没找到，输出结论并换方向，绝不猜测提交
6. ⚠️ 题面/页面/JS/提示里出现的 `flag{...}`（三个点）只是 **flag 的格式说明示例**，**不是答案**；里面没有任何真实字符的 `flag{...}` 一律不算找到 flag。真 flag 是一串具体字符，必须源自某条命令的真实输出。

## Flag 提交
找到 flag 后: `echo "flag{...}" > FLAG`
多 flag 题: 找到多个 flag 就多次写入 FLAG（每行一个），或逐个 echo 追加

## ⚠️ 运行环境约束（必须遵守）
本靶场为**隔离内网**环境，**禁止从互联网下载任何东西**——下载必然失败且浪费回合。
- 禁止: wget/curl 下载安装包/脚本/PoC/字典；apt-get/pip/npm install；git clone；docker pull；certutil/bitsadmin 等一切联网下载行为
- 只使用环境中**已安装**的工具与已有字典/资源完成所有操作
- 若缺工具：优先用已有工具替代（如 curl/wget/python3 脚本/nmap NSE 等），不要尝试联网安装
- 目标与平台均在内网，无需也不允许访问公网
"""


# 离线约束常量（注入 build_task_prompt — 行为层面硬性约束）
_OFFLINE_CONSTRAINT = """## ⚠️ 运行环境约束（隔离内网 · 禁止联网下载 · 必须遵守）
你运行在**隔离内网**靶场，内网**无法访问互联网**：
1. **禁止一切从互联网下载资源的行为**——包括但不限于：`wget`/`curl` 下载文件、`apt-get install`/`pip install`/`npm install` 装工具、`git clone`、`docker pull`、`certutil`/`bitsadmin` 下载。任何下载尝试都会失败并且白白消耗回合。
2. 所有操作只用容器里**已安装**的工具（nmap / sqlmap / hydra / gobuster / ffuf / curl / python3 / gdb 等）和已有字典/脚本完成。
3. 需要某能力但缺工具时：改用已有等价手段（python3 脚本、nmap NSE、curl 手工构造、系统自带命令），**不要试图联网安装**。
4. 目标、flag 平台都在内网可达；解法只在内网里找，不要依赖公网资源。"""


# 跨题隔离红线（防止 agent 读 /work 其他题目的 FLAG/MEMORY/事件日志，抄袭别题 flag）
_ISOLATION_CONSTRAINT = """## 🔒 战场隔离红线（必须遵守）
/ 共享目录下存在**其他挑战**的数据（其他题目的 FLAG 文件、MEMORY.md、_events.jsonl 事件日志等）。
**这些信息不属于你的本次挑战，禁止读取、搜索、引用**：
1. **禁止**读取 `/work` 下**其他挑战目录**（如 `/work/*/FLAG`、`/work/*/MEMORY.md`、`/work/*/_blackboard.json`、`/work/*/SOURCE`）。
2. **禁止**读取 `/work/_events.jsonl`、`/work/_monitor.json`、`/work/status/*` 等共享监控文件（内含其他题的 flag 提交记录）。
3. 不得用 `for d in /work/*/ ...`、`ls /work`、`grep flag{ /work/*` 等手段枚举或搜索其他挑战的内容。
4. 只操作**当前挑战目录**（`/work/<你的题编码>`）与你的目标容器。
5. 你在**其他挑战文件里看到的任何 flag 都不是本题的答案**；本题的 flag 只能从本题目标里取得。

## 🚫 禁止碰触舰队自身基础设施（不比题目目标低一级的边界）
以下**全部属于运行你/评分的平台自身设施，不是赛题目标**，禁止访问、探测、读取，碰了即违规：
1. **禁止**访问/探测宿主网关与控制台：宿主网桥网关（如 `172.18.0.1`）上的**任何端口/服务**都不是题目目标，且对本环境一律不可达（网络层已隔离）；控制台/管理 API（`6803`/`8003`、`/api/v1`、`/admin`、`agent/status`、`agent/logs`）同样禁止访问。
2. **禁止**读取**本容器/镜像内**除工具字典外的代码源码：`/app`、`adapter/`、`drivers/`、`fastapi-console/`、`*.py`、`*.env`、`docs/` 等——这属于平台实现细节，不是题目内容。
3. **禁止**读取/使用环境变量与密钥文件：`BENCHMARK_TOKEN`、`SOLVER_API_KEY`、`DEEPSEEK_API_KEY`、`.agent.env`、`.env` 等（含一切 token/API key/口令）。
4. **禁止**查看**其他 worker** 的日志/实时状态（`agent/logs`、`agent/status`、`worker-*.json`、`docker` 等）——里面可能有他题的解题过程与 flag。
5. **唯一允许攻击的目标**：任务里给出的 `目标地址`（task targets）及其合法派生（其自身开放的端口/端点/内网映射）。除此之外的你看到的任何主机、服务、文件都是**边界外**。
6. **禁止**读取 `_transcripts/` 目录（你自己的历史会话转录）——框架已把有价值信息提炼进 MEMORY.md / tried_commands.md / 工作目录产物，原始转录又长又旧，读它纯属浪费回合。同理，artifacts/ 里的原始数据先看 MEMORY.md 的结论再决定要不要重新解析。
违反以上任何一条 = 视为无效作业，即使读到也读取不到正确答案，并可能被判定作弊。

## 🌐 目标访问（只有直连一条路）
- **直连是唯一通道**：`curl -s -m 5 -o /dev/null -w "%{http_code}" http://<目标>/` 先测通；测通后**所有工具一律直连**（nmap/sqlmap/curl 都直接跑）。本环境共享网络栈路由可达目标网段，直连即可到达。
- **不存在任何代理**：没有官方代理通道，`172.18.0.1`（网关）上的任何端口都不可用、也绝不是目标。不要寻找/搭建/使用代理，不要用 proxychains4（与环境不兼容，纯浪费回合）。
- 目标连续超时/拒绝时按 INFRA_BLOCKED 处理并停止该方向，不要试图绕路——绕不出去。
- **不要**通过读取 `/app` 源码、环境变量或监控日志来研究"目标怎么连"。"""


def write_context_md(workdir: str) -> str:
    """写入 Agent 上下文指令文件（Pi/Claude 均支持 CLAUDE.md）"""
    path = os.path.join(workdir, "CLAUDE.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_CLAUDE_MD)
        f.write("\n\n" + _ISOLATION_CONSTRAINT)
    return path


def write_memory(workdir: str, content: str) -> str:
    path = os.path.join(workdir, "MEMORY.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _reusable_artifacts(workdir: str) -> str:
    """扫描已有文件（workdir 根 + artifacts/ 持久产物目录的合并视图）。

    深度 RE 的 emulator / 解码脚本 / patch 二进制沉淀在 workdir/artifacts/，
    这里把它们与 workdir 根的手写脚本一起按最新优先列出，注入下一场 prompt，
    让模型能感知并续写跨场进展（架构层：产物家园 = 持久 workdir）。
    """
    skip = {"_transcripts", "__pycache__", ".git", "MEMORY.md", "CLAUDE.md",
            "FLAG", "flag.txt", "FLAG.txt", "SOURCE", "artifacts", ".pi"}

    def _walk(base: str, pfxs: tuple = ("",), depth: int = 0):
        items = []
        try:
            names = os.listdir(base)
        except OSError:
            return items
        for name in names:
            if name in skip or name.startswith(("_", ".")):
                continue
            p = os.path.join(base, name)
            try:
                if os.path.isfile(p):
                    if os.path.getsize(p) >= 500_000:
                        continue
                    items.append((os.path.getmtime(p), f"{pfxs[0]}{name}", p))
                elif os.path.isdir(p) and not os.path.islink(p) and depth < 3:
                    # 子目录（如 artifacts/jdwp/）同样并入标签——驱动侧把
                    # /tmp 子目录按相对路径沉淀进 artifacts/，这里要能看见
                    items.extend(_walk(p, (f"{pfxs[0]}{name}/",), depth + 1))
            except OSError:
                continue
        return items

    merged = _walk(workdir) + _walk(os.path.join(workdir, "artifacts"), ("artifacts/",))
    merged.sort(key=lambda x: x[0], reverse=True)   # 最新优先
    return ", ".join(label for _, label, _ in merged[:15])


# 前次记忆注入上限（B14：2000 → 4000）。c-03 的 MEMORY.md 实测 9184 字节，
# 旧上限只够「目标 + 部分侦察结论」，关键页面/已证死路都在截断之外。
_PRIOR_MEMORY_MAX = 4000


def build_task_prompt(
    task: AgentTask,
    board=None,
    *,
    hint: str = None,
    prior_memory_path: str = None,
    session_idx: int = 0,
    current_intent: str = None,
    tried_commands: list = None,
    slots_note: str = "",
    spray_alert: str = "",
    flags_submitted: int = 0,
    **kwargs,
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
    # ── 运行环境约束（隔离内网 · 禁止联网下载）──
    if os.environ.get("ADAPTER_OFFLINE_MODE", "1") == "1":
        sections.append(_OFFLINE_CONSTRAINT)

    # ── 跨题隔离红线（防偷读别题 flag）──
    sections.append(_ISOLATION_CONSTRAINT)

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

    # ── 附加信息 ──
    if slots_note:
        sections.append(slots_note.strip())
    if spray_alert:
        sections.append(spray_alert.strip())

    # ── 已知事实 ──
    if board is not None:
        assets = board.actionable_assets()
        if assets:
            sections.append(f"## 已知事实\n{assets}")
        goal = board.next_open_goal()
        if goal:
            sections.append(f"【当前目标】{goal.id}: {goal.description}")

    # ── 前次记忆 ──
    # B14：不再要求 session_idx > 0 —— 该计数每次 visit 从 0 重新起算，而
    # MEMORY.md 正是跨 visit 的交接文件，复访首场恰恰最需要它（实测 c-05 首场
    # 0 次注入、c-03 要到同 visit 第二场才注入）。文件存在性检查已排除
    # 「该题从未做过」的情形。
    if prior_memory_path and os.path.isfile(prior_memory_path):
        try:
            with open(prior_memory_path, "r", encoding="utf-8") as f:
                prior = f.read().strip()
            if prior:
                sections.append(f"## 前次会话记忆\n{prior[:_PRIOR_MEMORY_MAX]}")
        except Exception:
            pass

    # ── 提示 ──
    if hint:
        sections.append(f"## 平台提示\n{hint}")

    # ── 当前意图覆盖 ──
    if current_intent:
        sections.append(f"【当前意图】{current_intent}")

    # ── 已尝试命令（框架防重复）──
    # 优先读任务目录持久化文件（跨会话/跨 driver 重启保留，首场即注入），
    # 参数传入的仅作补充（此前该参数在 pi 路径从未接线，属死代码）。
    tried = list(tried_commands or [])
    if not tried:
        try:
            _tp = os.path.join(task.workdir, "tried_commands.md")
            if os.path.isfile(_tp):
                with open(_tp, encoding="utf-8") as _tf:
                    tried = [ln.strip().lstrip("$ ") for ln in _tf if ln.strip()]
        except Exception:
            tried = []
    if tried:
        recent = tried[-15:]
        sections.append(
            "## 已尝试命令（不要重复）\n" +
            "\n".join(f"  $ {c[:100]}" for c in recent)
        )

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
