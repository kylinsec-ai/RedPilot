/**
 * 凭据层:只保存在当前浏览器 session,不写入 localStorage。
 *
 * 两个独立凭据(最小特权 —— 读答案不等于管控制面):
 *   readToken  → 观测读端 /api/*       (明文 flag 与完整 agent 实录,见后端 check_read_token)
 *   adminToken → 控制面 /api/v1/*      (evaluation 生命周期 / VPN 等特权操作)
 *
 * 维护提示:两者都是"同一头名不同取值"或"不同头名",后端约定见
 * packages/ghost/ghost/obs/ingest_common.py 与 src/api/client.ts 的注入点。
 */
import { ref } from "vue";

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

const READ_KEY = "ghost.read_token";
const ADMIN_KEY = "ghost.admin_token";

const _readToken = ref(read(READ_KEY));
const _adminToken = ref(read(ADMIN_KEY));

/** 供 api/client.ts 的拦截器同步取值(拦截器在模块作用域读,不能依赖组件实例)。 */
export const readToken = () => _readToken.value;
export const adminToken = () => _adminToken.value;

/** 响应式视图:凭据门与模板用这两个。 */
export const readAuth = _readToken;
export const adminAuth = _adminToken;

/** 读 token 变更订阅点:实时通道靠它离开"待授权"态并重连。 */
const readTokenListeners = new Set<() => void>();
export function onReadTokenChange(fn: () => void): () => void {
  readTokenListeners.add(fn);
  return () => readTokenListeners.delete(fn);
}

export function setReadToken(value: string): void {
  const token = value.trim();
  _readToken.value = token;
  write(READ_KEY, token);
  // 通知实时通道重连:token 变更前 SSE 可能停在"待授权"态(不自动重试)
  for (const fn of readTokenListeners) fn();
}

export function setAdminToken(value: string): void {
  const token = value.trim();
  _adminToken.value = token;
  write(ADMIN_KEY, token);
}
