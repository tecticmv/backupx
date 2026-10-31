export interface Job {
  name: string;
  backup_type: "filesystem" | "database";
  server_id: string;
  // Legacy fields for backwards compatibility (populated from server)
  remote_host?: string;
  ssh_port?: number;
  ssh_key?: string;
  // Filesystem backup fields
  directories: string[];
  excludes: string[];
  // Database backup fields
  database_config_id?: string;
  // S3 storage config: primary storage, plus every storage the job backs up to (primary first)
  s3_config_id: string;
  s3_config_ids: string[];
  // Legacy fields for backwards compatibility (populated from s3 config)
  s3_endpoint?: string;
  s3_bucket?: string;
  s3_access_key?: string;
  s3_secret_key?: string;
  restic_password: string;
  backup_prefix: string;
  schedule_enabled: boolean;
  schedule_cron: string;
  retention_hourly: number;
  retention_daily: number;
  retention_weekly: number;
  retention_monthly: number;
  timeout: number;
  status: "pending" | "running" | "success" | "failed" | "timeout" | "error";
  progress?: number;
  progress_message?: string;
  created_at: string;
  updated_at?: string;
  last_run?: string;
  last_success?: string;
  // Scheduled job that hasn't started a run since its last expected fire time (+ grace)
  overdue?: boolean;
  overdue_since?: string | null;
  // Latest retention forget/prune result per storage
  retention_runs?: RetentionRun[];
  // Point-in-time recovery (database jobs)
  pitr_enabled?: boolean;
  pitr_interval?: number;
  pitr_retention_days?: number;
  pitr_client_image?: string | null;
  pitr?: PitrSummary | null;
}

export type PitrState = "disabled" | "pending" | "ok" | "lagging" | "broken" | "unanchored";

export interface PitrSummary {
  enabled: boolean;
  state: PitrState;
  window_start: string | null;
  window_end: string | null;
  lag_minutes: number | null;
  last_error: string | null;
}

export interface PitrCheck {
  check: string;
  status: "ok" | "warn" | "fail";
  current: string;
  required: string;
  fix: string;
  needs_restart: boolean;
}

export interface PitrCheckResult {
  kind: "mysql" | "postgres";
  checks: PitrCheck[];
  ok: boolean;
  needs_restart: boolean;
  suggested_client_image?: string | null;
  postgresql_conf?: string[];
}

export interface PitrDetails extends PitrSummary {
  state_since: string | null;
  last_tick_at: string | null;
  last_ship_at: string | null;
  streamer_active: string | null;
  server_version: string | null;
  kind: "mysql" | "postgres" | null;
  chains: { chain_id: number; start_seq: number | null; reason: string; started_at: string | null }[];
  windows: { chain_id: number; from: string | null; to: string | null; anchored: boolean; reason: string }[];
  anchors: { id: number; time: string; binlog_file: string; binlog_pos: number; storages: string[]; forgotten: boolean }[];
  segments: { id: number; chain_id: number; first_file: string; last_file: string; end_time: string; size: number; forgotten: boolean }[];
}

export interface RetentionRun {
  storage_id: string;
  storage_name: string;
  action: "forget" | "prune" | "pitr-forget" | "pitr-prune";
  status: "success" | "failed" | "skipped";
  dry_run: boolean;
  snapshots_removed: number | null;
  bytes_freed: number | null;
  message: string;
  timestamp: string;
}

export interface RetentionSettings {
  enabled: boolean;
  dry_run: boolean;
  prune_cron: string;
  prune_max_unused: string;
}

export interface Jobs {
  [key: string]: Job;
}

export interface HistoryEntry {
  timestamp: string;
  job_id: string;
  job_name: string;
  status: "success" | "failed" | "timeout" | "error";
  message: string;
  duration: number;
}

export interface JobStorage {
  id: string | null;
  name: string;
  endpoint: string;
  bucket: string;
  status: "active" | "inactive";
  primary: boolean;
}

export interface Snapshot {
  id: string;
  time: string;
  hostname: string;
  paths: string[];
  tags: string[];
}

export interface RepoStats {
  total_size: number;
  total_file_count: number;
}

export interface SnapshotFile {
  name: string;
  path: string;
  type: "file" | "dir";
  size: number;
  mtime: string;
}

export interface SnapshotFilesResponse {
  files: SnapshotFile[];
  path: string;
  snapshot_id: string;
}

export interface JobFormData {
  job_id: string;
  name: string;
  backup_type: "filesystem" | "database";
  server_id: string;
  // Filesystem backup fields
  directories: string;
  excludes: string;
  // Database backup fields
  database_config_id: string;
  // S3 storages to back up to, primary first
  s3_config_ids: string[];
  restic_password: string;
  backup_prefix: string;
  schedule_enabled: boolean;
  schedule_cron: string;
  retention_hourly: number;
  retention_daily: number;
  retention_weekly: number;
  retention_monthly: number;
  timeout: number;
  pitr_enabled: boolean;
  pitr_interval: number;
  pitr_retention_days: number;
  pitr_client_image: string;
}
