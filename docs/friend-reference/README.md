# TSecBench Agent 舰队

> **历史快照（存档用，不是当前仓库的说明）。** 本目录是朋友那一版的原始配置与文档，
> 逐字保留以便对照。其中的路径对应**搬迁前的树**：`drivers/`、顶层 `adapter/`、
> `CHALLENGES_API.md` 等都已不在当前位置（策略层已搬进
> `packages/worker/ghost_worker/adapter/`，主循环在 `ghost_worker/orchestrator.py`）。
> 本目录里的 Dockerfile / `docker-compose.yaml` / `entrypoint.sh` **不参与任何构建**，
> 照它们跑会因源目录不存在而失败。当前用法看仓库根 `README.md` 与
> `packages/worker/README.md`。

这个仓库保留三个当前可部署组件：

- `worker-1`：舰队监控与 OpenVPN 网络命名空间提供者。
- `worker-2` / `worker-3`：两个同质解题 worker，共享 `worker-1` 的网络。
- `fastapi-console`：本机 Web 控制台，用于查看舰队、任务和运行产物。

`tsecbench/` 是可独立测试的本地题目 API 核心；三舰队的生产入口是 `entrypoint.sh` 和 `drivers/benchmark_driver.py`。

## 准备

1. 确保已安装 Docker 和 Docker Compose v2。
2. 复制配置模板，只在本机 `.env` 中填写实际凭据：

```bash
cp .env.example .env
```

3. 将 OpenVPN 配置放到 `vpn/client.ovpn`。`vpn/`、`.env`、`work/` 和 `data/` 都不会进入 Git 或镜像构建上下文。

## 构建与部署

首次部署：

```bash
docker compose build
docker compose up -d
docker compose ps
```

三台 worker 全量滚动重建时，先停掉共享旧网络的解题 worker，再替换 `worker-1`：

```bash
docker compose stop worker-2 worker-3
docker compose up -d --force-recreate --no-deps worker-1
until [ "$(docker inspect -f '{{.State.Health.Status}}' tsecbench-worker-1)" = healthy ]; do sleep 5; done
docker compose up -d --force-recreate --no-deps worker-2 worker-3
docker compose ps
```

不要执行 `docker compose down -v`；它会删除持久化数据。

## 运行检查

```bash
docker compose ps
docker compose logs --tail 100 worker-1 worker-2 worker-3
docker exec tsecbench-worker-1 ip addr show tun0
cat work/status/worker-*.json
```

没有待解题目时，worker 以配置的轮询间隔进入 `await-task`，不应持续占用 CPU。

## Web 控制台

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -r fastapi-console/requirements.txt
bash fastapi-console/run.sh start
```

默认地址为 `http://127.0.0.1:8003`。重启控制台：

```bash
bash fastapi-console/run.sh stop
bash fastapi-console/run.sh start
```

## 本地题目 API

如需单独启动 `tsecbench/` 的测试 API：

```bash
.venv/bin/python -m uvicorn 'tsecbench.api:create_app' --factory --host 127.0.0.1 --port 8000
```

完整接口规约见当时随树附带的 `CHALLENGES_API.md`（**已不在本仓库**；现行契约以
`packages/worker/ghost_worker/adapter/platform/tsecbench_http.py` 顶部的接口清单为准）。

## 验证

```bash
.venv/bin/python -m pytest -q
docker compose config -q
```

解题链路的回归测试位于 `tests/test_solver_regressions.py`，本地题目 API 测试位于 `tests/test_challenges_api.py`。

## 主要目录

- `adapter/`：调度、求解、验证、止损和运行观测。
- `drivers/`：三舰队运行入口。
- `skills/`：按题型注入的解题技能。
- `fastapi-console/`：现用 Web 控制台。
- `tsecbench/`：本地题目 API 和持久化实现。
- `work/`：运行产物，不进镜像和 Git。
