import type { PitrCheck, PitrDetails } from "@/types/job";
import type { Task } from "@/types/task";

// Enable-PITR wizard (P3 API contract): POST /api/jobs/:id/pitr/plan,
// POST /api/jobs/:id/pitr/apply, GET /api/jobs/:id/pitr/enable.

export const PITR_SETUP_STEPS = ["check", "plan", "apply", "restart", "stream", "anchor", "done"] as const;
export type PitrSetupStep = (typeof PITR_SETUP_STEPS)[number];

export type RestartMode = "now" | "at";

export interface PitrSetupTarget {
  database: string;
  server: string;
  install: "host" | "docker" | "managed" | "remote";
  flavor: "mariadb" | "mysql" | "postgres";
  version: string | null;
  service: string | null;
  container: string | null;
  config_method: "dropin" | "bind_mount" | "alter_system" | "conf_d" | "manual";
  config_path: string | null;
  managed_provider: string | null;
  restart_supported: boolean;
  restart_unsupported_reason: string | null;
}

export type PitrItemAction =
  | "dropin"
  | "sql"
  | "grant"
  | "job_field"
  | "client_charset"
  | "alter_system_reload"
  | "alter_system_restart"
  | "walg_install"
  | "manual";

export interface PitrSetupItem {
  id: string;
  check: string;
  status: "fail" | "warn";
  title: string;
  action: PitrItemAction;
  downtime: "none" | "restart";
  auto: boolean;
  needs_admin: boolean;
  default_selected: boolean;
  description: string;
  settings: Record<string, string> | null;
  sql: string[] | null;
  manual: { steps: string[]; commands: string[] } | null;
}

export interface PitrSetupAdmin {
  needed: boolean;
  socket_available: boolean;
  account: string | null;
  sql: string | null;
}

export interface PitrSetupSelection {
  items: string[];
  restart: RestartMode;
  restart_at: string | null;
  fresh_backup: "run" | "skip";
  last_backup_at: string | null;
}

export type PitrEnablePhase =
  | "apply"
  | "scheduled"
  | "restart"
  | "stream"
  | "anchor"
  | "done"
  | "failed"
  | "rolled_back"
  | "cancelled";

export interface PitrEnableResult {
  message?: string;
  state?: string;
  window_start?: string | null;
  window_end?: string | null;
  rolled_back?: string[] | string | null;
  [key: string]: unknown;
}

export interface PitrEnableRun {
  task: Task;
  restart_task: Task | null;
  phase: PitrEnablePhase;
  scheduled_for: string | null;
  active_task_id: string | null;
  result: PitrEnableResult | null;
}

export interface PitrEnableResponse {
  run: PitrEnableRun | null;
  pitr: PitrDetails | null;
}

export interface PitrPlanResponse {
  kind: "mysql" | "postgres";
  job: { id: string; name: string; pitr_enabled: boolean };
  target: PitrSetupTarget;
  checks: PitrCheck[];
  prereqs_ok: boolean;
  items: PitrSetupItem[];
  admin: PitrSetupAdmin;
  selection: PitrSetupSelection;
  steps: string[];
  downtime: string;
  needs_restart: boolean;
  plan: unknown;
  plan_hash: string;
  confirm_text: string;
  checked_at: string | null;
  enable_run: PitrEnableRun | null;
}

/** What the wizard sends to /plan when only the selection changed (and to /apply). */
export interface PitrSelectionRequest {
  items: string[];
  restart: RestartMode;
  restart_at?: string;
  fresh_backup: boolean;
}

export interface PitrPlanRequest extends Partial<PitrSelectionRequest> {
  refresh?: boolean;
}

export type PitrAdminCredential = null | { mode: "socket" } | { mode: "password"; username: string; password: string };

export interface PitrApplyRequest extends PitrSelectionRequest {
  plan_hash: string;
  confirm: string;
  admin: PitrAdminCredential;
}

/** Body of a non-2xx /apply answer (and of the 202 success). */
export interface PitrApplyResponse extends Partial<PitrPlanResponse> {
  success?: boolean;
  task?: Task;
  message?: string;
  error?: string;
  code?:
    | "plan_hash_missing"
    | "confirmation_mismatch"
    | "invalid_items"
    | "admin_required"
    | "invalid_restart_at"
    | "stale_check"
    | "plan_changed"
    | "busy"
    | "step_up_required"
    | string;
}
