export type TaskStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "rolled_back";

export type TaskTier = "safe" | "confirm" | "maintenance";

export interface TaskResult {
  message?: string;
  download_url?: string;
  filename?: string;
  size?: number;
  run_id?: string;
  [key: string]: unknown;
}

export interface Task {
  id: string;
  kind: string;
  title: string;
  parent_id: string | null;
  resource_type: string | null;
  resource_id: string | null;
  params: Record<string, unknown>;
  tier: TaskTier;
  status: TaskStatus;
  progress: number;
  step: string | null;
  cancel_requested: boolean;
  cancellable: boolean;
  created_by: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  result: TaskResult | null;
  error: string | null;
  log_seq: number;
}

export interface TaskLogLine {
  seq: number;
  ts: string | null;
  stream: "stdout" | "stderr" | "system" | string;
  line: string;
}

/** What POST endpoints that start a task return (HTTP 202). */
export interface TaskStartResponse {
  success: boolean;
  task: Task;
  deduplicated: boolean;
  message?: string;
  error?: string;
}

export const ACTIVE_TASK_STATUSES: TaskStatus[] = ["queued", "running"];

export function isActiveTask(task: Pick<Task, "status">): boolean {
  return ACTIVE_TASK_STATUSES.includes(task.status);
}
