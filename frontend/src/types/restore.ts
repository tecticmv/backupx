// Restore wizard (P2) types; mirrors the backend API contract.
import type { Snapshot } from "@/types/job";
import type { Task } from "@/types/task";

export type Engine = "mysql" | "mariadb" | "postgres" | "files";
export type PointType = "latest" | "snapshot" | "time";
/** DB step 3 (replace production is a later action on a ready restore). */
export type RestoreTarget = "scratch" | "download";
/** File restores (ZIP uses the existing snapshot_zip task). */
export type FileTarget = "original" | "alternate" | "scratch";

export type RestoreStatus =
  | "running"
  | "ready"
  | "failed"
  | "cancelled"
  | "removed"
  | "expired"
  | "replacing"
  | "replaced"
  | "undoing"
  | "undone"
  | "done";

export const WIZARD_STEPS = ["what", "when", "where", "run", "verify"] as const;
export type WizardStep = (typeof WIZARD_STEPS)[number];

/** Restic snapshot as returned with restore points (summary only on newer restic). */
export interface RestoreSnapshot extends Snapshot {
  short_id?: string;
  summary?: { total_bytes_processed?: number; [key: string]: unknown };
}

export interface VerifyConfig {
  key_tables: string[];
  check_sql: string;
  health_url: string;
  drill_count_only_tables?: string[];
}

export interface PitrWindow {
  from: string | null;
  to: string | null;
  anchored: boolean;
  reason: string;
}

export interface PitrGap {
  from: string;
  to: string;
  reason: string;
}

export interface RestorePoints {
  job_id: string;
  job_name: string;
  engine: Engine;
  backup_type: "database" | "filesystem";
  storages: { id: string | null; name: string; primary: boolean }[];
  storage_id: string | null;
  latest: {
    point_type: "time" | "snapshot";
    time: string | null;
    snapshot_id: string | null;
    label: string;
  } | null;
  pitr: {
    enabled: boolean;
    state: string;
    windows: PitrWindow[];
    gaps: PitrGap[];
    min: string | null;
    max: string | null;
  };
  snapshots: RestoreSnapshot[];
  snapshots_error: string | null;
  verify_config: VerifyConfig;
}

export interface RestorePlanRequest {
  job_id: string;
  point_type: PointType;
  snapshot_id?: string;
  target_time?: string;
  storage_id?: string | null;
  target: RestoreTarget;
  plan_hash?: string;
  /** Scratch restores: also compare row counts with production afterwards (reads production). Default false. */
  compare_production?: boolean;
}

export interface RestorePlan {
  job_id: string;
  job_name: string;
  engine: Engine;
  method: "dump" | "binlog" | "walg" | "files";
  point_type: PointType;
  target: RestoreTarget | FileTarget;
  target_time: string | null;
  storage: { id: string | null; name: string };
  dump: { snapshot_id: string; time: string; size: number | null } | null;
  binlogs: {
    files: number;
    segments: number;
    size: number;
    first: string;
    last: string;
    partial: boolean;
  } | null;
  wal: { base_backup: string; base_time: string; uncompressed_size: number | null } | null;
  summary: string;
  download_bytes: number;
  estimated_restored_bytes: number;
  disk: { path: string; free_bytes: number; needed_bytes: number; ok: boolean } | null;
  image: string | null;
  where: string;
  steps: string[];
  warnings: string[];
  /** Scratch database restores: whether the checks also read production row counts. */
  compare_production?: boolean;
}

export interface PlanResponse {
  plan: RestorePlan;
  plan_hash: string;
  ok: boolean;
  error?: string;
}

export interface Verify {
  checked_at: string;
  phase: "scratch" | "after_replace";
  /** False when production wasn't queried (copy-only counts); missing on checks made before this field. */
  production?: boolean;
  tables: {
    name: string;
    prod_estimate: number | null;
    restored_estimate: number | null;
    prod_exact: number | null;
    restored_exact: number | null;
    key: boolean;
    match: boolean | null;
  }[];
  prod_error: string | null;
  check_sql: { prod: string | null; restored: string | null; match: boolean | null; error: string | null } | null;
  health_url: { url: string; status: number | null; ok: boolean; error: string | null } | null;
  summary: {
    tables: number;
    key_tables: number;
    key_mismatches: number;
    /** tables counted exactly (key tables + small MySQL/MariaDB tables); missing on older checks */
    exact_tables?: number;
    exact_max_rows?: number | null;
  };
}

export interface Restore {
  id: string;
  job_id: string;
  job_name: string;
  engine: Engine;
  point_type: PointType;
  target_type: string;
  snapshot_id: string | null;
  target_time: string | null;
  storage_id: string | null;
  status: RestoreStatus;
  server: string;
  container: string | null;
  work_dir: string | null;
  connect_hint: string | null;
  plan: RestorePlan;
  task: Task | null;
  replace_task: Task | null;
  undo_task: Task | null;
  verify: Verify | null;
  pre_restore_snapshot: { storage_id: string | null; storage_name: string; snapshot_id: string }[] | null;
  pre_restore_expires_at: string | null;
  cleanup_at: string | null;
  can_replace: boolean;
  can_undo: boolean;
  error: string | null;
  created_at: string;
  updated_at: string | null;
  created_by: string | null;
  parent_id: string | null;
}

export interface ReplacePlan {
  restore_id: string;
  action: "replace" | "undo";
  job_id: string;
  job_name: string;
  engine: Engine;
  production: {
    database_config: string;
    host: string;
    container: string | null;
    databases: string[];
    path: "docker exec" | "direct" | "client image";
  };
  source: { container: string; point: string };
  steps: string[];
  downtime: string;
  pre_restore: string;
  rollback: string;
  warnings: string[];
}

export interface ReplacePreview {
  plan: ReplacePlan;
  plan_hash: string;
  confirm_text: string;
}

export interface FindMatch {
  path: string;
  type: "file" | "dir";
  size: number;
  mtime: string;
}

export interface FileRestoreRequest {
  job_id: string;
  snapshot_id: string;
  storage_id?: string | null;
  paths: string[];
  target: FileTarget;
  target_path?: string;
}

export interface FilePlanResponse {
  plan: RestorePlan & { paths?: string[]; target_path?: string | null };
  plan_hash: string;
  confirm_text?: string;
}

/** What task-starting restore routes answer (202), plus the error shapes we handle. */
export interface RestoreStartResponse {
  success?: boolean;
  task?: Task;
  restore?: Restore;
  deduplicated?: boolean;
  message?: string;
  error?: string;
  code?: string;
  plan?: RestorePlan;
  plan_hash?: string;
}
