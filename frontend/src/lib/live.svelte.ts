/**
 * 顶栏实时态:SSE(/api/events)+ 15s /api/status 兜底轮询(页面可见时)。
 * 语义与旧版一致:首帧快照即推送、断线 3s 手动重连、坏帧静默丢弃。
 * 跨传输定序:两路统一走 paintIfNewer,按快照 updated_at 去旧——
 * 延迟的 SSE 旧帧与过期的轮询响应都不得盖掉已绘制的新状态。
 * 断线重连在后台标签页不空转(visibilitychange 唤醒后再连)。
 *
 * 为何不用 EventSource:观测读端需凭据(返回明文 flag 与完整 agent 实录),
 * 而 EventSource 无法自定义请求头。改用 fetch + ReadableStream 手读 SSE
 * 帧格式(纯文本解析,无新依赖),从而把凭据放进 header 而非 URL
 * ——URL 会进 uvicorn access log / 浏览器历史 / Referer。
 */
import { fetchStatus, obsReadHeaders } from "./api";
import type { LiveSnap } from "./types";

export const live = $state<{
  phase: string;
  challenge_code: string;
  turns: number;
  conn: "connecting" | "live" | "reconnecting" | "unauthorized";
}>({ phase: "idle", challenge_code: "", turns: 0, conn: "connecting" });

function paint(s: LiveSnap): void {
  if (!s) return;
  live.phase = s.phase || "idle";
  live.challenge_code = s.challenge_code || "";
  if (typeof s.turns === "number") live.turns = s.turns;
}

let lastPaintedAt = -1; // 已绘制快照的最大 updated_at;跨传输旧帧过滤

function paintIfNewer(s: LiveSnap): void {
  if (!s) return;
  const at = typeof s.updated_at === "number" ? s.updated_at : -1;
  if (at >= 0) {
    if (at < lastPaintedAt) return; // 延迟 SSE / 过期轮询:旧状态不得回盖
    lastPaintedAt = at;
  }
  paint(s);
}

let started = false;

/** app 生命周期内只启动一次;返回清理函数(App 卸载时用) */
export function startLiveWatchers(): () => void {
  if (started) return () => {};
  started = true;

  let stopped = false;
  let ctrl: AbortController | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let running = false;

  const connect = async () => {
    if (stopped || running || document.hidden) return;
    if (retry) {
      clearTimeout(retry);
      retry = null;
    }
    running = true;
    ctrl = new AbortController();
    try {
      const r = await fetch("/api/events", {
        headers: obsReadHeaders(),
        signal: ctrl.signal,
      });
      if (!r.ok || !r.body) {
        // 凭据问题:重试无意义(且会持续打服务端)—— 停在待授权态,由用户补 token 后重连
        if (r.status === 401 || r.status === 403 || r.status === 503) {
          live.conn = "unauthorized";
          return;
        }
        throw new Error(`HTTP ${r.status}`);
      }
      live.conn = "live";
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let sep: number;
        // SSE 帧以空行分隔;`: heartbeat` 注释行无 data: 前缀,自然被忽略
        while ((sep = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          for (const line of frame.split("\n")) {
            if (!line.startsWith("data: ")) continue;
            try {
              paintIfNewer(JSON.parse(line.slice(6)));
            } catch {
              /* 坏帧静默 */
            }
          }
        }
      }
      throw new Error("stream closed");
    } catch {
      if (stopped) return;
      // 单链固定 3s 重试,无退避无堆积;后台时留给 visibilitychange 唤醒
      if (live.conn !== "unauthorized") live.conn = "reconnecting";
      if (!document.hidden) retry = setTimeout(connect, 3000);
    } finally {
      running = false;
    }
  };

  /** 凭据变更后由设置面板调用:清掉待授权态并立即重连。 */
  const reconnect = () => {
    ctrl?.abort();
    live.conn = "connecting";
    connect();
  };
  document.addEventListener("redpilot:read-token-changed", reconnect);

  const onVisible = () => {
    if (!stopped && !document.hidden && !running) connect();
  };
  document.addEventListener("visibilitychange", onVisible);

  connect();

  // SSE 断线兜底:15s 拉一次 status(切后台不拉);与 SSE 同走版本守卫
  const fallback = setInterval(() => {
    if (stopped || document.hidden) return;
    fetchStatus()
      .then((s) => {
        paintIfNewer({ ...s, kind: "poll" });
      })
      .catch(() => {});
  }, 15000);

  return () => {
    stopped = true;
    started = false;
    ctrl?.abort();
    ctrl = null;
    clearInterval(fallback);
    if (retry) clearTimeout(retry);
    document.removeEventListener("visibilitychange", onVisible);
    document.removeEventListener("redpilot:read-token-changed", reconnect);
  };
}
