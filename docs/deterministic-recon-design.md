# 确定性侦察与事实编译（Deterministic Recon / Fact Compilation）

> 触发本次设计的命题：**"渗透测试的本质是信息收集，所以先用确定性工具跑一遍，再把已知事实做上下文工程。"**
> 本文件评估这个命题，并给出与现有架构（竞技场 / 黑板 / Heimdall / 止损 / 图工程）的接线方案。
> 关联：`docs/graph-engineering-design.md`（图）、`docs/top10-offensive-agents-deep-dive.md`（对照）。

---

## 0. 命题评估：对在哪里，要修正在哪里

### 0.1 成立的部分（且是本仓库当前最大的浪费点）

- **侦察确实是瓶颈**。业界共识（"渗透 80% 是侦察"），也是 top10 里 **Shannon** 的架构选择：
  `recon + 漏洞分析 ∥ agentic SAST → reconciliation → exploitation`——**先用确定性/半确定性手段
  把攻击面编译成事实，再交给利用 Agent**。
- **确定性工具应该先跑，因为这一段的推理密度最低、token 成本最高、且最不该由 LLM 即兴发挥**。
  现在本仓库把 nmap/ffuf/whatweb/nuclei 的**命令选择权完全交给 LLM**（`taskprompt.py` 第 78 行
  列工具，`skills/web-recon-toolkit` 给命令范式）——结果是：
  - 同一目标不同会话可能跑不同命令 → 记忆不可比、回归不可测；
  - LLM 会漏掉基线项（忘了看 `robots.txt`、忘了 `-sV`、忘了 `/.git/HEAD`）；
  - **枚举的 token 花在最不需要智能的地方**。
- **结构化输出是免费的事实源**。nmap `-oX`（XML；`-oJ` 已从新版 nmap 移除，别用）、nuclei `-jsonl`、httpx `-json`、ffuf `-of json`、dirsearch `--format=json`、whatweb `--log-json`
  已经是 schema；用它们建事实**不需要正则猜测**，比现在 `blackboard.observe()` 的正则可靠一个量级。

### 0.2 必须修正的三点

| 修正 | 原因 |
|---|---|
| **① 侦察 ≠ 渗透的全部** | TSecBench 有 6 维（Web/二进制漏洞挖掘、漏洞利用、多阶段、云、对抗规避）。二进制 pwn/reverse/crypto/forensics 的"信息收集"是 `file`/`checksec`/`strings`/`binwalk`/符号分析，**不是 portdiscovery**；云是 `s3cmd/aws` 枚举；规避是 WAF 指纹与响应差分。**必须按 category 分档，不能一套 pipeline 打天下。** |
| **② "先跑一遍"是手段，不是阶段** | 若做成"侦察阶段跑完 → 再交给 LLM"，会**空耗竞技场时间盒**（LLM 在扫描期间闲着）。应是 **sidecar 并发**：T0 秒级探针先出，LLM 立即开工；T1/T2 边跑边把事实喂进图，供下一场用。本仓库已有 eager-submit 线程做同类并发，是现成范式。 |
| **③ 收集 ≠ 杠杆** | 两个 Agent 收集到同样的事实，赢的那个是**知道哪个事实能开下一道门**。所以"事实编译"的产出不是"更多事实"，而是**目标导向的、有预算的 brief + 边界（frontier）**。否则就是 recon theater（跑了一堆、Agent 不用）。 |

### 0.3 一句话结论

> **把侦察从"对话"改成"编译"：确定性工具把攻击面编译成类型化事实，LLM 只读编译产物做推理。**
> 但编译器必须**按题型分档、并发执行、产出目标导向的 brief、且把负结果也记成覆盖度**。

---

## 1. 环境现实（决定方案可行性）

### 1.1 镜像里已有什么

| 层 | 工具 |
|---|---|
| `kali-linux-headless`（基础镜像） | nmap、nikto、whatweb、gobuster、ffuf、sqlmap、hydra、dnsutils、smbclient、enum4linux 等 Kali 标准集 |
| `Dockerfile` 追加 | **nuclei**、dirsearch、feroxbuster、chromium+playwright、gdb、ropper、qemu-user-static、ltrace/strace、python3-{pwntools,z3,gmpy2,sympy,pycryptodome,filebytes}、foremost、steghide、sshpass、chisel、awscli、s3cmd、redis-tools、jq |

### 1.2 ProjectDiscovery 工具集的实际可用性判定

| 工具 | 装了吗 | 在本靶场能否用 | 结论 |
|---|---|---|---|
| **nuclei** | ✅ | **能**（但依赖模板集，见 1.3） | 核心，保留并强化 |
| httpx | ❌ | 能（纯 HTTP 探测，无需外网） | **建议加**（Go 单文件，收益/成本比最高） |
| katana | ❌ | 能（爬虫，自带 headless 可选） | **建议加**（补 feroxbuster 覆盖不到的链接面） |
| tlsx | ❌ | 能（TLS 指纹） | 可选 |
| naabu | ❌ | 能，但 **nmap 已覆盖**且更全（`-sV -sC`） | 不加，用 nmap |
| subfinder | ❌ | **不能**（被动 DNS 需公网/API key，靶标是隔离内网） | **不加** |
| dnsx | ❌ | 仅内网 DNS 有意义，价值低 | 不加 |
| shuffledns / alterx | ❌ | 名字变异，靶标无此需求 | 不加 |
| asnmap / cloudlist / uncover / cdncheck | ❌ | 需公网资产库 | **不加** |
| cvemap | ❌ | 需联网 CVE 库 | 不加 |
| mapcidr | ❌ | 内网网段切分，可 shell 替代 | 不加 |

> **诚实结论**：用户点名的"ProjectDiscovery 工具集"里，对本环境真正有价值的是
> **nuclei（已有）+ httpx + katana**；被动/资产类工具（subfinder/dnsx/uncover/asnmap/cloudlist）
> 在闭卷隔离靶场里**基本无用**。设计应学 PD 的**方法论（JSONL 结构化输出 + 模板化）**，
> 而不是照搬它的工具清单。

### 1.3 一个必须先验证的前提：nuclei 模板集

`nuclei` 没有模板就是空壳。apt 装的 `nuclei` 未必带模板，而 `-update-templates` 需要外网。
**这是本方案的头号前置风险**，落地第一步必须验证：

```bash
docker run --rm redpilot-adapter:latest nuclei -tl | wc -l    # 模板数，0 = 空壳
ls /root/nuclei-templates 2>/dev/null | head
```

若为空，需在构建时把模板集**烤进镜像**（离线可用），并按 category 只加载相关 tags
（见 3.1 的 T2 分档），否则全量模板扫描会吃满时间盒。

---

## 2. 核心设计：侦察即编译（Recon as a Compiler）

```
        ┌─────────────┐   ┌──────────┐   ┌───────────┐   ┌──────────────┐
target →│ Recon Plan  │ → │  Runner  │ → │  Parsers  │ → │  Graph Facts │
(cat)   │ (分档声明式)│   │(sidecar) │   │(JSON→节点)│   │ (origin=OBSERVED)
        └─────────────┘   └──────────┘   └───────────┘   └──────┬───────┘
                                                                 │
                        ┌────────────────────────────────────────┘
                        ▼
                  ┌───────────┐        ┌────────────────────┐
                  │  Brief    │──────→ │ 主 Agent 的 prompt │
                  │(预算+目标)│        │ (只读、可推翻)     │
                  └───────────┘        └────────────────────┘
```

四层各司其职，**每层都可独立单测**：

| 层 | 职责 | 关键性质 |
|---|---|---|
| **Plan** | 按 category 给出**声明式**步骤表（工具、参数、超时、事实映射、档位） | 纯数据，可测；不写业务逻辑 |
| **Runner** | 执行步骤、捕获 JSONL、超时/失败隔离、并发、落原始产物 | 旁路；绝不阻断解题 |
| **Parser** | 工具 JSON 字段 → 图节点/边（**含负结果**） | 确定、幂等、无正则猜测 |
| **Brief** | 图 → 有预算、有目标、可推翻的文本 | 只读；不 dump 原始输出 |

**与已有图工程的关系**：确定性 Runner 是图的**第 4 个 writer**，且是**质量最高的 writer**
（工具 schema = 事实 schema，`origin=OBSERVED`，`evidence_id` 指向原始 JSONL 行）。
这正好补上图设计里"机械抽取靠正则"的短板。

---

## 3. Recon Plan：按 category 分档（纯数据）

档位的语义是**成本递增**：T0 秒级、T1 分钟级、T2 分钟级（模板）、T3 深水（按需）。

### 3.1 web / pentest（主力，收益最大）

| 档 | 步骤 | 命令范式 | 事实映射 |
|---|---|---|---|
| **T0** 指纹 | 存活+头 | `curl -si -m 5 http://T/` | endpoint、server 头 → service |
| | Web 指纹 | `whatweb -a3 --log-json=<workdir>/.recon/whatweb.json http://T/` | service/版本、框架 |
| | 端口 | `nmap -Pn -sV -T4 --top-ports 1000 -oX .recon/nmap.xml T`（需要时 `-p-`） | host:port、service |
| **T1** 内容发现 | 小字典 | `ffuf -u http://T/FUZZ -w <common> -mc 200,204,301,302,307,403 -of json -o .recon/ffuf.json` | endpoint（带状态码） |
| | 扩展名 | `dirsearch -u http://T/ -e php,asp,aspx,jsp,json,bak,txt --format=json -o .recon/dirsearch.json` | endpoint |
| | 常见泄漏 | `.git/HEAD` `.env` `swagger` `api-docs` `actuator` `server-status` `backup` | endpoint + 高价值标记 |
| **T2** 模板 | 暴露/配置 | `nuclei -u http://T -tags exposure,misconfig,tech,default-login -jsonl` | vuln、service |
| | CVE | `nuclei -u http://T -tags cve -severity critical,high -jsonl` | vuln（带 CVE id） |
| | 目录/爬取 | `katana -u http://T -jsonl`（若加入镜像） | endpoint（链接面） |
| **T3** 按需 | 注入/爆破 | **不进自动档**，留给 LLM 决策（sqlmap/hydra） | — |

T1 的字典必须**镜像内自带**（`/usr/share/wordlists/dirb/common.txt` 等已确认存在；
seclists 缺失时 skill 已有降级规则）。

### 3.2 pwn / reverse（确定性优势同样大，但工具完全不同）

| 档 | 步骤 | 命令 | 事实映射 |
|---|---|---|---|
| T0 | 类型 | `file BIN` | artifact.kind（ELF/PE/脚本） |
| | 保护 | `checksec --file=BIN`（pwntools 亦提供） | vuln（NX/PIE/Canary/RELRO 组合） |
| | 架构 | `readelf -h` / `objdump -f` | attrs.arch |
| T1 | 符号/字符串 | `strings -n 6 BIN`、`nm -D`、`readelf -s` | 线索（flag 格式、菜单、后门串） |
| | 依赖 | `ldd BIN`、`readelf -d` | service/lib |
| T2 | 反汇编 | `objdump -d`、`ropper --file BIN`、`gdb -batch -ex ...` | 语义节点（**易爆，须严格预算**） |
| | 动态 | `strace`/`ltrace` 跑一次 | 行为事实 |

### 3.3 crypto / forensics / misc

| 档 | 步骤 |
|---|---|
| T0 | `file`、`strings`、大小/熵、文件头识别（magic） |
| T1 | `binwalk -e`（若无则 `foremost`）、`steghide info`、`exiftool`（若装）、编码识别（base64/hex/rot） |
| T2 | 脚本化：`python3` + z3/sympy/gmpy2/pycryptodome（**这层是推理，交给 LLM，不进自动档**） |

> 关键：crypto/forensics 的"信息收集"产出多是**单个 artifact 的属性**，事实量小；
> 确定性部分只做 T0/T1，很快，其余必须 LLM。

### 3.4 cloud

| 档 | 步骤 |
|---|---|
| T0 | `aws --endpoint-url ... s3 ls` / `s3cmd ls` / `curl` bucket 根、ACL 探测（`?acl`、`?list-type=2`） |
| T1 | 给定凭据做 `aws sts get-caller-identity`、`iam` 枚举（**注意：可能在题面给 key**） |
| T2 | 对象遍历 → artifact 节点 |

### 3.5 evasion（对抗规避）

| 档 | 步骤 |
|---|---|
| T0 | 基线请求 vs 畸形请求的**响应差分**（状态码/长度/WAF 页面指纹） |
| T1 | 对已知 payload 的拦截特征记录 → `technique` 节点 + `BLOCKED_BY` 边（给 waf-bypass skill 用） |

### 3.6 通用 T0（所有题型）

`ip addr` / `ip route` / `/etc/hosts`（内网多段题必要）、目标连通性探针（沿用
`taskprompt.py` 的 `curl -s -m 5 -o /dev/null -w "%{http_code}"`）。

---

## 4. Runner：sidecar、时间盒、失败隔离

```python
# adapter/recon.py
@dataclass(frozen=True)
class ReconStep:
    id: str                 # "web.t1.ffuf_common"
    tier: int               # 0/1/2
    tool: str
    argv: tuple[str, ...]   # 已模板化，无 shell 注入面
    timeout_s: int
    parser: str             # 映射到 parsers.py 的函数名
    requires: tuple[str, ...] = ()   # 依赖的前置 step id（如 T1 需 T0 存活）
    when: str = ""          # 条件表达式（如 "category in {web,pentest}"）

@dataclass(frozen=True)
class ReconResult:
    step_id: str
    argv: tuple[str, ...]
    tool_version: str
    started_at: float
    duration_s: float
    exit_code: int | None   # None = 超时被杀
    artifact_path: str      # 原始 JSONL 落盘（供 evidence_id 回读）
    output_sha1: str
    facts_added: int
```

**七条约束**：

1. **旁路并发**：与首个 LLM 会话并行启动（同 `eager` 线程范式），T0 先出；不阻塞主循环。
2. **总预算**：`ADAPTER_RECON_BUDGET_S`（默认首场时间盒的 35%），逐 step 扣减；超预算停跑。
   **必须与 `ADAPTER_PER_CHALLENGE_SECONDS` 联动**（现有时间盒/终身预算联动的教训）。
3. **逐 step 超时**：`timeout` 命令或 `subprocess` 超时，超时按 `exit_code=None` 记为"负结果"。
4. **失败隔离**：任何异常吞掉，绝不阻断解题（同 Heimdall 红线 4）。
5. **原始产物落盘**：`<workdir>/.recon/<step_id>.jsonl`，只存结构化输出（**存明文**——
   与图不同，因为这是"原始证据"，且 workdir 已按题隔离；提交门仍走独立校验）。
6. **可复现**：记录 `tool --version` + argv + sha1；复访时可比对。
7. **不联网安装**：只用镜像内工具；缺工具走 T0/T1 降级（遵守 `taskprompt.py` 第 172 行现有铁律）。

**重要**：Runner **不替 LLM 做决策**。它把"基线覆盖"变成保证，但把
`nuclei -t custom` / `sqlmap` / `hydra` 这类**方向性动作**留给 LLM（T3）。
同时把整条 pipeline 暴露成一个**元工具**（`recon --tier 1 --target X`），
让 LLM 可主动补跑——这借鉴了 tinyctfer 的"元工具设计"与 CyberStrikeAI 的"可复用图工作流"。

---

## 5. Parser：工具 JSON → 事实（**负结果也是事实**）

### 5.1 正向事实

```python
# adapter/recon_parsers.py
def parse_nmap_greppable_xml(path, *, evidence_id) -> list[GraphDelta]: ...
def parse_httpx_jsonl(path, *, evidence_id) -> list[GraphDelta]: ...
def parse_nuclei_jsonl(path, *, evidence_id) -> list[GraphDelta]: ...
def parse_ffuf_json(path, *, evidence_id) -> list[GraphDelta]: ...
```

映射示例（nuclei）：`{template-id, info.severity, matched-at, matcher-name}` →
`upsert_node(vuln, origin=OBSERVED, attrs={template, severity})` +
`upsert_edge(VULNERABLE_TO endpoint→vuln)` + `EVIDENCED_BY(evidence_id=该 JSONL 行)`。

> 命令参数以落地时的 `--help` 为准——上面给的是范式，不是逐字可粘贴的最终值
> （如 `whatweb --log-json` 只接文件路径，不接 `-`）。

**无正则、无猜测**——这是相对 `blackboard.observe()` 的核心升级。

### 5.2 负结果 = 覆盖度（**这是本设计最容易被忽略、但价值极高的一环**）

Strix 的 `coverage` 与 Nettacker 的"漂移检测"都指向同一点：**"没发现"必须被记录，否则 Agent 会重复劳动或误以为没查过。**

| 负结果 | 事实 |
|---|---|
| nmap 扫了 1000 端口全关闭 | `coverage(host, ports=1000, open=0)` → brief 里显示"端口面已覆盖" |
| nuclei 跑了 N 个模板 0 命中 | `coverage(endpoint, templates=N, hits=0)` |
| ffuf 字典跑完无新增路径 | `coverage(endpoint, wordlist=common, new=0)` |
| T2 因超时被砍 | `coverage(step, status=timeout)` → brief 里标"未完成，可重跑" |

**作用**：① 止损的"干场"判定不再靠命令相似度，而看覆盖度是否有**实质新增**；
② brief 可以明确告诉 LLM"这些面已经查过且空"，避免重复；③ 给"未完成"的面一个重跑机会。

---

## 6. Brief：上下文工程（不是 dump）

`recon` 的产出**不直接进 prompt**。进 prompt 的是从图渲染的、**目标导向 + 有预算**的 brief：

```
<recon-brief session=2 budget=1200 category=web>
攻击面（观测，可直接打）:
  http://10.1.1.5:8080     nginx/1.24  php/8.1     [endpoint,obs]
  ├─ /admin               403 → 待绕过               [endpoint,obs]
  ├─ /api/v1/users       200 JSON                   [endpoint,obs]
  └─ /.git/HEAD          200 (源码泄漏!)            [endpoint,obs,high]
漏洞线索（模板命中，需人工确认）:
  CVE-2024-xxxx @ /api/v1   critical                [vuln,obs,ev:9f2a]
已覆盖且为空（别重复）:
  端口 1-1000 已扫，开放仅 8080；common.txt 已跑，无新增
未完成（可重跑）:
  nuclei cve 档超时（budget），如需要可再跑
</recon-brief>
```

规则沿用 Heimdall 的成熟做法：`_MAX` 预算、每类 TopN、超预算**先砍低价值段**、
**无祈使句、每条附依据、可推翻**（红线 1）。空图返回空串。

**"目标导向"**：brief 的排序按 `open_goals + frontier`——例如目标是 `foothold` 时
`/admin` 与 `CVE` 排前，`/robots.txt` 排后。这是与"无脑 dump 扫描结果"的本质区别。

---

## 7. Drift：复访只跑差分（把侦察变成跨场资产）

竞技场是多轮重访的，侦察应**复用**而非重跑：

| 场次 | 侦察策略 |
|---|---|
| 第 1 场 | T0 + T1（+ T2 视预算） |
| 第 2 场+ | **只跑 T0 存活探针 + 差分**：新端口/新路径/状态码变化 → 新 `OBSERVED` 事实 |
| 实例重建（epoch 变更） | 全量重跑（旧 OBSERVED 事实 `expired`，与图红线 3 一致） |

差分出的**新事实**正是最可靠的"进展"信号：它能安全地重置止损的干场窗口，
且比"命令不同"准得多。这直接替代 `stoploss.py` 现有的 `last_commands` 快照判据。

---

## 8. 与现有架构的接线

| 位置 | 改动 |
|---|---|
| `adapter/recon.py` 新增 | Plan 数据 + Runner（sidecar 线程）+ 预算 |
| `adapter/recon_parsers.py` 新增 | 每工具一个纯函数 → `list[GraphDelta]` |
| `adapter/graph_store.py` | 接收第 4 个 writer 的 delta；`coverage` 作为节点属性/边 |
| `orchestrator.py` | 会话开始前启动 runner；会话边界停止/汇总；epoch 变更时全量重跑 |
| `adapter/stoploss.py` | 干场判据从 `last_facts`/`last_commands` 换成"图实质新增"（含 coverage 差分） |
| `adapter/taskprompt.py` | 增 `recon_brief` 段（优先于 `actionable_assets`）；保留黑板作为兼容 |
| `observability.py` | `_PASSTHROUGH_KEYS` 加 `_recon_steps_done/_recon_facts/_recon_coverage`（仅计数） |
| `verify.py` | **不改**——侦察发现的 flag 候选仍走同一接地门（严防侦察成为绕过提交门的后门） |
| `Dockerfile` | 候选：加 `httpx`、`katana` 二进制；**先验证并烤入 nuclei 模板集** |

**一条红线**：侦察管线**不得**成为 flag 提交路径。它只能产出 `origin=OBSERVED` 的事实；
候选 flag 一律仍走 `_flag_grounded_in_transcripts` 的既有门。

---

## 9. 反模式与度量

### 9.1 反模式

| 反模式 | 后果 | 对治 |
|---|---|---|
| **recon theater**：跑一堆，Agent 不用 | 烧时间、污染上下文 | brief 必须目标导向 + 有预算；coverage 让"没用到的"可见 |
| **全量 nuclei** | 吃满时间盒 | T2 分 tags/severity 档；`-timeout`/`-rate-limit` 收口 |
| **把原始输出灌进 prompt** | 上下文爆炸 | 只进 brief；原始只落 `.recon/` 供 evidence 回读 |
| **固定 pipeline 僵化** | 漏掉题面暗示的非标端口/路径 | 元工具让 LLM 可主动补跑；T0 始终重跑 |
| **"0 命中"当结论** | Agent 误以为打完了 | 负结果 = coverage；brief 区分"已覆盖为空"与"未完成" |
| **侦察绕过提交门** | 幻觉 flag 提交流失 | 侦察与 `verify` 完全解耦（红线） |
| **假设确定性=可复现** | 无法调试 | 记 tool version + argv + sha1；"deterministic"仅指"无 LLM 在环" |

### 9.2 度量（**必须先做 A/B，别假设它一定好**）

领域内已有警示：`Baselines Before Architecture`（arXiv:2607.13085）发现同模型的
plain-agent 基线常能匹配甚至超过复杂 harness。所以本方案**必须对基线做对照**：

| 指标 | 基线（当前 Pi Agent） | 加确定性侦察 |
|---|---|---|
| 首场"有效事实"数 / 分钟 | | |
| 解题率（按 category 分层） | | |
| 每 flag token 成本 | | |
| 干场误判率（stoploass） | | |
| 重复命令率（同一命令跨场重复） | | |

若在 web/pentest 上明显提升、在 crypto/forensics 上持平（因为 T0/T1 很快），则成立；
若整体无提升，则应退回"把 pipeline 做成元工具让 LLM 调用"而不是自动跑。

---

## 10. 分阶段落地

| 阶段 | 做什么 | 开关 | 验收 |
|---|---|---|---|
| **P0 验证前提** | 验证 nuclei 模板集；`httpx`/`katana` 是否加镜像；跑通一条 web T0 | — | 模板数 > 0；T0 < 10s 出事实 |
| **P1 Plan+Runner** | `recon.py` 纯 Plan + sidecar Runner；原始产物落 `.recon/`；**不解析、不注入** | `ADAPTER_RECON=run` | 不影响解题；产物可复盘 |
| **P2 Parser→图** | `recon_parsers.py`；事实与 coverage 进图（影子模式） | `ADAPTER_RECON=parse` | 工具 JSON 映射零误读；图可重放 |
| **P3 Brief** | `recon_brief` 段进 prompt；止损换用图增量 | `ADAPTER_RECON=brief` | A/B 指标；干场判据差异全部可解释 |
| **P4 元工具** | 把 pipeline 暴露给 LLM 主动调用（tier 选择） | `ADAPTER_RECON=meta` | LLM 能补跑且不重复 |

回滚：每阶段独立 env 开关；P2 之前侦察产物完全旁路，随时可关。

---

## 11. 一句话总结

> **用户的判断是对的，且指向本仓库当前最大的浪费点：枚举不该由 LLM 即兴发挥。**
> 但落地要三处修正——**按题型分档**（PD 工具集里只有 nuclei/httpx/katana 对本环境有用）、
> **并发而非前置**（别让时间盒空转）、**产出是目标导向的 brief + 覆盖度**（不是更多事实）。
> 技术形态就是给图工程加一个**第 4 个 writer：确定性 Runner**，其产物精度高于现有正则抽取，
> 且天然可复现、可差分、可审计。

---

*本文件为设计产物，未改动任何代码。落地前置：先验证 nuclei 模板集（1.3）。*
