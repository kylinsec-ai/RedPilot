/**
 * API 契约类型 —— 字段名逐字对齐 Python(redpilot/worker/roster.py 的题目总览、
 * redpilot/worker/live/ 的状态快照与 SSE 信封、redpilot/obs/schema.py 的 runs 行),
 * 改键名必须同步后端。
 *
 * 本文件是后端契约的手工镜像:单源在 Python 侧(redpilot/contracts/vocabulary.py /
 * redpilot/obs/schema.py),此处只做 TS 侧声明。
 */

export const ACTIVE_PHASES: readonly string[] = [
  "starting",
  "solving",
  "submitting",
  "closing",
];

/** /api/events 与 /api/status 快照 */
export interface LiveSnap {
  phase: string;
  challenge_code: string;
  turns?: number;
  kind?: string;
  ts?: number;
  updated_at?: number; // LiveState 每次 update 刷新(epoch s);跨传输版本守卫用
}

/** 单题目录本地痕迹(roster.py scan_local_dir,flag 是布尔:FLAG 文件存在与否) */
export interface LocalTrace {
  dir: string;
  flag: boolean;
  transcript_bytes: number;
  transcript_mtime: number;
  crashed: boolean;
  artifacts: string[];
  last_activity: number;
}

export interface ChallengeRow {
  unique_code: string;
  difficulty: string;
  level: number;
  total_score: number;
  flag_count: number;
  correct_flag_count: number;
  is_completed: boolean;
  container_status: string;
  container_addr: string[];
  description: string;
  local_only?: boolean;
  local?: LocalTrace;
}

export interface RosterSnapshot {
  fetched_at: number;
  stale: boolean;
  platform_error: string;
  platform_disabled: boolean;
  challenges: Record<string, ChallengeRow>;
}

/** /api/challenge:平台行合并 local + flags(仅本地题时平台字段可缺省) */
export interface ChallengeDetail {
  unique_code: string;
  flags: string[];
  difficulty?: string;
  level?: number;
  total_score?: number;
  flag_count?: number;
  correct_flag_count?: number;
  is_completed?: boolean;
  container_status?: string;
  container_addr?: string[];
  description?: string;
  local_only?: boolean;
  local?: LocalTrace;
}

/** 服务端 digest 不可用时返回空 meta({})——字段全部可缺省 */
export interface TimelineMeta {
  sessions?: number;
  agent_ends?: number;
  truncated?: boolean;
  dropped?: boolean;
  abrupt?: boolean;
  live?: boolean;
  bytes?: number;
  unparsed?: number;
}

export interface TimelineResp {
  next_seq: number;
  meta: TimelineMeta;
  entries: TimelineEntry[];
}

/** 条目基座 {seq, kind, t(毫秒!), turn}; _add 后再按 kind 补字段 */
interface TimelineEntryBase {
  seq: number;
  t?: number;
  turn: number | null;
}
export interface SessionEntry extends TimelineEntryBase {
  kind: "session";
  sid?: string;
  note?: string;
}
export interface TurnEntry extends TimelineEntryBase {
  kind: "turn";
  n: number;
  tokens?: number;
  stop?: string;
}
export interface AttemptEntry extends TimelineEntryBase {
  kind: "attempt";
  n?: number;
}
export interface ToolEntry extends TimelineEntryBase {
  kind: "tool";
  tool?: string;
  cmd?: string;
  id?: string;
  err?: boolean;
  out?: string;
  out_len?: number;
}
export interface TextEntry extends TimelineEntryBase {
  kind: "text";
  text?: string;
  more?: boolean;
}
export interface NoteEntry extends TimelineEntryBase {
  kind: "note";
  note?: string;
}
export interface ErrorEntry extends TimelineEntryBase {
  kind: "error";
  note?: string;
}
export type TimelineEntry =
  | SessionEntry
  | TurnEntry
  | AttemptEntry
  | ToolEntry
  | TextEntry
  | NoteEntry
  | ErrorEntry;

/** ── Runs 历史(/api/runs 系,obs 后端新增;run_id=32hex,status 对齐 SQL CHECK)── */

export const RUN_STATUSES = [
  "running",
  "solved",
  "done",
  "failed",
  "interrupted",
] as const;
export type RunStatus = (typeof RUN_STATUSES)[number];

export const RUN_STATUS_LABEL: Record<string, string> = {
  running: "进行中",
  solved: "已解出",
  done: "已结束",
  failed: "失败",
  interrupted: "中断",
};

export interface RunRow {
  run_id: string;
  worker_id: string;
  challenge_code: string;
  model?: string;
  status: RunStatus | string; // 宽松以容忍未来状态
  evaluation_id?: string | null;
  job_id?: string | null;
  attempt_id?: string | null;
  started_at: number; // epoch s
  ended_at?: number | null;
  duration_s?: number | null;
  error?: string | null;
  turns?: number | null;
  sessions?: number | null;
  flags_found?: number | null;
  flags_accepted?: string[]; // 后端已归一(无 = [])
  updated_at?: number;
  event_count: number;
}

export interface RunsListResp {
  runs: RunRow[];
}

export const EVALUATION_STATUSES = [
  "queued",
  "running",
  "completed",
  "canceled",
  "expired",
] as const;
export type EvaluationStatus = (typeof EVALUATION_STATUSES)[number];

export const WORKER_STATUSES = ["offline", "idle", "busy", "draining"] as const;
export type WorkerStatus = (typeof WORKER_STATUSES)[number];




export interface RunEventRow {
  seq: number;
  type: string;
  payload: string;
}

export interface RunEventsResp {
  events: RunEventRow[];
  next_seq: number;
  end: boolean;
}
