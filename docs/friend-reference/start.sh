#!/usr/bin/env bash
# TSecBench 一键启动/停止脚本：后端(8000) + 前端(5173)
# 用法: bash start.sh [start|stop]

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$BASE_DIR/tsecbench-frontend"
BACKEND_PORT="${BACKEND_PORT:-8000}"
DEMO_TOKEN="${TSECBENCH_TOKEN:-demo-token-001}"
ACTION="${1:-start}"

# ── 停止 ─────────────────────────────────────────────
if [ "$ACTION" = "stop" ]; then
  pkill -f "uvicorn main:app" 2>/dev/null || true
  pkill -f "vite" 2>/dev/null || true
  echo "已停止后端与前端"
  exit 0
fi

if [ "$ACTION" != "start" ]; then
  echo "用法: bash $0 [start|stop]"
  exit 1
fi

set -e

# ── 端口检查 ─────────────────────────────────────────
BACKEND_UP=0; FRONTEND_UP=0
if ss -tlnp 2>/dev/null | grep -q ":$BACKEND_PORT\b"; then
  echo "[!] 后端端口 $BACKEND_PORT 已被占用，跳过启动（可能已在运行）"
  BACKEND_UP=1
fi
if ss -tlnp 2>/dev/null | grep -q ":5173\b"; then
  echo "[!] 前端端口 5173 已被占用，跳过启动（可能已在运行）"
  FRONTEND_UP=1
fi

# ── 后端 ─────────────────────────────────────────────
if [ "$BACKEND_UP" = "0" ]; then
  echo "[1/2] 准备后端..."
  if [ ! -x "$BASE_DIR/.venv/bin/uvicorn" ]; then
    echo "     创建 Python 虚拟环境并安装依赖（首次较慢）..."
    python3 -m venv "$BASE_DIR/.venv"
    "$BASE_DIR/.venv/bin/pip" install -q -r "$BASE_DIR/requirements.txt" fastapi 'uvicorn[standard]' pydantic
  fi

  # 任务配置：优先 tasks.json / TSECBENCH_CONFIG，否则用演示任务
  if [ -n "$TSECBENCH_CONFIG" ] && [ -f "$TSECBENCH_CONFIG" ]; then
    export TSECBENCH_CONFIG
    echo "     使用任务配置: $TSECBENCH_CONFIG"
  elif [ -f "$BASE_DIR/tasks.json" ]; then
    export TSECBENCH_CONFIG="$BASE_DIR/tasks.json"
    echo "     使用任务配置: $BASE_DIR/tasks.json"
  else
    export BENCHMARK_TOKEN="$DEMO_TOKEN"
    echo "     未找到 tasks.json，使用演示任务（Token: $DEMO_TOKEN）"
  fi

  (
    cd "$BASE_DIR"
    setsid nohup "$BASE_DIR/.venv/bin/uvicorn" main:app --port "$BACKEND_PORT" \
      > "$BASE_DIR/backend.log" 2>&1 < /dev/null &
  )
  echo "     后端已启动 -> http://127.0.0.1:$BACKEND_PORT  (日志: backend.log)"
fi

# ── 前端 ─────────────────────────────────────────────
if [ "$FRONTEND_UP" = "0" ]; then
  echo "[2/2] 准备前端..."
  if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    echo "     安装前端依赖（首次较慢）..."
    (cd "$FRONTEND_DIR" && npm install --silent)
  fi
  export TSECBENCH_PROXY_TARGET="http://127.0.0.1:$BACKEND_PORT"
  setsid nohup npm run dev --prefix "$FRONTEND_DIR" \
    > "$BASE_DIR/frontend.log" 2>&1 < /dev/null &
  echo "     前端已启动 -> http://localhost:5173  (日志: frontend.log)"
fi

sleep 3
echo ""
echo "=============================================="
echo "  前端: http://localhost:5173  (Token: ${TSECBENCH_TOKEN:-$DEMO_TOKEN})"
echo "  后端: http://127.0.0.1:$BACKEND_PORT"
echo "  停止: bash $(basename "${BASH_SOURCE[0]}") stop"
echo "=============================================="
