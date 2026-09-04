#!/usr/bin/env bash
set -euo pipefail

echo "[adapter] === TsecBench 平台接入层适配器 ==="
echo "[adapter] BENCHMARK_BASE_URL=${BENCHMARK_BASE_URL:-<unset>}"

# ── 校验必需环境变量 ──
: "${BENCHMARK_TOKEN:?BENCHMARK_TOKEN must be provided}"
: "${BENCHMARK_BASE_URL:?BENCHMARK_BASE_URL must be provided}"

# LLM 凭据由 pi 自行解析(官方 env 名,其次 ~/.pi/agent/auth.json)。
# 空字符串的 *_API_KEY 视为未设后 unset(避免空值歧义;provider 凭据 env 名
# 以 _API_KEY 结尾是 pi 官方惯例,见 https://pi.dev/docs/latest/providers)。
have_key=0
if [[ -n "${SOLVER_API_KEY:-}" ]]; then
  echo "[adapter] WARNING: SOLVER_API_KEY 已废弃且不会被 pi 读取——请改用 pi 官方 env 名(如 DEEPSEEK_API_KEY,见 README 凭据说明)。" >&2
fi
while IFS= read -r k; do
  case "$k" in
    # 废弃别名不计入 have_key(上已告警),避免“有旧 key、无真凭据”时误报安全
    SOLVER_API_KEY) ;;
    *_API_KEY)
      if [[ -n "${!k}" ]]; then have_key=1; else unset "$k"; fi ;;
  esac
done < <(compgen -e)
if [[ $have_key -eq 0 && ! -s "${HOME:-/root}/.pi/agent/auth.json" ]]; then
  echo "[adapter] WARNING: 未检测到 *_API_KEY 凭据且无 ~/.pi/agent/auth.json,pi 可能无法鉴权(见 README 凭据说明)。" >&2
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

# ── 验证平台连通性（tsec-run 冒烟:官方 SDK CLI,只读 list;非致命,仅告警）──
echo "[adapter] testing platform API connectivity via tsec-run..."
if TSEC_BASE_URL="${BENCHMARK_BASE_URL}" TSEC_TOKEN="${BENCHMARK_TOKEN}" tsec-run > /tmp/tsec-run.log 2>&1; then
  echo "[adapter] platform API reachable"
  head -5 /tmp/tsec-run.log
else
  echo "[adapter] WARNING: platform unreachable / bad token (see /tmp/tsec-run.log)" >&2
  head -20 /tmp/tsec-run.log >&2 2>/dev/null || true
fi

echo "[adapter] starting benchmark driver..."
exec python3 /app/drivers/benchmark_driver.py
