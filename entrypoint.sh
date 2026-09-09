#!/usr/bin/env bash
set -euo pipefail

echo "[adapter] === TsecBench 平台接入层适配器 ==="
echo "[adapter] BENCHMARK_BASE_URL=${BENCHMARK_BASE_URL:-<unset>}"

# ── 校验必需环境变量 ──
: "${BENCHMARK_TOKEN:?BENCHMARK_TOKEN must be provided}"
: "${BENCHMARK_BASE_URL:?BENCHMARK_BASE_URL must be provided}"

# 模型密钥：网关 key（ANTHROPIC_AUTH_TOKEN）优先，SOLVER_API_KEY 兜底
if [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
  export DEEPSEEK_API_KEY="${ANTHROPIC_AUTH_TOKEN}"
elif [[ -n "${SOLVER_API_KEY:-}" ]]; then
  export DEEPSEEK_API_KEY="${SOLVER_API_KEY}"
else
  echo "[adapter] WARNING: no model API key set (ANTHROPIC_AUTH_TOKEN / SOLVER_API_KEY) — Pi Agent cannot authenticate." >&2
  export DEEPSEEK_API_KEY=""
fi

cd /app 2>/dev/null || true

# ── VPN 连接 ──
# TsecBench 要求: 所有题目入口地址必须通过 VPN 才能访问
# ADAPTER_VPN_CONFIG 为空/未设时跳过（如共享 worker-1 网络的 worker-2/3）
VPN_CONFIG="${ADAPTER_VPN_CONFIG:-}"

if [[ -n "${VPN_CONFIG}" && -f "${VPN_CONFIG}" ]]; then
  echo "[adapter] starting OpenVPN: ${VPN_CONFIG}"

  ovpn_args=(--config "${VPN_CONFIG}" --daemon --log /tmp/openvpn.log --writepid /tmp/openvpn.pid)
  # 保活: ping + 断线自动重连
  ovpn_args+=(--ping 10 --ping-restart 60)

  openvpn "${ovpn_args[@]}" || { echo "[adapter] FATAL: openvpn failed" >&2; exit 1; }

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
    echo "[adapter] WARNING: VPN tun0 not detected in 90s, check /tmp/openvpn.log" >&2
    tail -n 20 /tmp/openvpn.log >&2 2>/dev/null || true
    # 不直接退出，继续尝试运行（可能 API 不需要 VPN）
  fi

  sleep 3
  echo "[adapter] VPN status:"
  ip addr show tun0 2>/dev/null | head -5 || echo "  (tun0 not found)"
  ip route | grep tun 2>/dev/null || echo "  (no tun routes)"
else
  echo "[adapter] no VPN config at ${VPN_CONFIG}, skipping VPN"
  echo "[adapter] WARNING: 题目入口地址需要 VPN 才能访问！"
fi

# ── 创建工作目录 ──
mkdir -p "${ADAPTER_WORKDIR:-/work}"

# ── 验证平台连通性 ──
echo "[adapter] testing platform API connectivity..."
http_code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
  -H "BENCHMARK_TOKEN: ${BENCHMARK_TOKEN}" \
  "${BENCHMARK_BASE_URL}/openapi/v1/challenges" 2>/dev/null) || http_code="000"
echo "[adapter] platform API response: HTTP ${http_code}"

if [[ "${http_code}" == "000" ]]; then
  echo "[adapter] WARNING: cannot reach platform API"
elif [[ "${http_code}" == "404" ]]; then
  echo "[adapter] WARNING: token may be invalid (404)"
fi

echo "[adapter] starting benchmark driver..."
exec python3 /app/drivers/benchmark_driver.py
