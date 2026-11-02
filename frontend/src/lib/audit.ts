/**
 * Every audit action the backend writes, grouped for the Audit Log filter.
 * Keep in sync with app/audit/logger.py (AuditLogger.ACTION_*) and the
 * `_audit(...)` / `audit_logger.log(action=...)` calls in app/ (main.py,
 * restore_service.py, pitr_enable_service.py, health_service.py).
 * Values are matched exactly by the API, so case matters.
 */
export const AUDIT_ACTION_GROUPS = [
  {
    label: "Changes",
    actions: [
      { value: "CREATE", label: "Create" },
      { value: "UPDATE", label: "Update" },
      { value: "DELETE", label: "Delete" },
      { value: "READ", label: "Read" },
      { value: "reveal", label: "Reveal secret" },
    ],
  },
  {
    label: "Sign-in",
    actions: [
      { value: "LOGIN", label: "Login" },
      { value: "LOGOUT", label: "Logout" },
      { value: "LOGIN_FAILED", label: "Login failed" },
      { value: "STEP_UP", label: "Password re-entry" },
      { value: "STEP_UP_FAILED", label: "Password re-entry failed" },
    ],
  },
  {
    label: "Backups",
    actions: [
      { value: "RUN_BACKUP", label: "Run backup" },
      { value: "BACKUP_COMPLETE", label: "Backup complete" },
      { value: "BACKUP_FAILED", label: "Backup failed" },
      { value: "CANCEL_TASK", label: "Cancel task" },
    ],
  },
  {
    label: "Restore",
    actions: [
      { value: "RESTORE_START", label: "Restore started" },
      { value: "RESTORE_SCRATCH", label: "Scratch restore" },
      { value: "RESTORE_EXTEND", label: "Extend scratch copy" },
      { value: "RESTORE_REMOVE", label: "Remove scratch copy" },
      { value: "RESTORE_REPLACE", label: "Replace production" },
      { value: "RESTORE_UNDO", label: "Undo replace" },
      { value: "RESTORE_FILES", label: "Restore files" },
      { value: "RESTORE_VERIFY_CONFIG", label: "Restore drill settings" },
      { value: "DOWNLOAD_ZIP", label: "Download ZIP" },
    ],
  },
  {
    label: "Point-in-time recovery",
    actions: [
      { value: "PITR_ENABLE", label: "Enable PITR" },
      { value: "pitr_restore_script", label: "PITR restore script" },
    ],
  },
  {
    label: "Health",
    actions: [
      { value: "HEALTH_FIX", label: "Fix started" },
      { value: "HEALTH_FIX_DONE", label: "Fix finished" },
      { value: "HEALTH_FIX_ALL", label: "Fix all" },
      { value: "HEALTH_SNOOZE", label: "Snooze" },
      { value: "HEALTH_UNSNOOZE", label: "Unsnooze" },
    ],
  },
] as const;

export type AuditAction = (typeof AUDIT_ACTION_GROUPS)[number]["actions"][number]["value"];

export const AUDIT_RESOURCE_TYPES = [
  { value: "job", label: "Jobs" },
  { value: "server", label: "Servers" },
  { value: "s3_config", label: "S3 Storage" },
  { value: "db_config", label: "Databases" },
  { value: "notification_channel", label: "Notifications" },
  { value: "session", label: "Sessions" },
  { value: "task", label: "Tasks" },
  { value: "user", label: "Users" },
  { value: "system", label: "System" },
] as const;

export type AuditResourceType = (typeof AUDIT_RESOURCE_TYPES)[number]["value"];

/** Badge variant: failures and production-changing actions stand out. */
export function auditActionVariant(action: string): "default" | "secondary" | "destructive" | "outline" {
  if (action.endsWith("_FAILED") || action === "DELETE" || action === "RESTORE_REPLACE") return "destructive";
  if (action === "UPDATE" || action === "RESTORE_UNDO") return "secondary";
  if (action === "LOGOUT" || action === "READ") return "outline";
  return "default";
}
