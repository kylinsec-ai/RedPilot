/**
 * 类型化只读 API 客户端。同源:平台服务(obs)提供 /api/*;dev 由 vite 代理到 :8090。
 */
import type {
  ChallengeDetail,
  LiveSnap,
  RosterSnapshot,
  RunEventsResp,
  RunRow,
  RunsListResp,
  TimelineResp,
} from "./types";

async function j<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " HTTP " + r.status);
  return r.json() as Promise<T>;
}

export const fetchRoster = () => j<RosterSnapshot>("/api/roster");
export const fetchStatus = () => j<LiveSnap>("/api/status");
export const fetchChallenge = (code: string) =>
  j<ChallengeDetail>("/api/challenge?code=" + encodeURIComponent(code));
export const fetchTimeline = (code: string, after: number) =>
  j<TimelineResp>(`/api/timeline?code=${encodeURIComponent(code)}&after=${after}`);
export const fetchTranscript = (code: string, tail = 200) =>
  j<{ code: string; lines: string[] }>(
    `/api/transcript?code=${encodeURIComponent(code)}&tail=${tail}`,
  );

/** ── Runs 历史 ── */
export const fetchRuns = (qs = "") => j<RunsListResp>("/api/runs" + qs);
export const fetchRun = (runId: string) => j<RunRow>(`/api/runs/${runId}`);
export const fetchRunEvents = (runId: string, after = 0, limit = 500) =>
  j<RunEventsResp>(`/api/runs/${runId}/events?after=${after}&limit=${limit}`);
export const fetchRunTimeline = (runId: string, after = 0) =>
  j<TimelineResp>(`/api/runs/${runId}/timeline?after=${after}`);
