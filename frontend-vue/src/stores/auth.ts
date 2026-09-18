/**
 * 凭据层:只保存在当前浏览器 session,不写入 localStorage。
 *
 * **只有一个凭据**:`readToken` → 观测读端 `/api/*`(明文 flag 与完整 agent 实录,
 * 见后端 `redpilot/obs/ingest_common.py` 的 `check_read_token`)。注入点在
 * `src/api/client.ts`。
 *
 * 沿革(2026-09 死码清扫,承接 `fb96614` 的拆除):这里原有第二个凭据 `adminToken`
 * (控制面 `/api/v1/*`,evaluation 生命周期 / VPN 等特权操作)与配套的 `adminAuth`
 * 响应式视图、`setAdminToken` 写入口。整页控制台被 `fb96614` 拆除后,这三个都没有
 * 任何组件或视图引用 —— 零消费者的残留,一并删除。原委见 `src/api/client.ts` 的
 * 模块 docstring。
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

// 键名带品牌前缀:改名 ghost → redpilot 后一并跟上(见 `1d7465f`)。
// 改键名只会让已打开的页面丢一次 token(重填即可),无迁移成本。
const READ_KEY = "redpilot.read_token";

const _readToken = ref(read(READ_KEY));

/** 供 api/client.ts 的拦截器同步取值(拦截器在模块作用域读,不能依赖组件实例)。 */
export const readToken = () => _readToken.value;

/** 响应式视图:凭据门与模板用它。 */
export const readAuth = _readToken;

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
