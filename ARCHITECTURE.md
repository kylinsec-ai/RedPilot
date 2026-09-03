# TSecBench 架构说明(极简 MVP)

> 面向仓库维护者。描述**当前工作区**的真实结构(2026-09 极简瘦身后),不是设计稿。
> `文件:行号` 锚点会随改动漂移,以符号名称为准。

## 1. 系统是什么

两条最小路径:

1. **平台 core(`tsecbench/` + `main.py`)** — FastAPI 服务,管理"认证制 challenge 任务生命周期":列题、启停题目实例、发放 hint(扣分)、接收 flag 提交判分;SQLite 持久化。**纯 REST,无 UI**。
2. **单个求解 worker(容器)** — 常驻 Pi Agent worker(VPN 持有者),一次开一道题、串行刷完全部题;通过 HTTP 访问 core。

core 与 worker **分离**:core 只出题判分;解题 agent(Pi Agent CLI)跑在 worker 容器里。
已删除(历轮瘦身):fastapi-console、skills/ 渐进披露、多 worker 舰队/分片/派单、轮次/止损/黑板记忆/skeptic LLM 验证、网页前端与 /benchmark 代理、自动化测试。

## 2. 部署拓扑

```
   ┌──────────────────────────────────────────────────────────┐
   │ 宿主机                                                    │
   │  ┌──────────────────────┐    docker-compose(单服务)        │
   │  │ core :8000           │◀────┌────────────────────────┐  │
   │  │ python main.py       │ HTTP │ worker 容器             │  │
   │  │ tsecbench/ + SQLite  │      │ entrypoint.sh ── OpenVPN│  │
   │  │ 静态/题目实例(可选 docker) │      │   └─ benchmark_driver.py │  │
   │  └──────────────────────┘      │        └─ adapter/ + pi   │  │
   │                                └────────────────────────┘  │
   └──────────────────────────────────────────────────────────┘
```

- worker 经 `BENCHMARK_BASE_URL` 访问 core;所有题目入口地址须经 VPN 才能访问(entrypoint.sh 起 OpenVPN,worker 持 tun)。
- 层间纪律:core **零 import** `adapter/`;只通过 HTTP + 环境变量 + `work/` 目录衔接。

## 3. 平台层 `tsecbench/`(core)

### 3.1 REST 面(`/openapi/v1/*`,全部要求 header `BENCHMARK_TOKEN`,`api.py` `authenticated_token`)

| 路由 | 作用 |
|---|---|
| `GET /openapi/v1/challenges` | 列题 + 进度(`list_challenges`) |
| `POST /openapi/v1/challenges/start?unique_code=` | 启动实例,返回 `container_addr` |
| `GET /openapi/v1/challenges/hint?unique_code=` | 取 hint(已通关拒绝) |
| `POST /openapi/v1/challenges/submit` | 提交 `{unique_code, flag}` 判分 |
| `POST /openapi/v1/challenges/close?unique_code=` | 关闭释放资源 |
| `GET/POST /openapi/v1/vpn/{status,config,start,stop}` | OpenVPN 客户端生命周期 |

错误统一 `{code, message, detail}`;业务错误由 `tsecbench/errors.py` 工厂构造,`api.py` 有统一 exception handler。完整契约见 `CHALLENGES_API.md`。

### 3.2 状态机与业务规则

```
stopped ──start──▶ pending ──供给完成──▶ available ──close──▶ stop_pending ──▶ stopped
expired: 惰性判定(每次请求发现超期即写回,store.py),无后台定时器
```

- 状态迁移只在 `ChallengeService`(service.py);启动失败回滚 `stopped`;崩溃恢复复位遗留 pending。
- 并发上限 `Store.reserve_container` 原子预留(默认 3)。
- **flag 明文不落库**:SHA-256 存储与比对(models.py `FlagDefinition`);同一 flag_index 只能成功一次(重复报 `duplicate`,幂等)。
- hint 按 `hint_cost_radio` 扣分,`discounted_score` 取整。

### 3.3 模块

| 模块 | 职责 |
|---|---|
| `tsecbench/api.py` | `create_app()` 工厂:路由 + Pydantic 模型 + 异常 handler(纯 REST,无静态挂载/代理) |
| `tsecbench/service.py` | 业务规则层 `ChallengeService`,唯一推动状态机的地方 |
| `tsecbench/store.py` | SQLite 仓储(单连接 + RLock + WAL),惰性过期与崩溃恢复 |
| `tsecbench/models.py` | 纯校验/归一化:Flag/Challenge/TaskDefinition;分数分配与地址别名 |
| `tsecbench/provisioner.py` | 供给:static(认 `container_addr`)/ docker(`docker run -d --rm` + label),`provisioner_for` 由 `TSECBENCH_PROVISIONER` 选择 |
| `tsecbench/vpn.py` | `VPNManager` 本地 OpenVPN 守护进程管理 |
| `tsecbench/config.py` | `Settings.from_env` 读 `.env` + 环境变量;`load_tasks` 从 `TSECBENCH_CONFIG`/`TSECBENCH_TASKS_JSON` 读任务 |
| `tsecbench/errors.py` | 统一错误码工厂 |

## 4. 求解 worker

### 4.1 调用图(adapter/ 组件库,driver 唯一编排者)

```
drivers/benchmark_driver.py  main()
├─ adapter/config.py            SolverConfig(纯 env 装配)
├─ adapter/platform/factory.py create_platform() 直构
│    └─ tsecbench_http.py: TSecBenchHTTPBackend(GenericOpenAPIBackend 薄子类)
│         └─ generic_openapi.py: spec 驱动引擎(默认 spec 对齐 TSecBench REST)
├─ adapter/taskprompt.py        build_task_prompt(task, flags_submitted) / write_context_md
├─ adapter/task.py              AgentTask(任务描述数据类)
└─ adapter/solver/factory.py    create_solver() ──▶ pi_agent.py PiAgentBackend
     solve() = 子进程 `pi --mode json --print --no-session`,逐行解析 JSON 事件流
     (内含 stall 看门狗/会话超时/FLAG 文件补读/touch_heartbeat)
```

### 4.2 主循环(一次端到端求解)

1. 装配:校验 `BENCHMARK_BASE_URL/TOKEN`;读 `SolverConfig`;起 30s 心跳线程写 `/tmp/driver_heartbeat`(compose healthcheck 依据)。
2. `while True`:`list_challenges()` → 平台 `is_completed` 为完成唯一权威 → 未完成题 `_prioritize`(难度升序+分值降序)→ 逐题 `solve_one`;无题 sleep 60 再列(新题自动纳入)。列表失败 exit 3 由 restart 拉起;平台 409(任务结束)sleep 后重试。
3. `solve_one`: `_start_with_retry`(槽位竞争退避,≤8 次)→ 工作目录 `<WORKDIR>/<_safe_code>` + `write_context_md` → `build_task` → **单会话** `build_task_prompt`(flags_submitted=平台 correct_flag_count)→ `solver_backend.solve(...)`(无 on_fact/transcript)→ 候选 flag 去重后**逐个直提 `submit_flag`**(correct/duplicate 即闸门,全对 = solved)→ finally `_close_with_retry` 关实例。
4. 多 flag 剩题:当轮未全对 → 关题留平台,下一轮冷启动再试(无记忆;重启后重复提交由平台幂等兜底)。

### 4.3 env(worker 侧,全部无 CLI)

`BENCHMARK_TOKEN`、`BENCHMARK_BASE_URL`(平台,必填)、`SOLVER_PROVIDER/API_KEY/MODEL/SESSION_SECONDS`、`ADAPTER_WORKDIR`、`ADAPTER_FLAG_FORMAT`、`ADAPTER_VPN_CONFIG`(entrypoint)、`PI_STALL_TIMEOUT`(pi 内)、`WATCHDOG_MAX_IDLE_SECONDS`(compose 插值)。

## 5. 编排与目录约定

- **docker-compose.yaml**:单 `worker` 服务 —— 镜像 `tsecbench-adapter`、healthcheck 看 `/tmp/driver_heartbeat` 新鲜度、`cap_add NET_ADMIN,NET_RAW` + `/dev/net/tun`、`ADAPTER_VPN_CONFIG=/vpn/client.ovpn`、`./drivers`、`./adapter`、`./work`、`./vpn` 挂载(热更新/产物落宿主)。
- **entrypoint.sh**:校验必填 env → 起 OpenVPN(可选)→ 连通性测试(非致命)→ `exec benchmark_driver.py`。
- **`work/`**:worker 解题产物目录(每题 `<code>/` 子目录 + CLAUDE.md/FLAG 等),bind-mount 到宿主 `./work`,不入库。

## 6. 测试与验证

无自动化测试(已随瘦身删除)。把关手段:
- `python -m py_compile` + `pyflakes`(tsecbench/adapter/drivers)全绿;
- 冒烟:种子任务 config + 临时 DB 起 core → curl 走查 /openapi/v1 全路由(鉴权/列题/start/hint 扣分/submit/幂等/close/vpn);本地 core + 直跑 driver 观察 开题→pi→直提→关题→轮询;
- `docker compose config` 合法且恰 1 服务。

## 7. 已知边界(现状如此,非缺陷)

- 单 worker 串行,天然适配 VPN 每用户 1 连接;多 worker 需自行复制服务并共享网络栈(旧形态已删,需要时参考 git 历史)。
- pi_agent 的候选提取正则写死 `flag{...}`(`extract_flags`),题面 flag 格式由 `ADAPTER_FLAG_FORMAT` 在提示词中说明,两者需一致。
- driver 无状态,进程重启后 in-memory 去重归零;已提交正确的 flag 重复提交返回 `duplicate`,平台幂等无副作用。
- core 供给实例的容器 ≠ worker:题目实例由 core/provisioner 管理,worker 只是经 VPN 访问它的攻击者。
