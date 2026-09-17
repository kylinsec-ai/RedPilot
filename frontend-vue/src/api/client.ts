/**
 * 类型化 API 客户端。同源:统一服务提供 /api/* 与 /api/v1/*;dev 由 vite 代理到 :8000。
 *
 * 凭据分流是安全边界,不是便利:**两个 token 各自只发往自己的路由前缀**。
 *   /api/v1/* → X-Platform-Admin-Token(控制面)
 *   /api/*    → X-Observability-Token(观测读端;后端读端返回明文 flag 与完整实录)
 * 因此用两个 axios 实例 + 各自的拦截器,而不是一个实例按 URL 判断 ——
 * 单实例判前缀也能work,但双实例让"发错头"在结构上不可能发生。
 */
import axios, { type AxiosInstance } from "axios";
import { adminToken, readToken } from "../stores/auth";

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
/** 控制面凭据头名。只此一处(同上)。 */
export const ADMIN_HEADER = "X-Platform-Admin-Token";

/** 观测读端 /api/* */
export const obs: AxiosInstance = build(() => readToken(), OBS_READ_HEADER);

/** 控制面 /api/v1/* */
export const ctl: AxiosInstance = build(() => adminToken(), ADMIN_HEADER);

/** 清掉 axios 的包壳,只把既有调用点期望的 ApiError 抛出去。 */
export async function unwrap<T>(p: Promise<{ data: T }>): Promise<T> {
  return (await p).data;
}
