export type AuditAction =
  | 'CREATE'
  | 'READ'
  | 'UPDATE'
  | 'DELETE'
  | 'LOGIN'
  | 'LOGOUT'
  | 'LOGIN_FAILED'
  | 'RUN_BACKUP'
  | 'BACKUP_COMPLETE'
  | 'BACKUP_FAILED';

export type AuditResourceType =
  | 'job'
  | 'server'
  | 's3_config'
  | 'db_config'
  | 'notification_channel'
  | 'session';

export type AuditStatus = 'success' | 'failure';

export interface AuditEntry {
  id: number;
  timestamp: string;
  user_id: string | null;
  user_name: string | null;
  action: AuditAction;
  resource_type: AuditResourceType;
  resource_id: string | null;
  resource_name: string | null;
  changes: string | null;
  ip_address: string | null;
  user_agent: string | null;
  status: AuditStatus;
  error_message: string | null;
}

export interface AuditLogResponse {
  logs: AuditEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface AuditStats {
  total: number;
  by_action: Record<string, number>;
  by_status: Record<string, number>;
  by_resource_type: Record<string, number>;
}

export interface AuditFilters {
  user_id?: string;
  action?: AuditAction;
  resource_type?: AuditResourceType;
  resource_id?: string;
  start_date?: string;
  end_date?: string;
  status?: AuditStatus;
}
