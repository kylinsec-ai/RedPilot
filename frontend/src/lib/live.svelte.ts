/**
 * 顶栏实时态:SSE(/api/events)+ 15s /api/status 兜底轮询(页面可见时)。
 * 语义与旧版一致:首帧快照即推送、断线 3s 手动重连、坏帧静默丢弃。
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
    es = new EventSource("/api/events");
    es.onopen = () => {
      live.conn = "live";
    };
    es.onerror = () => {
      live.conn = "reconnecting";
      try {
        es?.close();
      } catch {
        /* noop */
      }
      retry = setTimeout(connect, 3000);
    };
    es.onmessage = (ev) => {
      try {
        paint(JSON.parse(ev.data));
      } catch {
        /* 坏帧静默 */
      }
    };
  };
  connect();

  // SSE 断线兜底:15s 拉一次 status(切后台不拉)
  const fallback = setInterval(() => {
    if (stopped || document.hidden) return;
    fetchStatus()
      .then((s) => paint({ ...s, kind: "poll" }))
      .catch(() => {});
  }, 15000);

  return () => {
    stopped = true;
    es?.close();
    clearInterval(fallback);
    if (retry) clearTimeout(retry);
  };
}
