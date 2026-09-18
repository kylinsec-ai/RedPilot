/**
 * 类型化 API 客户端。同源:统一服务提供 /api/* 与 /api/v1/*;dev 由 vite 代理到 :8000。
 *
 * **本 SPA 只持观测读端凭据**:`/api/*` → `X-Observability-Token`(读端返回明文 flag
 * 与完整 agent 实录,故需凭据)。凭据头名只此一处 —— 名字错/改都只该表现为 401,
 * 而不是某条通道(如 SSE)静默停在"需观测凭据"态。
 *
 * 沿革(2026-09 死码清扫,承接 `fb96614` 的拆除):这里原有第二个 axios 实例 `ctl`
 * (`/api/v1/*` → `X-Platform-Admin-Token` 控制面)与配套的 `ADMIN_HEADER`。
 * `fb96614` 拆派发控制面时删掉了整页控制台(`Control.vue` + `api/control.ts` +
 * 导航与路由)以及 `/api/v1/evaluations|workers|attempts` 全部路由,**但把服务它的
 * 底层管线落在了原地**:`ctl` 全仓无 import、`adminAuth` 无组件引用、
 * `setAdminToken` 无人调用。零消费者的残留,一并删除。
 * 若日后重建控制台视图,`build()` 就是那个可复用的构造器(它仍在,没有被删)。
 */
import axios, { type AxiosInstance } from "axios";
import { readToken } from "../stores/auth";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(url: string, status: number, code: string, message: string) {
    super(`${url} HTTP ${status} (${code}): ${message}`);
    this.status = status;
    this.code = code;
  }
}

function build(token: () => string, header: string): AxiosInstance {
  const inst = axios.create({ timeout: 30000 });
  inst.interceptors.request.use((cfg) => {
    const t = token();
    if (t) cfg.headers.set(header, t);
    return cfg;
  });
  inst.interceptors.response.use(
    (r) => r,
    (err) => {
      const url = String(err?.config?.url ?? "");
      const status = Number(err?.response?.status ?? 0);
      const body = err?.response?.data;
      let code = "http_error";
      let message = err?.message || "request failed";
      if (body && typeof body === "object") {
        if (typeof body.code === "string") code = body.code;
        if (typeof body.message === "string") message = body.message;
      } else if (status) {
        message = `HTTP ${status}`;
      }
      return Promise.reject(new ApiError(url, status, code, message));
    },
  );
  return inst;
}

/** 观测读端凭据头名。**只此一处** —— 名字错/改都只该在浏览器里表现为 401,
    而不是某条通道(如 SSE)静默停在"需观测凭据"态。 */
export const OBS_READ_HEADER = "X-Observability-Token";

/** 观测读端 /api/* */
export const obs: AxiosInstance = build(() => readToken(), OBS_READ_HEADER);

/** 清掉 axios 的包壳,只把既有调用点期望的 ApiError 抛出去。 */
export async function unwrap<T>(p: Promise<{ data: T }>): Promise<T> {
  return (await p).data;
}
