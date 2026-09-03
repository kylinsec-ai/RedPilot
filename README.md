# TSecBench(极简 MVP)

安全基准测试的最小闭环:**平台(core)出题判分 + 一个求解 worker 自动刷题**。

```
┌──────────────┐  HTTP(BENCHMARK_TOKEN)   ┌───────────────────────────────┐
│ core (宿主机) │◀─────────────────────────│ worker 容器(VPN + Pi Agent)    │
│ :8000        │  /openapi/v1/*           │ 常驻串行: 开题→解→直提→关题     │
│ 纯 REST 无 UI │                          └───────────────────────────────┘
└──────────────┘
```

- **平台 core** (`tsecbench/` + `main.py`):FastAPI 服务,管理跑分任务生命周期 —— 列题、起停题目实例(static/docker 供给)、可选 hint(扣分)、flag 提交判分(SHA-256,明文不落库)。状态持久化 SQLite。**无任何网页/控制台,worker 是唯一客户端**。
- **求解 worker** (`adapter/` + `drivers/benchmark_driver.py` + compose):单个带 VPN 的容器,一次开一道题、单会话 Pi Agent 求解、候选 flag 直接提交(平台判分/幂等即唯一闸门),刷完轮询待命。无多 worker 分片、无记忆/黑板/止损/skeptic 验证等重机制。
- 接口契约见 [`CHALLENGES_API.md`](CHALLENGES_API.md);内部结构见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 运行平台 core

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 任务目录(JSON,含 token 与 challenges;每题 flag 明文写在此文件)
export TSECBENCH_CONFIG=/path/to/tasks.json
# 可选: TSECBENCH_DB_PATH(默认 ./data/tsecbench.sqlite3)、PORT(8000)、
#        TSECBENCH_MAX_ACTIVE_CHALLENGES(3)、TSECBENCH_PROVISIONER(static|docker)
.venv/bin/python main.py
```

任务目录格式:`{"tasks":[{token, challenges:[{unique_code, description, difficulty,
level, total_score, flags, hint?, hint_cost_radio?, container_addr?, image?,
container_port?, docker_network?}]}]}`,或直接传对象/数组;`TSECBENCH_TASKS_JSON`
可传内联 JSON。鉴权:`BENCHMARK_TOKEN` 请求头(缺失/无效返回 404 task_not_found)。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TSECBENCH_CONFIG` / `TSECBENCH_TASKS_JSON` | unset | 任务目录(文件路径 / 内联 JSON) |
| `BENCHMARK_TOKEN` | unset | 请求鉴权 token(也注入任务定义) |
| `TSECBENCH_DB_PATH` | `./data/tsecbench.sqlite3` | SQLite 路径 |
| `TSECBENCH_MAX_ACTIVE_CHALLENGES` | `3` | 同时活跃题目实例上限 |
| `TSECBENCH_PROVISIONER` | `static` | 题目供给: static(认 container_addr)/ docker |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | `python main.py` 监听地址 |

## 运行求解 worker

前置:core 已起;`.env` 填好平台地址/凭据与模型 key;VPN 配置文件放在 `./vpn/client.ovpn`。

```bash
cp .env.example .env        # 填 BENCHMARK_TOKEN/BASE_URL/SOLVER_API_KEY
docker compose up -d --build
docker compose logs -f      # 观察刷题进度
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `BENCHMARK_TOKEN` / `BENCHMARK_BASE_URL` | — | 平台凭据(必填) |
| `SOLVER_PROVIDER` | `deepseek` | 模型供应商预设(deepseek-1m/glm/glm-1m) |
| `SOLVER_API_KEY` | — | Pi Agent 模型密钥(必填) |
| `SOLVER_MODEL` | 预设模型 | 模型名覆盖 |
| `SOLVER_SESSION_SECONDS` | `1500` | 单会话时长上限 |
| `ADAPTER_WORKDIR` | `/work` | 解题工作目录根(compose 已设) |
| `ADAPTER_FLAG_FORMAT` | `flag{...}` | 提示词中的 flag 格式说明 |
| `ADAPTER_VPN_CONFIG` | `/vpn/client.ovpn` | VPN 配置(entrypoint 读取,无则跳过) |
| `PI_STALL_TIMEOUT` | `480` | pi 无输出判定卡死的秒数 |
| `WATCHDOG_MAX_IDLE_SECONDS` | `300` | 心跳容忍(compose healthcheck) |

## 目录

- `tsecbench/` — 平台 core(纯 REST)
- `adapter/` — worker 侧组件库:config/task/taskprompt + `platform/`(HTTP 后端)+ `solver/`(Pi Agent)
- `drivers/benchmark_driver.py` — 串行求解主循环(唯一编排者)
- `entrypoint.sh` / `Dockerfile` / `docker-compose.yaml` — worker 镜像与编排
- `CHALLENGES_API.md` / `ARCHITECTURE.md` — 平台接口契约 / 内部结构
