/**
 * 观测读端端点(/api/*)。读端返回明文 flag 与完整 agent 实录 → 全部需凭据。
 */
import { obs, unwrap } from "./client";
import type {
  ChallengeDetail,
  LiveSnap,
  RosterSnapshot,
  RunEventsResp,
  RunRow,
  RunsListResp,
  TimelineResp,
} from "../types";

export const fetchRoster = () => unwrap<RosterSnapshot>(obs.get("/api/roster"));
export const fetchStatus = () => unwrap<LiveSnap>(obs.get("/api/status"));
export const fetchChallenge = (code: string) =>
  unwrap<ChallengeDetail>(obs.get("/api/challenge", { params: { code } }));
export const fetchTimeline = (code: string, after: number) =>
  unwrap<TimelineResp>(obs.get("/api/timeline", { params: { code, after } }));
export const fetchTranscript = (code: string, tail = 200) =>
  unwrap<{ code: string; lines: string[] }>(obs.get("/api/transcript", { params: { code, tail } }));

/** ── Runs 历史 ── */
export const fetchRuns = (qs = "") => unwrap<RunsListResp>(obs.get("/api/runs" + qs));
export const fetchRun = (runId: string) => unwrap<RunRow>(obs.get(`/api/runs/${runId}`));
export const fetchRunEvents = (runId: string, after = 0, limit = 500) =>
  unwrap<RunEventsResp>(obs.get(`/api/runs/${runId}/events`, { params: { after, limit } }));
export const fetchRunTimeline = (runId: string, after = 0) =>
  unwrap<TimelineResp>(obs.get(`/api/runs/${runId}/timeline`, { params: { after } }));
