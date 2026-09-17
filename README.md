# Ghost — 安全基准测试平台

三包 Python monorepo：一个**求解 worker**（连靶场 VPN，驱动 Pi Agent 解题、提交 flag），
一个**统一服务端**（控制面 + 观测平台，同一个 FastAPI `:8000`），一份**零依赖契约包**。

```
packages/contracts   ghost-contracts   零依赖契约：状态快照 schema、词汇表、redact/text/fsio、paths
packages/ghost       ghost             控制面（challenges/调度/VPN）+ 观测平台（摄取/读端/SSE/SPA）
packages/worker      ghost-worker      求解 worker：竞技场编排 + Pi Agent 引擎 + 观测中继
```

**依赖方向是红线**：`contracts ← ghost/worker`，反向一律不许（`packages/contracts/tests/test_purity.py`
把这条钉住了）。改契约先改 `contracts`，两个消费方各自跟上。

worker 的主循环是**竞技场**（`ghost_worker/orchestrator.py`）：多轮时间盒、多会话重访、多维止损、
eager 即时提交、能力分片、跨场续接、舰队监督与协作式热重载。求解引擎是 Pi Agent
（`ghost_worker/adapter/solver/pi_agent.py`，json print 模式）。**没有第二套编排** ——
框架早期那套（`orchestration.solve_one` / assignment claim 链路）已退位删除。

## 起起来

```bash
cp .env.example .env      # 填 BENCHMARK_TOKEN / BENCHMARK_BASE_URL / DEEPSEEK_API_KEY
docker compose up -d      # server + 三容器 worker 舰队
```

打开 `http://127.0.0.1:8000` 是服务端（观测 SPA + 控制面 API）。

舰队形态 —— `worker-1` 只维持 VPN 与状态汇总（`ADAPTER_ROLE=monitor`，不做题），
`worker-2/3` 用 `network_mode: service:worker-1` 复用它的网络命名空间：

一个靶场 VPN 只应有一条隧道，三个容器各起一条会互相抢路由 —— 所以 VPN 由 worker-1 独占。
代价是它必须常驻（它一停另两个就断网，由 netns 看门狗兜底）。舰队拓扑的取舍与排障见
[`packages/worker/README.md`](packages/worker/README.md)。

**单体形态**（一个容器自己持 VPN 自己解题，与改造前行为一致，方便对照与排障）点它的名字起：

```bash
docker compose up -d worker        # 只起这一个；另在 http://127.0.0.1:8080 起本地态势台
```

裸 `up -d` 不带它，是因为 `worker` 服务带了一个 profile。**别用
`--profile monolith up -d`** —— 那会把舰队三容器一起拉起来；compose 对显式点名的服务
不看 profile 门槛，所以点名就够了。

### 两个地址别搞混

| 变量 | 指向 |
|---|---|
| `BENCHMARK_BASE_URL` | **靶场平台**（题目 list/start/submit 的 REST API）。本地自测可指向自带的 server |
| `OBSERVABILITY_URL` | **观测平台**（本仓库的 server）。compose 内自动为 `http://server:8000` |

## 状态落在哪

两套状态、两个落点，都在 `/work`（宿主 `./work`）。**它们服务不同的消费方，都由
`observability.StatusBridge` 单向同步，字段名不同是刻意的**：

| 落点 | 内容 | 谁读 |
|---|---|---|
| `work/status/worker-<N>.json` | 编排层进度（`solving_active` / `sessions` / `flags_submitted` …） | `supervisor.py`、只读控制台 |
| `work/.live/<worker_id>.json` | 观测面快照（`phase` / `current_tool` / `turns` …） | relay → 服务端；本地 `:8080` 态势台 |

舰队形态下三个容器共享同一个 `/work`（`status/` 与 `.stoploss-locks/` 靠它协作）。

## 退出码契约

worker 容器与 compose 的 `restart: on-failure` 靠一组退出码配套（`0` 正常终止 / `86`
协作式热重载 / `4` VPN 层 / `3` 被孤立的解题 worker）。**完整表在
[`packages/worker/README.md`](packages/worker/README.md)** —— 那是唯一权威，这里不抄一份。

⚠️ 但有一条运维铁律值得在这里说：**配置错误绝不能非零退出**。`on-failure` 会重启一切
非零退出，用 exit 1 表达「环境变量没填」= 无限重启闷循环，真因被日志淹没。这也是
`driver.py` 与 `entrypoint.sh` 在配置校验失败时都走 exit 0 的原因。

## 凭据

| 变量 | 用途 |
|---|---|
| `BENCHMARK_TOKEN` | 靶场平台凭据（worker 的 list/start/submit；monitor 也用它做 VPN 预检） |
| `BENCHMARK_BASE_URL` | 靶场平台地址 |
| `DEEPSEEK_API_KEY` | LLM 凭据，**pi 的官方 provider env 名**（换 provider 就换这个名字，见 `.env.example`） |
| `OBSERVABILITY_TOKEN` | 遥测**写**端（worker → 平台）。未配则摄取响亮 503，不静默丢弃 |
| `OBSERVABILITY_READ_TOKEN` | 观测**读**端（态势台 / runs 历史 / transcript） |

读写两个 token 是**刻意不对称**的：读端返回**明文 flag 与完整 agent 实录**，而 worker
只持写端。所以「能写遥测」≠「能读答案」—— 这也是 `OBSERVABILITY_READ_TOKEN`
**不转发给 worker 容器**的原因。两者皆未设置时读端 503。

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q              # 全仓 352
.venv/bin/python -m pytest -q packages/    # 三包 177
.venv/bin/python -m pytest -q tests/       # 竞技场与策略层回归 175
```

`pytest.ini` 已把 `packages/`、`tests/` 收进 `testpaths`，并把三包与仓库根加进 `pythonpath`,
所以裸 `pytest` 就能收全部用例。仓库根 `tests/` 里是 100+ 条竞技场行为的回归用例（止损、
task epoch、eager 提交、交付账本、多段题推进、supervisor 判据）。

容器的 entrypoint 与 worker 装配层见 `entrypoint.sh` 与 `packages/worker/ghost_worker/driver.py`。

## 进不了镜像的两个资产

这两个目录都在仓库里、都**不在 worker 镜像里**（`.dockerignore` 排除）、也不被任何服务
启动。它们不是死代码 —— 但需要手动起，别以为 compose 会带上：

| 目录 | 是什么 | 怎么跑 |
|---|---|---|
| `tsecbench/` | **本地靶场 API**（题目/容器/提交/VPN 的完整挑战接口）。对着它就能端到端自测 worker，不需要真的靶场平台 | 自带 `.venv` 起：`.venv/bin/python -m uvicorn 'tsecbench.api:create_app' --factory --port 8000`；注意它和统一 server 抢同一个 8000，同时起要改端口 |
| `fastapi-console/` | **只读 Web 控制台**（看舰队/任务/运行产物） | `bash fastapi-console/run.sh start`（默认 `:8003`，`FAC_PORT` 可改）。它读仓库根的 `.agent.env` 拿平台凭据与 LLM key —— 该文件被 gitignore，需要自己建 |

两者都只被 `tests/` 引用，所以 `pytest` 会跑到它们、`docker compose` 不会。

## 文档地图

| 文件 | 内容 |
|---|---|
| `AGENTS.md` | **发给解题 Agent 的指令**（写进每道题的工作目录当 `CLAUDE.md`），不是仓库说明 |
| `skills/` | **解题技能库** —— 上游 [`yaklang/hack-skills`](https://github.com/yaklang/hack-skills) 的内联副本（103 个技能，三层路由）。溯源、同步方法与两处依赖见 `skills/PROVENANCE.md` |
| `packages/worker/README.md` | worker 的跑法、结构、四条关键约定（状态落点 / 退出码 / 进程回收 / flag 双闸门） |
| `docs/architecture/TARGET_ARCHITECTURE.md` | **目标架构** —— 把《智能体工程最佳实践研究报告》的十二条结论逐条对齐到本仓（六处已做对 / 三处方向相反 / 一处缺失），六面架构与 ROI 排序的差距清单。**只做设计，不含代码改动** |
| `.env.example` | 全部可调项，按段分组并标注「必须一起改」的联动项 |
| `docs/friend-reference/` | 朋友那一版的部分**历史快照**（只有 3 个部署文件逐字相同，其余是更早的版本；源码一份没存）。照它跑构建会失败 |
