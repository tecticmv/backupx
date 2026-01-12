"""
Audit logger implementation.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Sensitive fields that should be redacted in audit logs
SENSITIVE_FIELDS = {
    'password', 'secret_key', 'access_key', 'api_key', 'restic_password',
    's3_secret_key', 's3_access_key', 'agent_api_key', 'smtp_password',
    'token', 'secret', 'credential', 'private_key', 'ssh_key_content'
}

# Global audit logger instance
_audit_logger: Optional['AuditLogger'] = None


class AuditLogger:
    """
    Audit logger for tracking operations in BackupX.

    Logs:
    - Authentication events (login, logout)
    - CRUD operations on all resources
    - Backup job executions
    """

    # Action constants
    ACTION_CREATE = 'CREATE'
    ACTION_READ = 'READ'
    ACTION_UPDATE = 'UPDATE'
    ACTION_DELETE = 'DELETE'
    ACTION_LOGIN = 'LOGIN'
    ACTION_LOGOUT = 'LOGOUT'
    ACTION_LOGIN_FAILED = 'LOGIN_FAILED'
    ACTION_RUN_BACKUP = 'RUN_BACKUP'
    ACTION_BACKUP_COMPLETE = 'BACKUP_COMPLETE'
    ACTION_BACKUP_FAILED = 'BACKUP_FAILED'

    # Resource type constants
    RESOURCE_SESSION = 'session'
    RESOURCE_JOB = 'job'
    RESOURCE_SERVER = 'server'
    RESOURCE_S3_CONFIG = 's3_config'
    RESOURCE_DB_CONFIG = 'db_config'
    RESOURCE_NOTIFICATION = 'notification_channel'

    def __init__(self, db_backend, enabled: bool = True, log_reads: bool = False):
        """
        Initialize audit logger.

        Args:
            db_backend: Database backend instance
            enabled: Whether audit logging is enabled
            log_reads: Whether to log READ operations (verbose)
        """
        self.db = db_backend
        self.enabled = enabled
        self.log_reads = log_reads

    def log(
        self,
        action: str,
        resource_type: str,
        resource_id: Optional[str] = None,
        resource_name: Optional[str] = None,
        old_value: Optional[Dict] = None,
        new_value: Optional[Dict] = None,
        user_id: Optional[str] = None,
        user_name: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        status: str = 'success',
        error_message: Optional[str] = None
    ) -> Optional[int]:
        """
        Log an audit event.

        Args:
            action: Action performed (CREATE, READ, UPDATE, DELETE, LOGIN, etc.)
            resource_type: Type of resource (job, server, s3_config, etc.)
            resource_id: ID of the resource (optional)
            resource_name: Human-readable name of the resource (optional)
            old_value: Previous value for UPDATE/DELETE operations
            new_value: New value for CREATE/UPDATE operations
            user_id: ID of the user performing the action
            user_name: Username of the user performing the action
            ip_address: IP address of the request
            user_agent: User agent string
            status: 'success' or 'failure'
            error_message: Error message if status is 'failure'

        Returns:
            ID of the created audit log entry, or None if logging is disabled
        """
        if not self.enabled:
            return None

        # Skip READ operations unless explicitly enabled
        if action == self.ACTION_READ and not self.log_reads:
            return None

        # Compute changes for UPDATE operations
        changes = None
        if action == self.ACTION_UPDATE and old_value and new_value:
            changes = self._compute_changes(old_value, new_value)
        elif action == self.ACTION_CREATE and new_value:
            changes = json.dumps(self._redact_sensitive(new_value))
        elif action == self.ACTION_DELETE and old_value:
            changes = json.dumps({'deleted': self._redact_sensitive(old_value)})

        timestamp = datetime.now().isoformat()

        try:
            self.db.execute('''
                INSERT INTO audit_log (timestamp, user_id, user_name, action, resource_type,
                    resource_id, resource_name, changes, ip_address, user_agent, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (timestamp, user_id, user_name, action, resource_type,
                  resource_id, resource_name, changes, ip_address, user_agent, status, error_message))
            self.db.commit()

            logger.debug(f"Audit: {action} {resource_type} {resource_id} by {user_name} - {status}")
            return True

        except Exception as e:
            logger.error(f"Failed to write audit log: {e}")
            return None

    def _compute_changes(self, old_value: Dict, new_value: Dict) -> str:
        """
        Compute the diff between old and new values.

        Args:
            old_value: Previous value dictionary
            new_value: New value dictionary

        Returns:
            JSON string of changes with sensitive fields redacted
        """
        changes = {}

        # Find all keys
        all_keys = set(old_value.keys()) | set(new_value.keys())

        for key in all_keys:
            old_val = old_value.get(key)
            new_val = new_value.get(key)

            if old_val != new_val:
                # Redact sensitive fields
                if key.lower() in SENSITIVE_FIELDS:
                    changes[key] = {
                        'old': '[REDACTED]' if old_val else None,
                        'new': '[REDACTED]' if new_val else None
                    }
                else:
                    changes[key] = {
                        'old': old_val,
                        'new': new_val
                    }

        return json.dumps(changes) if changes else None

    def _redact_sensitive(self, data: Dict) -> Dict:
        """
        Redact sensitive fields from a dictionary.

        Args:
            data: Dictionary to redact

        Returns:
            New dictionary with sensitive fields replaced with '[REDACTED]'
        """
        if not data:
            return data

        redacted = {}
        for key, value in data.items():
            if key.lower() in SENSITIVE_FIELDS:
                redacted[key] = '[REDACTED]' if value else None
            elif isinstance(value, dict):
                redacted[key] = self._redact_sensitive(value)
            else:
                redacted[key] = value

        return redacted

    def get_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get audit log entries with filtering.

        Args:
            limit: Maximum number of entries to return
            offset: Number of entries to skip
            user_id: Filter by user ID
            action: Filter by action
            resource_type: Filter by resource type
            resource_id: Filter by resource ID
            start_date: Filter by start date (ISO format)
            end_date: Filter by end date (ISO format)
            status: Filter by status

        Returns:
            List of audit log entries
        """
        query = "SELECT * FROM audit_log WHERE 1=1"
        params = []

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if action:
            query += " AND action = ?"
            params.append(action)

        if resource_type:
            query += " AND resource_type = ?"
            params.append(resource_type)

        if resource_id:
            query += " AND resource_id = ?"
            params.append(resource_id)

        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)

        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date)

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        return self.db.fetchall(query, tuple(params))

    def get_log_count(
        self,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        status: Optional[str] = None
    ) -> int:
        """Get total count of audit logs matching filters."""
        query = "SELECT COUNT(*) as count FROM audit_log WHERE 1=1"
        params = []

        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)

        if action:
            query += " AND action = ?"
            params.append(action)

        if resource_type:
            query += " AND resource_type = ?"
            params.append(resource_type)

        if start_date:
            query += " AND timestamp >= ?"
            params.append(start_date)

        if end_date:
            query += " AND timestamp <= ?"
            params.append(end_date)

        if status:
            query += " AND status = ?"
            params.append(status)

        result = self.db.fetchone(query, tuple(params))
        return result['count'] if result else 0

    def cleanup(self, retention_days: int = 90) -> int:
        """
        Delete audit logs older than retention period.

        Args:
            retention_days: Number of days to retain logs

        Returns:
            Number of deleted entries
        """
        from datetime import timedelta

        cutoff_date = (datetime.now() - timedelta(days=retention_days)).isoformat()

        # Get count before deletion
        count_result = self.db.fetchone(
            "SELECT COUNT(*) as count FROM audit_log WHERE timestamp < ?",
            (cutoff_date,)
        )
        count = count_result['count'] if count_result else 0

        if count > 0:
            self.db.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff_date,))
            self.db.commit()
            logger.info(f"Cleaned up {count} audit log entries older than {retention_days} days")

        return count

    def export(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        format: str = 'json'
    ) -> str:
        """
        Export audit logs for archival.

        Args:
            start_date: Start date filter (ISO format)
            end_date: End date filter (ISO format)
            format: Export format ('json' or 'csv')

        Returns:
            Exported data as string
        """
        logs = self.get_logs(
            limit=100000,  # Large limit for export
            start_date=start_date,
            end_date=end_date
        )

        if format == 'csv':
            import csv
            import io

            output = io.StringIO()
            if logs:
                writer = csv.DictWriter(output, fieldnames=logs[0].keys())
                writer.writeheader()
                writer.writerows(logs)
            return output.getvalue()

        return json.dumps(logs, indent=2, default=str)


def get_audit_logger() -> Optional[AuditLogger]:
    """Get the global audit logger instance."""
    return _audit_logger


def init_audit_logger(db_backend) -> AuditLogger:
    """
    Initialize the global audit logger.

    Args:
        db_backend: Database backend instance

    Returns:
        AuditLogger instance
    """
    global _audit_logger

    enabled = os.environ.get('AUDIT_ENABLED', 'true').lower() in ('true', '1', 'yes')
    log_reads = os.environ.get('AUDIT_LOG_READS', 'false').lower() in ('true', '1', 'yes')

    _audit_logger = AuditLogger(db_backend, enabled=enabled, log_reads=log_reads)
    logger.info(f"Audit logger initialized (enabled={enabled}, log_reads={log_reads})")

    return _audit_logger
