/**
 * 顶栏实时态:SSE(/api/events)+ 15s /api/status 兜底轮询(页面可见时)。
 * 语义与旧版一致:首帧快照即推送、断线 3s 手动重连、坏帧静默丢弃。
 * 跨传输定序:两路统一走 paintIfNewer,按快照 updated_at 去旧——
 * 延迟的 SSE 旧帧与过期的轮询响应都不得盖掉已绘制的新状态。
 * 断线重连在后台标签页不空转(visibilitychange 唤醒后再连)。
 */
import { fetchStatus } from "./api";
import type { LiveSnap } from "./types";

export const live = $state<{
  phase: string;
  challenge_code: string;
  turns: number;
  conn: "connecting" | "live" | "reconnecting";
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
  let es: EventSource | null = null;
  let retry: ReturnType<typeof setTimeout> | null = null;

  const connect = () => {
    if (stopped) return;
    if (document.hidden) return; // 后台不建连:onVisible 唤醒后由 connect 接上
    if (retry) {
      clearTimeout(retry);
      retry = null;
    }
    const src = new EventSource("/api/events");
    es = src;
    src.onopen = () => {
      live.conn = "live";
    };
    src.onerror = () => {
      live.conn = "reconnecting";
      try {
        src.close();
      } catch {
        /* noop */
      }
      es = null;
      // 单链固定 3s 重试,无退避无堆积;后台时留给 visibilitychange 唤醒
      if (!document.hidden) retry = setTimeout(connect, 3000);
    };
    src.onmessage = (ev) => {
      try {
        paintIfNewer(JSON.parse(ev.data));
      } catch {
        /* 坏帧静默 */
      }
    };
  };

  const onVisible = () => {
    if (!stopped && !document.hidden && !es) connect();
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
    es?.close();
    es = null;
    clearInterval(fallback);
    if (retry) clearTimeout(retry);
    document.removeEventListener("visibilitychange", onVisible);
  };
}
