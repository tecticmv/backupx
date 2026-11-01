import type { Task } from "@/types/task";

// Health checks, fixes and the dashboard overview (P1 API contract).

export type Severity = "critical" | "warning" | "info";
export type FixTier = "safe" | "confirm" | "manual";

export interface IssueResource {
  type: "job" | "server" | "storage" | "channel" | "system";
  id: string;
  name: string;
  job_id?: string;
  job_name?: string;
  storage_id?: string;
  storage_name?: string;
  server_id?: string;
}

export interface IssueFix {
  action: string;
  tier: FixTier;
  label: string;
  description: string;
}

export type EvidenceValue = string | number | boolean | null;

export interface Issue {
  id: string;
  kind: string;
  title: string;
  explanation: string;
  severity: Severity;
  resource: IssueResource;
  first_seen: string;
  last_seen: string;
  snoozed_until: string | null;
  evidence: Record<string, EvidenceValue>;
  evidence_text?: string | null;
  fix: IssueFix | null;
  manual?: { steps: string[]; commands: string[] } | null;
  active_task?: Task | null;
}

export interface HealthSummary {
  status: "ok" | "warning" | "critical";
  total: number;
  critical: number;
  warning: number;
  info: number;
  snoozed: number;
  safe_fixable: number;
  last_sweep_at: string | null;
  sweep_running: boolean;
}

export interface HealthResponse {
  summary: HealthSummary;
  issues: Issue[];
}

export interface HealthQuery {
  severity?: Severity;
  resource_type?: "job" | "server" | "storage";
  resource_id?: string;
  include_snoozed?: boolean;
}

export interface Plan {
  action: string;
  title: string;
  steps: string[];
  downtime: string;
  details: Record<string, string | number | boolean | null | string[]>;
  warnings?: string[];
}

export interface FixPreviewResponse {
  issue_id: string;
  fix: IssueFix;
  plan: Plan;
  plan_hash: string;
  confirm_text?: string;
}

/** Body of a 409 plan_changed error. */
export interface PlanChangedError {
  code: "plan_changed";
  plan: Plan;
  plan_hash: string;
  confirm_text?: string;
}

export interface SafeFixPreviewItem {
  issue_id: string;
  action: string;
  label: string;
  title: string;
  resource: IssueResource;
}

export interface SafeFixPreviewResponse {
  count: number;
  fixes: SafeFixPreviewItem[];
}

// --- Dashboard overview -------------------------------------------------------

export type StorageRunStatus = "success" | "failed" | "not_run" | "timeout" | "error";

export interface JobCardStorage {
  id: string;
  name: string;
  status: StorageRunStatus | null;
  finished_at: string | null;
  error: string | null;
}

export interface JobCardPitr {
  enabled: boolean;
  state: string;
  window_start: string | null;
  window_end: string | null;
  lag_minutes: number | null;
  last_error?: string | null;
}

export interface JobCardData {
  id: string;
  name: string;
  backup_type: "filesystem" | "database";
  server_name: string | null;
  status: string;
  running: boolean;
  schedule_enabled: boolean;
  schedule_cron: string | null;
  next_run: string | null;
  last_run: string | null;
  last_success: string | null;
  overdue_since: string | null;
  issues: { critical: number; warning: number; info: number };
  storages: JobCardStorage[];
  last_run_id: string | null;
  failed_storage_ids: string[];
  pitr: JobCardPitr | null;
  retention: {
    state: "ok" | "failed" | "skipped" | "off" | "never";
    last_at: string | null;
    message: string | null;
  };
}

export interface ActivityEvent {
  ts: string;
  type: "backup" | "retention" | "task" | "pitr" | "audit";
  status: "success" | "failed" | "warning" | "info" | "running";
  title: string;
  detail: string | null;
  job_id?: string | null;
  task_id?: string | null;
}

export interface StorageUsage {
  storage_id: string;
  storage_name: string;
  job_id: string;
  job_name: string;
  bytes: number | null;
  files: number | null;
  snapshots: number | null;
  checked_at: string | null;
}

export interface DashboardOverview {
  jobs: JobCardData[];
  activity: ActivityEvent[];
  storage_usage: StorageUsage[];
  storage_usage_checked_at: string | null;
}

// --- Job runs (existing P0 endpoints) ----------------------------------------

export interface RunStorageResult {
  storage_id: string;
  storage_name: string | null;
  status: StorageRunStatus;
  exit_code: number | null;
  snapshot_id: string | null;
  error: string | null;
  has_log?: boolean;
}

export interface JobRun {
  run_id: string;
  job_id?: string;
  status: "success" | "partial" | "failed";
  trigger: string | null;
  started_at: string | null;
  finished_at: string | null;
  task_id: string | null;
  storages: RunStorageResult[];
  failed_storage_ids: string[];
}

export interface RunLog {
  run_id?: string;
  storage_id?: string;
  content: string;
  truncated: boolean;
}
