import { apiJson } from "@/lib/api";
import type { Task } from "@/types/task";
import type {
  PitrApplyRequest,
  PitrEnablePhase,
  PitrEnableResponse,
  PitrEnableRun,
  PitrPlanRequest,
  PitrPlanResponse,
  PitrSetupStep,
} from "@/types/pitrSetup";

const enc = encodeURIComponent;

/** Prerequisite check + host probe + remediation plan (can take ~2 min with refresh). */
export function planPitr(jobId: string, body: PitrPlanRequest, signal?: AbortSignal) {
  return apiJson<PitrPlanResponse>(`/api/jobs/${enc(jobId)}/pitr/plan`, { method: "POST", json: body, signal });
}

/** Raw fetch so the caller can run it through useStepUp().withStepUp and read 409 bodies. */
export function applyPitr(jobId: string, body: PitrApplyRequest) {
  return fetch(`/api/jobs/${enc(jobId)}/pitr/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function getPitrEnable(jobId: string, signal?: AbortSignal) {
  return apiJson<PitrEnableResponse>(`/api/jobs/${enc(jobId)}/pitr/enable`, { signal });
}

/** Phases in which the run still needs watching. */
export const ACTIVE_PHASES: PitrEnablePhase[] = ["apply", "scheduled", "restart", "stream", "anchor"];
export const FAILED_PHASES: PitrEnablePhase[] = ["failed", "rolled_back", "cancelled"];

export function isRunActive(run: PitrEnableRun | null | undefined): boolean {
  return !!run && ACTIVE_PHASES.includes(run.phase);
}

/** Wizard tab of a task step ("Restart: stopping mariadb" -> restart). */
export function stepFromTaskStep(step: string | null | undefined): PitrSetupStep | null {
  if (!step) return null;
  const prefix = step.split(":", 1)[0].trim().toLowerCase();
  if (prefix === "apply" || prefix === "restart" || prefix === "stream" || prefix === "anchor") return prefix;
  if (prefix === "done") return "done";
  return null;
}

/** The task whose progress/log to show now. */
export function activeRunTask(run: PitrEnableRun): Task {
  if (run.active_task_id) {
    if (run.restart_task?.id === run.active_task_id) return run.restart_task;
    if (run.task.id === run.active_task_id) return run.task;
  }
  if (run.restart_task && run.phase !== "apply") return run.restart_task;
  return run.task;
}

/** Wizard tab a run belongs on. Failed runs stay on the tab of the step that failed. */
export function stepForRun(run: PitrEnableRun): PitrSetupStep {
  switch (run.phase) {
    case "apply":
      return "apply";
    case "scheduled":
    case "restart":
      return "restart";
    case "stream":
      return "stream";
    case "anchor":
      return "anchor";
    case "done":
      return "done";
    default: {
      const fromStep = stepFromTaskStep(activeRunTask(run).step);
      return fromStep && fromStep !== "done" ? fromStep : "apply";
    }
  }
}

const pad = (n: number) => String(n).padStart(2, "0");

/** "HH:MM" of an epoch-ms value in local time. */
export function localTimeValue(ms: number) {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Tomorrow 02:00 local: a sensible default for a restart window. */
export function defaultWindowMs() {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(2, 0, 0, 0);
  return d.getTime();
}
