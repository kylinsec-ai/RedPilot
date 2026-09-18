"""
任务提示组装模块 — Pi Agent 风格

核心思路（与 hxbai 的关键区别）：
- hxbai: 11 类战术全量硬编码注入 prompt
- 我们: 技能不注入 prompt —— 路由交给 pi 原生渐进披露（Agent 按题目 read）

组装流程:
1. 写入 CLAUDE.md (硬规则) 到工作目录
2. 拼装任务信息 + 约束 + 已知事实 + 续接块
3. 技能面只放名录/自主调用指引，正文留在磁盘上由 Agent 自取
"""

from __future__ import annotations

import hashlib
import logging
import os

from .task import AgentTask
from .verify import (flag_evidence_policy,
                            skill_agent_enabled as _skill_agent_on,
                            subagent_enabled as _subagent_on)

log = logging.getLogger("adapter.taskprompt")

# 全局单例（延迟初始化）
_skill_store = None


def _get_skill_store():
    global _skill_store
    if _skill_store is None:
        from .skill_loader import SkillStore
        _skill_store = SkillStore()
    return _skill_store


# [B67] 技能自主调用指引（开关 verify.skill_agent_enabled）。pi 原生技能
# 渐进披露：pi 自己的文档也提醒「模型不总会主动读——用提示推一把」。
# 2026-09-16 起框架不再做正文预选（技能库换成上游 yaklang/hack-skills，
# 带自带三层路由）—— 这段是技能面**唯一**的框架侧输入，措辞必须是"从清单里自己挑"，
# 不能说"预选"。
_SKILL_SELF_SERVE_NOTE = (
    "**技能自主调用（你的分析优先）**：系统的 `<available_skills>` 清单里有全部"
    "技能的名字、一句话描述与 SKILL.md 路径（技能库分三层：总入口 `hack` → 六个分类"
    "入口 → 各深度题面技能）。**路由完全由你的题目分析决定**——先判断攻击面，"
    "拿不准就先 read `hack` 让它给你路由，再逐层下钻到对口技能，读全文后按其中的"
    "命令范式行动。推进中换了攻击面、或发现原方向不对口时，随时换读别的技能。"
)


# ── 内网多阶段渗透编排（多 flag 题）──────────────────

_INTRANET_ORCHESTRATION = """\
## 内网多阶段渗透编排（本题含多个 flag）
本题存在多个 flag，通常分布在内网不同深度。按阶段持续推进，全部找齐前不要停止：

- **阶段 A · 入口突破**: 突破入口服务/应用（Web/API/登录等），获取初始凭据或执行点。入口处的 flag 直接收集。
- **阶段 B · 内网探测与横向移动**: 拿到入口权限后进入内网拓扑——`ip addr`/`ip route` 看本机网段，对网段做服务探测（nmap -Pn 常见网段、常见端口 22/80/445/3306/6379/8080 等）；用已获凭据尝试 SSH/RDP/共享/数据库登录；需要隧道时使用已安装的 `chisel` 或 `ssh -L/-D`，不要使用环境不兼容的 `proxychains4`。每发现一台主机写入 MEMORY.md 并继续。
- **阶段 C · 提权与深度 flag**: 在横向到达的主机上提权（SUID 二进制、内核/服务漏洞、错误配置、弱口令复用），读取受保护目录/数据库/配置/管理后台中的剩余 flag。

规则:
1. 每个阶段推进前先更新 MEMORY.md，保证跨会话记忆连贯
2. 找到的答案立即写入 FLAG：一律使用 `printf '%s\\n' '答案内容' >> FLAG` 逐行追加（`>>` 在文件不存在时也会创建它）。FLAG 是即时投递队列，驱动确认后会在安全的会话边界移走已入账行；这是正常现象，已提交数量才是进度真相。**答案格式以题目描述为准**——题目要求 `flag{...}` 就写 `flag{...}`，要求密码/hash/密钥就写原始值，不要自己加外壳。写入后不要等待、不要重找这一条，立即沿当前内网链继续寻找下一条；禁止用 `>` 覆盖已经找到的答案。
3. 避免在内网重复扫描相同网段；优先利用已获得的凭据与已发现的服务
4. 若内网不可达（无目标网段/路由），明确输出 INFRA_BLOCKED 并停止该方向
5. 若目标端口能连上、但服务对任何请求都返回 5xx（Internal Server Error / Bad Gateway 等后端崩溃），明确输出 TARGET_BROKEN 并停止该方向——框架会重启题目容器后让你重试
6. 只有在连续多个会话都没有新增主机、服务、凭据或其他可验证事实时，才算暂时卡住；框架会安排一次平台提示复核。提示只是方向线索，必须回到目标命令验证；提示后的完整复核窗口仍要继续当前内网链。只有复核后继续没有任何新证据，才报告无进一步答题意义并结束本题。"""


# ── CLAUDE.md 内容 ──

_CLAUDE_MD = """\
# 工具使用指南

你是一个自主安全测试 Agent，正在对已授权的靶场目标进行安全评估。

## 可用工具
- **Web**: nmap, ffuf, gobuster, nikto, whatweb, sqlmap, nuclei, curl, wget
- **Pwn**: gdb, r2, objdump, strings, pwntools (python3)
- **Crypto**: pycryptodome, sympy, gmpy2, z3-solver
- **Forensics**: tshark, binwalk, foremost, exiftool, steghide
- **Network**: nc/netcat, socat, chisel, hydra
- **Database**: mysql, psql, redis-cli

## 技能库（动手前先读）
本环境已挂载技能库（`~/` 下的 `.pi/agent/skills/`），分三层：
总入口 `hack`（全局路由与作业纪律）/ 分类入口 / 各深度题面技能。
**动手前先 read 对口技能的 SKILL.md** —— 拿不准攻击面时先 read `hack`，由它路由；
分类入口再逐层下钻，读全文后按其中的命令范式行动。同目录的 SCENARIOS.md /
*_MATRIX.md 是该技能的配套材料，正文里指到了再读。清单以 `<available_skills>` 为准。

## 内网横向移动
- 入口权限后先看本机网段: `ip addr` / `ip route`
- 扫描内网: `nmap -Pn -p 22,80,445,3306,6379,8080 <网段>`（共享网络栈直连，不使用 proxychains4）
- 建立隧道: `chisel client <入口>:<端口> R:socks` / `ssh -D 1080 user@host`（不要用 proxychains4）
- 复用已获凭据横向: SSH/MySQL/Redis/SMB 登录尝试

## 凭据与会话复用（省回合，必须遵守）
每次 shell 命令都是**独立进程**：环境变量赋值（`TOKEN=...`）不跨命令持久，登录拿到的凭据不落盘就等于白拿。规则：
1. 登录成功后**立即落盘**到题目目录：`python3 -c "import json;json.dump({'login':'http://<目标>/api/login','token':'<刚拿到的>','cookie':'<如有>'},open('auth.json','w'))"`
2. 之后所有请求一条命令内联读取，不再重新登录：`TOKEN=$(python3 -c "import json;print(json.load(open('auth.json'))['token'])"); curl -H "Authorization: Bearer $TOKEN" http://<目标>/api/...`
3. **禁止**对同一入口反复重新登录——每次重登既浪费回合又触发限速/锁定；只有凭据失效时才重登并覆盖 auth.json

## 分析策略：理解机制后，优先让系统自己给出答案

当你通过静态分析弄清了一个系统的工作原理后，**优先让它跑起来，
从真实输出中取得答案**，而不是在纸面上推导最终值。

- 搞清了校验算法？→ 构造合法输入，运行程序，看它输出什么
- 找到了认证流程？→ 拿合法凭据调真实接口，从响应中取结果
- 逆出了编码方案？→ 解密后打印出来，再核对靶标上有没有更直接的获取方式

这不是死规则——有些题确实需要纯数学推导。但多数情况下，
**答案存在于某个运行态的输出里**（二进制 stdout、HTTP 响应、数据库查询），
而不是推导链的终点。如果你分析了多轮却**从没让系统在正确输入下完整跑过**，
大概率在绕远路。

特别注意：程序的实际输出（哪怕是 `.`、乱码、看似无意义的串）
**可能本身就是答案或其编码**——不要因为它"看起来不像 flag"就忽略。

**推导出的候选要果断提交试错，让平台当裁判**：如果你逆向/解码出一个
有根据的候选（来自密文、算法推导、二进制数据），把它打印出来（如
`printf '%s\\n' '候选值'`），平台会告诉你对不对。判错不等于白做——
平台反馈能够收敛方向。**不要因为"这个候选不一定对"就反复自我怀疑、
迟迟不提交**。有依据的推导值得一试；只有纯占位符（`flag{...}`）才不该写。

## 答案取证与反思（写入 FLAG 前必须执行）

在你执行 `printf ... >> FLAG` 写入任何答案之前，**必须先完成以下反思**：

**反思四问**（在思考过程中回答，不需要输出给用户）：
1. **来源确认**：这个答案的完整字符串是我在哪条命令的输出中**亲眼看到**的？
   - ✅ curl/wget 响应、cat 文件内容、grep 匹配结果、**Python/脚本计算并打印的结果**
   - ❌ 凭空想出来的、从未运行过任何命令就写下的
   - 关键：**自己写脚本解码/计算并打印出来的候选 = 合法证据**。脚本的输出就是命令输出。
     逆向题里"算出 flag 再打印"正是正规解法，不要因为"是我算出来的"就不写。
2. **幻觉排除**：这个答案有没有可能是我没有运行任何命令、空想编造的？
   - 如果你**跑过**解码/计算/请求命令、且打印结果里见到了这个串 → 不是幻觉，写入。
   - 只有"从未运行过任何命令、直接凭记忆/格式猜的串"才是幻觉 → 不写。
3. **格式匹配**：题目要求的答案格式是什么？我的答案格式对不对？
   - 仔细看题目描述：要求 `flag{...}` 格式？还是裸 hash、密码、密钥？
   - 题目说"找到密码" → 答案就是密码本身，不要包上 `flag{...}`
   - 题目说"找到 flag" → 答案应该是 `flag{...}` 格式
   - 题目没有明确格式 → 用你在命令输出中**实际看到**的原始格式
   - **格式错误 = 答案错误**，即使内容对也会被判错！
4. **置信度**：你对这个答案是真实且格式正确的信心是多少？
   - ≥99%：直接写入 FLAG
   - 70-99%：写入 FLAG 但附上存疑说明
   - <70%：不写，继续寻找更有力的证据

**硬性规则**：
- **有据推导 ≠ 编造**：运行解码/计算脚本并亲眼看到输出 = 合法证据，必须提交。
- **禁止的是空想**：不运行任何命令、直接凭记忆/题目示例猜的串 —— 这种不写。
- 题面里的 `flag{...}`（三个点）只是格式示例，不是答案
- **不要自己给答案加外壳**——题目输出什么格式就提交什么格式
- 写入 FLAG 时在 SOURCE 记录来源命令与输出片段

## 答案提交
找到答案后: `printf '%s\\n' '答案内容' >> FLAG`
- 如果答案是 `flag{xxx}` 格式 → `printf '%s\\n' 'flag{xxx}' >> FLAG`
- 如果答案是裸 hash/密码 → `printf '%s\\n' 'a1b2c3d4...' >> FLAG`
- 多答案题: 每个答案独占一行追加写入
- `>>` 会在 FLAG 不存在时自动创建，禁止用 `>` 覆盖

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

# 沿革：本约束第 1 条点名的行为（apt/pip/npm/git clone/docker pull …）曾由
# `adapter/eval_bridge.py` 的 `EGRESS_RULES` 编译成判据，该实现已随评估面于 2026-09 拆除
# （原委见 README.md「评估与轨迹面已移除」）。
# 下面这段正文不受影响 —— 它仍在注入 prompt，是行为层红线，与判据实现无关。


# 跨题隔离红线（防止 agent 读 /work 其他题目的 FLAG/MEMORY/事件日志，抄袭别题 flag）
#
# **注入点只有一处**：`write_context_md()` 把它写进逐题 CLAUDE.md。
# 2026-09 死码清扫删掉了 `build_task_prompt` 里那份逐字重复的第二份 —— 理由见那里的注释。
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
2. **禁止**读取**本容器/镜像内**除工具字典外的代码源码：`/app`、`adapter/`、`/opt/redpilot`、`*.py`、`*.env`、`docs/` 等——这属于平台实现细节，不是题目内容。
3. **禁止**读取/使用环境变量与密钥文件：`BENCHMARK_TOKEN`、`SOLVER_API_KEY`、`DEEPSEEK_API_KEY`、`.agent.env`、`.env` 等（含一切 token/API key/口令）。
4. **禁止**查看**其他 worker** 的日志/实时状态（`agent/logs`、`agent/status`、`worker-*.json`、`docker` 等）——里面可能有他题的解题过程与 flag。
5. **唯一允许攻击的目标**：任务里给出的 `目标地址`（task targets）及其合法派生（其自身开放的端口/端点/内网映射）。除此之外的你看到的任何主机、服务、文件都是**边界外**。
6. **禁止**读取 `_transcripts/` 目录（你自己的历史会话转录）——框架已把有价值信息提炼进 MEMORY.md / tried_commands.md / 工作目录产物，原始转录又长又旧，读它纯属浪费回合。同理，artifacts/ 里的原始数据先看 MEMORY.md 的结论再决定要不要重新解析。
违反以上任何一条 = 视为无效作业，即使读到也读取不到正确答案，并可能被判定作弊。

## 🌐 目标访问（只有直连一条路）
- **直连是唯一通道**：`curl -s -m 5 -o /dev/null -w "%{http_code}" http://<目标>/` 先测通；测通后**所有工具一律直连**（nmap/sqlmap/curl 都直接跑）。本环境共享网络栈路由可达目标网段，直连即可到达。
- **不存在任何代理**：没有官方代理通道，`172.18.0.1`（网关）上的任何端口都不可用、也绝不是目标。不要寻找/搭建/使用代理，不要用 proxychains4（与环境不兼容，纯浪费回合）。
- 目标连续超时/拒绝时按 INFRA_BLOCKED 处理并停止该方向，不要试图绕路——绕不出去。
- 目标能连上但**持续返回 5xx**（后端崩溃，任何请求都不成）时按 TARGET_BROKEN 处理并停止该方向——框架会重启题目容器。
- **不要**通过读取 `/app` 源码、环境变量或监控日志来研究"目标怎么连"。"""


# 子 Agent 委派指引（[B57] 注入 build_task_prompt；扩展未装载时不注入，
# 免得教 agent 用一个根本不存在的工具）
_SUBAGENT_GUIDE = """## 🧵 子 Agent 委派（遇到困难时随时叫帮手）

你有一个 `subagent` 工具，能在**独立上下文**里派出新的 pi 进程替你干活。

**核心原则：先自己做，做不动了再叫人。** 不要一上来就派——先亲手摸清题目的
基本情况（端口、服务、入口点）。当你发现自己遇到以下情况时，**果断派子 Agent**：

- 🔴 **卡住了**：同一个方向试了 3+ 次没进展，换攻击面又不确定往哪换
- 🔴 **面太多**：发现了多个独立服务/端口/入口，一个人串行探太慢
- 🔴 **深挖费劲**：某个点需要跑大量命令（枚举/爆破/解码），会污染你的上下文
- 🔴 **需要复核**：你有个重要发现但不确定，需要干净上下文独立验证

可用角色：
- `scout` — 并行侦察：`{"tasks":[{"agent":"scout","task":"探 A 面..."},
  {"agent":"scout","task":"探 B 面..."}]}`，最多 8 个任务、并发 4
- `worker` — 深挖单点：某个面要翻很多页/跑很多命令，派它去做你只收结论
- `checker` — 独立复核：用干净上下文重跑验证你的发现

铁律：
1. **派活必须自带背景**：子 Agent 看不到你的任何历史。目标地址、你已掌握的信息、
   你要它回答的具体问题——没写进去就等于白派。
2. **要求它附证据**：它的最终文本会回到你这里，但中间过程你看不到。
   task 里必须写明「结论必须附命令原文与输出片段」。
3. **边界要重复一遍**：子 Agent 与你同目录、同网络，但不知道你已知的边界——
   派活时把「只碰给定目标、禁止联网下载」再交代一次。
4. **它的结论不能代替你的判断**：最终提交由你负责。
5. **每个子 Agent 有约 8 分钟挂钟预算**：到时会被强杀，带着已完成的局部
   结论返回（结果里会注明超时）。派活切到 8 分钟内能出结论的粒度——
   「穷尽分析整个二进制」会超时作废，「还原 VM 的 handler 语义并给出
   下一步假设」刚好。"""


def write_context_md(workdir: str) -> str:
    """写入 Agent 上下文指令文件（Pi/Claude 均支持 CLAUDE.md）。

    `_ISOLATION_CONSTRAINT` 的**唯一**注入点就在这里（2026-09 起；此前
    `build_task_prompt` 还会再 append 一份逐字相同的，已删）。
    调用点在编排层每场会话的建上下文处（`orchestrator._solve_one_unlocked`），
    先于 `build_task_prompt` —— 别把它挪到 prompt 之后。
    """
    path = os.path.join(workdir, "CLAUDE.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_CLAUDE_MD)
        f.write("\n\n" + _ISOLATION_CONSTRAINT)
    return path


# 沿革（2026-09 死码清扫）：这里原有 `write_memory(workdir, content)`，**零调用点**
# ——它在 `orchestrator.py` 被 import 却从未被调用过。MEMORY.md 实际由 agent 自己写，
# 再由 `orchestrator._merge_memory` 往受围栏区域合并。框架侧那条写入路径从来没接上，
# 留着会让人以为"记忆写入是受控的"（TARGET §3.6 讨论过这一点）。


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


def _subagent_scheduling_policy(task: AgentTask, session_idx: int) -> str:
    """子 Agent 派发策略：不限预算，让 Agent 根据实际体感自主决定。"""
    if not _subagent_on():
        return ""
    return (
        "## 子 Agent 调度\n"
        "先自己做，做不动了再叫人。具体触发条件和用法见上方「子 Agent 委派」章节。\n"
        "没有次数限制——你觉得需要帮手就派，觉得能搞定就自己做；\n"
        "唯一硬约束是每个子 Agent 约 8 分钟挂钟预算，超时会带着局部结论被召回。"
    )


_EFFICIENCY_POLICY = """## 解题效率约束（必须遵守）
- 每条命令只验证一个假设；不要把多个大型扫描串在一条 shell 命令里。
- 普通枚举先用小字典/常见路径；扫描器的工具级超时按目标网络响应情况设置，框架不设固定秒数上限。
- 不要使用静默的无限扫描（例如没有任何进度/阶段输出的全字典 ffuf/gobuster）；需要长探测时分段执行并保留可见摘要。
- 同一假设允许适量复核：最多做 2 次验证/参数变体；一旦证据已经足够，或连续 2 次验证没有新增事实，就切换攻击面或进入下一阶段，不要继续重述同一推理。
- `tried_commands.md` 和 `[PI-SAFETY-REPEAT]` 表示命令已经执行过；应改变目标/参数/路径，而不是原样重试。
- 多 flag 题拿到部分 flag 后继续推进剩余阶段；效率规则只能触发换向，不能提前结束整题。
- 多 flag 题连续无新事实时也不能直接切题：先完成一次平台提示复核，再给提示后的完整探索窗口；只有该窗口仍无可验证进展才结束。
"""


_CHALLENGE_PREFLIGHT = """## 题目信息预读（硬性前置步骤）
在任何扫描、利用、提交或写入 FLAG 之前，先完整阅读并核对上面的 `## 任务`，以及当前工作目录中框架提供的题目说明和上下文文件。
第一步必须先输出一份简短摘要，至少包含：目标/入口地址、答案数量和预期格式、难度、已知约束、准备验证的攻击面。
摘要完成前禁止调用 bash、网络扫描、exploit、submit 或写入 FLAG；题目信息缺失的字段明确标为"未知"，不得猜测或补写赛题答案。
完成摘要后再按"低成本侦察 → 证据验证 → 提交"的顺序行动；多答案题先规划阶段并保留对剩余答案的探索，不能拿到一个就结束。
"""


# 前次记忆注入上限（B14：2000 → 4000）。实测某题的 MEMORY.md 达 9184 字节，
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
    rejected_flags: list = None,
    unverified_flags: list = None,
    unverified_details: list = None,
    heimdall_map: str = "",
    continuation_note: str = "",
    subagent_nudge: str = "",
    identity_note: str = "",
    surface_note: str = "",
    **kwargs,
) -> str:
    """
    组装完整 prompt

    技能面**不注入正文**（2026-09-16 起）：路由归 pi 的原生渐进披露
    （`_install_skills` 把 skills/ 软链进每题 HOME，系统提示自带
    `<available_skills>` 名录），这里只补一段"从清单里自己挑"的自主调用指引。
    仅当开关关闭（pi 侧不装技能）时才退回完整名录 XML 兜底。
    """
    sections = []
    # ── 角色 ──
    sections.append(
        "你是一个自主安全测试 Agent。你已获得明确授权对以下靶场目标进行安全评估。\n"
        "找到题目要求的所有答案（flag、密码、密钥等）并写入 FLAG 文件。"
    )
    # ── 运行环境约束（隔离内网 · 禁止联网下载）──
    if os.environ.get("ADAPTER_OFFLINE_MODE", "1") == "1":
        sections.append(_OFFLINE_CONSTRAINT)

    # ── 跨题隔离红线（防偷读别题 flag）──
    # 2026-09 死码清扫：这里曾再 append 一遍 `_ISOLATION_CONSTRAINT`，与
    # `write_context_md()` 写进逐题 CLAUDE.md 的那份**逐字重复**（同一常量、
    # 每次会话白烧 1834 字符）。**只走 CLAUDE.md 一条路** —— 那是 pi 的项目指令
    # 文件：Agent 可随时重读，且**子 Agent 进程**（同 cwd 的独立 pi）也会加载它，
    # 而父会话的 prompt 不会传给子 Agent。隔离红线恰恰必须对子 Agent 也生效。
    # 原委与判定依据见 docs/architecture/TARGET_ARCHITECTURE.md §2.1（`:235-238`、`:374`）。
    # ── 降权身份边界（M1；隔离关闭时为空串，整段不注入）──
    if identity_note:
        sections.append(str(identity_note).strip())
    # ── 上一场已关闭的攻击面（M3；无记录时为空串）──
    if surface_note:
        sections.append(str(surface_note).strip())
    # [B57] 子 Agent 委派指引（总开关与 pi_agent._install_subagents 同源）
    if _subagent_on():
        sections.append(_SUBAGENT_GUIDE)
        sections.append(_subagent_scheduling_policy(task, session_idx))
    sections.append(_EFFICIENCY_POLICY)

    # ── 任务信息 ──
    task_lines = [
        "## 任务",
        f"- 目标: {task.objective}",
        f"- 地址: {task.target_str()}",
        f"- Flag 数量: {task.flag_count}",
    ]
    if task.flag_count > 1:
        submitted_count = max(
            int(flags_submitted or 0),
            int(getattr(task, "correct_flag_count", 0) or 0),
        )
        remaining = max(0, task.flag_count - submitted_count)
        task_lines.append(
            f"- 已提交: {submitted_count}/{task.flag_count}，剩余 {remaining} 个（多 flag 题：剩余 flag 通常在内网深处，见内网编排）"
        )
        if submitted_count:
            task_lines.append(
                "- 续解状态: 已确认的 flag 可能已从 FLAG 投递队列自动移除；"
                "不要把 FLAG 是否为空当作完成判据。仅当上面的已提交数量达到总数时才结束。"
            )
    if task.flag_format and task.flag_format != "flag{...}":
        # 只在平台明确指定了非默认格式时才显示，避免误导 Agent 以为
        # 所有答案都必须是 flag{...} 格式。Agent 应从题目描述中判断
        # 实际要求的答案格式（密码、hash、密钥等）。
        task_lines.append(f"- 答案格式: {task.flag_format}")
    if task.difficulty:
        task_lines.append(f"- 难度: {task.difficulty}")
    evidence_policy = flag_evidence_policy(
        task.category or "", targets=task.targets, files=task.files,
        workdir=task.workdir)
    if evidence_policy.local_allowed:
        if evidence_policy.declared_inputs:
            task_lines.append(
                "- 取证模式: 远端或官方本地材料；可用本地输入: "
                + ", ".join(evidence_policy.declared_inputs))
        else:
            task_lines.append(
                "- 取证模式: 无网络本地题；只认可直接运行原始题目程序得到的完整输出，"
                "不要把静态 strings/grep 命中或自己生成的文件当答案。")
    elif evidence_policy.remote_artifact_allowed:
        task_lines.append(
            "- 取证模式: 活靶场响应；若当前靶场明确提供原始二进制/附件，可先从当前"
            "目标下载，再对未改写的原件做可复现分析。不得把其他本地文件、静态扫描"
            "结果或历史产物当答案。")
    else:
        task_lines.append("- 取证模式: 活靶场响应；本地静态字符串不作为提交证据。")
    sections.append("\n".join(task_lines))
    # 题目信息预读必须紧跟任务元数据，确保 Agent 在任何工具调用前先完成摘要。
    sections.append(_CHALLENGE_PREFLIGHT)

    # A Pi process is intentionally short-lived.  Without an explicit marker,
    # every process sees the same task header and often starts the entry scan
    # again even though the driver has already confirmed one or more stages.
    # Keep this separate from the answer/evidence sections: it is a control
    # hand-off containing only counters and steering text supplied by the
    # driver, never candidate values or raw tool output.
    if continuation_note and continuation_note.strip():
        sections.append(continuation_note.strip())
    # 子 Agent 自评触发（驱动在首场未解后强制注入）
    if subagent_nudge and subagent_nudge.strip():
        sections.append(subagent_nudge.strip())
    elif session_idx > 0:
        sections.append(
            "## 连续会话续接\n"
            f"这是同一题的第 {session_idx + 1} 个连续会话（目标实例可能已重建）。先承接当前 MEMORY.md、"
            "黑板摘要和已尝试命令，禁止把会话边界当成新题重新扫描；若上一阶段已确认，"
            "立即转向尚未验证的主机、服务或提权路径。"
        )

    # ── 内网多阶段编排（仅多 flag 题注入，节省 token）──
    if task.flag_count > 1:
        sections.append(_INTRANET_ORCHESTRATION)

    # ── Skill 名录（路由交给 Agent）──
    # 2026-09-16 技能库换成上游 yaklang/hack-skills（103 个技能，自带
    # hack → 分类入口 → 深度题面 三层路由）。框架不再用关键词表挑 top-2 正文
    # 注入——那张表覆盖不了题面、且每次同步技能库都要手改。路由归还给 pi 的
    # 原生渐进披露：_install_skills 把 skills/ 软链进每题 HOME，pi 的系统提示
    # 自己就带 <available_skills>（名字+描述+路径），Agent 按题目分析 read 全文。
    # 这里只在原生面缺席时兜底（ADAPTER_SKILL_AGENT=0，prompt 语料冻结那条路径）。
    if _skill_agent_on():
        sections.append(_SKILL_SELF_SERVE_NOTE)
    else:
        # 兜底：至少告诉 Agent 有哪些 skill 可用、正文在哪读
        # （仅此路径需要名录——懒构造，别让默认路径白扫一遍技能目录）
        sections.append("## 可用技能\n\n" + _get_skill_store().skill_summary_xml())
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

    # ── 观察者的镜子（B61）──
    # 位置：黑板（客观事实）与 MEMORY（主 Agent 自己的笔记）之间 —— 这张图是
    # 对**它自己行为**的反射，三者并列但不同源。图里没有任何指令（红线 1），
    # 这一段注释本身也不得含祈使句。
    if heimdall_map and heimdall_map.strip():
        sections.append(heimdall_map.strip())

    # ── 前次记忆 ──
    # B14：不再要求 session_idx > 0 —— 该计数每次 visit 从 0 重新起算，而
    # MEMORY.md 正是跨 visit 的交接文件，复访首场恰恰最需要它（实测某题首场
    # 0 次注入、另一题要到同 visit 第二场才注入）。文件存在性检查已排除
    # 「该题从未做过」的情形。
    if prior_memory_path and os.path.isfile(prior_memory_path):
        try:
            with open(prior_memory_path, "r", encoding="utf-8") as f:
                prior = f.read().strip()
            if prior:
                sections.append(f"## 前次会话记忆\n{prior[:_PRIOR_MEMORY_MAX]}")
        except Exception:
            pass

    # ── 已被平台否决的候选（跨会话错误账本 .rejected_flags）──
    # 账本此前只用于「跳过重复提交」，agent 完全不知道 → 同一个串在后续会话
    # 被重新发现、重新写进 FLAG、再被判 INCORRECT。这里回灌给 agent，让它不再
    # 原地打转；只列本题内闭环信息，不含任何题号/靶标情报。
    # 只给指纹、不给原文：原文回灌会把判错串反复摆回模型面前、反而强化执念。
    # 指纹既能比对（sha1 前 8 位），又不复现内容。
    # 代码层的重复提交闸仍按 .rejected_flags 原文比对。
    if rejected_flags:
        _rj = [str(x).strip() for x in rejected_flags if str(x).strip()][:20]
        if _rj:
            _fp_lines = []
            for _b in _rj:
                _h = hashlib.sha1(_b.encode("utf-8")).hexdigest()[:8]
                _head = _b[:3]
                _tail = _b[-3:] if len(_b) > 6 else ""
                _fp_lines.append(
                    f'  ✗ sha1:{_h}  长度 {len(_b)}  首尾 “{_head}...{_tail}”')
            sections.append(
                "## ⛔ 已被平台判错的候选（不要再提交）\n"
                "下列候选此前已提交并被平台判为 INCORRECT，不要重复提交，\n"
                "也不要再花回合重新推导它们。\n"
                "为避免把判错内容反复摆回你面前，这里只列指纹——要确认手上的候选\n"
                "是否已经试过，算 sha1(候选 body) 取前 8 位比对即可：\n"
                + "\n".join(_fp_lines)
            )

    # ── [B54] 提交口径（框架规则，每场必注入）──
    # 两个目的，**都是预防性的**（不依赖上一场有没有被拒过）：
    #   1. 说清「什么才算证据」，堵掉「凭空编一个串就交」这条白费回合的写法；
    #   2. 给「真推导但没资格」的候选一条**可执行的补救路径** —— 影子审计 29 条
    #      被拒里 19 条实为真答案，它们缺的不是能力，是「把来源跑出来」这一步。
    #      这是那 19/29 唯一能改善的地方：闸门不放宽，改的是产出形态。
    # 红线（B45）：**不得**写成「判错 / 别再提交」—— 只讲证据形态，不否定答案。
    #
    # [B54b] 落盘后渲染真实 prompt 才发现初版与 `## 工作指令` 第 5 条
    # （`找到 flag: echo 'flag{...}' > FLAG`）**正面冲突**：初版把 echo 交付
    # 说成「必然被拦下」，会教唆 agent 不敢写 FLAG —— 而 FLAG 是它向驱动交接
    # 答案的**唯一通道**，堵掉等于打断整条链路。
    # 根因是漏了判定的先后：verify 里 `if evidence:` **优先于** `else: authored`，
    # 「先在某条命令输出里看到、之后再 echo 交付」完全合法。故本节必须区分
    # **取证**（在输出里看到，证据成立）与**交付**（echo 进 FLAG，必须做）。
    _halu_n = 0
    try:
        from . import hallucination as _hm
        _halu_n = int(_hm.state(task.workdir).get("fabrications", 0) or 0)
    except Exception:
        _halu_n = 0
    if evidence_policy.local_allowed:
        _evidence_rule = (
            "**平台认的证据**：完整答案**逐字出现在某条工具命令的输出里**，"
            "且该命令要么来自与活靶标的交互，要么可复现地读取/运行任务声明的原始本地材料。")
        _local_warning = (
            "  3. 凭空猜测的串、或输入被改写后伪造出来的输出（而不是从官方材料"
            "推导/解码打印出来的）")
        _remedy = (
            "⚠️ **你自己推导 / 解码 / 计算出来的值，不等于它是错的** —— 但必须把"
            "推导链真实执行一遍：从活靶标取数，或从官方本地输入读取数据，令完整"
            "答案出现在命令输出里。自己把结论写进临时文件再打印不算取证。")
    else:
        _evidence_rule = (
            "**平台认的证据**：完整答案**逐字出现在某条工具命令的输出里**，"
            "且那次输出来自你和**活靶标**的交互（靶标响应、在靶标上跑出来的 stdout 都算）。")
        _local_warning = "  3. 只在本地静态文件里出现、没和活靶标交互过的串"
        _remedy = (
            "⚠️ **你自己推导 / 解码 / 计算出来的值，不等于它是错的** —— 只是还没变成"
            "平台认得的形态。补救办法不是放弃它，而是**回到靶标把来源跑出来**："
            "让「向靶标取数 → 推导 → 打印」这条链路真实地执行一遍，使完整答案"
            "出现在**命令输出**里。这样它才既是真的、又有据。")

    _sub = [
        "## 🎯 提交口径（框架规则 —— 决定你的答案能不能上平台）",
        "",
        _evidence_rule,
        "",
        "⚠️ 分清「**取证**」和「**交付**」—— 两步都要做，别搞混：",
        "  · **取证**：先在某条命令的**输出**里看到答案。证据在这一步就成立了。",
        "  · **交付**：确认看到之后，照常写入 FLAG 交上来：一律用 `printf '%s\\n' '答案内容' >> FLAG` 追加；文件不存在时 `>>` 会自动创建。",
        "    多答案题中，驱动会即时提交已取证的行，并在安全的会话边界移走已确认行；这是正常的投递确认，不是要求你停下。",
        "    这一步**必须做**，框架**不会**因为你写入 FLAG 就不认 —— 证据在取证那步已经成立。",
        "  只有「**除了你自己敲的那一下，它从未出现在任何命令输出里**」才会被拦下。",
        "",
        "下面三种写法**必然被拦下，纯属浪费回合**：",
        "  1. 猜一个 / 编一个串直接 echo 进 FLAG —— 它在任何输出里都没有来源",
        "  2. 把结论写进自己的文件（MEMORY / notes / 黑板）再 cat 出来**当作证据**",
        "     （记录进展到 MEMORY.md 是正常动作，别停；这里说的是拿它当来源）",
        _local_warning,
        "",
        _remedy,
    ]
    if _halu_n:
        _sub += [
            "",
            "⛔ 上一场你有 **%d 条候选从未在任何工具输出里出现过**（纯属自造）。" % _halu_n,
            "   同一套推导重复再多次也不会让它们变真 —— 请换思路，别再重申同一个串。",
        ]
    sections.append("\n".join(_sub))

    # 与上一节的区别是根本性的：上一节是**平台判错**（确凿为假），本节是框架
    # **没能坐实**（缺「活靶标响应」证据而放弃提交）—— 后者里混着真 flag
    # （影子审计实测 19/29 被拒候选实为正确答案）。故措辞红线：
    # 「未通过验证 ≠ 它是错的」，不得写成「判错 / 别再提交」，否则会劝退 agent
    # 手里那条真答案。本节唯一目的：别让 agent 因为 FLAG 里出现过它，就以为
    # 本题已解、提前收工。同样只给指纹、不给原文。
    # 但当连续多场 unverified-only 时（driver 传了 unverified_details），
    # 指纹模式无法让 Agent 把"正在发现的 flag"与"被拒列表"关联起来 → 死循环。
    # 此时改为展示完整 body + 拒绝原因 + 可操作的取证补救建议。
    if unverified_details:
        _uvd = [d for d in unverified_details if isinstance(d, dict)][:5]
        if _uvd:
            _uvd_lines = []
            for _d in _uvd:
                _body = str(_d.get("body", ""))[:60]
                _reason = str(_d.get("reason", "unknown"))
                _count = int(_d.get("count", 1) or 1)
                _uvd_lines.append(
                    f"  ✗ `{_body}...` — 已出现 {_count} 次，原因: {_reason}")
            sections.append(
                "## 🚨 反复被拒的候选（必须换取证方式——这是最后机会）\n"
                "以下答案已被多次发现但**无法通过框架验证**，每条都从未提交到平台：\n"
                + "\n".join(_uvd_lines) + "\n\n"
                "**问题在于你的命令格式**。验证器要求答案出现在**命令的直接输出**中。\n"
                "你之前的命令失败原因（必须全部避免）：\n"
                "  ❌ `curl -o resp.html` 然后 `grep flag resp.html` —— 两步法，证据链断\n"
                "  ❌ `cd /work/... && curl ...` —— `cd &&` 前缀让验证器认不出来源\n"
                "  ❌ `python3 -c '...requests.get...'` —— 脚本内的网络调用不算直接输出\n\n"
                "**正确做法**：用**一条命令**让答案直接出现在 stdout 里：\n"
                "```\n"
                "curl -s -b /tmp/cookies.txt http://<目标>/dashboard.php\n"
                "```\n"
                "关键要求：\n"
                "1. curl/wget 必须是命令的**第一段**——前面**不要加** `cd`、`export`、`rm` 等\n"
                "2. 不要 `curl -o` 写文件再 cat/grep —— 让答案直接出现在命令输出里\n"
                "3. 如果需要登录，先单独执行登录命令保存 cookie，再单独执行 curl 读取页面\n"
                "4. 已经知道答案？直接用上面的命令重新获取一次就行\n"
            )
    elif unverified_flags:
        _uv = [str(x).strip() for x in unverified_flags if str(x).strip()][:20]
        if _uv:
            _uv_lines = []
            for _b in _uv:
                _h = hashlib.sha1(_b.encode("utf-8")).hexdigest()[:8]
                _head = _b[:3]
                _tail = _b[-3:] if len(_b) > 6 else ""
                _uv_lines.append(
                    f'  ? sha1:{_h}  长度 {len(_b)}  首尾 “{_head}...{_tail}”')
            sections.append(
                "## ⚠️ 尚未坐实的候选（不要据此收工）\n"
                "下列串曾被写入 FLAG，但**没能通过框架验证**（没有在靶标的真实\n"
                "响应里逐字出现），已从 FLAG 摘除、也未提交平台。\n"
                "注意：**「未通过验证」不等于「它是错的」**—— 只是证据不足。\n"
                "1. 不要因为 FLAG 里出现过它们就认为本题已解；FLAG 为空就继续找。\n"
                "2. 不要再花回合重新推导它们，除非你能拿到新的证据。\n"
                "3. 若你在某条命令输出里**亲眼看到**其中某条（说明它确实来自\n"
                "   目标），把它重新写入 FLAG 并附上来源命令 —— 那才是有效证据。\n"
                "要确认手上的候选是否在此列，算 sha1(候选 body) 取前 8 位比对：\n"
                + "\n".join(_uv_lines)
            )

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
        "3. 侦察 → 漏洞发现 → 利用 → 获取答案\n"
        "4. 每个重要发现写入 MEMORY.md\n"
        "5. 找到答案：用 `printf '%s\\n' '答案内容' >> FLAG` 逐行追加（文件不存在也可用）；多答案题每交一条后立即继续当前阶段，直到已提交数等于总数\n"
        "6. 未解出时输出续接块:\n"
        "   已达成原语: <进展>\n"
        "   已证死路: <死路+原因>\n"
        "   下一步: <具体命令>"
    )

    return "\n\n".join(sections)
