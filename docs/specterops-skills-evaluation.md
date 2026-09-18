# 评估：用 SpecterOps/skills 替换现有 skills

> 结论先行：**不建议全量替换。** 两个仓库不是同类物：现有 `skills/` 是面向 TSecBench
> 六维评分的**单层技能库**；`SpecterOps/skills` 是面向 Codex / Claude Code 的
> **插件市场**，主体是 AD/BloodHound、C2 开发、钓鱼、报告撰写，且绝大多数技能依赖
> 容器里**不存在**的外部服务。全量替换会净损失能力、并让现有测试变红。
>
> 建议走**选择性合并**（第 5 节）。若要硬替换，第 6 节给确切后果与命令。

---

## 1. 两个仓库的本质差异

| | 现有 `skills/` | SpecterOps/skills |
|---|---|---|
| 形态 | **单层** `skills/<name>/SKILL.md` | **插件市场** `plugins/<plugin>/skills/<skill>/SKILL.md` + `.codex-plugin/` + `.claude-plugin/` |
| 数量 | 23 | 72（+3 standalone） |
| 总量 | 139,995 字节 | （分散在 24 个 plugin） |
| 面向 | TSecBench 闭卷靶场（flag 提交） | 真实企业红队 / 代码审计 / 报告交付 |
| 许可证 | 仓库内自持 | **Apache-2.0**（仓库）+ 各 skill 标 **MIT** |
| 调用语法 | 由 `skill_loader` 关键词打分预选 | 部分 skill 用 Codex 专有 `$skill-name` 调用 |
| 依赖 | 纯 Markdown 指令 + 镜像内已装工具 | 大量依赖 MCP 服务与外部基础设施 |

### 布局不兼容（硬事实）

`skill_loader._scan()` 只扫**一层**：

```python
for entry in os.listdir(self._dir):
    skill_md = os.path.join(self._dir, entry, "SKILL.md")
```

把 SpecterOps 仓库原样拷进 `skills/`，顶层条目是 `plugins/`、`agents/`、`skills/`……
**没有一个含 SKILL.md → 扫描结果为 0**，而这正是 `test_skills_root.py` 那条回归守卫
专门盯过的失效模式（"技能面静默退化成 0，只打一条 warning"）。必须扁平化。

frontmatter 本身**基本兼容**（`name` + `description` 都有；`_parse_frontmatter` 是朴素行解析，
多出的 `license`/`metadata` 键无害）。真正的阻断项是布局与依赖。

---

## 2. 覆盖面矩阵（按 TSecBench 六维）

TSecBench：Web / 二进制漏洞挖掘、漏洞利用、多阶段渗透、云攻击、对抗规避。

| TSecBench 维度 | 现有 skills | SpecterOps 覆盖 |
|---|---|---|
| **Web 利用** | `web`(5.3K) `web-deep`(15.8K) `web-attack`(18.6K) `web-recon-toolkit`(2.5K) `waf-bypass`(2.8K) `java-exploit`(18.9K) | **零**（`webapp-review`/`security-review` 是 git diff 的白盒代码审计，不是黑盒利用） |
| **二进制 / pwn / reverse** | `pwn` `network-pwn` `reverse`(5.3K) `reverse-engineering`(3.2K) `sandbox-escape` | 仅 `binary-ninja-mcp-analysis` `ghidra-mcp-analysis`（**需 MCP**） |
| **多阶段渗透 / 后渗透** | `pentest` `post-exploit`(6.8K) `known-cve-playbook` | `nmap-parse` ✅ `ssh-ops`（部分）`proxychains-tunnel` ❌（见 §3） |
| **云攻击** | `cloud`(16.0K) `cloud-security` | `iac-attack-surface`（**需 terraform**，且是 IaC 审计非对象存储利用） |
| **对抗规避** | `evasion` `waf-bypass` | **零** |
| **AD / 域** | `ad`(6.5K，Kerberoasting/委派/ADCS) | 9 个 `bloodhound-*`，但**全部需 BloodHound CE + MCP** |
| **其它现有** | `crypto` `forensics` `ebpf` `llm` `cicd` | **零**（改为 8 report-timeline + 6 social-engineering + 6 workflows + 6 C2/payload） |

### SpecterOps 72 个技能的实际归类

| 插件 | 数量 | 与闭卷靶场相关性 |
|---|---:|---|
| bloodhound | 9 | 需 BloodHound CE 图数据库 + MCP → **不可执行** |
| report-timeline | 8 | 报告交付 → **无关** |
| workflows-development | 6 | 开发脚手架 → **无关** |
| social-engineering | 6 | 钓鱼/话术 → **无关且超范围** |
| payloads | 4 | Electron 打包 → 无关 |
| ops-infrastructure | 4 | 1 个环境冲突、3 个需外部工具 |
| report-drafting | 3 | 报告 → **无关** |
| ops-sccm | 3 | 需 SCCM 环境 → **不可执行** |
| ops-reconnaissance | 3 | 1 ✅ / 2 需外网 API |
| ops-appsec | 3 | 白盒代码审计（非靶场利用） |
| code-review-and-qa | 3 | 开发流程 → 无关 |
| c2-mythic / c2-cobaltstrike / c2-outflankc2 / c2-extensions | 10 | **需 C2 框架**，无关 |
| reverse-engineering | 2 | 需 Binary Ninja / Ghidra MCP |
| tradecraft-{windows,mac,linux} | 3 | Linux 一个或可借鉴 |
| ludus / go-review / workflows-research / codex-observability | 4 | 无关 |

**零新增基础设施、可直接用：`nmap-parse`（纯文本处理，2,968 B）。** 加上"部分可借鉴"
（`ssh-ops`、`tradecraft-linux`、`webapp-review`）也**约 1–5 个 / 72**。

---

## 3. 三个硬冲突（不是"不划算"，是"会出错"）

1. **`proxychains-tunnel` 与本环境明令矛盾。**
   `taskprompt.py` 四处写明（L58/L87/L88/L198）：
   > "需要隧道时使用已安装的 `chisel` 或 `ssh -L/-D`，**不要使用环境不兼容的 `proxychains4`**"
   > "不存在任何代理……不要用 proxychains4（与环境不兼容，纯浪费回合）"

   导入这个技能会**主动误导 Agent 去浪费回合**。

2. **AD 技能指向不存在的图数据库。** `bloodhound-analysis` 开篇即要求
   "check the BloodHound connection, verify MCP health"。容器里没有 BloodHound CE、
   没有数据、没有 MCP（已核：`bloodhound` 未安装）。而现有 `ad` 技能是**纯命令行可执行**
   的 Kerberoasting/委派/ADCS——对容器内挑战更对症。

3. **C2 / 钓鱼 / 报告类与"闭卷 flag 基准"目标正交。** 这些在真实红队交付里有价值，
   但 TSecBench 计分是 flag 提交，不产出报告、不建 C2、不钓鱼。

---

## 4. 全量替换会破坏什么（可验证）

| 破坏项 | 证据 |
|---|---|
| **naive 拷贝 → 技能面归零** | `skill_loader._scan()` 只扫一层；`cp -r` 后顶层是 `plugins/`，无 `SKILL.md` → 0 个技能（正是 `test_skills_root` 守卫过的「静默退化」）。**必须扁平化**才能避免 |
| **回归守卫会「偶然通过」，比变红更糟** | `test_taskprompt_injects_a_matched_skill` 断言 `match_skills("web sql injection on /login.php")` 非空。但 `match_skills` 第 2/3 步是**描述指纹 + bigram 交集**，SpecterOps 的 `webapp-review` 描述含 "web" → 仍会命中。测试**不会拦下**这次替换，只会把一道黑盒 SQLi 题**静默路由到白盒代码审计技能** |
| **预选器大面积失效** | `_DOMAIN_SIGNALS` 硬编码映射到 `web/pwn/forensics/crypto/pentest/reverse/cloud/evasion/java-exploit/web-deep/web-attack/post-exploit/cicd/llm/ebpf/ad`；替换后除 `ad`（也已失效，见 §2）外全是死映射 |
| **净损失约 14 万字节** | 现有 139,995 B 覆盖六维；替换进来的可用部分约 1–5 个文件、不到 15 KB |
| **许可证合规** | 需保留 Apache-2.0 NOTICE + 各 skill 的 MIT 归属；原样拷贝而删署名违反两者 |

---

## 5. 建议：选择性合并（保留 CTF 核心，按需引进）

### 5.1 可以引进的（建议）

| 技能 | 理由 | 前置 |
|---|---|---|
| `nmap-parse` | 纯文本/XML 解析 + 侦察笔记，与"确定性侦察"设计天然契合 | 无 ✅ |
| `ssh-ops` | 隧道/运维命令范式，与现有 `chisel`/`ssh -D` 路线一致 | 无（需去掉 proxychains 倾向） |
| `tradecraft-linux`（节选） | 进程注入/持久化，可补 `post-exploit` 的 Linux 段 | 需审内容 |
| `webapp-review` / `security-review` | **仅当**遇到白盒源码题时作补充（现有 skill 偏黑盒） | 无 |

### 5.2 明确不引进的

`bloodhound-*`（9）、`ops-sccm`（3）、`c2-*`（10）、`payloads`（4）、`social-engineering`（6）、
`report-*`（11）、`workflows-*`（7）、`codex-observability`（2）、`ludus`、`go-review`、
`proxychains-tunnel`、`iac-attack-surface`。
**理由统一：依赖不存在的服务，或与闭卷 flag 基准目标正交。**

### 5.3 合并机制（若采纳）

1. **扁平化 + 加前缀**：`skills/sp-<name>/SKILL.md`，避免与现有同名（如 `reverse-engineering` 插件）。
2. **加 `requires:` frontmatter**，让 `skill_loader` 跳过依赖不满足的技能
   （例如 `requires: bloodhound-mcp` → 容器无该服务则不入库）。这是对预选器的一个小而有用的增强。
3. **许可证归属**：新增 `skills/_vendor/specterops/NOTICE`（Apache-2.0）+ 在 `skills/README` 标注来源。
4. **`_DOMAIN_SIGNALS` 增补**（不替换）：为引进的技能加信号，保持现有信号不动。
5. **回归**：`test_skills_root` 必须保持绿（技能数只增不减）；新增"引进技能可被 `load_skill` 读出"的用例。

### 5.4 若你真正想要的是"更专业的红队技能面"

那目标不是替换，而是**补两个现有缺口**：
- **AD 可执行化**：把 BloodHound **数据**（若题目给）或 `bloodhound-python`/`impacket` 的
  采集命令写进现有 `ad` 技能，而不是引进需要 CE 服务的 `bloodhound-*`。
- **白盒代码审计**：现有技能偏黑盒；可引进 `webapp-review`/`security-review`/`owasp-security-code-review`
  作为**新增**，与 `web*` 并存。

---

## 6. 如果你仍要硬替换：确切后果与命令

```bash
# ⚠️ 破坏性；执行前先 git stash / 分支
git clone --depth 1 https://github.com/SpecterOps/skills /tmp/specterops
rm -rf skills/*
# 扁平化（注意：会丢失 plugin/agent/MCP 清单结构，且技能数仍为 0 除非改写 loader）
for f in /tmp/specterops/plugins/*/skills/*/SKILL.md; do
  n=$(basename "$(dirname "$f")"); mkdir -p "skills/$n"; cp "$f" "skills/$n/SKILL.md"
done
# 必须同时改 skill_loader._scan 支持新布局 + 重写 _DOMAIN_SIGNALS，否则：
#   · test_skills_root 变红
#   · match_skills 对 web/pwn/crypto/... 全部失效
```

**执行前请确认**：接受"失去 Web/pwn/crypto/forensics/云/规避 的技能覆盖，
换取 AD/C2/报告/代码审计技能"，并愿意同步改 `_DOMAIN_SIGNALS` 与测试。
**我的建议是不接受，走第 5 节的选择性合并。**

---

## 7. 待你确认的一个问题

> 你要的是 **(A) 更专业的真实红队技能面**（→ 补 AD 可执行化 + 白盒代码审计，保留现有），
> 还是 **(B) 就是要 SpecterOps 那套**（→ 我按 §5.3 做扁平化 + 许可证归属 + requires 过滤，
> 但**不删**现有 23 个）？

拿到确认后我立即实施：引进 `nmap-parse`/`ssh-ops`（+ 按选择加 `webapp-review`），
加 `requires:` 支持与 NOTICE，保持 `test_skills_root` 全绿。

---

*本文件为评估产物，**未改动任何代码或技能文件**。核验命令与数据均来自本次会话实测。*
