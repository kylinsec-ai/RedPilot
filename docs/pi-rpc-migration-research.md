# 研究：把求解引擎从 `pi --mode json --print` 换成 `pi --mode rpc`

> 结论先行：**RPC 能解决本仓库当前最硬的三个限制**（会话中注入、真实预算遥测、优雅中止），
> 但它不是改一个 flag，而是**求解传输层的一次重写**。注意切面：RPC 与 print 是**同一个
> Pi 引擎的两种传输**，不是两套引擎——因此引入 `ADAPTER_PI_TRANSPORT=print|rpc`，
> **不要**在 `ADAPTER_SOLVER` 下加并列后端（`factory.py` 现在写死"固定 Pi 引擎"）。
> 分阶段切换，**不要在 Phase 0 验证前删掉 print 通道**。
>
> **P0 尖刺已于 2026-09-17 实测通过**（pi 0.85.1 / provider `opencode-go` / model `kimi-k2.6`）：
> 工具自动执行 ✅、`agent_settled` 可达 ✅、`--no-session` 下 `get_session_stats` 可用 ✅、
> 会话中 `steer` 被接受 ✅、`abort` 后进程存活且可复用 ✅。详见 §5。
>
> 依据：pi 官方 `docs/rpc.md`、`docs/json.md`、`docs/extensions.md`（`ctx.mode`/`ctx.hasUI` 表）、
> `docs/usage.md`（旗标表）、`docs/security.md`，以及本仓库 `adapter/solver/pi_agent.py` 的实际实现。
> 本地 pi 版本：**0.85.1**。

---

## 1. 两种模式到底差在哪

| 维度 | 现状 `--mode json --print`（一次性） | `--mode rpc`（常驻） |
|---|---|---|
| 进程生命周期 | 起 → 跑完 → 退（prompt 作为**命令行参数**） | **常驻**；prompt 经 stdin 发 |
| 协议 | stdout JSONL 事件（单向） | stdin 命令 + stdout 事件/响应（**双向**） |
| 会话中干预 | **无**（只能杀进程树） | `steer` / `follow_up` / `clear_queue` |
| 中止 | SIGTERM→SIGKILL 整棵进程树 | `abort` / `abort_bash`（等 turn 收尾） |
| 预算遥测 | 只能从事件里攒 | `get_session_stats`：tokens/cost/**contextUsage** |
| 上下文压缩 | 只能靠 pi 自动 | `compact` / `set_auto_compaction`（驱动侧可控） |
| 重试 | 只有本仓库的 provider 失败护栏 | `set_auto_retry` / `abort_retry` |
| 终态信号 | `agent_end`（且本仓库必须**运行时打补丁**才靠得住） | **`agent_settled`**（retry/compaction/queue 全部落定） |
| 会话树 | `--no-session`，无树 | `new_session` / `fork` / `clone` / `switch_session` / `get_entries` / `get_tree` |
| 技能核验 | 无 | `get_commands`（可确认 75 个 `skill:` 已装载） |
| 扩展 UI | `ctx.hasUI=false` → 扩展跳过弹窗 | **`ctx.hasUI=true`** → 扩展弹窗会**阻塞等客户端回应** |
| 额外事件 | — | `agent_settled`、`queue_update`、`compaction_start/end`、`auto_retry_*`、`extension_error` |
| 分帧 | 逐行 | **严格 JSONL，仅认 `\n`**（官方明说 Node `readline` 不合规：它还会切 `U+2028/2029`） |

官方对 Node 用户的建议是用 SDK 的 `AgentSession`（进程内）；但它是 **TypeScript API**，
本仓库求解器是 Python，**唯一可行形态就是 RPC 子进程**。

---

## 2. RPC 能解决本仓库的什么（逐条对应到具体缺口）

| 本仓库当前的硬限制 | 证据 | RPC 解法 |
|---|---|---|
| **只能按场次注入上下文**：确定性侦察事实、Heimdall 图、eager 检测到的 flag 都要等**下一场**才进 prompt | `docs/deterministic-recon-design.md` §2 明确写"mid-session injection 在 `--print` 下做不到" | `steer`（当前 turn 工具执行完后、下一次 LLM 调用前送达）/ `follow_up` |
| **止损看不到真实烧钱**：`stoploss.py` 用会话数/时间/命令相似度，没有 token/cost/上下文占用 | `stoploss.py` 的 `_ChallengeState` | `get_session_stats` → `contextUsage.percent`、`cost`、`tokens` |
| **超时/卡死只能硬杀**：`_stop_process_tree` SIGTERM→SIGKILL，脱组子孙靠 token 扫 `/proc` 回收 | `pi_agent.py::_stop_process_tree` | `abort` + `abort_bash` 优雅收尾，`agent_settled` 确认空闲 |
| **终态判定靠补丁**：因为 `stopReason` 不可靠，运行时改 `subagent/index.ts` 去认 `agent_end` | `_patch_subagent_exit_bug()`（`pi_agent.py` 顶部 docstring） | `agent_settled` 是官方语义的"真正落定"，补丁必要性下降 |
| **上下文管理只能被动**：靠会话保活 `ADAPTER_MULTIFLAG_MAX_TURNS` | `adapter/config.py` | `compact` / `set_auto_compaction` 由驱动按预算主动压 |
| **重试策略在驱动侧重造**：`test_provider_failure_guard.py` | repo | `set_auto_retry` / `abort_retry` + `auto_retry_*` 事件 |
| **无法确认技能装载** | `_install_skills` 只看软链数 | `get_commands` 直接列出 `skill:*` |
| **多会话只能重开** | 竞技场每场新起进程 | `new_session` / `fork` / `clone`（从好的分支续） |
| **flag 候选出现时机只有工具事件**（eager 线程盯 FLAG 文件与工具输出） | `orchestrator.py::_eager_*` | 同左，但可叠加 `steer` 把候选立刻回灌 |

> 一句话：RPC 把"**一次性问答**"升级成"**可干预的会话**"，而这恰好是本仓库
> 竞技场/确定性侦察/图工程三个设计反复撞到的那堵墙。

---

## 3. 硬阻断与风险（必须先处理，否则会挂死或静默降级）

> **P0 后的状态**：R2（stats 可用，仅 `sessionFile=null`）、R6（工具自动执行）、R7（`agent_settled` 可达）
> **已实测排除**；R1（扩展 UI）处理逻辑已就位但未实战触发；**R3（生命周期重写）、R4（补丁重验）、
> R5（观测面过滤）仍是主体工作量**。

### R1（最高）扩展 UI 协议：RPC 下 `hasUI=true`，弹窗会阻塞

官方明确（`docs/extensions.md` §ctx.hasUI）：
> `true` in TUI and RPC modes. `false` in print mode (`-p`) and JSON mode.

而本仓库**在用**的官方 subagent 扩展里有：
```ts
// examples/extensions/subagent/index.ts:520
if ((agentScope === "project" || agentScope === "both")
    && confirmProjectAgents && ctx.hasUI
    && !ctx.isProjectTrusted()) { ... const ok = await ctx.ui.confirm(...) }
```
- **现状为何安全**：确认块只在"真的请求了 project-local agent"时才执行，而本仓库
  `_install_subagents()` 把角色装到 **`$HOME/.pi/agent/agents/`（user 级）**
  （`pi_agent.py:626` 起），所以 `projectAgentsRequested` 为空 → 不弹窗。print/json 下
  `hasUI=false` 更是直接跳过。
- **上 RPC 后**：`hasUI=true`，一旦有 project 级 agent（例如未来把角色放进
  `<workdir>/.pi/agent/agents`），`ctx.ui.confirm` 会发
  `{"type":"extension_ui_request","method":"confirm",...}` 并**阻塞等待** stdin 回应。
- **对策（必做）**：驱动实现 `extension_ui_response` 子协议；对 `confirm` 一律回
  `{"confirmed": false}`（无人值守场景下"拒绝"是安全默认），`select/input/editor` 回
  `{"cancelled": true}`，`notify/setStatus/...` 直接忽略。这是**几行代码的保险**，
  不做则一旦触发就是整场挂死（且与"静默烧题"同类，很难排查）。

### R2 `--no-session` 与"会话树类命令"可能互斥

RPC 命令里 `fork`/`clone`/`switch_session`/`get_entries`/`get_tree`/`export_html`
依赖会话树；本仓库用 `--no-session`（不落盘）。需要 Phase 0 实测：
- `get_session_stats` / `compact` 是否在 `--no-session` 下可用（它们作用于内存上下文，
  **预期可用**，但 `sessionFile` 可能为 null）；
- 若要用 `fork`/`clone`，就得放弃 `--no-session` 并接管 pi 的 session 文件与
  `transcript_path`（本仓库现在是**自建 transcript**，不是 pi 的 session 文件）。

### R3 生命周期重写（工作量主体）

现在的读循环是 `subprocess.Popen` + `select.select` + 非阻塞 `os.read` + 自己拼行
（`pi_agent.py:997` 起，注释已说明"必须用 `os.read` 非阻塞读，`readline` 会在部分行时永久阻塞"）。
RPC 要求：
- **读线程 + 写线程**并存，边写 `steer` 边抽干 stdout（否则管道背压死锁）；
- **严格按 `\n` 分帧**，**不能**用 `readline`（官方警告 `U+2028/2029` 会被误切）；
- 请求/响应关联（`id` 字段）——`prompt` 的 `success:true` 只代表"已接受"，
  **接受之后的失败走事件流，不会再回第二个 `response`**（官方原文）；
- 分帧缓冲要能容忍 `response`、`extension_ui_request` 与事件**交织**。

### R4 运行时补丁需要重新验证

本仓库有两处"改 pi 安装源码"的补丁：
- `adapter/pi_ext/patch_pi_bash.py`：改 `dist/core/tools/bash.js`（重复命令短路 +
  `tried_commands.md`）——**模式无关**，RPC 下仍在同一个 bash 工具路径生效，预期不破；
- `_patch_subagent_exit_bug()`：改 subagent 扩展的 `index.ts`，靠 `agent_end` 强杀子进程。
  它修的是**子 pi 进程**不退出的问题。上 RPC 后要确认：子 Agent 由扩展 spawn 时用的是
  哪种 mode（若是 print/json，则补丁照旧必要；若也是 rpc，则终态判定要换成 `agent_settled`）。

### R5 观测面污染

`relay` 吃的是 `_emit()` 归一化后的 `LIVE_EVENT_KINDS`，**不受** RPC 新事件影响；
但**转录文件**是直接写 pi 的原始事件（`transcript_f.write(slim)`）。RPC stdout 会混入
`type:"response"` 与 `type:"extension_ui_request"`，必须在这两处过滤，否则
`heimdall.transcript_digest()`（按 `message_end`/`tool_execution_*` 解析）会拿到脏行——
它虽有 `json.loads` try/except，不会崩，但**脏行会占用摘要预算**。

### R6 `--print` 去掉后工具是否仍自动执行

`--print`/`-p` 的官方定义是 "Print response and exit"（`usage.md:174`），**不是**工具批准开关；
非交互模式（`-p`/`--mode json`/`--mode rpc`）都不弹 trust 提示（`security.md:29`）。
预期 RPC 下工具照常自动执行，但**这是 Phase 0 必须实测的第一件事**（否则整条链路不成立）。

### R7 终态语义变化：`agent_end` ≠ 完成

`agent_end` 可能后接 retry / compaction / 队列续跑；**`agent_settled` 才是"不会再自动继续"**。
现引擎把 `agent_end` 当终点，切 RPC 后必须改判，否则会把 retry/队列当结束提前收场。

### R8 不建议走 SDK

`docs/sdk.md` 的 `createAgentSession` 是 TypeScript。Python 侧要进程内嵌入就得引入
Node sidecar/桥，复杂度高于 RPC，收益不明确。**保持 RPC 子进程**。

---

## 4. 迁移方案（与现有引擎并存，分阶段）

**先纠正一个定位**：RPC 与 print **是同一个引擎（Pi Agent）的两种传输**，不是第二套引擎。
`adapter/solver/factory.py` 现在写死 "固定使用 Pi Agent 作为解题引擎（`ADAPTER_SOLVER`
不再多选）"，仓库 README 也把"没有第二套编排"列为红线。所以**不要**新增
`RpcSolver(SolverBackend)` 作为并列后端——那会把"换传输"包装成"换引擎"，与既有设计冲突。

正确的切面是**传输层**：

```
adapter/solver/
├── pi_agent.py        ← 不动（print/json 传输，保持默认与回退）
├── pi_transport.py    ← 新增：PrintTransport / RpcTransport（同一 Pi 引擎的两种驱动）
├── pi_rpc.py          ← 新增：RPC 传输的进程/协议实现（读写线程、分帧、ui 回应）
├── normalize.py       ← 新增：pi 事件 → _emit(kind)/SolveResult 的共享归一化
├── base.py            ← SolverBackend 契约（SolveResult/extract_flags/extract_handoff）已够用
└── factory.py         ← 仍只返回 PiAgentBackend；在内部按 ADAPTER_PI_TRANSPORT 选传输
```

关键：**事件归一化与传输解耦**。把现在 `pi_agent.py` 里"pi 事件 → `_emit(kind)` /
`SolveResult`"的那段（`message_end`/`tool_execution_*`/`turn_end`/`agent_end` 分支）
抽成共享的 `normalize_event()`，两种传输都调它。这样：
- 转录格式、`on_fact` 回调、flag 提取、`INFRA_BLOCKED`/`TARGET_BROKEN` 判定**行为不变**；
- 只有"怎么起进程、怎么发 prompt、什么时候算完"这三件事不同。
- `factory.py` 的对外契约（`create_solver(...) -> SolverBackend`）与"固定 Pi 引擎"的
  设计陈述**都不破**。

| 阶段 | 做什么 | 开关 | 验收 |
|---|---|---|---|
| **P0 尖刺** | 手工跑通 RPC：`--skill` + `-e bash_guard.js` + `--no-session`；确认 ① 工具自动执行 ② `agent_settled` 会发 ③ `get_session_stats` 可用 ④ `steer` 送达时机 | 无（手工脚本） | 四条全绿；拿不到就不投入 |
| **P1 对等** | `pi_rpc.py` + `normalize.py` 实现"与 print 模式行为等价"的最小闭环（单 prompt、事件归一化、终态用 `agent_settled`、转录过滤 response/ui） | `ADAPTER_PI_TRANSPORT=rpc` | 同一题 A/B：SolveResult/flags/transcript 与 print 模式**逐字段可比** |
| **P2 开启收益** | ① `steer` 注入确定性侦察事实/Heimdall 图 ② `abort` 替代进程树硬杀 ③ `get_session_stats` 喂止损 | `ADAPTER_RPC_FEATURES=steer,abort,stats` | 会话中注入生效；干场/预算判据用真实 token；无挂死 |
| **P3 会话树** | 评估 `fork`/`clone` 用于跨场续接（需放弃 `--no-session`） | `ADAPTER_RPC_SESSION=1` | transcript 与 pi session 文件的一致性方案定稿 |
| **P4 收编** | 若 P1–P3 稳定，print 通道降级为回退或删除 | — | 全量回归通过 |

**回滚**：每阶段独立 env；P1 之前 `pi_agent.py` 一行不改，`ADAPTER_PI_TRANSPORT` 缺省仍是 `print`。

---

## 5. Phase 0 实测结果（2026-09-17，已执行，全部通过）

**环境**：pi **0.85.1**，provider `opencode-go`，model `kimi-k2.6`；
启动参数与现有 `_build_cmd` 对齐：`pi --mode rpc --no-session --skill <repo>/skills`。

**原始结果**（尖刺脚本实测输出，未编辑）：

```json
{
  "get_state": true,
  "get_state_model": "kimi-k2.6",
  "get_state_streaming": false,
  "get_commands_total": 76,
  "skill_commands": 75,
  "skill_sample": ["skill:azurehound-analysis",
                   "skill:beacon-object-file-development",
                   "skill:binary-ninja-mcp-analysis"],
  "stats_no_session_ok": true,
  "stats_sessionFile": null,
  "prompt_events": {
    "tool_start": 1, "tool_end": 1,
    "agent_end": 1, "agent_settled": 1, "text_end": 1,
    "ui_request": 0, "error": 0, "response_after_prompt": 1
  },
  "prompt_settled": true,
  "steer_started_stream": true,
  "steer_accepted": true,
  "abort_ok": true,
  "proc_alive_after_abort": true,
  "reusable_after_abort": true
}
```

**逐条判定**：

| 问题 | 结果 | 结论 |
|---|---|---|
| **R6** RPC 下工具是否自动执行 | `tool_start=1`、`tool_end=1` | ✅ bash 工具自动执行，**不需要** `--print` |
| **R7** `agent_settled` 是否可靠可达 | `agent_end=1`、`agent_settled=1` | ✅ 可作为终态判据（比现状的 `agent_end` 补丁更稳） |
| **R2** `--no-session` 下 stats 是否可用 | `success=true`，`sessionFile=null` | ✅ 可用；**`sessionFile` 确为 null**（符合预期：stats/compact 走内存，树类命令待 P3 单独验证） |
| **P2 前提** 会话中 `steer` | 在 `tool_execution_start` 后发，`success=true` | ✅ 流中途接受 steering |
| **P2 前提** 优雅 `abort` | `abort_ok=true`，进程存活，`reusable_after_abort=true` | ✅ 常驻语义成立，可复用 |
| **R1** 扩展 UI 请求 | `ui_request=0`（未触发） | ⚠️ 未被触发（与 §3-R1 的预测一致：无 project 级 agent）。**处理逻辑按构造实现但未被实战检验** |
| **附带** `--skill` 装载 | `get_commands` 报 **75 个 `skill:*`** | ✅ **上一轮 SpecterOps 替换在真实 pi 下确实装载成功**（独立交叉验证） |
| 协议卫生 | 无非 JSON 行、无 stderr 输出 | ✅ 严格 LF 分帧可行 |

**仍未验证（留给后续阶段）**：
- `compact` / `set_auto_compaction` 在 `--no-session` 下的行为；
- `fork` / `clone` / `get_entries` / `get_tree`（预期需放弃 `--no-session`）；
- 与 `-e bash_guard.js` 及官方 subagent 扩展**同时**装载时的交互（本轮未加 `-e`）；
- `extension_ui_request` 的实战回应（需构造 project 级 agent 触发）。

**复现方式**：起 `pi --mode rpc --no-session`，stdin 依次发
`get_state` → `get_commands` → `get_session_stats` → `prompt`（要求用 bash 跑一条命令）
→（流中途）`steer` → `abort`；stdout 严格按 `\n` 分帧解析，遇到
`type:"extension_ui_request"` 即回 `{"confirmed":false}` 或 `{"cancelled":true}`。

---

## 5b. 替换实施结果（2026-09-17，已落地）

### 落地方案：传输层而非第二套引擎

```
redpilot/worker/adapter/solver/
├── pi_transport.py   ← 新增：PrintTransport / RpcTransport + rpc_available() 探测
├── pi_agent.py       ← 改：_build_cmd(transport=) + solve() 接线（事件循环体未动）
└── ../../../tests/test_pi_rpc_transport.py  ← 新增：7 条回归
```

- 开关：`ADAPTER_PI_TRANSPORT`（**默认 `rpc`**；`print` 为回退）。
- `factory.py` / `create_solver()` 对外契约不变，"固定 Pi 引擎"的设计陈述不破。
- 事件处理分支（`tool_execution_*` / `message_update` / 终态 / `_emit` / flag 提取 /
  `INFRA_BLOCKED` / `TARGET_BROKEN`）**一行未改** —— 差异全被传输层吸收。
- 老版 pi（`--help` 无 `rpc`）自动降级 print（`rpc_available()` 每进程缓存一次探测）。

### 验证（全绿）

| 验证 | 结果 |
|---|---|
| 完整测试套件 | **374 passed / 0 failed**（含 7 条新 RPC 回归） |
| 真实 pi 端到端 `solve()` | `turns=1`、`completed`、10.1s、工具输出命中、**0 个残留 `pi --mode rpc` 进程** |
| `--skill` 装载（真实 pi） | `get_commands` 报 **75 个 `skill:*`**（交叉验证 SpecterOps 替换） |
| 扩展 UI 自动应答 | 假 RPC pi 仅在收到 `extension_ui_response{confirmed:false}` 后才推进 → 证明不挂死 |
| 转录洁净 | 转录里无 `response` / `extension_ui_request` 协议帧 |

### 实测发现的新失败模式（已加护栏）

**RPC 下「prompt 被接受但 agent 不启动」是完全静默的。** 实测复现：给
`--model deepseek/deepseek-v4-flash` 传一个本机不可解析的模型时，pi 只回
`{"type":"response","command":"prompt","success":true}`，之后**不发 `agent_start`、
不报错、也不退出**，静默直到会话 deadline。

对比：**print 模式**下同类错误会非零退出，被现有 0-turn + `junk_tail` 护栏接住；
RPC 常驻所以那条护栏**根本不触发**。这正是该仓库反复防的「静默烧题」类别，
而 RPC 让它变得更隐蔽。

**已加护栏**（`pi_agent.py` 空闲分支）：RPC 传输下，若 `prompt` 已接受但
`_events_seen == 0` 持续超过 `ADAPTER_RPC_STARTUP_GRACE`（默认 90s），
直接判 `error="rpc_no_agent_start"` 并终止，不再空等会话预算。

---

## 6. 与其它既有设计的关系

- **确定性侦察**（`docs/deterministic-recon-design.md`）：其 §2 的"并发而非前置"在 RPC 下
  才能真正兑现——sidecar 跑出的事实可以**当场 `steer` 进正在跑的会话**，而不是等下一场。
- **图工程**（`docs/graph-engineering-design.md`）：图的 `frontier`/`open_hypotheses`
  变化可以触发 `steer`，让"边界"实时进入主 Agent 视野。
- **止损**（`stoploss.py`）：信号源可从"命令相似度"升级为 `get_session_stats` 的真实
  token/上下文占用 + 图增量；骨架不用动（同图上一次的结论）。
- **provider 失败护栏**：RPC 的 `auto_retry_*` 事件可以让护栏从"重开进程"降级为"观察+续跑"。

---

## 7. 一句话总结

> 现状是**一次性问答**：prompt 进、事件出、进程退。RPC 把它变成**可干预的会话**——
> `steer` 解决"事实只能下一场注入"、`get_session_stats` 解决"止损看不见真实烧钱"、
> `agent_settled` 解决"终态判定靠打补丁"、`abort` 解决"卡死只能硬杀"。
> 代价是求解引擎重写（双向线程、严格分帧、扩展 UI 回应、终态语义变更），
> 且有两个必须实测的前置（工具是否自动执行、`--no-session` 下 stats/树命令是否可用）。
> **先在 `ADAPTER_SOLVER` 下并存，别删 print 通道。**

---

*本文件为调研产物，未改动任何代码。所有 pi 侧结论来自本地安装的
`@earendil-works/pi-coding-agent` 文档与其 `examples/extensions/subagent/index.ts` 源码。*
