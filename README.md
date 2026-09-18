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
.venv/bin/python -m pytest -q                      # 全仓 324（passed；另有 2 skipped）
.venv/bin/python -m pytest -q tests/architecture   # 边界执行点 25
.venv/bin/python -m pytest -q tests/               # 架构 + 模块 + 竞技场回归
```

`pytest.ini` 已把 `tests/` 收进 `testpaths`，并把仓库根加进 `pythonpath`，
所以裸 `pytest` 就能收全部用例。其中：

| 目录 | 内容 |
|---|---|
| `tests/architecture/` | 25 条边界执行点：禁边、façade、运行时 import 足迹、数据所有权、单一来源，以及守卫自检 |
| `tests/contracts/` `tests/control/` `tests/obs/` `tests/worker/` `tests/app/` | 各模块单测（由原 `packages/*/tests/` 迁入） |
| `tests/*.py` | 89 条竞技场行为回归（止损、task epoch、eager 提交、交付账本、多段题推进、supervisor 判据） |

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

## 评估与轨迹面已移除

2026-09 死代码清扫删除了评估与轨迹面（`redpilot/eval/` 与判据桥 `adapter/eval_bridge.py`），
共 1637 行 Python（另 264 行数据集 JSON）。判据是**生产消费者为零**：`app.py`、`main.py`、compose、两个 Dockerfile 与
`entrypoint.sh` 均不引用它，`redpilot/` 内也没有任何文件 import `redpilot.eval`。

| 已删 | 曾经是什么 | 为什么删 |
|---|---|---|
| `redpilot/eval/` | 评估与轨迹面：回放(`replay`) / 任务卡(`dataset`，含 `datasets/skill_routing.json` 20 张卡) / 判据(`graders`) / 报告(`report`，pass^k、触发率、成本) / 落库(`store`) | 生产侧零消费者。它是 `docs/architecture/TARGET_ARCHITECTURE.md` 的 P3「首切片」，**已实现但从未接线**到任何运行时入口 —— 只有测试在跑它 |
| `redpilot/worker/adapter/eval_bridge.py` | 判据桥：把 `taskprompt._OFFLINE_CONSTRAINT` 点名的行为编译成 7 条 `EGRESS_RULES` 正则，按 `Predicates` 协议注入，以免 `redpilot.eval` 反向 import `redpilot.worker` | 它的存在理由就是给评估面供判据；评估面既去，桥随之失去消费方。**注意**：被编译的那条约束本身仍在 `taskprompt.py`，且仍在注入 prompt |

连带删除的 6 个测试文件（152 条）：

| 文件 | 条数 | 依赖 |
|---|---|---|
| `tests/eval/test_dataset.py` | 54 | `redpilot.eval.dataset` |
| `tests/eval/test_graders.py` | 30 | `redpilot.eval.graders` |
| `tests/worker/test_eval_bridge.py` | 22 | `adapter/eval_bridge` |
| `tests/eval/test_replay.py` | 20 | `redpilot.eval.replay` |
| `tests/eval/test_report.py` | 16 | `redpilot.eval.report` |
| `tests/test_eval_end_to_end.py` | 10 | 两包跨接：评估面 + 判据桥 |

> ⚠️ 记一笔覆盖损失：这 152 条与被删的两个离线外围资产那次不同 —— 那回 90 条里
> 有 55 条是被文件级 import **连坐**的（其实在测竞技场）。这次 6 个文件**没有一条连坐**，
> 全部直接测被删的面本身。
> 但 `test_eval_end_to_end.py` 值得单独记一笔：它的 docstring 写明自己回答的是
> "判据注入这条路**走得通吗**？还是说『注入协议』只是个说法、真接的时候接不上？"
> —— 删掉它，等于把"这套注入设计至少被验证过一次"这一点也一并删掉了。
> 需要时从 git 取回（最后存在于 `a3ea29c`）。

**代价**：失去"把轨迹离线跑判据、量化每次改动"的能力。`TARGET_ARCHITECTURE` 的 P3
退回未落地，其缺口清单第 1 项（"无评估面，一切改动无法量化"）复现。

---

## 死码清扫（2026-09）

判据只有一条：**零消费者** —— 生产路径不 import、不调用、不构造。分批记：

| 已删 | 曾经是什么 | 判据 |
|---|---|---|
| `worker/solver/base.py` 的 `SolveResult` | 框架侧结果模型，带一个 `provider_failure` 判据（0-turn + 报错 = 引擎没跑起来） | 没有任何生产代码构造它；唯一 import 方是它自己的 3 条测试。判据本身**没丢** —— 执行点在编排侧（`orchestrator._is_api_fault` 与 stoploss），原委与该保留的教训写在 `solver/base.py` 的模块 docstring |
| `adapter/solver/base.py` 的 `CCResult` | `SolveResult` 的"兼容旧名"别名 | 全仓 3 处引用全是定义与再导出，零消费者 |
| `adapter/progress.py`（整模块，159 行） | 成功侧记忆的退役残骸（`ChallengeProgress` / `extract_progress_from_result`） | 唯一引用是 `orchestrator` 那行 import，两个名字从未被使用。**注意**：删的是残骸，"试过并放弃了成功侧记忆"这条历史教训仍然成立（见选型文档 §3.4） |
| `contracts/paths.py` 的 `harness_dir()` / `harness_subdir()` | `.harness/` 路径拼装 helper | 真正的消费方都 import 常量 `HARNESS_DIR` 自己 join |
| `worker/adapter/taskprompt.py` 的 `write_memory()` | 框架侧 MEMORY.md 写入口 | 被 import 却从未被调用；MEMORY.md 实际由 agent 自己写、由 `_merge_memory` 合并。**框架侧那条写入路径从来没接上** |
| `ControllerConfig.round_timeboxes` + `ADAPTER_ROUND_TIMEBOXES` | "各轮单题访问时长"旋钮 | 零读者。真正的算法是 `timebox_for_difficulty(难度) × round_factors[轮次]`；两者默认值还对不上（旧 `[480,820,1500,2000]` vs 新 `3600×[1.0,1.7,3.0,4.0]`） |
| `adapter/verify.py` 的 `_python_mutated_artifacts` → `_python_noncopy_write_paths` → `_python_open_mode` | 一条 136 行的互调链 | **链根无人调用**。⚠️ 三者不连续（中间夹着两个**有**活调用点的函数），按行号一刀切会连坐删错的 |
| `adapter/verify.py` 的 `normalize_flag_body` | flag 归一化工具 | 零调用点，且语义是**已被推翻的那个**（统一小写 —— 会让错误的小写提交把正确的大写答案拉黑）。`adapter/hallucination.py` 的注释曾以「与它同口径」为据，等于把有害旧口径当标准引用，已改为自述规则 |
| `control/` 的 `stop_task` 三层链 + `active_container_count` / `delete_challenge` / `task_tokens` / `create_task` / `internal_error` | 控制面零散方法 | 链的**入口**零调用点（连测试都没有），其余各只有自己的定义。`delete_challenge` 是可级联删除提交记录的能力 —— 删它等于撤掉一个本就无人可达的运维入口，需要时从 git 取回 |
| `obs/control_proxy.py`（整模块 71 行） | 同源控制面代理 | **两个独立死因**：① 打开它的 `OBS_CONTROL_URL` 不在任何部署文件里，恒为关闭；② 即便打开也转发到 `/api/v1/*`，而那条前缀已随 `fb96614` 拆除（存活的只有 `/openapi/v1/*`）—— 按构造不可能工作 |
| `flags_banked` / `_FLAGS_WITH_ARG` / `LIVE_STATE_FMT` / `_MIGRATION_4_REMOVED` / `platform_mode` / `strip_provider` / `MAX_CONCURRENT`×2 | 零散符号 | 各自全仓只有自己的定义。注：删 `ControllerConfig.platform_mode` **字段**但保留 `ADAPTER_PLATFORM` **环境变量** —— 后者才是活入口 |
| 前端 `ctl` / `ADMIN_HEADER` / `adminToken` / `adminAuth` / `setAdminToken` | 控制面 `/api/v1/*` 的 axios 实例与凭据存储 | **上次拆除的漏网**：`fb96614` 删了整页控制台与全部 `/api/v1/*` 路由，却把服务它的底层管线留在了原地 |
| 28 处未使用 import 中的 10 处 | — | pyflakes；余下 18 处是 `platform_client` / `obs.schema` 的**有意再导出**，已核实保留 |

**提示面去重**：`_ISOLATION_CONSTRAINT`（1834 字符）曾每次会话被**逐字注入两遍**
（逐题 `CLAUDE.md` 与 prompt 各一份）。现只走 `CLAUDE.md` —— 它是 pi 的项目指令文件，
Agent 可随时重读，且**子 Agent 进程也会加载它**，而父会话的 prompt 不会传给子 Agent。
守卫在 [`tests/worker/test_prompt_single_source.py`](tests/worker/test_prompt_single_source.py)
（已实测：把重复加回去会变红）。

**改名残留**：`ghost → redpilot` 的前端面与若干 docstring 漏网已收尾 —— 侧边栏 HTML 里
渲染的字面量 `Ghost`、`sessionStorage` 键 `ghost.*`、以及多处指向 `ghost_worker/…`、
`packages/ghost/ghost/…` 等不存在路径的注释。`web/` 是**提交物**，故随源码重建。
（`skills/ghost-bits-cast-attack` 是真实攻击技术，与改名无关。）

> **未做**：`SolverConfig` 有四个**写而不读**的字段（`subagent_model` / `effort_level` /
> `auto_compact_window` / `api_timeout_ms`），其中三个在 `-1m` preset 里被显式赋值。
> 这不是死代码而是**没接线的意图**（1M 上下文模型本该给大压缩窗口与长超时），
> 需要先决定"接进 pi"还是"删掉 preset"，故留给 P1 上下文面一并处理。
> 详见 `TARGET_ARCHITECTURE.md` §2.1。

**覆盖损失记账**：第一轮删 `provider_failure` 三条用例（-3）、新增提示面单源守卫三条（+3）；
第二轮删 R1 守卫的重复实现一条（-1）—— 它有不变量相同的另一份实现仍在跑（实测两向都验过）。
合计 **324 passed / 2 skipped**。

**四件"看着该删、复核后留下"的事**（记录在案，免得下一轮再查一遍）：

| 对象 | 为什么留 |
|---|---|
| `adapter/heimdall.py`（515 行，默认关、**零测试**） | 它端到端接着（gate / init / produce / consume 四处齐全，`heimdall_map=` 注入 prompt）。默认关是**设计**（"新链路先默认不参与，观察够了再打开"）。真正的缺口是**零测试**，不是死码 |
| `contracts/vocabulary.py` 的 10 个事件类型常量 | 不是死码，是**未接线的单一来源** —— `digest.py` 用字面量比较同样的 kind。删声明会让字面量成为唯一来源，与单源原则**反向**；正解是把 `digest.py` 接上去（一次跨 4 文件的改动） |
| `tests/control/test_smoke.py`（断言重言） | 它是**唯一**以 env 默认路径构造 `create_app()` 的用例（其余全传显式 settings）。断言重言，但那个**调用**就是覆盖 |
| 6 个证据溯源测试文件（~1200 行） | "闸门已撤、断言不再检查所宣称属性"**不成立**：`claim.verified` 仍被提交路径读（`orchestrator.py:4298/4308/5498`），`claim.provenance` 仍被 grounding 判据读（`:1808`） |

另有一条**应做而未做**：`contracts/vocabulary.py` 的 10 个常量与 `digest.py`
的字面量应当收敛成一处（单源原则）。它是重构不是删除，故留待专门的改动。

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
