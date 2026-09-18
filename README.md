# RedPilot

> 面向 **TSecBench** 靶场的自主安全测试 Agent：连上靶场 VPN，自主解题、提交 flag，
> 并把全过程实时推到观测平台。

RedPilot 是一个**单发行版、模块化单体**应用：一个**求解 worker**（竞技场编排 + Pi Agent 引擎）、
一个**统一服务端**（控制面 + 观测平台，同一个 FastAPI `:8000`）、共享一份**零依赖内核**。
四个模块同住一个包、同版本发布，用两个进程角色（server / worker）部署：

```
redpilot/contracts   共享内核：状态快照 schema、词汇表、redact/text/fsio、paths（零第三方依赖）
redpilot/control     控制面：challenges / 容器供给 / VPN / 判分
redpilot/obs         观测平台：摄取 / 读端 / SSE / SPA
redpilot/worker      求解 worker：竞技场编排 + Pi Agent 引擎 + 观测中继 + 本地态势台
redpilot/app.py      平台进程装配根（唯一同时 import control 与 obs 的地方）
```

**依赖方向是红线**：`contracts ←（control/obs/worker）`，worker ↛ control/obs，control ↮ obs ——
执行点是 [`tests/architecture/`](tests/architecture/)（AST 边界测试 + 运行时 import 足迹），
不再依赖发行版拆分。设计全文见
[`docs/modular-monolith-design.md`](docs/modular-monolith-design.md)。

---

## 架构一览

```
                          ┌──────────────────────── server (:8000) ────────────────────────┐
                          │  控制面 control：challenges / 容器供给 / VPN / 判分           │
   靶场平台 ◄── REST ──── │  观测平台 obs：摄取 / 读端 / SSE / SPA（独立 SQLite）          │
  (list/start/submit)     └───────────────▲──────────────────────────▲───────────────────┘
                                          │ telemetry(写端 token)     │ 读端 token（含明文 flag）
                          ┌───────────────┴──────────────────────────┴───────────────────┐
                          │  worker 舰队（共享 /work 卷）                                  │
                          │  worker-1  持 VPN(tun) + netns 提供者 + 他管；不做题           │
                          │  worker-2  ┐ network_mode: service:worker-1（复用隧道）        │
                          │  worker-3  ┘ 竞技场主循环 → Pi Agent → flag 双闸门 → 平台      │
                          └───────────────────────────────────────────────────────────────┘
                                                        │
                                              靶场 VPN ─┴─ 目标靶标
```

worker 的主循环是**竞技场**（`redpilot/worker/orchestrator.py`）：多轮时间盒、多会话重访、
多维止损、eager 即时提交、能力分片、跨场续接、舰队监督与协作式热重载。
求解引擎是 **Pi Agent**（`redpilot/worker/adapter/solver/`）。**没有第二套编排** ——
框架早期那套（`orchestration.solve_one` / assignment claim 链路）已退位删除。

---

## 起起来

```bash
cp .env.example .env      # 至少填 BENCHMARK_TOKEN / BENCHMARK_BASE_URL / LLM 凭据
docker compose up -d      # server + 三容器 worker 舰队
```

打开 `http://127.0.0.1:8000` 是服务端（观测 SPA + 控制面 API）。

### 舰队 vs 单体

舰队形态下 `worker-1` 只维持 VPN 与状态汇总（`ADAPTER_ROLE=monitor`，不做题），
`worker-2/3` 用 `network_mode: service:worker-1` 复用它的网络命名空间。

一个靶场 VPN 只应有一条隧道，三个容器各起一条会互相抢路由 —— 所以 VPN 由 worker-1 独占。
代价是它必须常驻（它一停另两个就断网，由 netns 看门狗兜底）。舰队拓扑的取舍与排障见
[`docs/worker.md`](docs/worker.md)。

**单体形态**（一个容器自己持 VPN 自己解题，方便对照与排障）点它的名字起：

```bash
docker compose up -d worker        # 只起这一个；另在 http://127.0.0.1:8080 起本地态势台
```

裸 `up -d` 不带它，是因为 `worker` 服务带了 profile。**别用 `--profile monolith up -d`** ——
那会把舰队三容器一起拉起来；compose 对显式点名的服务不看 profile 门槛，点名就够了。

### 两个地址别搞混

| 变量 | 指向 |
|---|---|
| `BENCHMARK_BASE_URL` | **靶场平台**（题目 list/start/submit 的 REST API）。本地自测可指向自带的 server |
| `OBSERVABILITY_URL` | **观测平台**（本仓库的 server）。compose 内自动为 `http://server:8000` |

---

## 求解引擎

### 传输：默认 RPC，可回退

Pi Agent 用两种传输之一驱动，开关 `ADAPTER_PI_TRANSPORT`：

| 值 | 形态 | 说明 |
|---|---|---|
| `rpc`（**默认**） | `pi --mode rpc`，常驻 JSONL，prompt 走 stdin | 靠 `agent_settled` 收尾、可会话中 `steer`、可优雅 `abort`、有真实 token/上下文遥测 |
| `print` | `pi --mode json --print`，一次性，prompt 在 argv | 旧行为，回退通道；老版 pi 无 `--mode rpc` 时**自动降级**到这里 |

两种传输对事件处理循环**完全同接口**（`adapter/solver/pi_transport.py`），差异被挡在传输层：
协议帧过滤、扩展弹窗自动应答、常驻进程收尾。设计依据与实测见
[`docs/pi-rpc-migration-research.md`](docs/pi-rpc-migration-research.md)。

> RPC 有一条专属护栏：`prompt` 被接受却迟迟没有 `agent_start`（模型/凭据解析不了时
> pi 会**静默**），超过 `ADAPTER_RPC_STARTUP_GRACE`（默认 90s）即判
> `rpc_no_agent_start`，不再空等整个会话预算。

### provider 与模型：都可配

| 变量 | 作用 | 缺省 |
|---|---|---|
| `ADAPTER_PROVIDER`（别名 `SOLVER_PROVIDER`） | 网关路由标签：决定 `--model` 前缀、逐题 `models.json` 的 `providers` 键、凭据 env 名 | `deepseek` |
| `SOLVER_MODEL` | 模型；裸名补上面的 provider，带 `<provider>/` 前缀则**前缀优先** | `mimo-v2.5` → `deepseek/mimo-v2.5` |

未登记的 provider 自动归为 `<PROVIDER>_API_KEY` + OpenAI 兼容 —— **换网关不用改仓库代码**。
完整 provider→凭据 env 对照表见
[`docs/worker.md`](docs/worker.md#provider-与模型都可配)。

---

## 技能面

`skills/` 下是 **103 个技能**（`<语义标识>/SKILL.md` + 同目录配套材料），来自
[`yaklang/hack-skills`](https://github.com/yaklang/hack-skills)（MIT）的内联副本。
溯源、同步点、上游布局约束与本仓库对它的两处依赖见
[`skills/PROVENANCE.md`](skills/PROVENANCE.md)。

**路由完全交给 Agent，框架不做预选。** 技能库自身分三层：总入口 `hack` →
六个分类入口（RED_TEAM / BLUE_TEAM / CODE_AUDIT / SOURCE_LEAK / EVIDENCE_REPORT /
TEST_MATRIX）→ 各深度题面技能。装载走 pi 的原生渐进式披露：
`redpilot/worker/adapter/solver/pi_agent.py` 的 `_install_skills` 把 `skills/` 逐目录
软链进**本题 HOME**（`$HOME/.pi/agent/skills/`），pi 的系统提示自带 `<available_skills>`
名录（只放名字 + 一句话描述 + 路径）；正文留在磁盘上，由 Agent 按题目分析自己 `read`。
框架侧唯一的技能输入是 `adapter/taskprompt.py` 里那段自主调用指引 —— 措辞刻意写成
「从清单里自己挑」而非「预选」。

> ⚠️ 这是一个**刻意撤掉的能力**，不是没做。框架曾按关键词预选 top-2 技能并把正文
> 注入 prompt；2026-09-16 技能库换成 yaklang/hack-skills（自带三层路由）时一并撤除，
> 因为「框架替 Agent 选技能」既与原生渐进披露重复，又在多域交织题上提前收窄了视野。
> 判定依据与不要退回的理由见
> [`docs/architecture/TARGET_ARCHITECTURE.md`](docs/architecture/TARGET_ARCHITECTURE.md) §1.4。

---

## 状态落在哪

两套状态、两个落点，都在 `/work`（宿主 `./work`）。**它们服务不同的消费方，都由
`observability.StatusBridge` 单向同步，字段名不同是刻意的**：

| 落点 | 内容 | 谁读 |
|---|---|---|
| `work/status/worker-<N>.json` | 编排层进度（`solving_active` / `sessions` / `flags_submitted` …） | `supervisor.py`、只读控制台 |
| `work/.live/<worker_id>.json` | 观测面快照（`phase` / `current_tool` / `turns` …） | relay → 服务端；本地 `:8080` 态势台 |

舰队形态下三个容器共享同一个 `/work`（`status/` 与 `.stoploss-locks/` 靠它协作）。

---

## 退出码契约

worker 容器与 compose 的 `restart: on-failure` 靠一组退出码配套（`0` 正常终止 / `86`
协作式热重载 / `4` VPN 层 / `3` 被孤立的解题 worker）。**完整表在
[`docs/worker.md`](docs/worker.md)** —— 那是唯一权威，这里不抄一份。

⚠️ 一条运维铁律：**配置错误绝不能非零退出**。`on-failure` 会重启一切非零退出，用 exit 1
表达「环境变量没填」= 无限重启闷循环，真因被日志淹没。这也是 `driver.py` 与
`entrypoint.sh` 在配置校验失败时都走 **exit 0** 的原因。

---

## 凭据

| 变量 | 用途 |
|---|---|
| `BENCHMARK_TOKEN` | 靶场平台凭据（worker 的 list/start/submit；monitor 也用它做 VPN 预检） |
| `BENCHMARK_BASE_URL` | 靶场平台地址 |
| `<PROVIDER>_API_KEY` | LLM 凭据，**pi 的官方 provider env 名**。默认 provider=deepseek → `DEEPSEEK_API_KEY` |
| `OBSERVABILITY_TOKEN` | 遥测**写**端（worker → 平台）。未配则摄取响亮 503，不静默丢弃 |
| `OBSERVABILITY_READ_TOKEN` | 观测**读**端（态势台 / runs 历史 / transcript） |

读写两个 token 是**刻意不对称**的：读端返回**明文 flag 与完整 agent 实录**，而 worker
只持写端。所以「能写遥测」≠「能读答案」—— 这也是 `OBSERVABILITY_READ_TOKEN`
**不转发给 worker 容器**的原因。两者皆未设置时读端 503。

---

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                      # 全仓 513
.venv/bin/python -m pytest -q tests/architecture   # 边界执行点 24
.venv/bin/python -m pytest -q tests/               # 架构 + 模块 + 竞技场回归
```

`pytest.ini` 已把 `tests/` 收进 `testpaths`，并把仓库根加进 `pythonpath`，
所以裸 `pytest` 就能收全部用例。其中：

| 目录 | 内容 |
|---|---|
| `tests/architecture/` | 24 条边界执行点：禁边、façade、运行时 import 足迹、数据所有权、单一来源，以及守卫自检 |
| `tests/contracts/` `tests/control/` `tests/obs/` `tests/worker/` `tests/app/` | 各模块单测（由原 `packages/*/tests/` 迁入） |
| `tests/*.py` | 99 条竞技场行为回归（止损、task epoch、eager 提交、交付账本、多段题推进、supervisor 判据） |

容器的 entrypoint 与 worker 装配层见 `entrypoint.sh` 与 `redpilot/worker/driver.py`。

---

## 离线自测靶场已移除

仓库里曾有两个**不进镜像、也不被任何服务启动**的离线资产，2026-09 一并删除：

| 已删 | 曾经是什么 | 为什么删 |
|---|---|---|
| `tsecbench/` | 本地靶场 API（题目/容器/提交/VPN 的完整挑战接口），对着它就能不连真靶场跑通 worker | 它是 `redpilot/control/` 的**逐字 fork**（`models.py` 269 行对 269 行，只差一行 docstring），两边持续分叉；且只被 `tests/` 与 `fastapi-console/` 引用 |
| `fastapi-console/` | 朋友的 Web 控制台（`agent.py:25` 硬编码 `tsecbench-worker-1/2/3`） | 它盯的容器名早已不存在（现为 `redpilot-worker-*`），启停目标落空；且它的观测面功能与 `redpilot/obs` 重复，却走另一条数据链（直读 `work/status/*.json` 而非观测库） |

连带删除的 4 个测试文件（90 条）与它们同命：

| 文件 | 条数 | 依赖 |
|---|---|---|
| `tests/test_solver_regressions.py` | 62 | `fastapi_console.agent`（**文件级 import**） |
| `tests/test_console_compliance.py` | 15 | `fastapi_console` + `tsecbench.errors` |
| `tests/test_challenges_api.py` | 8 | `tsecbench.api` |
| `tests/test_console_epoch_isolation.py` | 5 | `fastapi_console.agent` |

> ⚠️ 记一笔覆盖损失：`test_solver_regressions.py` 里**只有 7 条真的测那个控制台**
> （`test_console_priority_*` / `test_event_aggregation_*`），其余 55 条测的是竞技场
> 止损、task epoch、交付账本——它们是被第 29-30 行的文件级 import 连坐的。
> 需要时从 git 历史取回（最后一次存在于 `edfd569`）。

**代价**：失去"不连真靶场就能端到端自测 worker"的能力。以后 E2E 验证只对真靶场平台做。

---

## 文档地图

| 文件 | 内容 |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | **系统级总体架构**：目标与不变量、运行时拓扑、四模块、竞技场/求解/判分/记忆设计、数据与线格式、退化矩阵、演进路线 |
| [`docs/modular-monolith-design.md`](docs/modular-monolith-design.md) | **单发行版四模块（模块化单体）** 的架构设计、红线 R1–R8 与迁移记录 |
| [`docs/architecture/TARGET_ARCHITECTURE.md`](docs/architecture/TARGET_ARCHITECTURE.md) | **目标架构** —— 把《智能体工程最佳实践研究报告》的十二条结论逐条对齐到本仓（六处已做对 / 三处方向相反 / 一处缺失），六面架构与 ROI 排序的差距清单 |
| [`docs/architecture/AGENT_ARCHITECTURE_SELECTION.md`](docs/architecture/AGENT_ARCHITECTURE_SELECTION.md) | **架构选型** —— 把 35 个公开智能体架构按本仓任务形状逐个裁决（采纳 8 / 已具备 7 / 部分 6 / 拒绝 13 / 待度量 1）。TARGET 管「怎么修」，它管「抄哪个」 |
| [`docs/worker.md`](docs/worker.md) | worker 的跑法、结构、provider/模型配置、四条关键约定（状态落点 / 退出码 / 进程回收 / flag 双闸门） |
| [`.env.example`](.env.example) | 全部可调项，按段分组并标注「必须一起改」的联动项 |
| [`docs/pi-rpc-migration-research.md`](docs/pi-rpc-migration-research.md) | 从 `--print` 换到 RPC 的调研、实测与失败模式 |
| [`docs/graph-engineering-design.md`](docs/graph-engineering-design.md) | 证据图设计（把黑板 / Heimdall / 证据出身连成一张可追溯的图） |
| [`docs/deterministic-recon-design.md`](docs/deterministic-recon-design.md) | 确定性侦察与事实编译（先跑工具、再对事实做上下文工程） |
| [`docs/solver-isolation-design.md`](docs/solver-isolation-design.md) | 求解面隔离与预算执行（补 D3/D4/D5：Pi 降权、控制面状态搬家、会话内 surface 预算、work 保留）—— **M1–M4 已实施**，容器内真降权待探针 |
| [`docs/autonomous-offensive-agent-comparison.md`](docs/autonomous-offensive-agent-comparison.md) | 自主进攻性安全 Agent 架构谱系与本仓库对照 |
| [`docs/top10-offensive-agents-deep-dive.md`](docs/top10-offensive-agents-deep-dive.md) | 开源前十名 Agent 的源码级深潜 |
| [`docs/specterops-skills-evaluation.md`](docs/specterops-skills-evaluation.md) | skills 替换决策的评估与取舍。⚠️ **该评估选定的 SpecterOps/skills 未予采纳** —— 技能库最终是 yaklang/hack-skills，见下方「技能面」 |
