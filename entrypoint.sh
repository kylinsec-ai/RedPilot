#!/usr/bin/env bash
set -euo pipefail

echo "[adapter] === RedPilot 平台接入层适配器 ==="
echo "[adapter] BENCHMARK_BASE_URL=${BENCHMARK_BASE_URL:-<unset>}"
echo "[adapter] ADAPTER_ROLE=${ADAPTER_ROLE:-solver} ADAPTER_WORKER_ID=${ADAPTER_WORKER_ID:-<auto>}"

# ── 校验必需环境变量 ──
# 缺失 = 配置错误:明示后 exit 0 停止(restart:on-failure 会重启一切非零退出,
# 只有 exit 0 能"停一次";用 :? 会 exit 1 无限闷循环,掩盖真因)
#
# 注:worker-1(ADAPTER_ROLE=monitor)也要 BENCHMARK_TOKEN —— 它虽然不做题,
# 但要用同一个客户端做 VPN 预检与状态汇总。
if [[ -z "${BENCHMARK_TOKEN:-}" ]]; then
  echo "[adapter] FATAL: BENCHMARK_TOKEN 未设置(.env 或 compose 环境变量)——容器停止,补齐后重新 docker compose up -d" >&2
  exit 0
elif [[ -z "${BENCHMARK_BASE_URL:-}" ]]; then
  echo "[adapter] FATAL: BENCHMARK_BASE_URL 未设置——容器停止,补齐后重新 docker compose up -d" >&2
  exit 0
fi

# LLM 凭据由 pi 自行解析(官方 env 名,其次 ~/.pi/agent/auth.json)。
# 空字符串的 *_API_KEY 视为未设后 unset(避免空值歧义;provider 凭据 env 名
# 以 _API_KEY 结尾是 pi 官方惯例,见 https://pi.dev/docs/latest/providers)。

# ── 旧别名兜底桥接（先于下面的凭据检测）──
# pi 只认官方 env 名。旧部署沿用朋友的 `SOLVER_API_KEY` / `ANTHROPIC_AUTH_TOKEN` 写
# .env，只打 WARNING 不桥接的话 pi 会以**无凭据**启动:每题 0-turn 失败、靠熔断才停
# 下来,日志里看不出是凭据问题（一类很难查的静默失败）。所以这里桥一次 —— 与朋友
# entrypoint 同序（网关 key 优先），但官方名优先、且明确告警。
# 官方名一旦设了，下面两条**都不做**（尊重显式配置，也避免把网关 key 误当 deepseek key）。
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  if [[ -n "${SOLVER_API_KEY:-}" ]]; then
    export DEEPSEEK_API_KEY="${SOLVER_API_KEY}"
    echo "[adapter] WARNING: DEEPSEEK_API_KEY 未设,已从旧别名 SOLVER_API_KEY 桥接。" >&2
    echo "[adapter] WARNING: 该别名已废弃,请改用 pi 官方 env 名(见 packages/worker/README.md 凭据说明)。" >&2
  elif [[ -z "${ANTHROPIC_API_KEY:-}" && -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    export DEEPSEEK_API_KEY="${ANTHROPIC_AUTH_TOKEN}"
    echo "[adapter] WARNING: DEEPSEEK_API_KEY 未设,已从 ANTHROPIC_AUTH_TOKEN 桥接(朋友的旧部署走网关 key)。" >&2
    echo "[adapter] WARNING: 仅在默认 provider=deepseek 时成立;换 provider 请显式设其官方 env 名。" >&2
  fi
fi

have_key=0
while IFS= read -r k; do
  case "$k" in
    # 废弃别名不计入 have_key(上已告警/已桥接),避免"有旧 key、无真凭据"时误报安全
    SOLVER_API_KEY) ;;
    *_API_KEY)
      if [[ -n "${!k}" ]]; then have_key=1; else unset "$k"; fi ;;
  esac
done < <(compgen -e)
if [[ $have_key -eq 0 && ! -s "${HOME:-/root}/.pi/agent/auth.json" ]]; then
  echo "[adapter] WARNING: 未检测到 *_API_KEY 凭据且无 ~/.pi/agent/auth.json,pi 可能无法鉴权(见 packages/worker/README.md 凭据说明)。" >&2
fi

cd /app 2>/dev/null || true

# ── VPN 连接 ──
# RedPilot 要求: 所有题目入口地址必须通过 VPN 才能访问
# ADAPTER_VPN_CONFIG 为空/未设时跳过（如共享 worker-1 网络的 worker-2/3）
VPN_CONFIG="${ADAPTER_VPN_CONFIG:-}"

if [[ -n "${VPN_CONFIG}" && -f "${VPN_CONFIG}" ]]; then
  echo "[adapter] starting OpenVPN: ${VPN_CONFIG}"

  ovpn_args=(--config "${VPN_CONFIG}" --daemon --log /tmp/openvpn.log --writepid /tmp/openvpn.pid)
  # 保活: ping + 断线自动重连。
  # ping-restart **默认 600s 而不是 60s** —— 朋友的实测记录:60s 对空闲隧道
  # 就是死刑（4 小时内 47 次重启、间隔精确 360s、隧道在线率 ≈1/6）。空闲时
  # ping 不回不代表隧道坏了，降到 60s 会把整轮跑分切成碎片。
  ovpn_args+=(--ping 10 --ping-restart "${ADAPTER_VPN_PING_RESTART:-600}")

  # openvpn 启动失败视为 VPN 层瞬断:exit 4 → restart:on-failure 自动拉起(与 driver 同款语义)
  openvpn "${ovpn_args[@]}" || { echo "[adapter] FATAL: openvpn failed, exiting 4 (restart will retry)" >&2; exit 4; }

  # 等待 VPN 建立 — 检测 tun 设备或尝试访问内网
  echo "[adapter] waiting for VPN tunnel..."
  up=0
  for i in $(seq 1 90); do
    # 检查 tun 设备是否出现
    if ip link show tun0 >/dev/null 2>&1; then
      # 尝试 ping 一个内网地址（如果知道的话）
      echo "[adapter] VPN tun0 up after ${i}s"
      up=1
      break
    fi
    sleep 1
  done

  if [[ "$up" != "1" ]]; then
    echo "[adapter] FATAL: VPN tun0 not detected in 90s" >&2
    tail -n 20 /tmp/openvpn.log >&2 2>/dev/null || true
    # 配置了 VPN 就要求它真的起来:此前是"告警后继续",结果是整轮跑分都在
    # 无隧道状态下撞靶场地址(每道题都失败,却看不出是网络层问题)。
    # exit 4 = VPN 层瞬断语义,restart:on-failure 会拉起重试(与 driver 一致)。
    # 确需容忍时显式设 VPN_REQUIRE_TUN=0。
    if [[ "${VPN_REQUIRE_TUN:-1}" == "1" ]]; then
      exit 4
    fi
    echo "[adapter] WARNING: VPN_REQUIRE_TUN=0, continuing without tun0" >&2
  else
    # tun0 出现 ≠ 隧道可用:openvpn 在鉴权完成前就会建 tun 设备。
    # 再验一条到隧道的路由,把"设备在但没路由"这种半通状态挡在跑分之前。
    if ! ip route get 1.1.1.1 2>/dev/null | grep -q "dev tun0"; then
      # 默认路由未必走 tun(常见于 split-tunnel 配置),退化检查:是否存在 tun 路由
      if ! ip route | grep -q " dev tun0"; then
        echo "[adapter] WARNING: tun0 present but no route via tun0 — tunnel may not be established" >&2
      fi
    fi
  fi

  sleep 3
  echo "[adapter] VPN status:"
  ip addr show tun0 2>/dev/null | head -5 || echo "  (tun0 not found)"
  ip route | grep tun 2>/dev/null || echo "  (no tun routes)"
else
  echo "[adapter] no VPN config at ${VPN_CONFIG}, skipping VPN"
  echo "[adapter] WARNING: 题目入口地址需要 VPN 才能访问！"
fi

# ── 创建工作目录(失败=配置错:exit 0 明示停止,不进 restart 闷循环) ──
if ! mkdir -p "${ADAPTER_WORKDIR:-/work}"; then
  echo "[adapter] FATAL: cannot create ADAPTER_WORKDIR=${ADAPTER_WORKDIR:-/work}" >&2
  exit 0
fi

# ── 验证平台连通性（tsec-run 冒烟:官方 SDK CLI,只读 list;非致命,仅告警）──
echo "[adapter] testing platform API connectivity via tsec-run..."
# timeout 兜底：这条检查**不是**启动门禁（平台不可达时编排层自己会重试与退避），
# 但它是同步阻塞的 —— 平台无响应时 SDK 自己的超时可能很久，而编排层马上就
# 会做同一件事（list_challenges）。卡在这里只会推迟真正该开始的那一步。
if timeout 20 env TSEC_BASE_URL="${BENCHMARK_BASE_URL}" TSEC_TOKEN="${BENCHMARK_TOKEN}" tsec-run > /tmp/tsec-run.log 2>&1; then
  echo "[adapter] platform API reachable"
  head -5 /tmp/tsec-run.log
else
  echo "[adapter] WARNING: platform unreachable / bad token (see /tmp/tsec-run.log)" >&2
  head -20 /tmp/tsec-run.log >&2 2>/dev/null || true
fi

echo "[adapter] starting arena driver (redpilot_worker.orchestrator via driver 装配层)..."
exec python3 -m redpilot_worker.driver
