#!/usr/bin/env bash
# FastAPI 控制台启动/停止脚本
# 用法: bash run.sh [start|stop]
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$BASE_DIR/../.venv"
PORT="${FAC_PORT:-8003}"

if [ "$1" = "stop" ]; then
  pkill -f "uvicorn fastapi_console.main:app --app-dir "$BASE_DIR"" 2>/dev/null || true
  pkill -f "fastapi-console.main:app" 2>/dev/null || true
  echo "FastAPI 控制台已停止 (端口 $PORT)"
  exit 0
fi

if [ -z "$BENCHMARK_TOKEN" ] && [ -z "$TSECBENCH_TASKS_JSON" ] && [ -z "$TSECBENCH_CONFIG" ]; then
  echo "[init] 未配置任务：请设置 BENCHMARK_TOKEN + TSECBENCH_CONFIG/tasks.json（或网页设置页配置远端平台）"
  echo "      不会内置任何赛题信息。"
fi

(
  cd "$BASE_DIR"
  setsid nohup "$VENV/bin/python" -m uvicorn fastapi_console.main:app --app-dir "$BASE_DIR" --host 0.0.0.0 --port "$PORT" \
    > "$BASE_DIR/fastapi.log" 2>&1 < /dev/null &
)
echo "FastAPI 控制台已启动 -> http://localhost:$PORT  (日志: fastapi.log)"
echo "停止: bash $0 stop"
