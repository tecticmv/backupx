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
  // S3 storage config
  s3_config_id: string;
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
  created_at: string;
  updated_at?: string;
  last_run?: string;
  last_success?: string;
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
  // S3 storage config
  s3_config_id: string;
  restic_password: string;
  backup_prefix: string;
  schedule_enabled: boolean;
  schedule_cron: string;
  retention_hourly: number;
  retention_daily: number;
  retention_weekly: number;
  retention_monthly: number;
  timeout: number;
}
