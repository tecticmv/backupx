export interface Job {
  name: string;
  server_id: string;
  // Legacy fields for backwards compatibility (populated from server)
  remote_host?: string;
  ssh_port?: number;
  ssh_key?: string;
  directories: string[];
  excludes: string[];
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

export interface JobFormData {
  job_id: string;
  name: string;
  server_id: string;
  directories: string;
  excludes: string;
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
