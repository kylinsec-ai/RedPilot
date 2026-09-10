/**
 * 类型化只读 API 客户端。同源:平台服务(obs)提供 /api/*;dev 由 vite 代理到 :8090。
 */
import type {
  ChallengeDetail,
  EvaluationRow,
  LiveSnap,
  RosterSnapshot,
  RunEventsResp,
  RunRow,
  RunsListResp,
  TimelineResp,
  WorkersResp,
} from "./types";
import { adminAuth } from "./auth.svelte";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(url: string, status: number, code: string, message: string) {
    super(`${url} HTTP ${status} (${code}): ${message}`);
    this.status = status;
    this.code = code;
  }
}

async function j<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  // 管理 token 只发控制面:公开读端不带(少一次泄露面)。
  if (url.startsWith("/api/v1/") && adminAuth.token)
    headers.set("X-Platform-Admin-Token", adminAuth.token);
  const r = await fetch(url, { ...init, headers });
  if (!r.ok) {
    let code = "http_error";
    let message = r.statusText || "request failed";
    try {
      const body = await r.json();
      if (body && typeof body === "object") {
        if (typeof body.code === "string") code = body.code;
        if (typeof body.message === "string") message = body.message;
      }
    } catch {
      /* 非 JSON 错误体,保留状态行 */
    }
    throw new ApiError(url, r.status, code, message);
  }
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

/** ── 控制面(经 obs 同源代理 /api/v1/*) ── */
export const fetchEvaluations = () => j<{ evaluations: EvaluationRow[] }>("/api/v1/evaluations");
export const fetchWorkers = () => j<WorkersResp>("/api/v1/workers");
export const createEvaluation = (taskToken: string, projectId: string, idempotencyKey: string) =>
  j<EvaluationRow>("/api/v1/evaluations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      task_token: taskToken,
      project_id: projectId,
      idempotency_key: idempotencyKey || undefined,
    }),
  });

export const cancelEvaluation = (evaluationId: string) =>
  j<EvaluationRow>(`/api/v1/evaluations/${evaluationId}/cancel`, { method: "POST" });