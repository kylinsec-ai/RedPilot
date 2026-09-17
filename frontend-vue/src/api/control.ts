/**
 * 控制面端点(/api/v1/*),经 obs 同源代理转发到 control。需管理员凭据。
 */
import { ctl, unwrap } from "./client";
import type { EvaluationRow, WorkersResp } from "../types";

export const fetchEvaluations = () =>
  unwrap<{ evaluations: EvaluationRow[] }>(ctl.get("/api/v1/evaluations"));
export const fetchWorkers = () => unwrap<WorkersResp>(ctl.get("/api/v1/workers"));
export const createEvaluation = (taskToken: string, projectId: string, idempotencyKey: string) =>
  unwrap<EvaluationRow>(
    ctl.post("/api/v1/evaluations", {
      task_token: taskToken,
      project_id: projectId,
      idempotency_key: idempotencyKey || undefined,
    }),
  );
export const cancelEvaluation = (evaluationId: string) =>
  unwrap<EvaluationRow>(ctl.post(`/api/v1/evaluations/${evaluationId}/cancel`));
