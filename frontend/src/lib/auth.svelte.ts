/**
 * 凭据层:只保存在当前浏览器 session,不写入 localStorage。
 *
 * 两个独立凭据(最小特权 —— 读答案不等于管控制面):
 *   readAuth  → 观测读端 /api/*       (明文 flag 与完整 agent 实录,见后端 check_read_token)
 *   adminAuth → 控制面 /api/v1/*      (evaluation 生命周期 / VPN 等特权操作)
 *
 * 维护提示:两者都是"同一头名不同取值"或"不同头名",后端约定见
 * packages/redpilot/redpilot/obs/ingest_common.py 与 frontend/src/lib/api.ts 的注入点。
 */

function read(key: string): string {
  if (typeof window === "undefined") return "";
  try {
    return window.sessionStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

function write(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    if (value) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch {
    // sessionStorage disabled: in-memory token remains usable for this page.
  }
}

const READ_KEY = "redpilot.read_token";
const ADMIN_KEY = "redpilot.admin_token";

/** 观测读端凭据:读 /api/*(态势台、runs 历史、transcript)。 */
export const readAuth = $state({ token: read(READ_KEY) });

/** 控制面管理员凭据:读 /api/v1/*(evaluation 生命周期、VPN)。 */
export const adminAuth = $state({ token: read(ADMIN_KEY) });

export function setReadToken(value: string): void {
  const token = value.trim();
  readAuth.token = token;
  write(READ_KEY, token);
  // 通知实时通道重连:token 变更前 SSE 可能停在"待授权"态(不自动重试)
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("redpilot:read-token-changed"));
  }
}

export function setAdminToken(value: string): void {
  const token = value.trim();
  adminAuth.token = token;
  write(ADMIN_KEY, token);
}
