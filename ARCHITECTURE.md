# TSecBench 架构说明

> 面向本仓库的后续维护者与开发者。本文描述**当前工作区**的真实结构(2026-09 大规模清理后),不是设计稿。
> 文中 `文件:行号` 锚点会随改动漂移,以符号名称为准,行号仅作定位提示。

## 1. 系统是什么

TSecBench 由三部分组成:

1. **挑战评测平台(core,`tsecbench/`)** — 一个 FastAPI 服务,管理"认证制 challenge 任务生命周期":列出题目、启停题目容器实例、发放 hint(扣分)、接收 flag 提交判分;任务/题目/提交状态持久化到 SQLite。
2. **AI 求解舰队(worker 容器)** — 一组 Docker 容器,每个容器里是一个 `drivers/benchmark_driver.py` 进程 + 一个 Pi Agent CLI,自动解平台上的题目并提交 flag。
3. **管理控制台(`fastapi-console/`)** — 独立的多页 Web 应用,人工查看进度、启停舰队、派单、AI 猜 flag。

核心服务(core)与求解工作区是**分离的**:core 只负责出题与判分;解题的 Pi Agent 跑在 worker 容器里,通过 HTTP API 与 core 交互。

## 2. 部署拓扑(两种)

```
                      ┌──────────────────────────────────────────────┐
   [托管模式]          │  单机模式(fastapi-console 本地模式)            │
   ┌──────────────┐   │                                              │
   │ 远端平台       │   │  ┌──────────┐        ┌──────────────────┐   │
   │ BENCHMARK_   │──▶│  │ core     │◀──────│ fastapi-console    │   │
   │ BASE_URL     │HTTP│  │ :8000    │ HTTP  │ :8003             │   │
   └──────────────┘   │  │ /openapi/ │◀─────▶│ (同库直连 import)   │   │
                      │  └──────────┘  直接调 │                    │   │
                      │       ▲ 用同一 sqlite │ agent.py 起停舰队   │   │
                      │  ┌────┴────────────┐ └─────────┬──────────┘   │
                      │  │ docker-compose  │◀──────────┘ docker CLI    │
                      │  │ worker-1/2/3    │                          │
                      │  │ entrypoint.sh ──▶ benchmark_driver.py      │
                      │  └─────────────────┘    └──▶ adapter/ 组件库    │
                      │                              └──▶ pi 子进程      │
                      └──────────────────────────────────────────────┘
```

- **core(:8000)**:`python main.py` 启动(`main.py:5` 的 `app = create_app()`,uvicorn)。
- **console(:8003)**:`fastapi-console/` 独立 FastAPI 应用,**双模式**(`services.py` + `cfg.py:40`)——
  - **本地模式**:直接 `import tsecbench.*`,进程内共用一个 `Store`/`ChallengeService`/`VPNManager`,与 core **共享同一个 SQLite 文件**(默认 `data/tsecbench.sqlite3`),两套 HTTP 面指向同一状态;
  - **远端模式**:页面填 `BENCHMARK_BASE_URL` + token,console 通过 `_remote_request`(`services.py:29`)HTTP 转发到平台。
- **求解舰队**:`docker-compose.yaml` 编排,3 个 worker 分片(见 §6);console 的 `agent.py` 用 `docker compose` 起停舰队。
- **core 自身的控制台与代理**:`GET /` 返回内联 `{baseUrl, token}` 的静态页(`api.py:223-224`),`/benchmark{path}` 是同源反向代理,把浏览器流量转发到 `BENCHMARK_BASE_URL` 并注入 token(`api.py:231`),绕过远端 CORS。

**分层纪律**:core **零 import** `adapter/` 与 `fastapi-console`;层与层之间只通过 HTTP + 环境变量 + `work/` 目录文件约定衔接。

## 3. 平台层 `tsecbench/`(core)

### 3.1 REST 面(`/openapi/v1/*`,全部要求 header `BENCHMARK_TOKEN`,`api.py:128`)

| 路由 | 作用 |
|---|---|
| `GET /openapi/v1/challenges` | 列题 + 进度(`api.py:136`) |
| `POST /openapi/v1/challenges/start?unique_code=` | 启动容器,返回 `container_addr`(`api.py:144`) |
| `GET /openapi/v1/challenges/hint?unique_code=` | 取 hint(已通关题目拒绝,`service.py:115`) |
| `POST /openapi/v1/challenges/submit` | 提交 `{unique_code, flag}` 判分(`api.py:166`) |
| `POST /openapi/v1/challenges/close?unique_code=` | 关闭释放资源(`api.py:177`) |
| `GET/POST /openapi/v1/vpn/{status,config,start,stop}` | OpenVPN 客户端生命周期(`api.py:184-198`) |

错误统一为 `{code, message, detail}`;业务错误由 `tsecbench/errors.py` 工厂构造(`task_not_found:27`、`challenge_not_found:31`、`invalid_state:35`、`duplicate:39`、`resource_unavailable:43`、`internal_error:47`),`api.py:120-125` 有统一 exception handler。

### 3.2 Challenge 状态机与业务规则

```
stopped ──start──▶ pending ──供给完成──▶ available ──close──▶ stop_pending ──▶ stopped
                      │(进程崩溃时启动恢复:_recover_inflight_containers)
expired:惰性判定 —— 每次请求发现超过 expires_at 即写回(store.py:200-211),无后台定时器
```

- 状态迁移只由 `ChallengeService` 触发(`service.py:16`;`authenticate:36` / `start:74` / `hint:110` / `submit:121` / `close:156`),启动失败回滚到 `stopped`。
- 并发上限在 `Store.reserve_container` 内原子预留并计数(`store.py:282`,默认 3)。
- **flag 明文不落库**:存储与比对都用 SHA-256(`models.py:78 FlagDefinition`;提交时 `service.py:127-128` 对输入哈希后比对)。
- hint 按 `hint_cost_radio` 扣分,`discounted_score` 取整(`models.py`);每个 `flag_index` 只能提交成功一次(重复报 `duplicate`,幂等)。

### 3.3 存储与模块职责

**SQLite**(`tsecbench/store.py:37 Store`):单连接 + `RLock` + WAL,三表 `tasks` / `challenges`(静态定义 + 容器运行时状态叠在一行)/ `submissions`(`(task_token, unique_code, flag_index)` 复合主键,`store.py:18-28`);启动时遗留 `pending`/`stop_pending` 全部复位(`store.py:126`)。

| 模块 | 职责 |
|---|---|
| `tsecbench/api.py` | FastAPI 装配工厂 `create_app()`(:76);路由、Pydantic 请求/响应模型、异常 handler、静态控制台渲染与 `/benchmark` 代理 |
| `tsecbench/service.py` | 业务规则层 `ChallengeService`(:16),唯一推动状态机的地方 |
| `tsecbench/store.py` | SQLite 仓储,含惰性过期(`task_is_active`:200)与崩溃恢复(:126) |
| `tsecbench/models.py` | 纯校验/归一化:`FlagDefinition`:78、`ChallengeDefinition`:108、`TaskDefinition`:217;推导 flag 分数分配与地址别名 |
| `tsecbench/provisioner.py` | 容器供给:`static`(:36,认配置里的 `container_addr`,默认安全模式)/ `docker`(:53,`docker run -d --rm` + label + 动态 IP),`provisioner_for`:128 由 `TSECBENCH_PROVISIONER` 选择 |
| `tsecbench/vpn.py` | `VPNManager`(:29):本地 OpenVPN 守护进程管理(status:71 / save_config:82 / start:93 / stop:124),状态文件在 `<db目录>/vpn/` |
| `tsecbench/config.py` | `Settings.from_env`(:29) 读 `.env` + 环境变量;`load_tasks`:62 从 `TSECBENCH_CONFIG` 文件或 `TSECBENCH_TASKS_JSON` 读任务目录 |
| `tsecbench/errors.py` | 统一错误码工厂 |

注意:**provisioner 起的容器是"被攻击的题目实例",不是求解 agent 的沙箱**;解题 agent 跑在 docker-compose 的 worker 容器里。题目的 `container_addr` 通常需要 VPN 才能访问(见 `CHALLENGES_API.md`)。

## 4. 求解舰队:骨架与数据流

### 4.1 调用图(adapter/ 是组件库,driver 是唯一编排者)

```
drivers/benchmark_driver.py main() :706
├─ adapter/config.py            SolverConfig / ControllerConfig(纯 env 装配)
├─ adapter/platform_client.py   PlatformClient 兼容壳 :29(真实现已下沉到 platform/)
│    └─ adapter/platform/factory.py create_platform :29
│         └─ platform/base.py PlatformBackend :145 + 数据模型/异常族
│              实现(ADAPTER_PLATFORM 选择):
│              tsecbench_http.py:18   默认,GenericOpenAPIBackend 薄子类(仅补 VPN 地址与头)
│              generic_openapi.py:103 spec 驱动的通用后端(DEFAULT_SPEC 对齐 TSecBench REST)
│              tsecbench_sdk.py:32    官方 SDK,仅 ADAPTER_PLATFORM=tsecbench-sdk 且已安装
│    └─ RateLimitedClient platform_client.py:80 ──▶ throttle.py RateLimiter(请求节流)
├─ adapter/verify.py  Verifier :128(grounding + skeptic LLM 三重验证)
├─ adapter/llm.py      LLMClient :27(仅 skeptic 门使用)
├─ adapter/stoploss.py StopLoss :39(单例,多题止损状态表)
├─ adapter/scheduler.py run_fleet :20(线程池并发执行 visit 函数)
├─ adapter/observability.py 结构化事件日志(configure:24 / emit:40)
└─ adapter/solver/factory.py create_solver :20 ──▶ pi_agent.py PiAgentBackend :67
     solve() :93 = 子进程 `pi --mode json --print --no-session`,逐行解析 JSON 事件流
```

### 4.2 一次端到端求解(driver `main` :706 起)

1. **装配**:校验 `BENCHMARK_BASE_URL`/`BENCHMARK_TOKEN`(:707);读 `SolverConfig`/`ControllerConfig`;起 30s 心跳线程写 `/tmp/driver_heartbeat`(:728,compose healthcheck 依赖它)。
2. **VPN 前置**:`check_vpn` 不过即 exit 2(:770);另起 VPN 看门狗线程(`_start_vpn_watchdog`:320,连续 3 次失败 `os._exit(4)` 让容器重启重连)。
3. **列题与分片**:`client.list_challenges()` → 过滤已完成 → `_prioritize`(难度升序 + 分值降序,:111)→ `_worker_shard` 分片(:198,hostname 尾号或 `ADAPTER_WORKER_ID`)→ `_claim_priority` 认领控制台派单题(:288,派单题永不 dropped)。
4. **轮循环** `schedule_rounds` :589:轮次递增时间盒(基础难度 × round_factors);API 熔断(连续空转/401/402 → 暂停 300s,多次后退出);每轮 `run_fleet`(:674,并发 = min(配置, worker 模式 1))逐题 `solve_one` :357。
5. **单题会话循环** `solve_one` :357:
   - `stoploss.should_stop`(:105) 检查预算/会话数/连续干旱/不可达,`rearm_dry`(:163) 允许 stuck 题下轮复活;
   - `_start_with_retry` 启动容器(:137,派单题 30 次重试)→ 建工作目录 → `write_context_md`(`taskprompt.py:88`)→ `build_task`(`benchmark_driver.py:120`)→ 每 worker 一个持久 `Blackboard`(`_shared_board_for`:91)→ `stoploss.start`;
   - 每会话:**`build_task_prompt`**(`taskprompt.py:121`)按题面经 `SkillStore.match_skills`(`skill_loader.py:158`)渐进披露 skills、注入 board 已知事实与上一轮 `MEMORY.md`;
   - **`solver_backend.solve()`**(`pi_agent.py:93`):子进程 Pi Agent,逐行解析事件流;`tool_execution_end` 触发 `on_fact` 回调 → `board.observe`(`blackboard.py:163`,正则抽取 IP/服务/凭证/flag 事实);stall 看门狗与超时兜底;结束补读工作目录 FLAG 文件;
   - **验证链**:候选 flag → `flag_confidence`(`verify.py:66`,格式/字符集/低熵/grounding 置信度)→ `Verifier.verify`(:141,中置信走 LLM skeptic 多票,`_skeptic_check`:182)→ **verified 才 `submit_flag`**(driver :520)→ correct 记 `stoploss.record_flag` 并累计 `cumulative_score`;全对 = solved;
   - 收尾:写 `MEMORY.md`(下轮续接),finally `board.flush` + `_close_with_retry` 关容器(:176)。
6. **出口**:观测事件流(`obs.emit`)+ `_update_status`(:78)写 `work/status/worker-N.json` 供控制台展示;结束 `run_end`(:874)输出报告。

### 4.3 关键抽象

| 抽象 | 位置 | 契约 |
|---|---|---|
| `PlatformBackend` | `platform/base.py:145` | `list_challenges / start_challenge / get_hint / submit_flag / close_challenge` + `check_vpn / health_check`;数据类 `Challenge`(:22)、`StartResult`(:56)、`SubmitResult`(:63,含 `cumulative_score`/`duplicate`)、`HintResult`(:76)、`CloseResult`(:83);异常族 `TaskNotFound`/`ChallengeNotFound`/`InvalidState`/`DuplicateSubmit`/`ResourceUnavailable`(:110-130) |
| `SolverBackend` | `solver/base.py:94` | 唯一抽象方法 `solve(prompt, workdir, cfg, *, flag_format, on_fact, transcript_path, ...) → SolveResult`(:103);`SolveResult`(:58) 含 flags/tool_outputs/observed_output/turns 等;flag 工具 `extract_flags`:41、`touch_heartbeat`:85(写心跳文件) |
| `Blackboard` | `blackboard.py:63` | 每题事实板:`Fact`(:29),`observe`(:118)/`query`(:162)/`actionable_assets`(:168);5s 节流落盘 `_blackboard.json`,`flush`:102 兜底 |
| `StopLoss` | `stoploss.py:39` | `start`:64 / `record_*`:72-99 / `should_stop`:105(时间/会话/干旱/不可达四维)/ `remaining_seconds`:154 |
| `Verifier` | `verify.py:128` | grounding 逐字比对 + skeptic LLM(APPROVE/REJECT)多数票;平台 `correct` 字段才是最终对错 |
| `RateLimiter` | `throttle.py:13` | 线程安全最小间隔门控,`RateLimitedClient`(0.5s)与 `LLMClient` 复用 |
| `SkillStore` | `skill_loader.py:68` | frontmatter 只读头 + 惰性正文加载,`match_skills`:158 按 `_DOMAIN_SIGNALS` 加权匹配 |
| `run_fleet` | `scheduler.py:20` | 无状态线程池调度器,纯函数,无类 |

### 4.4 env 变量分组(driver/adapter 全部无 CLI,配置都走环境变量)

- **平台**:`BENCHMARK_BASE_URL`、`BENCHMARK_TOKEN`、`ADAPTER_PLATFORM`(后端选择)、`PLATFORM_SPEC_FILE`
- **求解器**:`SOLVER_PROVIDER/BASE_URL/API_KEY/MODEL`、`ADAPTER_SOLVER_MODEL`、`SOLVER_SESSION_SECONDS`、`PI_STALL_TIMEOUT`、`ADAPTER_SKILLS_DIR`
- **调度/止损**:`ADAPTER_MAX_CONCURRENCY/WORKER_CONCURRENCY`、`ADAPTER_BEST_OF`、`ADAPTER_PER_CHALLENGE_SECONDS`、`ADAPTER_MAX_SESSIONS`、`ADAPTER_TOTAL_SECONDS`、`ADAPTER_TIMEBOX_EASY/MEDIUM/HARD`、`ADAPTER_ROUND_FACTORS`、`ADAPTER_WORKER_COUNT/ID`、`ADAPTER_CHALLENGE_ONLY`、`ADAPTER_DRY_FACTS_CUTOFF`、`ADAPTER_API_PAUSE_SECONDS/LIMIT`、`ADAPTER_MAX_ACTIVE_RETRIES`
- **LLM(skeptic)**:`LLM_PROVIDER/BASE_URL/API_KEY/MODEL/TEMPERATURE/MAX_TOKENS/TIMEOUT/MIN_INTERVAL/THINKING/REASONING_EFFORT`、`SKEPTIC_VOTES`
- **运行**:`ADAPTER_WORKDIR`、`ADAPTER_FLAG_FORMAT`、`HOSTNAME`、`HOME`

## 5. 管理控制台 `fastapi-console/`

Jinja2 服务端渲染的多页应用(:8003,替代旧 Django/前端方案的新后端),**不 import adapter/**,与舰队通过 docker CLI + `work/` 文件交互。

- 页面:Dashboard(`main.py:85`)、舰队页(:92)、单题工作区(:99)、Settings(:107)、Admin(:114);所有 `/api/v1/*` JSON 路由经 `_api` 装饰器(:53)把 `APIError` 归一为 `{code, message, detail}`。
- `services.py`:挑战/VPN 业务 —— 远端模式 HTTP 转发(`_remote_request`:29);本地模式直接实例化 core 的 `Store`/`ChallengeService`/`VPNManager` 并 seed 题目。AI 盲猜流水线 `run_ai_auto`:123 / `run_ai_round`:253(LLM 猜 flag → 批量提交 → accepted/rejected 历史)。
- `solver.py`:`ask_llm`(:24,OpenAI 兼容 `/chat/completions`,urllib + 重试)+ `extract_flags`(:85)。
- `agent.py`:舰队控制 —— `fleet_start`(:266,`docker compose up -d`)/ `fleet_stop`:309 / `fleet_status`:176(读 `work/status/worker-N.json` + `_events.jsonl` 兜底解析,`_read_worker_status`:114 / `_aggregate_events`:124);**派单** `solve_one`(:355) 把 `code|worker_id` 追加到 `work/priority.txt`;配置读写 `.agent.env`(`load_agent_env`:45)。
- `session.py` / `cfg.py`:cookie 会话中间件 + JSON 落盘(`data/fastapi_sessions/`);会话级平台/LLM 配置。

## 6. 编排与目录约定

**docker-compose.yaml**(3 worker):一个容器 = 一个 Pi Agent,一次只解一道题(`docker-compose.yaml:3`);`x-worker-common`(:9) 定义镜像 `tsecbench-adapter`、healthcheck 检查 `/tmp/driver_heartbeat` 新鲜度(:19-20)、把 `./drivers`、`./adapter`、`./work` 挂载进容器(热更新,`:25`);**worker-1 是唯一 VPN 持有者**(NET_ADMIN + tun,:56-58),worker-2/3 `network_mode: service:worker-1` 共享其网络栈(:71-87)。`entrypoint.sh` 内部连 OpenVPN 后 `exec python3 /app/drivers/benchmark_driver.py`(entrypoint.sh:80)。

**`work/` 目录是 worker 与宿主机控制台之间的唯一信使**:

| 文件/路径 | 写者 | 读者 |
|---|---|---|
| `work/priority.txt`(`code|worker_id` 行) | console `agent.py` 派单 | driver `_load_priority`/`_claim_priority`(benchmark_driver.py:244/288) |
| `work/status/worker-N.json` | driver `_update_status`(benchmark_driver.py:78) | console `agent.py:114` |
| `work/_events.jsonl` | driver(obs.configure :737) | console `agent.py:124` 聚合展示 |
| `/tmp/driver_heartbeat`(容器内) | driver 心跳线程 :728 + pi 每行事件 `touch_heartbeat` | compose healthcheck |
| `<workdir>/_blackboard.json`、`MEMORY.md` | driver / board | 下轮 session 续接 |

**数据目录**:`skills/`(7 类 SKILL.md,喂 `skill_loader`)、`.env` / `.agent.env`(不入库)。求解 agent 的运行规则由 driver 每轮写入任务工作目录的 `CLAUDE.md`(`taskprompt.py` 的 `_CLAUDE_MD` / `write_context_md`)承载,仓库根不再放 AGENTS.md。

## 7. 测试布局

| 层 | 位置 | 手段 | 运行 |
|---|---|---|---|
| core 单元 | `tests/test_challenges_api.py` | `TestClient` + 临时 sqlite,覆盖鉴权/列题/起停/hint 扣分/submit 计分/多 flag/过期 | `python -m pytest`(pytest.ini `testpaths = tests`) |
| 端到端 | `e2e/` | **真服务器真浏览器**(Playwright):conftest 起 `main.py` 子进程 + 临时 DB + 静态供给的 3 道种子题,测 `tsecbench/static` 前端 | `python -m pytest e2e`(需 playwright chromium) |
现状:**adapter/、drivers/、fastapi-console/ 均无自动化测试**(此前唯一的 local-eval 冒烟已随清理删除)。

## 8. 已知遗留与冗余(待办)

- 两套 Web 前端并存:`tsecbench/static/`(core 挂载的 field console,e2e 测它)与 `fastapi-console/templates/`(管理控制台)——各自的定位与服务对象不同,保留。
- **`adapter/platform_client.py` 是纯兼容壳**:docstring 自述旧版客户端已重构为 `adapter/platform/`,保留原类名只为 `benchmark_driver` 兼容;新代码应直接走 `create_platform`。
- fastapi-console 的 AI 盲猜流水线(`solver.py`+`run_ai_round`)与 adapter/ 的 Pi Agent 求解是两条并行路径(前者轻量、后者重武器),console 未复用 adapter——原因是 Pi Agent 无法在浏览器侧运行。
- console 本地模式下 core(:8000)与 console(:8003)同时监听、**共用同一个 sqlite 文件**,需注意双写进程风险(均有锁,但属隐式耦合)。
